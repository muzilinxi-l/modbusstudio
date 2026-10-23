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
from ..models import DataType, ModbusPoint


# 业务层 DataType 到底层 ModbusDataType 的集中转换映射表。
# 为什么需要转换：业务层定义为 FLOAT64，而底层编解码器命名为 DOUBLE64（modbus_codec.py:76）；
# 其余 8 种类型（INT16/UINT16/INT32/UINT32/FLOAT32/BOOL/STRING/HEX16）名称完全相同。
_CODEC_TYPE_MAP = {
    DataType.FLOAT64.value: ModbusDataType.DOUBLE64,
}


def to_codec_type(data_type: Any) -> ModbusDataType:
    """把业务层 DataType 映射为底层 ModbusDataType。

    为什么集中收口：业务层 FLOAT64 在底层对应 DOUBLE64，集中收口保证
    point_value.py 与 reconcile.py 复用同一逻辑，防止两处各自内联导致修一处漏一处。
    参数：data_type（DataType 实例或类型字符串）。返回：ModbusDataType 底层枚举。
    """
    val = data_type.value if hasattr(data_type, "value") else str(data_type)
    target = _CODEC_TYPE_MAP.get(val, val)
    return ModbusDataType(target)


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
    data_type = to_codec_type(point.data_type)
    byte_order = ByteOrderMode(point.byte_order.value)
    return decode_value(registers, data_type, byte_order, point.string_length)


def scale_value(value: Any, point: ModbusPoint) -> Any:
    """对解算值应用点位的 scale 系数，得到最终工程值。

    为什么显式类型检查 + try/except 兜底：
    1. Python 中 int * str（如 100 * "bad"）会执行字符串序列乘法产生垃圾串，
       且不会抛 TypeError/ValueError。因此入口显式校验 scale 为 int/float；
    2. Python 中 bool 是 int 的子类（isinstance(True, int) 为 True），
       但布尔值作为系数在工业点表中无实际意义，显式排除 bool；
    3. 个别点位计算异常时退回未换算值，不阻断整条链路。
    参数：value（decode_point 的解算结果）；point（点位定义）。
    返回：数值型且 scale 合法时返回 value*scale；否则原样返回。
    """
    if not is_scalable(point):
        return value
    # 显式校验 scale 为合法数值类型（int 或 float，排除 bool）
    if not isinstance(point.scale, (int, float)) or isinstance(point.scale, bool):
        return value
    try:
        return value * point.scale
    except (TypeError, ValueError):
        # 兜底：计算无法完成时退回未换算值，避免整批失败
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
