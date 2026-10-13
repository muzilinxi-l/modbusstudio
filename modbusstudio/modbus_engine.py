"""
模块职责：Modbus 通信底层核心引擎模块，提供从机模拟服务端与主机轮询客户端的完整协议栈实现。
涵盖四大核心层次：
    1. 报文协议帧构建与拦截解析（RTU CRC16 校验、MBAP 报头封装、收发报文解码日志）。
    2. 多从机路由上下文（MultiStationServerContext）：按 MBAP Unit ID / RTU 站地址将请求精准分发到对应从机实例。
    3. 物理传输层监听服务与单例中枢（ModbusTransportServer + ModbusServerHub）：管理端口复用、健康探活自愈与优雅启停。
    4. 从机模拟器引擎（ModbusSlaveEngine）与主机轮询引擎（ModbusPollEngine）。
关键依赖：
    - asyncio / threading：驱动 pymodbus 异步服务端事件循环与后台波形仿真/轮询守护线程。
    - pymodbus (client / datastore / server / constants)：工业标准 Modbus TCP/RTU 协议栈实现。
    - .modbus_codec：寄存器字序重排、强类型编解码与数值钳位工具。
对外暴露：
    - AreaType：引擎层四大功能区常量。
    - crc16_modbus / build_modbus_rtu_req_hex / build_modbus_tcp_req_hex / build_modbus_tcp_resp_hex / format_modbus_trace_packet：报文工具函数。
    - MultiStationServerContext / ModbusTransportServer / ModbusServerHub：多从机物理端点复用与路由架构。
    - ModbusSlaveEngine：单站点从机模拟器引擎。
    - ModbusPollEngine：主机轮询与写入测试客户端引擎。
"""
# ==============================================================================
# 【核心引擎架构与协议准则导读】
# 1. 字节序铁律：Modbus 寄存器在线路传输时固定为大端序（高字节在前），唯独 RTU 尾部的 CRC16 校验码为小端序（低字节在前）。
# 2. 端口共享模型：同一 TCP (IP:Port) 或 RTU 串口仅启动一个 ModbusTransportServer，通过 MultiStationServerContext 按 Unit ID 路由。
# 3. 单一事实源：从机挂载字典仅保存在 MultiStationServerContext._slaves 中，ModbusTransportServer 直接委托查询，消除双锁死锁隐患。
# 4. 确定性地址偏移：统一通过 engine.address_base (默认 1) 计算数组索引 start_idx = address - base，严禁根据运行时数值猜测偏移。
# 5. 只读存储区防篡改：在 async_setValues 入口严格校验功能码白名单 (5, 6, 15, 16)，拦截对 FC2 (离散输入) 与 FC4 (输入寄存器) 的非法写操作。
# 6. 事件驱动极速启动：通过 threading.Event (_ready) 在 _server_task 创建完成瞬间唤醒主调线程，将单端口启动耗时从 0.9s 盲等降至 ~2ms。
# 7. 跨线程协程清理：stop() 必须通过 asyncio.run_coroutine_threadsafe 将 shutdown() 投递回子线程事件循环执行，防止套接字句柄泄漏。
# 8. 锁外停止原则：ModbusServerHub.unregister_and_stop_slave 在锁内仅摘除字典引用，释放全局锁后再执行耗时的 server.stop()。
# 9. 三重探活自愈：is_healthy() 同时检查 _is_running、_thread.is_alive()、_loop.is_running() 与 0.3s 启动宽限，支持半死状态无损重建。
# 10. 仿真线程可唤醒退出：_run_simulation_loop 使用 _sim_stop_evt.wait(1) 替代 time.sleep(1)，使从机停止操作在微秒级立即响应。
# 11. 轮询防重入与脏数据隔离：ModbusPollEngine 通过 _poll_stop_event + join 确保单线程轮询，回调仅传递当次读取的 fresh 字典。
# ==============================================================================

# 为什么导入 asyncio：pymodbus 3.x 的服务端（ModbusTcpServer / ModbusSerialServer）完全基于协程构建，
# 需要在独立后台线程中创建专属 EventLoop 来运行异步监听任务
import asyncio
import logging
# 为什么导入 threading：Tkinter GUI 主线程是单线程事件循环，耗时的网络/串口监听、波形仿真与定时轮询
# 必须放在后台守护线程中运行，并通过 Lock 与 Event 实现跨线程安全同步
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import warnings

# 为什么过滤 DeprecationWarning：屏蔽 pymodbus 版本演进过程中内部 API 触发的弃用警告，保持控制台与日志整洁
warnings.filterwarnings("ignore", category=DeprecationWarning)

from pymodbus.client import ModbusSerialClient, ModbusTcpClient
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusSerialServer, ModbusTcpServer
from pymodbus.constants import ExcCodes

# 为什么使用 try-except 双路径导入：兼顾作为包导入（python -m modbusstudio）与直接脚本执行两种启动方式
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


# 初始化引擎模块级日志记录器，供网络异常、非法写拦截与探活重建输出诊断信息
logger = logging.getLogger("ModbusEngine")


class AreaType:
    """职责：定义引擎层使用的四大 Modbus 存储区域标识字符串常量。"""
    # 设计说明：
    # 与领域层 models.AreaType 的 friendly_name 保持一致，便于服务层直接透传或按前缀（0x/1x/3x/4x）模糊匹配。

    # 0x 线圈区（功能码 FC01 读 / FC05 单写 / FC15 多写，可读写布尔位）
    COIL = "0x_Coil (线圈)"
    # 1x 离散输入区（功能码 FC02 读，严格只读布尔位）
    DISCRETE_INPUT = "1x_DiscreteInput (离散输入)"
    # 3x 输入寄存器区（功能码 FC04 读，严格只读 16 位寄存器）
    INPUT_REGISTER = "3x_InputRegister (输入寄存器)"
    # 4x 保持寄存器区（功能码 FC03 读 / FC06 单写 / FC16 多写，可读写 16 位寄存器）
    HOLDING_REGISTER = "4x_HoldingRegister (保持寄存器)"


def crc16_modbus(data: bytes) -> bytes:
    """计算工业标准 Modbus RTU 报文的 CRC-16 校验码（低字节在前，高字节在后）。"""
    # 参数：
    # data: 不含 CRC 尾部的原始 RTU 报文帧字节序列（站地址 + 功能码 + 数据区）。
    # 返回：
    # bytes: 长度为 2 的字节串 `[crc_low, crc_high]`。
    # 关键步骤（为什么这么写）：
    # 1. 初值设为 `0xFFFF`（Modbus 标准规定全 1 寄存器初值，可检测前导零字节丢失）。
    # 2. 逐字节与 CRC 低 8 位异或，随后执行 8 次右移：若移出位为 1，则与反向生成多项式 `0xA001`
    # （即标准多项式 `0x8005` 的按位反转）异或。
    # 3. 注意 Modbus RTU 规定只有 CRC16 字段采用小端序（低字节先发、高字节后发），与数据区大端序相反！
    # 初值置为 0xFFFF，符合 Modbus 串行链路规约标准
    crc = 0xFFFF
    for b in data:
        # 当前字节并入 CRC 低字节
        crc ^= b
        # 逐位右移处理 8 个比特
        for _ in range(8):
            if crc & 0x0001:
                # 最低位为 1 时右移一位并与反射多项式 0xA001 异或
                crc = (crc >> 1) ^ 0xA001
            else:
                # 最低位为 0 时仅右移一位
                crc >>= 1
    # 为什么低字节在前：Modbus RTU 规范要求 CRC 校验码先发送低 8 位（crc & 0xFF），再发送高 8 位
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def build_modbus_rtu_req_hex(slave_id: int, fc: int, start_addr: int, count_or_val: int) -> str:
    """构建 Modbus RTU 读/单写请求的标准十六进制报文字符串（含尾部 CRC16 校验码）。"""
    # 参数：
    # slave_id: 目标从机站地址（1~247）。
    # fc: Modbus 功能码（如 1/2/3/4 读，5/6 单写）。
    # start_addr: 起始寄存器/线圈地址（16 位无符号整数）。
    # count_or_val: 读取时的点位数量，或单写时的设定数值（16 位无符号整数）。
    # 返回：
    # str: 以空格分隔的大写十六进制字符串（共 8 字节，如 `"01 03 00 00 00 0A C5 CD"`）。
    # 组装前 6 字节请求主体：1B 站地址 + 1B 功能码 + 2B 大端起始地址 + 2B 大端数量/数值
    raw = bytes([
        slave_id & 0xFF,
        fc & 0xFF,
        (start_addr >> 8) & 0xFF, start_addr & 0xFF,
        (count_or_val >> 8) & 0xFF, count_or_val & 0xFF,
    ])
    # 计算并追加 2 字节小端 CRC16 校验码
    crc = crc16_modbus(raw)
    return " ".join(f"{b:02X}" for b in (raw + crc))


def build_modbus_tcp_req_hex(slave_id: int, fc: int, start_addr: int, count_or_val: int, tx_id: int = 1) -> str:
    """构建 Modbus TCP 读/单写请求的标准十六进制报文字符串（7 字节 MBAP 报头 + 5 字节 PDU）。"""
    # 参数：
    # slave_id: 单元标识符 Unit ID（即从机站地址）。
    # fc: Modbus 功能码。
    # start_addr: 起始寄存器/线圈地址。
    # count_or_val: 读取数量或单写设定值。
    # tx_id: 事务元标识符（Transaction ID），每次请求递增以匹配异步响应。
    # 返回：
    # str: 以空格分隔的大写十六进制字符串（共 12 字节）。
    # 设计说明（为什么 TCP 不需要 CRC16）：
    # Modbus TCP 运行在 TCP/IP 协议栈之上，以太网帧 FCS 与 TCP 校验和已保证传输完整性，
    # 因此去掉了 RTU 尾部的 2 字节 CRC，改为在头部前置 7 字节 MBAP（Modbus Application Protocol）报头。
    raw = [
        (tx_id >> 8) & 0xFF, tx_id & 0xFF,  # 事务元标识符 (2 字节，大端)
        0x00, 0x00,                         # 协议标识符 (Modbus TCP 固定为 0x0000)
        0x00, 0x06,                         # 后续字节长度 (UnitID 1B + PDU 5B = 6 字节)
        slave_id & 0xFF,                    # 单元标识符 (从机站地址 Unit ID)
        fc & 0xFF,                          # 功能码 (1 字节)
        (start_addr >> 8) & 0xFF, start_addr & 0xFF,       # 寄存器起始地址 (2 字节，大端)
        (count_or_val >> 8) & 0xFF, count_or_val & 0xFF     # 读取数量或写入数值 (2 字节，大端)
    ]
    return " ".join(f"{b:02X}" for b in raw)


def build_modbus_tcp_resp_hex(slave_id: int, fc: int, data_bytes: List[int], tx_id: int = 1) -> str:
    """构建 Modbus TCP 读响应的标准十六进制报文字符串（MBAP 报头 + 含字节计数的响应 PDU）。"""
    # 参数：
    # slave_id: 响应从机的单元标识符 Unit ID。
    # fc: 响应功能码。
    # data_bytes: 响应数据载荷字节列表（每个元素为 0~255 的整数）。
    # tx_id: 与请求报文对应的事务元标识符。
    # 返回：
    # str: 以空格分隔的大写十六进制报文字符串。
    # 组装响应 PDU：1B 功能码 + 1B 载荷字节数 + N 字节数据载荷
    pdu = [fc & 0xFF, len(data_bytes) & 0xFF] + [b & 0xFF for b in data_bytes]
    # MBAP Length 字段定义为 Unit ID (1 字节) 加上整个 PDU 的字节长度
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
    """拦截并解析从机收发的原始 Modbus 字节流，提取目标站地址并生成人类可读的结构化中文诊断日志。"""
    # 参数：
    # comm_type: 通信规约类型（`"RTU"` 或 `"TCP"`）。
    # sending: 报文方向标志（`False` 表示从机接收到的主机问询 RX，`True` 表示从机发出的响应 TX）。
    # data: 底层 socket 或串口实际收发的原始字节串。
    # 返回：
    # Tuple[int, str]: `(unit_id, 格式化中文日志字符串)`。若报文为空或解析异常，安全兜底返回原始帧，绝不抛出异常中断通信。
    # 关键步骤（为什么这么写）：
    # - 在共享物理端口挂载多个从机（多 Unit ID）时，底层 `ModbusTcpServer` / `ModbusSerialServer` 只有一个 `trace_packet` 钩子。
    # - 通过在钩子中实时解析报文头部（RTU 第 0 字节、TCP MBAP 第 6 字节）提取 `unit_id`，
    # 才能将报文精准归属并分发到对应从机实例的日志回调与 UI 通信监控台中。
    # 空数据包直接快速返回，避免无意义解析
    if not data:
        return 0, ""
    try:
        # 将原始字节流格式化为空格分隔的大写 Hex 字符串，供日志尾部附上完整帧原文
        hex_frame = " ".join(f"{b:02X}" for b in data)
        # 标准 Modbus 功能码到中文语义名称的映射字典
        fc_names = {
            1: "读线圈", 2: "读离散输入", 3: "读保持寄存器", 4: "读输入寄存器",
            5: "写单个线圈", 6: "写单个保持寄存器", 15: "写多个线圈", 16: "写多个保持寄存器"
        }

        if comm_type == "RTU":
            # RTU 最小合法帧长度为 4 字节（站地址 1B + 功能码 1B + CRC 2B），不足则原样输出
            if len(data) < 4:
                return (data[0] if len(data) > 0 else 0), f"[RTU 原始帧] [{hex_frame}]"
            # RTU 帧首字节即为从机站地址，次字节为功能码，末尾两字节为 CRC16
            unit_id = data[0]
            fc = data[1]
            fc_desc = fc_names.get(fc, f"功能码0x{fc:02X}")
            crc_hex = f"{data[-2]:02X} {data[-1]:02X}"

            if not sending:
                # --- 分支 A1：从机接收 (RX) 到的 RTU 主机问询帧 ---
                if fc in (1, 2, 3, 4) and len(data) >= 8:
                    # 读请求固定 8 字节：[站地址][FC][起始地址高][低][数量高][低][CRC低][高]
                    start_addr = int.from_bytes(data[2:4], "big")
                    count = int.from_bytes(data[4:6], "big")
                    msg = f"[RX 串口问询] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{start_addr} | 数量:{count} | 帧:[{hex_frame}] (CRC:{crc_hex})"
                elif fc in (5, 6) and len(data) >= 8:
                    # 单写请求固定 8 字节：[站地址][FC][目标地址高][低][设定值高][低][CRC低][高]
                    addr = int.from_bytes(data[2:4], "big")
                    val = int.from_bytes(data[4:6], "big")
                    val_hex = f"0x{val:04X}"
                    msg = f"[RX 串口写问询] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 目标地址:{addr} | 设定值:{val} ({val_hex}) | 帧:[{hex_frame}]"
                elif fc in (15, 16) and len(data) >= 9:
                    # 批量写请求至少 9 字节：[站地址][FC][起始地址2B][数量2B][字节数1B][数据...][CRC2B]
                    addr = int.from_bytes(data[2:4], "big")
                    count = int.from_bytes(data[4:6], "big")
                    msg = f"[RX 串口批量写] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{addr} | 数量:{count} | 帧:[{hex_frame}]"
                else:
                    msg = f"[RX 串口问询] 从机:{unit_id} | FC:{fc:02X} | 帧:[{hex_frame}]"
            else:
                # --- 分支 A2：从机发出 (TX) 的 RTU 响应帧 ---
                if fc in (1, 2, 3, 4) and len(data) >= 5:
                    # 读响应：[站地址][FC][字节数][数据载荷...][CRC2B]
                    byte_count = data[2]
                    payload = data[3:-2]
                    if fc in (3, 4) and len(payload) >= 2:
                        # 对保持/输入寄存器读响应，前 8 个寄存器解码为十进制预览，方便工程师肉眼核对数据
                        regs = [int.from_bytes(payload[i:i+2], "big") for i in range(0, len(payload), 2)]
                        preview = ", ".join(str(r) for r in regs[:8])
                        if len(regs) > 8:
                            preview += f", ... (共{len(regs)}项)"
                        msg = f"[TX 串口响应] 从机:{unit_id} | FC:{fc:02X} | 字节数:{byte_count} | 数据:[{preview}] | 帧:[{hex_frame}]"
                    else:
                        msg = f"[TX 串口响应] 从机:{unit_id} | FC:{fc:02X} | 字节数:{byte_count} | 帧:[{hex_frame}]"
                elif fc in (5, 6) and len(data) >= 8:
                    # 单写响应回显原请求地址与写入值
                    addr = int.from_bytes(data[2:4], "big")
                    val = int.from_bytes(data[4:6], "big")
                    msg = f"[TX 串口写确认] 从机:{unit_id} | FC:{fc:02X} | 地址:{addr} | 确认成功 | 帧:[{hex_frame}]"
                elif fc >= 0x80:
                    # 功能码最高位置 1（即 >= 0x80）代表 Modbus 异常响应帧，data[2] 为异常码
                    err_code = data[2] if len(data) >= 3 else 0
                    msg = f"[TX 串口异常响应] 从机:{unit_id} | FC:{fc:02X} | 异常码:{err_code} | 帧:[{hex_frame}]"
                else:
                    msg = f"[TX 串口响应] 从机:{unit_id} | FC:{fc:02X} | 帧:[{hex_frame}]"
            return unit_id, msg

        else:
            # --- TCP 规约帧 (含 7 字节 MBAP 报头，字节 6 为 Unit ID) ---
            if len(data) < 7:
                return 0, f"[TCP 原始帧] [{hex_frame}]"
            # 解析 7 字节 MBAP 报头：[0:2]事务ID, [2:4]协议ID, [4:6]后续长度, [6]Unit ID
            trans_id = int.from_bytes(data[0:2], "big")
            length = int.from_bytes(data[4:6], "big")
            unit_id = data[6]

            if not sending:
                # --- 分支 B1：从机接收 (RX) 到的 TCP 主机问询帧 ---
                if len(data) >= 8:
                    fc = data[7]
                    fc_desc = fc_names.get(fc, f"功能码0x{fc:02X}")
                    if fc in (1, 2, 3, 4) and len(data) >= 12:
                        # TCP 读请求 12 字节：7B MBAP + 1B FC + 2B 起始地址 + 2B 数量
                        start_addr = int.from_bytes(data[8:10], "big")
                        count = int.from_bytes(data[10:12], "big")
                        msg = f"[RX 问询报文] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{start_addr} | 数量:{count} | 帧:[{hex_frame}]"
                    elif fc in (5, 6) and len(data) >= 12:
                        # TCP 单写请求 12 字节：7B MBAP + 1B FC + 2B 目标地址 + 2B 设定值
                        addr = int.from_bytes(data[8:10], "big")
                        val = int.from_bytes(data[10:12], "big")
                        val_hex = f"0x{val:04X}"
                        msg = f"[RX 写入问询] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 目标地址:{addr} | 设定值:{val} ({val_hex}) | 帧:[{hex_frame}]"
                    elif fc in (15, 16) and len(data) >= 13:
                        # TCP 批量写请求：7B MBAP + 1B FC + 2B 起始地址 + 2B 数量 + 1B 字节数 + 数据...
                        addr = int.from_bytes(data[8:10], "big")
                        count = int.from_bytes(data[10:12], "big")
                        msg = f"[RX 批量写问询] 从机:{unit_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{addr} | 数量:{count} | 帧:[{hex_frame}]"
                    else:
                        msg = f"[RX 问询报文] 从机:{unit_id} | FC:{fc:02X} | 帧:[{hex_frame}]"
                else:
                    msg = f"[RX 问询报文] 从机:{unit_id} | 帧:[{hex_frame}]"
            else:
                # --- 分支 B2：从机发出 (TX) 的 TCP 响应帧 ---
                if len(data) >= 8:
                    fc = data[7]
                    if fc in (1, 2, 3, 4) and len(data) >= 9:
                        # TCP 读响应：7B MBAP + 1B FC + 1B 字节数 + 数据载荷...
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
        # 任何报文截断或畸形都不应影响底层正常收发，记录 debug 日志后返回原始 Hex 兜底
        logger.debug(f"解析从机报文异常: {ex}")
        return 0, f"[{comm_type} 原始报文] [{hex_frame}]"


# =====================================================================
# 2. 多从机路由与实时读写上下文 (MultiStationServerContext)
# =====================================================================
class MultiStationServerContext(ModbusServerContext):
    """职责：支持同端口多从机（多 Unit ID）动态路由与实时读写的服务端数据上下文。"""
    # 成员变量：
    # - old_simulator (bool): 设为 True，启用 pymodbus 3.x 将读写请求转发给 `async_getValues`/`async_setValues` 的分发路径。
    # - _slaves (Dict[int, ModbusSlaveEngine]): 已挂载的站地址到从机引擎实例的映射表（唯一真实数据源）。
    # - _devices (Dict[int, ModbusDeviceContext]): 兼容 pymodbus 基类结构的设备上下文字典。
    # - _lock (threading.Lock): 保护 `_slaves` 字典并发挂载、卸载与查询的互斥锁。
    # 使用场景：
    # 每个 `ModbusTransportServer` 持有一个 `MultiStationServerContext` 实例。当 EMS 一体机向同一 IP:Port（如 502）
    # 或同一 RS485 串口轮询不同站地址（如 BMS=1, PCS=2）时，由本类根据报文中的 `device_id` 路由到对应从机的数据块。

    # [REVIEW-FIX-6] 显式调用父类 super().__init__() 规范初始化基类状态
    # 注意：当前实现依赖 pymodbus 的 context.old_simulator 分发机制将请求路由到 async_getValues/async_setValues。
    # 后续若升级 pymodbus，需重点回归多从机路由行为。
    def __init__(self):
        """初始化多从机服务端路由上下文。"""
        # 关键步骤：
        # 1. 构造一个虚拟 `ModbusDeviceContext` 传给父类 `super().__init__()`，满足 pymodbus 3.x 非空入参检查。
        # 2. 开启 `self.old_simulator = True`，让 pymodbus 请求处理器直接回调本类的 `async_getValues` 和 `async_setValues`。
        # 3. 初始化线程锁 `self._lock` 与从机映射表 `self._slaves`。
        # 传入虚拟从机设备上下文以满足 pymodbus 3.x 基类入参非空约束
        dummy_dev = ModbusDeviceContext(co=ModbusSequentialDataBlock(1, [False]))
        super().__init__(devices={0: dummy_dev}, single=False)
        # 为什么设 old_simulator = True：pymodbus 3.x 中只有当 context.old_simulator 为真时，
        # 才会把每次协议读写委托给 context.async_getValues / async_setValues 协程处理
        self.old_simulator = True
        self.simdevices = []
        self._devices = {}
        self._slaves: Dict[int, Any] = {}
        self._lock = threading.Lock()

    def device_ids(self) -> List[int]:
        """获取当前已挂载的所有从机站地址（Unit ID）列表（线程安全）。"""
        # 返回：
        # List[int]: 当前处于激活挂载状态的从机站地址列表快照。
        with self._lock:
            return list(self._slaves.keys())

    # [REVIEW-FIX-4] 提供线程安全从机查询接口，使 MultiStationServerContext 成为从机挂载唯一真实源
    def get_slave(self, slave_id: int) -> Optional[Any]:
        """按站地址线程安全地查询已挂载的从机引擎实例。"""
        # 参数：
        # slave_id: 目标从机站地址（Unit ID）。
        # 返回：
        # Optional[ModbusSlaveEngine]: 匹配到的从机引擎实例；若未挂载则返回 `None`。
        with self._lock:
            return self._slaves.get(slave_id)

    def attach_slave(self, slave_id: int, engine: Any) -> None:
        """将指定站地址的从机引擎动态挂载到当前路由上下文中（线程安全）。"""
        # 参数：
        # slave_id: 从机站地址（Unit ID）。
        # engine: 待挂载的 `ModbusSlaveEngine` 实例。
        with self._lock:
            self._slaves[slave_id] = engine
            self._devices[slave_id] = engine._device_context

    def detach_slave(self, slave_id: int) -> int:
        """从当前路由上下文中安全卸载指定站地址的从机，并返回卸载后剩余的挂载从机总数。"""
        # 参数：
        # slave_id: 待卸载的从机站地址（Unit ID）。
        # 返回：
        # int: 当前物理端点下剩余仍在运行的从机数量（当返回 0 时，上层 Hub 将关闭物理监听端口）。
        with self._lock:
            self._slaves.pop(slave_id, None)
            self._devices.pop(slave_id, None)
            return len(self._slaves)

    # [REVIEW-FIX-1] 引入显式地址基准计算，彻底废除根据运行时数值推断索引的 hack
    # [REVIEW-FIX-2] 在 async_setValues 中严格拦截只读功能码 (FC2, FC4)，禁止外部主站篡改只读存储区
    async def async_getValues(
        self, device_id: int, func_code: int, address: int, count: int = 1
    ) -> Union[List[int], List[bool], ExcCodes]:
        """异步处理外部主机发来的 Modbus 读取请求（支持 FC01/FC02/FC03/FC04）。"""
        # 参数：
        # device_id: 请求报文中的目标站地址（Unit ID）。
        # func_code: Modbus 功能码（1=读线圈, 2=读离散输入, 3=读保持寄存器, 4=读输入寄存器）。
        # address: 请求报文中的起始地址。
        # count: 请求读取的连续寄存器或线圈数量。
        # 返回：
        # Union[List[int], List[bool], ExcCodes]: 读取到的数值列表；若目标站地址不存在则返回 `GATEWAY_NO_RESPONSE`。
        # 关键步骤（为什么这么写）：
        # 1. 仅在读取 `self._slaves` 字典时短暂加锁，随后立即释放锁再读取数据块，避免高并发轮询锁竞争。
        # 2. 使用显式 `address_base` 计算数组下标 `start_idx = address - (1 if base == 1 else 0)`，
        # 彻底消灭旧实现中“根据运行时值是否为 0 猜测地址偏移”导致的 FLOAT32 高低字错位撕裂 bug。
        # 3. 越界访问时返回 `0` 或 `False` 安全填充，防止EMS主机大跨度批量扫描点表时因单个越界地址断开整条连接。
        # 1. 根据报文中的 device_id 查找目标从机实例
        with self._lock:
            engine = self._slaves.get(device_id)
        if not engine:
            # 若该站地址未在本端口注册挂载，返回网关无响应异常码
            return ExcCodes.GATEWAY_NO_RESPONSE

        # 2. 将功能码映射到对应的四大存储区标识
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

        # 3. 逐个提取点位值：FC01/02 强转为 bool，FC03/04 掩码截断为 16 位无符号整数
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
        """异步处理外部主机发来的 Modbus 写入请求（严格限定可写功能码 FC05/FC06/FC15/FC16）。"""
        # 参数：
        # device_id: 请求报文中的目标站地址（Unit ID）。
        # func_code: Modbus 功能码（仅允许 5=写单线圈, 6=写单保持寄存器, 15=写多线圈, 16=写多保持寄存器）。
        # address: 写入起始地址。
        # values: 待写入的数值或布尔列表。
        # 返回：
        # Optional[ExcCodes]: 写入成功返回 `None`；若站地址未挂载则返回 `GATEWAY_NO_RESPONSE`。
        # 关键步骤（为什么这么写）：
        # - **只读区防篡改（[REVIEW-FIX-2]）**：Modbus 规范中 `1x_DiscreteInput`（FC2）与 `3x_InputRegister`（FC4）
        # 属于严格只读区域（如传感器实时采样值）。若不拦截非法功能码，外部异常请求可能污染只读存储区。
        # [REVIEW-FIX-2] 功能码白名单校验：Modbus 协议规约中 FC2 (读离散输入) 和 FC4 (读输入寄存器) 严格只读
        # 主站不可使用或通过映射写入 1x/3x 只读区；若是只读功能码直接 warning 并 no-op 返回 None
        if func_code not in (5, 6, 15, 16):
            logger.warning(
                f"[非法写请求拦截] 拒绝向从机 {device_id} 执行非法功能码 FC:{func_code} 的写操作 (FC2/FC4 等只读区受保护)"
            )
            return None

        # 查找目标从机实例
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

        # 将主机写入值逐个落盘到底层 simdata 数组
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
    """职责：管理单一物理网络端口（TCP `host:port`）或物理串口（RTU `COMx`）上的底层 pymodbus 异步监听服务。"""
    # 成员变量：
    # - endpoint_key (tuple): 唯一标识物理端点的元组，如 `("TCP", "0.0.0.0", 502)` 或 `("RTU", "COM1", 9600, 8, "N", 1)`。
    # - _server_context (MultiStationServerContext): 承载本物理端口上所有已挂载从机路由的上下文（唯一真实源）。
    # - _loop (Optional[asyncio.AbstractEventLoop]): 后台监听线程专属的 asyncio 事件循环。
    # - _server (Optional[Union[ModbusTcpServer, ModbusSerialServer]]): pymodbus 底层异步服务端对象。
    # - _server_task (Optional[asyncio.Task]): 运行 `serve_forever()` 的协程任务句柄。
    # - _thread (Optional[threading.Thread]): 承载事件循环的后台守护线程。
    # - _ready (threading.Event): 服务端创建就绪信号量，使 `start()` 在 1~5ms 内事件驱动立即返回，无需固定盲等。
    # 设计说明（为什么不自己维护 `_slaves` 字典）：
    # 遵循单一事实源（Single Source of Truth）原则，将所有从机挂载、卸载与查询全部委托给 `self._server_context`，
    # 既消除了双份字典状态漂移，也避免了 `ModbusTransportServer._lock` 与 `MultiStationServerContext._lock` 嵌套死锁。

    # [REVIEW-FIX-4] 方案 A (推荐)：ModbusTransportServer 不再自己重复维护 _slaves 与冗余嵌套锁
    # 所有从机挂载与查询统一委托给 MultiStationServerContext，彻底消除双份状态漂移与锁嵌套反向获取隐患
    def __init__(self, endpoint_key: tuple):
        """初始化指定物理端点的传输层监听服务。"""
        # 参数：
        # endpoint_key: 经 `ModbusServerHub.make_endpoint_key` 归一化后的物理端点标识元组。
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
        # [SLAVE-SPEED-1] 启动就绪事件：替代 30×0.03s 固定盲等循环
        self._ready = threading.Event()

    @property
    def is_running(self) -> bool:
        """返回当前物理传输层监听服务是否处于运行标记状态。"""
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
        """pymodbus 底层报文收发拦截回调钩子（`trace_packet`）。"""
        # 参数：
        # sending: `True` 表示 TX 发送响应，`False` 表示 RX 接收问询。
        # data: 原始报文字节流。
        # 返回：
        # bytes: 必须原样返回 `data` 字节流，否则 pymodbus 底层将无法继续处理或发送该报文！
        # 关键步骤：
        # 1. 调用 `format_modbus_trace_packet` 提取报文所属的 `unit_id` 和结构化中文描述。
        # 2. 根据 `unit_id` 查找对应的从机引擎并触发其 `on_packet_log` 回调，将报文推送到 UI 日志面板。
        if not data:
            return data
        try:
            # 解析报文获取目标站地址与人类可读的日志文本
            unit_id, msg = format_modbus_trace_packet(self.comm_type, sending, data)
            slave = self._server_context.get_slave(unit_id)
            direction = "TX" if sending else "RX"
            # 优先分发给目标从机实例的日志回调；若目标从机未挂载则回退到传输服务级回调
            if slave and slave.on_packet_log:
                slave.on_packet_log(msg, direction)
            elif self.on_packet_log:
                self.on_packet_log(msg, direction)
        except Exception as ex:
            logger.debug(f"trace_packet 异常: {ex}")
        # 必须原封不动返回原始 bytes，供 pymodbus 协议栈继续解包或发包
        return data

    # [SLAVE-RECONNECT] 新增健康探测方法：判断底层 listener 是否真正在监听
    def is_healthy(self) -> bool:
        """探测底层网络/串口监听服务是否真正处于健康存活状态（三重探活检查）。"""
        # 返回：
        # bool: 若运行标志为真、后台线程存活、asyncio 事件循环正在运行且 `_server` 实例已就绪，则返回 `True`。
        # 设计说明（为什么不能只看 `self._is_running`）：
        # 在现场复杂网络或串口拔插场景下，后台监听协程可能因底层未捕获的 I/O 异常提前退出，
        # 此时容易出现 `_is_running` 仍为 True 但实际线程或事件循环已死的“半死（僵死）状态”。
        # 通过同时校验 `_is_running`、`_thread.is_alive()`、`_loop.is_running()` 与 `_server`，才能精准识别僵死端口。
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
        """若探测到底层 listener 处于僵死（不健康）状态，则自动执行停止清理并重新启动监听。"""
        # 返回：
        # bool: 若执行了自愈重建则返回 `True`；若原本健康无需重建则返回 `False`。
        # 关键设计（为什么重建后无需重新注册从机）：
        # `self._server_context` 是 `ModbusTransportServer` 的成员变量，`stop()` 只销毁线程、事件循环与 socket，
        # 不会清空 `_server_context._slaves` 中已挂载的从机映射与寄存器数据块。因此 `start()` 新建的服务器
        # 绑定同一个 `_server_context` 后，原先挂载的所有从机立即无缝恢复通信。
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
        """启动底层物理端点监听线程，并使用事件驱动机制等待 `self._server` 就绪。"""
        # 关键步骤（为什么使用 `self._ready.wait(timeout=1.0)` 替代盲等循环）：
        # - 旧实现使用 `for _ in range(30): time.sleep(0.03)`，若后台线程在极短时间（如 2ms）内完成初始化，
        # 主调线程仍可能白白等待至少一个 `0.03s` 粒度；若端口绑定失败立即抛错，更会盲等满 `30 × 0.03s = 0.9s`！
        # - 改为 `threading.Event` 后，一旦 `_serve()` 创建好 `_server_task`（或异常进入 `except`/`finally`）立即调用 `_ready.set()`，
        # 正常启动仅耗时 1~5ms，异常端口占用也能在几毫秒内立即唤醒返回。
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
        # 启动前先复位就绪信号量
        self._ready.clear()
        self._is_running = True
        self._start_ts = time.time()
        self._thread = threading.Thread(target=self._run_server_loop, daemon=True)
        self._thread.start()
        # [SLAVE-SPEED-1] 事件驱动等待后台 listener 就绪，取代 30×0.03s 固定盲等
        self._ready.wait(timeout=1.0)
        if not self._server:
            logger.warning(f"ModbusTransportServer {self.endpoint_key} 1s 内未就绪")

    # [REVIEW-FIX-3] 完善 stop() 资源彻底回收：取消 task 并在事件循环中安全 await shutdown()，避免 socket 与 loop 句柄泄漏
    # [SLAVE-SPEED-2] 将清理与线程 join 超时上限从 2.0s+3.0s 压缩至 1.0s+1.0s
    def stop(self) -> None:
        """优雅停止物理传输层监听服务，安全释放底层 TCP socket 或串口句柄及事件循环。"""
        # 关键步骤（为什么必须通过 `run_coroutine_threadsafe` 跨线程调度 `_cleanup`）：
        # - `self._server` 和 `self._server_task` 绑定在后台子线程的 `self._loop` 事件循环上。
        # - asyncio 的协程对象和 Task 绝不能在外部线程直接调用 `cancel()` 或 `shutdown()`，必须通过
        # `asyncio.run_coroutine_threadsafe(_cleanup(), self._loop)` 投递回子线程事件循环内部安全执行，
        # 才能彻底释放操作系统端口句柄，保证紧接着再次启动同一端口不会报 `Address already in use`。
        self._is_running = False
        if self._loop and self._loop.is_running():
            async def _cleanup():
                """在后台事件循环线程内部执行的异步清理协程：先关闭底层 server 监听，再取消常驻任务。"""
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
                # 跨线程向子线程事件循环投递清理协程，最多等待 1.0 秒
                fut = asyncio.run_coroutine_threadsafe(_cleanup(), self._loop)
                fut.result(timeout=1.0)
            except Exception as e:
                logger.debug(f"stop() 通知 loop 清理超时或异常: {e}")

        # 等待后台监听线程彻底退出
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                logger.warning(f"ModbusTransportServer 后台服务线程 join 超时 ({self.endpoint_key})")

        # 确保全部引用彻底重置为 None，防止残留半清理状态
        self._thread = None
        self._loop = None
        self._server = None
        self._server_task = None
        self._ready.clear()

    def _run_server_loop(self) -> None:
        """后台守护线程主入口：创建独立 asyncio 事件循环并运行 pymodbus 服务端监听协程。"""
        # 关键步骤（为什么在正常分支、`CancelledError`、`Exception` 和 `finally` 四处都调用 `self._ready.set()`）：
        # - 主调线程在 `start()` 中阻塞在 `self._ready.wait(timeout=1.0)`。
        # - 正常情况下，创建 `_server_task` 后立即 `set()` 唤醒主调线程。
        # - 但如果构造 `ModbusTcpServer`/`ModbusSerialServer` 时立刻抛出异常（例如串口不存在或权限不足），
        # 代码会直接跳入 `except` 与 `finally`。在所有退出口兜底调用 `self._ready.set()`，可确保主调线程
        # 在失败场景下也无需干等 1.0 秒超时，实现毫秒级快速失败返回。
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _serve():
            """实例化 pymodbus 服务端并启动无限监听协程任务。"""
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
                # 启动后台监听 Task，并立即置位 _ready 唤醒 start() 调用方
                self._server_task = asyncio.create_task(self._server.serve_forever())
                self._ready.set()
                await self._server_task
            except asyncio.CancelledError:
                self._ready.set()
            except Exception as e:
                self._ready.set()
                logger.error(f"ModbusTransportServer 运行异常 ({self.endpoint_key}): {e}")
                if self.on_packet_log:
                    self.on_packet_log(f"[服务异常崩溃] 无法监听 {self.endpoint_key}: {e}", "ERROR")

        try:
            self._loop.run_until_complete(_serve())
        finally:
            self._ready.set()
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
    """职责：Modbus 传输层物理服务中枢（全局线程安全单例），负责在多从机并发场景下实现物理端口（IP:Port 或 COM 口）的共享复用与生命周期调度。"""
    # 成员变量：
    # - _instance (Optional[ModbusServerHub]): 全局唯一单例引用。
    # - _lock (threading.Lock): 保护单例初始化及 `_servers` 字典并发读写的互斥锁。
    # - _servers (Dict[tuple, ModbusTransportServer]): 归一化端点标识 `endpoint_key` 到传输服务实例的映射表。
    # 设计说明（为什么需要 Hub 单例）：
    # 操作系统不允许两个独立 Socket 重复绑定同一个 TCP 端口（或两个句柄同时打开同一个 COM 串口）。
    # 当用户在工作台中模拟同一 RS485 总线或同一网关 IP:Port 下的多台设备（如站地址 1~10 的多台 BMS/PCS）时，
    # `ModbusServerHub` 将它们自动汇聚到同一个 `ModbusTransportServer` 实例下，并在最后一个从机停止时自动释放物理端口。

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "ModbusServerHub":
        """获取 `ModbusServerHub` 全局唯一单例（采用双重检查锁定 DCL 模式保证线程安全）。"""
        # 返回：
        # ModbusServerHub: 全局唯一的传输层服务中枢实例。
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = ModbusServerHub()
        return cls._instance

    def __init__(self):
        """初始化中枢内部端点映射表 `_servers`。"""
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
        """根据通信参数生成标准化的物理端点唯一键元组（用于 `_servers` 字典索引）。"""
        # 参数：
        # comm_type: `"TCP"` 或 `"RTU"`。
        # host: TCP 监听地址（空字符串或 `"*"` 会自动归一化为 `"0.0.0.0"`）。
        # port: TCP 监听端口号。
        # serial_port: 串口名称（如 `"COM1"`）。
        # baudrate / bytesize / parity / stopbits: 串口通信参数。
        # 返回：
        # tuple: 归一化后的不可变元组 key。
        c_type = str(comm_type).upper().strip()
        if c_type == "RTU":
            return ("RTU", str(serial_port).upper().strip(), int(baudrate), int(bytesize), str(parity).upper().strip(), int(stopbits))
        else:
            h = str(host).strip()
            # 将空主机名或通配符统一归一化为全网卡监听地址 0.0.0.0
            h_norm = "0.0.0.0" if h in ("0.0.0.0", "", "*") else h
            return ("TCP", h_norm, int(port))

    def register_and_start_slave(self, engine: Any) -> Tuple[bool, str]:
        """将从机引擎注册挂载到对应的物理端点传输服务上；若该物理端点尚未启动或已僵死，则自动创建并启动监听。"""
        # 参数：
        # engine: 待启动挂载的 `ModbusSlaveEngine` 实例。
        # 返回：
        # Tuple[bool, str]: `(是否成功, 提示或错误消息)`。若同端点下存在相同 `slave_id` 则拒绝并返回冲突提示。
        # 关键步骤（为什么这么写）：
        # 1. 通配地址兼容匹配：若已有 `"0.0.0.0:502"` 监听服务，后续注册 `"127.0.0.1:502"` 的从机也会自动复用该服务，
        # 因为 `0.0.0.0` 已占用本机所有网卡的 502 端口，再次绑定 `127.0.0.1:502` 会引发端口冲突。
        # 2. 复用前探活（`[SLAVE-RECONNECT-3b]`）：若字典中已存在的 `server` 处于僵死状态（`not server.is_healthy()`），
        # 直接丢弃旧实例并重建新 `ModbusTransportServer`，防止新从机挂载到死端口上。
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
            # 若未精确命中但属于 TCP 模式，检查是否存在同端口且包含 0.0.0.0 通配绑定的可复用服务
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

            # 若尚无可用传输服务，则新建并立即启动监听
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
        """从物理端点注销指定从机；当该物理端点下挂载的从机数量归零时，自动停止并释放底层物理监听端口。"""
        # 参数：
        # engine: 待停止注销的 `ModbusSlaveEngine` 实例。
        # 关键设计（为什么把 `server_to_stop.stop()` 放在 `with self._lock:` 外部执行）：
        # `server.stop()` 涉及跨线程通知事件循环关闭 socket 以及 `thread.join()` 等待，可能耗时数十毫秒至 1 秒。
        # 若在持有 Hub 全局锁 `_lock` 期间调用 `stop()`，会导致其他物理端口的从机注册、探活查询被无辜阻塞。
        # 因此在锁内仅执行字典摘除，释放锁后再执行底层耗时停止操作。
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
                # 当该物理端口上最后一个从机也被卸载时，将其从活跃服务表中移除并标记待停止
                if remaining == 0:
                    self._servers.pop(key, None)
                    server_to_stop = server

        # 在 Hub 全局锁外部执行耗时的底层网络/串口停止与线程 join
        if server_to_stop:
            server_to_stop.stop()

    def stop_all(self) -> None:
        """
        停止并清空所有物理端点的传输监听服务（通常在应用退出或一键清空从机时调用）。
        """
        servers_to_stop = []
        with self._lock:
            servers_to_stop = list(self._servers.values())
            self._servers.clear()
        # 同样在全局锁外部逐个停止底层服务
        for s in servers_to_stop:
            s.stop()

    # [SLAVE-RECONNECT] 按 engine 的端点参数反查该 engine 挂载的 transport server
    def get_server_for_engine(self, engine: Any) -> Optional[ModbusTransportServer]:
        """根据从机引擎的通信配置反查其当前挂载的 `ModbusTransportServer` 实例。"""
        # 参数：
        # engine: 待查询的 `ModbusSlaveEngine` 实例。
        # 返回：
        # Optional[ModbusTransportServer]: 对应的传输层服务实例；若未找到则返回 `None`。
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
        """扫描中枢管理的所有物理端点传输服务，对检测到僵死的 listener 执行自愈重建。"""
        # 返回：
        # int: 本次成功执行自愈重建的物理端点数量。
        restarted = 0
        with self._lock:
            servers = list(self._servers.values())
        for srv in servers:
            if srv.restart_if_broken():
                restarted += 1
        return restarted


# =====================================================================
# 4. 从机模拟逻辑引擎 (ModbusSlaveEngine)
# =====================================================================
class ModbusSlaveEngine:
    """职责：单个 Modbus 从机站点的逻辑模拟引擎（同时支持以太网 TCP 与串行端口 RTU 232/485，支持同端口多站并发挂载）。"""
    # 成员变量：
    # - comm_type / host / port / serial_port / baudrate / bytesize / parity / stopbits / slave_id: 站点通信与协议寻址配置。
    # - address_base (int): 寄存器寻址基准偏移（默认 1，即 1-based 标准地址），彻底消除启发式看值猜偏移。
    # - _coils / _discrete_inputs / _input_registers / _holding_registers: 四大独立 `ModbusSequentialDataBlock`（各预分配 65535 点位）。
    # - points (Dict[Union[int, Tuple[str, int]], Dict[str, Any]]): 参与动态波形仿真的点位配置表，推荐使用 `(area, address)` 复合键。
    # - _sim_thread (Optional[threading.Thread]): 后台 1s 周期波形仿真守护线程。
    # - _sim_stop_evt (threading.Event): 仿真线程可唤醒退出事件，使 `stop()` 能在毫秒级立刻打断 1s 睡眠并退出。
    # - on_packet_log / on_point_value_changed: 报文追踪回调与点位动态变化通知回调。
    # 使用场景：
    # 由业务层 `SlaveService` 为每个 `SlaveDevice` 实例化并维护。启动时通过 `ModbusServerHub` 挂载到共享物理端口，
    # 运行期间由 `_run_simulation_loop` 定时生成正弦波/方波/步进/随机波形并写回底层寄存器数据块。

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
        """初始化单个从机模拟引擎实例及其四大 65535 容量寄存器存储区。"""
        # 参数：
        # comm_type: 通信方式（`"TCP"` 或 `"RTU"`）。
        # host / port: TCP 监听地址与端口。
        # serial_port / baudrate / bytesize / parity / stopbits: RTU 串口五要素参数。
        # slave_id: 从机站地址（Unit ID，默认 1）。
        # address_base: 协议地址起始基准（默认 1）。
        self.comm_type = comm_type.upper()  # "TCP" 或 "RTU"
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baudrate = baudrate
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.bytesize = bytesize
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.parity = parity
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.stopbits = stopbits
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.slave_id = slave_id
        # [REVIEW-FIX-1] 显式定义地址偏移基准 (默认 1-based)，消除启发式看值推断
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.address_base = address_base

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._server = None
        self._server_task: Optional[asyncio.Task] = None
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._is_running = False

        # 初始化 4 大存储区，每个存储区预分配 65535 个点位 (标准 Modbus 地址 1~65535)
        # 为什么一次性预分配 65535：现场 EMS 点表常出现跨度极大的稀疏地址（如 1~100 和 40001~40500），
        # 全量预分配连续内存数组可支持任意地址 O(1) 下标直达读写，且在服务未启动前即可预热写入初值
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._coils = ModbusSequentialDataBlock(1, [False] * 65535)
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._discrete_inputs = ModbusSequentialDataBlock(1, [False] * 65535)
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._input_registers = ModbusSequentialDataBlock(1, [0] * 65535)
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._holding_registers = ModbusSequentialDataBlock(1, [0] * 65535)

        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._device_context = ModbusDeviceContext(
            di=self._discrete_inputs,
            co=self._coils,
            hr=self._holding_registers,
            ir=self._input_registers,
        )
        # 保留单机上下文兼容已有属性
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._server_context = ModbusServerContext(devices=self._device_context, single=True)

        # 模拟点位配置表: address -> {type, mode, sim_rule, ...}
        self.points: Dict[Union[int, Tuple[str, int]], Dict[str, Any]] = {}
        self._sim_thread: Optional[threading.Thread] = None
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._sim_running = False
        # [SLAVE-SPEED-3] 仿真线程可唤醒退出事件：取代固定 1s 睡眠盲等
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._sim_stop_evt = threading.Event()
        # 外部通信问询与响应报文回调: (message, level) -> None
        self.on_packet_log: Optional[Callable[[str, str], None]] = None
        # 模拟点位数值实时变动通知回调: (area, address, new_val) -> None
        self.on_point_value_changed: Optional[Callable[[str, int, Any], None]] = None

    @property
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [is_running]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def is_running(self) -> bool:
        """返回当前从机模拟实例是否处于运行状态。"""
        # 从 [is_running] 返回执行结果给调用方，结束当前处理分支
        return self._is_running

    # [SLAVE-RECONNECT] 新增健康探测：该从机对应的底层 transport 是否仍正常监听
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [is_healthy]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def is_healthy(self) -> bool:
        """探测当前从机所挂载的底层物理传输服务（`ModbusTransportServer`）是否健康存活。"""
        # 返回：
        # bool: 若从机正在运行且其所属的底层 `ModbusTransportServer.is_healthy()` 为真，则返回 `True`。
        # 在 [is_healthy] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self._is_running:
            # 从 [is_healthy] 返回执行结果给调用方，结束当前处理分支
            return False
        # 找到本从机所在的 ModbusTransportServer（通过 endpoint_key 反查 Hub）
        # 在 [is_healthy] 中更新局部变量 [hub]：为后续协议编码、服务调度或界面渲染准备中间状态
        hub = ModbusServerHub.get_instance()
        # 在 [is_healthy] 中更新局部变量 [server]：为后续协议编码、服务调度或界面渲染准备中间状态
        server = hub.get_server_for_engine(self)
        # 从 [is_healthy] 返回执行结果给调用方，结束当前处理分支
        return server.is_healthy() if server else False

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [start]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def start(self) -> None:
        """启动从机服务：通过 `ModbusServerHub` 将本实例挂载到共享物理端口，并启动后台动态波形仿真线程。"""
        # 异常：
        # RuntimeError: 当同物理端点下已存在相同站地址（`slave_id` 冲突）时抛出。
        # 在 [start] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._is_running:
            # 从 [start] 返回执行结果给调用方，结束当前处理分支
            return

        # 1. 向全局物理端点中枢注册并启动底层监听
        # 在 [start] 中更新局部变量 [hub]：为后续协议编码、服务调度或界面渲染准备中间状态
        hub = ModbusServerHub.get_instance()
        ok, err = hub.register_and_start_slave(self)
        # 在 [start] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not ok:
            raise RuntimeError(err)

        # 在 [start] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._is_running = True

        # 2. 启动模拟数据定时刷新线程（启动前先清除停止信号量 _sim_stop_evt）
        self._sim_stop_evt.clear()
        # 在 [start] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._sim_running = True
        # 在 [start] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._sim_thread = threading.Thread(target=self._run_simulation_loop, daemon=True)
        self._sim_thread.start()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [stop]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def stop(self) -> None:
        """停止从机服务：立即唤醒并终止波形仿真线程，随后从物理传输中枢安全卸载（若为该端口最后一台从机则关闭物理端口）。"""
        # 关键步骤（为什么先 `self._sim_stop_evt.set()` 再注销 Hub）：
        # - 旧实现中仿真线程使用 `time.sleep(1)`，每次调用 `stop()` 平均要卡顿 0.5~1.0 秒等待 sleep 结束。
        # - 改用 `_sim_stop_evt.set()` 后，正在 `_sim_stop_evt.wait(1)` 中等待的仿真线程会在微秒级被立即唤醒并退出循环，
        # 从而让单机停止与“全部停止”批量操作耗时从秒级降至毫秒级。
        # 在 [stop] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._sim_running = False
        # 立即触发停止事件，唤醒正在 wait(1) 的后台波形仿真线程
        self._sim_stop_evt.set()
        # 在 [stop] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._is_running = False

        # 从全局物理中枢卸载本从机实例
        # 在 [stop] 中更新局部变量 [hub]：为后续协议编码、服务调度或界面渲染准备中间状态
        hub = ModbusServerHub.get_instance()
        hub.unregister_and_stop_slave(self)

        # 回收仿真线程句柄
        # 在 [stop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._sim_thread:
            self._sim_stop_evt.set()
            self._sim_thread.join(timeout=0.3)
            # 在 [stop] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._sim_thread = None

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_trace_packet]（入参: sending, data）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _on_trace_packet(self, sending: bool, data: bytes) -> bytes:
        """单机模式下的兼容性报文拦截回调：解析收发字节帧并转发给 `self.on_packet_log`。"""
        # 参数：
        # sending: `True` 为发送响应，`False` 为接收问询。
        # data: 原始报文字节流。
        # 返回：
        # bytes: 原样返回 `data` 供协议栈继续处理。
        unit_id, msg = format_modbus_trace_packet(self.comm_type, sending, data)
        # 在 [_on_trace_packet] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.on_packet_log:
            self.on_packet_log(msg, "TX" if sending else "RX")
        # 从 [_on_trace_packet] 返回执行结果给调用方，结束当前处理分支
        return data

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_get_function_code]（入参: area）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _get_function_code(self, area: str) -> int:
        """根据存储区域描述字符串推断对应的标准 Modbus 读取功能码（FC01~FC04）。"""
        # 参数：
        # area: 存储区域标识字符串（支持 `"0x"`/`"COIL"`/`"线圈"` 等中英文前缀）。
        # 返回：
        # int: 1（读线圈）、2（读离散输入）、3（读保持寄存器）或 4（读输入寄存器）。
        # 在 [_get_function_code] 中更新局部变量 [area_s]：为后续协议编码、服务调度或界面渲染准备中间状态
        area_s = str(area).upper()
        # 在 [_get_function_code] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if "0X" in area_s or "COIL" in area_s or "线圈" in str(area):
            # 从 [_get_function_code] 返回执行结果给调用方，结束当前处理分支
            return 1
        # 在 [_get_function_code] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        elif "1X" in area_s or "DISCRETE" in area_s or "离散" in str(area):
            # 从 [_get_function_code] 返回执行结果给调用方，结束当前处理分支
            return 2
        # 在 [_get_function_code] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        elif "3X" in area_s or "INPUT" in area_s or "输入" in str(area):
            # 从 [_get_function_code] 返回执行结果给调用方，结束当前处理分支
            return 4
        # 从 [_get_function_code] 返回执行结果给调用方，结束当前处理分支
        return 3  # HOLDING_REGISTER

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_get_datablock]（入参: area）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _get_datablock(self, area: str):
        """根据存储区域标识获取本从机对应的底层 `ModbusSequentialDataBlock` 实例。"""
        # 参数：
        # area: 存储区域标识字符串。
        # 返回：
        # ModbusSequentialDataBlock: 对应的线圈、离散输入、输入寄存器或保持寄存器数据块。
        # 在 [_get_datablock] 中更新局部变量 [area_s]：为后续协议编码、服务调度或界面渲染准备中间状态
        area_s = str(area).upper()
        # 在 [_get_datablock] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if "0X" in area_s or "COIL" in area_s or "线圈" in str(area):
            # 从 [_get_datablock] 返回执行结果给调用方，结束当前处理分支
            return self._coils
        # 在 [_get_datablock] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        elif "1X" in area_s or "DISCRETE" in area_s or "离散" in str(area):
            # 从 [_get_datablock] 返回执行结果给调用方，结束当前处理分支
            return self._discrete_inputs
        # 在 [_get_datablock] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        elif "3X" in area_s or "INPUT" in area_s or "输入" in str(area):
            # 从 [_get_datablock] 返回执行结果给调用方，结束当前处理分支
            return self._input_registers
        # 从 [_get_datablock] 返回执行结果给调用方，结束当前处理分支
        return self._holding_registers

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [read_raw_values]（入参: area, address, count）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def read_raw_values(self, area: str, address: int, count: int) -> List[int]:
        """从本地数据块直接读取连续的原始 16 位寄存器整数值或布尔位（0/1），支持在服务启动前后随时调用。"""
        # 参数：
        # area: 存储区域标识。
        # address: 起始点位地址。
        # count: 读取的连续寄存器/线圈数量。
        # 返回：
        # List[int]: 长度为 `count` 的整数列表（布尔位 True/False 已统一转为 1/0；越界位置补 0）。
        # 在 [read_raw_values] 中更新局部变量 [block]：为后续协议编码、服务调度或界面渲染准备中间状态
        block = self._get_datablock(area)
        # 在 [read_raw_values] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if block and hasattr(block, "simdata") and block.simdata:
            # 在 [read_raw_values] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                # 计算相对于数据块起始地址的数组下标
                # 在 [read_raw_values] 中更新局部变量 [base_addr]：为后续协议编码、服务调度或界面渲染准备中间状态
                base_addr = block.simdata[0].address
                # 在 [read_raw_values] 中更新局部变量 [start_idx]：为后续协议编码、服务调度或界面渲染准备中间状态
                start_idx = address - 1 - base_addr
                # 在 [read_raw_values] 中更新局部变量 [values_list]：为后续协议编码、服务调度或界面渲染准备中间状态
                values_list = block.simdata[0].values
                # 在 [read_raw_values] 中更新局部变量 [res]：为后续协议编码、服务调度或界面渲染准备中间状态
                res = []
                # 在 [read_raw_values] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
                for i in range(count):
                    # 在 [read_raw_values] 中更新局部变量 [idx]：为后续协议编码、服务调度或界面渲染准备中间状态
                    idx = start_idx + i
                    # 在 [read_raw_values] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if 0 <= idx < len(values_list):
                        # 在 [read_raw_values] 中更新局部变量 [v]：为后续协议编码、服务调度或界面渲染准备中间状态
                        v = values_list[idx]
                        res.append(1 if v is True else 0 if v is False else int(v))
                    else:
                        res.append(0)
                # 从 [read_raw_values] 返回执行结果给调用方，结束当前处理分支
                return res
            except Exception as e:
                logger.error(f"read_raw_values 异常: {e}")
        # 从 [read_raw_values] 返回执行结果给调用方，结束当前处理分支
        return [0] * count

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [write_raw_values]（入参: area, address, values）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def write_raw_values(self, area: str, address: int, values: List[int]) -> None:
        """向本地数据块直接写入连续的原始 16 位寄存器值或布尔位（支持服务未启动时的点表初值预热写入）。"""
        # 参数：
        # area: 存储区域标识。
        # address: 起始点位地址。
        # values: 待写入的原始整数列表（线圈/离散输入区会自动转为 bool，寄存器区会按 `& 0xFFFF` 截断为 16 位无符号字）。
        # 在 [write_raw_values] 中更新局部变量 [block]：为后续协议编码、服务调度或界面渲染准备中间状态
        block = self._get_datablock(area)
        # 在 [write_raw_values] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if block and hasattr(block, "simdata") and block.simdata:
            # 在 [write_raw_values] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                # 在 [write_raw_values] 中更新局部变量 [base_addr]：为后续协议编码、服务调度或界面渲染准备中间状态
                base_addr = block.simdata[0].address
                # 在 [write_raw_values] 中更新局部变量 [start_idx]：为后续协议编码、服务调度或界面渲染准备中间状态
                start_idx = address - 1 - base_addr
                # 在 [write_raw_values] 中更新局部变量 [values_list]：为后续协议编码、服务调度或界面渲染准备中间状态
                values_list = block.simdata[0].values
                # 在 [write_raw_values] 中更新局部变量 [is_bit]：为后续协议编码、服务调度或界面渲染准备中间状态
                is_bit = (AreaType.COIL in area or AreaType.DISCRETE_INPUT in area)
                # 在 [write_raw_values] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
                for i, val in enumerate(values):
                    # 在 [write_raw_values] 中更新局部变量 [idx]：为后续协议编码、服务调度或界面渲染准备中间状态
                    idx = start_idx + i
                    # 在 [write_raw_values] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if 0 <= idx < len(values_list):
                        values_list[idx] = bool(val) if is_bit else (int(val) & 0xFFFF)
            except Exception as ex:
                logger.warning(f"写入数据块异常 ({area}:{address}): {ex}")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [write_typed_value]（入参: area, address, value, data_type）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def write_typed_value(
        self,
        area: str,
        address: int,
        value: Any,
        data_type: Any,
        mode: Any = ByteOrderMode.ABCD,
        byte_order: Any = None,
    ) -> None:
        """将业务工程数值按指定数据类型（如 `FLOAT32`、`INT32`）和字节序（如 `ABCD`、`CDAB`）编码后写入本地数据块。"""
        # 参数：
        # area: 存储区域标识。
        # address: 起始寄存器或线圈地址。
        # value: 待写入的工程值（支持数值、布尔或字符串）。
        # data_type: 数据类型枚举或字符串。
        # mode: 默认字序枚举或字符串。
        # byte_order: 可选别名参数（若传入则优先于 `mode`，兼容不同调用方命名习惯）。
        # 在 [write_typed_value] 中更新局部变量 [actual_mode]：为后续协议编码、服务调度或界面渲染准备中间状态
        actual_mode = byte_order if byte_order is not None else mode
        # 若目标区域为 0x 线圈或 1x 离散输入，按布尔量解析并写入单比特
        # 在 [write_typed_value] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if AreaType.COIL in area or AreaType.DISCRETE_INPUT in area or "0X" in str(area).upper() or "1X" in str(area).upper():
            # 在 [write_typed_value] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                # 在 [write_typed_value] 中更新局部变量 [b_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                b_val = bool(int(float(str(value).strip())))
            except Exception:
                # 在 [write_typed_value] 中更新局部变量 [b_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                b_val = str(value).strip().lower() in ("1", "true", "yes", "on")
            self.write_raw_values(area, address, [1 if b_val else 0])
        else:
            # 若目标区域为 3x/4x 寄存器，调用 modbus_codec.encode_value 编码为 16 位寄存器列表后落盘
            # 在 [write_typed_value] 中更新局部变量 [regs]：为后续协议编码、服务调度或界面渲染准备中间状态
            regs = encode_value(value, data_type, actual_mode)
            self.write_raw_values(area, address, regs)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [read_typed_value]（入参: area, address, data_type, mode）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def read_typed_value(
        self,
        area: str,
        address: int,
        data_type: ModbusDataType,
        mode: ByteOrderMode = ByteOrderMode.ABCD,
    ) -> Any:
        """从本地数据块读取指定地址的连续寄存器，并按数据类型与字节序解码还原为工程数值。"""
        # 参数：
        # area: 存储区域标识。
        # address: 起始点位地址。
        # data_type: 目标 `ModbusDataType` 枚举。
        # mode: 字节序 `ByteOrderMode` 枚举。
        # 返回：
        # Any: 解码后的 Python 强类型值（`bool` / `int` / `float` / `str`）。
        # 在 [read_typed_value] 中更新局部变量 [count]：为后续协议编码、服务调度或界面渲染准备中间状态
        count = TYPE_REGISTER_COUNT.get(data_type, 1)
        # 在 [read_typed_value] 中更新局部变量 [raw]：为后续协议编码、服务调度或界面渲染准备中间状态
        raw = self.read_raw_values(area, address, count)
        # 在 [read_typed_value] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if AreaType.COIL in area or AreaType.DISCRETE_INPUT in area:
            # 从 [read_typed_value] 返回执行结果给调用方，结束当前处理分支
            return bool(raw[0])
        # 从 [read_typed_value] 返回执行结果给调用方，结束当前处理分支
        return decode_value(raw, data_type, mode)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_run_simulation_loop]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _run_simulation_loop(self) -> None:
        """后台动态波形模拟循环（1s 周期）：遍历 `self.points` 中非“固定”规则的点位，计算下一拍波形值并同步写入底层寄存器。"""
        # 关键步骤（为什么这么写）：
        # 1. **可唤醒等待（`self._sim_stop_evt.wait(1)`）**：替代阻塞式 `time.sleep(1)`。正常运行时每 1 秒推进一拍 `step`；
        # 一旦调用 `stop()` 置位 `_sim_stop_evt`，线程立即跳出循环结束，绝不拖慢从机停止速度。
        # 2. **字典快照（`list(self.points.items())`）**：UI 线程可能在运行期间动态增删点位或批量修改模拟规则，
        # 先拷贝浅快照再遍历可彻底避免 `RuntimeError: dictionary changed size during iteration`。
        # 3. **复合键去重（`has_composite`）**：支持 `(area, addr)` 复合键以区分不同存储区的同地址点位；
        # 当存在复合键时自动跳过历史兼容留存的纯整型 `addr` 键，防止同一寄存器在一拍内被重复步进两次。
        # 4. **地址相位偏移（`addr % 100`）**：正弦波与方波在计算相位时引入点位地址偏移，使点表中不同地址的曲线呈现错落有致的自然波浪，而非所有点位机械地同相涨跌。
        # 在 [_run_simulation_loop] 中更新局部变量 [step]：为后续协议编码、服务调度或界面渲染准备中间状态
        step = 0
        import math
        import random

        # 在 [_run_simulation_loop] 中维持受控循环：配合运行标志位或事件信号量实现可持续中断的周期性处理
        while self._sim_running and not self._sim_stop_evt.is_set():
            # 等待 1 秒或直到收到停止信号；若返回 True 表示被 stop() 唤醒，立即终止循环
            # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self._sim_stop_evt.wait(1):
                break
            # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not self._sim_running or self._sim_stop_evt.is_set():
                break
            # 在 [_run_simulation_loop] 中更新局部变量 [step]：为后续协议编码、服务调度或界面渲染准备中间状态
            step = (step + 1) % 100000

            # 制作字典项安全快照，防止多线程迭代期间大小并发变动
            # 在 [_run_simulation_loop] 中更新局部变量 [items_snapshot]：为后续协议编码、服务调度或界面渲染准备中间状态
            items_snapshot = list(self.points.items())
            # 在 [_run_simulation_loop] 中更新局部变量 [has_composite]：为后续协议编码、服务调度或界面渲染准备中间状态
            has_composite = any(isinstance(k, tuple) for k, _ in items_snapshot)

            # 在 [_run_simulation_loop] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for key, point in items_snapshot:
                # 若存在复合键 (area, addr)，忽略历史单一整型 key 以避免重复模拟
                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if has_composite and not isinstance(key, tuple):
                    continue

                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if isinstance(key, tuple) and len(key) >= 2:
                    k_area, addr = str(key[0]), int(key[1])
                else:
                    # 在 [_run_simulation_loop] 中更新局部变量 [addr]：为后续协议编码、服务调度或界面渲染准备中间状态
                    addr = int(key)
                    # 在 [_run_simulation_loop] 中更新局部变量 [k_area]：为后续协议编码、服务调度或界面渲染准备中间状态
                    k_area = point.get("area", AreaType.HOLDING_REGISTER)

                # 在 [_run_simulation_loop] 中更新局部变量 [sim_mode]：为后续协议编码、服务调度或界面渲染准备中间状态
                sim_mode = point.get("sim_mode") or point.get("sim_rule") or "固定"
                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if sim_mode == "固定":
                    continue

                # 在 [_run_simulation_loop] 中更新局部变量 [dt_raw]：为后续协议编码、服务调度或界面渲染准备中间状态
                dt_raw = point.get("data_type") or point.get("type") or ModbusDataType.INT16
                # 在 [_run_simulation_loop] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                try:
                    # 在 [_run_simulation_loop] 中更新局部变量 [data_type]：为后续协议编码、服务调度或界面渲染准备中间状态
                    data_type = dt_raw if isinstance(dt_raw, ModbusDataType) else ModbusDataType(str(dt_raw))
                except Exception:
                    # 在 [_run_simulation_loop] 中更新局部变量 [data_type]：为后续协议编码、服务调度或界面渲染准备中间状态
                    data_type = ModbusDataType.INT16

                # 在 [_run_simulation_loop] 中更新局部变量 [bo_raw]：为后续协议编码、服务调度或界面渲染准备中间状态
                bo_raw = point.get("byte_order") or point.get("mode") or ByteOrderMode.ABCD
                # 在 [_run_simulation_loop] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                try:
                    # 在 [_run_simulation_loop] 中更新局部变量 [mode]：为后续协议编码、服务调度或界面渲染准备中间状态
                    mode = bo_raw if isinstance(bo_raw, ByteOrderMode) else ByteOrderMode(str(bo_raw))
                except Exception:
                    # 在 [_run_simulation_loop] 中更新局部变量 [mode]：为后续协议编码、服务调度或界面渲染准备中间状态
                    mode = ByteOrderMode.ABCD

                # 在 [_run_simulation_loop] 中更新局部变量 [area]：为后续协议编码、服务调度或界面渲染准备中间状态
                area = point.get("area", AreaType.HOLDING_REGISTER)
                # 在 [_run_simulation_loop] 中更新局部变量 [curr]：为后续协议编码、服务调度或界面渲染准备中间状态
                curr = point.get("current_val", point.get("value", 0))

                # 功能码 01(线圈) 与 02(离散输入)：只要有模拟规则，数据就在 0 和 1 之间翻转变动
                # 在 [_run_simulation_loop] 中更新局部变量 [is_fc01_02]：为后续协议编码、服务调度或界面渲染准备中间状态
                is_fc01_02 = (
                    "0X" in str(area).upper()
                    or "1X" in str(area).upper()
                    or "COIL" in str(area).upper()
                    or "DISCRETE" in str(area).upper()
                    or point.get("fc") in (1, 2)
                    or data_type == ModbusDataType.BOOL
                )
                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if is_fc01_02:
                    # 在 [_run_simulation_loop] 中更新局部变量 [curr_bool_int]：为后续协议编码、服务调度或界面渲染准备中间状态
                    curr_bool_int = 1 if (curr is True or str(curr) in ("1", "True", "true")) else 0
                    # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if sim_mode == "正弦波":
                        # 正弦波对于布尔量：按时钟相位正负半周切换高低电平 (0/1)
                        # 在 [_run_simulation_loop] 中更新局部变量 [phase]：为后续协议编码、服务调度或界面渲染准备中间状态
                        phase = (step * 0.35) + ((addr % 10) * 0.4)
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = 1 if math.sin(phase) >= 0 else 0
                    # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    elif sim_mode == "方波":
                        # 在 [_run_simulation_loop] 中更新局部变量 [phase]：为后续协议编码、服务调度或界面渲染准备中间状态
                        phase = (step + (addr % 5)) % 6
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = 1 if phase >= 3 else 0
                    # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    elif sim_mode == "随机波动":
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = random.choice([0, 1])
                    else:
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = 0 if curr_bool_int == 1 else 1

                    point["current_val"] = new_val
                    self.write_typed_value(area, addr, new_val, data_type, mode)
                    # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if self.on_point_value_changed:
                        # 在 [_run_simulation_loop] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                        try:
                            self.on_point_value_changed(area, addr, new_val)
                        except Exception:
                            pass
                    continue

                # 功能码 03(保持寄存器) 与 04(输入寄存器)：受具体模拟规则算法与数据类型限制
                # 1. 确保当前值在数据类型的合法范围内
                # 在 [_run_simulation_loop] 中更新局部变量 [curr]：为后续协议编码、服务调度或界面渲染准备中间状态
                curr = clamp_value_to_type(curr, data_type)

                # 2. 字符/十六进制/二进制不自动波动
                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if data_type in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
                    continue

                # 3. 数值类型强类型模拟 (严格遵守上下限，无符号数绝不产生负数)
                # 在 [_run_simulation_loop] 中更新局部变量 [limits]：为后续协议编码、服务调度或界面渲染准备中间状态
                limits = TYPE_RANGES.get(data_type, (-32768, 32767, int))
                min_v, max_v, v_type = limits

                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if sim_mode in ("累加递增", "步进递增"):
                    # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if v_type is int:
                        # 在 [_run_simulation_loop] 中更新局部变量 [wrap_limit]：为后续协议编码、服务调度或界面渲染准备中间状态
                        wrap_limit = min(max_v, 10000)
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = (curr + 1) if curr < wrap_limit else min_v
                    else:
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = round((curr + 0.5) % 1000.0, 2)

                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                elif sim_mode == "随机波动":
                    # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if v_type is int:
                        # 在 [_run_simulation_loop] 中更新局部变量 [delta]：为后续协议编码、服务调度或界面渲染准备中间状态
                        delta = random.choice([-2, -1, 0, 1, 2])
                        # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                        if min_v == 0 and curr <= 1:
                            # 在 [_run_simulation_loop] 中更新局部变量 [delta]：为后续协议编码、服务调度或界面渲染准备中间状态
                            delta = random.choice([0, 1, 2, 3])
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = int(max(min_v, min(max_v, curr + delta)))
                    else:
                        # 在 [_run_simulation_loop] 中更新局部变量 [delta]：为后续协议编码、服务调度或界面渲染准备中间状态
                        delta = random.uniform(-0.5, 0.5)
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = round(max(min_v, min(max_v, curr + delta)), 2)

                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                elif sim_mode == "正弦波":
                    # 正弦波算法：结合时钟 step 与点位地址 addr 相位偏移，生成丝滑不同步的正弦波浪
                    # 在 [_run_simulation_loop] 中更新局部变量 [phase]：为后续协议编码、服务调度或界面渲染准备中间状态
                    phase = (step * 0.25) + ((addr % 100) * 0.15)
                    # 在 [_run_simulation_loop] 中更新局部变量 [sin_factor]：为后续协议编码、服务调度或界面渲染准备中间状态
                    sin_factor = math.sin(phase)
                    # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if min_v == 0:  # 无符号整型 (如 UINT16: 0~65535)
                        # 在 [_run_simulation_loop] 中更新局部变量 [base]：为后续协议编码、服务调度或界面渲染准备中间状态
                        base = 50.0 if (curr == 0) else max(25.0, float(curr))
                        # 在 [_run_simulation_loop] 中更新局部变量 [amp]：为后续协议编码、服务调度或界面渲染准备中间状态
                        amp = min(25.0, base * 0.5)
                        # 在 [_run_simulation_loop] 中更新局部变量 [calc_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        calc_val = base + amp * sin_factor
                    else:  # 有符号整型/浮点数 (如 INT16: -32768~32767)
                        # 在 [_run_simulation_loop] 中更新局部变量 [base]：为后续协议编码、服务调度或界面渲染准备中间状态
                        base = 0.0 if (curr == 0) else float(curr)
                        # 在 [_run_simulation_loop] 中更新局部变量 [calc_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        calc_val = base + 30.0 * sin_factor

                    # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if v_type is int:
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = int(max(min_v, min(max_v, round(calc_val))))
                    else:
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = round(max(min_v, min(max_v, calc_val)), 2)

                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                elif sim_mode == "方波":
                    # 在 [_run_simulation_loop] 中更新局部变量 [phase]：为后续协议编码、服务调度或界面渲染准备中间状态
                    phase = (step + (addr % 10)) % 10
                    # 在 [_run_simulation_loop] 中更新局部变量 [high_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                    high_val = 100 if min_v == 0 else 50
                    # 在 [_run_simulation_loop] 中更新局部变量 [low_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                    low_val = 0 if min_v == 0 else -50
                    # 在 [_run_simulation_loop] 中更新局部变量 [val_out]：为后续协议编码、服务调度或界面渲染准备中间状态
                    val_out = high_val if phase >= 5 else low_val
                    # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if v_type is int:
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = int(max(min_v, min(max_v, val_out)))
                    else:
                        # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                        new_val = float(max(min_v, min(max_v, val_out)))

                else:
                    # 在 [_run_simulation_loop] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
                    new_val = curr

                # 保存并写入底层 Context
                point["current_val"] = new_val
                self.write_typed_value(area, addr, new_val, data_type, mode)

                # 回调通知外部模型同步
                # 在 [_run_simulation_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if self.on_point_value_changed:
                    # 在 [_run_simulation_loop] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                    try:
                        self.on_point_value_changed(area, addr, new_val)
                    except Exception:
                        pass


# =====================================================================
# 5. Poll 主机轮询客户端引擎 (ModbusPollEngine)
# =====================================================================
class ModbusPollEngine:
    """职责：Modbus 主机轮询与远程写入测试客户端引擎（同时支持以太网 TCP 与串行端口 RTU）。"""
    # 成员变量：
    # - comm_type / host / port / serial_port / baudrate / bytesize / parity / stopbits / slave_id: 目标从机连接与寻址参数。
    # - client (Optional[Union[ModbusTcpClient, ModbusSerialClient]]): pymodbus 同步客户端实例。
    # - _poll_thread (Optional[threading.Thread]): 后台周期性轮询工作线程。
    # - _poll_stop_event (threading.Event): 轮询线程停止信号量，避免 `stop_polling` 后重连产生双线程并发抢占 socket。
    # - tx_count / rx_count / err_count / last_rtt_ms: 实时通信统计指标（发送帧数、成功接收数、异常数、往返时延毫秒）。
    # - cached_registers (Dict[int, int]): 最近读取到的寄存器原始值缓存表。
    # - on_poll_success / on_poll_error / on_packet_log: 轮询结果与报文日志回调。
    # 使用场景：
    # 由 `PollService` 持有并调度，用于在工作台右侧“主机轮询测试”面板中主动连接外部一体机或本机模拟从机，
    # 执行单次读取、连续定时轮询以及 FC05/FC06/FC16 远程遥控遥调写值验证。

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [__init__]（入参: host, port, slave_id）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def __init__(self, host: str = "127.0.0.1", port: int = 5020, slave_id: int = 1):
        """初始化主机轮询客户端引擎。"""
        # 参数：
        # host: 默认目标 TCP 主机 IP（默认 `"127.0.0.1"`）。
        # port: 默认目标 TCP 端口（默认 `5020`）。
        # slave_id: 默认目标从机站地址（默认 `1`）。
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.comm_type = "TCP"  # "TCP" 或 "RTU"
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.host = host
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.port = port
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.serial_port = "COM1"
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.baudrate = 9600
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.bytesize = 8
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.parity = "N"
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.stopbits = 1
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.slave_id = slave_id

        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.client = None
        self._poll_thread: Optional[threading.Thread] = None
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._is_polling = False
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._poll_interval = 1.0  # 秒
        # [POLL-CONFIG] 引入退出事件通知机制，避免 stop_polling 后产生双线程轮询
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._poll_stop_event = threading.Event()

        # 统计指标
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.tx_count = 0
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.rx_count = 0
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.err_count = 0
        # 在 [__init__] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.last_rtt_ms = 0.0

        # 当前读取到的原始寄存器缓存 (address -> raw_value)
        self.cached_registers: Dict[int, int] = {}
        # 轮询回调函数
        self.on_poll_success: Optional[Callable[[Dict[int, int]], None]] = None
        self.on_poll_error: Optional[Callable[[str], None]] = None
        # 问询与响应报文日志回调: (message, level) -> None
        self.on_packet_log: Optional[Callable[[str, str], None]] = None

    @property
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [is_connected]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def is_connected(self) -> bool:
        """返回底层 pymodbus 客户端是否已建立有效物理连接。"""
        # 从 [is_connected] 返回执行结果给调用方，结束当前处理分支
        return self.client is not None and self.client.connected

    @property
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [is_polling]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def is_polling(self) -> bool:
        """返回后台连续定时轮询线程是否正在运行。"""
        # 从 [is_polling] 返回执行结果给调用方，结束当前处理分支
        return self._is_polling

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [connect_tcp]（入参: host, port）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def connect_tcp(self, host: str = "", port: int = 0) -> bool:
        """建立以太网 Modbus TCP 客户端连接（超时时间设为 2.0 秒）。"""
        # 参数：
        # host: 可选覆盖目标 IP 地址。
        # port: 可选覆盖目标端口号。
        # 返回：
        # bool: 连接成功返回 `True`，否则返回 `False`。
        # 在 [connect_tcp] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if host:
            # 在 [connect_tcp] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self.host = host
        # 在 [connect_tcp] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if port:
            # 在 [connect_tcp] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self.port = port
        # 在 [connect_tcp] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.comm_type = "TCP"
        # 在 [connect_tcp] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.is_connected:
            # 从 [connect_tcp] 返回执行结果给调用方，结束当前处理分支
            return True
        # 在 [connect_tcp] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.client = ModbusTcpClient(self.host, port=self.port, timeout=2.0)
        # 从 [connect_tcp] 返回执行结果给调用方，结束当前处理分支
        return self.client.connect()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [connect_rtu]（入参: serial_port, baudrate, bytesize, parity）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def connect_rtu(
        self,
        serial_port: str = "",
        baudrate: int = 0,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
    ) -> bool:
        """建立串行端口 Modbus RTU 客户端连接（超时时间设为 2.0 秒）。"""
        # 参数：
        # serial_port: 串口名（如 `"COM1"`）。
        # baudrate / bytesize / parity / stopbits: 串口通信五要素参数。
        # 返回：
        # bool: 串口打开成功返回 `True`，否则返回 `False`。
        # 在 [connect_rtu] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if serial_port:
            # 在 [connect_rtu] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self.serial_port = serial_port
        # 在 [connect_rtu] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if baudrate:
            # 在 [connect_rtu] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self.baudrate = baudrate
        # 在 [connect_rtu] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.bytesize = bytesize
        # 在 [connect_rtu] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.parity = parity
        # 在 [connect_rtu] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.stopbits = stopbits
        # 在 [connect_rtu] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.comm_type = "RTU"
        # 在 [connect_rtu] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.is_connected:
            # 从 [connect_rtu] 返回执行结果给调用方，结束当前处理分支
            return True
        # 在 [connect_rtu] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.client = ModbusSerialClient(
            port=self.serial_port,
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=self.parity,
            stopbits=self.stopbits,
            timeout=2.0,
        )
        # 从 [connect_rtu] 返回执行结果给调用方，结束当前处理分支
        return self.client.connect()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [connect]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def connect(self) -> bool:
        """根据当前 `self.comm_type`（`"TCP"` 或 `"RTU"`）自动分派建立底层连接。"""
        # 返回：
        # bool: 连接是否成功建立。
        # 在 [connect] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.comm_type == "RTU":
            # 从 [connect] 返回执行结果给调用方，结束当前处理分支
            return self.connect_rtu()
        # 从 [connect] 返回执行结果给调用方，结束当前处理分支
        return self.connect_tcp()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [disconnect]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def disconnect(self) -> None:
        """
        先安全停止后台连续轮询线程，再关闭底层 TCP socket 或串口连接句柄。
        """
        self.stop_polling()
        # 在 [disconnect] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.client:
            self.client.close()
            # 在 [disconnect] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self.client = None

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [read_block]（入参: area, start_addr, count）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def read_block(self, area: str, start_addr: int, count: int) -> Tuple[bool, List[int], str]:
        """向目标从机发起单次连续块读取请求（支持 TCP 与 RTU，自动测算往返耗时 RTT 并记录收发报文日志）。"""
        # 参数：
        # area: 目标存储区域标识字符串（0x/1x/3x/4x）。
        # start_addr: 起始读取地址。
        # count: 连续读取的点位数量。
        # 返回：
        # Tuple[bool, List[int], str]: `(是否成功, 读取到的原始整数列表, 错误描述字符串)`。
        # 1. 若尚未连接，尝试自动重连；连接失败则记录错误日志并返回
        # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self.is_connected:
            # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not self.connect():
                # 在 [read_block] 中更新局部变量 [target_desc]：为后续协议编码、服务调度或界面渲染准备中间状态
                target_desc = f"串口 {self.serial_port}" if self.comm_type == "RTU" else f"{self.host}:{self.port}"
                # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if self.on_packet_log:
                    self.on_packet_log(f"[ERR 连接失败] 无法建立与从机 ({target_desc}) 的连接", "ERROR")
                # 从 [read_block] 返回执行结果给调用方，结束当前处理分支
                return False, [], f"无法连接到从机 ({target_desc})"

        # 2. 记录高精度起始时间戳并递增发送计数器
        # 在 [read_block] 中更新局部变量 [t0]：为后续协议编码、服务调度或界面渲染准备中间状态
        t0 = time.perf_counter()
        self.tx_count += 1
        # 在 [read_block] 中更新局部变量 [res]：为后续协议编码、服务调度或界面渲染准备中间状态
        res = None

        # 3. 映射功能码并构造标准十六进制请求报文供日志输出
        # 在 [read_block] 中更新局部变量 [fc]：为后续协议编码、服务调度或界面渲染准备中间状态
        fc = 3
        # 在 [read_block] 中更新局部变量 [fc_desc]：为后续协议编码、服务调度或界面渲染准备中间状态
        fc_desc = "读保持寄存器"
        # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if AreaType.COIL in area:
            # 在 [read_block] 中更新局部变量 [fc]：为后续协议编码、服务调度或界面渲染准备中间状态
            fc = 1
            # 在 [read_block] 中更新局部变量 [fc_desc]：为后续协议编码、服务调度或界面渲染准备中间状态
            fc_desc = "读线圈"
        # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        elif AreaType.DISCRETE_INPUT in area:
            # 在 [read_block] 中更新局部变量 [fc]：为后续协议编码、服务调度或界面渲染准备中间状态
            fc = 2
            # 在 [read_block] 中更新局部变量 [fc_desc]：为后续协议编码、服务调度或界面渲染准备中间状态
            fc_desc = "读离散输入"
        # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        elif AreaType.INPUT_REGISTER in area:
            # 在 [read_block] 中更新局部变量 [fc]：为后续协议编码、服务调度或界面渲染准备中间状态
            fc = 4
            # 在 [read_block] 中更新局部变量 [fc_desc]：为后续协议编码、服务调度或界面渲染准备中间状态
            fc_desc = "读输入寄存器"

        # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.comm_type == "RTU":
            # 在 [read_block] 中更新局部变量 [req_hex]：为后续协议编码、服务调度或界面渲染准备中间状态
            req_hex = build_modbus_rtu_req_hex(self.slave_id, fc, start_addr, count)
        else:
            # 在 [read_block] 中更新局部变量 [req_hex]：为后续协议编码、服务调度或界面渲染准备中间状态
            req_hex = build_modbus_tcp_req_hex(self.slave_id, fc, start_addr, count, self.tx_count)

        # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.on_packet_log:
            self.on_packet_log(
                f"[TX 问询报文] ID:{self.slave_id} | FC:{fc:02X}({fc_desc}) | 起始地址:{start_addr} | 数量:{count} | 帧:[{req_hex}]",
                "TX",
            )

        # 在 [read_block] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 4. 调用 pymodbus 客户端发起同步读取请求
            # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if AreaType.COIL in area:
                # 在 [read_block] 中更新局部变量 [res]：为后续协议编码、服务调度或界面渲染准备中间状态
                res = self.client.read_coils(start_addr, count=count, device_id=self.slave_id)
            # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            elif AreaType.DISCRETE_INPUT in area:
                # 在 [read_block] 中更新局部变量 [res]：为后续协议编码、服务调度或界面渲染准备中间状态
                res = self.client.read_discrete_inputs(start_addr, count=count, device_id=self.slave_id)
            # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            elif AreaType.INPUT_REGISTER in area:
                # 在 [read_block] 中更新局部变量 [res]：为后续协议编码、服务调度或界面渲染准备中间状态
                res = self.client.read_input_registers(start_addr, count=count, device_id=self.slave_id)
            else:  # HOLDING_REGISTER
                # 在 [read_block] 中更新局部变量 [res]：为后续协议编码、服务调度或界面渲染准备中间状态
                res = self.client.read_holding_registers(start_addr, count=count, device_id=self.slave_id)

            # 5. 计算往返耗时 RTT（毫秒，保留两位小数）
            # 在 [read_block] 中更新局部变量 [t1]：为后续协议编码、服务调度或界面渲染准备中间状态
            t1 = time.perf_counter()
            # 在 [read_block] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self.last_rtt_ms = round((t1 - t0) * 1000, 2)

            # 6. 检查 Modbus 协议级异常响应（如非法地址、从机忙等）
            # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if res.isError():
                self.err_count += 1
                # 在 [read_block] 中更新局部变量 [err_msg]：为后续协议编码、服务调度或界面渲染准备中间状态
                err_msg = str(res)
                # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if self.on_packet_log:
                    self.on_packet_log(f"[ERR 问询异常] ID:{self.slave_id} | FC:{fc:02X} | 错误: {err_msg}", "ERROR")
                # 从 [read_block] 返回执行结果给调用方，结束当前处理分支
                return False, [], err_msg

            # 7. 提取有效结果并更新缓存字典与接收计数器
            self.rx_count += 1
            # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if AreaType.COIL in area or AreaType.DISCRETE_INPUT in area:
                # 在 [read_block] 中更新局部变量 [vals]：为后续协议编码、服务调度或界面渲染准备中间状态
                vals = [1 if b else 0 for b in res.bits[:count]]
            else:
                # 在 [read_block] 中更新局部变量 [vals]：为后续协议编码、服务调度或界面渲染准备中间状态
                vals = list(res.registers[:count])

            # 在 [read_block] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for i, v in enumerate(vals):
                self.cached_registers[start_addr + i] = v

            # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self.on_packet_log:
                # 在 [read_block] 中更新局部变量 [preview]：为后续协议编码、服务调度或界面渲染准备中间状态
                preview = ", ".join(str(x) for x in vals[:8])
                # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if len(vals) > 8:
                    preview += f", ... (共{len(vals)}项)"
                self.on_packet_log(
                    f"[RX 响应报文] ID:{self.slave_id} | FC:{fc:02X} | 耗时:{self.last_rtt_ms:.1f}ms | 项数:{len(vals)} | 数据:[{preview}]",
                    "RX",
                )

            # 从 [read_block] 返回执行结果给调用方，结束当前处理分支
            return True, vals, ""

        except Exception as e:
            self.err_count += 1
            # 在 [read_block] 中更新局部变量 [err_msg]：为后续协议编码、服务调度或界面渲染准备中间状态
            err_msg = str(e)
            # 在 [read_block] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self.on_packet_log:
                self.on_packet_log(f"[ERR 问询异常] ID:{self.slave_id} | FC:{fc:02X} | 异常: {err_msg}", "ERROR")
            # 从 [read_block] 返回执行结果给调用方，结束当前处理分支
            return False, [], err_msg

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [write_single_register]（入参: address, value）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def write_single_register(self, address: int, value: int) -> Tuple[bool, str]:
        """使用功能码 FC06（0x06）向目标从机写入单个 16 位保持寄存器。"""
        # 参数：
        # address: 目标保持寄存器地址。
        # value: 待写入的 16 位整数值（内部按 `& 0xFFFF` 取低 16 位发送）。
        # 返回：
        # Tuple[bool, str]: `(是否成功, 结果或错误消息)`。
        # 在 [write_single_register] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self.is_connected and not self.connect():
            # 从 [write_single_register] 返回执行结果给调用方，结束当前处理分支
            return False, "未连接"
        # 在 [write_single_register] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.comm_type == "RTU":
            # 在 [write_single_register] 中更新局部变量 [req_hex]：为后续协议编码、服务调度或界面渲染准备中间状态
            req_hex = build_modbus_rtu_req_hex(self.slave_id, 0x06, address, value & 0xFFFF)
        else:
            # 在 [write_single_register] 中更新局部变量 [req_hex]：为后续协议编码、服务调度或界面渲染准备中间状态
            req_hex = build_modbus_tcp_req_hex(self.slave_id, 0x06, address, value & 0xFFFF, self.tx_count + 1)
        # 在 [write_single_register] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.on_packet_log:
            self.on_packet_log(f"[TX 写入报文] ID:{self.slave_id} | FC:06(写单个保持寄存器) | 地址:{address} | 数值:{value} | 帧:[{req_hex}]", "TX")
        # 在 [write_single_register] 中更新局部变量 [res]：为后续协议编码、服务调度或界面渲染准备中间状态
        res = self.client.write_register(address, value & 0xFFFF, device_id=self.slave_id)
        # 在 [write_single_register] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if res.isError():
            # 在 [write_single_register] 中更新局部变量 [err_msg]：为后续协议编码、服务调度或界面渲染准备中间状态
            err_msg = str(res)
            # 在 [write_single_register] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self.on_packet_log:
                self.on_packet_log(f"[ERR 写入异常] ID:{self.slave_id} | FC:06 | 错误: {err_msg}", "ERROR")
            # 从 [write_single_register] 返回执行结果给调用方，结束当前处理分支
            return False, err_msg
        # 在 [write_single_register] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.on_packet_log:
            self.on_packet_log(f"[RX 写入响应] ID:{self.slave_id} | FC:06 | 写入保持寄存器成功", "RX")
        # 从 [write_single_register] 返回执行结果给调用方，结束当前处理分支
        return True, "写入成功"

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [write_single_coil]（入参: address, value）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def write_single_coil(self, address: int, value: bool) -> Tuple[bool, str]:
        """使用功能码 FC05（0x05）向目标从机写入单个线圈状态。"""
        # 参数：
        # address: 目标线圈地址。
        # value: 布尔状态（协议帧中 `True` 编码为 `0xFF00`，`False` 编码为 `0x0000`）。
        # 返回：
        # Tuple[bool, str]: `(是否成功, 结果或错误消息)`。
        # 在 [write_single_coil] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self.is_connected and not self.connect():
            # 从 [write_single_coil] 返回执行结果给调用方，结束当前处理分支
            return False, "未连接"
        # Modbus 协议规定 FC05 置位线圈值为 0xFF00，复位线圈值为 0x0000
        # 在 [write_single_coil] 中更新局部变量 [coil_val]：为后续协议编码、服务调度或界面渲染准备中间状态
        coil_val = 0xFF00 if value else 0x0000
        # 在 [write_single_coil] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.comm_type == "RTU":
            # 在 [write_single_coil] 中更新局部变量 [req_hex]：为后续协议编码、服务调度或界面渲染准备中间状态
            req_hex = build_modbus_rtu_req_hex(self.slave_id, 0x05, address, coil_val)
        else:
            # 在 [write_single_coil] 中更新局部变量 [req_hex]：为后续协议编码、服务调度或界面渲染准备中间状态
            req_hex = build_modbus_tcp_req_hex(self.slave_id, 0x05, address, coil_val, self.tx_count + 1)
        # 在 [write_single_coil] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.on_packet_log:
            self.on_packet_log(f"[TX 写入报文] ID:{self.slave_id} | FC:05(写单个线圈) | 地址:{address} | 状态:{value} | 帧:[{req_hex}]", "TX")
        # 在 [write_single_coil] 中更新局部变量 [res]：为后续协议编码、服务调度或界面渲染准备中间状态
        res = self.client.write_coil(address, value, device_id=self.slave_id)
        # 在 [write_single_coil] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if res.isError():
            # 在 [write_single_coil] 中更新局部变量 [err_msg]：为后续协议编码、服务调度或界面渲染准备中间状态
            err_msg = str(res)
            # 在 [write_single_coil] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self.on_packet_log:
                self.on_packet_log(f"[ERR 写入异常] ID:{self.slave_id} | FC:05 | 错误: {err_msg}", "ERROR")
            # 从 [write_single_coil] 返回执行结果给调用方，结束当前处理分支
            return False, err_msg
        # 在 [write_single_coil] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self.on_packet_log:
            self.on_packet_log(f"[RX 写入响应] ID:{self.slave_id} | FC:05 | 写入线圈成功", "RX")
        # 从 [write_single_coil] 返回执行结果给调用方，结束当前处理分支
        return True, "写入成功"

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [write_typed]（入参: area, address, value, data_type）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def write_typed(
        self,
        area: str,
        address: int,
        value: Any,
        data_type: ModbusDataType,
        mode: ByteOrderMode = ByteOrderMode.ABCD,
    ) -> Tuple[bool, str]:
        """按指定数据类型与字节序编码工程值，并自动选择 FC05（单线圈）、FC06（单字寄存器）或 FC16（多字寄存器）发送写入请求。"""
        # 参数：
        # area: 存储区域标识（仅支持 0x 线圈或 4x 保持寄存器）。
        # address: 写入起始地址。
        # value: 工程数值（如浮点数 `23.5` 或 32 位整数）。
        # data_type: 数据类型 `ModbusDataType`。
        # mode: 字节序 `ByteOrderMode`。
        # 返回：
        # Tuple[bool, str]: `(是否成功, 响应提示消息)`。
        # 在 [write_typed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self.is_connected and not self.connect():
            # 从 [write_typed] 返回执行结果给调用方，结束当前处理分支
            return False, "未连接"

        # 在 [write_typed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if AreaType.COIL in area:
            # 在 [write_typed] 中更新局部变量 [b_val]：为后续协议编码、服务调度或界面渲染准备中间状态
            b_val = bool(int(value)) if str(value).isdigit() else bool(value)
            # 从 [write_typed] 返回执行结果给调用方，结束当前处理分支
            return self.write_single_coil(address, b_val)

        # 将工程值编码为 16 位寄存器数组：单字走 FC06，多字（如 FLOAT32/INT32/FLOAT64）走 FC16 (0x10)
        # 在 [write_typed] 中更新局部变量 [regs]：为后续协议编码、服务调度或界面渲染准备中间状态
        regs = encode_value(value, data_type, mode)
        # 在 [write_typed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if len(regs) == 1:
            # 从 [write_typed] 返回执行结果给调用方，结束当前处理分支
            return self.write_single_register(address, regs[0])
        else:
            # 在 [write_typed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self.on_packet_log:
                # 在 [write_typed] 中更新局部变量 [hex_regs]：为后续协议编码、服务调度或界面渲染准备中间状态
                hex_regs = ", ".join(f"0x{r:04X}" for r in regs)
                self.on_packet_log(
                    f"[TX 写入报文] ID:{self.slave_id} | FC:10(写多个保持寄存器) | 起始地址:{address} | 寄存器数:{len(regs)} | 值:[{hex_regs}]",
                    "TX",
                )
            # 在 [write_typed] 中更新局部变量 [res]：为后续协议编码、服务调度或界面渲染准备中间状态
            res = self.client.write_registers(address, regs, device_id=self.slave_id)
            # 在 [write_typed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if res.isError():
                # 在 [write_typed] 中更新局部变量 [err_msg]：为后续协议编码、服务调度或界面渲染准备中间状态
                err_msg = str(res)
                # 在 [write_typed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if self.on_packet_log:
                    self.on_packet_log(f"[ERR 写入异常] ID:{self.slave_id} | FC:10 | 错误: {err_msg}", "ERROR")
                # 从 [write_typed] 返回执行结果给调用方，结束当前处理分支
                return False, err_msg
            # 在 [write_typed] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self.on_packet_log:
                self.on_packet_log(f"[RX 写入响应] ID:{self.slave_id} | FC:10 | 成功写入 {len(regs)} 个寄存器", "RX")
            # 从 [write_typed] 返回执行结果给调用方，结束当前处理分支
            return True, f"成功写入 {len(regs)} 个寄存器"

    # [POLL-CONFIG] 启动后台连续定时循环轮询
    # 注意：on_poll_success / on_poll_error 回调直接在 worker 线程中触发，UI 消费者须自行调度切换到主线程
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [start_polling]（入参: area, start_addr, count, interval_ms）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def start_polling(self, area: str, start_addr: int, count: int, interval_ms: int = 1000) -> None:
        """启动后台守护线程，按指定时间间隔周期性调用 `read_block` 轮询目标从机。"""
        # 参数：
        # area: 轮询存储区域标识。
        # start_addr: 轮询起始地址。
        # count: 每次连续读取的寄存器/线圈数量。
        # interval_ms: 轮询周期毫秒数（最小钳位为 100ms，即 0.1s）。
        # 副作用与线程安全约定（为什么回调只传 `fresh` 字典）：
        # 1. 回调 `on_poll_success` 与 `on_poll_error` 在后台 `_poll_worker` 线程中直接触发，
        # UI 层（如 `PollPanel`）必须使用 `self.after(0, ...)` 切回 Tkinter 主线程更新表格控件。
        # 2. 每次轮询仅构造当次读取到的 `{addr: val}` 局部字典 `fresh` 传给回调，避免用户在运行中修改起始地址后，
        # 历史 `cached_registers` 中的旧地址脏数据继续污染新一轮 UI 表格渲染。
        # 在 [start_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._is_polling:
            # 从 [start_polling] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [start_polling] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._is_polling = True
        self._poll_stop_event.clear()
        # 在 [start_polling] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._poll_interval = max(0.1, interval_ms / 1000.0)

        # ──────────────────────────────────────────────────────────────────
        # 方法实现 [_poll_worker]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
        # ──────────────────────────────────────────────────────────────────
        def _poll_worker():
            """后台定时轮询工作线程主函数：循环执行单次块读取并通过 Event 实现可中断间隔等待。"""
            # 在 [start_polling] 中维持受控循环：配合运行标志位或事件信号量实现可持续中断的周期性处理
            while self._is_polling and not self._poll_stop_event.is_set():
                success, vals, err = self.read_block(area, start_addr, count)
                # 在 [start_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if success and self.on_poll_success:
                    # [POLL-CONFIG] 方案 A：使用本地 fresh 字典存储本次读取到的有效地址值，避免残留历史旧地址脏数据
                    # 在 [start_polling] 中更新局部变量 [fresh]：为后续协议编码、服务调度或界面渲染准备中间状态
                    fresh = {start_addr + i: v for i, v in enumerate(vals)}
                    self.on_poll_success(fresh)
                # 在 [start_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                elif not success and self.on_poll_error:
                    self.on_poll_error(err)

                # 使用 Event 等待间隔时间，若收到停止事件则立即退出循环，无需干等整段 sleep
                # 在 [start_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if self._poll_stop_event.wait(timeout=self._poll_interval):
                    break

        # 在 [start_polling] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._poll_thread = threading.Thread(target=_poll_worker, daemon=True)
        self._poll_thread.start()

    # [POLL-CONFIG] 安全停止后台连续轮询，防止遗留后台游离线程产生并发读写冲突
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [stop_polling]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def stop_polling(self) -> None:
        """安全停止后台连续轮询线程，置位 `_poll_stop_event` 并执行 `join` 等待线程彻底退出。"""
        # 关键设计（为什么必须 `join` 等待旧轮询线程退出）：
        # 若只把 `self._is_polling` 设为 `False` 而不唤醒和 `join` 旧线程，当用户修改轮询参数并立即重新点击“开始轮询”时，
        # 旧线程可能尚未退出，导致新旧两个轮询线程同时使用同一个 `ModbusTcpClient` socket 并发收发报文，造成事务 ID 错乱与粘包报错。
        # 在 [stop_polling] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._is_polling = False
        self._poll_stop_event.set()
        # 在 [stop_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)
            # 在 [stop_polling] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self._poll_thread.is_alive():
                logger.warning("ModbusPollEngine 轮询后台线程在 2.0s 内未退出，等待其自动终止")
            # 在 [stop_polling] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._poll_thread = None
