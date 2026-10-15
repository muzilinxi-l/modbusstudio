"""tests/test_session_state_persistence.py
会话状态持久化全量自检验收集：
1. SlaveDevice 往返序列化一致性（to_dict -> from_dict -> to_dict）；
2. 无头 AppCore 存取恢复（验证包含正弦/方波等动态模拟规则点表的无损还原）；
3. 业务数据库文件失效时的优雅回落与独立从机恢复容错。
"""

import json
import os
import pytest
from modbusstudio.app import (
    ModbusStudioAppCore,
    save_session_state,
    load_session_state,
    SAVE_FILENAME,
    SCHEMA_VERSION,
)
from modbusstudio.models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    ModbusPoint,
    SimRule,
    SlaveDevice,
)
from modbusstudio.services import PointService


def test_slave_device_to_from_dict_roundtrip():
    """验证 SlaveDevice 序列化与反序列化往返一致性（包含复合键字符串化与动态模拟规则）。"""
    cfg = ConnectionConfig(
        comm_type=CommType.TCP,
        host="127.0.0.1",
        port=5020,
        unit_id=2,
    )
    dev = SlaveDevice(
        id="slave_test_1",
        name="测试从机_1",
        conn_config=cfg,
        db_file="C:/test/sample.cdb",
        db_device_id=88,
        db_device_name="TestDevice",
    )

    # 挂载多个典型点位（含固定、正弦波、方波、UINT16 与 FLOAT32）
    ok1, pt1, _ = PointService.validate_and_build_point(
        address=10,
        description="功率设定 (正弦波)",
        area="4x_HoldingRegister",
        data_type="FLOAT32",
        byte_order="CDAB",
        val_input=125.5,
        sim_rule="正弦波",
    )
    assert ok1 and pt1 is not None
    dev.add_point(pt1)

    ok2, pt2, _ = PointService.validate_and_build_point(
        address=20,
        description="告警状态 (方波)",
        area="0x_Coil",
        data_type="BOOL",
        byte_order="ABCD",
        val_input=1,
        sim_rule="方波",
    )
    assert ok2 and pt2 is not None
    dev.add_point(pt2)

    # 1. 序列化为 dict
    d1 = dev.to_dict()
    assert d1["id"] == "slave_test_1"
    assert d1["name"] == "测试从机_1"
    assert d1["db_file"] == "C:/test/sample.cdb"
    assert d1["db_device_id"] == 88
    assert "4x_HoldingRegister|10" in d1["points"]
    assert "0x_Coil|20" in d1["points"]
    assert d1["points"]["4x_HoldingRegister|10"]["sim_rule"] == "正弦波"
    assert d1["points"]["4x_HoldingRegister|10"]["value"] == 125.5

    # 2. 从 dict 还原
    dev2 = SlaveDevice.from_dict(d1)
    d2 = dev2.to_dict()

    # 3. 往返一致性断言
    assert d1 == d2
    assert (AreaType.HOLDING.value, 10) in dev2.points
    assert (AreaType.COIL.value, 20) in dev2.points
    restored_pt = dev2.points[(AreaType.HOLDING.value, 10)]
    assert restored_pt.sim_rule == "正弦波"
    assert restored_pt.value == 125.5
    assert restored_pt.byte_order == ByteOrder.CDAB


def test_app_core_save_and_load_session_state(tmp_path):
    """验证无头 AppCore 会话状态持久化保存与恢复。"""
    state_file = str(tmp_path / "test_session_state.json")
    custom_db = str(tmp_path / "custom_test.cdb")
    # 创建一个空文件模拟存在的数据库文件
    with open(custom_db, "w", encoding="utf-8") as f:
        f.write("mock db")

    # 启动一个不加载默认会话的干净 AppCore
    app = ModbusStudioAppCore(restore_session=False)
    app.db_path = custom_db

    # 清空默认从机，加入定制的从机
    app.slave_service.clear_all_devices()
    cfg = ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=5030, unit_id=5)
    custom_dev = SlaveDevice(id="custom_slave", name="定制储能变流器", conn_config=cfg)

    ok, pt, _ = PointService.validate_and_build_point(
        address=100,
        description="直流母线电压",
        area="4x_HoldingRegister",
        data_type="FLOAT32",
        byte_order="CDAB",
        val_input=750.8,
        sim_rule="正弦波",
    )
    assert ok and pt is not None
    custom_dev.add_point(pt)
    app.slave_service.register_device(custom_dev)

    # 保存会话状态到自定义文件
    save_ok = app.save_session_state(path=state_file)
    assert save_ok is True
    assert os.path.exists(state_file)

    # 再次保存，验证 .bak 备份机制生成
    save_ok2 = app.save_session_state(path=state_file)
    assert save_ok2 is True
    assert os.path.exists(state_file + ".bak")

    # 启动第二个 AppCore 实例并指向该文件进行恢复
    app2 = ModbusStudioAppCore(restore_session=True, session_path=state_file)

    # 验证数据库路径恢复
    assert app2.db_path == os.path.abspath(custom_db)

    # 验证从机与点表恢复
    recovered_devices = app2.slave_service.get_devices()
    assert len(recovered_devices) == 1
    rec_dev = recovered_devices[0]
    assert rec_dev.id == "custom_slave"
    assert rec_dev.name == "定制储能变流器"
    assert rec_dev.conn_config.port == 5030
    assert rec_dev.conn_config.unit_id == 5

    # 验证点表与模拟规则无损还原
    pt_key = (AreaType.HOLDING.value, 100)
    assert pt_key in rec_dev.points
    rec_pt = rec_dev.points[pt_key]
    assert rec_pt.description == "直流母线电压"
    assert rec_pt.value == 750.8
    assert rec_pt.sim_rule == "正弦波"
    assert rec_pt.byte_order == ByteOrder.CDAB


def test_session_state_db_file_missing_fallback(tmp_path):
    """验证当存档中的业务数据库文件在磁盘上已被删除或失效时，AppCore 能优雅回落，且从机点表正常保留。"""
    state_file = str(tmp_path / "broken_db_session.json")
    non_existent_db = "C:/path_not_exists_2026/broken_hems.cdb"

    # 手工构造一个 last_db_path 失效的 JSON 存档
    payload = {
        "schema_version": SCHEMA_VERSION,
        "last_db_path": non_existent_db,
        "slaves": [
            {
                "id": "slave_preserved",
                "name": "保留从机",
                "conn_config": {
                    "comm_type": "TCP",
                    "host": "127.0.0.1",
                    "port": 5040,
                    "com_port": "COM1",
                    "baudrate": 9600,
                    "data_bits": 8,
                    "stop_bits": 1,
                    "parity": "N",
                    "unit_id": 3,
                    "timeout": 1.0,
                    "reconnect_delay": 0.5,
                },
                "db_file": non_existent_db,
                "db_device_id": 99,
                "db_device_name": "PCS-99",
                "points": {
                    "4x_HoldingRegister|1": {
                        "address": 1,
                        "description": "有功功率给定",
                        "area": "4x_HoldingRegister",
                        "data_type": "INT16",
                        "byte_order": "ABCD",
                        "value": 300,
                        "sim_rule": "方波",
                        "raw_hex": "0x012C",
                        "string_length": 16,
                        "field_name": "ActivePower",
                        "scale": 1.0,
                        "unit": "kW",
                    }
                },
            }
        ],
        "saved_at": "2026-10-15T10:00:00+08:00",
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # 实例化 AppCore 并加载该损坏路径存档
    app = ModbusStudioAppCore(restore_session=True, session_path=state_file)

    # 1. 验证 db_path 回落到了 _probe_default_db() 的结果（不等于失效的 non_existent_db）
    assert app.db_path != non_existent_db
    # 正常环境下若 extra/ 下有 hems.cdb 则命中，若无则为 None
    expected_probe = app._probe_default_db()
    assert app.db_path == expected_probe

    # 2. 验证从机及点表独立恢复成功，并未因库路径失效而被丢弃
    devs = app.slave_service.get_devices()
    assert len(devs) == 1
    assert devs[0].id == "slave_preserved"
    assert (AreaType.HOLDING.value, 1) in devs[0].points
    pt = devs[0].points[(AreaType.HOLDING.value, 1)]
    assert pt.value == 300
    assert pt.sim_rule == "方波"

    # 3. 验证日志服务记录了失效提示文案
    entries = app.logging_service.drain_batch(100)
    sys_logs = [e.message for e in entries if e.direction == "SYS"]

    expected_msg_sub = f"存档中记录的库文件 [{non_existent_db}] 已失效，本次按默认库恢复"
    assert any(expected_msg_sub in msg for msg in sys_logs), f"未在日志中发现失效提示: {sys_logs}"
