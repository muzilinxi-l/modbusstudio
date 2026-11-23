"""P1-1 ~ P1-6 稳态与一致性修复专项回归测试。"""

import csv
import json
import sqlite3
import threading
import time
import pytest

from modbusstudio.app import ModbusStudioAppCore, load_session_state, save_session_state
from modbusstudio.hems_db_loader import HemsAppModel, HemsDatabase
from modbusstudio.hems_pairing import HemsPairingEngine
from modbusstudio.modbus_codec import (
    ByteOrderMode,
    ModbusDataType,
    clamp_value_to_type,
    decode_value,
    encode_value,
)
from modbusstudio.modbus_engine import ModbusPollEngine, ModbusSlaveEngine
from modbusstudio.models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    SlaveDevice,
)
from modbusstudio.services import LoggingService, PointService, SlaveService


def test_p1_1_external_master_write_syncs_device_model(tmp_path):
    """P1-1：外部主站通过 FC16/FC05 写入运行中的从机后，领域模型与仿真点表实时反向同步。"""
    port = 25201
    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    slave_svc = SlaveService(log_svc)
    dev = SlaveDevice(
        id="dev_ext_write",
        name="外写同步从机",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=port, unit_id=3),
    )
    ok_f, pt_f, _ = PointService.validate_and_build_point(
        address=10,
        description="有功功率设定",
        area=AreaType.HOLDING,
        data_type=DataType.FLOAT32,
        byte_order=ByteOrder.CDAB,
        val_input=10.0,
        sim_rule="固定",
    )
    ok_c, pt_c, _ = PointService.validate_and_build_point(
        address=1,
        description="合闸遥控",
        area=AreaType.COIL,
        data_type=DataType.BOOL,
        byte_order=ByteOrder.ABCD,
        val_input=0,
        sim_rule="固定",
    )
    assert ok_f and pt_f and ok_c and pt_c
    dev.add_point(pt_f)
    dev.add_point(pt_c)
    slave_svc.register_device(dev)

    poll = ModbusPollEngine(host="127.0.0.1", port=port, slave_id=3)
    try:
        started, msg = slave_svc.start_slave(dev.id)
        assert started, msg
        assert poll.connect_tcp("127.0.0.1", port)

        ok_w1, msg_w1 = poll.write_typed(AreaType.HOLDING.value, 10, 225.5, ModbusDataType.FLOAT32, ByteOrderMode.CDAB)
        assert ok_w1, msg_w1
        ok_w2, msg_w2 = poll.write_single_coil(1, True)
        assert ok_w2, msg_w2

        # 验证从机领域模型与引擎点表均已反向同步
        assert pt_f.value == pytest.approx(225.5, rel=1e-4)
        expected_regs = encode_value(225.5, ModbusDataType.FLOAT32, ByteOrderMode.CDAB)
        assert pt_f.raw_hex == " ".join(f"0x{r:04X}" for r in expected_regs)
        assert pt_c.value == 1
        assert pt_c.raw_hex == "0x01"
    finally:
        poll.disconnect()
        slave_svc.stop_slave(dev.id)
        log_svc.close()


def test_p1_2_logging_service_concurrent_csv_writes_and_close_safety(tmp_path):
    """P1-2：多线程并发写 LoggingService CSV 无损坏，close() 后继续 post 不抛异常。"""
    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    errors = []

    def worker(tid: int):
        try:
            for i in range(50):
                log_svc.post("TX", tid, f"msg-{tid}-{i}", "01 03 00 01")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    csv_path = log_svc.csv_path
    assert csv_path is not None
    log_svc.close()
    # close() 后后台残留线程再次投递不得报错
    log_svc.post("SYS", 0, "after-close-should-ignore")

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 1 + 8 * 50
    assert all(len(r) == 5 for r in rows)


def test_p1_3_remove_and_clear_runtime_points(tmp_path):
    """P1-3：运行中移除或清空点位时同步清理底层引擎 points，阻止已删点继续模拟。"""
    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    slave_svc = SlaveService(log_svc)
    dev = SlaveDevice(
        id="dev_rm_rt",
        name="删点同步从机",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=25203, unit_id=1),
    )
    _, p1, _ = PointService.validate_and_build_point(1, "P1", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 10, "步进递增")
    _, p2, _ = PointService.validate_and_build_point(2, "P2", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 20, "步进递增")
    dev.add_point(p1)
    dev.add_point(p2)
    slave_svc.register_device(dev)

    try:
        ok, msg = slave_svc.start_slave(dev.id)
        assert ok, msg
        engine = slave_svc._active_engines[dev.id]
        assert (AreaType.HOLDING.value, 1) in engine.points
        assert 1 in engine.points

        slave_svc.remove_point_from_runtime(dev.id, AreaType.HOLDING, 1)
        assert (AreaType.HOLDING.value, 1) not in engine.points
        assert 1 not in engine.points
        assert (AreaType.HOLDING.value, 2) in engine.points

        slave_svc.clear_runtime_points(dev.id)
        assert len(engine.points) == 0
    finally:
        slave_svc.stop_slave(dev.id)
        log_svc.close()


def test_p1_4_hems_zero_address_pairing_fallback_and_roundtrip_save(tmp_path):
    """P1-4：HEMS 保留 0 号地址、配对容忍 FC03 与回退源格式、save_app_points 写回保序不丢英文名。"""
    db_file = tmp_path / "hems_p1.cdb"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE app (
            Id INTEGER PRIMARY KEY,
            Type INTEGER,
            [English Name] TEXT,
            [Chinese Name] TEXT,
            [Controller Library] TEXT,
            [Referred Model] TEXT,
            [Local Parameters] TEXT,
            More TEXT,
            enable INTEGER
        )
    """)
    south_more = {
        "Pollings": [],
        "Realtime Variables": [
            {
                "Register Start Address": 0,
                "English Name": "ZeroAddrVolt",
                "Chinese Name": "零号地址电压",
                "Function Code": "3",
                "Variable Transfer Format": "Float 3",
                "Scale Factor": "bad_scale",
                "Unit": "V",
            }
        ],
    }
    north_more = {
        "Pollings": [],
        "Referred Variables": [
            {
                "Register Address": 0,
                "English Name": "PCS_South|ZeroAddrVolt",
                "Function Code": "FC03",
                "Slave Id": "1",
                "Variable Transfer Format": "",
            }
        ],
    }
    cur.execute(
        "INSERT INTO app VALUES (1, 1, 'PCS_South', '南向PCS', 'MbMaster', 'M1', '[]', ?, 1)",
        (json.dumps(south_more),),
    )
    cur.execute(
        "INSERT INTO app VALUES (2, 2, 'North_Svc', '北向服务', 'MbSlave', 'M2', '[]', ?, 1)",
        (json.dumps(north_more),),
    )
    conn.commit()
    conn.close()

    db = HemsDatabase(str(db_file))
    apps = db.load_modbus_apps()
    south = next(a for a in apps if a.app_id == 1)
    pts = south.extract_studio_points()
    assert len(pts) == 1
    assert pts[0]["address"] == 0
    assert pts[0]["scale"] == 1.0
    assert pts[0]["field_name"] == "ZeroAddrVolt"
    assert pts[0]["unit"] == "V"

    pairing = HemsPairingEngine(str(db_file))
    rules = pairing.load_rules()
    assert len(rules) == 1
    assert rules[0].target_register == 0
    assert rules[0].src_address == 0
    assert rules[0].target_fc == 3
    # 北向未配格式时正确回退南向源格式 Float 3 (FLOAT32 + CDAB)
    assert rules[0].data_type == ModbusDataType.FLOAT32
    assert rules[0].byte_order == ByteOrderMode.CDAB

    # 写回数据库后再重新加载，应保留原英文名与 Float 3 (CDAB) 字节序
    assert db.save_app_points(1, pts) is True
    reloaded = next(a for a in db.load_modbus_apps() if a.app_id == 1)
    reloaded_pts = reloaded.extract_studio_points()
    assert len(reloaded_pts) == 1
    assert reloaded_pts[0]["field_name"] == "ZeroAddrVolt"
    assert reloaded_pts[0]["data_type"] == ModbusDataType.FLOAT32
    assert reloaded_pts[0]["byte_order"] == ByteOrderMode.CDAB


def test_p1_5_codec_nan_string_length_and_point_clamping():
    """P1-5：验证 NaN/inf 钳位、STRING 长度防越界、PointService 上下界钳位与 address_base=0 一致性。"""
    assert clamp_value_to_type("NaN", ModbusDataType.FLOAT32) == 0.0
    assert clamp_value_to_type(float("inf"), ModbusDataType.FLOAT32) == pytest.approx(3.402823466e38)
    assert clamp_value_to_type(float("-inf"), ModbusDataType.FLOAT32) == pytest.approx(-3.402823466e38)

    # STRING 严格受 string_length 限制，不溢出覆写相邻寄存器
    regs = encode_value("ABCDEFGHIJ", ModbusDataType.STRING, ByteOrderMode.ABCD, string_length=4)
    assert len(regs) == 2
    assert decode_value(regs, ModbusDataType.STRING, ByteOrderMode.ABCD, string_length=4) == "ABCD"
    assert decode_value(regs, ModbusDataType.STRING, ByteOrderMode.ABCD, string_length=2) == "AB"

    # PointService 对越界 UINT16 钳位，保证 value 与 raw_hex 一致
    ok, pt, _ = PointService.validate_and_build_point(1, "Over", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 99999)
    assert ok and pt is not None
    assert pt.value == 65535
    assert pt.raw_hex == "0xFFFF"

    # STRING 点位透传 string_length 计算 raw_hex
    ok_s, pt_s, _ = PointService.validate_and_build_point(
        10, "SN", AreaType.HOLDING, DataType.STRING, ByteOrder.ABCD, "SN01", string_length=8
    )
    assert ok_s and pt_s is not None
    assert len(pt_s.raw_hex.split()) == 4

    # address_base=0 时本地读写地址 0 落入数组下标 0
    eng0 = ModbusSlaveEngine(address_base=0)
    eng0.write_raw_values(AreaType.HOLDING.value, 0, [4321])
    assert eng0.read_raw_values(AreaType.HOLDING.value, 0, 1) == [4321]


def test_p1_6_atomic_session_save_bak_fallback_and_dirty_from_dict(tmp_path):
    """P1-6：验证会话原子写入（无 .bak 残留，备份归档至 logs/*.backup.json）、主文件损坏自动回退恢复、以及脏字段反序列化容错。"""
    state_file = str(tmp_path / "session_state.json")
    app = ModbusStudioAppCore(restore_session=False, session_path=state_file)
    try:
        assert app.save_session_state() is True
        # 二次保存将旧版归档到 logs/session_state.backup.json，不在根目录产生 .bak 文件
        assert app.save_session_state() is True
        assert not (tmp_path / "session_state.json.bak").exists()
        assert (tmp_path / "logs" / "session_state.backup.json").exists()
        assert not (tmp_path / "session_state.json.tmp").exists()

        # 破坏主存档文件，验证 load_session_state 自动从 logs 备份恢复
        with open(state_file, "w", encoding="utf-8") as f:
            f.write("{truncated_json")

        recovered = load_session_state(state_file)
        assert recovered is not None
        assert len(recovered.get("slaves", [])) >= 1
    finally:
        app.shutdown()

    # 脏字段反序列化容错：单个坏 scale/baudrate/unit_id 不丢弃整个点位或从机
    dirty = {
        "id": "dirty_slave",
        "name": "容错从机",
        "conn_config": {
            "comm_type": "TCP",
            "host": "127.0.0.1",
            "port": 5060,
            "baudrate": "bad_baud",
            "unit_id": "bad_uid",
            "timeout": "bad_to",
        },
        "points": {
            "4x_HoldingRegister|5": {
                "address": 5,
                "description": "坏系数点位",
                "area": "4x_HoldingRegister",
                "data_type": "UINT16",
                "byte_order": "ABCD",
                "value": 100,
                "scale": "not_a_float",
                "string_length": "bad_len",
            }
        },
    }
    dev = SlaveDevice.from_dict(dirty)
    assert dev.conn_config.baudrate == 9600
    assert dev.conn_config.unit_id == 1
    assert dev.conn_config.timeout == 1.0
    pt = dev.get_point(AreaType.HOLDING, 5)
    assert pt is not None
    assert pt.scale == 1.0
    assert pt.string_length == 16
