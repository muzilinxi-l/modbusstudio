"""
多从机同端口并发与站地址隔离集成测试
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
验证 Modbus TCP 标准规范：
- 同一 IP:Port 物理端点下挂载多个不同 Unit ID 的从机实例并发运行；
- 外部 Modbus 主站根据 MBAP Header 中的 Unit Identifier 精准寻址并隔离数据；
- 独立启停单个站地址不影响同一端口下其他站地址；
- 全部从机停止后物理端口监听安全释放。
"""

import time
import pytest
from pymodbus.client import ModbusTcpClient

from modbusstudio.models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    SlaveDevice,
)
from modbusstudio.services import LoggingService, PointService, SlaveService


def test_same_port_three_slaves_concurrency_and_data_isolation():
    """测试 127.0.0.1:5055 下并发运行 3 个从机 (UID=1, 2, 3) 且数据完全隔离"""
    log_svc = LoggingService(log_dir="logs")
    slave_svc = SlaveService(log_svc)

    test_port = 5055

    # 1. 创建 3 个不同从机，配置相同 IP 和端口，不同 Unit ID
    dev1 = SlaveDevice(
        id="dev_uid_1",
        name="逆变器_UID1",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=test_port, unit_id=1),
    )
    dev2 = SlaveDevice(
        id="dev_uid_2",
        name="电表_UID2",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=test_port, unit_id=2),
    )
    dev3 = SlaveDevice(
        id="dev_uid_3",
        name="BMS_UID3",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=test_port, unit_id=3),
    )

    # 给 UID 1 配置保持寄存器 10 = 1111 (UINT16)
    _, p1, _ = PointService.validate_and_build_point(
        address=10, description="UID1点位", area=AreaType.HOLDING,
        data_type=DataType.UINT16, byte_order=ByteOrder.ABCD, val_input=1111
    )
    dev1.add_point(p1)

    # 给 UID 2 配置保持寄存器 10 = 2222 (UINT16) (相同寄存器地址，数据隔离)
    _, p2, _ = PointService.validate_and_build_point(
        address=10, description="UID2点位", area=AreaType.HOLDING,
        data_type=DataType.UINT16, byte_order=ByteOrder.ABCD, val_input=2222
    )
    dev2.add_point(p2)

    # 给 UID 3 配置保持寄存器 10 = 3333 (UINT16)
    _, p3, _ = PointService.validate_and_build_point(
        address=10, description="UID3点位", area=AreaType.HOLDING,
        data_type=DataType.UINT16, byte_order=ByteOrder.ABCD, val_input=3333
    )
    dev3.add_point(p3)

    slave_svc.register_device(dev1)
    slave_svc.register_device(dev2)
    slave_svc.register_device(dev3)

    try:
        # 并发启动 3 个从机
        ok1, msg1 = slave_svc.start_slave("dev_uid_1")
        assert ok1 is True, f"UID 1 启动失败: {msg1}"

        ok2, msg2 = slave_svc.start_slave("dev_uid_2")
        assert ok2 is True, f"UID 2 启动失败: {msg2}"

        ok3, msg3 = slave_svc.start_slave("dev_uid_3")
        assert ok3 is True, f"UID 3 启动失败: {msg3}"

        assert dev1.is_running and dev2.is_running and dev3.is_running

        # 等待服务器就绪
        time.sleep(0.3)

        # 2. 外部客户端连接同一个 IP:Port，分别读取 UID=1, 2, 3
        client = ModbusTcpClient("127.0.0.1", port=test_port)
        assert client.connect() is True, "无法连接到 Modbus TCP 服务端口"

        # 读 UID 1
        res1 = client.read_holding_registers(10, count=1, device_id=1)
        assert not res1.isError()
        assert res1.registers[0] == 1111, f"UID 1 读取数据不匹配: {res1.registers}"

        # 读 UID 2
        res2 = client.read_holding_registers(10, count=1, device_id=2)
        assert not res2.isError()
        assert res2.registers[0] == 2222, f"UID 2 读取数据不匹配: {res2.registers}"

        # 读 UID 3
        res3 = client.read_holding_registers(10, count=1, device_id=3)
        assert not res3.isError()
        assert res3.registers[0] == 3333, f"UID 3 读取数据不匹配: {res3.registers}"

        # 3. 测试独立停止 UID 2
        slave_svc.stop_slave("dev_uid_2")
        assert dev2.is_running is False
        assert dev1.is_running is True
        assert dev3.is_running is True

        # UID 1 与 UID 3 仍能正常应答
        res1_after = client.read_holding_registers(10, count=1, device_id=1)
        assert not res1_after.isError() and res1_after.registers[0] == 1111

        res3_after = client.read_holding_registers(10, count=1, device_id=3)
        assert not res3_after.isError() and res3_after.registers[0] == 3333

        # 4. 再次重启 UID 2 并修改点位值
        p2.value = 8888
        PointService.re_encode_point(p2)
        ok2_restart, _ = slave_svc.start_slave("dev_uid_2")
        assert ok2_restart is True
        slave_svc.sync_point_to_runtime("dev_uid_2", p2)

        res2_restart = client.read_holding_registers(10, count=1, device_id=2)
        assert not res2_restart.isError()
        assert res2_restart.registers[0] == 8888

        client.close()

    finally:
        # 清理所有从机并安全释放端口
        slave_svc.clear_all_devices()
        time.sleep(0.2)


def test_same_port_same_station_collision_rejection():
    """测试同端口配置相同站号时准确拦截报错"""
    log_svc = LoggingService(log_dir="logs")
    slave_svc = SlaveService(log_svc)

    test_port = 5056

    dev_a = SlaveDevice(
        id="dev_a",
        name="设备A",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=test_port, unit_id=5),
    )
    dev_b = SlaveDevice(
        id="dev_b",
        name="设备B",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=test_port, unit_id=5),
    )

    slave_svc.register_device(dev_a)
    slave_svc.register_device(dev_b)

    try:
        ok_a, _ = slave_svc.start_slave("dev_a")
        assert ok_a is True

        # 尝试启动相同端口相同站号的设备 B
        ok_b, msg_b = slave_svc.start_slave("dev_b")
        assert ok_b is False
        assert "站地址冲突" in msg_b
    finally:
        slave_svc.clear_all_devices()


# [REVIEW-FIX-2] 验收测试：验证 async_setValues 拒绝向 FC4 (输入寄存器) 写入数据
def test_async_set_values_fc4_readonly_protection():
    """验证对从机使用只读功能码 FC4 调用 async_setValues 时为 no-op，数据区不受修改"""
    import asyncio
    from modbusstudio.modbus_engine import MultiStationServerContext, ModbusSlaveEngine

    engine = ModbusSlaveEngine(comm_type="TCP", port=5991, slave_id=1)
    engine.write_raw_values("3x_InputRegister", 10, [1234])
    ctx = MultiStationServerContext()
    ctx.attach_slave(1, engine)

    # 尝试使用只读功能码 FC4 执行写操作 (传入非法修改值 9999)
    res = asyncio.run(ctx.async_setValues(device_id=1, func_code=4, address=10, values=[9999]))
    assert res is None, "FC4 写操作应当被 no-op 拦截返回 None"

    # 验证底层 3x 输入寄存器值完全未被篡改，依然保持 1234
    vals = asyncio.run(ctx.async_getValues(device_id=1, func_code=4, address=10, count=1))
    assert vals == [1234], f"只读输入寄存器数据被非法篡改: {vals}"


# [REVIEW-FIX-1] 验收测试：验证消除看值 hack 后，0 值与邻居寄存器非 0 时寻址恒定不漂移
def test_address_indexing_deterministic_without_hack():
    """验证当寄存器为 0、前一寄存器非 0 时，读写寻址绝对稳定，不发生错位漂移"""
    import asyncio
    from modbusstudio.modbus_engine import MultiStationServerContext, ModbusSlaveEngine

    engine = ModbusSlaveEngine(comm_type="TCP", port=5992, slave_id=1, address_base=1)
    # 点位 9 设置为 8888，点位 10 设置为 0 (正常为 0V 或关闭状态)
    engine.write_raw_values("4x_HoldingRegister", 9, [8888])
    engine.write_raw_values("4x_HoldingRegister", 10, [0])

    ctx = MultiStationServerContext()
    ctx.attach_slave(1, engine)

    # 读取点位 10：必须稳定读取出 0，绝不能因为 values[9]!=0 漂移到读取点位 9 的 8888
    vals = asyncio.run(ctx.async_getValues(device_id=1, func_code=3, address=10, count=1))
    assert vals == [0], f"点位 10 为 0 时发生数据漂移错位，读出错误值: {vals}"


# [REVIEW-FIX-5] 验收测试：验证单端点停止从机期间，跨端点的从机读写不受阻塞，延迟 < 100ms
def test_cross_endpoint_non_blocking_during_stop():
    """验证 A 端点停止从机并释放底层 server 时，B 端点的读取请求不会因全局锁被阻塞"""
    import asyncio
    import threading
    import time
    from modbusstudio.modbus_engine import ModbusServerHub, ModbusSlaveEngine

    hub = ModbusServerHub.get_instance()
    eng_a = ModbusSlaveEngine(comm_type="TCP", port=5981, slave_id=1)
    eng_b = ModbusSlaveEngine(comm_type="TCP", port=5982, slave_id=2)
    eng_b.write_raw_values("4x_HoldingRegister", 1, [999])

    hub.register_and_start_slave(eng_a)
    hub.register_and_start_slave(eng_b)

    try:
        # 在独立线程中触发 eng_a 的停止注销 (含底层网络关闭)
        stop_thread = threading.Thread(target=lambda: hub.unregister_and_stop_slave(eng_a))
        stop_thread.start()

        # 在主线程中立即并发读取 eng_b 的上下文数值
        t0 = time.perf_counter()
        # 获取 B 端点的 server
        key_b = hub.make_endpoint_key("TCP", eng_b.host, eng_b.port, "", 0, 0, "", 0)
        server_b = hub._servers.get(key_b)
        assert server_b is not None
        res_b = asyncio.run(server_b._server_context.async_getValues(device_id=2, func_code=3, address=1, count=1))
        duration = time.perf_counter() - t0

        assert res_b == [999]
        assert duration < 0.1, f"跨端点读写被全局锁阻塞耗时过长: {duration:.4f}s"
        stop_thread.join(timeout=3.0)
    finally:
        hub.unregister_and_stop_slave(eng_b)


