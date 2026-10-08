"""Modbus 通信引擎 (Slave 从机服务 + Poll 主机轮询客户端).

结合 pymodbus 与 modbus_codec，支持全数据类型与 4 种变位模式的读写。
"""

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from pymodbus.client import ModbusTcpClient
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

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
# 1. Slave 从机服务引擎
# =====================================================================
class ModbusSlaveEngine:
    """Modbus TCP Slave (从机模拟器) 引擎."""

    def __init__(self, host: str = "0.0.0.0", port: int = 5020, slave_id: int = 1):
        self.host = host
        self.port = port
        self.slave_id = slave_id

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[ModbusTcpServer] = None
        self._server_task: Optional[asyncio.Task] = None
        self._is_running = False

        # 初始化 4 大存储区，每个存储区预分配 1000 个点位
        self._coils = ModbusSequentialDataBlock(1, [False] * 1000)
        self._discrete_inputs = ModbusSequentialDataBlock(1, [False] * 1000)
        self._input_registers = ModbusSequentialDataBlock(1, [0] * 1000)
        self._holding_registers = ModbusSequentialDataBlock(1, [0] * 1000)

        self._device_context = ModbusDeviceContext(
            di=self._discrete_inputs,
            co=self._coils,
            hr=self._holding_registers,
            ir=self._input_registers,
        )
        self._server_context = ModbusServerContext(devices=self._device_context, single=True)

        # 模拟点位配置表: address -> {type, mode, sim_rule, ...}
        self.points: Dict[int, Dict[str, Any]] = {}
        self._sim_thread: Optional[threading.Thread] = None
        self._sim_running = False
        # 外部通信问询与响应报文回调: (message, level) -> None
        self.on_packet_log: Optional[Callable[[str, str], None]] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start(self) -> None:
        """在独立后台线程中启动 Slave 服务."""
        if self._is_running:
            return

        self._is_running = True
        self._thread = threading.Thread(target=self._run_server_loop, daemon=True)
        self._thread.start()

        # 等待服务器初始化就绪
        for _ in range(20):
            if self._server and self._server.context:
                break
            time.sleep(0.05)

        # 启动模拟数据定时刷新线程
        self._sim_running = True
        self._sim_thread = threading.Thread(target=self._run_simulation_loop, daemon=True)
        self._sim_thread.start()

    def stop(self) -> None:
        """停止 Slave 服务."""
        self._sim_running = False
        self._is_running = False
        if self._loop and self._server_task:
            self._loop.call_soon_threadsafe(self._server_task.cancel)
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._server = None

    def _on_trace_packet(self, sending: bool, data: bytes) -> bytes:
        """拦截并解析从机收发的 Modbus TCP 报文 (RX 外部问询 / TX 从机响应)."""
        if not data or len(data) < 7:
            return data
        try:
            hex_frame = " ".join(f"{b:02X}" for b in data)
            trans_id = int.from_bytes(data[0:2], "big")
            length = int.from_bytes(data[4:6], "big")
            unit_id = data[6]

            if not sending:
                # RX: 外部主站发来的问询报文
                if len(data) >= 8:
                    fc = data[7]
                    fc_desc = {
                        1: "读线圈", 2: "读离散输入", 3: "读保持寄存器", 4: "读输入寄存器",
                        5: "写单个线圈", 6: "写单个保持寄存器", 15: "写多个线圈", 16: "写多个保持寄存器"
                    }.get(fc, f"功能码0x{fc:02X}")

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

                    if self.on_packet_log:
                        self.on_packet_log(msg, "RX")
            else:
                # TX: 从机回复给外部主站的响应报文
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

                    if self.on_packet_log:
                        self.on_packet_log(msg, "TX")
        except Exception as ex:
            logger.debug(f"解析从机报文异常: {ex}")
        return data

    def _run_server_loop(self) -> None:
        """异步事件循环运行函数."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _serve():
            try:
                self._server = ModbusTcpServer(
                    context=self._server_context,
                    address=(self.host, self.port),
                    trace_packet=self._on_trace_packet,
                )
                self._server_task = asyncio.create_task(self._server.serve_forever())
                await self._server_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Slave 运行异常: {e}")

        try:
            self._loop.run_until_complete(_serve())
        finally:
            self._loop.close()
            self._is_running = False

    def _get_function_code(self, area: str) -> int:
        if AreaType.COIL in area:
            return 1
        elif AreaType.DISCRETE_INPUT in area:
            return 2
        elif AreaType.INPUT_REGISTER in area:
            return 4
        return 3  # HOLDING_REGISTER

    def read_raw_values(self, area: str, address: int, count: int) -> List[int]:
        """读取底层原始数据 (线程安全跨事件循环调用)."""
        if not self._server or not self._loop or not self._server.context:
            return [0] * count
        dev0 = getattr(self._server.context, "devices", {}).get(0)
        if not dev0:
            return [0] * count

        fc = self._get_function_code(area)
        try:
            fut = asyncio.run_coroutine_threadsafe(
                dev0.async_getValues(fc, address, count), self._loop
            )
            res = fut.result(timeout=1.0)
            if isinstance(res, list):
                return [1 if x is True else 0 if x is False else int(x) for x in res]
        except Exception as e:
            logger.error(f"read_raw_values 失败: {e}")
        return [0] * count

    def write_raw_values(self, area: str, address: int, values: List[int]) -> None:
        """写入底层原始数据 (线程安全跨事件循环调用)."""
        if not self._server or not self._loop or not self._server.context:
            return
        dev0 = getattr(self._server.context, "devices", {}).get(0)
        if not dev0:
            return

        fc = self._get_function_code(area)
        try:
            if AreaType.COIL in area or AreaType.DISCRETE_INPUT in area:
                bool_vals = [bool(x) for x in values]
                fut = asyncio.run_coroutine_threadsafe(
                    dev0.async_setValues(fc, address, bool_vals), self._loop
                )
            else:
                fut = asyncio.run_coroutine_threadsafe(
                    dev0.async_setValues(fc, address, [int(x) & 0xFFFF for x in values]), self._loop
                )
            fut.result(timeout=1.0)
        except Exception as e:
            logger.error(f"write_raw_values 失败: {e}")

    def write_typed_value(
        self,
        area: str,
        address: int,
        value: Any,
        data_type: ModbusDataType,
        mode: ByteOrderMode = ByteOrderMode.ABCD,
    ) -> None:
        """以指定数据类型和变位模式写入数值."""
        if AreaType.COIL in area or AreaType.DISCRETE_INPUT in area:
            b_val = bool(int(value)) if str(value).isdigit() else bool(value)
            self.write_raw_values(area, address, [1 if b_val else 0])
        else:
            regs = encode_value(value, data_type, mode)
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
        """后台模拟更新循环 (强关联数据类型安全更新，杜绝无符号溢出与异常巨值)."""
        step = 0
        import math
        import random

        while self._sim_running:
            time.sleep(1.0)
            step += 1
            for addr, point in list(self.points.items()):
                sim_mode = point.get("sim_mode", "固定")
                if sim_mode == "固定":
                    continue

                data_type = point.get("data_type", ModbusDataType.INT16)
                mode = point.get("byte_order", ByteOrderMode.ABCD)
                area = point.get("area", AreaType.HOLDING_REGISTER)
                curr = point.get("current_val", 0)

                # 1. 确保当前值在数据类型的合法范围内
                curr = clamp_value_to_type(curr, data_type)

                # 2. BOOL / Bit
                if data_type == ModbusDataType.BOOL:
                    if sim_mode == "累加递增":
                        new_val = 1 if curr == 0 else 0
                    elif sim_mode == "随机波动":
                        new_val = random.choice([0, 1])
                    elif sim_mode == "正弦波":
                        new_val = 1 if math.sin(step * 0.2) > 0 else 0
                    else:
                        new_val = curr

                # 3. 字符/十六进制/二进制不自动波动
                elif data_type in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
                    continue

                # 4. 数值类型强类型模拟 (严格遵守上下限，无符号数绝不产生负数)
                else:
                    limits = TYPE_RANGES.get(data_type, (-32768, 32767, int))
                    min_v, max_v, v_type = limits

                    if sim_mode == "累加递增":
                        if v_type is int:
                            wrap_limit = min(max_v, 10000)
                            if min_v == 0:  # 无符号数 (UINT16 / UINT32 / UINT64)
                                new_val = (curr + 1) if curr < wrap_limit else 0
                            else:
                                new_val = (curr + 1) if curr < wrap_limit else 0
                        else:
                            new_val = round((curr + 0.5) % 1000.0, 2)

                    elif sim_mode == "随机波动":
                        if v_type is int:
                            # 整数步长为整数，且无符号整型下限必须严格大于等于 0
                            delta = random.choice([-2, -1, 0, 1, 2])
                            if min_v == 0 and curr <= 1:
                                delta = random.choice([0, 1, 2, 3])
                            new_val = int(max(min_v, min(max_v, curr + delta)))
                        else:
                            delta = random.uniform(-0.5, 0.5)
                            new_val = round(max(min_v, min(max_v, curr + delta)), 2)

                    elif sim_mode == "正弦波":
                        if min_v == 0:
                            base = max(30.0, float(curr) if curr > 0 else 50.0)
                            amp = min(20.0, base * 0.5)
                            calc_val = base + amp * math.sin(step * 0.2)
                        else:
                            calc_val = 50.0 + 20.0 * math.sin(step * 0.2)

                        if v_type is int:
                            new_val = int(max(min_v, min(max_v, round(calc_val))))
                        else:
                            new_val = round(max(min_v, min(max_v, calc_val)), 2)
                    else:
                        new_val = curr

                # 保存并写入底层 Context
                point["current_val"] = new_val
                self.write_typed_value(area, addr, new_val, data_type, mode)


# =====================================================================
# 2. Poll 主机轮询客户端引擎
# =====================================================================
class ModbusPollEngine:
    """Modbus TCP Poll (主机轮询与测试客户端) 引擎."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5020, slave_id: int = 1):
        self.host = host
        self.port = port
        self.slave_id = slave_id

        self.client: Optional[ModbusTcpClient] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._is_polling = False
        self._poll_interval = 1.0  # 秒

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

    def connect(self) -> bool:
        """建立 TCP 连接."""
        if self.is_connected:
            return True
        self.client = ModbusTcpClient(self.host, port=self.port, timeout=2.0)
        return self.client.connect()

    def disconnect(self) -> None:
        """断开连接并停止轮询."""
        self.stop_polling()
        if self.client:
            self.client.close()
            self.client = None

    def read_block(self, area: str, start_addr: int, count: int) -> Tuple[bool, List[int], str]:
        """单次读取指定区域数据块 (线程安全).

        :return: (is_success, values, error_message)
        """
        if not self.is_connected:
            if not self.connect():
                if self.on_packet_log:
                    self.on_packet_log(f"[ERR 连接失败] 无法建立与从机 {self.host}:{self.port} 的 TCP 连接", "ERROR")
                return False, [], "无法连接到从机"

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

    def start_polling(self, area: str, start_addr: int, count: int, interval_ms: int = 1000) -> None:
        """启动后台定时循环轮询."""
        if self._is_polling:
            return
        self._is_polling = True
        self._poll_interval = max(0.1, interval_ms / 1000.0)

        def _poll_worker():
            while self._is_polling:
                success, vals, err = self.read_block(area, start_addr, count)
                if success and self.on_poll_success:
                    self.on_poll_success(self.cached_registers)
                elif not success and self.on_poll_error:
                    self.on_poll_error(err)
                time.sleep(self._poll_interval)

        self._poll_thread = threading.Thread(target=_poll_worker, daemon=True)
        self._poll_thread.start()

    def stop_polling(self) -> None:
        """停止轮询."""
        self._is_polling = False
        if self._poll_thread:
            self._poll_thread.join(timeout=1.0)
            self._poll_thread = None
