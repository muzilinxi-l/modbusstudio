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
from typing import Any, Dict, List, Optional, Tuple, Union


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

# 各数据类型的取值上下限及数值构造器 (用于防止无符号数下溢与类型越界)
TYPE_RANGES: Dict[ModbusDataType, Tuple[Union[int, float], Union[int, float], type]] = {
    ModbusDataType.BOOL: (0, 1, int),
    ModbusDataType.INT16: (-32768, 32767, int),
    ModbusDataType.UINT16: (0, 65535, int),
    ModbusDataType.INT32: (-2147483648, 2147483647, int),
    ModbusDataType.UINT32: (0, 4294967295, int),
    ModbusDataType.INT64: (-9223372036854775808, 9223372036854775807, int),
    ModbusDataType.UINT64: (0, 18446744073709551615, int),
    ModbusDataType.FLOAT32: (-3.402823466e38, 3.402823466e38, float),
    ModbusDataType.DOUBLE64: (-1.7976931348623157e308, 1.7976931348623157e308, float),
}


def clamp_value_to_type(val: Any, data_type: ModbusDataType) -> Any:
    """根据数据类型严格钳位合法数值范围，防止无符号整型下溢变成 65535 或 4294967295 等异常巨值."""
    if data_type == ModbusDataType.BOOL:
        if isinstance(val, bool):
            return 1 if val else 0
        try:
            return 1 if int(val) != 0 else 0
        except Exception:
            return 0

    if data_type in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
        return val

    limits = TYPE_RANGES.get(data_type)
    if not limits:
        return val

    min_v, max_v, v_type = limits
    try:
        numeric = v_type(val)
        if v_type is int:
            return max(min_v, min(max_v, numeric))
        else:
            return max(min_v, min(max_v, numeric))
    except Exception:
        return min_v if min_v > 0 else 0


# =====================================================================
# 工业标准 44 种数据类型字典 (对应图二与图三规范)
# 格式: ID -> (英文名称, 中文名称, ModbusDataType, ByteOrderMode, 寄存器数)
# =====================================================================
HEMS_TYPE_DEFINITIONS: Dict[int, Tuple[str, str, ModbusDataType, ByteOrderMode, int]] = {
    1: ("Invalid", "无效", ModbusDataType.INT16, ByteOrderMode.ABCD, 1),
    2: ("Bit", "比特", ModbusDataType.BOOL, ByteOrderMode.ABCD, 1),
    3: ("Signed char", "有符号字节", ModbusDataType.INT16, ByteOrderMode.ABCD, 1),
    4: ("Unsigned char", "无符号字节", ModbusDataType.UINT16, ByteOrderMode.ABCD, 1),
    5: ("Signed short 1", "有符号短整型1", ModbusDataType.INT16, ByteOrderMode.ABCD, 1),
    6: ("Signed short 2", "有符号短整型2", ModbusDataType.INT16, ByteOrderMode.BADC, 1),
    7: ("Unsigned short 1", "无符号短整型1", ModbusDataType.UINT16, ByteOrderMode.ABCD, 1),
    8: ("Unsigned short 2", "无符号短整型2", ModbusDataType.UINT16, ByteOrderMode.BADC, 1),
    9: ("Signed int 1", "有符号整形1", ModbusDataType.INT32, ByteOrderMode.ABCD, 2),
    10: ("Signed int 2", "有符号整形2", ModbusDataType.INT32, ByteOrderMode.BADC, 2),
    11: ("Signed int 3", "有符号整形3", ModbusDataType.INT32, ByteOrderMode.CDAB, 2),
    12: ("Signed int 4", "有符号整形4", ModbusDataType.INT32, ByteOrderMode.DCBA, 2),
    13: ("Unsigned int 1", "无符号整形1", ModbusDataType.UINT32, ByteOrderMode.ABCD, 2),
    14: ("Unsigned int 2", "无符号整形2", ModbusDataType.UINT32, ByteOrderMode.BADC, 2),
    15: ("Unsigned int 3", "无符号整形3", ModbusDataType.UINT32, ByteOrderMode.CDAB, 2),
    16: ("Unsigned int 4", "无符号整形4", ModbusDataType.UINT32, ByteOrderMode.DCBA, 2),
    17: ("Signed int64 1", "有符号长整形1", ModbusDataType.INT64, ByteOrderMode.ABCD, 4),
    18: ("Signed int64 2", "有符号长整形2", ModbusDataType.INT64, ByteOrderMode.BADC, 4),
    19: ("Signed int64 3", "有符号长整形3", ModbusDataType.INT64, ByteOrderMode.CDAB, 4),
    20: ("Signed int64 4", "有符号长整形4", ModbusDataType.INT64, ByteOrderMode.DCBA, 4),
    21: ("Signed int64 5", "有符号长整形5", ModbusDataType.INT64, ByteOrderMode.ABCD, 4),
    22: ("Signed int64 6", "有符号长整形6", ModbusDataType.INT64, ByteOrderMode.BADC, 4),
    23: ("Signed int64 7", "有符号长整形7", ModbusDataType.INT64, ByteOrderMode.CDAB, 4),
    24: ("Signed int64 8", "有符号长整形8", ModbusDataType.INT64, ByteOrderMode.DCBA, 4),
    25: ("Unsigned int64 1", "无符号长整形1", ModbusDataType.UINT64, ByteOrderMode.ABCD, 4),
    26: ("Unsigned int64 2", "无符号长整形2", ModbusDataType.UINT64, ByteOrderMode.BADC, 4),
    27: ("Unsigned int64 3", "无符号长整形3", ModbusDataType.UINT64, ByteOrderMode.CDAB, 4),
    28: ("Unsigned int64 4", "无符号长整形4", ModbusDataType.UINT64, ByteOrderMode.DCBA, 4),
    29: ("Unsigned int64 5", "无符号长整形5", ModbusDataType.UINT64, ByteOrderMode.ABCD, 4),
    30: ("Unsigned int64 6", "无符号长整形6", ModbusDataType.UINT64, ByteOrderMode.BADC, 4),
    31: ("Unsigned int64 7", "无符号长整形7", ModbusDataType.UINT64, ByteOrderMode.CDAB, 4),
    32: ("Unsigned int64 8", "无符号长整形8", ModbusDataType.UINT64, ByteOrderMode.DCBA, 4),
    33: ("Float 1", "浮点数1", ModbusDataType.FLOAT32, ByteOrderMode.ABCD, 2),
    34: ("Float 2", "浮点数2", ModbusDataType.FLOAT32, ByteOrderMode.BADC, 2),
    35: ("Float 3", "浮点数3", ModbusDataType.FLOAT32, ByteOrderMode.CDAB, 2),
    36: ("Float 4", "浮点数4", ModbusDataType.FLOAT32, ByteOrderMode.DCBA, 2),
    37: ("Double 1", "双精度浮点数1", ModbusDataType.DOUBLE64, ByteOrderMode.ABCD, 4),
    38: ("Double 2", "双精度浮点型2", ModbusDataType.DOUBLE64, ByteOrderMode.BADC, 4),
    39: ("Double 3", "双精度浮点型3", ModbusDataType.DOUBLE64, ByteOrderMode.CDAB, 4),
    40: ("Double 4", "双精度浮点型4", ModbusDataType.DOUBLE64, ByteOrderMode.DCBA, 4),
    41: ("Double 5", "双精度浮点型5", ModbusDataType.DOUBLE64, ByteOrderMode.ABCD, 4),
    42: ("Double 6", "双精度浮点型6", ModbusDataType.DOUBLE64, ByteOrderMode.BADC, 4),
    43: ("Double 7", "双精度浮点型7", ModbusDataType.DOUBLE64, ByteOrderMode.CDAB, 4),
    44: ("Double 8", "双精度浮点型8", ModbusDataType.DOUBLE64, ByteOrderMode.DCBA, 4),
}


def parse_hems_type(indicator: Union[str, int]) -> Optional[Tuple[ModbusDataType, ByteOrderMode, int]]:
    """根据图二/图三的类型英文名称、中文名称或序号 ID，解析出 (ModbusDataType, ByteOrderMode, 寄存器数)."""
    if isinstance(indicator, int) or (isinstance(indicator, str) and str(indicator).strip().isdigit()):
        type_id = int(indicator)
        if type_id in HEMS_TYPE_DEFINITIONS:
            _, _, dtype, mode, cnt = HEMS_TYPE_DEFINITIONS[type_id]
            return dtype, mode, cnt

    key = str(indicator).strip().lower()
    for _, (eng, chn, dtype, mode, cnt) in HEMS_TYPE_DEFINITIONS.items():
        if key == eng.lower() or key == chn.lower():
            return dtype, mode, cnt
    return None


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
    # 强关联数据类型安全钳位，杜绝无符号类型负数溢出或数值爆大
    value = clamp_value_to_type(value, data_type)

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
