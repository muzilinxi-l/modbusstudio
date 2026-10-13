"""models.py — Modbus Studio 2.0 领域数据模型与值对象定义（Domain Models）。
关键导入：dataclasses（实体自动构造与默认值工厂）、enum（强类型协议枚举）、typing（静态类型契约）、time（轮询时间戳）。
对外暴露：CommType/AreaType/ByteOrder/DataType/SimRule 五大枚举，ConnectionConfig/ModbusPoint/SlaveDevice/PollTask/PollResult/LogEntry 六大实体。
"""

# 启用 PEP 563 延迟类型注解求值：允许在类内部的方法签名（如 from_str、from_dict）中直接返回当前类名本身
# 为什么领域模型层（models.py）严禁导入 tkinter 或 pymodbus：
#   遵循 Clean Architecture 依赖内指原则，领域模型处于最内圈，任何外部 UI 框架或通信库的变更都不应波及数据契约。
#   本文件所有类均为纯 Python 数据载体，可在主线程、仿真线程、asyncio 协程间安全传递。
from __future__ import annotations
# 导入 dataclass 与 field：免去手写样板 __init__，并用 default_factory 为每个实例创建独立的字典/列表内存
from dataclasses import dataclass, field
# 导入 Enum：将协议魔术字符串收敛为强类型枚举，同时继承 str 以直接兼容 JSON 序列化与 Tkinter 控件绑定
from enum import Enum
# 导入 typing 类型构造器：为点表复合键 Dict[Tuple[str, int], ModbusPoint] 等复杂结构提供精确的静态类型提示
from typing import Any, Dict, List, Optional, Tuple, Union
# 导入 time：用于 PollResult 默认记录轮询结果产生的精确 Unix 纪元秒数
import logging
import time

logger = logging.getLogger("ModbusStudioModels")


def _parse_port(val: Any, default: int = 502) -> int:
    """将输入安全转换为合法的 TCP 端口号 (1~65535)，遇到非数字、越界或异常时记录警告并回退到 default。"""
    if val is None:
        return default
    s = str(val).strip()
    try:
        p = int(s)
        if 1 <= p <= 65535:
            return p
        logger.warning(f"端口数值越界 ({s})，回退默认端口 {default}")
        return default
    except (ValueError, TypeError):
        logger.warning(f"无法解析端口 ({s})，回退默认端口 {default}")
        return default


# =============================================================================
# 1. 通信物理层枚举（CommType）
# 为什么同时继承 str 和 Enum：
#   继承 str 后，CommType.TCP == "TCP" 直接成立，写入 JSON 或传给 UI 下拉框时无需处处调用 .value
# =============================================================================
class CommType(str, Enum):
    """通信物理层类型枚举：区分以太网 Modbus TCP 与串行总线 RS232/RS485 Modbus RTU。
    成员变量：TCP（以太网套接字通信）、RTU（串行端口二进制帧通信）。
    使用场景：在 ConnectionConfig 中标识端点传输介质，决定底层创建 TCP Server 还是 Serial Server。
    """
    # 以太网 TCP/IP 协议传输模式（报文带 7 字节 MBAP 头部，依赖 TCP 校验和，无 CRC16 尾部）
    TCP = "TCP"
    # 串行总线 RTU 二进制紧凑帧传输模式（无 MBAP 头，依赖 3.5 字符静默间隔分帧，尾部带 2 字节 CRC16）
    RTU = "RTU"

    @classmethod
    def from_str(cls, val: str) -> CommType:
        """将任意中英文字符串宽松解析为标准的 CommType 枚举值。
        参数：cls（CommType 类）、val（待解析字符串或任意对象）。返回：CommType（匹配 RTU/串口返回 RTU，其余返回 TCP）。
        步骤：1.输入转大写字符串；2.检查是否含 RTU 或中文'串口'；3.兜底返回 TCP。副作用：无。
        """
        # 为什么兼容中文“串口”与大小写“rtu”：
        #   UI 界面下拉框可能传入带中文描述的值，外部 JSON 导入可能传入小写 "rtu"
        if "RTU" in str(val).upper() or "串口" in str(val):
            return cls.RTU
        # 默认回退到工业联调最常用的 TCP 模式，防止未知配置导致启动崩溃
        return cls.TCP


# -----------------------------------------------------------------------------
# 系统资源配额与容量上限常量 (System Resource Quotas)
# 为什么设定 100 台从机上限：
#   1) 防止用户误操作在批量导入时一次性创建上千个从机导致内存膨胀与 Tkinter 列表渲染卡顿；
#   2) 在同端口多站点（Server Hub）架构下，100 台从机足以覆盖大型储能电站（EMS + 多台 PCS/BMS）全站联调。
# -----------------------------------------------------------------------------
MAX_TCP_SLAVE_INSTANCES: int = 100
"""以太网 Modbus TCP 从机服务实例最大承载数量上限"""

MAX_RTU_SLAVE_INSTANCES: int = 100
"""串行总线 Modbus RTU 从机服务实例最大承载数量上限"""


# =============================================================================
# 2. Modbus 四大标准存储区域枚举（AreaType）
# 为什么按 4x / 3x / 0x / 1x 命名：
#   遵循 Modicon PLC 传统前缀约定：0x=线圈(可读可写位)、1x=离散输入(只读位)、3x=输入寄存器(只读字)、4x=保持寄存器(可读可写字)
# =============================================================================
class AreaType(str, Enum):
    """Modbus 四大标准寄存器/线圈存储区域类型枚举。
    成员变量：HOLDING（4x 保持寄存器）、INPUT（3x 输入寄存器）、COIL（0x 线圈）、DISCRETE（1x 离散输入）。
    使用场景：定义点位所属的数据块，决定底层使用功能码 FC01~04 读取以及是否允许 FC05/06/15/16 写入。
    """
    # 4x 保持寄存器（16-bit 字，支持 FC03 读、FC06/FC16 写，常用于储能 PCS 有功/无功功率设定与运行参数）
    HOLDING = "4x_HoldingRegister"
    # 3x 输入寄存器（16-bit 字，仅支持 FC04 只读，常用于 BMS 电芯电压/温度等只读遥测采样）
    INPUT = "3x_InputRegister"
    # 0x 线圈状态（1-bit 位，支持 FC01 读、FC05/FC15 写，常用于开关合闸/分闸遥控命令）
    COIL = "0x_Coil"
    # 1x 离散量输入（1-bit 位，仅支持 FC02 只读，常用于故障告警位、门禁开关状态等只读遥信）
    DISCRETE = "1x_DiscreteInput"

    @property
    def friendly_name(self) -> str:
        """获取适合在 UI 界面表格与下拉框中展示的中文友好名称。
        参数：self（AreaType 实例）。返回：str，带中文说明与传统地址前缀的展示字符串。
        步骤：1.构造枚举到中文名的映射字典；2.查找当前实例对应名称；3.未命中回退原始 value。副作用：无。
        """
        # 将内部枚举标识转换为现场工程师熟悉的“中文名 (前缀)”展示格式
        mapping = {
            # 保持寄存器对应功能码 03/06/16，工业文档常写作 4x
            AreaType.HOLDING: "保持寄存器 (4x)",
            # 输入寄存器对应功能码 04，工业文档常写作 3x
            AreaType.INPUT: "输入寄存器 (3x)",
            # 线圈对应功能码 01/05/15，工业文档常写作 0x
            AreaType.COIL: "线圈 (0x)",
            # 离散输入对应功能码 02，工业文档常写作 1x
            AreaType.DISCRETE: "离散输入 (1x)",
        }
        # 使用 dict.get 防御性取值，确保未来扩展枚举成员时也不会抛 KeyError
        return mapping.get(self, str(self.value))

    @classmethod
    def normalize(cls, val: Any) -> AreaType:
        """将各种来源（枚举、中文名、功能码前缀、数据库字段）的区域标识归一化为标准 AreaType。
        参数：cls（AreaType 类）、val（任意类型的区域描述）。返回：AreaType 标准枚举值（兜底 HOLDING）。
        步骤：1.已是 AreaType 直接返回；2.剥离类名前缀转大写；3.按 DISCRETE->COIL->HOLDING->INPUT 顺序匹配。副作用：无。
        """
        # 快速路径：如果调用方传入的本来就是 AreaType 实例，直接原样返回，省去字符串开销
        if isinstance(val, AreaType):
            return val
        # 兼容其它枚举类型：若带有 .value 属性则先解包取出原始值
        if hasattr(val, "value"):
            val = val.value
        # 统一转为去首尾空白的大写字符串，消除大小写与前后空格差异
        s = str(val).strip().upper()
        # 若字符串形如 "AREATYPE.HOLDING"，按点号分割取最后一段名称
        if "." in s:
            s = s.split(".")[-1]
        # 必须优先判定离散输入与线圈，防止被通用的"输入/INPUT"误匹配
        # 为什么这里的 if 顺序绝对不能颠倒：
        #   DISCRETE 的枚举值是 "1x_DiscreteInput"（含 INPUT），中文是“离散输入”（含“输入”）；
        #   若把 INPUT 分支放在前面，所有离散输入都会被误判为 3x 输入寄存器！
        if "DISCRETE" in s or "离散" in s or "1X" in s or s.startswith("1"):
            return cls.DISCRETE
        # 其次判定 0x 线圈区域（匹配 COIL、中文“线圈”或 0X/0 前缀）
        if "COIL" in s or "线圈" in s or "0X" in s or s.startswith("0"):
            return cls.COIL
        # 再次判定 4x 保持寄存器区域（匹配 HOLDING、中文“保持”或 4X/4 前缀）
        if "HOLDING" in s or "保持" in s or "4X" in s or s.startswith("4"):
            return cls.HOLDING
        # 最后判定 3x 输入寄存器区域（此时已排除 1x 离散输入，匹配 INPUT 或“输入”绝对安全）
        if "INPUT" in s or "输入" in s or "3X" in s or s.startswith("3"):
            return cls.INPUT
        # 兜底策略：工业现场绝大多数点位为 4x 保持寄存器，未知标识默认回退到 HOLDING
        return cls.HOLDING


# =============================================================================
# 3. 多寄存器字节序与字序变位枚举（ByteOrder）
# 为什么 Modbus 会有 4 种字节序：
#   Modbus 协议规范只规定单个 16 位寄存器内部按大端（高字节在前 A, B）传输，
#   但当拼接 2 个寄存器表示 32 位浮点数/整数时，不同厂家（英博、华为、阳光电源、欧姆龙）
#   对高低字顺序的定义不同，衍生出 ABCD、CDAB、BADC、DCBA 四种排列。
# =============================================================================
class ByteOrder(str, Enum):
    """32/64位多寄存器字节序与字序变位模式枚举。
    成员变量：ABCD（标准大端）、CDAB（大端字交换，国内逆变器/PCS 最常用）、BADC（字节内交换）、DCBA（纯小端）。
    使用场景：在 ModbusPoint 与 PollTask 中指定跨寄存器数据的编解码字节重排规则。
    """
    # 标准大端（Big-Endian）：高字在前，字内高字节在前 [A, B, C, D]
    ABCD = "ABCD"  # Big-Endian
    # 字交换（Word-Swapped）：低字在前，字内仍保持高字节在前 [C, D, A, B]
    CDAB = "CDAB"  # Word-Swapped (工业及部分逆变器最常用)
    # 字节交换（Byte-Swapped）：高字在前，但每个 16 位寄存器内部高低字节互换 [B, A, D, C]
    BADC = "BADC"  # Byte-Swapped
    # 纯小端（Little-Endian）：低字在前，且字内低字节在前，4 字节完全逆序 [D, C, B, A]
    DCBA = "DCBA"  # Little-Endian

    @classmethod
    def normalize(cls, val: Any) -> ByteOrder:
        """将外部输入的字节序字符串或枚举对象归一化为标准 ByteOrder 枚举。
        参数：cls（ByteOrder 类）、val（任意字节序标识）。返回：ByteOrder 枚举实例（兜底 ABCD）。
        步骤：1.已是 ByteOrder 直接返回；2.转大写剥离前缀；3.精确匹配枚举；4.子串匹配；5.兜底返回 ABCD。副作用：无。
        """
        # 快速路径：若已是 ByteOrder 实例则直接返回
        if isinstance(val, ByteOrder):
            return val
        # 解包其它枚举类型的 .value（例如 modbus_codec.ByteOrderMode）
        if hasattr(val, "value"):
            val = val.value
        # 转大写并消除首尾空格
        s = str(val).strip().upper()
        # 去除形如 "BYTEORDER.CDAB" 的类名前缀
        if "." in s:
            s = s.split(".")[-1]
        # 第一轮：尝试精确匹配枚举的 value 或 name
        for member in cls:
            if member.value == s or member.name == s:
                return member
        # 第二轮：子串包含匹配（如 UI 下拉框文案 "CDAB (字交换)" 中包含 "CDAB"）
        for order in ("CDAB", "ABCD", "BADC", "DCBA"):
            if order in s:
                return cls(order)
        # 无法识别时回退到 Modbus 标准大端 ABCD
        return cls.ABCD


# =============================================================================
# 4. 寄存器解析数据类型枚举（DataType）
# =============================================================================
class DataType(str, Enum):
    """寄存器承载的工程解析数据类型枚举。
    成员变量：INT16/UINT16（16位整型）、INT32/UINT32（32位整型）、FLOAT32/FLOAT64（浮点型）、BOOL（布尔）、STRING（字符串）、HEX16（十六进制）。
    使用场景：决定单个点位占用几个连续的 16 位寄存器，以及如何将寄存器字数组还原为工程物理量。
    """
    # 16 位有符号整数（占 1 个寄存器，范围 -32768 ~ 32767，负数采用二进制补码表示）
    INT16 = "INT16"
    # 16 位无符号整数（占 1 个寄存器，范围 0 ~ 65535）
    UINT16 = "UINT16"
    # 32 位有符号整数（占 2 个连续寄存器，范围 -2147483648 ~ 2147483647）
    INT32 = "INT32"
    # 32 位无符号整数（占 2 个连续寄存器，范围 0 ~ 4294967295，常用于累计充电量/放电量）
    UINT32 = "UINT32"
    # 32 位 IEEE 754 单精度浮点数（占 2 个连续寄存器，工业遥测最常用浮点格式）
    FLOAT32 = "FLOAT32"
    # 64 位 IEEE 754 双精度浮点数（占 4 个连续寄存器，用于高精度电表总电能累计）
    FLOAT64 = "FLOAT64"
    # 1 位布尔开关量（在线圈区占 1 个位，在寄存器区占最低 bit0）
    BOOL = "BOOL"
    # ASCII 文本字符串（每 1 个 16 位寄存器存放 2 个 ASCII 字符，默认 8 个寄存器即 16 字符，如设备 SN 序列号）
    STRING = "STRING"
    # 16 位原始十六进制展示格式（占 1 个寄存器，形如 0x1A2B，适合观察按位打包的状态字/故障字）
    HEX16 = "HEX16"

    @property
    def register_count(self) -> int:
        """计算当前数据类型默认占用的 16 位 Modbus 寄存器个数。
        参数：self（DataType 实例）。返回：int，占用的连续 16 位寄存器数量（1、2、4 或 8）。
        步骤：1.16位及布尔返回 1；2.32位整型与单精度浮点返回 2；3.64位双精度返回 4；4.字符串默认返回 8。副作用：无。
        """
        # 16 位宽类型与布尔类型：只占用 1 个 16-bit 寄存器（2 字节）
        if self in (DataType.INT16, DataType.UINT16, DataType.HEX16, DataType.BOOL):
            return 1
        # 32 位宽类型：32 bits / 16 bits = 占用 2 个连续寄存器（4 字节）
        elif self in (DataType.INT32, DataType.UINT32, DataType.FLOAT32):
            return 2
        # 64 位宽类型：64 bits / 16 bits = 占用 4 个连续寄存器（8 字节）
        elif self in (DataType.FLOAT64,):
            return 4
        # 字符串类型：默认分配 8 个寄存器（可容纳 16 个 ASCII 字节）
        elif self == DataType.STRING:
            return 8  # 默认 8 个寄存器 (16 字符)
        return 1

    @classmethod
    def normalize(cls, val: Any) -> DataType:
        """将 C 语言风格类型名（如 unsigned short、float、dword）或枚举字符串归一化为 DataType。
        参数：cls（DataType 类）、val（任意类型描述）。返回：DataType 标准枚举实例（兜底 UINT16）。
        步骤：1.若是 DataType 直接返回；2.剥离前缀转大写；3.精确匹配枚举；4.按位宽从高到低匹配 C 类型别名。副作用：无。
        """
        # 快速路径：已是 DataType 枚举直接返回
        if isinstance(val, DataType):
            return val
        # 解包其它枚举对象的 .value（如 modbus_codec.ModbusDataType）
        if hasattr(val, "value"):
            val = val.value
        s = str(val).strip().upper()
        if "." in s:
            s = s.split(".")[-1]
        # 优先精确匹配标准枚举名（如 "FLOAT32"、"INT16"）
        for member in cls:
            if member.value == s or member.name == s:
                return member
        # 为什么模糊匹配时必须先判断 FLOAT64 再判断 FLOAT，先判断 UINT16/UINT32 再判断 INT16/INT32：
        #   因为 "FLOAT64" 包含子串 "FLOAT"，"UINT16" 包含子串 "INT16"，"UNSIGNED SHORT" 包含 "SHORT"；
        #   必须先匹配带 UNSIGNED / 64 的长特征串，否则无符号类型会被有符号分支提前截胡！
        # 匹配 64 位双精度浮点（C 语言 double）
        if "FLOAT64" in s or "DOUBLE" in s:
            return cls.FLOAT64
        # 匹配 32 位单精度浮点（C 语言 float）
        if "FLOAT" in s or "FLOAT32" in s:
            return cls.FLOAT32
        # 匹配 32 位无符号整数（C 语言 unsigned int / Windows DWORD）
        if "UINT32" in s or "DWORD" in s:
            return cls.UINT32
        # 匹配 32 位有符号整数（C 语言 signed int）
        if "INT32" in s:
            return cls.INT32
        # 匹配 16 位无符号短整数（C 语言 unsigned short / ushort）
        if "UINT16" in s or "USHORT" in s or "UNSIGNED SHORT" in s:
            return cls.UINT16
        # 匹配 16 位有符号短整数（C 语言 signed short / short）
        if "INT16" in s or "SHORT" in s or "SIGNED SHORT" in s:
            return cls.INT16
        # 匹配布尔/单比特位类型（兼容 UI 展示名 "BOOL(比特)" / "比特" / "Bit"）
        if "BOOL" in s or "BIT" in s or "比特" in s:
            return cls.BOOL
        # 匹配 ASCII 文本字符串类型
        if "STRING" in s or "STR" in s:
            return cls.STRING
        # 匹配 16 位十六进制掩码展示类型
        if "HEX" in s:
            return cls.HEX16
        # 兜底返回无符号 16 位整数
        return cls.UINT16


# =============================================================================
# 5. 从机动态仿真波形规则枚举（SimRule）
# =============================================================================
class SimRule(str, Enum):
    """从机数据变位动态模拟规则枚举：控制后台仿真线程每秒如何自动更新点位数值。
    成员变量：CONSTANT（固定值不变）、RANDOM（随机波动）、INCREMENT（步进递增）、SINE（正弦波）、RECT（方波）。
    使用场景：在从机运行期间，由 ModbusSlaveEngine._run_simulation_loop 按此规则动态生成实时测试波形。
    """
    # 静态固定值：不随时间自动变化，仅响应人工在 UI 修改或外部主机报文写入
    CONSTANT = "固定"
    # 随机波动：模拟现场传感器采样噪声（如电压在 220V 附近微幅跳动）
    RANDOM = "随机波动"
    # 步进递增：模拟电表累计电量或运行计时器单调递增
    INCREMENT = "步进递增"
    # 正弦波：模拟周期性平滑起伏的物理量（如日内光伏功率或交流波形包络）
    SINE = "正弦波"
    # 方波：模拟高低电平周期性切换的状态量或脉冲心跳信号
    RECT = "方波"

    @classmethod
    def normalize(cls, val: Any) -> str:
        """将任意输入规则名称归一化为标准的中文仿真规则字符串。
        参数：cls（SimRule 类）、val（枚举或字符串）。返回：str，对应的中文规则名称（兜底 '固定'）。
        步骤：1.若是 SimRule 直接取 .value；2.剥离枚举类名前缀；3.匹配枚举值或枚举名；4.兜底返回 '固定'。副作用：无。
        """
        if isinstance(val, SimRule):
            return val.value
        if hasattr(val, "value"):
            val = val.value
        s = str(val).strip()
        if "." in s:
            s = s.split(".")[-1]
        # 同时支持英文枚举名（如 "RANDOM"）与中文枚举值（如 "随机波动"）双向识别
        for member in cls:
            if member.value == s or member.name == s or member.value in s:
                return member.value
        return cls.CONSTANT.value


# =============================================================================
# 6. 通信连接配置值对象（ConnectionConfig）
# 为什么把 TCP 和 RTU 参数合在一个 dataclass 中：
#   让用户在 UI 界面来回切换 TCP / RTU 模式时，之前填好的 IP:Port 或串口波特率不会丢失。
# =============================================================================
@dataclass
class ConnectionConfig:
    """通信连接配置值对象（无论从机监听还是主机轮询连接，均采用同一配置结构）。
    成员变量：comm_type（TCP/RTU）、host/port（TCP 地址与端口）、com_port/baudrate/data_bits/parity/stop_bits（串口五元组）、unit_id（站点号 1~247）、timeout（超时秒数）。
    使用场景：挂载在 SlaveDevice 或 PollTask 下，描述物理端点与协议站号。
    """
    # 物理层传输类型：默认使用以太网 TCP
    comm_type: CommType = CommType.TCP
    # TCP 监听或目标主机 IP 地址：默认本地回环 127.0.0.1
    host: str = "127.0.0.1"
    # TCP 端口号：Modbus 标准端口为 502
    port: int = 502
    # 串口名称：Windows 下形如 COM1~COM256，Linux 一体机下形如 /dev/ttyS1 或 /dev/ttyUSB0
    com_port: str = "COM1"
    # 串口波特率：工业现场最常用 9600 或 115200 bps
    baudrate: int = 9600
    # 串口数据位：Modbus RTU 标准固定为 8 位二进制数据位
    data_bits: int = 8
    # 串口奇偶校验位：N=无校验(None), E=偶校验(Even), O=奇校验(Odd)
    parity: str = "N"          # N, E, O
    # 串口停止位：通常为 1 位或 2 位
    stop_bits: int = 1
    # Modbus 从机站点地址（Unit ID）：同一端口/总线上靠此 ID 区分不同从机实例（1~247）
    unit_id: int = 1           # Modbus 从机站号
    # 通信响应超时时间（单位：秒）
    timeout: float = 1.0

    @property
    def summary(self) -> str:
        """生成单行简明连接摘要字符串，用于从机下拉框与状态栏展示。
        参数：self（ConnectionConfig 实例）。返回：str，如 'TCP 127.0.0.1:502 (ID:1)'。
        步骤：1.判断 comm_type 是否为 TCP；2.按 TCP 或 RTU 格式拼接端点与站点号字符串并返回。副作用：无。
        """
        # TCP 模式展示 IP:Port 与从机站点号
        if self.comm_type == CommType.TCP:
            return f"TCP {self.host}:{self.port} (ID:{self.unit_id})"
        # RTU 模式展示工业标准串口简写（如 COM1@9600,8N1）与从机站点号
        return f"RTU {self.com_port}@{self.baudrate},{self.data_bits}{self.parity}{self.stop_bits} (ID:{self.unit_id})"

    def to_dict(self) -> Dict[str, Any]:
        """将当前连接配置序列化为纯 Python 字典，便于导出为 JSON 配置文件。
        参数：self（ConnectionConfig 实例）。返回：Dict[str, Any]，包含全部连接参数的键值字典。
        步骤：1.将 comm_type 枚举转为纯字符串 .value；2.其余基础类型字段原样装入字典返回。副作用：无。
        """
        # 将枚举字段显式拆解为基本字符串，确保 json.dumps 不会因自定义 Enum 类型报错
        return {
            # 通信类型字符串（"TCP" 或 "RTU"）
            "comm_type": self.comm_type.value,
            # 以太网 IP 地址与端口
            "host": self.host,
            "port": self.port,
            # 串口通信五元组参数
            "com_port": self.com_port,
            "baudrate": self.baudrate,
            "data_bits": self.data_bits,
            "parity": self.parity,
            "stop_bits": self.stop_bits,
            # 从机站点地址与请求超时时间
            "unit_id": self.unit_id,
            "timeout": self.timeout,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ConnectionConfig:
        """从字典反序列化还原 ConnectionConfig 实例，对缺失字段提供工业标准默认值。
        参数：cls（ConnectionConfig 类）、d（连接配置字典）。返回：ConnectionConfig 实例。
        步骤：1.通过 CommType.from_str 解析模式；2.对端口、波特率、站点号做显式类型强转；3.返回实例。副作用：无。
        """
        # 对每个字段做显式 int/str/float 转换，防止外部 JSON 中把数字写成字符串导致后续 socket 绑定报错
        return cls(
            # 解析通信介质，缺失时默认走 TCP
            comm_type=CommType.from_str(d.get("comm_type", "TCP")),
            # 强转主机地址与端口号（通过 _parse_port 防御非法输入与越界）
            host=str(d.get("host", "127.0.0.1")),
            port=_parse_port(d.get("port", 502), default=502),
            # 强转串口名称、波特率、数据位、校验位、停止位
            com_port=str(d.get("com_port", "COM1")),
            baudrate=int(d.get("baudrate", 9600)),
            data_bits=int(d.get("data_bits", 8)),
            parity=str(d.get("parity", "N")),
            stop_bits=int(d.get("stop_bits", 1)),
            # 强转从机站点号与超时秒数
            unit_id=int(d.get("unit_id", 1)),
            timeout=float(d.get("timeout", 1.0)),
        )


# =============================================================================
# 7. 单个 Modbus 点位实体模型（ModbusPoint）
# =============================================================================
@dataclass
class ModbusPoint:
    """单个 Modbus 寄存器/线圈点位实体模型，承载地址、数据类型、字节序、实时值与业务元数据。
    成员变量：address（起始地址）、description（中文描述）、area（存储区）、data_type（数据类型）、byte_order（字节序）、value（当前值）、sim_rule（仿真规则）、raw_hex（十六进制串）、string_length（字符长度）、field_name（业务变量名）、scale（缩放系数）、unit（单位）。
    使用场景：存储在 SlaveDevice.points 字典中，作为 UI 点表展示、引擎内存块读写与 HEMS 数据库配对的核心载体。
    """
    # 寄存器/线圈在当前存储区内的起始偏移地址（非负整数）
    address: int
    # 点位中文名称或业务功能说明
    description: str = ""
    # 所属 Modbus 存储区域（默认 4x 保持寄存器）
    area: AreaType = AreaType.HOLDING
    # 寄存器承载的数据类型（默认 UINT16）
    data_type: DataType = DataType.UINT16
    # 多寄存器字节序变位模式（默认标准大端 ABCD）
    byte_order: ByteOrder = ByteOrder.ABCD
    # 当前点位的工程值（随人工设置、主机写入或后台仿真更新）
    value: Any = 0
    # 动态仿真变位规则名称（默认“固定”不自动跳变）
    sim_rule: str = "固定"
    # 编码后的原始寄存器十六进制报文预览字符串（如 "0x02EE"）
    raw_hex: str = "0x0000"
    string_length: int = 16    # 当为 STRING 类型时指定的字符长度
    field_name: str = ""       # 业务数据库绑定的英文标识符
    scale: float = 1.0         # 缩放比例
    unit: str = ""             # 工程单位

    @property
    def register_count(self) -> int:
        """动态计算当前点位实际占用的连续 16 位寄存器数量。
        参数：self（ModbusPoint 实例）。返回：int，占用的 16 位寄存器个数（>= 1）。
        步骤：1.若为 STRING 类型，按每 2 个字符占 1 个寄存器向上取整；2.其余类型委托给 data_type.register_count。副作用：无。
        """
        if self.data_type == DataType.STRING:
            # 两个字符占一个 16-bit 寄存器
            # 为什么用 (string_length + 1) // 2：
            #   当字符长度为奇数（如 5 个字符）时，(5 + 1) // 2 = 3 个寄存器（6 字节），自动向上取整补齐偶数个字节
            return max(1, (self.string_length + 1) // 2)
        return self.data_type.register_count

    def to_dict(self) -> Dict[str, Any]:
        """将点位对象序列化为纯基础类型字典，用于点表 JSON 导入导出。
        参数：self（ModbusPoint 实例）。返回：Dict[str, Any]，点位属性字典。
        步骤：1.将 area/data_type/byte_order 三个枚举转为字符串值；2.打包其余标量字段返回。副作用：无。
        """
        # 导出点位全部属性，枚举全部转为 .value 字符串以保证 JSON 兼容性
        return {
            # 协议层基础定位与类型属性
            "address": self.address,
            "description": self.description,
            "area": self.area.value,
            "data_type": self.data_type.value,
            "byte_order": self.byte_order.value,
            # 运行期实时数值与仿真波形规则
            "value": self.value,
            "sim_rule": self.sim_rule,
            "raw_hex": self.raw_hex,
            "string_length": self.string_length,
            # 现场 HEMS 数据库关联元数据（变量名、缩放倍率、单位）
            "field_name": self.field_name,
            "scale": self.scale,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ModbusPoint:
        """从字典安全反序列化构造 ModbusPoint 实例，自动归一化各枚举与规则字段。
        参数：cls（ModbusPoint 类）、d（点位配置字典）。返回：ModbusPoint 实例。
        步骤：1.转换地址与描述；2.调用 AreaType/DataType/ByteOrder/SimRule 的 normalize 方法；3.返回实例。副作用：无。
        """
        # 使用各枚举类的 normalize 方法，兼容不同版本导出的 JSON 点表文件格式
        return cls(
            # 强转起始地址与描述文案
            address=int(d.get("address", 0)),
            description=str(d.get("description", "")),
            # 归一化存储区域、数据类型与字节序枚举
            area=AreaType.normalize(d.get("area", "4x_HoldingRegister")),
            data_type=DataType.normalize(d.get("data_type", "UINT16")),
            byte_order=ByteOrder.normalize(d.get("byte_order", "ABCD")),
            # 还原工程值、仿真规则与十六进制字符串
            value=d.get("value", 0),
            sim_rule=SimRule.normalize(d.get("sim_rule", "固定")),
            raw_hex=str(d.get("raw_hex", "0x0000")),
            # 还原字符串长度与 HEMS 业务扩展属性
            string_length=int(d.get("string_length", 16)),
            field_name=str(d.get("field_name", "")),
            scale=float(d.get("scale", 1.0)),
            unit=str(d.get("unit", "")),
        )


# =============================================================================
# 8. 从机实例聚合根（SlaveDevice）
# 为什么 points 字典用 (area_str, address) 二元组做主键而不只用 address：
#   在 Modbus 标准中，4x 保持寄存器的地址 1 与 3x 输入寄存器的地址 1 是两块完全独立的内存区，
#   用 (分区名, 地址) 复合键才能允许不同分区存在相同偏移地址而不互相覆盖。
# =============================================================================
@dataclass
class SlaveDevice:
    """Modbus 从机实例聚合根（Aggregate Root），统管单台从机的连接参数、点表集合与运行状态。
    成员变量：id（唯一标识）、name（显示名称）、conn_config（连接配置）、points（复合键点表字典）、is_running（是否运行中）、runtime_info（状态文本）、db_file/db_device_id/db_device_name（关联的 HEMS 数据库溯源信息）。
    使用场景：由 SlaveService 集中管理，一台 SlaveDevice 对应底层一个 ModbusSlaveEngine 实例。
    """
    # 从机实例在内存中的唯一业务 ID（如 "slave_1"）
    id: str
    # 从机在 UI 列表与日志中展示的名称（如 "[南向]英博PCS_1"）
    name: str
    # 该从机绑定的物理端点与站点号配置
    conn_config: ConnectionConfig
    # 使用 field(default_factory=dict) 确保每台从机拥有独立的点表字典，绝不跨实例共享可变默认字典
    points: Dict[Tuple[str, int], ModbusPoint] = field(default_factory=dict)
    # 标记当前从机是否已启动底层监听与仿真循环
    is_running: bool = False
    # 供 UI 状态栏展示的简短运行状态说明
    runtime_info: str = "已就绪"
    # 以下三个字段记录该从机是从哪个现场 .cdb 数据库的哪个设备导入的，用于“写回数据库”与南北向配对
    db_file: Optional[str] = None
    db_device_id: Optional[int] = None
    db_device_name: Optional[str] = None

    def add_point(self, point: ModbusPoint) -> None:
        """向当前从机点表中添加或覆盖一个点位。
        参数：self（SlaveDevice 实例）、point（待加入的 ModbusPoint 实体）。返回：None。
        步骤：1.提取 (point.area.value, point.address) 构造复合主键；2.存入 self.points 字典。副作用：修改 self.points。
        """
        # 以 (存储区字符串, 寄存器起始地址) 作为唯一复合键
        key = (point.area.value, point.address)
        self.points[key] = point

    def remove_point(self, area: Union[str, AreaType], address: int) -> Optional[ModbusPoint]:
        """从当前从机点表中按区域与地址移除指定点位。
        参数：self（SlaveDevice 实例）、area（区域枚举或字符串）、address（起始地址 int）。返回：被删点位或 None。
        步骤：1.将 area 归一化为标准枚举 value 字符串；2.调用 dict.pop 安全移除并返回。副作用：修改 self.points。
        """
        # 先归一化区域名称，保证传入 AreaType.HOLDING 或 "4x_HoldingRegister" 都能正确命中主键
        area_str = AreaType.normalize(area).value
        return self.points.pop((area_str, address), None)

    def get_point(self, area: Union[str, AreaType], address: int) -> Optional[ModbusPoint]:
        """按存储区域与寄存器地址 O(1) 快速查找点位对象。
        参数：self（SlaveDevice 实例）、area（区域枚举或字符串）、address（起始地址 int）。返回：点位对象或 None。
        步骤：1.将 area 归一化为标准枚举 value 字符串；2.调用 dict.get 查询复合键。副作用：无。
        """
        area_str = AreaType.normalize(area).value
        return self.points.get((area_str, address))

    def clear_points(self) -> None:
        """清空当前从机下的所有点位数据。
        参数：self（SlaveDevice 实例）。返回：None。
        步骤：1.调用 self.points.clear() 原地清空点表字典。副作用：self.points 变为空字典。
        """
        self.points.clear()


# =============================================================================
# 9. 主机轮询任务、轮询结果与日志条目模型（PollTask / PollResult / LogEntry）
# =============================================================================
@dataclass
class PollTask:
    """主机主动轮询任务配置模型，封装单次或周期性读取请求的参数。
    成员变量：conn_config（目标从机连接配置）、area（读取区域）、start_address（起始地址）、count（读取数量）、byte_order（解码字节序）、is_loop（是否循环轮询）、interval_ms（轮询周期毫秒数）。
    使用场景：由 PollPanel 组装并传给 PollService，驱动底层客户端发起 Modbus 读取请求。
    """
    # 目标从机的网络或串口连接参数
    conn_config: ConnectionConfig
    # 要读取的存储区域（默认 4x 保持寄存器）
    area: AreaType = AreaType.HOLDING
    # 批量读取的起始寄存器地址
    start_address: int = 1
    # 连续读取的寄存器/线圈数量（Modbus 单帧上限通常为 125 个寄存器或 2000 个线圈）
    count: int = 10
    # 读取回来后按哪种字节序解码预览
    byte_order: ByteOrder = ByteOrder.ABCD
    # 是否开启后台定时循环轮询
    is_loop: bool = False
    # 循环轮询的间隔毫秒数（默认 1000ms = 1秒一次）
    interval_ms: int = 1000


@dataclass
class PollResult:
    """主机单次读取/轮询返回结果数据实体，供 UI 轮询面板渲染表格与统计耗时。
    成员变量：success（是否成功）、timestamp（完成时间戳）、area（区域）、start_address（起始地址）、count（数量）、raw_registers（16位整数数组）、raw_frame_hex（原始报文十六进制）、error_msg（失败原因）、elapsed_ms（往返耗时毫秒）。
    使用场景：由 PollService 在每次读取完成后生成，通过回调推给 PollPanel 更新界面。
    """
    # 本次读取是否成功拿到从机响应
    success: bool
    # 响应生成时的系统时间戳（每次实例化时动态调用 time.time 获取）
    timestamp: float = field(default_factory=time.time)
    # 本次读取对应的存储区、起始地址与数量
    area: AreaType = AreaType.HOLDING
    start_address: int = 0
    count: int = 0
    # 从机返回的原始 16 位无符号寄存器值列表（失败时为空列表）
    raw_registers: List[int] = field(default_factory=list)
    # 供报文分析使用的原始十六进制帧字符串
    raw_frame_hex: str = ""
    # 当 success=False 时记录具体超时或异常码说明
    error_msg: str = ""
    # 本次请求从发出到收到响应的往返耗时（毫秒）
    elapsed_ms: float = 0.0


@dataclass
class LogEntry:
    """通信报文与系统审计日志条目实体，用于双缓冲日志节流池投递。
    成员变量：timestamp（格式化时间字符串）、direction（方向：SYS/RX/TX/ERROR）、slave_id（从机站点号）、message（日志正文）、raw_frame_hex（可选原始十六进制帧）。
    使用场景：由 LoggingService.post 创建并缓冲，供 MainWindow 底部日志视图批量拉取渲染。
    """
    # 精确到毫秒的本地时间字符串（如 "14:20:05.123"）
    timestamp: str
    direction: str       # "SYS", "RX", "TX", "ERROR"
    # 关联的从机站点号（系统级日志固定传 0）
    slave_id: int
    # 日志人类可读正文描述
    message: str
    # 可选的底层原始报文十六进制字节串
    raw_frame_hex: str = ""

    def format_line(self) -> str:
        """将当前日志条目格式化为带换行符的单行人类可读文本，供 UI 日志框插入。
        参数：self（LogEntry 实例）。返回：str，末尾带 `\\n` 的格式化日志行。
        步骤：1.若为 SYS 标注 [系统]；2.若为 ERROR 标注 [错误]；3.若为 TX/RX 附带从机站号与方向。副作用：无。
        """
        # 系统级与错误级日志不展示“从机:0”，保持界面干净直观
        if self.direction == "SYS":
            return f"[{self.timestamp}] [系统] {self.message}\n"
        if self.direction == "ERROR":
            return f"[{self.timestamp}] [错误] {self.message}\n"
        # 收发报文日志显式标出方向（RX/TX）与目标从机站点号，方便多站点并发联调时区分报文归属
        return f"[{self.timestamp}] [{self.direction}] 从机:{self.slave_id} | {self.message}\n"
