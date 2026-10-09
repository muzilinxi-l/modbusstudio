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
