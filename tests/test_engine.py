"""
Modbus 业务服务、点位构建与从机生命周期测试
"""

from modbusstudio.models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    SlaveDevice,
)
from modbusstudio.services import LoggingService, PointService, SlaveService


def test_validate_and_build_point_success():
    """测试标准点位校验与创建 (以 FLOAT32 为例)"""
    ok, point, err = PointService.validate_and_build_point(
        address=10,
        description="直流侧电压",
        area="保持寄存器 (4x)",
        data_type="FLOAT32",
        byte_order="CDAB",
        val_input=752.4,
        sim_rule="固定",
    )
    assert ok is True
    assert point is not None
    assert point.address == 10
    assert point.area == AreaType.HOLDING
    assert point.data_type == DataType.FLOAT32
    assert point.byte_order == ByteOrder.CDAB
    assert point.register_count == 2
    assert point.raw_hex.startswith("0x")


def test_validate_and_build_point_invalid_address():
    """测试非法地址拒绝拦截"""
    ok, point, err = PointService.validate_and_build_point(
        address=-1,
        description="错误地址",
        area=AreaType.HOLDING,
        data_type=DataType.UINT16,
        byte_order=ByteOrder.ABCD,
        val_input=0,
    )
    assert ok is False
    assert "超出有效范围" in err

    ok, point, err = PointService.validate_and_build_point(
        address=70000,
        description="溢出地址",
        area=AreaType.HOLDING,
        data_type=DataType.UINT16,
        byte_order=ByteOrder.ABCD,
        val_input=0,
    )
    assert ok is False
    assert "超出有效范围" in err


def test_slave_service_registration_and_retrieval():
    """测试从机注册与获取管理"""
    log_svc = LoggingService(log_dir="logs")
    slave_svc = SlaveService(log_svc)

    cfg = ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=5020, unit_id=1)
    dev = SlaveDevice(id="test_slave_1", name="测试从机", conn_config=cfg)

    slave_svc.register_device(dev)
    assert slave_svc.get_device("test_slave_1") is not None
    assert len(slave_svc.get_devices()) == 1


def test_slave_service_port_collision_prevention():
    """测试端口冲突防护"""
    log_svc = LoggingService(log_dir="logs")
    slave_svc = SlaveService(log_svc)

    cfg1 = ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=5020, unit_id=1)
    dev1 = SlaveDevice(id="s1", name="从机1", conn_config=cfg1)
    dev1.is_running = True  # 模拟已在运行

    cfg2 = ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=5020, unit_id=2)
    dev2 = SlaveDevice(id="s2", name="从机2", conn_config=cfg2)

    slave_svc.register_device(dev1)
    slave_svc.register_device(dev2)

    ok, msg = slave_svc.start_slave("s2")
    assert ok is False
    assert "TCP端口冲突" in msg


def test_slave_engine_pre_startup_data_persistence():
    """测试在服务器启动前写入点位，数据正确写入本地数据块并能读出"""
    from modbusstudio.modbus_engine import ModbusSlaveEngine, ModbusDataType, ByteOrderMode
    engine = ModbusSlaveEngine(comm_type="TCP", port=5999)
    # 服务尚未启动
    assert engine.is_running is False
    engine.write_typed_value("4x_HoldingRegister", 10, 752.4, ModbusDataType.FLOAT32, ByteOrderMode.CDAB)
    # 读出验证
    read_val = engine.read_typed_value("4x_HoldingRegister", 10, ModbusDataType.FLOAT32, ByteOrderMode.CDAB)
    import pytest
    assert pytest.approx(read_val, rel=1e-4) == 752.4

    # 验证高地址支持 (例如 12000)
    engine.write_typed_value("4x_HoldingRegister", 12000, 65000, ModbusDataType.UINT16, ByteOrderMode.ABCD)
    assert engine.read_typed_value("4x_HoldingRegister", 12000, ModbusDataType.UINT16, ByteOrderMode.ABCD) == 65000


def test_input_register_3x_read_write():
    """测试输入寄存器 (3x_InputRegister) 的精确读写与 DataBlock 路由映射"""
    from modbusstudio.modbus_engine import ModbusSlaveEngine, ModbusDataType, ByteOrderMode
    engine = ModbusSlaveEngine(comm_type="TCP", port=5998)

    # 写入输入寄存器 3x 地址 844
    engine.write_typed_value("3x_InputRegister", 844, 1234, ModbusDataType.UINT16, ByteOrderMode.ABCD)

    # 从输入寄存器读出原始寄存器值
    raw = engine.read_raw_values("3x_InputRegister", 844, 1)
    assert raw[0] == 1234, f"输入寄存器未正确写入底层 _input_registers: {raw}"

    # 验证保持寄存器未被误写 (应当仍为 0)
    raw_hr = engine.read_raw_values("4x_HoldingRegister", 844, 1)
    assert raw_hr[0] == 0, f"输入寄存器写入误污染了保持寄存器: {raw_hr}"


def test_sine_wave_simulation_value_changes():
    """测试正弦波模拟算法生成动态波形数值"""
    from modbusstudio.modbus_engine import ModbusSlaveEngine, ModbusDataType, ByteOrderMode
    engine = ModbusSlaveEngine(comm_type="TCP", port=5997)

    # 配置正弦波点位
    engine.points[844] = {
        "type": "UINT16",
        "mode": "ABCD",
        "sim_rule": "正弦波",
        "area": "3x_InputRegister",
        "current_val": 0,
    }

    recorded_values = []
    engine.on_point_value_changed = lambda area, addr, val: recorded_values.append(val)

    # 启动服务短时间运行
    engine.start()
    import time
    time.sleep(2.5)
    engine.stop()

    assert len(recorded_values) >= 2, f"正弦波未能按秒产生数据: {recorded_values}"
    assert all(isinstance(v, int) for v in recorded_values), "UINT16 正弦波输出应当为整数"
    assert all(v >= 0 for v in recorded_values), "UINT16 正弦波不应当产生负数"
    # 数值不应当全部相同为 0
    assert any(v > 0 for v in recorded_values), f"正弦波数值未生效，仍为全0: {recorded_values}"


