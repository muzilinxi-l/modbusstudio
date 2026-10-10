"""Modbus 通信引擎 (Slave 从机服务 + Poll 主机轮询客户端).

结合 pymodbus 与 modbus_codec，支持全数据类型与 4 种变位模式的读写。
"""

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from pymodbus.client import ModbusSerialClient, ModbusTcpClient
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusSerialServer, ModbusTcpServer
from pymodbus.constants import ExcCodes

try:
    from .modbus_codec import (
        ByteOrderMode,
        ModbusDataType,
        TYPE_RANGES,
        TYPE_REGISTER_COUNT,
        clamp_value_to_type,
        decode_value,
        encode_value,
    )
except ImportError:
    from modbus_codec import (
        ByteOrderMode,
        ModbusDataType,
        TYPE_RANGES,
        TYPE_REGISTER_COUNT,
        clamp_value_to_type,
        decode_value,
        encode_value,
    )


logger = logging.getLogger("ModbusEngine")


class AreaType:
    """Modbus 区域类型."""

    COIL = "0x_Coil (线圈)"
    DISCRETE_INPUT = "1x_DiscreteInput (离散输入)"
    INPUT_REGISTER = "3x_InputRegister (输入寄存器)"
    HOLDING_REGISTER = "4x_HoldingRegister (保持寄存器)"


def crc16_modbus(data: bytes) -> bytes:
    """计算工业标准 Modbus RTU 的 CRC16 (低字节在前，高字节在后)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def build_modbus_rtu_req_hex(slave_id: int, fc: int, start_addr: int, count_or_val: int) -> str:
    """构建 Modbus RTU 请求的标准十六进制报文字符串 (含 CRC16)."""
    raw = bytes([
        slave_id & 0xFF,
        fc & 0xFF,
        (start_addr >> 8) & 0xFF, start_addr & 0xFF,
        (count_or_val >> 8) & 0xFF, count_or_val & 0xFF,
    ])
    crc = crc16_modbus(raw)
    return " ".join(f"{b:02X}" for b in (raw + crc))


def build_modbus_tcp_req_hex(slave_id: int, fc: int, start_addr: int, count_or_val: int, tx_id: int = 1) -> str:
    """构建 Modbus TCP 请求的标准十六进制报文字符串 (MBAP 报头 + PDU)."""
    raw = [
        (tx_id >> 8) & 0xFF, tx_id & 0xFF,  # 事务元标识符 (2 字节)
        0x00, 0x00,                         # 协议标识符 (Modbus TCP = 0)
        0x00, 0x06,                         # 后续长度 (UnitID + PDU 共 6 字节)
        slave_id & 0xFF,                    # 单元标识符 (从机 ID)
        fc & 0xFF,                          # 功能码
        (start_addr >> 8) & 0xFF, start_addr & 0xFF,       # 寄存器起始地址
        (count_or_val >> 8) & 0xFF, count_or_val & 0xFF     # 读取数量或写入数值
    ]
    return " ".join(f"{b:02X}" for b in raw)


def build_modbus_tcp_resp_hex(slave_id: int, fc: int, data_bytes: List[int], tx_id: int = 1) -> str:
    """构建 Modbus TCP 响应的标准十六进制报文字符串 (MBAP 报头 + PDU)."""
    pdu = [fc & 0xFF, len(data_bytes) & 0xFF] + [b & 0xFF for b in data_bytes]
    length = len(pdu) + 1  # 包含 unit_id
    mbap = [
        (tx_id >> 8) & 0xFF, tx_id & 0xFF,
        0x00, 0x00,
        (length >> 8) & 0xFF, length & 0xFF,
        slave_id & 0xFF,
    ]
    return " ".join(f"{b:02X}" for b in (mbap + pdu))


# =====================================================================
# =====================================================================
# 1. 报文协议拦截解析辅助函数 (支持 RTU 与 TCP，精准提取 Unit ID)
# =====================================================================
def format_modbus_trace_packet(comm_type: str, sending: bool, data: bytes) -> Tuple[int, str]:
    """拦截并解析从机收发的 Modbus 报文帧，返回 (unit_id, 格式化日志描述)."""
    if not data:
        return 0, ""
    try:
        hex_frame = " ".join(f"{b:02X}" for b in data)
        fc_names = {
            1: "读线圈", 2: "读离散输入", 3: "读保持寄存器", 4: "读输入寄存器",
            5: "写单个线圈", 6: "写单个保持寄存器", 15: "写多个线圈", 16: "写多个保持寄存器"
        }

        if comm_type == "RTU":
            if len(data) < 4:
                return (data[0] if len(data) > 0 else 0), f"[RTU 原始帧] [{hex_frame}]"
            unit_id = data[0]
            fc = data[1]
            fc_desc = fc_names.get(fc, f"功能码0x{fc:02X}")
            crc_hex = f"{data[-2]:02X} {data[-1]:02X}"

            if not sending:
                if fc in (1, 2, 3, 4) and len(data) >= 8:
                    start_addr = int.from_bytes(data[2:4], "big")
                    count = int.from_bytes(data[4:6], "big")
                    msg = f"[RX 串口问询] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{start_addr} | 数量:{count} | 帧:[{hex_frame}] (CRC:{crc_hex})"
                elif fc in (5, 6) and len(data) >= 8:
                    addr = int.from_bytes(data[2:4], "big")
                    val = int.from_bytes(data[4:6], "big")
                    val_hex = f"0x{val:04X}"
                    msg = f"[RX 串口写问询] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 目标地址:{addr} | 设定值:{val} ({val_hex}) | 帧:[{hex_frame}]"
                elif fc in (15, 16) and len(data) >= 9:
                    addr = int.from_bytes(data[2:4], "big")
                    count = int.from_bytes(data[4:6], "big")
                    msg = f"[RX 串口批量写] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{addr} | 数量:{count} | 帧:[{hex_frame}]"
                else:
                    msg = f"[RX 串口问询] 从机:{unit_id} | FC:{fc:02X} | 帧:[{hex_frame}]"
            else:
                if fc in (1, 2, 3, 4) and len(data) >= 5:
                    byte_count = data[2]
                    payload = data[3:-2]
                    if fc in (3, 4) and len(payload) >= 2:
                        regs = [int.from_bytes(payload[i:i+2], "big") for i in range(0, len(payload), 2)]
                        preview = ", ".join(str(r) for r in regs[:8])
                        if len(regs) > 8:
                            preview += f", ... (共{len(regs)}项)"
                        msg = f"[TX 串口响应] 从机:{unit_id} | FC:{fc:02X} | 字节数:{byte_count} | 数据:[{preview}] | 帧:[{hex_frame}]"
                    else:
                        msg = f"[TX 串口响应] 从机:{unit_id} | FC:{fc:02X} | 字节数:{byte_count} | 帧:[{hex_frame}]"
                elif fc in (5, 6) and len(data) >= 8:
                    addr = int.from_bytes(data[2:4], "big")
                    val = int.from_bytes(data[4:6], "big")
                    msg = f"[TX 串口写确认] 从机:{unit_id} | FC:{fc:02X} | 地址:{addr} | 确认成功 | 帧:[{hex_frame}]"
                elif fc >= 0x80:
                    err_code = data[2] if len(data) >= 3 else 0
                    msg = f"[TX 串口异常响应] 从机:{unit_id} | FC:{fc:02X} | 异常码:{err_code} | 帧:[{hex_frame}]"
                else:
                    msg = f"[TX 串口响应] 从机:{unit_id} | FC:{fc:02X} | 帧:[{hex_frame}]"
            return unit_id, msg

        else:
            # --- TCP 规约帧 (含 7 字节 MBAP 报头，字节 6 为 Unit ID) ---
            if len(data) < 7:
                return 0, f"[TCP 原始帧] [{hex_frame}]"
            trans_id = int.from_bytes(data[0:2], "big")
            length = int.from_bytes(data[4:6], "big")
            unit_id = data[6]

            if not sending:
                if len(data) >= 8:
                    fc = data[7]
                    fc_desc = fc_names.get(fc, f"功能码0x{fc:02X}")
                    if fc in (1, 2, 3, 4) and len(data) >= 12:
                        start_addr = int.from_bytes(data[8:10], "big")
                        count = int.from_bytes(data[10:12], "big")
                        msg = f"[RX 问询报文] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{start_addr} | 数量:{count} | 帧:[{hex_frame}]"
                    elif fc in (5, 6) and len(data) >= 12:
                        addr = int.from_bytes(data[8:10], "big")
                        val = int.from_bytes(data[10:12], "big")
                        val_hex = f"0x{val:04X}"
                        msg = f"[RX 写入问询] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 目标地址:{addr} | 设定值:{val} ({val_hex}) | 帧:[{hex_frame}]"
                    elif fc in (15, 16) and len(data) >= 13:
                        addr = int.from_bytes(data[8:10], "big")
                        count = int.from_bytes(data[10:12], "big")
                        msg = f"[RX 批量写问询] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{addr} | 数量:{count} | 帧:[{hex_frame}]"
                    else:
                        msg = f"[RX 问询报文] 从机:{unit_id} | FC:{fc:02X} | 帧:[{hex_frame}]"
                else:
                    msg = f"[RX 问询报文] 从机:{unit_id} | 帧:[{hex_frame}]"
            else:
                if len(data) >= 8:
                    fc = data[7]
                    if fc in (1, 2, 3, 4) and len(data) >= 9:
                        byte_count = data[8]
                        payload = data[9:]
                        if fc in (3, 4) and len(payload) >= 2:
                            regs = [int.from_bytes(payload[i:i+2], "big") for i in range(0, len(payload), 2)]
                            preview = ", ".join(str(r) for r in regs[:8])
                            if len(regs) > 8:
                                preview += f", ... (共{len(regs)}项)"
                            msg = f"[TX 从机响应] 从机:{unit_id} | FC:{fc:02X} | 字节数:{byte_count} | 数据:[{preview}] | 帧:[{hex_frame}]"
                        else:
                            msg = f"[TX 从机响应] 从机:{unit_id} | FC:{fc:02X} | 字节数:{byte_count} | 帧:[{hex_frame}]"
                    elif fc in (5, 6) and len(data) >= 12:
                        addr = int.from_bytes(data[8:10], "big")
                        val = int.from_bytes(data[10:12], "big")
                        msg = f"[TX 写入确认] 从机:{unit_id} | FC:{fc:02X} | 地址:{addr} | 确认成功 | 帧:[{hex_frame}]"
                    elif fc in (15, 16) and len(data) >= 12:
                        addr = int.from_bytes(data[8:10], "big")
                        count = int.from_bytes(data[10:12], "big")
                        msg = f"[TX 批量写确认] 从机:{unit_id} | FC:{fc:02X} | 地址:{addr} | 数量:{count} | 帧:[{hex_frame}]"
                    elif fc >= 0x80:
                        err_code = data[8] if len(data) >= 9 else 0
                        msg = f"[TX 异常响应] 从机:{unit_id} | FC:{fc:02X} | 异常码:{err_code} | 帧:[{hex_frame}]"
                    else:
                        msg = f"[TX 从机响应] 从机:{unit_id} | FC:{fc:02X} | 帧:[{hex_frame}]"
                else:
                    msg = f"[TX 从机响应] 从机:{unit_id} | 帧:[{hex_frame}]"
            return unit_id, msg
    except Exception as ex:
        logger.debug(f"解析从机报文异常: {ex}")
        return 0, f"[{comm_type} 原始报文] [{hex_frame}]"


# =====================================================================
# 2. 多从机路由与实时读写上下文 (MultiStationServerContext)
# =====================================================================
class MultiStationServerContext(ModbusServerContext):
    """支持多从机路由与实时读写的 Modbus 服务端数据上下文 (完全契合 MBAP Unit ID 路由规范)."""

    # [REVIEW-FIX-6] 显式调用父类 super().__init__() 规范初始化基类状态
    # 注意：当前实现依赖 pymodbus 的 context.old_simulator 分发机制将请求路由到 async_getValues/async_setValues。
    # 后续若升级 pymodbus，需重点回归多从机路由行为。
    def __init__(self):
        # 传入虚拟从机设备上下文以满足 pymodbus 3.x 基类入参非空约束
        dummy_dev = ModbusDeviceContext(co=ModbusSequentialDataBlock(1, [False]))
        super().__init__(devices={0: dummy_dev}, single=False)
        self.old_simulator = True
        self.simdevices = []
        self._devices = {}
        self._slaves: Dict[int, Any] = {}
        self._lock = threading.Lock()

    def device_ids(self) -> List[int]:
        with self._lock:
            return list(self._slaves.keys())

    # [REVIEW-FIX-4] 提供线程安全从机查询接口，使 MultiStationServerContext 成为从机挂载唯一真实源
    def get_slave(self, slave_id: int) -> Optional[Any]:
        with self._lock:
            return self._slaves.get(slave_id)

    def attach_slave(self, slave_id: int, engine: Any) -> None:
        with self._lock:
            self._slaves[slave_id] = engine
            self._devices[slave_id] = engine._device_context

    def detach_slave(self, slave_id: int) -> int:
        with self._lock:
            self._slaves.pop(slave_id, None)
            self._devices.pop(slave_id, None)
            return len(self._slaves)

    # [REVIEW-FIX-1] 引入显式地址基准计算，彻底废除根据运行时数值推断索引的 hack
    # [REVIEW-FIX-2] 在 async_setValues 中严格拦截只读功能码 (FC2, FC4)，禁止外部主站篡改只读存储区
    async def async_getValues(
        self, device_id: int, func_code: int, address: int, count: int = 1
    ) -> Union[List[int], List[bool], ExcCodes]:
        with self._lock:
            engine = self._slaves.get(device_id)
        if not engine:
            return ExcCodes.GATEWAY_NO_RESPONSE

        area_str = "4x_HoldingRegister"
        if func_code == 1:
            area_str = "0x_Coil"
        elif func_code == 2:
            area_str = "1x_DiscreteInput"
        elif func_code == 4:
            area_str = "3x_InputRegister"

        block = engine._get_datablock(area_str)
        if not block or not hasattr(block, "simdata") or not block.simdata:
            return ExcCodes.ILLEGAL_ADDRESS

        values_list = block.simdata[0].values
        # [REVIEW-FIX-1] 使用显式地址基准 address_base (默认为 1)，start_idx = address - address_base
        # 彻底移除 values_list[address] == 0 的运行时看值 hack，避免波形变化和 0 值引发数据漂移与 FLOAT32 撕裂
        base = getattr(engine, "address_base", 1)
        start_idx = address - (1 if base == 1 else 0)

        is_bit = func_code in (1, 2)
        res = []
        for i in range(count):
            idx = start_idx + i
            if 0 <= idx < len(values_list):
                v = values_list[idx]
                res.append(bool(v) if is_bit else (int(v) & 0xFFFF))
            else:
                # 越界安全保护：返回 0 或 False 填充，不抛出异常破坏通信连接
                res.append(False if is_bit else 0)
        return res

    async def async_setValues(
        self, device_id: int, func_code: int, address: int, values: List[Any]
    ) -> Optional[ExcCodes]:
        # [REVIEW-FIX-2] 功能码白名单校验：Modbus 协议规约中 FC2 (读离散输入) 和 FC4 (读输入寄存器) 严格只读
        # 主站不可使用或通过映射写入 1x/3x 只读区；若是只读功能码直接 warning 并 no-op 返回 None
        if func_code not in (5, 6, 15, 16):
            logger.warning(
                f"[非法写请求拦截] 拒绝向从机 {device_id} 执行非法功能码 FC:{func_code} 的写操作 (FC2/FC4 等只读区受保护)"
            )
            return None

        with self._lock:
            engine = self._slaves.get(device_id)
        if not engine:
            return ExcCodes.GATEWAY_NO_RESPONSE

        # 仅允许写入 0x_Coil (FC5, FC15) 或 4x_HoldingRegister (FC6, FC16)
        area_str = "0x_Coil" if func_code in (5, 15) else "4x_HoldingRegister"

        block = engine._get_datablock(area_str)
        if not block or not hasattr(block, "simdata") or not block.simdata:
            return ExcCodes.ILLEGAL_ADDRESS

        values_list = block.simdata[0].values
        # [REVIEW-FIX-1] 使用显式 address_base 计算写入索引，越界安全忽略
        base = getattr(engine, "address_base", 1)
        start_idx = address - (1 if base == 1 else 0)

        is_bit = func_code in (5, 15)
        for i, val in enumerate(values):
            idx = start_idx + i
            if 0 <= idx < len(values_list):
                values_list[idx] = bool(val) if is_bit else (int(val) & 0xFFFF)
        return None


# =====================================================================
# 3. 物理传输层监听服务 (ModbusTransportServer)
# =====================================================================
class ModbusTransportServer:
    """管理单一物理网络端口或物理串口上的 Modbus 传输层监听服务 (基于 MultiStationServerContext 支持多 Unit ID 挂载与动态路由)."""

    # [REVIEW-FIX-4] 方案 A (推荐)：ModbusTransportServer 不再自己重复维护 _slaves 与冗余嵌套锁
    # 所有从机挂载与查询统一委托给 MultiStationServerContext，彻底消除双份状态漂移与锁嵌套反向获取隐患
    def __init__(self, endpoint_key: tuple):
        self.endpoint_key = endpoint_key
        self.comm_type = endpoint_key[0]  # "TCP" 或 "RTU"
        self._lock = threading.Lock()

        # 核心：使用 MultiStationServerContext 作为从机路由与挂载的唯一真理源
        self._server_context = MultiStationServerContext()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._server_task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self._is_running = False
        self.on_packet_log: Optional[Callable[[str, str], None]] = None
        # [SLAVE-RECONNECT] 记录 start() 时刻，供 is_healthy() 做 0.3s 启动宽限判断
        self._start_ts: float = time.time()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def attach_slave(self, slave_id: int, engine: Any) -> None:
        """动态挂载从机数据区至 ServerContext (直接委托 Context，无锁嵌套)."""
        self._server_context.attach_slave(slave_id, engine)

    def detach_slave(self, slave_id: int) -> int:
        """动态注销从机，返回当前端点剩余从机数 (直接委托 Context，无锁嵌套)."""
        return self._server_context.detach_slave(slave_id)

    def get_slave(self, slave_id: int) -> Optional[Any]:
        """查询已挂载的从机实例."""
        return self._server_context.get_slave(slave_id)

    def active_slave_count(self) -> int:
        """获取当前物理端点已挂载从机总数."""
        return len(self._server_context.device_ids())

    def _on_trace_packet(self, sending: bool, data: bytes) -> bytes:
        if not data:
            return data
        try:
            unit_id, msg = format_modbus_trace_packet(self.comm_type, sending, data)
            slave = self._server_context.get_slave(unit_id)
            direction = "TX" if sending else "RX"
            if slave and slave.on_packet_log:
                slave.on_packet_log(msg, direction)
            elif self.on_packet_log:
                self.on_packet_log(msg, direction)
        except Exception as ex:
            logger.debug(f"trace_packet 异常: {ex}")
        return data

    # [SLAVE-RECONNECT] 新增健康探测方法：判断底层 listener 是否真正在监听
    def is_healthy(self) -> bool:
        if not self._is_running:
            return False
        # 事件循环线程还活着、loop 还在跑、_server 已被创建
        if not (self._thread and self._thread.is_alive()):
            return False
        if not (self._loop and self._loop.is_running()):
            return False
        if self._server is None:
            # 可能仍在启动过程中，给 0.3s 宽限
            return self._is_running and (time.time() - self._start_ts) < 0.3
        return True

    # [SLAVE-RECONNECT] 新增自愈重建方法：若底层 listener 僵死，stop + start 重建
    def restart_if_broken(self) -> bool:
        """若底层 listener 僵死（不健康），停止并重建；返回是否执行了重建."""
        if self.is_healthy():
            return False
        # 僵死：走一次 stop + start，重建 loop / server / 线程
        self.stop()
        # stop 后 _is_running 已清、_loop/_server/_thread 已置 None
        self.start()
        # start 后 5 个已注册的从机（_server_context._slaves）仍需保持挂载，
        # 因为 _server_context 是 self 的属性，未被 stop 清除；
        # 新起的 TCP server 仍用同一个 context，路由表保留，从机数据块保留。
        return True

    def start(self) -> None:
        # [SLAVE-RECONNECT] 已在健康运行 → 直接返回
        if self.is_healthy():
            return
        # 半死状态（_is_running=True 但底层僵死）→ 清理残留再重建
        if self._is_running:
            logger.warning(f"[SLAVE-RESTART] {self.endpoint_key} 检测到半死状态，强制重建 listener")
            self._is_running = False
            # 不 detach 从机，只清理 loop / thread / server 引用
            if self._loop:
                try:
                    self._loop.close()
                except Exception:
                    pass
                self._loop = None
            if self._thread:
                self._thread.join(timeout=0.5)
                self._thread = None
            self._server = None
            self._server_task = None
        self._is_running = True
        self._start_ts = time.time()
        self._thread = threading.Thread(target=self._run_server_loop, daemon=True)
        self._thread.start()
        for _ in range(30):
            if self._server:
                break
            time.sleep(0.03)

    # [REVIEW-FIX-3] 完善 stop() 资源彻底回收：取消 task 并在事件循环中安全 await shutdown()，避免 socket 与 loop 句柄泄漏
    def stop(self) -> None:
        self._is_running = False
        if self._loop and self._loop.is_running():
            async def _cleanup():
                if self._server:
                    try:
                        await self._server.shutdown()
                    except Exception as ex:
                        logger.debug(f"ModbusTransportServer shutdown 异常: {ex}")
                if self._server_task and not self._server_task.done():
                    self._server_task.cancel()
                    try:
                        await self._server_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as ex:
                        logger.debug(f"等待 server_task 取消异常: {ex}")

            try:
                fut = asyncio.run_coroutine_threadsafe(_cleanup(), self._loop)
                fut.result(timeout=2.0)
            except Exception as e:
                logger.debug(f"stop() 通知 loop 清理超时或异常: {e}")

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                logger.warning(f"ModbusTransportServer 后台服务线程 join 超时 ({self.endpoint_key})")

        # 确保全部引用彻底重置为 None，防止残留半清理状态
        self._thread = None
        self._loop = None
        self._server = None
        self._server_task = None

    def _run_server_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _serve():
            try:
                if self.comm_type == "RTU":
                    # ("RTU", serial_port, baudrate, bytesize, parity, stopbits)
                    _, s_port, baud, bsize, parity, stopb = self.endpoint_key
                    self._server = ModbusSerialServer(
                        context=self._server_context,
                        port=s_port,
                        baudrate=baud,
                        bytesize=bsize,
                        parity=parity,
                        stopbits=stopb,
                        trace_packet=self._on_trace_packet,
                    )
                else:
                    # ("TCP", host, port)
                    _, host, port = self.endpoint_key
                    self._server = ModbusTcpServer(
                        context=self._server_context,
                        address=(host, port),
                        trace_packet=self._on_trace_packet,
                    )
                self._server_task = asyncio.create_task(self._server.serve_forever())
                await self._server_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"ModbusTransportServer 运行异常 ({self.endpoint_key}): {e}")
                if self.on_packet_log:
                    self.on_packet_log(f"[服务异常崩溃] 无法监听 {self.endpoint_key}: {e}", "ERROR")

        try:
            self._loop.run_until_complete(_serve())
        finally:
            # [REVIEW-FIX-3] 确保在子线程结束时关闭事件循环，无 loop 句柄泄漏
            try:
                self._loop.close()
            except Exception:
                pass
            self._is_running = False
            # [SLAVE-RECONNECT] 异常退出时显式提醒，便于日志排查；下次 start() 将自动重建
            if self._server:
                logger.warning(
                    f"ModbusTransportServer listener 已退出 ({self.endpoint_key})，下次 start() 将自动重建"
                )


# =====================================================================
# 3. 物理端点传输中枢 (ModbusServerHub 单例)
# =====================================================================
class ModbusServerHub:
    """Modbus 传输层物理服务中枢 (单例)，维护各物理通信端点的单例 Server 实例."""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "ModbusServerHub":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = ModbusServerHub()
        return cls._instance

    def __init__(self):
        self._servers: Dict[tuple, ModbusTransportServer] = {}

    @staticmethod
    def make_endpoint_key(
        comm_type: str,
        host: str = "0.0.0.0",
        port: int = 502,
        serial_port: str = "COM1",
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
    ) -> tuple:
        c_type = str(comm_type).upper().strip()
        if c_type == "RTU":
            return ("RTU", str(serial_port).upper().strip(), int(baudrate), int(bytesize), str(parity).upper().strip(), int(stopbits))
        else:
            h = str(host).strip()
            h_norm = "0.0.0.0" if h in ("0.0.0.0", "", "*") else h
            return ("TCP", h_norm, int(port))

    def register_and_start_slave(self, engine: Any) -> Tuple[bool, str]:
        with self._lock:
            key = self.make_endpoint_key(
                comm_type=engine.comm_type,
                host=engine.host,
                port=engine.port,
                serial_port=engine.serial_port,
                baudrate=engine.baudrate,
                bytesize=engine.bytesize,
                parity=engine.parity,
                stopbits=engine.stopbits,
            )
            server = self._servers.get(key)
            if server is None and key[0] == "TCP":
                for (c, h, p), s in self._servers.items():
                    if c == "TCP" and p == key[2] and (h == "0.0.0.0" or key[1] == "0.0.0.0" or h == key[1]):
                        server = s
                        key = (c, h, p)
                        break

            # [SLAVE-RECONNECT-3b] 复用前探活：僵死则丢弃并新建，避免复用已死的 listener
            if server and not server.is_healthy():
                logger.warning(f"[SLAVE-RECONNECT-3b] {key} 已有僵死 listener，重建新实例")
                server = None

            if server is None:
                server = ModbusTransportServer(key)
                self._servers[key] = server
                server.start()

            # [REVIEW-FIX-4] 站地址冲突检查：直接查询 server.get_slave，无需嵌套持有 server._lock
            if server.get_slave(engine.slave_id) is not None:
                return False, f"物理端点 {key} 下已存在站地址 {engine.slave_id} 的从机！"

            server.attach_slave(engine.slave_id, engine)
            return True, "挂载成功"

    # [REVIEW-FIX-5] 避免在 Hub 全局锁 (self._lock) 内执行 server.stop() (含阻塞等待)，防止跨端点读写与操作被无辜阻塞卡顿
    def unregister_and_stop_slave(self, engine: Any) -> None:
        server_to_stop: Optional[ModbusTransportServer] = None
        with self._lock:
            key = self.make_endpoint_key(
                comm_type=engine.comm_type,
                host=engine.host,
                port=engine.port,
                serial_port=engine.serial_port,
                baudrate=engine.baudrate,
                bytesize=engine.bytesize,
                parity=engine.parity,
                stopbits=engine.stopbits,
            )
            server = self._servers.get(key)
            if server is None and key[0] == "TCP":
                for (c, h, p), s in self._servers.items():
                    if c == "TCP" and p == key[2] and (h == "0.0.0.0" or key[1] == "0.0.0.0" or h == key[1]):
                        server = s
                        key = (c, h, p)
                        break

            if server:
                remaining = server.detach_slave(engine.slave_id)
                if remaining == 0:
                    self._servers.pop(key, None)
                    server_to_stop = server

        # 在 Hub 全局锁外部执行耗时的底层网络/串口停止与线程 join
        if server_to_stop:
            server_to_stop.stop()

    def stop_all(self) -> None:
        servers_to_stop = []
        with self._lock:
            servers_to_stop = list(self._servers.values())
            self._servers.clear()
        for s in servers_to_stop:
            s.stop()

    # [SLAVE-RECONNECT] 按 engine 的端点参数反查该 engine 挂载的 transport server
    def get_server_for_engine(self, engine: Any) -> Optional[ModbusTransportServer]:
        with self._lock:
            key = self.make_endpoint_key(
                comm_type=engine.comm_type, host=engine.host, port=engine.port,
                serial_port=engine.serial_port, baudrate=engine.baudrate,
                bytesize=engine.bytesize, parity=engine.parity, stopbits=engine.stopbits,
            )
            server = self._servers.get(key)
            if server is None and key[0] == "TCP":
                for (c, h, p), s in self._servers.items():
                    if c == "TCP" and p == key[2] and (h == "0.0.0.0" or key[1] == "0.0.0.0" or h == key[1]):
                        return s
            return server

    # [SLAVE-RECONNECT] 扫描所有端点，重建所有僵死 listener；返回重建数量
    def restart_all_broken(self) -> int:
        restarted = 0
        with self._lock:
            servers = list(self._servers.values())
        for srv in servers:
            if srv.restart_if_broken():
                restarted += 1
        return restarted


# =====================================================================
# 4. Slave 从机逻辑引擎 (ModbusSlaveEngine)
# =====================================================================
class ModbusSlaveEngine:
    """Modbus Slave (从机模拟器) 引擎 (同时支持以太网 TCP 与串行端口 RTU 232/485，支持同端口多站并发挂载)."""

    def __init__(
        self,
        comm_type: str = "TCP",
        host: str = "0.0.0.0",
        port: int = 5020,
        serial_port: str = "COM1",
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        slave_id: int = 1,
        address_base: int = 1,
    ):
        self.comm_type = comm_type.upper()  # "TCP" 或 "RTU"
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.slave_id = slave_id
        # [REVIEW-FIX-1] 显式定义地址偏移基准 (默认 1-based)，消除启发式看值推断
        self.address_base = address_base

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._server_task: Optional[asyncio.Task] = None
        self._is_running = False

        # 初始化 4 大存储区，每个存储区预分配 65535 个点位 (标准 Modbus 地址 1~65535)
        self._coils = ModbusSequentialDataBlock(1, [False] * 65535)
        self._discrete_inputs = ModbusSequentialDataBlock(1, [False] * 65535)
        self._input_registers = ModbusSequentialDataBlock(1, [0] * 65535)
        self._holding_registers = ModbusSequentialDataBlock(1, [0] * 65535)

        self._device_context = ModbusDeviceContext(
            di=self._discrete_inputs,
            co=self._coils,
            hr=self._holding_registers,
            ir=self._input_registers,
        )
        # 保留单机上下文兼容已有属性
        self._server_context = ModbusServerContext(devices=self._device_context, single=True)

        # 模拟点位配置表: address -> {type, mode, sim_rule, ...}
        self.points: Dict[Union[int, Tuple[str, int]], Dict[str, Any]] = {}
        self._sim_thread: Optional[threading.Thread] = None
        self._sim_running = False
        # 外部通信问询与响应报文回调: (message, level) -> None
        self.on_packet_log: Optional[Callable[[str, str], None]] = None
        # 模拟点位数值实时变动通知回调: (area, address, new_val) -> None
        self.on_point_value_changed: Optional[Callable[[str, int, Any], None]] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    # [SLAVE-RECONNECT] 新增健康探测：该从机对应的底层 transport 是否仍正常监听
    def is_healthy(self) -> bool:
        if not self._is_running:
            return False
        # 找到本从机所在的 ModbusTransportServer（通过 endpoint_key 反查 Hub）
        hub = ModbusServerHub.get_instance()
        server = hub.get_server_for_engine(self)
        return server.is_healthy() if server else False

    def start(self) -> None:
        """启动从机服务：通过物理中枢挂载到共享物理端口并启动波形仿真."""
        if self._is_running:
            return

        hub = ModbusServerHub.get_instance()
        ok, err = hub.register_and_start_slave(self)
        if not ok:
            raise RuntimeError(err)

        self._is_running = True

        # 启动模拟数据定时刷新线程
        self._sim_running = True
        self._sim_thread = threading.Thread(target=self._run_simulation_loop, daemon=True)
        self._sim_thread.start()

    def stop(self) -> None:
        """停止从机服务并从物理传输中枢安全注销 (最后从机停止时释放端口)."""
        self._sim_running = False
        self._is_running = False

        hub = ModbusServerHub.get_instance()
        hub.unregister_and_stop_slave(self)

        if self._sim_thread:
            self._sim_thread.join(timeout=1.0)
            self._sim_thread = None

    def _on_trace_packet(self, sending: bool, data: bytes) -> bytes:
        """兼容性报文拦截."""
        unit_id, msg = format_modbus_trace_packet(self.comm_type, sending, data)
        if self.on_packet_log:
            self.on_packet_log(msg, "TX" if sending else "RX")
        return data

    def _get_function_code(self, area: str) -> int:
        area_s = str(area).upper()
        if "0X" in area_s or "COIL" in area_s or "线圈" in str(area):
            return 1
        elif "1X" in area_s or "DISCRETE" in area_s or "离散" in str(area):
            return 2
        elif "3X" in area_s or "INPUT" in area_s or "输入" in str(area):
            return 4
        return 3  # HOLDING_REGISTER

    def _get_datablock(self, area: str):
        """获取本地直接数据块 (支持在服务未启动时预热写入，严密匹配存储区域)."""
        area_s = str(area).upper()
        if "0X" in area_s or "COIL" in area_s or "线圈" in str(area):
            return self._coils
        elif "1X" in area_s or "DISCRETE" in area_s or "离散" in str(area):
            return self._discrete_inputs
        elif "3X" in area_s or "INPUT" in area_s or "输入" in str(area):
            return self._input_registers
        return self._holding_registers

    def read_raw_values(self, area: str, address: int, count: int) -> List[int]:
        """读取底层原始数据 (线程安全，支持服务启动前后无缝读取)."""
        block = self._get_datablock(area)
        if block and hasattr(block, "simdata") and block.simdata:
            try:
                base_addr = block.simdata[0].address
                start_idx = address - 1 - base_addr
                values_list = block.simdata[0].values
                res = []
                for i in range(count):
                    idx = start_idx + i
                    if 0 <= idx < len(values_list):
                        v = values_list[idx]
                        res.append(1 if v is True else 0 if v is False else int(v))
                    else:
                        res.append(0)
                return res
            except Exception as e:
                logger.error(f"read_raw_values 异常: {e}")
        return [0] * count

    def write_raw_values(self, area: str, address: int, values: List[int]) -> None:
        """写入底层原始数据 (线程安全，支持服务启动前后无缝同步写入)."""
        block = self._get_datablock(area)
        if block and hasattr(block, "simdata") and block.simdata:
            try:
                base_addr = block.simdata[0].address
                start_idx = address - 1 - base_addr
                values_list = block.simdata[0].values
                is_bit = (AreaType.COIL in area or AreaType.DISCRETE_INPUT in area)
                for i, val in enumerate(values):
                    idx = start_idx + i
                    if 0 <= idx < len(values_list):
                        values_list[idx] = bool(val) if is_bit else (int(val) & 0xFFFF)
            except Exception as ex:
                logger.warning(f"写入数据块异常 ({area}:{address}): {ex}")

    def write_typed_value(
        self,
        area: str,
        address: int,
        value: Any,
        data_type: Any,
        mode: Any = ByteOrderMode.ABCD,
        byte_order: Any = None,
    ) -> None:
        """以指定数据类型和变位模式写入数值 (同时兼容 mode 与 byte_order 参数)."""
        actual_mode = byte_order if byte_order is not None else mode
        if AreaType.COIL in area or AreaType.DISCRETE_INPUT in area or "0X" in str(area).upper() or "1X" in str(area).upper():
            try:
                b_val = bool(int(float(str(value).strip())))
            except Exception:
                b_val = str(value).strip().lower() in ("1", "true", "yes", "on")
            self.write_raw_values(area, address, [1 if b_val else 0])
        else:
            regs = encode_value(value, data_type, actual_mode)
            self.write_raw_values(area, address, regs)

    def read_typed_value(
        self,
        area: str,
        address: int,
        data_type: ModbusDataType,
        mode: ByteOrderMode = ByteOrderMode.ABCD,
    ) -> Any:
        """以指定数据类型和变位模式读取数值."""
        count = TYPE_REGISTER_COUNT.get(data_type, 1)
        raw = self.read_raw_values(area, address, count)
        if AreaType.COIL in area or AreaType.DISCRETE_INPUT in area:
            return bool(raw[0])
        return decode_value(raw, data_type, mode)

    def _run_simulation_loop(self) -> None:
        """后台模拟更新循环 (支持 (area, addr) 复合键，消除多物理区域同地址冲突)."""
        step = 0
        import math
        import random

        while self._sim_running:
            time.sleep(1.0)
            step = (step + 1) % 100000

            # 制作字典项安全快照，防止多线程迭代期间大小并发变动
            items_snapshot = list(self.points.items())
            has_composite = any(isinstance(k, tuple) for k, _ in items_snapshot)

            for key, point in items_snapshot:
                # 若存在复合键 (area, addr)，忽略历史单一整型 key 以避免重复模拟
                if has_composite and not isinstance(key, tuple):
                    continue

                if isinstance(key, tuple) and len(key) >= 2:
                    k_area, addr = str(key[0]), int(key[1])
                else:
                    addr = int(key)
                    k_area = point.get("area", AreaType.HOLDING_REGISTER)

                sim_mode = point.get("sim_mode") or point.get("sim_rule") or "固定"
                if sim_mode == "固定":
                    continue

                dt_raw = point.get("data_type") or point.get("type") or ModbusDataType.INT16
                try:
                    data_type = dt_raw if isinstance(dt_raw, ModbusDataType) else ModbusDataType(str(dt_raw))
                except Exception:
                    data_type = ModbusDataType.INT16

                bo_raw = point.get("byte_order") or point.get("mode") or ByteOrderMode.ABCD
                try:
                    mode = bo_raw if isinstance(bo_raw, ByteOrderMode) else ByteOrderMode(str(bo_raw))
                except Exception:
                    mode = ByteOrderMode.ABCD

                area = point.get("area", AreaType.HOLDING_REGISTER)
                curr = point.get("current_val", point.get("value", 0))

                # 功能码 01(线圈) 与 02(离散输入)：只要有模拟规则，数据就在 0 和 1 之间翻转变动
                is_fc01_02 = (
                    "0X" in str(area).upper()
                    or "1X" in str(area).upper()
                    or "COIL" in str(area).upper()
                    or "DISCRETE" in str(area).upper()
                    or point.get("fc") in (1, 2)
                    or data_type == ModbusDataType.BOOL
                )
                if is_fc01_02:
                    curr_bool_int = 1 if (curr is True or str(curr) in ("1", "True", "true")) else 0
                    if sim_mode == "正弦波":
                        # 正弦波对于布尔量：按时钟相位正负半周切换高低电平 (0/1)
                        phase = (step * 0.35) + ((addr % 10) * 0.4)
                        new_val = 1 if math.sin(phase) >= 0 else 0
                    elif sim_mode == "方波":
                        phase = (step + (addr % 5)) % 6
                        new_val = 1 if phase >= 3 else 0
                    elif sim_mode == "随机波动":
                        new_val = random.choice([0, 1])
                    else:
                        new_val = 0 if curr_bool_int == 1 else 1

                    point["current_val"] = new_val
                    self.write_typed_value(area, addr, new_val, data_type, mode)
                    if self.on_point_value_changed:
                        try:
                            self.on_point_value_changed(area, addr, new_val)
                        except Exception:
                            pass
                    continue

                # 功能码 03(保持寄存器) 与 04(输入寄存器)：受具体模拟规则算法与数据类型限制
                # 1. 确保当前值在数据类型的合法范围内
                curr = clamp_value_to_type(curr, data_type)

                # 2. 字符/十六进制/二进制不自动波动
                if data_type in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
                    continue

                # 3. 数值类型强类型模拟 (严格遵守上下限，无符号数绝不产生负数)
                limits = TYPE_RANGES.get(data_type, (-32768, 32767, int))
                min_v, max_v, v_type = limits

                if sim_mode in ("累加递增", "步进递增"):
                    if v_type is int:
                        wrap_limit = min(max_v, 10000)
                        new_val = (curr + 1) if curr < wrap_limit else min_v
                    else:
                        new_val = round((curr + 0.5) % 1000.0, 2)

                elif sim_mode == "随机波动":
                    if v_type is int:
                        delta = random.choice([-2, -1, 0, 1, 2])
                        if min_v == 0 and curr <= 1:
                            delta = random.choice([0, 1, 2, 3])
                        new_val = int(max(min_v, min(max_v, curr + delta)))
                    else:
                        delta = random.uniform(-0.5, 0.5)
                        new_val = round(max(min_v, min(max_v, curr + delta)), 2)

                elif sim_mode == "正弦波":
                    # 正弦波算法：结合时钟 step 与点位地址 addr 相位偏移，生成丝滑不同步的正弦波浪
                    phase = (step * 0.25) + ((addr % 100) * 0.15)
                    sin_factor = math.sin(phase)
                    if min_v == 0:  # 无符号整型 (如 UINT16: 0~65535)
                        base = 50.0 if (curr == 0) else max(25.0, float(curr))
                        amp = min(25.0, base * 0.5)
                        calc_val = base + amp * sin_factor
                    else:  # 有符号整型/浮点数 (如 INT16: -32768~32767)
                        base = 0.0 if (curr == 0) else float(curr)
                        calc_val = base + 30.0 * sin_factor

                    if v_type is int:
                        new_val = int(max(min_v, min(max_v, round(calc_val))))
                    else:
                        new_val = round(max(min_v, min(max_v, calc_val)), 2)

                elif sim_mode == "方波":
                    phase = (step + (addr % 10)) % 10
                    high_val = 100 if min_v == 0 else 50
                    low_val = 0 if min_v == 0 else -50
                    val_out = high_val if phase >= 5 else low_val
                    if v_type is int:
                        new_val = int(max(min_v, min(max_v, val_out)))
                    else:
                        new_val = float(max(min_v, min(max_v, val_out)))

                else:
                    new_val = curr

                # 保存并写入底层 Context
                point["current_val"] = new_val
                self.write_typed_value(area, addr, new_val, data_type, mode)

                # 回调通知外部模型同步
                if self.on_point_value_changed:
                    try:
                        self.on_point_value_changed(area, addr, new_val)
                    except Exception:
                        pass


# =====================================================================
# 2. Poll 主机轮询客户端引擎
# =====================================================================
class ModbusPollEngine:
    """Modbus Poll (主机轮询与测试客户端) 引擎 (同时支持 TCP 网口与 RTU 串口)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5020, slave_id: int = 1):
        self.comm_type = "TCP"  # "TCP" 或 "RTU"
        self.host = host
        self.port = port
        self.serial_port = "COM1"
        self.baudrate = 9600
        self.bytesize = 8
        self.parity = "N"
        self.stopbits = 1
        self.slave_id = slave_id

        self.client = None
        self._poll_thread: Optional[threading.Thread] = None
        self._is_polling = False
        self._poll_interval = 1.0  # 秒
        # [POLL-CONFIG] 引入退出事件通知机制，避免 stop_polling 后产生双线程轮询
        self._poll_stop_event = threading.Event()

        # 统计指标
        self.tx_count = 0
        self.rx_count = 0
        self.err_count = 0
        self.last_rtt_ms = 0.0

        # 当前读取到的原始寄存器缓存 (address -> raw_value)
        self.cached_registers: Dict[int, int] = {}
        # 轮询回调函数
        self.on_poll_success: Optional[Callable[[Dict[int, int]], None]] = None
        self.on_poll_error: Optional[Callable[[str], None]] = None
        # 问询与响应报文日志回调: (message, level) -> None
        self.on_packet_log: Optional[Callable[[str, str], None]] = None

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.connected

    @property
    def is_polling(self) -> bool:
        return self._is_polling

    def connect_tcp(self, host: str = "", port: int = 0) -> bool:
        """建立以太网 Modbus TCP 连接."""
        if host:
            self.host = host
        if port:
            self.port = port
        self.comm_type = "TCP"
        if self.is_connected:
            return True
        self.client = ModbusTcpClient(self.host, port=self.port, timeout=2.0)
        return self.client.connect()

    def connect_rtu(
        self,
        serial_port: str = "",
        baudrate: int = 0,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
    ) -> bool:
        """建立串行端口 Modbus RTU 连接."""
        if serial_port:
            self.serial_port = serial_port
        if baudrate:
            self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.comm_type = "RTU"
        if self.is_connected:
            return True
        self.client = ModbusSerialClient(
            port=self.serial_port,
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=self.parity,
            stopbits=self.stopbits,
            timeout=2.0,
        )
        return self.client.connect()

    def connect(self) -> bool:
        """根据当前 comm_type 自动建立连接."""
        if self.comm_type == "RTU":
            return self.connect_rtu()
        return self.connect_tcp()

    def disconnect(self) -> None:
        """断开连接并停止轮询."""
        self.stop_polling()
        if self.client:
            self.client.close()
            self.client = None

    def read_block(self, area: str, start_addr: int, count: int) -> Tuple[bool, List[int], str]:
        """单次读取指定区域数据块 (线程安全，支持 TCP 与 RTU).
        :return: (is_success, values, error_message)
        """
        if not self.is_connected:
            if not self.connect():
                target_desc = f"串口 {self.serial_port}" if self.comm_type == "RTU" else f"{self.host}:{self.port}"
                if self.on_packet_log:
                    self.on_packet_log(f"[ERR 连接失败] 无法建立与从机 ({target_desc}) 的连接", "ERROR")
                return False, [], f"无法连接到从机 ({target_desc})"

        t0 = time.perf_counter()
        self.tx_count += 1
        res = None

        fc = 3
        fc_desc = "读保持寄存器"
        if AreaType.COIL in area:
            fc = 1
            fc_desc = "读线圈"
        elif AreaType.DISCRETE_INPUT in area:
            fc = 2
            fc_desc = "读离散输入"
        elif AreaType.INPUT_REGISTER in area:
            fc = 4
            fc_desc = "读输入寄存器"

        if self.comm_type == "RTU":
            req_hex = build_modbus_rtu_req_hex(self.slave_id, fc, start_addr, count)
        else:
            req_hex = build_modbus_tcp_req_hex(self.slave_id, fc, start_addr, count, self.tx_count)

        if self.on_packet_log:
            self.on_packet_log(
                f"[TX 问询报文] ID:{self.slave_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{start_addr} | 数量:{count} | 帧:[{req_hex}]",
                "TX",
            )

        try:
            if AreaType.COIL in area:
                res = self.client.read_coils(start_addr, count=count, device_id=self.slave_id)
            elif AreaType.DISCRETE_INPUT in area:
                res = self.client.read_discrete_inputs(start_addr, count=count, device_id=self.slave_id)
            elif AreaType.INPUT_REGISTER in area:
                res = self.client.read_input_registers(start_addr, count=count, device_id=self.slave_id)
            else:  # HOLDING_REGISTER
                res = self.client.read_holding_registers(start_addr, count=count, device_id=self.slave_id)

            t1 = time.perf_counter()
            self.last_rtt_ms = round((t1 - t0) * 1000, 2)

            if res.isError():
                self.err_count += 1
                err_msg = str(res)
                if self.on_packet_log:
                    self.on_packet_log(f"[ERR 问询异常] ID:{self.slave_id} | FC:{fc:02X} | 错误: {err_msg}", "ERROR")
                return False, [], err_msg

            self.rx_count += 1
            if AreaType.COIL in area or AreaType.DISCRETE_INPUT in area:
                vals = [1 if b else 0 for b in res.bits[:count]]
            else:
                vals = list(res.registers[:count])

            for i, v in enumerate(vals):
                self.cached_registers[start_addr + i] = v

            if self.on_packet_log:
                preview = ", ".join(str(x) for x in vals[:8])
                if len(vals) > 8:
                    preview += f", ... (共{len(vals)}项)"
                self.on_packet_log(
                    f"[RX 响应报文] ID:{self.slave_id} | FC:{fc:02X} | 耗时:{self.last_rtt_ms:.1f}ms | 项数:{len(vals)} | 数据:[{preview}]",
                    "RX",
                )

            return True, vals, ""

        except Exception as e:
            self.err_count += 1
            err_msg = str(e)
            if self.on_packet_log:
                self.on_packet_log(f"[ERR 问询异常] ID:{self.slave_id} | FC:{fc:02X} | 异常: {err_msg}", "ERROR")
            return False, [], err_msg

    def write_single_register(self, address: int, value: int) -> Tuple[bool, str]:
        """功能码 06: 写单个保持寄存器."""
        if not self.is_connected and not self.connect():
            return False, "未连接"
        if self.comm_type == "RTU":
            req_hex = build_modbus_rtu_req_hex(self.slave_id, 0x06, address, value & 0xFFFF)
        else:
            req_hex = build_modbus_tcp_req_hex(self.slave_id, 0x06, address, value & 0xFFFF, self.tx_count + 1)
        if self.on_packet_log:
            self.on_packet_log(f"[TX 写入报文] ID:{self.slave_id} | FC:06(写单个保持寄存器) | 地址:{address} | 数值:{value} | 帧:[{req_hex}]", "TX")
        res = self.client.write_register(address, value & 0xFFFF, device_id=self.slave_id)
        if res.isError():
            err_msg = str(res)
            if self.on_packet_log:
                self.on_packet_log(f"[ERR 写入异常] ID:{self.slave_id} | FC:06 | 错误: {err_msg}", "ERROR")
            return False, err_msg
        if self.on_packet_log:
            self.on_packet_log(f"[RX 写入响应] ID:{self.slave_id} | FC:06 | 写入保持寄存器成功", "RX")
        return True, "写入成功"

    def write_single_coil(self, address: int, value: bool) -> Tuple[bool, str]:
        """功能码 05: 写单个线圈."""
        if not self.is_connected and not self.connect():
            return False, "未连接"
        coil_val = 0xFF00 if value else 0x0000
        if self.comm_type == "RTU":
            req_hex = build_modbus_rtu_req_hex(self.slave_id, 0x05, address, coil_val)
        else:
            req_hex = build_modbus_tcp_req_hex(self.slave_id, 0x05, address, coil_val, self.tx_count + 1)
        if self.on_packet_log:
            self.on_packet_log(f"[TX 写入报文] ID:{self.slave_id} | FC:05(写单个线圈) | 地址:{address} | 状态:{value} | 帧:[{req_hex}]", "TX")
        res = self.client.write_coil(address, value, device_id=self.slave_id)
        if res.isError():
            err_msg = str(res)
            if self.on_packet_log:
                self.on_packet_log(f"[ERR 写入异常] ID:{self.slave_id} | FC:05 | 错误: {err_msg}", "ERROR")
            return False, err_msg
        if self.on_packet_log:
            self.on_packet_log(f"[RX 写入响应] ID:{self.slave_id} | FC:05 | 写入线圈成功", "RX")
        return True, "写入成功"

    def write_typed(
        self,
        area: str,
        address: int,
        value: Any,
        data_type: ModbusDataType,
        mode: ByteOrderMode = ByteOrderMode.ABCD,
    ) -> Tuple[bool, str]:
        """按类型和变位模式写入寄存器或线圈."""
        if not self.is_connected and not self.connect():
            return False, "未连接"

        if AreaType.COIL in area:
            b_val = bool(int(value)) if str(value).isdigit() else bool(value)
            return self.write_single_coil(address, b_val)

        regs = encode_value(value, data_type, mode)
        if len(regs) == 1:
            return self.write_single_register(address, regs[0])
        else:
            if self.on_packet_log:
                hex_regs = ", ".join(f"0x{r:04X}" for r in regs)
                self.on_packet_log(
                    f"[TX 写入报文] ID:{self.slave_id} | FC:10(写多个保持寄存器) | 起始地址:{address} | 寄存器数:{len(regs)} | 值:[{hex_regs}]",
                    "TX",
                )
            res = self.client.write_registers(address, regs, device_id=self.slave_id)
            if res.isError():
                err_msg = str(res)
                if self.on_packet_log:
                    self.on_packet_log(f"[ERR 写入异常] ID:{self.slave_id} | FC:10 | 错误: {err_msg}", "ERROR")
                return False, err_msg
            if self.on_packet_log:
                self.on_packet_log(f"[RX 写入响应] ID:{self.slave_id} | FC:10 | 成功写入 {len(regs)} 个寄存器", "RX")
            return True, f"成功写入 {len(regs)} 个寄存器"

    # [POLL-CONFIG] 启动后台连续定时循环轮询
    # 注意：on_poll_success / on_poll_error 回调直接在 worker 线程中触发，UI 消费者须自行调度切换到主线程
    def start_polling(self, area: str, start_addr: int, count: int, interval_ms: int = 1000) -> None:
        """启动后台定时循环轮询.

        注意：on_poll_success / on_poll_error 由后台 worker 线程调用，UI 消费者须自行切回主线程进行渲染。
        """
        if self._is_polling:
            return
        self._is_polling = True
        self._poll_stop_event.clear()
        self._poll_interval = max(0.1, interval_ms / 1000.0)

        def _poll_worker():
            while self._is_polling and not self._poll_stop_event.is_set():
                success, vals, err = self.read_block(area, start_addr, count)
                if success and self.on_poll_success:
                    # [POLL-CONFIG] 方案 A：使用本地 fresh 字典存储本次读取到的有效地址值，避免残留历史旧地址脏数据
                    fresh = {start_addr + i: v for i, v in enumerate(vals)}
                    self.on_poll_success(fresh)
                elif not success and self.on_poll_error:
                    self.on_poll_error(err)

                # 使用 Event 等待间隔时间，若收到停止事件则立即退出循环，无需干等整段 sleep
                if self._poll_stop_event.wait(timeout=self._poll_interval):
                    break

        self._poll_thread = threading.Thread(target=_poll_worker, daemon=True)
        self._poll_thread.start()

    # [POLL-CONFIG] 安全停止后台连续轮询，防止遗留后台游离线程产生并发读写冲突
    def stop_polling(self) -> None:
        """停止轮询."""
        self._is_polling = False
        self._poll_stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)
            if self._poll_thread.is_alive():
                logger.warning("ModbusPollEngine 轮询后台线程在 2.0s 内未退出，等待其自动终止")
            self._poll_thread = None
