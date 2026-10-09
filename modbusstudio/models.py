"""
Modbus Studio - 领域数据模型 (Domain Models)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
纯粹的数据实体与值对象定义，完全解耦于 UI (Tkinter) 与网络协议库 (PyModbus/PySerial)。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
import time


class CommType(str, Enum):
    """通信物理层类型"""
    TCP = "TCP"
    RTU = "RTU"

    @classmethod
    def from_str(cls, val: str) -> CommType:
        if "RTU" in str(val).upper() or "串口" in str(val):
            return cls.RTU
        return cls.TCP


# -----------------------------------------------------------------------------
# 系统资源配额与容量上限常量 (System Resource Quotas)
# -----------------------------------------------------------------------------
MAX_TCP_SLAVE_INSTANCES: int = 100
"""以太网 Modbus TCP 从机服务实例最大承载数量上限"""

MAX_RTU_SLAVE_INSTANCES: int = 100
"""串行总线 Modbus RTU 从机服务实例最大承载数量上限"""



class AreaType(str, Enum):
    """Modbus 存储区域类型"""
    HOLDING = "4x_HoldingRegister"
    INPUT = "3x_InputRegister"
    COIL = "0x_Coil"
    DISCRETE = "1x_DiscreteInput"

    @property
    def friendly_name(self) -> str:
        mapping = {
            AreaType.HOLDING: "保持寄存器 (4x)",
            AreaType.INPUT: "输入寄存器 (3x)",
            AreaType.COIL: "线圈 (0x)",
            AreaType.DISCRETE: "离散输入 (1x)",
        }
        return mapping.get(self, str(self.value))

    @classmethod
    def normalize(cls, val: Any) -> AreaType:
        if isinstance(val, AreaType):
            return val
        if hasattr(val, "value"):
            val = val.value
        s = str(val).strip()
        if "." in s:
            s = s.split(".")[-1]
        if "4" in s or "保持" in s or "HOLDING" in s.upper():
            return cls.HOLDING
        if "3" in s or "输入" in s or "INPUT" in s.upper():
            return cls.INPUT
        if "0" in s or "线圈" in s or "COIL" in s.upper():
            return cls.COIL
        if "1" in s or "离散" in s or "DISCRETE" in s.upper():
            return cls.DISCRETE
        return cls.HOLDING


class ByteOrder(str, Enum):
    """32/64位多寄存器字节序与变位模式"""
    ABCD = "ABCD"  # Big-Endian
    CDAB = "CDAB"  # Word-Swapped (工业及部分逆变器最常用)
    BADC = "BADC"  # Byte-Swapped
    DCBA = "DCBA"  # Little-Endian

    @classmethod
    def normalize(cls, val: Any) -> ByteOrder:
        if isinstance(val, ByteOrder):
            return val
        if hasattr(val, "value"):
            val = val.value
        s = str(val).strip().upper()
        if "." in s:
            s = s.split(".")[-1]
        for member in cls:
            if member.value == s or member.name == s:
                return member
        for order in ("CDAB", "ABCD", "BADC", "DCBA"):
            if order in s:
                return cls(order)
        return cls.ABCD


class DataType(str, Enum):
    """寄存器承载的解析数据类型"""
    INT16 = "INT16"
    UINT16 = "UINT16"
    INT32 = "INT32"
    UINT32 = "UINT32"
    FLOAT32 = "FLOAT32"
    FLOAT64 = "FLOAT64"
    BOOL = "BOOL"
    STRING = "STRING"
    HEX16 = "HEX16"

    @property
    def register_count(self) -> int:
        if self in (DataType.INT16, DataType.UINT16, DataType.HEX16, DataType.BOOL):
            return 1
        elif self in (DataType.INT32, DataType.UINT32, DataType.FLOAT32):
            return 2
        elif self in (DataType.FLOAT64,):
            return 4
        elif self == DataType.STRING:
            return 8  # 默认 8 个寄存器 (16 字符)
        return 1

    @classmethod
    def normalize(cls, val: Any) -> DataType:
        if isinstance(val, DataType):
            return val
        if hasattr(val, "value"):
            val = val.value
        s = str(val).strip().upper()
        if "." in s:
            s = s.split(".")[-1]
        for member in cls:
            if member.value == s or member.name == s:
                return member
        if "FLOAT64" in s or "DOUBLE" in s:
            return cls.FLOAT64
        if "FLOAT" in s or "FLOAT32" in s:
            return cls.FLOAT32
        if "UINT32" in s or "DWORD" in s:
            return cls.UINT32
        if "INT32" in s:
            return cls.INT32
        if "UINT16" in s or "USHORT" in s or "UNSIGNED SHORT" in s:
            return cls.UINT16
        if "INT16" in s or "SHORT" in s or "SIGNED SHORT" in s:
            return cls.INT16
        if "BOOL" in s or "BIT" in s:
            return cls.BOOL
        if "STRING" in s or "STR" in s:
            return cls.STRING
        if "HEX" in s:
            return cls.HEX16
        return cls.UINT16


class SimRule(str, Enum):
    """从机数据变位动态模拟规则"""
    CONSTANT = "固定"
    RANDOM = "随机波动"
    INCREMENT = "步进递增"
    SINE = "正弦波"
    RECT = "方波"

    @classmethod
    def normalize(cls, val: Any) -> str:
        if isinstance(val, SimRule):
            return val.value
        if hasattr(val, "value"):
            val = val.value
        s = str(val).strip()
        if "." in s:
            s = s.split(".")[-1]
        for member in cls:
            if member.value == s or member.name == s or member.value in s:
                return member.value
        return cls.CONSTANT.value


@dataclass
class ConnectionConfig:
    """通信连接配置（无论从机监听或主机连接，统一定义）"""
    comm_type: CommType = CommType.TCP
    host: str = "127.0.0.1"
    port: int = 502
    com_port: str = "COM1"
    baudrate: int = 9600
    data_bits: int = 8
    parity: str = "N"          # N, E, O
    stop_bits: int = 1
    unit_id: int = 1           # Modbus 从机站号
    timeout: float = 1.0

    @property
    def summary(self) -> str:
        if self.comm_type == CommType.TCP:
            return f"TCP {self.host}:{self.port} (ID:{self.unit_id})"
        return f"RTU {self.com_port}@{self.baudrate},{self.data_bits}{self.parity}{self.stop_bits} (ID:{self.unit_id})"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "comm_type": self.comm_type.value,
            "host": self.host,
            "port": self.port,
            "com_port": self.com_port,
            "baudrate": self.baudrate,
            "data_bits": self.data_bits,
            "parity": self.parity,
            "stop_bits": self.stop_bits,
            "unit_id": self.unit_id,
            "timeout": self.timeout,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ConnectionConfig:
        return cls(
            comm_type=CommType.from_str(d.get("comm_type", "TCP")),
            host=str(d.get("host", "127.0.0.1")),
            port=int(d.get("port", 502)),
            com_port=str(d.get("com_port", "COM1")),
            baudrate=int(d.get("baudrate", 9600)),
            data_bits=int(d.get("data_bits", 8)),
            parity=str(d.get("parity", "N")),
            stop_bits=int(d.get("stop_bits", 1)),
            unit_id=int(d.get("unit_id", 1)),
            timeout=float(d.get("timeout", 1.0)),
        )


@dataclass
class ModbusPoint:
    """单个 Modbus 点位实体模型"""
    address: int
    description: str = ""
    area: AreaType = AreaType.HOLDING
    data_type: DataType = DataType.UINT16
    byte_order: ByteOrder = ByteOrder.ABCD
    value: Any = 0
    sim_rule: str = "固定"
    raw_hex: str = "0x0000"
    string_length: int = 16    # 当为 STRING 类型时指定的字符长度
    field_name: str = ""       # 业务数据库绑定的英文标识符
    scale: float = 1.0         # 缩放比例
    unit: str = ""             # 工程单位

    @property
    def register_count(self) -> int:
        if self.data_type == DataType.STRING:
            # 两个字符占一个 16-bit 寄存器
            return max(1, (self.string_length + 1) // 2)
        return self.data_type.register_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "address": self.address,
            "description": self.description,
            "area": self.area.value,
            "data_type": self.data_type.value,
            "byte_order": self.byte_order.value,
            "value": self.value,
            "sim_rule": self.sim_rule,
            "raw_hex": self.raw_hex,
            "string_length": self.string_length,
            "field_name": self.field_name,
            "scale": self.scale,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ModbusPoint:
        return cls(
            address=int(d.get("address", 0)),
            description=str(d.get("description", "")),
            area=AreaType.normalize(d.get("area", "4x_HoldingRegister")),
            data_type=DataType.normalize(d.get("data_type", "UINT16")),
            byte_order=ByteOrder.normalize(d.get("byte_order", "ABCD")),
            value=d.get("value", 0),
            sim_rule=SimRule.normalize(d.get("sim_rule", "固定")),
            raw_hex=str(d.get("raw_hex", "0x0000")),
            string_length=int(d.get("string_length", 16)),
            field_name=str(d.get("field_name", "")),
            scale=float(d.get("scale", 1.0)),
            unit=str(d.get("unit", "")),
        )


@dataclass
class SlaveDevice:
    """Modbus 从机实例聚合根 (Aggregate Root)"""
    id: str
    name: str
    conn_config: ConnectionConfig
    points: Dict[Tuple[str, int], ModbusPoint] = field(default_factory=dict)
    is_running: bool = False
    runtime_info: str = "已就绪"
    db_file: Optional[str] = None
    db_device_id: Optional[int] = None
    db_device_name: Optional[str] = None

    def add_point(self, point: ModbusPoint) -> None:
        key = (point.area.value, point.address)
        self.points[key] = point

    def remove_point(self, area: Union[str, AreaType], address: int) -> Optional[ModbusPoint]:
        area_str = AreaType.normalize(area).value
        return self.points.pop((area_str, address), None)

    def get_point(self, area: Union[str, AreaType], address: int) -> Optional[ModbusPoint]:
        area_str = AreaType.normalize(area).value
        return self.points.get((area_str, address))

    def clear_points(self) -> None:
        self.points.clear()


@dataclass
class PollTask:
    """主机主动轮询任务模型"""
    conn_config: ConnectionConfig
    area: AreaType = AreaType.HOLDING
    start_address: int = 1
    count: int = 10
    byte_order: ByteOrder = ByteOrder.ABCD
    is_loop: bool = False
    interval_ms: int = 1000


@dataclass
class PollResult:
    """单次读取/轮询结果数据实体"""
    success: bool
    timestamp: float = field(default_factory=time.time)
    area: AreaType = AreaType.HOLDING
    start_address: int = 0
    count: int = 0
    raw_registers: List[int] = field(default_factory=list)
    raw_frame_hex: str = ""
    error_msg: str = ""
    elapsed_ms: float = 0.0


@dataclass
class LogEntry:
    """通信报文与系统日志条目实体（用于缓冲节流池）"""
    timestamp: str
    direction: str       # "SYS", "RX", "TX", "ERROR"
    slave_id: int
    message: str
    raw_frame_hex: str = ""

    def format_line(self) -> str:
        if self.direction == "SYS":
            return f"[{self.timestamp}] [系统] {self.message}\n"
        if self.direction == "ERROR":
            return f"[{self.timestamp}] [错误] {self.message}\n"
        return f"[{self.timestamp}] [{self.direction}] 从机:{self.slave_id} | {self.message}\n"
