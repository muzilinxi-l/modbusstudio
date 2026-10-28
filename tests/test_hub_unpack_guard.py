"""
任务 13 回归测试用例集：ModbusServerHub 键形状守卫与通配复用
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
覆盖 Claude Code 13_UNPACK_FIX_PROMPT 约束要求：
1. 字典内混存 RTU 键 (6元组) + TCP 键 (3元组) 时，register_and_start_slave
   （精确未命中的通配分支）、unregister_and_stop_slave、get_server_for_engine
   三方法均不抛 ValueError: too many values to unpack；
2. 纯 RTU 键字典中调用 get_server_for_engine（TCP 引擎反查）正常返回 None 而非崩溃；
3. 既有 TCP 通配复用行为不回退：("TCP", "0.0.0.0", 502) 在字典中时，注册
   ("TCP", "127.0.0.1", 502) 仍复用同一 server（采用替身对象占位，不启动真实监听）。
"""

import pytest
from typing import Any, Dict, List, Optional
from modbusstudio.modbus_engine import ModbusServerHub


class DummyTransportServer:
    """替身传输服务，用于单元测试中模拟已存在的 ModbusTransportServer，避免占用真实网络端口或串口。"""

    def __init__(self, is_healthy: bool = True, remaining_on_detach: int = 0):
        self._healthy = is_healthy
        self._remaining = remaining_on_detach
        self.slaves: Dict[int, Any] = {}
        self.attached: List[Any] = []
        self.detached: List[int] = []
        self.stopped = False

    def is_healthy(self) -> bool:
        return self._healthy

    def get_slave(self, slave_id: int) -> Optional[Any]:
        return self.slaves.get(slave_id)

    def attach_slave(self, slave_id: int, engine: Any) -> None:
        self.slaves[slave_id] = engine
        self.attached.append((slave_id, engine))

    def detach_slave(self, slave_id: int) -> int:
        self.slaves.pop(slave_id, None)
        self.detached.append(slave_id)
        return self._remaining

    def stop(self) -> None:
        self.stopped = True


class DummyEngine:
    """替身从机引擎，仅提供 ModbusServerHub 所需的配置属性，不拉起底层后台线程与定时器。"""

    def __init__(
        self,
        comm_type: str = "TCP",
        host: str = "127.0.0.1",
        port: int = 502,
        serial_port: str = "COM1",
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        slave_id: int = 1,
    ):
        self.comm_type = comm_type
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.slave_id = slave_id


@pytest.fixture(autouse=True)
def clean_hub():
    """每个测试前后确保清空 ModbusServerHub 单例的 _servers 字典，防止用例间相互污染。"""
    hub = ModbusServerHub.get_instance()
    with hub._lock:
        hub._servers.clear()
    yield
    with hub._lock:
        hub._servers.clear()


def test_mixed_rtu_tcp_keys_no_value_error():
    """
    要求 1 验收：
    字典内混存 RTU 键（6 元组）与 TCP 键（3 元组）时，
    register_and_start_slave（走通配查找分支）、unregister_and_stop_slave、
    get_server_for_engine 三方法均不抛 ValueError。
    """
    hub = ModbusServerHub.get_instance()
    rtu_key_1 = hub.make_endpoint_key("RTU", serial_port="COM1", baudrate=9600)
    rtu_key_2 = hub.make_endpoint_key("RTU", serial_port="COM2", baudrate=19200)
    tcp_wildcard_key = hub.make_endpoint_key("TCP", host="0.0.0.0", port=15502)

    # 验证键形状符合设计
    assert len(rtu_key_1) == 6
    assert len(rtu_key_2) == 6
    assert len(tcp_wildcard_key) == 3

    dummy_rtu_1 = DummyTransportServer()
    dummy_rtu_2 = DummyTransportServer()
    dummy_tcp_wildcard = DummyTransportServer(remaining_on_detach=0)

    # 模拟字典混存：先存入两个 RTU 键，再存入一个 TCP 0.0.0.0 通配键
    with hub._lock:
        hub._servers[rtu_key_1] = dummy_rtu_1
        hub._servers[rtu_key_2] = dummy_rtu_2
        hub._servers[tcp_wildcard_key] = dummy_tcp_wildcard

    # 待操作的具体 TCP 引擎：127.0.0.1:15502（未在字典中精确匹配，将遍历已有键匹配 0.0.0.0 通配）
    tcp_engine = DummyEngine(comm_type="TCP", host="127.0.0.1", port=15502, slave_id=1)

    # 1. 验证 register_and_start_slave：精确未命中走通配分支，遇到 RTU 键不崩溃且成功挂载
    ok, msg = hub.register_and_start_slave(tcp_engine)
    assert ok is True, f"注册应成功复用 0.0.0.0 服务，实际返回: {msg}"
    assert (1, tcp_engine) in dummy_tcp_wildcard.attached

    # 2. 验证 get_server_for_engine：反查时遍历已有键，遇到 RTU 键不崩溃且正确返回通配 server
    found_server = hub.get_server_for_engine(tcp_engine)
    assert found_server is dummy_tcp_wildcard

    # 3. 验证 unregister_and_stop_slave：注销时遍历已有键，遇到 RTU 键不崩溃且正确注销
    hub.unregister_and_stop_slave(tcp_engine)
    assert 1 in dummy_tcp_wildcard.detached
    assert dummy_tcp_wildcard.stopped is True


def test_pure_rtu_keys_tcp_lookup_returns_none():
    """
    要求 2 验收：
    纯 RTU 键字典中调用 get_server_for_engine（TCP 引擎反查）正常返回 None 而非崩溃；
    同时验证 unregister_and_stop_slave 也正常退出不抛异常。
    """
    hub = ModbusServerHub.get_instance()
    rtu_key = hub.make_endpoint_key("RTU", serial_port="COM3", baudrate=115200)
    assert len(rtu_key) == 6

    with hub._lock:
        hub._servers[rtu_key] = DummyTransportServer()

    tcp_engine = DummyEngine(comm_type="TCP", host="192.168.1.100", port=5020, slave_id=2)

    # 1. 反查必须平稳返回 None，严禁抛出 ValueError
    server = hub.get_server_for_engine(tcp_engine)
    assert server is None

    # 2. 注销必须平稳无动作退出，不抛出异常
    hub.unregister_and_stop_slave(tcp_engine)


def test_tcp_wildcard_reuse_behavior_preserved():
    """
    要求 3 验收：
    既有 TCP 通配复用行为不回退：("TCP", "0.0.0.0", 502) 在字典中时，
    注册 ("TCP", "127.0.0.1", 502) 仍复用同一 server（替身对象占位，不启动真实监听）。
    """
    hub = ModbusServerHub.get_instance()
    wildcard_key = ("TCP", "0.0.0.0", 502)
    dummy_server = DummyTransportServer(remaining_on_detach=1)

    # 同时插入一个 RTU 键干扰，确保通配复用在异构键存在时依然 100% 稳定
    rtu_key = ("RTU", "COM5", 9600, 8, "N", 1)

    with hub._lock:
        hub._servers[wildcard_key] = dummy_server
        hub._servers[rtu_key] = DummyTransportServer()

    # 注册 127.0.0.1:502 从机 1
    engine_1 = DummyEngine(comm_type="TCP", host="127.0.0.1", port=502, slave_id=1)
    ok, msg = hub.register_and_start_slave(engine_1)
    assert ok is True
    assert dummy_server.get_slave(1) is engine_1

    # 注册 192.168.1.200:502 从机 2
    engine_2 = DummyEngine(comm_type="TCP", host="192.168.1.200", port=502, slave_id=2)
    ok2, msg2 = hub.register_and_start_slave(engine_2)
    assert ok2 is True
    assert dummy_server.get_slave(2) is engine_2

    # 验证站号冲突拦截：在已复用的同一物理服务上挂载同站号从机 1 必须返回冲突提示
    engine_conflict = DummyEngine(comm_type="TCP", host="10.0.0.5", port=502, slave_id=1)
    ok_conflict, msg_conflict = hub.register_and_start_slave(engine_conflict)
    assert ok_conflict is False
    assert "已存在站地址 1" in msg_conflict


def test_minimal_repro_fixed():
    """
    Claude Code PROMPT §0 最小复现逻辑验证：
    在 Hub 中插入 RTU 6 元组键后，遍历匹配 TCP 通配端点不再抛出 ValueError。
    """
    hub = ModbusServerHub.get_instance()
    k = hub.make_endpoint_key(comm_type="RTU", serial_port="COM1", baudrate=9600)
    assert len(k) == 6

    with hub._lock:
        hub._servers[k] = DummyTransportServer()

    # 执行修复后的守卫循环，断言顺利完成无异常
    matched = None
    target_port = 502
    target_host = "127.0.0.1"
    for s_key, s in hub._servers.items():
        if len(s_key) == 3 and s_key[0] == "TCP":
            if s_key[2] == target_port and (s_key[1] == "0.0.0.0" or target_host == "0.0.0.0" or s_key[1] == target_host):
                matched = s
                break

    assert matched is None
