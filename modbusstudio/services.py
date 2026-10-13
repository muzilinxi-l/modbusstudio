"""
    Modbus Studio 业务服务层模块，为 UI 视图层提供日志双缓冲、点位校验编解码、从机生命周期与主机轮询统一入口。
    关键导入：csv/queue/time/datetime（线程安全日志队列与 CSV 归档）、models/modbus_codec/modbus_engine（领域模型、编解码与通信引擎）。
对外暴露：LoggingService（日志服务）、PointService（点位校验编解码服务）、SlaveService（从机管理服务）、PollService（主机轮询服务）。
"""

from __future__ import annotations
from datetime import datetime
import csv
import logging
import os
import queue
import time

from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# 导入领域层标准枚举、配置值对象、点位实体、从机聚合根与配额上限常量
from .models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    LogEntry,
    MAX_RTU_SLAVE_INSTANCES,
    MAX_TCP_SLAVE_INSTANCES,
    ModbusPoint,
    PollResult,
    PollTask,
    SimRule,
    SlaveDevice,
)
# 导入底层纯函数编解码器，用于在业务层预生成或重算点位的原始十六进制寄存器字符串
from .modbus_codec import (
    ByteOrderMode,
    ModbusDataType,
    clamp_value_to_type,
    decode_value,
    encode_value,
)
# 导入底层通信引擎：从机模拟引擎、主机轮询引擎与多站点传输中枢单例
from .modbus_engine import (
    ModbusSlaveEngine,
    ModbusPollEngine,
    ModbusServerHub,
)

# 初始化服务层命名日志器
logger = logging.getLogger("ModbusStudioServices")


# =====================================================================
# 1. 高吞吐线程安全日志与持久化服务 (LoggingService)
# 架构设计：
# - 生产者（后台 asyncio 从机线程、主机轮询线程、UI 主线程）调用 `post()` 非阻塞入队；
# - 消费者（`MainWindow._flush_log_loop`）每 80ms 调用 `drain_batch()` 批量拉取渲染；
# - 同时在 `post()` 内部实时将完整毫秒级时间戳与报文写入按日切分的 UTF-8 BOM CSV 文件。
# =====================================================================
class LoggingService:
    """高吞吐线程安全日志分发与文件持久化服务（双缓冲节流设计，杜绝 UI 假死与闪退）。"""

    def __init__(self, log_dir: str = "logs"):
        """初始化日志服务，创建日志归档目录、有界内存队列 (容量 20000) 并打开当日 CSV 文件句柄。"""
        # 参数：log_dir（日志文件存储目录，默认相对路径 "logs"）。
        # 副作用：在磁盘创建 `logs/` 目录及 `modbus_traffic_YYYYMMDD.csv` 文件。
        self.log_dir = os.path.abspath(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)
        # 为什么使用 maxsize=20000 的有界 Queue：
        # 当主窗口最小化或被模态弹窗阻塞时，有界队列配合满队列淘汰旧项策略可防止后台持续收发报文撑爆内存。
        self._queue: queue.Queue[LogEntry] = queue.Queue(maxsize=20000)
        self.csv_path: Optional[str] = None
        self._csv_file = None
        self._csv_writer = None
        self._init_csv()

    def _init_csv(self) -> None:
        """创建或以追加模式打开当日通信报文 CSV 归档文件，并在新文件首行写入中文表头。"""
        # 按当前日期生成文件名（如 modbus_traffic_20261012.csv），实现按天自然归档
        today_str = time.strftime("%Y%m%d")
        self.csv_path = os.path.join(self.log_dir, f"modbus_traffic_{today_str}.csv")
        is_new = not os.path.exists(self.csv_path)
        # 为什么使用 encoding="utf-8-sig"：写入 BOM 头使 Excel 直接双击打开时不出现中文乱码
        try:
            self._csv_file = open(self.csv_path, "a", newline="", encoding="utf-8-sig")
            self._csv_writer = csv.writer(self._csv_file)
            if is_new:
                self._csv_writer.writerow(["时间戳", "方向", "从机ID", "概要信息", "原始报文Hex"])
                self._csv_file.flush()
        except Exception as e:
            logger.error(f"创建日志 CSV 文件失败: {e}")

    def post(self, direction: str, slave_id: int, message: str, raw_frame_hex: str = "") -> None:
        """供后台通信线程或主线程快速非阻塞投递一条日志记录，并同步刷入磁盘 CSV。"""
        # 参数：direction（"RX"/"TX"/"SYS"/"ERROR"）、slave_id（从机站号，系统级为 0）、message（摘要）、raw_frame_hex（可选原始帧）。
        # 步骤：1. 生成毫秒级屏幕时间与完整日期时间；2. put_nowait 压入有界队列（满则淘汰队首）；3. 追加写入 CSV 并 flush。
        now = datetime.now()
        # 切片 `[:-3]` 将 6 位微秒截断为 3 位毫秒，兼顾工业报文时序分析精度与屏幕紧凑度
        now_time_ms = now.strftime("%H:%M:%S.%f")[:-3]        # 屏幕显示毫秒时间: 09:38:10.123
        now_full_ms = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # 磁盘归档完整毫秒时间: 2026-10-09 09:38:10.123

        entry = LogEntry(
            timestamp=now_time_ms,
            direction=direction,
            slave_id=slave_id,
            message=message,
            raw_frame_hex=raw_frame_hex,
        )
        # 为什么使用 put_nowait + queue.Full 丢弃最旧条目重试：
        # 后台通信线程绝不能因为 UI 消费慢而阻塞在日志锁上，否则会导致 Modbus 响应超时。
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(entry)
            except Exception:
                pass

        if self._csv_writer and self._csv_file:
            try:
                self._csv_writer.writerow([now_full_ms, direction, slave_id, message, raw_frame_hex])
                self._csv_file.flush()
            except Exception:
                pass

    def drain_batch(self, max_count: int = 150) -> List[LogEntry]:
        """供 UI 主线程定时批量拉取队列中的日志条目（单次最多拉取 max_count 条）。"""
        # 参数：max_count（单次拉取上限，防止单帧渲染耗时过长）。返回：按时间顺序排列的 LogEntry 列表。
        entries: List[LogEntry] = []
        for _ in range(max_count):
            try:
                entries.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return entries

    def close(self) -> None:
        """安全刷新缓冲区并关闭磁盘 CSV 日志文件句柄（应用退出时由 ModbusStudioAppCore.shutdown 调用）。"""
        if self._csv_file:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None



# =====================================================================
# 2. 点位校验、归一化与编解码服务 (PointService)
# 设计背景：
# 集中收口所有点位新建、批量修改、双击设值与数据库导入时的校验规则：
# - 校验寄存器地址范围 (0~65535)；
# - 强制保证存储区与数据类型双向兼容（0x/1x 必为 BOOL，BOOL 必在 0x/1x）；
# - 调用 `modbus_codec.encode_value` 计算初始 `raw_hex` 预览字符串。
# =====================================================================
class PointService:
    """点位业务统一操作入口（负责参数校验、存储区-类型双向归一化、构建模型与十六进制重编码）。"""

    @staticmethod
    def validate_and_build_point(
        address: Union[int, str],
        description: str,
        area: Union[str, AreaType],
        data_type: Union[str, DataType],
        byte_order: Union[str, ByteOrder],
        val_input: Any,
        sim_rule: Union[str, SimRule] = "固定",
        string_length: int = 16,
        field_name: str = "",
        scale: float = 1.0,
        unit: str = "",
    ) -> Tuple[bool, Optional[ModbusPoint], str]:
        """校验用户输入参数有效性、执行存储区与类型双向对齐，并构建带十六进制编码的 ModbusPoint 实体。"""
        # 参数：address（0~65535）、description、area、data_type、byte_order、val_input、sim_rule 等。
        # 返回：(是否成功, ModbusPoint 实例或 None, 错误提示文本)。
        # 步骤：1. 校验地址边界；2. 枚举归一化与区域-类型双向修正；3. 转换标量初值；4. 编码 raw_hex 并实例化 ModbusPoint。
        try:
            addr_int = int(str(address).strip())
            if addr_int < 0 or addr_int > 65535:
                return False, None, f"地址超出有效范围 (0~65535): {addr_int}"
        except Exception:
            return False, None, f"无效的寄存器起始地址: {address}"

        area_enum = AreaType.normalize(area)
        dt_enum = DataType.normalize(data_type)
        bo_enum = ByteOrder.normalize(byte_order)
        sim_str = SimRule.normalize(sim_rule)

        # 为什么强制执行存储区域与数据类型的双向对齐修正：
        # 1. Modbus 协议中 0x(线圈) 与 1x(离散输入) 每个地址仅占 1 bit，物理上只能存储 BOOL；
        # 2. 反之，若用户在界面上将类型改为 BOOL，但存储区仍停留在 3x/4x（16 位寄存器区），
        #    自动将其校正到对应的 1x(只读位) 或 0x(读写位)，防止后续用 FC01/FC02 读取时找不到数据。
        if area_enum in (AreaType.COIL, AreaType.DISCRETE):
            dt_enum = DataType.BOOL
        elif dt_enum == DataType.BOOL:
            # 布尔量绝不可放在 3x/4x 寄存器，自动校正为对应物理布尔区域
            if area_enum == AreaType.INPUT:
                area_enum = AreaType.DISCRETE
            elif area_enum == AreaType.HOLDING:
                area_enum = AreaType.COIL

        # 按归一化后的数据类型将用户输入的字符串/数字转换为目标 Python 标量（无符号数自动钳位非负）
        val_converted = val_input
        try:
            if dt_enum == DataType.BOOL:
                val_converted = 1 if str(val_input).strip().lower() in ("1", "true", "on", "yes") else 0
            elif dt_enum in (DataType.INT16, DataType.INT32):
                val_converted = int(float(str(val_input).strip() or "0"))
            elif dt_enum in (DataType.UINT16, DataType.UINT32):
                val_converted = int(float(str(val_input).strip() or "0"))
                if val_converted < 0:
                    val_converted = 0
            elif dt_enum in (DataType.FLOAT32, DataType.FLOAT64):
                val_converted = float(str(val_input).strip() or "0.0")
            elif dt_enum == DataType.STRING:
                val_converted = str(val_input)
            elif dt_enum == DataType.HEX16:
                s_hex = str(val_input).strip()
                val_converted = int(s_hex, 16) if s_hex.startswith("0x") else int(s_hex, 16)
        except Exception as e:
            return False, None, f"数值格式与类型 {dt_enum.value} 不匹配: {e}"

        # 原始十六进制编码（遵循 Modbus 工业标准与原始代码规范）：
        # 布尔量 (0x / 1x) 显示单字节状态 0x00 或 0x01；寄存器量显示 0x0000 及多字空格分隔组合
        if area_enum in (AreaType.COIL, AreaType.DISCRETE) or dt_enum == DataType.BOOL:
            b_val = 1 if (val_converted is True or str(val_converted).strip().lower() in ("1", "true", "yes", "on")) else 0
            val_converted = b_val
            raw_hex = "0x01" if b_val else "0x00"
        else:
            raw_hex = "0x0000"
            try:
                raw_regs = encode_value(val_converted, dt_enum.value, bo_enum.value)
                if raw_regs:
                    raw_hex = " ".join([f"0x{r:04X}" for r in raw_regs])
            except Exception as e:
                logger.warning(f"点位初始值编码转换警告: {e}")

        point = ModbusPoint(
            address=addr_int,
            description=str(description).strip(),
            area=area_enum,
            data_type=dt_enum,
            byte_order=bo_enum,
            value=val_converted,
            sim_rule=sim_str,
            raw_hex=raw_hex,
            string_length=string_length,
            field_name=field_name,
            scale=scale,
            unit=unit,
        )
        return True, point, ""

    @classmethod
    def re_encode_point(cls, point: ModbusPoint) -> None:
        """根据点位当前数值与字节序变位模式原地重新计算并更新 `point.raw_hex` 字符串。"""
        # 场景：当后台波形仿真线程更新了 point.value 或用户批量切换了 byte_order 时调用，保持表格 Hex 列同步。
        try:
            if point.area in (AreaType.COIL, AreaType.DISCRETE) or point.data_type == DataType.BOOL:
                b_val = 1 if (point.value is True or str(point.value).strip().lower() in ("1", "true", "yes", "on")) else 0
                point.raw_hex = "0x01" if b_val else "0x00"
            else:
                raw_regs = encode_value(point.value, point.data_type.value, point.byte_order.value)
                if raw_regs:
                    point.raw_hex = " ".join([f"0x{r:04X}" for r in raw_regs])
        except Exception as e:
            logger.warning(f"重新编码点位失败: {e}")


# =====================================================================
# 3. 从机实例生命周期与通信资源管理器 (SlaveService)
# 架构设计：
# - 维护领域模型字典 `_devices` (device_id -> SlaveDevice) 与活跃引擎字典 `_active_engines` (device_id -> ModbusSlaveEngine)；
# - 实施 TCP / RTU 各自最多 100 个从机实例的容量配额硬限制；
# - 在 `start_slave` 中集成 `[SLAVE-RECONNECT]` 底层健康探活与僵死引擎先注销再重建的自愈链路；
# - 负责将领域层 `ModbusPoint` 实时同步至底层 `ModbusSlaveEngine`，并接收仿真回调反向更新领域模型。
# =====================================================================
class SlaveService:
    """从机实例生命周期与通信资源管理器（独占式统一启停、配额守卫与双向点表同步）。"""

    def __init__(self, logging_service: LoggingService):
        """初始化从机服务管理器，绑定日志服务并创建设备字典与运行中引擎字典。"""
        self.logging_service = logging_service
        # _devices 保存所有已注册的从机配置与点表模型（含未启动和已启动）
        self._devices: Dict[str, SlaveDevice] = {}
        # _active_engines 仅保存当前处于启动运行状态的底层 ModbusSlaveEngine 实例
        self._active_engines: Dict[str, ModbusSlaveEngine] = {}

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [get_devices]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def get_devices(self) -> List[SlaveDevice]:
        """获取当前已注册的所有从机设备实体列表。"""
        # 从 [get_devices] 返回执行结果给调用方，结束当前处理分支
        return list(self._devices.values())

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [get_device]（入参: device_id）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def get_device(self, device_id: str) -> Optional[SlaveDevice]:
        """根据设备唯一标识 `device_id` 查询对应的 SlaveDevice 实体。"""
        # 从 [get_device] 返回执行结果给调用方，结束当前处理分支
        return self._devices.get(device_id)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [count_devices_by_comm]（入参: comm_type）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def count_devices_by_comm(self, comm_type: CommType) -> int:
        """按物理通信协议类型 (TCP 或 RTU) 统计当前已注册的从机实例数量。"""
        # 从 [count_devices_by_comm] 返回执行结果给调用方，结束当前处理分支
        return sum(1 for d in self._devices.values() if d.conn_config.comm_type == comm_type)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [can_register_device]（入参: comm_type）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def can_register_device(self, comm_type: CommType) -> Tuple[bool, str]:
        """检查指定通信协议下的从机实例数是否未超过系统配额上限（TCP/RTU 独立上限 100 个）。"""
        # 为什么对 TCP 和 RTU 分别设置 100 个实例的硬上限：
        # 每个运行中的从机均伴随独立的后台仿真线程与 4×65535 寄存器内存块，限制单协议 100 实例可保障工业现场长时间稳定运行。
        # 在 [can_register_device] 中更新局部变量 [limit]：为后续协议编码、服务调度或界面渲染准备中间状态
        limit = MAX_RTU_SLAVE_INSTANCES if comm_type == CommType.RTU else MAX_TCP_SLAVE_INSTANCES
        # 在 [can_register_device] 中更新局部变量 [current]：为后续协议编码、服务调度或界面渲染准备中间状态
        current = self.count_devices_by_comm(comm_type)
        # 在 [can_register_device] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if current >= limit:
            # 在 [can_register_device] 中更新局部变量 [proto_name]：为后续协议编码、服务调度或界面渲染准备中间状态
            proto_name = "Modbus RTU (串口)" if comm_type == CommType.RTU else "Modbus TCP (以太网)"
            # 从 [can_register_device] 返回执行结果给调用方，结束当前处理分支
            return False, f"{proto_name} 从机服务实例已达系统容量上限 ({limit} 个)，禁止继续创建！"
        # 从 [can_register_device] 返回执行结果给调用方，结束当前处理分支
        return True, ""

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [register_device]（入参: device, check_quota）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def register_device(self, device: SlaveDevice, check_quota: bool = True) -> Tuple[bool, str]:
        """注册或更新从机设备实例，对新增设备默认执行容量配额硬限制检查。"""
        # 若是新设备 ID 且开启了配额检查，则先验证对应协议（TCP/RTU）剩余槽位
        # 在 [register_device] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if check_quota and device.id not in self._devices:
            ok, err = self.can_register_device(device.conn_config.comm_type)
            # 在 [register_device] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not ok:
                # 从 [register_device] 返回执行结果给调用方，结束当前处理分支
                return False, err
        self._devices[device.id] = device
        # 从 [register_device] 返回执行结果给调用方，结束当前处理分支
        return True, "注册成功"

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [remove_device]（入参: device_id）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def remove_device(self, device_id: str) -> Tuple[bool, str]:
        """停止并从内存中移除指定 ID 的从机设备实例。"""
        # 若该从机正在运行，先调用 stop_slave 安全注销底层监听与仿真线程，防止残留孤儿从机响应主站问询
        # 在 [remove_device] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if device_id in self._active_engines:
            self.stop_slave(device_id)
        # 在 [remove_device] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if device_id in self._devices:
            del self._devices[device_id]
            # 从 [remove_device] 返回执行结果给调用方，结束当前处理分支
            return True, f"成功移除从机 {device_id}"
        # 从 [remove_device] 返回执行结果给调用方，结束当前处理分支
        return False, "从机不存在"

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [clear_all_devices]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def clear_all_devices(self) -> int:
        """停止所有运行中的底层通信引擎，并彻底清空所有从机设备实例与点表（如切换数据库时调用）。"""
        # 在 [clear_all_devices] 中更新局部变量 [count]：为后续协议编码、服务调度或界面渲染准备中间状态
        count = len(self._devices)
        # 1. 逐个停止运行中的底层通信引擎并释放网络/串口资源
        # 在 [clear_all_devices] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for device_id, engine in list(self._active_engines.items()):
            # 在 [clear_all_devices] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                engine.stop()
            except Exception as e:
                logger.error(f"停止底层引擎异常 [{device_id}]: {e}")
        self._active_engines.clear()

        # 2. 清空所有内存设备实体并记录系统级审计日志
        self._devices.clear()
        self.logging_service.post("SYS", 0, f"已安全停止底层通信并清空所有历史从机服务实例 (共 {count} 个)")
        # 从 [clear_all_devices] 返回执行结果给调用方，结束当前处理分支
        return count

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [start_slave]（入参: device_id）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def start_slave(self, device_id: str) -> Tuple[bool, str]:
        """统一启动指定从机服务：执行健康探活与僵死清理、检查同端点站号冲突、装配点表并启动监听与波形仿真。"""
        # 步骤拆解：
        # 1. 查找目标 SlaveDevice；
        # 2. 若标记为运行中，调用 engine.is_healthy() 探活：健康则直接幂等返回，僵死则先从 Hub 注销并停止旧引擎；
        # 3. 扫描其余运行中从机，校验同一物理端点 (IP:Port 或 COM 口) 下是否存在相同 Unit ID 冲突；
        # 4. 创建 ModbusSlaveEngine、绑定报文与数值变动回调、全量写入点表并调用 engine.start()。
        # 在 [start_slave] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
        device = self.get_device(device_id)
        # 在 [start_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 从 [start_slave] 返回执行结果给调用方，结束当前处理分支
            return False, f"未找到从机配置: {device_id}"

        # [SLAVE-RECONNECT] 探活放行：底层僵死则先彻底清理（清 Hub 映射 + 停旧 transport）再重跑注册路径
        # 在 [start_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if device.is_running:
            # 在 [start_slave] 中更新局部变量 [engine]：为后续协议编码、服务调度或界面渲染准备中间状态
            engine = self._active_engines.get(device_id)
            # 在 [start_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if engine and engine.is_healthy():
                # 从 [start_slave] 返回执行结果给调用方，结束当前处理分支
                return True, f"从机 {device.name} 运行中"
            # 僵死：先 unregister（清 Hub 映射 + 停旧 transport）再清 active，
            # 保证下方 register 走"新建"路径，不会复用僵死实例
            # 在 [start_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if engine:
                # 在 [start_slave] 中更新局部变量 [hub]：为后续协议编码、服务调度或界面渲染准备中间状态
                hub = ModbusServerHub.get_instance()
                # 在 [start_slave] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                try:
                    # [SLAVE-RECONNECT] 必须走 unregister_and_stop_slave 彻底清映射，而非仅 engine.stop()
                    hub.unregister_and_stop_slave(engine)
                except Exception as ex:
                    logger.warning(f"unregister 僵死 engine 失败: {ex}")
                # 在 [start_slave] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                try:
                    engine.stop()
                except Exception as ex:
                    logger.warning(f"清理僵死 engine 失败: {ex}")
                self._active_engines.pop(device_id, None)
            # 在 [start_slave] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.is_running = False   # 让下方注册 / 启动逻辑重跑

        # 检查通信资源冲突：标准 Modbus 规范下，同一物理端口允许挂载多个不同 Unit ID 的从机，
        # 仅当同一物理端点且从机站地址 (Unit ID) 完全相同时才判定为冲突。
        # 在 [start_slave] 中更新局部变量 [cfg]：为后续协议编码、服务调度或界面渲染准备中间状态
        cfg = device.conn_config
        # 在 [start_slave] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for other_id, other_dev in self._devices.items():
            # 在 [start_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if other_id != device_id and other_dev.is_running:
                # 在 [start_slave] 中更新局部变量 [other_cfg]：为后续协议编码、服务调度或界面渲染准备中间状态
                other_cfg = other_dev.conn_config
                # 在 [start_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if cfg.comm_type == other_cfg.comm_type:
                    # 在 [start_slave] 中更新局部变量 [same_endpoint]：为后续协议编码、服务调度或界面渲染准备中间状态
                    same_endpoint = False
                    # 在 [start_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if cfg.comm_type == CommType.RTU:
                        # 在 [start_slave] 中更新局部变量 [same_endpoint]：为后续协议编码、服务调度或界面渲染准备中间状态
                        same_endpoint = (cfg.com_port.upper().strip() == other_cfg.com_port.upper().strip())
                    # 在 [start_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    elif cfg.comm_type == CommType.TCP:
                        # 0.0.0.0 通配监听与任意具体本地 IP 在同一端口上共享同一物理套接字
                        # 在 [start_slave] 中更新局部变量 [same_endpoint]：为后续协议编码、服务调度或界面渲染准备中间状态
                        same_endpoint = (
                            cfg.port == other_cfg.port
                            and (cfg.host == other_cfg.host or cfg.host in ("0.0.0.0", "") or other_cfg.host in ("0.0.0.0", ""))
                        )

                    # 在 [start_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if same_endpoint and cfg.unit_id == other_cfg.unit_id:
                        # 在 [start_slave] 中更新局部变量 [proto_name]：为后续协议编码、服务调度或界面渲染准备中间状态
                        proto_name = "串口" if cfg.comm_type == CommType.RTU else "TCP网络"
                        # 在 [start_slave] 中更新局部变量 [ep_desc]：为后续协议编码、服务调度或界面渲染准备中间状态
                        ep_desc = cfg.com_port if cfg.comm_type == CommType.RTU else f"{cfg.host}:{cfg.port}"
                        # 从 [start_slave] 返回执行结果给调用方，结束当前处理分支
                        return False, f"站地址冲突：{proto_name} [{ep_desc}] 下已存在站地址为 {cfg.unit_id} 的运行中从机 [{other_dev.name}]！"

        # 在 [start_slave] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [start_slave] 中更新局部变量 [engine]：为后续协议编码、服务调度或界面渲染准备中间状态
            engine = ModbusSlaveEngine(
                comm_type=cfg.comm_type.value,
                host=cfg.host,
                port=cfg.port,
                serial_port=cfg.com_port,
                baudrate=cfg.baudrate,
                bytesize=cfg.data_bits,
                parity=cfg.parity,
                stopbits=cfg.stop_bits,
                slave_id=cfg.unit_id,
            )

            # 注册报文日志回调：根据报文前缀中的 RX/TX 标记自动归类日志方向并投递至 LoggingService
            # 在 [start_slave] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            engine.on_packet_log = lambda msg, level: self.logging_service.post(
                "RX" if "RX" in msg else ("TX" if "TX" in msg else "SYS"),
                cfg.unit_id,
                msg,
            )

            # 注册动态模拟数值变动回调：实时将后台仿真产生的新值反向同步给领域模型 ModbusPoint
            # 在 [start_slave] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            engine.on_point_value_changed = lambda area, addr, val: self._on_engine_point_value_changed(device_id, area, addr, val)

            # 同步所有已配置的点位到底层 DataBlock 及仿真点表字典：
            # 为什么同时写入 `(point.area.value, point.address)` 复合键与 `point.address` 整数键：
            # 复合键用于隔离不同存储区（如 3x 与 4x）的同号地址，整数键用于向下兼容旧版单键访问逻辑。
            # 在 [start_slave] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for point in device.points.values():
                # 在 [start_slave] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                try:
                    engine.write_typed_value(
                        area=point.area.value,
                        address=point.address,
                        value=point.value,
                        data_type=point.data_type.value,
                        mode=point.byte_order.value,
                        byte_order=point.byte_order.value,
                    )
                    # 在 [start_slave] 中更新局部变量 [pt_dict]：为后续协议编码、服务调度或界面渲染准备中间状态
                    pt_dict = {
                        "address": point.address,
                        "desc": point.description,
                        "area": point.area.value,
                        "data_type": point.data_type.value,
                        "type": point.data_type.value,
                        "byte_order": point.byte_order.value,
                        "mode": point.byte_order.value,
                        "sim_mode": point.sim_rule,
                        "sim_rule": point.sim_rule,
                        "current_val": point.value,
                    }
                    engine.points[(point.area.value, point.address)] = pt_dict
                    engine.points[point.address] = pt_dict
                except Exception as ex:
                    logger.warning(f"同步点位到底层失败 {point.address}: {ex}")

            # 启动底层从机引擎（挂载至 ModbusServerHub 并开启后台波形仿真线程）
            engine.start()
            self._active_engines[device_id] = engine
            # 在 [start_slave] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.is_running = True
            # 在 [start_slave] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.runtime_info = f"运行中 ({cfg.summary})"
            self.logging_service.post("SYS", cfg.unit_id, f"从机服务 [{device.name}] 成功启动: {cfg.summary}")
            # 从 [start_slave] 返回执行结果给调用方，结束当前处理分支
            return True, f"从机服务 [{device.name}] 启动成功"
        except Exception as e:
            # 在 [start_slave] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.is_running = False
            # 在 [start_slave] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.runtime_info = f"启动失败: {e}"
            self.logging_service.post("ERROR", cfg.unit_id, f"从机 [{device.name}] 启动失败: {e}")
            # 从 [start_slave] 返回执行结果给调用方，结束当前处理分支
            return False, f"启动异常: {e}"

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_engine_point_value_changed]（入参: device_id, area_str, address, new_val）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _on_engine_point_value_changed(self, device_id: str, area_str: str, address: int, new_val: Any) -> None:
        """接收底层引擎波形仿真产生的新数值，反向更新 SlaveDevice 点位实体并重算十六进制表示。"""
        # 在 [_on_engine_point_value_changed] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
        device = self.get_device(device_id)
        # 在 [_on_engine_point_value_changed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 从 [_on_engine_point_value_changed] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_on_engine_point_value_changed] 中更新局部变量 [area_enum]：为后续协议编码、服务调度或界面渲染准备中间状态
        area_enum = AreaType.normalize(area_str)
        # 在 [_on_engine_point_value_changed] 中更新局部变量 [point]：为后续协议编码、服务调度或界面渲染准备中间状态
        point = device.get_point(area_enum, address)
        # 在 [_on_engine_point_value_changed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if point:
            # 在 [_on_engine_point_value_changed] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            point.value = new_val
            PointService.re_encode_point(point)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [stop_slave]（入参: device_id）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def stop_slave(self, device_id: str) -> Tuple[bool, str]:
        """统一停止指定从机服务，从物理传输中枢注销并释放串口/网络句柄（最后一台停止时关闭监听）。"""
        # 在 [stop_slave] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
        device = self.get_device(device_id)
        # 在 [stop_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 从 [stop_slave] 返回执行结果给调用方，结束当前处理分支
            return False, "从机不存在"

        # 在 [stop_slave] 中更新局部变量 [engine]：为后续协议编码、服务调度或界面渲染准备中间状态
        engine = self._active_engines.pop(device_id, None)
        # 在 [stop_slave] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if engine:
            # 在 [stop_slave] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                engine.stop()
            except Exception as e:
                logger.error(f"关闭底层服务发生异常: {e}")

        # 在 [stop_slave] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        device.is_running = False
        # 在 [stop_slave] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        device.runtime_info = "已停止"
        self.logging_service.post("SYS", device.conn_config.unit_id, f"从机服务 [{device.name}] 已停止并释放通信资源")
        # 从 [stop_slave] 返回执行结果给调用方，结束当前处理分支
        return True, f"从机 [{device.name}] 已停止"

    # [SLAVE-RECONNECT] 兜底自动重连：对所有在跑从机做一次探活，僵死的自动重建
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [auto_reconnect_if_needed]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def auto_reconnect_if_needed(self) -> int:
        """委托 ModbusServerHub 扫描所有活跃物理端点，自动重建僵死的底层 listener 并返回重建数量。"""
        # 在 [auto_reconnect_if_needed] 中更新局部变量 [hub]：为后续协议编码、服务调度或界面渲染准备中间状态
        hub = ModbusServerHub.get_instance()
        # 在 [auto_reconnect_if_needed] 中更新局部变量 [n]：为后续协议编码、服务调度或界面渲染准备中间状态
        n = hub.restart_all_broken()
        # 在 [auto_reconnect_if_needed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if n > 0:
            self.logging_service.post("SYS", 0, f"[自动重连] 检测到 {n} 个僵死 listener，已重建")
        # 从 [auto_reconnect_if_needed] 返回执行结果给调用方，结束当前处理分支
        return n

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [sync_point_to_runtime]（入参: device_id, point）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def sync_point_to_runtime(self, device_id: str, point: ModbusPoint) -> None:
        """当界面或业务运行期修改了点位数值、类型、变位或模拟规则时，实时热推入运行中的底层引擎。"""
        # 在 [sync_point_to_runtime] 中更新局部变量 [engine]：为后续协议编码、服务调度或界面渲染准备中间状态
        engine = self._active_engines.get(device_id)
        # 在 [sync_point_to_runtime] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if engine:
            # 在 [sync_point_to_runtime] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                engine.write_typed_value(
                    area=point.area.value,
                    address=point.address,
                    value=point.value,
                    data_type=point.data_type.value,
                    mode=point.byte_order.value,
                    byte_order=point.byte_order.value,
                )
                # 在 [sync_point_to_runtime] 中更新局部变量 [pt_dict]：为后续协议编码、服务调度或界面渲染准备中间状态
                pt_dict = {
                    "address": point.address,
                    "desc": point.description,
                    "area": point.area.value,
                    "data_type": point.data_type.value,
                    "type": point.data_type.value,
                    "byte_order": point.byte_order.value,
                    "mode": point.byte_order.value,
                    "sim_mode": point.sim_rule,
                    "sim_rule": point.sim_rule,
                    "current_val": point.value,
                }
                engine.points[(point.area.value, point.address)] = pt_dict
                engine.points[point.address] = pt_dict
            except Exception as e:
                logger.error(f"动态同步点位到底层失败: {e}")


# =====================================================================
# 4. 主机轮询与指令下发服务 (PollService)
# 架构设计：
# 封装底层 `ModbusPollEngine`，向 `PollPanel` 暴露统一的连接管理、单次寄存器读取 (`read_registers`)
# 与自定义间隔的后台连续轮询 (`start_continuous_polling` / `stop_continuous_polling`) 接口。
# =====================================================================
class PollService:
    """主机轮询与指令下发业务服务。"""

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [__init__]（入参: logging_service）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def __init__(self, logging_service: LoggingService):
        """初始化主机轮询服务，绑定日志分发器并初始化底层引擎与连接状态变量。"""
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.logging_service = logging_service
        self._engine: Optional[ModbusPollEngine] = None
        self._is_connected: bool = False
        self._current_config: Optional[ConnectionConfig] = None

    @property
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [is_connected]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def is_connected(self) -> bool:
        """返回当前主机客户端是否已与目标从机建立物理连接。"""
        # 从 [is_connected] 返回执行结果给调用方，结束当前处理分支
        return self._is_connected

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [connect]（入参: cfg）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def connect(self, cfg: ConnectionConfig) -> Tuple[bool, str]:
        """根据 ConnectionConfig 建立 Modbus TCP 或 RTU 主机连接，并挂载报文跟踪日志回调。"""
        # 若已有旧连接处于活跃状态，先安全断开并释放旧套接字/串口句柄
        # 在 [connect] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._is_connected and self._engine:
            self.disconnect()

        # 在 [connect] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [connect] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._engine = ModbusPollEngine()
            # 在 [connect] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._engine.on_packet_log = lambda msg, level: self.logging_service.post(
                "RX" if "RX" in msg else ("TX" if "TX" in msg else "SYS"),
                cfg.unit_id,
                msg,
            )

            # 在 [connect] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if cfg.comm_type == CommType.RTU:
                # 在 [connect] 中更新局部变量 [ok]：为后续协议编码、服务调度或界面渲染准备中间状态
                ok = self._engine.connect_rtu(
                    serial_port=cfg.com_port,
                    baudrate=cfg.baudrate,
                    bytesize=cfg.data_bits,
                    parity=cfg.parity,
                    stopbits=cfg.stop_bits,
                )
            else:
                # 在 [connect] 中更新局部变量 [ok]：为后续协议编码、服务调度或界面渲染准备中间状态
                ok = self._engine.connect_tcp(
                    host=cfg.host,
                    port=cfg.port,
                )

            # 在 [connect] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if ok:
                # 在 [connect] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                self._is_connected = True
                # 在 [connect] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                self._current_config = cfg
                self.logging_service.post("SYS", cfg.unit_id, f"主机客户端连接成功: {cfg.summary}")
                # 从 [connect] 返回执行结果给调用方，结束当前处理分支
                return True, "连接成功"
            else:
                # 在 [connect] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                self._is_connected = False
                # 从 [connect] 返回执行结果给调用方，结束当前处理分支
                return False, "无法建立连接"
        except Exception as e:
            # 在 [connect] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._is_connected = False
            # 从 [connect] 返回执行结果给调用方，结束当前处理分支
            return False, f"连接异常: {e}"

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [disconnect]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def disconnect(self) -> Tuple[bool, str]:
        """停止可能正在运行的后台连续轮询，并断开主机通信连接。"""
        # 在 [disconnect] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._engine:
            # 在 [disconnect] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                self._engine.disconnect()
            except Exception:
                pass
            # 在 [disconnect] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._engine = None
        # 在 [disconnect] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._is_connected = False
        # 从 [disconnect] 返回执行结果给调用方，结束当前处理分支
        return True, "已断开连接"

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [read_registers]（入参: task）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def read_registers(self, task: PollTask) -> PollResult:
        """执行单次寄存器/线圈块读取（若尚未连接则自动按 task.conn_config 尝试建连），返回强类型 PollResult。"""
        # 在 [read_registers] 中更新局部变量 [t0]：为后续协议编码、服务调度或界面渲染准备中间状态
        t0 = time.time()
        # 为什么在未连接时自动调用 self.connect(task.conn_config)：
        # 允许用户打开主机面板后无需先手动点“建立连接”，直接点击“单次读取”即可一键完成建连与问询。
        # 在 [read_registers] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self._is_connected or not self._engine:
            ok, msg = self.connect(task.conn_config)
            # 在 [read_registers] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not ok:
                # 从 [read_registers] 返回执行结果给调用方，结束当前处理分支
                return PollResult(
                    success=False,
                    area=task.area,
                    start_address=task.start_address,
                    count=task.count,
                    error_msg=f"未连接或连接失败: {msg}",
                    elapsed_ms=(time.time() - t0) * 1000,
                )

        # 在 [read_registers] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            ok, values, err_msg = self._engine.read_block(
                area=task.area.value,
                start_addr=task.start_address,
                count=task.count,
            )
            # 在 [read_registers] 中更新局部变量 [elapsed]：为后续协议编码、服务调度或界面渲染准备中间状态
            elapsed = (time.time() - t0) * 1000
            # 在 [read_registers] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if ok:
                # 从 [read_registers] 返回执行结果给调用方，结束当前处理分支
                return PollResult(
                    success=True,
                    area=task.area,
                    start_address=task.start_address,
                    count=task.count,
                    raw_registers=values,
                    elapsed_ms=elapsed,
                )
            else:
                # 从 [read_registers] 返回执行结果给调用方，结束当前处理分支
                return PollResult(
                    success=False,
                    area=task.area,
                    start_address=task.start_address,
                    count=task.count,
                    error_msg=err_msg or "读取超时或无响应",
                    elapsed_ms=elapsed,
                )
        except Exception as e:
            # 从 [read_registers] 返回执行结果给调用方，结束当前处理分支
            return PollResult(
                success=False,
                area=task.area,
                start_address=task.start_address,
                count=task.count,
                error_msg=str(e),
                elapsed_ms=(time.time() - t0) * 1000,
            )

    # [POLL-CONFIG] 新增后台连续轮询控制服务接口，支持自定义轮询间隔 (interval_ms)
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [start_continuous_polling]（入参: task, interval_ms, on_success, on_error）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def start_continuous_polling(
        self,
        task: PollTask,
        interval_ms: int = 1000,
        on_success: Optional[Callable[[Dict[int, int]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str]:
        """启动后台连续定时轮询（间隔 interval_ms 毫秒），自动确保已建立连接并绑定成功/失败回调。"""
        # 1. 若尚未连接，先按任务配置自动建立物理连接
        # 在 [start_continuous_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self._is_connected or not self._engine:
            ok, msg = self.connect(task.conn_config)
            # 在 [start_continuous_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not ok:
                # 从 [start_continuous_polling] 返回执行结果给调用方，结束当前处理分支
                return False, f"建立连接失败: {msg}"

        # 2. 防重入保护：若底层引擎已在轮询中则拒绝重复启动
        # 在 [start_continuous_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._engine.is_polling:
            # 从 [start_continuous_polling] 返回执行结果给调用方，结束当前处理分支
            return False, "轮询已在运行中"

        # 3. 绑定成功与错误回调（注意：回调在后台 worker 线程触发，UI 调用方须通过 after(0) 切回主线程）
        # 在 [start_continuous_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if on_success:
            # 在 [start_continuous_polling] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._engine.on_poll_success = on_success
        # 在 [start_continuous_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if on_error:
            # 在 [start_continuous_polling] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._engine.on_poll_error = on_error

        self._engine.start_polling(
            area=task.area.value,
            start_addr=task.start_address,
            count=task.count,
            interval_ms=interval_ms,
        )
        # 从 [start_continuous_polling] 返回执行结果给调用方，结束当前处理分支
        return True, "轮询已启动"

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [stop_continuous_polling]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def stop_continuous_polling(self) -> None:
        """安全停止后台连续轮询任务并唤醒等待中的轮询线程退出。"""
        # 在 [stop_continuous_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._engine:
            self._engine.stop_polling()


