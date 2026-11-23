"""P0-1 ~ P0-5 核心缺陷专项回归测试。"""

import time
import pytest

from modbusstudio.modbus_codec import ByteOrderMode, ModbusDataType, encode_value
from modbusstudio.modbus_engine import (
    ModbusServerHub,
    ModbusSlaveEngine,
    ModbusTransportServer,
)
from modbusstudio.models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    ModbusPoint,
    PollTask,
    SlaveDevice,
)
from modbusstudio.services import LoggingService, PointService, PollService, SlaveService
from modbusstudio.tools.point_value import compute_point_value, is_scalable


def test_p0_1_poll_service_unit_id_and_all_four_areas(tmp_path):
    """P0-1：验证 PollService 严格生效 unit_id 且 0x/1x/3x/4x 四大区域分别路由到 FC01/02/04/03。"""
    port = 25101
    s1 = ModbusSlaveEngine(comm_type="TCP", host="127.0.0.1", port=port, slave_id=1)
    s2 = ModbusSlaveEngine(comm_type="TCP", host="127.0.0.1", port=port, slave_id=2)

    # 站 1 与站 2 写入截然不同的 4 区数据，验证站号与功能码均精准匹配
    s1.write_raw_values(AreaType.COIL.value, 1, [1, 0])
    s1.write_raw_values(AreaType.DISCRETE.value, 1, [0, 1])
    s1.write_raw_values(AreaType.INPUT.value, 1, [111, 112])
    s1.write_raw_values(AreaType.HOLDING.value, 1, [1001, 1002])

    s2.write_raw_values(AreaType.COIL.value, 1, [0, 1])
    s2.write_raw_values(AreaType.DISCRETE.value, 1, [1, 1])
    s2.write_raw_values(AreaType.INPUT.value, 1, [221, 222])
    s2.write_raw_values(AreaType.HOLDING.value, 1, [2001, 2002])

    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    poll_svc = PollService(log_svc)
    try:
        s1.start()
        s2.start()

        cfg2 = ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=port, unit_id=2)
        for area, expected in [
            (AreaType.COIL, [0, 1]),
            (AreaType.DISCRETE, [1, 1]),
            (AreaType.INPUT, [221, 222]),
            (AreaType.HOLDING, [2001, 2002]),
        ]:
            res = poll_svc.read_registers(PollTask(conn_config=cfg2, area=area, start_address=1, count=2))
            assert res.success, f"读取站2 {area.value} 失败: {res.error_msg}"
            assert res.raw_registers == expected, f"站2 {area.value} 数据不匹配: {res.raw_registers} != {expected}"

        # 不重建连接直接切换目标站号为 1，应实时切到站 1
        cfg1 = ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=port, unit_id=1)
        res_in1 = poll_svc.read_registers(PollTask(conn_config=cfg1, area=AreaType.INPUT, start_address=1, count=2))
        assert res_in1.success
        assert res_in1.raw_registers == [111, 112]
    finally:
        poll_svc.disconnect()
        s1.stop()
        s2.stop()
        log_svc.close()


def test_p0_2_hex16_no_double_hex_conversion():
    """P0-2：验证 HEX16/HEX32 不会对整数值做二次十六进制解析。"""
    ok1, pt1, err1 = PointService.validate_and_build_point(
        address=1,
        description="HexFF",
        area="4x",
        data_type="HEX16",
        byte_order="ABCD",
        val_input="0x00FF",
    )
    assert ok1 and pt1 is not None, err1
    assert pt1.value == 0x00FF
    assert pt1.raw_hex == "0x00FF"

    ok2, pt2, err2 = PointService.validate_and_build_point(
        address=2,
        description="Hex1A2B",
        area="4x",
        data_type="HEX16",
        byte_order="ABCD",
        val_input="0x1A2B",
    )
    assert ok2 and pt2 is not None, err2
    assert pt2.value == 0x1A2B
    assert pt2.raw_hex == "0x1A2B"

    PointService.re_encode_point(pt2)
    assert pt2.raw_hex == "0x1A2B"

    # 直接传整数入参同样不可双重解析
    ok3, pt3, err3 = PointService.validate_and_build_point(
        address=3,
        description="HexInt",
        area="4x",
        data_type="HEX16",
        byte_order="ABCD",
        val_input=0x00FF,
    )
    assert ok3 and pt3 is not None, err3
    assert pt3.value == 255
    assert pt3.raw_hex == "0x00FF"

    assert encode_value(0x00FF, ModbusDataType.HEX16, ByteOrderMode.ABCD) == [0x00FF]
    assert encode_value(0x12345678, ModbusDataType.HEX32, ByteOrderMode.ABCD) == [0x1234, 0x5678]


def test_p0_3_transport_server_rapid_start_stop_no_race():
    """P0-3：验证连续 5 次紧邻 start()->stop() 均快速完成，无 2s join 超时竞态。"""
    port = 25103
    for i in range(5):
        srv = ModbusTransportServer(("TCP", "127.0.0.1", port))
        try:
            t0 = time.perf_counter()
            srv.start()
            assert srv.is_healthy(), f"第 {i+1} 轮 start() 后应处于健康态"
            srv.stop()
            elapsed = time.perf_counter() - t0
            assert not srv.is_running
            assert elapsed < 0.8, f"第 {i+1} 轮 start+stop 耗时 {elapsed:.3f}s 过长"
        finally:
            srv.stop()


def test_p0_4_restore_devices_allows_same_port_multi_station(tmp_path):
    """P0-4：验证会话恢复允许同 TCP 端口挂载不同站号从机，仅拦截同端点同站号冲突。"""
    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    slave_svc = SlaveService(log_svc)
    try:
        d1 = SlaveDevice(
            id="hub_s1",
            name="PCS_1",
            conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=25104, unit_id=1),
        ).to_dict()
        d2 = SlaveDevice(
            id="hub_s2",
            name="PCS_2",
            conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=25104, unit_id=2),
        ).to_dict()
        d_dup = SlaveDevice(
            id="hub_s1_dup",
            name="PCS_1_重复",
            conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=25104, unit_id=1),
        ).to_dict()

        count, warnings = slave_svc.restore_devices([d1, d2, d_dup])
        assert count == 2
        assert len(warnings) == 1
        assert "相同站地址 1" in warnings[0]
        assert slave_svc.get_device("hub_s1") is not None
        assert slave_svc.get_device("hub_s2") is not None
        assert slave_svc.get_device("hub_s1_dup") is None
    finally:
        log_svc.close()


def test_p0_5_float64_simulation_and_int64_uint64_pipeline(tmp_path):
    """P0-5：验证 INT64/UINT64 领域模型不丢失精度，且 FLOAT64 模拟循环不会降级为 INT16。"""
    assert DataType.normalize("INT64") == DataType.INT64
    assert DataType.normalize("UINT64") == DataType.UINT64
    assert DataType.INT64.register_count == 4
    assert DataType.UINT64.register_count == 4

    big_i64 = 9007199254740995  # 超过 float64 精确整数范围 (2^53)
    ok, pt_i64, err = PointService.validate_and_build_point(
        address=10,
        description="总电量64位",
        area=AreaType.HOLDING,
        data_type="INT64",
        byte_order="ABCD",
        val_input=str(big_i64),
    )
    assert ok and pt_i64 is not None, err
    assert pt_i64.value == big_i64
    assert len(pt_i64.raw_hex.split()) == 4
    assert is_scalable(pt_i64) is True

    regs = encode_value(1000, ModbusDataType.INT64, ByteOrderMode.ABCD)
    pt_scaled = ModbusPoint(address=20, data_type=DataType.INT64, scale=0.1)
    raw_v, scaled_v = compute_point_value(regs, pt_scaled)
    assert raw_v == 1000
    assert scaled_v == pytest.approx(100.0)

    # 验证 FLOAT64 在 SlaveService 启动的动态模拟循环中保持浮点步进 (+0.5) 且占 4 寄存器
    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    slave_svc = SlaveService(log_svc)
    dev = SlaveDevice(
        id="dev_f64_sim",
        name="F64仿真测试",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=25105, unit_id=1),
    )
    ok_f, pt_f64, _ = PointService.validate_and_build_point(
        address=1,
        description="高精度累计电能",
        area=AreaType.HOLDING,
        data_type=DataType.FLOAT64,
        byte_order=ByteOrder.ABCD,
        val_input=10.0,
        sim_rule="步进递增",
    )
    assert ok_f and pt_f64 is not None
    dev.add_point(pt_f64)
    slave_svc.register_device(dev)

    try:
        started, msg = slave_svc.start_slave(dev.id)
        assert started, msg
        time.sleep(1.3)
        assert isinstance(pt_f64.value, float), f"FLOAT64 模拟后被降级为 {type(pt_f64.value)}: {pt_f64.value}"
        assert pt_f64.value > 10.0
        assert len(pt_f64.raw_hex.split()) == 4
    finally:
        slave_svc.stop_slave(dev.id)
        log_svc.close()
