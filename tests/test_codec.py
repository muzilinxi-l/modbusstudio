"""
Modbus 编解码与 4 种变位模式自动化单元测试
"""

import pytest
from modbusstudio.modbus_codec import (
    ByteOrderMode,
    HEMS_TYPE_DEFINITIONS,
    ModbusDataType,
    decode_value,
    encode_value,
    parse_hems_type,
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


def test_parse_hems_type_menu_alignment():
    """验证 HEMS_TYPE_DEFINITIONS 数字编码严格对齐 extra/hems.cdb menu 表 (0~43)"""
    assert list(HEMS_TYPE_DEFINITIONS.keys()) == list(range(0, 44))
    # 0 = Invalid -> None (交由上层兜底)
    assert parse_hems_type(0) is None
    assert parse_hems_type("0") is None
    assert parse_hems_type("Invalid") is None
    assert parse_hems_type("无效") is None

    # 1~43 关键数字编码核对
    assert parse_hems_type(1) == (ModbusDataType.BOOL, ByteOrderMode.ABCD, 1)
    assert parse_hems_type(2) == (ModbusDataType.INT16, ByteOrderMode.ABCD, 1)
    assert parse_hems_type(3) == (ModbusDataType.UINT16, ByteOrderMode.ABCD, 1)
    assert parse_hems_type(4) == (ModbusDataType.INT16, ByteOrderMode.ABCD, 1)
    assert parse_hems_type(5) == (ModbusDataType.INT16, ByteOrderMode.BADC, 1)
    assert parse_hems_type(6) == (ModbusDataType.UINT16, ByteOrderMode.ABCD, 1)
    assert parse_hems_type(7) == (ModbusDataType.UINT16, ByteOrderMode.BADC, 1)
    assert parse_hems_type(8) == (ModbusDataType.INT32, ByteOrderMode.ABCD, 2)
    assert parse_hems_type(10) == (ModbusDataType.INT32, ByteOrderMode.CDAB, 2)
    assert parse_hems_type(12) == (ModbusDataType.UINT32, ByteOrderMode.ABCD, 2)
    assert parse_hems_type(16) == (ModbusDataType.INT64, ByteOrderMode.ABCD, 4)
    assert parse_hems_type(24) == (ModbusDataType.UINT64, ByteOrderMode.ABCD, 4)
    assert parse_hems_type(32) == (ModbusDataType.FLOAT32, ByteOrderMode.ABCD, 2)
    assert parse_hems_type(34) == (ModbusDataType.FLOAT32, ByteOrderMode.CDAB, 2)
    assert parse_hems_type(36) == (ModbusDataType.DOUBLE64, ByteOrderMode.ABCD, 4)
    assert parse_hems_type(43) == (ModbusDataType.DOUBLE64, ByteOrderMode.DCBA, 4)


def test_parse_hems_type_id_and_name_equivalence():
    """验证 1~43 的数字 ID、数字字符串、英文名、中文名解析结果完全一致"""
    for type_id in range(1, 44):
        eng, chn, dtype, mode, cnt = HEMS_TYPE_DEFINITIONS[type_id]
        expected = (dtype, mode, cnt)
        assert parse_hems_type(type_id) == expected
        assert parse_hems_type(str(type_id)) == expected
        assert parse_hems_type(eng) == expected
        assert parse_hems_type(chn) == expected

