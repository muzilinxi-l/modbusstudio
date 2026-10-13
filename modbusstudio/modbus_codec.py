"""modbus_codec.py — Modbus 工业级通用数据类型编解码与 4 种字节序变位转换引擎。
关键导入：enum（定义数据类型与字节序枚举）、math（检测 IEEE 754 浮点数 NaN）、struct（底层 C 二进制字节流打包与解包）、typing（类型注解）。
对外暴露：ByteOrderMode/ModbusDataType、HEMS_TYPE_DEFINITIONS/parse_hems_type、transform_bytes/registers_to_bytes/bytes_to_registers/decode_value/encode_value/clamp_value_to_type。
"""

# 导入 Enum：构建继承自 str 的数据类型与字节序枚举，方便与 models.py 及 JSON 字典无缝互转
from enum import Enum
# 导入 math：用于通过 math.isnan() 识别 IEEE 754 浮点数解码出的 NaN（如寄存器全为 0xFFFF 时）
import math
# 导入 struct：Python 标准库二进制打包/解包核心模块，底层调用 C 语言实现极速字节序与补码转换
# 常用 struct 格式符速查表（本文件全程使用 `>` 大端前缀，因为变位重排已由 transform_bytes 预先完成）：
#   - `>h` / `>H` : 2 字节（1 个寄存器）有符号 / 无符号 16 位短整数（int16 / uint16）
#   - `>i` / `>I` : 4 字节（2 个寄存器）有符号 / 无符号 32 位整数（int32 / uint32）
#   - `>q` / `>Q` : 8 字节（4 个寄存器）有符号 / 无符号 64 位长整数（int64 / uint64）
#   - `>f` / `>d` : 4 字节单精度（float32） / 8 字节双精度（float64/double）IEEE 754 浮点数
# 为什么工业 Modbus 协议中经常遇到负数变成 65535（如 -1 -> 65535、-100 -> 65436）：
#   因为 16 位寄存器在链路上传输的永远是 2 个原始字节（0x0000 ~ 0xFFFF）；
#   当表示负数时采用“二进制补码（Two's Complement）”：负数 x 的无符号表示为 65536 + x（如 -1 = 65535 = 0xFFFF）。
#   解码时若误选 UINT16（`>H`）就会显示 65535，只有选择 INT16（`>h`）才会还原为 -1。
import struct
# 导入 typing 类型注解工具：明确标识寄存器列表 List[int] 与多类型返回契约
from typing import Any, Dict, List, Optional, Tuple, Union


# =============================================================================
# 1. 字节序与字序变位模式枚举（ByteOrderMode）
# 为什么要用 4 个字母 A B C D 来直观命名字节序：
#   假设一个 32 位数据在大端标准下的 4 个字节依次为 [A(最高字节), B, C, D(最低字节)]，
#   占两个 16 位寄存器 R0=(A,B) 与 R1=(C,D)：
#   +------+-------------------+--------------+--------------+--------------------------+
#   | 模式 | 内存字节排列顺序  | 第1寄存器 R0 | 第2寄存器 R1 | 工业典型设备场景         |
#   +------+-------------------+--------------+--------------+--------------------------+
#   | ABCD | [A, B, C, D]      | 0xAB         | 0xCD         | 标准大端 PLC / 施耐德    |
#   | CDAB | [C, D, A, B]      | 0xCD (低字)  | 0xAB (高字)  | 国内 PCS/BMS/电表最常用  |
#   | BADC | [B, A, D, C]      | 0xBA         | 0xDC         | 16位字内高低字节交换     |
#   | DCBA | [D, C, B, A]      | 0xDC         | 0xBA         | 纯小端 x86/ARM 原生内存  |
#   +------+-------------------+--------------+--------------+--------------------------+
# =============================================================================
class ByteOrderMode(str, Enum):
    """多寄存器字节序与字序变位模式枚举。
    成员变量：ABCD（标准大端）、CDAB（大端字交换）、BADC（大端字节交换）、DCBA（纯小端全逆序）。
    使用场景：作为 transform_bytes、decode_value、encode_value 的模式参数控制字节重排。
    """

    ABCD = "ABCD"  # 大端 (Big Endian)
    CDAB = "CDAB"  # 小端字交换 (Little Endian Word Swap)
    BADC = "BADC"  # 大端字节交换 (Big Endian Byte Swap)
    DCBA = "DCBA"  # 纯小端 (Little Endian)


# =============================================================================
# 2. 编解码器支持的工业数据类型枚举（ModbusDataType）
# 为什么编解码层的枚举比 UI 模型层的 DataType 多出 INT64/UINT64/HEX32/BINARY16：
#   因为现场一体机（extra/hems.cdb）底层支持 44 种细分类型（含 64 位长整型与二进制位串），
#   底层编解码器保持全集能力，向上可无缝覆盖所有扩展需求。
# =============================================================================
class ModbusDataType(str, Enum):
    """编解码器底层支持的 13 种工业数据类型枚举。
    成员变量：BOOL、INT16/UINT16、INT32/UINT32、INT64/UINT64、FLOAT32/DOUBLE64、HEX16/HEX32、BINARY16、STRING。
    使用场景：指导 struct.pack / struct.unpack 选择正确的格式化字符（如 >h, >H, >i, >I, >f, >d）。
    """

    # 1 位布尔值（0 或 1）
    BOOL = "BOOL"
    # 16 位有符号/无符号短整数（占 1 个寄存器）
    INT16 = "INT16"
    UINT16 = "UINT16"
    # 32 位有符号/无符号整数（占 2 个连续寄存器）
    INT32 = "INT32"
    UINT32 = "UINT32"
    # 64 位有符号/无符号长整数（占 4 个连续寄存器）
    INT64 = "INT64"
    UINT64 = "UINT64"
    # 32 位单精度 / 64 位双精度 IEEE 754 浮点数
    FLOAT32 = "FLOAT32"
    DOUBLE64 = "DOUBLE64"
    # 16 位 / 32 位十六进制字符串格式（如 0x1234）
    HEX16 = "HEX16"
    HEX32 = "HEX32"
    # 16 位按 4-bit 分组的二进制位串格式（如 "0001 0010 0011 0100"）
    BINARY16 = "BINARY16"
    # 多寄存器 ASCII 字符串格式
    STRING = "STRING"


# 每种数据类型对应的寄存器数量 (16位)
# 为什么需要维护这张静态查表字典：
#   Modbus 协议读写的最小字单位是 16-bit 寄存器（2 字节），32 位类型必须一次连续读写 2 个寄存器，64 位必须连续读写 4 个寄存器
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
# 为什么要显式定义二进制补码边界（如 INT16 为 -32768 ~ 32767）：
#   16 位有符号整数最高位 bit15 是符号位（0 正 1 负），负数用补码表示（例如 -1 的 16 位补码是 0xFFFF = 65535）；
#   若不对用户输入或随机波动后的数值做上下限钳位，struct.pack('>h', 40000) 会直接抛出 struct.error 崩溃！
TYPE_RANGES: Dict[ModbusDataType, Tuple[Union[int, float], Union[int, float], type]] = {
    # 1 位布尔量：仅允许 0（关/分闸）或 1（开/合闸）
    ModbusDataType.BOOL: (0, 1, int),
    # 16 位有符号整数：-2^15 (-32768) ~ 2^15 - 1 (32767)
    ModbusDataType.INT16: (-32768, 32767, int),
    # 16 位无符号整数：0 ~ 2^16 - 1 (65535)
    ModbusDataType.UINT16: (0, 65535, int),
    # 32 位有符号整数：-2^31 ~ 2^31 - 1
    ModbusDataType.INT32: (-2147483648, 2147483647, int),
    # 32 位无符号整数：0 ~ 2^32 - 1
    ModbusDataType.UINT32: (0, 4294967295, int),
    # 64 位有符号长整数：-2^63 ~ 2^63 - 1
    ModbusDataType.INT64: (-9223372036854775808, 9223372036854775807, int),
    # 64 位无符号长整数：0 ~ 2^64 - 1
    ModbusDataType.UINT64: (0, 18446744073709551615, int),
    # IEEE 754 32 位单精度浮点数最大有限正负范围（约 ±3.4028e38，超出即变为 ±inf）
    ModbusDataType.FLOAT32: (-3.402823466e38, 3.402823466e38, float),
    # IEEE 754 64 位双精度浮点数最大有限正负范围（约 ±1.7977e308）
    ModbusDataType.DOUBLE64: (-1.7976931348623157e308, 1.7976931348623157e308, float),
}


def clamp_value_to_type(val: Any, data_type: ModbusDataType) -> Any:
    """根据数据类型严格钳位合法数值范围，防止无符号整型下溢变成 65535 或有符号整型越界引发 struct.error。
    参数：val（待校验输入值 Any）、data_type（目标数据类型 ModbusDataType）。返回：Any，钳位后的安全值。
    步骤：1.BOOL 归一化为 0/1；2.文本与十六进制原样放行；3.查表获取上下限并用 max/min 夹逼；4.异常输入兜底归零。副作用：无。
    """
    # 针对布尔类型做特殊处理：将 True/非零整数统一转为 1，False/0/非法字符串统一转为 0
    if data_type == ModbusDataType.BOOL:
        if isinstance(val, bool):
            return 1 if val else 0
        try:
            # 支持字符串如 "1"、"-1" 转为真（1），"0" 转为假（0）
            return 1 if int(val) != 0 else 0
        except Exception:
            # 转换失败（如空串）时安全回退为 0
            return 0

    # 字符串与十六进制/二进制位串不走十进制数值上下限夹逼，交由后续专用分支解析
    if data_type in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
        return val

    # 查表获取当前类型的 (最小值, 最大值, Python 构造类型 int/float)，兼容上层模型传入的 "FLOAT64"
    lookup_type = ModbusDataType.DOUBLE64 if data_type == "FLOAT64" else data_type
    limits = TYPE_RANGES.get(lookup_type)
    if not limits:
        return val

    min_v, max_v, v_type = limits
    # 为什么外包 try/except 并做 max(min_v, min(max_v, numeric)) 钳位：
    #   例如在仿真随机波动时，UINT16 点位原本为 0，随机减去 2 变成 -2；
    #   若不钳位而直接做 `-2 & 0xFFFF`，无符号数会瞬间下溢变成 65534，导致 EMS 误报严重过压故障！
    try:
        numeric = v_type(val)
        if v_type is int:
            # 整型夹逼在 [min_v, max_v] 闭区间内
            return max(min_v, min(max_v, numeric))
        else:
            # 浮点型同样夹逼在有限浮点数范围内，避免产生正负无穷大 (inf)
            return max(min_v, min(max_v, numeric))
    except Exception:
        # 当传入无法转为数字的脏字符串时，安全回退到 0（或正下限）
        return min_v if min_v > 0 else 0


# =====================================================================
# 工业标准 44 种数据类型字典 (严格对齐 extra/hems.cdb menu 表 Variable Transfer Format 0~43)
# 格式: ID -> (英文名称, 中文名称, ModbusDataType, ByteOrderMode, 寄存器数)
# 为什么现场一体机（extra/hems.cdb）要把数据类型编号为 0~43：
#   一体机底层 C++ 驱动把“基础数据类型 + 字节序变位模式”合并编码成了单个整数 ID（如 0=Invalid，1=Bit，
#   32=Float 1=FLOAT32+ABCD，34=Float 3=FLOAT32+CDAB）。通过这张字典，导入 .cdb 数据库时即可一键还原出精确的数据类型与字节序。
#   尾号规律速记：1=ABCD（标准大端），2=BADC（字内字节交换），3=CDAB（大端字交换），4=DCBA（纯小端）。
# =====================================================================
HEMS_TYPE_DEFINITIONS: Dict[int, Tuple[str, str, ModbusDataType, ByteOrderMode, int]] = {
    # 0~3：无效占位与基础单字节/比特类型（在 Modbus 中仍至少占用 1 个寄存器/位地址）
    0: ("Invalid", "无效", ModbusDataType.INT16, ByteOrderMode.ABCD, 1),
    1: ("Bit", "比特", ModbusDataType.BOOL, ByteOrderMode.ABCD, 1),
    2: ("Signed char", "有符号字节", ModbusDataType.INT16, ByteOrderMode.ABCD, 1),
    3: ("Unsigned char", "无符号字节", ModbusDataType.UINT16, ByteOrderMode.ABCD, 1),
    # 4~7：16 位短整型（1=标准大端 ABCD，2=字内高低字节交换 BADC）
    4: ("Signed short 1", "有符号短整型1", ModbusDataType.INT16, ByteOrderMode.ABCD, 1),
    5: ("Signed short 2", "有符号短整型2", ModbusDataType.INT16, ByteOrderMode.BADC, 1),
    6: ("Unsigned short 1", "无符号短整型1", ModbusDataType.UINT16, ByteOrderMode.ABCD, 1),
    7: ("Unsigned short 2", "无符号短整型2", ModbusDataType.UINT16, ByteOrderMode.BADC, 1),
    # 8~11：32 位有符号整型（后缀 1=ABCD, 2=BADC, 3=CDAB, 4=DCBA，占 2 个寄存器）
    8: ("Signed int 1", "有符号整形1", ModbusDataType.INT32, ByteOrderMode.ABCD, 2),
    9: ("Signed int 2", "有符号整形2", ModbusDataType.INT32, ByteOrderMode.BADC, 2),
    10: ("Signed int 3", "有符号整形3", ModbusDataType.INT32, ByteOrderMode.CDAB, 2),
    11: ("Signed int 4", "有符号整形4", ModbusDataType.INT32, ByteOrderMode.DCBA, 2),
    # 12~15：32 位无符号整型（后缀 1=ABCD, 2=BADC, 3=CDAB, 4=DCBA，占 2 个寄存器）
    12: ("Unsigned int 1", "无符号整形1", ModbusDataType.UINT32, ByteOrderMode.ABCD, 2),
    13: ("Unsigned int 2", "无符号整形2", ModbusDataType.UINT32, ByteOrderMode.BADC, 2),
    14: ("Unsigned int 3", "无符号整形3", ModbusDataType.UINT32, ByteOrderMode.CDAB, 2),
    15: ("Unsigned int 4", "无符号整形4", ModbusDataType.UINT32, ByteOrderMode.DCBA, 2),
    # 16~23：64 位有符号长整型（1~4 与 5~8 均映射为四模式，占 4 个连续寄存器）
    16: ("Signed int64 1", "有符号长整形1", ModbusDataType.INT64, ByteOrderMode.ABCD, 4),
    17: ("Signed int64 2", "有符号长整形2", ModbusDataType.INT64, ByteOrderMode.BADC, 4),
    18: ("Signed int64 3", "有符号长整形3", ModbusDataType.INT64, ByteOrderMode.CDAB, 4),
    19: ("Signed int64 4", "有符号长整形4", ModbusDataType.INT64, ByteOrderMode.DCBA, 4),
    20: ("Signed int64 5", "有符号长整形5", ModbusDataType.INT64, ByteOrderMode.ABCD, 4),
    21: ("Signed int64 6", "有符号长整形6", ModbusDataType.INT64, ByteOrderMode.BADC, 4),
    22: ("Signed int64 7", "有符号长整形7", ModbusDataType.INT64, ByteOrderMode.CDAB, 4),
    23: ("Signed int64 8", "有符号长整形8", ModbusDataType.INT64, ByteOrderMode.DCBA, 4),
    # 24~31：64 位无符号长整型（占 4 个连续寄存器）
    24: ("Unsigned int64 1", "无符号长整形1", ModbusDataType.UINT64, ByteOrderMode.ABCD, 4),
    25: ("Unsigned int64 2", "无符号长整形2", ModbusDataType.UINT64, ByteOrderMode.BADC, 4),
    26: ("Unsigned int64 3", "无符号长整形3", ModbusDataType.UINT64, ByteOrderMode.CDAB, 4),
    27: ("Unsigned int64 4", "无符号长整形4", ModbusDataType.UINT64, ByteOrderMode.DCBA, 4),
    28: ("Unsigned int64 5", "无符号长整形5", ModbusDataType.UINT64, ByteOrderMode.ABCD, 4),
    29: ("Unsigned int64 6", "无符号长整形6", ModbusDataType.UINT64, ByteOrderMode.BADC, 4),
    30: ("Unsigned int64 7", "无符号长整形7", ModbusDataType.UINT64, ByteOrderMode.CDAB, 4),
    31: ("Unsigned int64 8", "无符号长整形8", ModbusDataType.UINT64, ByteOrderMode.DCBA, 4),
    # 32~35：32 位 IEEE 754 单精度浮点数（1=ABCD, 2=BADC, 3=CDAB, 4=DCBA，储能现场最常用 32 与 34）
    32: ("Float 1", "浮点数1", ModbusDataType.FLOAT32, ByteOrderMode.ABCD, 2),
    33: ("Float 2", "浮点数2", ModbusDataType.FLOAT32, ByteOrderMode.BADC, 2),
    34: ("Float 3", "浮点数3", ModbusDataType.FLOAT32, ByteOrderMode.CDAB, 2),
    35: ("Float 4", "浮点数4", ModbusDataType.FLOAT32, ByteOrderMode.DCBA, 2),
    # 36~43：64 位 IEEE 754 双精度浮点数（注意 Double 1 为"双精度浮点数1"，Double 2~8 为"双精度浮点型2~8"，严格照 menu 表保留）
    36: ("Double 1", "双精度浮点数1", ModbusDataType.DOUBLE64, ByteOrderMode.ABCD, 4),
    37: ("Double 2", "双精度浮点型2", ModbusDataType.DOUBLE64, ByteOrderMode.BADC, 4),
    38: ("Double 3", "双精度浮点型3", ModbusDataType.DOUBLE64, ByteOrderMode.CDAB, 4),
    39: ("Double 4", "双精度浮点型4", ModbusDataType.DOUBLE64, ByteOrderMode.DCBA, 4),
    40: ("Double 5", "双精度浮点型5", ModbusDataType.DOUBLE64, ByteOrderMode.ABCD, 4),
    41: ("Double 6", "双精度浮点型6", ModbusDataType.DOUBLE64, ByteOrderMode.BADC, 4),
    42: ("Double 7", "双精度浮点型7", ModbusDataType.DOUBLE64, ByteOrderMode.CDAB, 4),
    43: ("Double 8", "双精度浮点型8", ModbusDataType.DOUBLE64, ByteOrderMode.DCBA, 4),
}


def parse_hems_type(indicator: Union[str, int]) -> Optional[Tuple[ModbusDataType, ByteOrderMode, int]]:
    """根据一体机数据库的序号 ID（0~43）、英文名或中文名，解析出（数据类型, 字节序, 寄存器数）三元组。
    参数：indicator（类型编号 int 或名称 str）。返回：匹配成功返回三元组，0(Invalid) 或失败返回 None。
    步骤：1.若为数字直接按字典主键 O(1) 查表（0=Invalid 返回 None）；2.否则转小写遍历比对中英文名称；3.未命中返回 None。副作用：无。
    """
    # 分支 1：当传入数字 ID（如数据库字段中的 6 或 "6"）时，直接用字典键查找，速度最快
    if (isinstance(indicator, int) and not isinstance(indicator, bool)) or (
        isinstance(indicator, str) and str(indicator).strip().isdigit()
    ):
        type_id = int(str(indicator).strip())
        # 0=Invalid（无效）：返回 None 交给上层 map_db_format_to_data_type 走默认兜底逻辑，不映射为有效类型
        if type_id == 0:
            return None
        if type_id in HEMS_TYPE_DEFINITIONS:
            # 忽略前两项中英文展示名称，只提取核心的 (ModbusDataType, ByteOrderMode, 占用寄存器数)
            _, _, dtype, mode, cnt = HEMS_TYPE_DEFINITIONS[type_id]
            return dtype, mode, cnt

    # 分支 2：当传入英文名（如 "Unsigned short 1"）或中文名（如 "无符号短整型1"）时，忽略大小写遍历匹配
    key = str(indicator).strip().lower()
    if not key or key in ("invalid", "无效"):
        return None
    for type_id, (eng, chn, dtype, mode, cnt) in HEMS_TYPE_DEFINITIONS.items():
        if type_id == 0:
            continue
        if key == eng.lower() or key == chn.lower():
            return dtype, mode, cnt
    # 若既不是合法编号也不是已知名称，返回 None 交由上层使用默认类型兜底
    return None


def transform_bytes(data: bytes, mode: Union[ByteOrderMode, str] = ByteOrderMode.ABCD) -> bytes:
    """按指定的字节序变位模式（ABCD/CDAB/BADC/DCBA）重排 2/4/8 字节流。
    参数：data（原始大端字节流 bytes，长度 2/4/8）、mode（变位模式）。返回：bytes，重排后的字节流。
    步骤：1.提取大写模式名；2.按字节长度 n=2/4/8 分支重排索引；3.返回新 bytes。副作用：无。
    """
    # 为什么编码（encode_value）和解码（decode_value）能调用同一个 transform_bytes 函数：
    #   因为 ABCD（恒等）、CDAB（高低字对调）、BADC（字内高低字节对调）、DCBA（全逆序）
    #   在群论上全部是“2 阶对合置换（Involution）”：即 transform(transform(x, m), m) == x！
    # 兼容传入枚举实例或带类名前缀的字符串（如 "ByteOrder.CDAB"）
    mode_str = (mode.value if isinstance(mode, ByteOrderMode) else str(mode)).upper()
    if "." in mode_str:
        mode_str = mode_str.split(".")[-1]
    n = len(data)

    # 情况 1：单寄存器（16-bit = 2 字节 [A, B]）
    # 为什么 CDAB 在 2 字节时保持不变：因为只有 1 个 16 位字（Word），没有第二个字可供跨字交换
    if n == 2:
        # 16-bit: 只有 ABCD (不变) 和 BADC/DCBA (两字节互换)
        if mode_str in ("BADC", "DCBA"):
            # 将索引 0 和索引 1 的字节互换位置
            return bytes([data[1], data[0]])
        return data

    # 情况 2：双寄存器（32-bit = 4 字节 [A, B, C, D]，最核心的工业 32 位变位逻辑）
    elif n == 4:
        # 32-bit: [A, B, C, D]
        # 解包出 4 个独立字节（Python 中 bytes 索引取出的是 0~255 的 int）
        a, b, c, d = data[0], data[1], data[2], data[3]
        if mode_str == "ABCD":
            # 标准大端：保持原序 [A, B, C, D]
            return bytes([a, b, c, d])
        elif mode_str == "CDAB":  # 字交换
            # 字交换（Word Swap）：把后两个字节 (C, D) 放到前两个字节 (A, B) 之前
            return bytes([c, d, a, b])
        elif mode_str == "BADC":  # 字节交换
            # 字节交换（Byte Swap）：每个 16 位寄存器内部高低字节对调
            return bytes([b, a, d, c])
        elif mode_str == "DCBA":  # 纯小端
            # 纯小端（Little Endian）：4 个字节彻底倒序
            return bytes([d, c, b, a])
        return data

    # 情况 3：四寄存器（64-bit = 8 字节，用于 INT64 / UINT64 / DOUBLE64）
    elif n == 8:
        # 64-bit: 8 字节 [b0, b1, b2, b3, b4, b5, b6, b7]
        # 4 个字: W0=(b0,b1), W1=(b2,b3), W2=(b4,b5), W3=(b6,b7)
        if mode_str == "ABCD":
            # 标准大端：直接原样返回 8 字节
            return data
        elif mode_str == "CDAB":  # 字倒序: W3, W2, W1, W0
            # 以 2 字节（1 个寄存器字）为单位整体倒序排列，字内高低字节不变
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
            # 4 个字顺序不变，但每个字内部的奇偶字节互换
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
            # 切片步长 -1 实现 8 字节完全逆序
            return data[::-1]
        return data

    return data


def registers_to_bytes(registers: List[int]) -> bytes:
    """将 16 位无符号整数寄存器列表按 Modbus 标准大端打包为连续二进制字节流。
    参数：registers（16位整数列表 List[int]）。返回：bytes，长度为 `2 * len(registers)` 的大端字节流。
    步骤：1.对每个整数做 `r & 0xFFFF` 掩码截断；2.调用 `struct.pack('>{N}H', ...)` 打包。副作用：无。
    """
    # 为什么要用 `r & 0xFFFF` 按位与掩码：
    #   防止列表中混入负数或超过 65535 的整数导致 struct.pack('>H') 抛出范围溢出异常；
    #   格式串 `>{len}H` 中，`>` 表示标准网络大端（Big-Endian），`H` 表示 2 字节无符号短整型（uint16）
    return struct.pack(f">{len(registers)}H", *[r & 0xFFFF for r in registers])


def bytes_to_registers(raw: bytes) -> List[int]:
    """将二进制字节流按每 2 字节一组解包为 16 位无符号寄存器整数列表。
    参数：raw（待解包的字节流 bytes）。返回：List[int]，每个元素为 0~65535 的 16 位无符号整数。
    步骤：1.奇数长度末尾补 0x00；2.计算寄存器数 count = len // 2；3.调用 `struct.unpack('>{count}H', raw)`。副作用：无。
    """
    # 防御性对齐：struct.unpack('>H') 要求字节流长度必须是 2 的整数倍，奇数长度末尾补零字节对齐
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
    """从 16 位寄存器整数列表中，按指定的数据类型和字节序变位模式解码还原出 Python 工程值。
    参数：registers（寄存器列表）、data_type（数据类型）、mode（字节序）、string_length（字符长度）。返回：解码后的 Python 值。
    步骤：1.处理空列表与 BOOL；2.转原始字节流；3.调用 transform_bytes 重排；4.调用 struct.unpack 解出数值。副作用：无。
    """
    # 空列表防御：未读到任何寄存器时安全返回 0，防止索引越界
    if not registers:
        return 0

    # BOOL 类型直接取首个寄存器的最低位 bit0（`& 1` 位与运算），无需走字节流转换
    if data_type == ModbusDataType.BOOL:
        return bool(registers[0] & 1)

    # 1. 转为原始字节流
    # 先把所有 16 位寄存器按大端平铺成连续 bytes，再按需切片并重排字节序
    raw_bytes = registers_to_bytes(registers)

    # INT16（有符号 16 位整数）：
    # struct 格式 `>h` 表示大端 signed short（2 字节），自动将 0x8000~0xFFFF 按二进制补码还原为负数 -32768~-1
    if data_type == ModbusDataType.INT16:
        # 截取前 2 个字节（1 个寄存器）并根据模式（如 BADC）决定是否交换高低字节
        adjusted = transform_bytes(raw_bytes[:2], mode)
        # struct.unpack 返回单元素元组，取 [0] 即为还原后的 Python int
        return struct.unpack(">h", adjusted)[0]

    # UINT16（无符号 16 位整数）：
    # struct 格式 `>H` 表示大端 unsigned short（2 字节），0x8000~0xFFFF 解释为正整数 32768~65535
    elif data_type == ModbusDataType.UINT16:
        # 截取前 2 字节并应用字节内交换（若为 BADC/DCBA）
        adjusted = transform_bytes(raw_bytes[:2], mode)
        return struct.unpack(">H", adjusted)[0]

    # HEX16（16 位十六进制展示）：
    # 解出无符号整数后格式化为固定 4 位大写十六进制（如 0x02EE），不足 4 位左侧补 0
    elif data_type == ModbusDataType.HEX16:
        adjusted = transform_bytes(raw_bytes[:2], mode)
        val = struct.unpack(">H", adjusted)[0]
        # `:04X` 表示宽度固定为 4 位、左侧补 0、使用大写字母 A~F
        return f"0x{val:04X}"

    # BINARY16（16 位二进制位串展示）：
    # 先格式化为 16 位 0/1 字符串，再每 4 个 bit 插入一个空格分隔，方便工程师逐位核对 BMS/PCS 故障位
    elif data_type == ModbusDataType.BINARY16:
        adjusted = transform_bytes(raw_bytes[:2], mode)
        val = struct.unpack(">H", adjusted)[0]
        # `:016b` 将整数格式化为满 16 位的二进制位串（bit15 在最左，bit0 在最右）
        b = f"{val:016b}"
        # 按半字节（Nibble，4 bits）分组拼接空格，提高人工读图效率
        return f"{b[0:4]} {b[4:8]} {b[8:12]} {b[12:16]}"

    # INT32（有符号 32 位整数，占 4 字节）：
    # 若传入寄存器不足 2 个，先用 ljust 在右侧补零至 4 字节防解包报错，再用 `>i`（signed int）解包
    elif data_type == ModbusDataType.INT32:
        if len(raw_bytes) < 4:
            # 长度不足 4 字节时右侧补零，避免 struct.unpack('>i') 抛异常
            raw_bytes = raw_bytes.ljust(4, b"\x00")
        # 将 4 字节从设备变位顺序（如 CDAB）还原回标准大端 ABCD
        adjusted = transform_bytes(raw_bytes[:4], mode)
        return struct.unpack(">i", adjusted)[0]

    # UINT32（无符号 32 位整数，占 4 字节）：
    # 使用 `>I`（unsigned int）解包为 0 ~ 4294967295
    elif data_type == ModbusDataType.UINT32:
        if len(raw_bytes) < 4:
            raw_bytes = raw_bytes.ljust(4, b"\x00")
        adjusted = transform_bytes(raw_bytes[:4], mode)
        return struct.unpack(">I", adjusted)[0]

    # FLOAT32（IEEE 754 单精度浮点数，占 4 字节）：使用 `>f` 解包
    # 为什么要检查 math.isnan 并 round(..., 6)：
    #   1) 当两个寄存器全为 0xFFFF（现场通信未就绪默认值）时，IEEE 754 解出来是 NaN，转字符串 "NaN" 防 UI 报错；
    #   2) 单精度浮点只有约 7 位有效十进制数字，round(f_val, 6) 可消除二进制浮点如 220.500003814 的尾数毛刺
    elif data_type == ModbusDataType.FLOAT32:
        if len(raw_bytes) < 4:
            raw_bytes = raw_bytes.ljust(4, b"\x00")
        # 先按 ABCD/CDAB/BADC/DCBA 重排 4 字节，再按 IEEE 754 标准大端解包
        adjusted = transform_bytes(raw_bytes[:4], mode)
        f_val = struct.unpack(">f", adjusted)[0]
        # 拦截非数字（Not a Number），避免传入 UI 数值计算逻辑时报错
        if math.isnan(f_val):
            return "NaN"
        # 保留 6 位小数，滤除 float32 转 float64 时的精度尾数噪声
        return round(f_val, 6)

    # HEX32（32 位十六进制字符串）：格式化为 0x 加 8 位大写十六进制数字
    elif data_type == ModbusDataType.HEX32:
        if len(raw_bytes) < 4:
            raw_bytes = raw_bytes.ljust(4, b"\x00")
        adjusted = transform_bytes(raw_bytes[:4], mode)
        val = struct.unpack(">I", adjusted)[0]
        return f"0x{val:08X}"

    # INT64（有符号 64 位长整数，占 8 字节）：使用 `>q`（signed long long）解包
    elif data_type == ModbusDataType.INT64:
        if len(raw_bytes) < 8:
            # 不足 4 个寄存器（8 字节）时右侧补零对齐
            raw_bytes = raw_bytes.ljust(8, b"\x00")
        adjusted = transform_bytes(raw_bytes[:8], mode)
        return struct.unpack(">q", adjusted)[0]

    # UINT64（无符号 64 位长整数，占 8 字节）：使用 `>Q`（unsigned long long）解包
    elif data_type == ModbusDataType.UINT64:
        if len(raw_bytes) < 8:
            raw_bytes = raw_bytes.ljust(8, b"\x00")
        adjusted = transform_bytes(raw_bytes[:8], mode)
        return struct.unpack(">Q", adjusted)[0]

    # DOUBLE64（IEEE 754 双精度浮点数，占 8 字节）：使用 `>d`（double）解包并保留 8 位小数
    elif data_type in (ModbusDataType.DOUBLE64, "FLOAT64"):
        if len(raw_bytes) < 8:
            raw_bytes = raw_bytes.ljust(8, b"\x00")
        adjusted = transform_bytes(raw_bytes[:8], mode)
        d_val = struct.unpack(">d", adjusted)[0]
        if math.isnan(d_val):
            return "NaN"
        return round(d_val, 8)

    # STRING（ASCII 文本字符串）：忽略非 ASCII 坏字节并剥离尾部填充的 `\x00` 空字符
    elif data_type == ModbusDataType.STRING:
        # 字符串可以截断到请求长度，并去除末尾 null
        clean = raw_bytes.decode("ascii", errors="replace").rstrip("\x00")
        return clean

    # 未知类型兜底直接返回首个寄存器的原始整数值
    return registers[0]


def encode_value(
    value: Any,
    data_type: ModbusDataType,
    mode: ByteOrderMode = ByteOrderMode.ABCD,
    string_length: int = 4,
) -> List[int]:
    """将 Python 工程值按照指定的数据类型和字节序变位模式编码为 16 位无符号寄存器整数列表。
    参数：value（待编码工程值）、data_type（数据类型）、mode（字节序）、string_length（字符长度）。返回：List[int] 寄存器列表。
    步骤：1.调用 clamp_value_to_type 钳位；2.调用 struct.pack 打包；3.调用 transform_bytes 重排；4.拆分为 16 位寄存器列表。副作用：无。
    """
    # 强关联数据类型安全钳位，杜绝无符号类型负数溢出或数值爆大
    value = clamp_value_to_type(value, data_type)

    # BOOL 编码：支持传入 "1"/"0"、1/0、True/False，统一编码为单寄存器 [1] 或 [0]
    if data_type == ModbusDataType.BOOL:
        b_val = bool(int(value)) if str(value).isdigit() else bool(value)
        return [1 if b_val else 0]

    # INT16 编码：
    # `>h` 将 -32768~32767 自动打包为 2 字节大端二进制补码，随后经 transform_bytes 重排并转为单元素寄存器列表
    if data_type == ModbusDataType.INT16:
        val = int(value)
        # 先打包为 2 字节标准大端 bytes
        raw = struct.pack(">h", val)
        # 再按目标字节序（如 BADC）重排字节
        adjusted = transform_bytes(raw, mode)
        # 最后拆包为 16 位无符号寄存器整数列表供 PyModbus 存储
        return bytes_to_registers(adjusted)

    # UINT16 编码：
    # `val & 0xFFFF` 双重保险限制在 16 位无符号区间，用 `>H` 打包后按字节序转换
    elif data_type == ModbusDataType.UINT16:
        val = int(value)
        raw = struct.pack(">H", val & 0xFFFF)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # HEX16 编码：
    # 剥离可选的 "0x" 前缀后按 16 进制基数（base=16）解析为整数，再打包为 1 个寄存器
    elif data_type == ModbusDataType.HEX16:
        str_val = str(value).strip().lower()
        # 若用户输入了 "0x1A2B"，去掉前两个字符 "0x"
        if str_val.startswith("0x"):
            str_val = str_val[2:]
        # 空串兜底按 "0" 解析，并掩码截断在 16 位以内
        val = int(str_val or "0", 16) & 0xFFFF
        raw = struct.pack(">H", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # BINARY16 编码：
    # 剔除为美观添加的空格后按 2 进制基数（base=2）解析为 16 位整数
    elif data_type == ModbusDataType.BINARY16:
        # 去除 "0001 0010" 中间的分隔空格
        str_val = str(value).replace(" ", "").strip()
        val = int(str_val or "0", 2) & 0xFFFF
        raw = struct.pack(">H", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # INT32 编码：
    # `>i` 打包为 4 字节大端补码，再按 ABCD/CDAB/BADC/DCBA 变位后拆成 2 个 16 位寄存器
    elif data_type == ModbusDataType.INT32:
        val = int(value)
        raw = struct.pack(">i", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # UINT32 编码：
    # `>I` 打包为 4 字节无符号整数（掩码 0xFFFFFFFF），拆分为 2 个 16 位寄存器
    elif data_type == ModbusDataType.UINT32:
        val = int(value)
        raw = struct.pack(">I", val & 0xFFFFFFFF)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # FLOAT32 编码：
    # `>f` 按 IEEE 754 标准将 float 转为 4 字节（1位符号+8位指数+23位尾数），再按字节序重排为 2 个寄存器
    elif data_type == ModbusDataType.FLOAT32:
        val = float(value)
        # 例如 752.4 经 `>f` 打包为 4 字节，若模式为 CDAB 则高低字互换后再生成两个 16 位寄存器值
        raw = struct.pack(">f", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # HEX32 编码：
    # 解析 8 位十六进制字符串（掩码 0xFFFFFFFF）并拆分为 2 个 16 位寄存器
    elif data_type == ModbusDataType.HEX32:
        str_val = str(value).strip().lower()
        if str_val.startswith("0x"):
            str_val = str_val[2:]
        val = int(str_val or "0", 16) & 0xFFFFFFFF
        raw = struct.pack(">I", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # INT64 编码：
    # `>q` 打包为 8 字节有符号长整型，经 8 字节变位后拆分为 4 个 16 位寄存器
    elif data_type == ModbusDataType.INT64:
        val = int(value)
        raw = struct.pack(">q", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # UINT64 编码：
    # `>Q` 打包为 8 字节无符号长整型（掩码 64 位全 1），拆分为 4 个 16 位寄存器
    elif data_type == ModbusDataType.UINT64:
        val = int(value)
        raw = struct.pack(">Q", val & 0xFFFFFFFFFFFFFFFF)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # DOUBLE64 编码：
    # `>d` 打包为 8 字节 IEEE 754 双精度浮点数，拆分为 4 个 16 位寄存器
    elif data_type in (ModbusDataType.DOUBLE64, "FLOAT64"):
        val = float(value)
        raw = struct.pack(">d", val)
        adjusted = transform_bytes(raw, mode)
        return bytes_to_registers(adjusted)

    # STRING 编码：
    # 转为 ASCII 字节流后向上对齐到偶数长度（因 1 个寄存器装 2 个字节），不足部分右侧补 `\x00`
    elif data_type == ModbusDataType.STRING:
        # 非 ASCII 字符使用 '?' 替换，防止中文误填入 STRING 寄存器导致 UnicodeEncodeError
        s_bytes = str(value).encode("ascii", errors="replace")
        # 取实际字节长度与预设最小长度中的较大者
        target_len = max(len(s_bytes), string_length)
        # 奇数个字节时加 1 凑成偶数字节，以便完整装入 16 位寄存器
        if target_len % 2 != 0:
            target_len += 1
        # 右侧补零字节（C 风格字符串结尾 '\0'）
        s_bytes = s_bytes.ljust(target_len, b"\x00")
        return bytes_to_registers(s_bytes)

    # 未知类型兜底返回单寄存器 [0]
    return [0]
