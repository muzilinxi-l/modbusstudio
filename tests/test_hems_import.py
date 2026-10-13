"""
HEMS 业务数据库适配与点表导入单元测试
"""

import json
import sqlite3
import pytest
from modbusstudio.hems_db_loader import HemsDatabase, map_db_format_to_data_type, map_db_format_to_byte_order
from modbusstudio.modbus_codec import ByteOrderMode, ModbusDataType


def test_map_db_format_to_data_type():
    """测试 HEMS 数据库数字格式编码 (0~43) 与 Modbus 数据类型映射"""
    # 0 = Invalid -> 兜底 INT16
    assert map_db_format_to_data_type(0) == ModbusDataType.INT16
    assert map_db_format_to_data_type("0") == ModbusDataType.INT16
    assert map_db_format_to_data_type("Invalid") == ModbusDataType.INT16
    # 1 = Bit -> BOOL
    assert map_db_format_to_data_type(1) == ModbusDataType.BOOL
    assert map_db_format_to_data_type("1") == ModbusDataType.BOOL
    assert map_db_format_to_data_type("Bit") == ModbusDataType.BOOL
    assert map_db_format_to_data_type("bit") == ModbusDataType.BOOL
    assert map_db_format_to_data_type("Bool") == ModbusDataType.BOOL
    assert map_db_format_to_data_type("bool") == ModbusDataType.BOOL
    assert map_db_format_to_data_type("比特") == ModbusDataType.BOOL
    # 6 = Unsigned short 1, 8 = Signed int 1, 12 = Unsigned int 1, 32 = Float 1, 34 = Float 3 (CDAB)
    assert map_db_format_to_data_type(6) == ModbusDataType.UINT16
    assert map_db_format_to_data_type(8) == ModbusDataType.INT32
    assert map_db_format_to_data_type(12) == ModbusDataType.UINT32
    assert map_db_format_to_data_type(32) == ModbusDataType.FLOAT32
    assert map_db_format_to_data_type(34) == ModbusDataType.FLOAT32
    # 变位测试
    assert map_db_format_to_byte_order(34) == ByteOrderMode.CDAB


def test_hems_database_loading_memory(tmp_path):
    """测试模拟数据库的 app 表读取与设备模型生成（含 Float 3=34 与 Bit="1"）"""
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
                "Variable Transfer Format": 34,  # Float 3 (CDAB)
            },
            {
                "Register Start Address": 12,
                "Chinese Name": "合闸状态位",
                "English Name": "Breaker_Bit",
                "Function Code": "1",
                "Variable Transfer Format": "1",  # Bit (比特) -> BOOL
            },
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
    assert len(points) == 2
    assert points[0]["address"] == 10
    assert points[0]["desc"] == "直流母线电压"
    assert points[0]["data_type"] == ModbusDataType.FLOAT32
    assert points[0]["byte_order"] == ByteOrderMode.CDAB
    assert points[1]["address"] == 12
    assert points[1]["desc"] == "合闸状态位"
    assert points[1]["data_type"] == ModbusDataType.BOOL


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
            {"Register Start Address": 100, "English Name": "Active_Power", "Chinese Name": "有功功率", "Function Code": "3", "Variable Transfer Format": 34}
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


def test_function_code_strict_priority_over_status_keywords():
    """测试需求 1：只要 Function Code 非空（如 '3'/'03'/3/'0x03'），严禁被 '状态/status/告警' 关键字劫持为 3x"""
    from modbusstudio.hems_db_loader import HemsAppModel
    from modbusstudio.models import AreaType

    for raw_fc in ("3", "03", 3, "0x03", "FC03", "6", "16"):
        assert HemsAppModel._normalize_function_code(raw_fc) in (3, 6, 16)

    for raw_fc in ("4", "04", 4, "0x04", "FC04"):
        assert HemsAppModel._normalize_function_code(raw_fc) == 4

    more = {
        "Pollings": [{"Function Code": "03", "Start Address": "0", "Read Count": "20"}],
        "Realtime Variables": [
            {
                "Register Start Address": "0",
                "English Name": "Work status",
                "Chinese Name": "工作状态",
                "Function Code": "03",
                "Variable Transfer Format": "Signed short 1",
            },
            {
                "Register Start Address": "1",
                "English Name": "System alarm",
                "Chinese Name": "系统告警",
                "Function Code": "",  # 点位未填，应从 Pollings 继承 03
                "Variable Transfer Format": "Unsigned short 1",
            },
            {
                "Register Start Address": "2",
                "English Name": "Grid frequency",
                "Chinese Name": "电网频率",
                "Function Code": "04",
                "Variable Transfer Format": "Float 3",
            },
        ],
    }
    model = HemsAppModel(
        app_id=174,
        app_type=1,
        english_name="PCS",
        chinese_name="储能变流器",
        model_name="PCS",
        controller_lib="libgeneral_modbus_master.so",
        enable=1,
        raw_more=more,
        local_params={},
    )
    pts = model.extract_studio_points()
    assert len(pts) == 3
    assert pts[0]["desc"] == "工作状态"
    assert AreaType.normalize(pts[0]["area"]) == AreaType.HOLDING
    assert pts[1]["desc"] == "系统告警"
    assert AreaType.normalize(pts[1]["area"]) == AreaType.HOLDING
    assert pts[2]["desc"] == "电网频率"
    assert AreaType.normalize(pts[2]["area"]) == AreaType.INPUT


def test_top_level_chinese_name_backfill(tmp_path):
    """测试需求 4：当 More.Realtime Variables 缺失 Chinese Name 时，自动从 app 表顶层 [Realtime Variables] 列回填"""
    db_file = tmp_path / "test_chn_backfill.cdb"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE app (
            Id INTEGER PRIMARY KEY,
            Type INTEGER,
            [English Name] TEXT,
            [Chinese Name] TEXT,
            [Controller Library] TEXT,
            [Referred Model] TEXT,
            [Local Parameters] TEXT,
            [Realtime Variables] TEXT,
            [Referred Variables] TEXT,
            More TEXT,
            enable INTEGER
        )
        """
    )
    top_rt = [
        {"English Name": "Work status", "Chinese Name": "工作状态"},
        {"English Name": "Active power setpoint", "Chinese Name": "有功功率设置"},
    ]
    more_obj = {
        "Realtime Variables": [
            {
                "Register Start Address": "0",
                "English Name": "Work status",
                "Function Code": "3",
                "Variable Transfer Format": "Signed short 1",
            },
            {
                "Register Start Address": "1",
                "English Name": "Active power setpoint",
                "Function Code": "3",
                "Variable Transfer Format": "Float 3",
            },
        ]
    }
    cur.execute(
        """
        INSERT INTO app (Id, Type, [English Name], [Chinese Name], [Controller Library], [Realtime Variables], More, enable)
        VALUES (174, 1, 'PCS', 'PCS', 'libgeneral_modbus_master.so', ?, ?, 1)
        """,
        (json.dumps(top_rt), json.dumps(more_obj)),
    )
    conn.commit()
    conn.close()

    db = HemsDatabase(str(db_file))
    apps = db.load_modbus_apps()
    assert len(apps) == 1
    pts = apps[0].extract_studio_points()
    assert [p["desc"] for p in pts] == ["工作状态", "有功功率设置"]


def test_slave_panel_data_type_choices_bool_and_double64():
    """测试 SlavePanel.DATA_TYPE_CHOICES 含 BOOL(比特) 与 DOUBLE64，且写回点位时还原为底层枚举值"""
    from modbusstudio.models import AreaType, ByteOrder, DataType
    from modbusstudio.services import PointService
    from modbusstudio.ui.slave_panel import SlavePanel

    assert "BOOL(比特)" in SlavePanel.DATA_TYPE_CHOICES
    assert "DOUBLE64" in SlavePanel.DATA_TYPE_CHOICES
    assert SlavePanel.normalize_data_type_choice("BOOL(比特)") == ModbusDataType.BOOL.value
    assert SlavePanel.normalize_data_type_choice("DOUBLE64") == ModbusDataType.DOUBLE64.value

    ok_bool, pt_bool, _ = PointService.validate_and_build_point(
        address=1,
        description="比特测试点",
        area=AreaType.COIL,
        data_type=SlavePanel.normalize_data_type_choice("BOOL(比特)"),
        byte_order=ByteOrder.ABCD,
        val_input=1,
    )
    assert ok_bool is True and pt_bool is not None
    assert pt_bool.data_type == DataType.BOOL
    assert pt_bool.data_type.value == "BOOL"

    ok_dbl, pt_dbl, _ = PointService.validate_and_build_point(
        address=10,
        description="双精度测试点",
        area=AreaType.HOLDING,
        data_type=SlavePanel.normalize_data_type_choice("DOUBLE64"),
        byte_order=ByteOrder.ABCD,
        val_input=123.456789,
    )
    assert ok_dbl is True and pt_dbl is not None
    assert pt_dbl.register_count == 4


