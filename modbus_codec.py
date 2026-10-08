"""Modbus 工业级通用编解码与变位转换引擎.

支持工业常用全部数据类型：
- BOOL (1-bit)
- INT16 / UINT16 (1 个寄存器)
- INT32 / UINT32 (2 个寄存器)
- INT64 / UINT64 (4 个寄存器)
- FLOAT32 (单精度浮点数，2 个寄存器)
- DOUBLE64 (双精度浮点数，4 个寄存器)
- HEX16 / HEX32 (十六进制)
- BINARY16 (二进制位串)
- STRING (ASCII 文本)

支持 4 种经典工业变位（Endianness / 字节序与字序）模式：
- ABCD : 标准大端 (Big-Endian, 高字节高字在前)
- CDAB : 字交换 (Word Swap / Mid-Little Endian，欧姆龙/三菱/能量表常用)
- BADC : 字节交换 (Byte Swap)
- DCBA : 纯小端 (Little-Endian，全部逆序)
"""

from enum import Enum
import math
import struct
from typing import Any, List, Tuple, Union


class ByteOrderMode(str, Enum):
    """变位模式枚举."""

    ABCD = "ABCD"  # 大端 (Big Endian)
    CDAB = "CDAB"  # 小端字交换 (Little Endian Word Swap)
    BADC = "BADC"  # 大端字节交换 (Big Endian Byte Swap)
    DCBA = "DCBA"  # 纯小端 (Little Endian)


class ModbusDataType(str, Enum):
    """数据类型枚举."""

    BOOL = "BOOL"
    INT16 = "INT16"
    UINT16 = "UINT16"
    INT32 = "INT32"
    UINT32 = "UINT32"
    INT64 = "INT64"
    UINT64 = "UINT64"
    FLOAT32 = "FLOAT32"
    DOUBLE64 = "DOUBLE64"
    HEX16 = "HEX16"
    HEX32 = "HEX32"
    BINARY16 = "BINARY16"
    STRING = "STRING"


# 每种数据类型对应的寄存器数量 (16位)
TYPE_REGISTER_COUNT = {
    ModbusDataType.BOOL: 1,  # 独立点或打包在寄存器
    ModbusDataType.INT16: 1,
    ModbusDataType.UINT16: 1,
    ModbusDataType.HEX16: 1,
    ModbusDataType.BINARY16: 1,
    ModbusDataType.INT32: 2,
    ModbusDataType.UINT32: 2,
    ModbusDataType.FLOAT32: 2,
    ModbusDataType.HEX32: 2,
    ModbusDataType.INT64: 4,
    ModbusDataType.UINT64: 4,
    ModbusDataType.DOUBLE64: 4,
    ModbusDataType.STRING: 2,  # 默认 2 寄存器 (4 字符)，可动态扩展
}


def transform_bytes(data: bytes, mode: Union[ByteOrderMode, str] = ByteOrderMode.ABCD) -> bytes:
    """根据变位模式对原始字节流进行重排 (该操作在 2/4/8 字节上是对合自逆的)."""
    mode_str = (mode.value if isinstance(mode, ByteOrderMode) else str(mode)).upper()
    if "." in mode_str:
        mode_str = mode_str.split(".")[-1]
    n = len(data)

    if n == 2:
        # 16-bit: 只有 ABCD (不变) 和 BADC/DCBA (两字节互换)
        if mode_str in ("BADC", "DCBA"):
            return bytes([data[1], data[0]])
        return data

    elif n == 4:
        # 32-bit: [A, B, C, D]
        a, b, c, d = data[0], data[1], data[2], data[3]
        if mode_str == "ABCD":
            return bytes([a, b, c, d])
        elif mode_str == "CDAB":  # 字交换
            return bytes([c, d, a, b])
        elif mode_str == "BADC":  # 字节交换
            return bytes([b, a, d, c])
        elif mode_str == "DCBA":  # 纯小端
            return bytes([d, c, b, a])
        return data

    elif n == 8:
        # 64-bit: 8 字节 [b0, b1, b2, b3, b4, b5, b6, b7]
        # 4 个字: W0=(b0,b1), W1=(b2,b3), W2=(b4,b5), W3=(b6,b7)
        if mode_str == "ABCD":
            return data
        elif mode_str == "CDAB":  # 字倒序: W3, W2, W1, W0
            return bytes(
                [
                    data[6],
                    data[7],
                    data[4],
                    data[5],
                    data[2],
                    data[3],
                    data[0],
                    data[1],
                ]
            )
        elif mode_str == "BADC":  # 字节每字内部交换
            return bytes(
                [
                    data[1],
                    data[0],
                    data[3],
                    data[2],
                    data[5],
                    data[4],
                    data[7],
                    data[6],
                ]
            )
        elif mode_str == "DCBA":  # 纯小端: 全反转
            return data[::-1]
        return data

    return data


def registers_to_bytes(registers: List[int]) -> bytes:
    """将 16 位整数寄存器列表打包为标准大端原始字节流."""
    return struct.pack(f">{len(registers)}H", *[r & 0xFFFF for r in registers])


def bytes_to_registers(raw: bytes) -> List[int]:
    """将字节流解析为 16 位寄存器整数列表 (长度需为偶数)."""
    if len(raw) % 2 != 0:
        raw += b"\x00"
    count = len(raw) // 2
    return list(struct.unpack(f">{count}H", raw))


def decode_value(
    registers: List[int],
    data_type: ModbusDataType,
    mode: ByteOrderMode = ByteOrderMode.ABCD,
    string_length: int = 4,
) -> Any:
    """从 16 位寄存器列表中，按照指定的类型和变位模式解码出实际值.

    :param registers: 原始 16 位整数列表
    :param data_type: 目标数据类型
    :param mode: 变位模式 (ABCD / CDAB / BADC / DCBA)
    :param string_length: STRING 类型时的字符长度
    :return: 解码后的 Python 值 (int, float, str, bool 等)
    """
    if not registers:
        return 0

    if data_type == ModbusDataType.BOOL:
        return bool(registers[0] & 1)

    # 1. 转为原始字节流
    raw_bytes = registers_to_bytes(registers)

    if data_type == ModbusDataType.INT16:
        adjusted = transform_bytes(raw_bytes[:2], mode)
        return struct.unpack(">h", adjusted)[0]

    elif data_type == ModbusDataType.UINT16:
        adjusted = transform_bytes(raw_bytes[:2], mode)
        return struct.unpack(">H", adjusted)[0]

    elif data_type == ModbusDataType.HEX16:
        adjusted = transform_bytes(raw_bytes[:2], mode)
        val = struct.unpack(">H", adjusted)[0]
        return f"0x{val:04X}"

    elif data_type == ModbusDataType.BINARY16:
        adjusted = transform_bytes(raw_bytes[:2], mode)
        val = struct.unpack(">H", adjusted)[0]
        b = f"{val:016b}"
        return f"{b[0:4]} {b[4:8]} {b[8:12]} {b[12:16]}"

    elif data_type == ModbusDataType.INT32:
        if len(raw_bytes) < 4:
            raw_bytes = raw_bytes.ljust(4, b"\x00")
        adjusted = transform_bytes(raw_bytes[:4], mode)
        return struct.unpack(">i", adjusted)[0]

    elif data_type == ModbusDataType.UINT32:
        if len(raw_bytes) < 4:
            raw_bytes = raw_bytes.ljust(4, b"\x00")
        adjusted = transform_bytes(raw_bytes[:4], mode)
        return struct.unpack(">I", adjusted)[0]

    elif data_type == ModbusDataType.FLOAT32:
        if len(raw_bytes) < 4:
            raw_bytes = raw_bytes.ljust(4, b"\x00")
        adjusted = transform_bytes(raw_bytes[:4], mode)
        f_val = struct.unpack(">f", adjusted)[0]
        if math.isnan(f_val):
            return "NaN"
        return round(f_val, 6)

    elif data_type == ModbusDataType.HEX32:
        if len(raw_bytes) < 4:
            raw_bytes = raw_bytes.ljust(4, b"\x00")
        adjusted = transform_bytes(raw_bytes[:4], mode)
        val = struct.unpack(">I", adjusted)[0]
        return f"0x{val:08X}"

    elif data_type == ModbusDataType.INT64:
        if len(raw_bytes) < 8:
            raw_bytes = raw_bytes.ljust(8, b"\x00")
        adjusted = transform_bytes(raw_bytes[:8], mode)
        return struct.unpack(">q", adjusted)[0]

    elif data_type == ModbusDataType.UINT64:
        if len(raw_bytes) < 8:
            raw_bytes = raw_bytes.ljust(8, b"\x00")
        adjusted = transform_bytes(raw_bytes[:8], mode)
        return struct.unpack(">Q", adjusted)[0]

    elif data_type == ModbusDataType.DOUBLE64:
        if len(raw_bytes) < 8:
            raw_bytes = raw_bytes.ljust(8, b"\x00")
        adjusted = transform_bytes(raw_bytes[:8], mode)
        d_val = struct.unpack(">d", adjusted)[0]
        if math.isnan(d_val):
            return "NaN"
        return round(d_val, 8)

    elif data_type == ModbusDataType.STRING:
        # 字符串可以截断到请求长度，并去除末尾 null
        clean = raw_bytes.decode("ascii", errors="replace").rstrip("\x00")
        return clean

    return registers[0]


def encode_value(
    value: Any,
    data_type: ModbusDataType,
    mode: ByteOrderMode = ByteOrderMode.ABCD,
    string_length: int = 4,
) -> List[int]:
    """将 Python 值按照指定的数据类型和变位模式编码为 16 位寄存器整数列表.

    :param value: 要编码的值 (支持字符串、整数、浮点数、十六进制等)
    :param data_type: 目标数据类型
    :param mode: 变位模式 (ABCD / CDAB / BADC / DCBA)
    :param string_length: STRING 类型的字节长度
    :return: 16 位无符号整数列表
    """
    if data_type == ModbusDataType.BOOL:
        b_val = bool(int(value)) if str(value).isdigit() else bool(value)
        return [1 if b_val else 0]

    if data_type == ModbusDataType.INT16:
        val = int(value)
        raw = struct.pack(">h", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.UINT16:
        val = int(value)
        raw = struct.pack(">H", val & 0xFFFF)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.HEX16:
        str_val = str(value).strip().lower()
        if str_val.startswith("0x"):
            str_val = str_val[2:]
        val = int(str_val or "0", 16) & 0xFFFF
        raw = struct.pack(">H", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.BINARY16:
        str_val = str(value).replace(" ", "").strip()
        val = int(str_val or "0", 2) & 0xFFFF
        raw = struct.pack(">H", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.INT32:
        val = int(value)
        raw = struct.pack(">i", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.UINT32:
        val = int(value)
        raw = struct.pack(">I", val & 0xFFFFFFFF)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.FLOAT32:
        val = float(value)
        raw = struct.pack(">f", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.HEX32:
        str_val = str(value).strip().lower()
        if str_val.startswith("0x"):
            str_val = str_val[2:]
        val = int(str_val or "0", 16) & 0xFFFFFFFF
        raw = struct.pack(">I", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.INT64:
        val = int(value)
        raw = struct.pack(">q", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.UINT64:
        val = int(value)
        raw = struct.pack(">Q", val & 0xFFFFFFFFFFFFFFFF)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.DOUBLE64:
        val = float(value)
        raw = struct.pack(">d", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    elif data_type == ModbusDataType.STRING:
        s_bytes = str(value).encode("ascii", errors="replace")
        target_len = max(len(s_bytes), string_length)
        if target_len % 2 != 0:
            target_len += 1
        s_bytes = s_bytes.ljust(target_len, b"\x00")
        return bytes_to_registers(s_bytes)

    return [0]
