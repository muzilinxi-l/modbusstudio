"""
针对数据类型与存储区域智能对应、初始值识别、正弦波动态模拟及 Hex 编码的持久化回归测试
"""

import math
import time
import pytest

from modbusstudio.models import AreaType, DataType, ByteOrder, SimRule, SlaveDevice, ConnectionConfig, CommType
from modbusstudio.services import LoggingService, SlaveService, PointService
from modbusstudio.hems_db_loader import HemsAppModel
from modbusstudio.modbus_codec import ModbusDataType


def test_area_type_normalization_priority():
    """验证离散输入与输入寄存器的解析优先级，杜绝误判"""
    assert AreaType.normalize("离散输入 (1x)") == AreaType.DISCRETE
    assert AreaType.normalize("1x_DiscreteInput") == AreaType.DISCRETE
    assert AreaType.normalize("输入寄存器 (3x)") == AreaType.INPUT
    assert AreaType.normalize("3x_InputRegister") == AreaType.INPUT
    assert AreaType.normalize("线圈 (0x)") == AreaType.COIL
    assert AreaType.normalize("保持寄存器 (4x)") == AreaType.HOLDING


def test_alarm_points_auto_mapping_and_initial_values():
    """验证从数据库导入告警布尔点位时，存储区域与数据类型的合规对应及初值提取"""
    mock_app = HemsAppModel(
        app_id=101,
        app_type=1,
        english_name="BMS_Cluster",
        chinese_name="电池簇管理系统",
        model_name="BMS",
        controller_lib="Mb",
        enable=1,
        raw_more={
            "Realtime Variables": [
                {
                    "Register Start Address": "1",
                    "English Name": "Cluster Over Voltage Alarm",
                    "Chinese Name": "簇过压告警",
                    "Function Code": "4",
                    "Variable Transfer Format": "Bit",
                },
                {
                    "Register Start Address": "2",
                    "English Name": "Cluster Voltage",
                    "Chinese Name": "簇端电压",
                    "Function Code": "4",
                    "Variable Transfer Format": "Unsigned short 1",
                    "Initial Value": "750",
                }
            ]
        }
    )
    pts = mock_app.extract_studio_points()
    assert len(pts) == 2

    # 告警布尔位必须自动归属离散输入 (1x)
    assert "DiscreteInput" in pts[0]["area"] or "离散" in pts[0]["area"]
    assert pts[0]["data_type"] == ModbusDataType.BOOL

    # 数值型寄存器保留在输入寄存器 (3x)
    assert "InputRegister" in pts[1]["area"] or "输入" in pts[1]["area"]
    assert pts[1]["data_type"] == ModbusDataType.UINT16
    assert pts[1]["current_val"] == 750


def test_point_service_hex_encoding():
    """验证布尔位为 0x00/0x01 单字节编码，寄存器为 0xXXXX 四位编码"""
    ok1, p1, _ = PointService.validate_and_build_point(
        address=1,
        description="Alarm_1",
        area=AreaType.DISCRETE,
        data_type=DataType.BOOL,
        byte_order=ByteOrder.ABCD,
        val_input=0,
        sim_rule="正弦波"
    )
    assert ok1
    assert p1.raw_hex == "0x00"

    p1.value = 1
    PointService.re_encode_point(p1)
    assert p1.raw_hex == "0x01"

    ok2, p2, _ = PointService.validate_and_build_point(
        address=2,
        description="Voltage_1",
        area=AreaType.INPUT,
        data_type=DataType.UINT16,
        byte_order=ByteOrder.ABCD,
        val_input=100,
        sim_rule="正弦波"
    )
    assert ok2
    assert p2.raw_hex == "0x0064"


def test_sine_simulation_and_runtime_sync():
    """测试正弦波动态模拟数据能够持续生成并正确反向同步至点位模型"""
    log_svc = LoggingService()
    slave_svc = SlaveService(log_svc)
    dev = SlaveDevice(
        id="dev_sine_test",
        name="正弦波测试从机",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, port=25030, unit_id=1)
    )

    _, p_bool, _ = PointService.validate_and_build_point(
        address=1,
        description="Alarm_Sine",
        area=AreaType.DISCRETE,
        data_type=DataType.BOOL,
        byte_order=ByteOrder.ABCD,
        val_input=0,
        sim_rule="正弦波"
    )
    _, p_num, _ = PointService.validate_and_build_point(
        address=2,
        description="Voltage_Sine",
        area=AreaType.INPUT,
        data_type=DataType.UINT16,
        byte_order=ByteOrder.ABCD,
        val_input=500,
        sim_rule="正弦波"
    )

    dev.add_point(p_bool)
    dev.add_point(p_num)
    slave_svc.register_device(dev)

    ok, msg = slave_svc.start_slave(dev.id)
    assert ok, f"启动失败: {msg}"

    # 运行 2 秒等待波形变动
    time.sleep(2.2)

    # 验证动态数据已成功产生
    assert p_bool.raw_hex in ("0x00", "0x01")
    assert p_num.value != 500 or p_num.raw_hex != "0x01F4"

    slave_svc.stop_slave(dev.id)
