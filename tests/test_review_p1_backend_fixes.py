"""深度 Review 第 2 批核心 P1（Hub/引擎并发、HEMS 加载/配对、模型与对账边界）回归测试。"""

import json
import math
import socket
import sqlite3

from modbusstudio.hems_db_loader import HemsAppModel, map_db_format_to_data_type
from modbusstudio.hems_pairing import HemsPairingEngine
from modbusstudio.modbus_codec import ByteOrderMode, ModbusDataType
from modbusstudio.modbus_engine import ModbusServerHub, ModbusSlaveEngine
from modbusstudio.models import AreaType, SlaveDevice, _parse_float
from modbusstudio.services import SlaveService
from modbusstudio.tools import reconcile


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_p1_hub_port_bind_failure_not_false_success_and_wildcard_direction():
    """验证端口被占用时 register_and_start_slave 真实返回失败；已有 127.0.0.1 时拒绝 0.0.0.0 错向复用。"""
    hub = ModbusServerHub.get_instance()

    # 1. 外部 socket 提前独占端口：启动必须失败且不残留坏 server
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    eng_busy = ModbusSlaveEngine(comm_type="TCP", host="127.0.0.1", port=busy_port, slave_id=1)
    try:
        ok, err = hub.register_and_start_slave(eng_busy)
        assert ok is False, f"端口被占用时不应假报成功: {err}"
        assert ("TCP", "127.0.0.1", busy_port) not in hub._servers
    finally:
        blocker.close()

    # 2. 先启动具体 IP 127.0.0.1:port，再注册 0.0.0.0:port 必须拒绝误复用窄监听
    port = _pick_free_port()
    eng_local = ModbusSlaveEngine(comm_type="TCP", host="127.0.0.1", port=port, slave_id=1)
    eng_wild = ModbusSlaveEngine(comm_type="TCP", host="0.0.0.0", port=port, slave_id=2)
    try:
        ok1, msg1 = hub.register_and_start_slave(eng_local)
        assert ok1 is True, msg1
        ok2, msg2 = hub.register_and_start_slave(eng_wild)
        assert ok2 is False, f"不应把 0.0.0.0 误复用到 127.0.0.1 窄监听: {msg2}"
    finally:
        hub.unregister_and_stop_slave(eng_local)
        hub.unregister_and_stop_slave(eng_wild)


def test_p1_read_typed_value_float64_alias_and_list_available_ports():
    """验证 read_typed_value 兼容 'FLOAT64' 别名读取 4 寄存器，且 SlaveService.list_available_ports 可用。"""
    eng = ModbusSlaveEngine(address_base=1)
    eng.write_typed_value(AreaType.HOLDING.value, 10, 123456.789012, "FLOAT64", ByteOrderMode.ABCD)
    val = eng.read_typed_value(AreaType.HOLDING.value, 10, "FLOAT64", ByteOrderMode.ABCD)
    assert math.isclose(val, 123456.789012, rel_tol=1e-9)

    ports = SlaveService.list_available_ports()
    assert isinstance(ports, list) and len(ports) >= 1


def test_p1_hems_64bit_format_null_guards_and_large_int_string_init():
    """验证 HEMS 64 位非标格式顺序、JSON null 防崩溃、STRING 初始值与 64 位大整数精度保留。"""
    assert map_db_format_to_data_type("unsigned int64") == ModbusDataType.UINT64
    assert map_db_format_to_data_type("signed int64") == ModbusDataType.INT64
    assert map_db_format_to_data_type("float64") == ModbusDataType.DOUBLE64

    model = HemsAppModel(
        app_id=99,
        app_type=1,
        english_name="PCS_NULL_TEST",
        chinese_name="测试设备",
        model_name="PCS",
        controller_lib="Mb",
        enable=1,
        raw_more={
            "Pollings": None,
            "Realtime Variables": [
                None,
                {
                    "Register Start Address": "0",
                    "English Name": None,
                    "Chinese Name": None,
                    "Function Code": "3",
                    "Variable Transfer Format": "string",
                    "Value": "SN20260001",
                },
                {
                    "Register Start Address": "10",
                    "English Name": "BigEnergy",
                    "Chinese Name": "大累计电量",
                    "Function Code": "3",
                    "Variable Transfer Format": "Unsigned int64 1",
                    "Value": "9007199254740993",
                },
            ],
        },
    )
    pts = model.extract_studio_points()
    assert len(pts) == 2
    assert pts[0]["current_val"] == "SN20260001"
    assert pts[1]["current_val"] == 9007199254740993


def test_p1_hems_pairing_fc_rule_id_null_guard_and_slave_id_fallback(tmp_path):
    """验证 HemsPairingEngine 防空名/北向自污染、rule_id 含 FC 防撞、JSON null 容错及站号回退。"""
    db_file = tmp_path / "pair.cdb"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            'CREATE TABLE app (Id INTEGER PRIMARY KEY, Type INTEGER, "English Name" TEXT, '
            '"Chinese Name" TEXT, "Referred Model" TEXT, "Controller Library" TEXT, '
            'enable INTEGER, More TEXT, "Local Parameters" TEXT)'
        )
        south_more = {
            "Realtime Variables": [
                {"English Name": "V_dc", "Chinese Name": "直流电压", "Register Start Address": "0", "Variable Transfer Format": "Float 3"},
            ]
        }
        north_more = {
            "Referred Variables": [
                None,
                {"English Name": None, "Register Address": "1"},
                {"English Name": "PCS1|V_dc", "Register Address": "0", "Function Code": "3"},
                {"English Name": "PCS1|V_dc", "Register Address": "0", "Function Code": "4"},
            ]
        }
        conn.execute(
            "INSERT INTO app VALUES (1, 1, 'PCS1', '', 'PCS', 'Modbus', 1, ?, '{}')",
            (json.dumps(south_more),),
        )
        conn.execute(
            "INSERT INTO app VALUES (2, 2, 'North1', '北向1', 'North', 'Modbus', 1, ?, ?)",
            (json.dumps(north_more), json.dumps([{"English Name": "Slave ID", "Value": "7"}])),
        )
        conn.commit()
    finally:
        conn.close()

    eng = HemsPairingEngine(str(db_file))
    rules = eng.load_rules()
    assert len(rules) == 3
    matched_rules = [r for r in rules if r.src_app_name == "PCS1"]
    assert len(matched_rules) == 2
    assert matched_rules[0].rule_id != matched_rules[1].rule_id
    assert all(r.target_slave_id == 7 for r in rules)


def test_p1_models_nan_float_guard_and_reconcile_non_dict_session(tmp_path):
    """验证 _parse_float 拦截 NaN/Inf、SlaveDevice.from_dict 容忍非字典 points、reconcile 拒绝非字典存档。"""
    assert _parse_float("NaN", 1.0) == 1.0
    assert _parse_float("inf", 1.0) == 1.0
    assert _parse_float("-inf", 1.0) == 1.0

    dev = SlaveDevice.from_dict({"id": "d1", "name": "n1", "points": ["not", "a", "dict"]})
    assert dev.points == {}

    bad_session = tmp_path / "bad_session.json"
    bad_session.write_text("[1, 2, 3]", encoding="utf-8")
    slaves, warnings = reconcile.load_slaves(str(bad_session))
    assert slaves == []
    assert len(warnings) == 1
