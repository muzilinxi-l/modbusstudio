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
