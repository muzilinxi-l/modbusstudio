"""
Modbus 编解码与 4 种变位模式自动化单元测试
"""

import pytest
from modbusstudio.modbus_codec import (
    ByteOrderMode,
    ModbusDataType,
    decode_value,
    encode_value,
)


def test_uint16_codec():
    """测试 UINT16 标准编码与解码"""
    val = 12345
    regs = encode_value(val, ModbusDataType.UINT16, ByteOrderMode.ABCD)
    assert len(regs) == 1
    assert regs[0] == 12345
    decoded = decode_value(regs, ModbusDataType.UINT16, ByteOrderMode.ABCD)
    assert decoded == 12345


def test_int16_negative_codec():
    """测试 INT16 负数补码编码与解码"""
    val = -500
    regs = encode_value(val, ModbusDataType.INT16, ByteOrderMode.ABCD)
    assert len(regs) == 1
    assert regs[0] == 65536 - 500
    decoded = decode_value(regs, ModbusDataType.INT16, ByteOrderMode.ABCD)
    assert decoded == -500


def test_float32_all_byte_orders():
    """测试 FLOAT32 在 4 种变位模式 (ABCD, CDAB, BADC, DCBA) 下的精确往返转换"""
    val = 123.456
    for mode in [ByteOrderMode.ABCD, ByteOrderMode.CDAB, ByteOrderMode.BADC, ByteOrderMode.DCBA]:
        regs = encode_value(val, ModbusDataType.FLOAT32, mode)
        assert len(regs) == 2, f"FLOAT32 应占用 2 个寄存器, 模式: {mode}"
        decoded = decode_value(regs, ModbusDataType.FLOAT32, mode)
        assert pytest.approx(decoded, rel=1e-5) == val, f"模式 {mode} 往返解码精度失败"


def test_float32_word_swap_cdab():
    """专门测试工业光储逆变器最常用的 CDAB (Word-Swap) 变位"""
    val = 750.5
    regs_abcd = encode_value(val, ModbusDataType.FLOAT32, ByteOrderMode.ABCD)
    regs_cdab = encode_value(val, ModbusDataType.FLOAT32, ByteOrderMode.CDAB)
    # CDAB 模式高低字互换
    assert regs_cdab[0] == regs_abcd[1]
    assert regs_cdab[1] == regs_abcd[0]
    decoded = decode_value(regs_cdab, ModbusDataType.FLOAT32, ByteOrderMode.CDAB)
    assert pytest.approx(decoded, rel=1e-5) == val


def test_int32_codec():
    """测试 INT32 32位整数及变位"""
    val = -100000
    regs = encode_value(val, ModbusDataType.INT32, ByteOrderMode.CDAB)
    assert len(regs) == 2
    decoded = decode_value(regs, ModbusDataType.INT32, ByteOrderMode.CDAB)
    assert decoded == -100000


def test_bool_codec():
    """测试 BOOL 类型"""
    regs_true = encode_value(True, ModbusDataType.BOOL, ByteOrderMode.ABCD)
    assert regs_true[0] == 1
    assert decode_value(regs_true, ModbusDataType.BOOL, ByteOrderMode.ABCD) is True

    regs_false = encode_value(False, ModbusDataType.BOOL, ByteOrderMode.ABCD)
    assert regs_false[0] == 0
    assert decode_value(regs_false, ModbusDataType.BOOL, ByteOrderMode.ABCD) is False
