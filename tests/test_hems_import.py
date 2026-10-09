"""
HEMS 业务数据库适配与点表导入单元测试
"""

import json
import sqlite3
import pytest
from modbusstudio.hems_db_loader import HemsDatabase, map_db_format_to_data_type, map_db_format_to_byte_order
from modbusstudio.modbus_codec import ByteOrderMode, ModbusDataType


def test_map_db_format_to_data_type():
    """测试 HEMS 数据库数字格式编码与 Modbus 数据类型映射"""
    assert map_db_format_to_data_type(2) == ModbusDataType.BOOL
    assert map_db_format_to_data_type(7) == ModbusDataType.UINT16
    assert map_db_format_to_data_type(9) == ModbusDataType.INT32
    assert map_db_format_to_data_type(13) == ModbusDataType.UINT32
    assert map_db_format_to_data_type(33) == ModbusDataType.FLOAT32
    assert map_db_format_to_data_type(35) == ModbusDataType.FLOAT32
    # 变位测试
    assert map_db_format_to_byte_order(35) == ByteOrderMode.CDAB


def test_hems_database_loading_memory(tmp_path):
    """测试模拟数据库的 app 表读取与设备模型生成"""
    db_file = tmp_path / "test_hems.cdb"
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("""
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

    sample_more = {
        "Pollings": [],
        "Realtime Variables": [
            {
                "Register Start Address": 10,
                "Chinese Name": "直流母线电压",
                "English Name": "U_dc",
                "Function Code": "3",
                "Variable Transfer Format": 35,  # Float 3 (CDAB)
            }
        ]
    }

    cursor.execute("""
        INSERT INTO app (Id, Type, [English Name], [Chinese Name], [Controller Library], [Referred Model], [Local Parameters], More, enable)
        VALUES (1, 1, 'Inaccess_PCS', '英博PCS', 'MbMaster', 'PCS_Model', '[]', ?, 1)
    """, (json.dumps(sample_more),))
    conn.commit()
    conn.close()

    db = HemsDatabase(str(db_file))
    apps = db.load_modbus_apps()

    assert len(apps) == 1
    app_model = apps[0]
    assert app_model.app_id == 1
    assert app_model.english_name == "Inaccess_PCS"
    assert app_model.is_south_master is True

    points = app_model.extract_studio_points()
    assert len(points) == 1
    assert points[0]["address"] == 10
    assert points[0]["desc"] == "直流母线电压"
    assert points[0]["data_type"] == ModbusDataType.FLOAT32
    assert points[0]["byte_order"] == ByteOrderMode.CDAB


def test_hems_pairing_engine_load_rules(tmp_path):
    """测试 HEMS 业务配对引擎加载与属性访问"""
    from modbusstudio.hems_pairing import HemsPairingEngine
    db_file = tmp_path / "test_pairing.cdb"
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
    # 南向应用
    more_south = {
        "Realtime Variables": [
            {"Register Start Address": 100, "English Name": "Active_Power", "Chinese Name": "有功功率", "Function Code": "3", "Variable Transfer Format": 35}
        ]
    }
    # 北向应用
    more_north = {
        "Referred Variables": [
            {
                "Register Address": 200,
                "English Name": "PCS|Active_Power",
                "Function Code": 4,
                "Slave Id": 1,
                "Variable Transfer Format": "Float 3",
            }
        ]
    }
    cur.execute("INSERT INTO app (Id, Type, [English Name], [Chinese Name], [Controller Library], More, enable) VALUES (1, 1, 'PCS', '英博PCS', 'MbMaster', ?, 1)", (json.dumps(more_south),))
    cur.execute("INSERT INTO app (Id, Type, [English Name], [Chinese Name], [Controller Library], More, enable) VALUES (2, 2, 'North_Forward', '北向转发', 'MbSlave', ?, 1)", (json.dumps(more_north),))
    conn.commit()
    conn.close()

    engine = HemsPairingEngine(str(db_file))
    rules = engine.load_rules()
    assert len(rules) == 1
    r = rules[0]
    assert r.target_register == 200
    assert r.src_app_name == "PCS"
    assert r.src_var_name == "Active_Power"
    assert r.src_address == 100
    assert "Active_Power" in r.source_var
    assert "200" in r.target_var
    assert r.rule_id != ""


def test_datatype_and_point_service_float32():
    """测试 DataType 严格规范化与 PointService 浮点数/双精度字长不丢失"""
    from modbusstudio.models import DataType, ByteOrder, AreaType
    from modbusstudio.services import PointService

    assert DataType.normalize(DataType.FLOAT32) == DataType.FLOAT32
    assert DataType.normalize("FLOAT32") == DataType.FLOAT32
    assert DataType.normalize("DataType.FLOAT32") == DataType.FLOAT32
    assert ByteOrder.normalize(ByteOrder.CDAB) == ByteOrder.CDAB

    ok, pt, err = PointService.validate_and_build_point(
        address=10,
        description="直流侧总电压",
        area=AreaType.HOLDING,
        data_type=DataType.FLOAT32,
        byte_order=ByteOrder.CDAB,
        val_input=752.4,
    )
    assert ok is True
    assert pt.data_type == DataType.FLOAT32
    assert pt.byte_order == ByteOrder.CDAB
    assert pt.value == 752.4
    assert pt.register_count == 2
