"""
09_REVIEW_FIX_PROMPT 缺陷修复回归测试用例集
覆盖：H1 (配对字节序动态提取)、H2 (端口探测避让)、H3 (端口解析防御)
"""

import pytest
from modbusstudio.hems_pairing import PairingRule
from modbusstudio.modbus_codec import ByteOrderMode, ModbusDataType
from modbusstudio.models import CommType, ConnectionConfig, SlaveDevice, _parse_port
from modbusstudio.services import LoggingService, SlaveService


def test_hems_pairing_byte_orders():
    """H1 验收：验证 PairingRule.byte_order 随 target_format 动态确定，不再硬编码 CDAB"""
    # 格式 32: Float 1 (ABCD)
    rule_32 = PairingRule(
        target_app_id=1,
        target_app_name="North_Forward",
        target_register=100,
        target_fc=3,
        target_slave_id=1,
        target_format=32,
        src_app_id=2,
        src_app_name="PCS",
        src_var_name="Power",
        src_address=10,
        src_format=32,
    )
    assert rule_32.data_type == ModbusDataType.FLOAT32
    assert rule_32.byte_order == ByteOrderMode.ABCD

    # 格式 37: Double 2 (BADC)
    rule_37 = PairingRule(
        target_app_id=1,
        target_app_name="North_Forward",
        target_register=102,
        target_fc=3,
        target_slave_id=1,
        target_format=37,
        src_app_id=2,
        src_app_name="BMS",
        src_var_name="Energy",
        src_address=20,
        src_format=37,
    )
    assert rule_37.data_type == ModbusDataType.DOUBLE64
    assert rule_37.byte_order == ByteOrderMode.BADC

    # 格式 34: Float 3 (CDAB)
    rule_34 = PairingRule(
        target_app_id=1,
        target_app_name="North_Forward",
        target_register=106,
        target_fc=3,
        target_slave_id=1,
        target_format=34,
        src_app_id=2,
        src_app_name="Meter",
        src_var_name="Voltage",
        src_address=30,
        src_format=34,
    )
    assert rule_34.data_type == ModbusDataType.FLOAT32
    assert rule_34.byte_order == ByteOrderMode.CDAB

    # M2 加固测试：格式均为空时回退默认 INT16 / CDAB
    rule_empty = PairingRule(
        target_app_id=1,
        target_app_name="North_Forward",
        target_register=108,
        target_fc=3,
        target_slave_id=1,
        target_format=None,
        src_app_id=2,
        src_app_name="Meter",
        src_var_name="Unknown",
        src_address=40,
        src_format=None,
    )
    assert rule_empty.data_type == ModbusDataType.INT16
    assert rule_empty.byte_order == ByteOrderMode.CDAB


def test_slave_service_tcp_port_probing(tmp_path):
    """H2 验收：验证 get_next_available_tcp_port 自动避让已占用端口，从 502 起步分配首个可用端口"""
    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    slave_svc = SlaveService(logging_service=log_svc)

    # 预置 2 台从机，分别占用 502 和 504（模拟某台手动改端口为 504）
    dev1 = SlaveDevice(
        id="dev1",
        name="从机1",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, port=502),
    )
    dev2 = SlaveDevice(
        id="dev2",
        name="从机2",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, port=504),
    )
    slave_svc.register_device(dev1)
    slave_svc.register_device(dev2)

    occupied = slave_svc.get_occupied_tcp_ports()
    assert 502 in occupied
    assert 504 in occupied

    # 探测应避开 502 和 504，首个未占用端口应为 503
    next_port = slave_svc.get_next_available_tcp_port(start_port=502)
    assert next_port == 503

    # 若 503 也被占用，应顺延至 505
    dev3 = SlaveDevice(
        id="dev3",
        name="从机3",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, port=503),
    )
    slave_svc.register_device(dev3)
    next_port_2 = slave_svc.get_next_available_tcp_port(start_port=502)
    assert next_port_2 == 505


def test_parse_port_safety():
    """H3 验收：验证 _parse_port 防御非法输入、浮点、越界并安全回退到默认端口 502"""
    assert _parse_port("abc") == 502
    assert _parse_port("502.5") == 502
    assert _parse_port("0") == 502
    assert _parse_port("65536") == 502
    assert _parse_port("-1") == 502
    assert _parse_port("502 ") == 502
    assert _parse_port("502") == 502
    assert _parse_port(503) == 503
    assert _parse_port(None) == 502

    # ConnectionConfig.from_dict 防御异常端口不崩溃
    cfg = ConnectionConfig.from_dict({"host": "127.0.0.1", "port": "invalid_port"})
    assert cfg.port == 502


def test_is_bit_derivation_robustness():
    """M1 验收：验证 write_raw_values 对派生串及原始枚举的 is_bit 判定健壮，布尔区写入 bool 状态"""
    from modbusstudio.modbus_engine import ModbusSlaveEngine
    from modbusstudio.models import AreaType

    server = ModbusSlaveEngine(comm_type="TCP", port=15502, slave_id=1)
    # 模拟数据块预热
    coils_block = server._coils.simdata[0]
    # base_addr 默认 0
    # 测试 1: 传入派生串 "0x_Coil (线圈) @ 1"，地址 1，写入非零值 99
    server.write_raw_values("0x_Coil (线圈) @ 1", 1, [99])
    assert coils_block.values[0] is True

    # 测试 2: 传入裸 "0x_Coil"，地址 2，写入 0
    server.write_raw_values("0x_Coil", 2, [0])
    assert coils_block.values[1] is False

    # 测试 3: 传入标准枚举 AreaType.COIL.value，地址 3，写入 1
    server.write_raw_values(AreaType.COIL.value, 3, [1])
    assert coils_block.values[2] is True

    # 对照组：保持寄存器写入 99 应为整数 99，而不是 bool
    holding_block = server._holding_registers.simdata[0]
    server.write_raw_values(AreaType.HOLDING.value, 1, [99])
    assert holding_block.values[0] == 99
    assert not isinstance(holding_block.values[0], bool)


def test_validate_and_build_point_hex16():
    """M3 验收：验证 HEX16 格式校验支持 0x1A2B 与 1A2B，非法字符 ZZ 拒绝通过"""
    from modbusstudio.services import PointService

    # 1. 带 0x 前缀的合法十六进制
    ok1, pt1, err1 = PointService.validate_and_build_point(
        address=10,
        description="HexTest1",
        area="4x",
        data_type="HEX16",
        byte_order="CDAB",
        val_input="0x1A2B",
    )
    assert ok1 is True
    assert pt1 is not None
    assert pt1.value == 0x1A2B
    assert pt1.value == 6699

    # 2. 不带 0x 前缀的合法十六进制
    ok2, pt2, err2 = PointService.validate_and_build_point(
        address=11,
        description="HexTest2",
        area="4x",
        data_type="HEX16",
        byte_order="CDAB",
        val_input="1A2B",
    )
    assert ok2 is True
    assert pt2 is not None
    assert pt2.value == 0x1A2B

    # 3. 包含非法非十六进制字符 ZZ
    ok3, pt3, err3 = PointService.validate_and_build_point(
        address=12,
        description="HexTest3",
        area="4x",
        data_type="HEX16",
        byte_order="CDAB",
        val_input="ZZ",
    )
    assert ok3 is False
    assert pt3 is None
    assert "不匹配" in err3

