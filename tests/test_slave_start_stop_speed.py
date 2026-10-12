"""从机启动 / 停止 / 仿真线程退出提速专项测试 (04_SLAVE_START_STOP_SPEED).

覆盖 3 个核心计时用例：
- 用例 A (test_transport_server_start_event_driven_fast)：启动等待事件驱动，耗时 < 0.4s
- 用例 B (test_transport_server_stop_fast)：停止清理快速完成，耗时 < 1.5s
- 用例 C (test_slave_engine_stop_wakes_sim_thread_immediately)：仿真线程被 Event 立即唤醒退出，stop() 耗时 < 0.6s
"""

import time

from modbusstudio.modbus_engine import (
    ModbusServerHub,
    ModbusSlaveEngine,
    ModbusTransportServer,
)

TEST_PORT = 5097


def _cleanup_hub_port(port: int = TEST_PORT) -> None:
    """确保测试端口在 ModbusServerHub 单例中无残留."""
    hub = ModbusServerHub.get_instance()
    to_stop = []
    with hub._lock:
        for key in list(hub._servers.keys()):
            if len(key) >= 3 and key[0] == "TCP" and key[2] == port:
                srv = hub._servers.pop(key, None)
                if srv is not None:
                    to_stop.append(srv)
    for srv in to_stop:
        try:
            srv.stop()
        except Exception:
            pass


def test_transport_server_start_event_driven_fast():
    """用例 A：ModbusTransportServer.start() 事件驱动就绪，不盲等 0.9s，耗时 < 0.4s."""
    _cleanup_hub_port(TEST_PORT)
    srv = ModbusTransportServer(("TCP", "127.0.0.1", TEST_PORT))
    try:
        t0 = time.time()
        srv.start()
        elapsed = time.time() - t0
        print(f"\n[TIMING-A] ModbusTransportServer.start() elapsed = {elapsed * 1000:.2f} ms ({elapsed:.4f} s)")
        assert srv.is_healthy(), "启动后 ModbusTransportServer 应处于健康监听状态"
        assert elapsed < 0.4, f"start() 耗时 {elapsed:.4f}s 超过 0.4s 阈值（旧实现盲等 ~0.9s）"
    finally:
        srv.stop()
        _cleanup_hub_port(TEST_PORT)


def test_transport_server_stop_fast():
    """用例 B：ModbusTransportServer.stop() 快速回收资源，耗时 < 1.5s."""
    _cleanup_hub_port(TEST_PORT)
    srv = ModbusTransportServer(("TCP", "127.0.0.1", TEST_PORT))
    try:
        srv.start()
        assert srv.is_healthy()
        t0 = time.time()
        srv.stop()
        elapsed = time.time() - t0
        print(f"\n[TIMING-B] ModbusTransportServer.stop() elapsed = {elapsed * 1000:.2f} ms ({elapsed:.4f} s)")
        assert not srv.is_running
        assert elapsed < 1.5, f"stop() 耗时 {elapsed:.4f}s 超过 1.5s 阈值"
    finally:
        srv.stop()
        _cleanup_hub_port(TEST_PORT)


def test_slave_engine_stop_wakes_sim_thread_immediately():
    """用例 C：ModbusSlaveEngine.stop() 通过 Event 立即唤醒仿真线程退出，耗时 < 0.6s."""
    _cleanup_hub_port(TEST_PORT)
    engine = ModbusSlaveEngine(comm_type="TCP", host="127.0.0.1", port=TEST_PORT, slave_id=1)
    try:
        engine.start()
        assert engine.is_healthy()
        # 稍等 50ms 确保后台 _sim_thread 已进入 _sim_stop_evt.wait(1)
        time.sleep(0.05)
        t0 = time.time()
        engine.stop()
        elapsed = time.time() - t0
        print(f"\n[TIMING-C] ModbusSlaveEngine.stop() elapsed = {elapsed * 1000:.2f} ms ({elapsed:.4f} s)")
        assert not engine.is_running
        assert elapsed < 0.6, f"engine.stop() 耗时 {elapsed:.4f}s 超过 0.6s 阈值（旧实现盲等 ~1.0s）"
    finally:
        try:
            engine.stop()
        except Exception:
            pass
        _cleanup_hub_port(TEST_PORT)
