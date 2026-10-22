"""
点位值换算核心（纯逻辑，无 tkinter）
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
把"原始 16-bit 寄存器 → 解码工程值 → 乘 scale 系数"这条缺失的链路补上，
作为日后 MQTT 云端侧对账时的**本地基准值**来源。

设计原则（对应 AGENTS.md「修改最小化」）：
- 不碰 modbus_codec.decode_value 内部，只在它之上加换算层；
- 纯函数、可单测、可被 CLI 与未来的 MQTT 模块 import 复用；
- scale 只对数值型点位生效，STRING/HEX16/BOOL 乘系数无意义，原样返回。
"""

from typing import Any, List, Tuple

# 底层编解码器（只 import 它现成的 decode_value，不改它内部）
from ..modbus_codec import decode_value, ModbusDataType, ByteOrderMode
# 业务层点位模型（data_type/byte_order 是业务层枚举，值与底层同名）
from ..models import ModbusPoint


# 参与 scale 换算的数据类型集合（数值型才乘系数有意义）。
# 设计取舍：STRING 返回 str、HEX16 返回 "0x..." 展示串、BOOL 返回 bool，
# 这些乘一个 float 系数只会得到无意义的结果，所以排除在外。
_SCALABLE_TYPES = frozenset({
    "INT16", "UINT16", "INT32", "UINT32", "FLOAT32", "FLOAT64",
})


def is_scalable(point: ModbusPoint) -> bool:
    """判断该点位的类型是否参与 scale 换算。

    为什么单独抽出来：类型判定是"业务策略"，把它和换算分离，
    日后若策略变了（比如允许 BOOL 点位带 scale）只改这一处。
    参数：point（ModbusPoint 实例）。返回：bool（数值型 → True）。
    """
    # 业务层 DataType 是 str 枚举，直接取 .value 与底层集合比对
    return point.data_type.value in _SCALABLE_TYPES


def decode_point(registers: List[int], point: ModbusPoint) -> Any:
    """把原始寄存器解算为未乘系数的工程值。

    参数：registers（自 point.area+point.address 起、长度为
          point.register_count 的原始 16-bit 值列表）；point（点位定义）。
    返回：decode_value 解算后的原始工程值（int/float/bool/str）。
    步骤：桥接业务层枚举到底层枚举，再委托 decode_value。副作用：无。
    """
    data_type = ModbusDataType(point.data_type.value)
    byte_order = ByteOrderMode(point.byte_order.value)
    return decode_value(registers, data_type, byte_order, point.string_length)


def scale_value(value: Any, point: ModbusPoint) -> Any:
    """对解算值应用点位的 scale 系数，得到最终工程值。

    为什么 try/except 兜底：个别点位 scale 可能被配成异常值，
    换算层不应让单个坏点位把整条对账链拖崩，乘不出就原样返回。
    参数：value（decode_point 的解算结果）；point（点位定义）。
    返回：数值型返回 value*scale；非数值型（is_scalable=False）原样返回。
    """
    if not is_scalable(point):
        return value
    try:
        return value * point.scale
    except (TypeError, ValueError):
        # 兜底：系数或值无法相乘时退回未换算值，避免整批失败
        return value


def compute_point_value(
    registers: List[int], point: ModbusPoint
) -> Tuple[Any, Any]:
    """一站式：给定原始寄存器与点位定义，返回 (解算值, 换算后工程值)。

    参数：registers（原始寄存器列表）；point（点位定义）。
    返回：(raw_value, scaled_value)。raw_value 未乘系数，
          scaled_value 为数值型时已乘 point.scale，否则等于 raw_value。
    副作用：无。
    """
    raw_value = decode_point(registers, point)
    scaled_value = scale_value(raw_value, point)
    return raw_value, scaled_value


def point_key(point: ModbusPoint) -> str:
    """生成点位复合键，形如 "4x_HoldingRegister|0"。

    为什么这么拼：与 session_state.json 的 points 主键、以及未来
    MQTT payload 的 point_key 保持同一结构，保证两侧能直接对齐比对。
    参数：point（ModbusPoint 实例）。返回：str 复合键。副作用：无。
    """
    return f"{point.area.value}|{point.address}"
