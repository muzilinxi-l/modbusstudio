"""
Modbus Studio - 业务服务层 (Application Services)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
为界面层提供统一的操作入口，负责参数校验、模型编排、编解码调用及底层通信资源调度。
界面层只负责收集输入和呈现结果，不直接碰触硬件资源与通信细节。
"""

from __future__ import annotations
from datetime import datetime
import csv
import logging
import os
import queue
import time

from typing import Any, Callable, Dict, List, Optional, Tuple, Union

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
from .modbus_codec import (
    ByteOrderMode,
    ModbusDataType,
    clamp_value_to_type,
    decode_value,
    encode_value,
)
from .modbus_engine import (
    ModbusSlaveEngine,
    ModbusPollEngine,
)

logger = logging.getLogger("ModbusStudioServices")


class LoggingService:
    """高吞吐线程安全日志分发与文件持久化服务（双缓冲节流设计，杜绝 UI 假死与闪退）"""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = os.path.abspath(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)
        self._queue: queue.Queue[LogEntry] = queue.Queue(maxsize=20000)
        self.csv_path: Optional[str] = None
        self._csv_file = None
        self._csv_writer = None
        self._init_csv()

    def _init_csv(self) -> None:
        today_str = time.strftime("%Y%m%d")
        self.csv_path = os.path.join(self.log_dir, f"modbus_traffic_{today_str}.csv")
        is_new = not os.path.exists(self.csv_path)
        try:
            self._csv_file = open(self.csv_path, "a", newline="", encoding="utf-8-sig")
            self._csv_writer = csv.writer(self._csv_file)
            if is_new:
                self._csv_writer.writerow(["时间戳", "方向", "从机ID", "概要信息", "原始报文Hex"])
                self._csv_file.flush()
        except Exception as e:
            logger.error(f"创建日志 CSV 文件失败: {e}")

    def post(self, direction: str, slave_id: int, message: str, raw_frame_hex: str = "") -> None:
        """后台通信线程快速非阻塞投递日志 (支持毫秒级时间戳)"""
        now = datetime.now()
        now_time_ms = now.strftime("%H:%M:%S.%f")[:-3]        # 屏幕显示毫秒时间: 09:38:10.123
        now_full_ms = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # 磁盘归档完整毫秒时间: 2026-10-09 09:38:10.123

        entry = LogEntry(
            timestamp=now_time_ms,
            direction=direction,
            slave_id=slave_id,
            message=message,
            raw_frame_hex=raw_frame_hex,
        )
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
        """UI 主线程定时批量拉取日志条目，单次集中渲染"""
        entries: List[LogEntry] = []
        for _ in range(max_count):
            try:
                entries.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return entries

    def close(self) -> None:
        """安全刷新并关闭磁盘日志文件句柄"""
        if self._csv_file:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None



class PointService:
    """点位业务统一操作入口（负责校验、构建模型、编解码转换）"""

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
        """统一入口：校验输入有效性、计算占用字长并生成已编码的 ModbusPoint 实体"""
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

        # 确保存储区域与数据类型严格双向匹配：
        if area_enum in (AreaType.COIL, AreaType.DISCRETE):
            dt_enum = DataType.BOOL
        elif dt_enum == DataType.BOOL:
            # 布尔量绝不可放在 3x/4x 寄存器，自动校正为对应物理布尔区域
            if area_enum == AreaType.INPUT:
                area_enum = AreaType.DISCRETE
            elif area_enum == AreaType.HOLDING:
                area_enum = AreaType.COIL

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

        # 原始十六进制编码 (遵循 Modbus 工业标准与原始代码规范)：
        # 布尔量 (0x / 1x) 显示单字节状态 0x00 或 0x01；寄存器量显示 0x0000 及多字组合
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
        """根据当前值与变位配置重新生成十六进制原始字"""
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


class SlaveService:
    """从机实例生命周期与通信资源管理器（独占式统一启停与点表同步）"""

    def __init__(self, logging_service: LoggingService):
        self.logging_service = logging_service
        self._devices: Dict[str, SlaveDevice] = {}
        self._active_engines: Dict[str, ModbusSlaveEngine] = {}

    def get_devices(self) -> List[SlaveDevice]:
        """获取当前已配置的所有从机设备列表"""
        return list(self._devices.values())

    def get_device(self, device_id: str) -> Optional[SlaveDevice]:
        """根据唯一标识获取从机设备实例"""
        return self._devices.get(device_id)

    def count_devices_by_comm(self, comm_type: CommType) -> int:
        """按物理通信协议类型统计当前已注册的从机实例数量

        Args:
            comm_type: 通信物理类型 (TCP 或 RTU)

        Returns:
            int: 该协议下当前实例数
        """
        return sum(1 for d in self._devices.values() if d.conn_config.comm_type == comm_type)

    def can_register_device(self, comm_type: CommType) -> Tuple[bool, str]:
        """检查特定通信协议的从机实例数是否达到系统硬性配额上限 (独立限制 100 个)

        Args:
            comm_type: 通信物理类型 (TCP 或 RTU)

        Returns:
            Tuple[bool, str]: (是否允许创建, 错误或提示信息)
        """
        limit = MAX_RTU_SLAVE_INSTANCES if comm_type == CommType.RTU else MAX_TCP_SLAVE_INSTANCES
        current = self.count_devices_by_comm(comm_type)
        if current >= limit:
            proto_name = "Modbus RTU (串口)" if comm_type == CommType.RTU else "Modbus TCP (以太网)"
            return False, f"{proto_name} 从机服务实例已达系统容量上限 ({limit} 个)，禁止继续创建！"
        return True, ""

    def register_device(self, device: SlaveDevice, check_quota: bool = True) -> Tuple[bool, str]:
        """注册从机设备，并执行容量配额硬限制检查

        Args:
            device: 从机设备实体
            check_quota: 是否检查协议配额（默认为 True；若是更新已有设备则不计新增）

        Returns:
            Tuple[bool, str]: (是否注册成功, 状态描述信息)
        """
        if check_quota and device.id not in self._devices:
            ok, err = self.can_register_device(device.conn_config.comm_type)
            if not ok:
                return False, err
        self._devices[device.id] = device
        return True, "注册成功"

    def remove_device(self, device_id: str) -> Tuple[bool, str]:
        if device_id in self._active_engines:
            self.stop_slave(device_id)
        if device_id in self._devices:
            del self._devices[device_id]
            return True, f"成功移除从机 {device_id}"
        return False, "从机不存在"

    def clear_all_devices(self) -> int:
        """停止所有运行中的底层通信服务，并完全清空所有从机设备实例与点表

        Returns:
            int: 成功停止并清空的从机实例总数
        """
        count = len(self._devices)
        # 1. 逐个停止运行中的底层通信引擎并释放资源
        for device_id, engine in list(self._active_engines.items()):
            try:
                engine.stop()
            except Exception as e:
                logger.error(f"停止底层引擎异常 [{device_id}]: {e}")
        self._active_engines.clear()

        # 2. 清空所有内存设备实体
        self._devices.clear()
        self.logging_service.post("SYS", 0, f"已安全停止底层通信并清空所有历史从机服务实例 (共 {count} 个)")
        return count

    def start_slave(self, device_id: str) -> Tuple[bool, str]:
        """统一启动从机服务，装配点表并开启网络/串口监听"""
        device = self.get_device(device_id)
        if not device:
            return False, f"未找到从机配置: {device_id}"

        if device.is_running:
            return True, f"从机 {device.name} 已经在运行中"

        # 检查通信资源冲突：标准 Modbus 规范下，仅当同一物理端点且从机站地址 (Unit ID) 完全相同时才判定为冲突
        cfg = device.conn_config
        for other_id, other_dev in self._devices.items():
            if other_id != device_id and other_dev.is_running:
                other_cfg = other_dev.conn_config
                if cfg.comm_type == other_cfg.comm_type:
                    same_endpoint = False
                    if cfg.comm_type == CommType.RTU:
                        same_endpoint = (cfg.com_port.upper().strip() == other_cfg.com_port.upper().strip())
                    elif cfg.comm_type == CommType.TCP:
                        same_endpoint = (
                            cfg.port == other_cfg.port
                            and (cfg.host == other_cfg.host or cfg.host in ("0.0.0.0", "") or other_cfg.host in ("0.0.0.0", ""))
                        )

                    if same_endpoint and cfg.unit_id == other_cfg.unit_id:
                        proto_name = "串口" if cfg.comm_type == CommType.RTU else "TCP网络"
                        ep_desc = cfg.com_port if cfg.comm_type == CommType.RTU else f"{cfg.host}:{cfg.port}"
                        return False, f"站地址冲突：{proto_name} [{ep_desc}] 下已存在站地址为 {cfg.unit_id} 的运行中从机 [{other_dev.name}]！"

        try:
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

            # 注册报文日志回调
            engine.on_packet_log = lambda msg, level: self.logging_service.post(
                "RX" if "RX" in msg else ("TX" if "TX" in msg else "SYS"),
                cfg.unit_id,
                msg,
            )

            # 注册动态模拟数值变动回调：实时反向同步给应用模型实体
            engine.on_point_value_changed = lambda area, addr, val: self._on_engine_point_value_changed(device_id, area, addr, val)

            # 同步所有已配置的点位到底层 DataBlock
            for point in device.points.values():
                try:
                    engine.write_typed_value(
                        area=point.area.value,
                        address=point.address,
                        value=point.value,
                        data_type=point.data_type.value,
                        mode=point.byte_order.value,
                        byte_order=point.byte_order.value,
                    )
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

            # 启动服务
            engine.start()
            self._active_engines[device_id] = engine
            device.is_running = True
            device.runtime_info = f"运行中 ({cfg.summary})"
            self.logging_service.post("SYS", cfg.unit_id, f"从机服务 [{device.name}] 成功启动: {cfg.summary}")
            return True, f"从机服务 [{device.name}] 启动成功"
        except Exception as e:
            device.is_running = False
            device.runtime_info = f"启动失败: {e}"
            self.logging_service.post("ERROR", cfg.unit_id, f"从机 [{device.name}] 启动失败: {e}")
            return False, f"启动异常: {e}"

    def _on_engine_point_value_changed(self, device_id: str, area_str: str, address: int, new_val: Any) -> None:
        """接收底层引擎模拟新数值并反向同步至 Device 点位实体"""
        device = self.get_device(device_id)
        if not device:
            return
        area_enum = AreaType.normalize(area_str)
        point = device.get_point(area_enum, address)
        if point:
            point.value = new_val
            PointService.re_encode_point(point)

    def stop_slave(self, device_id: str) -> Tuple[bool, str]:
        """统一停止从机服务并释放串口/网络句柄"""
        device = self.get_device(device_id)
        if not device:
            return False, "从机不存在"

        engine = self._active_engines.pop(device_id, None)
        if engine:
            try:
                engine.stop()
            except Exception as e:
                logger.error(f"关闭底层服务发生异常: {e}")

        device.is_running = False
        device.runtime_info = "已停止"
        self.logging_service.post("SYS", device.conn_config.unit_id, f"从机服务 [{device.name}] 已停止并释放通信资源")
        return True, f"从机 [{device.name}] 已停止"

    def sync_point_to_runtime(self, device_id: str, point: ModbusPoint) -> None:
        """当界面或业务修改点位数值时，实时推入运行中的底层引擎"""
        engine = self._active_engines.get(device_id)
        if engine:
            try:
                engine.write_typed_value(
                    area=point.area.value,
                    address=point.address,
                    value=point.value,
                    data_type=point.data_type.value,
                    mode=point.byte_order.value,
                    byte_order=point.byte_order.value,
                )
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


class PollService:
    """主机轮询与指令下发服务"""

    def __init__(self, logging_service: LoggingService):
        self.logging_service = logging_service
        self._engine: Optional[ModbusPollEngine] = None
        self._is_connected: bool = False
        self._current_config: Optional[ConnectionConfig] = None

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def connect(self, cfg: ConnectionConfig) -> Tuple[bool, str]:
        """建立 Modbus 主机通信连接"""
        if self._is_connected and self._engine:
            self.disconnect()

        try:
            self._engine = ModbusPollEngine()
            self._engine.on_packet_log = lambda msg, level: self.logging_service.post(
                "RX" if "RX" in msg else ("TX" if "TX" in msg else "SYS"),
                cfg.unit_id,
                msg,
            )

            if cfg.comm_type == CommType.RTU:
                ok = self._engine.connect_rtu(
                    serial_port=cfg.com_port,
                    baudrate=cfg.baudrate,
                    bytesize=cfg.data_bits,
                    parity=cfg.parity,
                    stopbits=cfg.stop_bits,
                )
            else:
                ok = self._engine.connect_tcp(
                    host=cfg.host,
                    port=cfg.port,
                )

            if ok:
                self._is_connected = True
                self._current_config = cfg
                self.logging_service.post("SYS", cfg.unit_id, f"主机客户端连接成功: {cfg.summary}")
                return True, "连接成功"
            else:
                self._is_connected = False
                return False, "无法建立连接"
        except Exception as e:
            self._is_connected = False
            return False, f"连接异常: {e}"

    def disconnect(self) -> Tuple[bool, str]:
        """断开连接并安全关闭通信通道"""
        if self._engine:
            try:
                self._engine.disconnect()
            except Exception:
                pass
            self._engine = None
        self._is_connected = False
        return True, "已断开连接"

    def read_registers(self, task: PollTask) -> PollResult:
        """执行单次数据读取"""
        t0 = time.time()
        if not self._is_connected or not self._engine:
            ok, msg = self.connect(task.conn_config)
            if not ok:
                return PollResult(
                    success=False,
                    area=task.area,
                    start_address=task.start_address,
                    count=task.count,
                    error_msg=f"未连接或连接失败: {msg}",
                    elapsed_ms=(time.time() - t0) * 1000,
                )

        try:
            ok, values, err_msg = self._engine.read_block(
                area=task.area.value,
                start_addr=task.start_address,
                count=task.count,
            )
            elapsed = (time.time() - t0) * 1000
            if ok:
                return PollResult(
                    success=True,
                    area=task.area,
                    start_address=task.start_address,
                    count=task.count,
                    raw_registers=values,
                    elapsed_ms=elapsed,
                )
            else:
                return PollResult(
                    success=False,
                    area=task.area,
                    start_address=task.start_address,
                    count=task.count,
                    error_msg=err_msg or "读取超时或无响应",
                    elapsed_ms=elapsed,
                )
        except Exception as e:
            return PollResult(
                success=False,
                area=task.area,
                start_address=task.start_address,
                count=task.count,
                error_msg=str(e),
                elapsed_ms=(time.time() - t0) * 1000,
            )
