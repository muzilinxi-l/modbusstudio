"""深度 Review 第 1 批 P0 稳定性与数据安全回归测试。"""

import asyncio
import json
import sqlite3
import time
import tkinter as tk
from unittest.mock import MagicMock, patch

from modbusstudio.app import ModbusStudioAppCore
from modbusstudio.hems_db_loader import HemsDatabase
from modbusstudio.modbus_codec import (
    ByteOrderMode,
    ModbusDataType,
    clamp_value_to_type,
    parse_bool_value,
)
from modbusstudio.modbus_engine import (
    ModbusPollEngine,
    ModbusSlaveEngine,
    MultiStationServerContext,
)
from modbusstudio.models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    SlaveDevice,
)
from modbusstudio.services import LoggingService, PointService, SlaveService
from modbusstudio.ui.main_window import MainWindow


def test_p0_fc05_coil_write_echo_routes_to_coil_not_holding():
    """验证 FC05 写线圈后的回读(async_getValues func_code=5)正确路由至 0x_Coil 而非 4x 保持寄存器。"""
    engine = ModbusSlaveEngine(comm_type="TCP", port=0, slave_id=1, address_base=1)
    # 故意让 4x 地址 1 为 0、线圈地址 1 写 True；再让 4x 地址 1 为 999、线圈地址 1 写 False
    engine.write_raw_values("4x_HoldingRegister", 1, [0])
    ctx = MultiStationServerContext()
    ctx.attach_slave(1, engine)

    asyncio.run(ctx.async_setValues(device_id=1, func_code=5, address=1, values=[True]))
    echo_true = asyncio.run(ctx.async_getValues(device_id=1, func_code=5, address=1, count=1))
    assert echo_true == [True], f"FC05 写 True 回显错误: {echo_true}"

    engine.write_raw_values("4x_HoldingRegister", 1, [999])
    asyncio.run(ctx.async_setValues(device_id=1, func_code=5, address=1, values=[False]))
    echo_false = asyncio.run(ctx.async_getValues(device_id=1, func_code=5, address=1, count=1))
    assert echo_false == [False], f"FC05 写 False 回显错误: {echo_false}"


def test_p0_zero_address_and_65535_capacity(tmp_path):
    """验证 65536 容量完整覆盖 0~65535，且含 0 号地址点表的从机启动时自动采用 0-based 寻址。"""
    eng0 = ModbusSlaveEngine(address_base=0)
    eng0.write_raw_values(AreaType.HOLDING.value, 0, [1111])
    eng0.write_raw_values(AreaType.HOLDING.value, 65535, [2222])
    assert eng0.read_raw_values(AreaType.HOLDING.value, 0, 1) == [1111]
    assert eng0.read_raw_values(AreaType.HOLDING.value, 65535, 1) == [2222]

    # SlaveService 启动包含 0 号地址点位的从机时，0 号地址不能被静默丢弃
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    slave_svc = SlaveService(log_svc)
    dev = SlaveDevice(
        id="dev_zero",
        name="零基从机",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=port, unit_id=1),
    )
    ok_p, pt0, _ = PointService.validate_and_build_point(
        address=0, description="零号寄存器", area=AreaType.HOLDING,
        data_type=DataType.UINT16, byte_order=ByteOrder.ABCD, val_input=4567,
    )
    assert ok_p and pt0 is not None
    dev.add_point(pt0)
    slave_svc.register_device(dev)

    try:
        ok_s, msg = slave_svc.start_slave(dev.id)
        assert ok_s, msg
        active = slave_svc._active_engines[dev.id]
        assert active.address_base == 0
        assert active.read_raw_values(AreaType.HOLDING.value, 0, 1) == [4567]
    finally:
        slave_svc.stop_slave(dev.id)
        log_svc.close()


def test_p0_bool_parse_and_write_typed_no_false_energize():
    """验证布尔解析不把 'False'/'off'/'0.0' 误判为 True，write_typed 拦截只读区且不误合闸。"""
    assert parse_bool_value("False") is False
    assert parse_bool_value("false") is False
    assert parse_bool_value("off") is False
    assert parse_bool_value("0.0") is False
    assert parse_bool_value("true") is True
    assert clamp_value_to_type("true", ModbusDataType.BOOL) == 1
    assert clamp_value_to_type("false", ModbusDataType.BOOL) == 0

    poll = ModbusPollEngine(host="127.0.0.1", port=502, slave_id=1)
    mock_client = MagicMock()
    mock_client.connected = True
    mock_res = MagicMock()
    mock_res.isError.return_value = False
    mock_client.write_coil.return_value = mock_res
    poll.client = mock_client

    ok, _ = poll.write_typed("0x_Coil", 10, "False", ModbusDataType.BOOL)
    assert ok is True
    mock_client.write_coil.assert_called_with(10, False, device_id=1)

    ok2, _ = poll.write_typed("0x_Coil", 10, "0.0", ModbusDataType.BOOL)
    assert ok2 is True
    mock_client.write_coil.assert_called_with(10, False, device_id=1)

    # 只读区必须拒绝写入
    ok_ro, msg_ro = poll.write_typed("3x_InputRegister", 1, 100, ModbusDataType.UINT16)
    assert ok_ro is False
    assert "只读" in msg_ro


def test_p0_hems_save_app_points_preserves_corrupt_more_virtual_points_and_area_keys(tmp_path):
    """验证 save_app_points 拒绝覆盖损坏 More、保留无地址虚点、按区域隔离同地址元数据并按 Type 选键。"""
    db_file = tmp_path / "test_hems.cdb"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "CREATE TABLE app (Id INTEGER PRIMARY KEY, Type INTEGER, More TEXT)"
        )
        # 1. 损坏 JSON：必须返回 False 且保留原字符串不清空
        conn.execute("INSERT INTO app (Id, Type, More) VALUES (1, 1, '{bad_json')")

        # 2. 南向正常记录：含 Pollings、无地址虚点、以及 0x/4x 同地址 10 的独立扩展字段
        south_more = {
            "Pollings": [{"Start": 0, "Count": 10}],
            "Realtime Variables": [
                {
                    "English Name": "VirtualCalc",
                    "Chinese Name": "虚拟计算点",
                    "Register Start Address": "",
                },
                {
                    "English Name": "Coil10",
                    "Chinese Name": "线圈10",
                    "Function Code": "1",
                    "Register Start Address": "10",
                    "CustomTag": "COIL_META",
                },
                {
                    "English Name": "Hold10",
                    "Chinese Name": "保持10",
                    "Function Code": "3",
                    "Register Start Address": "10",
                    "CustomTag": "HOLD_META",
                },
            ],
        }
        conn.execute("INSERT INTO app (Id, Type, More) VALUES (2, 1, ?)", (json.dumps(south_more),))

        # 3. 北向记录(Type=2) 含空 Realtime Variables：应按 Type=2 写入 Referred Variables
        north_more = {"Realtime Variables": [], "Referred Variables": []}
        conn.execute("INSERT INTO app (Id, Type, More) VALUES (3, 2, ?)", (json.dumps(north_more),))
        conn.commit()
    finally:
        conn.close()

    hdb = HemsDatabase(str(db_file))
    assert hdb.save_app_points(1, [{"address": 0, "area": "4x_HoldingRegister", "data_type": "UINT16", "byte_order": "ABCD"}]) is False

    ok = hdb.save_app_points(
        2,
        [
            {"address": 10, "area": "0x_Coil", "data_type": "BOOL", "byte_order": "ABCD", "desc": "新线圈10"},
            {"address": 10, "area": "4x_HoldingRegister", "data_type": "UINT16", "byte_order": "ABCD", "desc": "新保持10"},
        ],
    )
    assert ok is True

    ok_n = hdb.save_app_points(
        3,
        [{"address": 1, "area": "4x_HoldingRegister", "data_type": "UINT16", "byte_order": "ABCD", "desc": "北向点1"}],
    )
    assert ok_n is True

    conn = sqlite3.connect(str(db_file))
    try:
        assert conn.execute("SELECT More FROM app WHERE Id = 1").fetchone()[0] == "{bad_json"

        m2 = json.loads(conn.execute("SELECT More FROM app WHERE Id = 2").fetchone()[0])
        assert m2["Pollings"] == [{"Start": 0, "Count": 10}]
        rv = m2["Realtime Variables"]
        # 2 个物理点 + 1 个保留的虚点
        assert len(rv) == 3
        coil_item = [x for x in rv if x.get("Function Code") == "1"][0]
        hold_item = [x for x in rv if x.get("Function Code") == "3"][0]
        virt_item = [x for x in rv if x.get("English Name") == "VirtualCalc"][0]
        assert coil_item["CustomTag"] == "COIL_META"
        assert hold_item["CustomTag"] == "HOLD_META"
        assert virt_item["Chinese Name"] == "虚拟计算点"

        m3 = json.loads(conn.execute("SELECT More FROM app WHERE Id = 3").fetchone()[0])
        assert len(m3["Referred Variables"]) == 1
        assert m3["Referred Variables"][0]["Chinese Name"] == "北向点1"
    finally:
        conn.close()


def test_p0_poll_panel_exception_recovery_and_readonly_log_finally(tmp_path):
    """验证 PollPanel 后台异常必恢复按钮与忙标志、以及 MainWindow 异常后日志框保持只读。"""
    root = None
    for _ in range(3):
        try:
            root = tk.Tk()
            break
        except tk.TclError:
            import gc
            gc.collect()
            time.sleep(0.05)
    assert root is not None
    root.withdraw()
    app = ModbusStudioAppCore(restore_session=False, session_path=str(tmp_path / "state.json"))
    try:
        win = MainWindow(root, app)
        panel = win.poll_panel

        # 1. 模拟 connect 抛未捕获异常：btn_connect 必须恢复 normal 而非永久卡死
        with patch.object(app.poll_service, "connect", side_effect=RuntimeError("boom")), \
             patch("modbusstudio.ui.poll_panel.messagebox.showerror") as mock_err:
            panel._toggle_connect()
            deadline = time.time() + 1.5
            while time.time() < deadline:
                root.update()
                if str(panel.btn_connect.cget("state")) == "normal":
                    break
                time.sleep(0.02)
            assert str(panel.btn_connect.cget("state")) == "normal"
            assert mock_err.called

        # 2. 模拟 read_registers 抛异常：_is_poll_busy 必须恢复 False
        with patch.object(app.poll_service, "read_registers", side_effect=RuntimeError("read_boom")):
            panel._poll_once()
            deadline = time.time() + 1.5
            while time.time() < deadline:
                root.update()
                if not panel._is_poll_busy:
                    break
                time.sleep(0.02)
            assert panel._is_poll_busy is False

        # 3. 模拟 _flush_log_loop 渲染异常：log_text 必须恢复 disabled 只读态
        app.logging_service.post("SYS", 0, "触发一次异常渲染测试")
        with patch.object(win.log_text, "insert", side_effect=RuntimeError("insert_boom")):
            win._flush_log_loop()
        assert str(win.log_text.cget("state")) == "disabled"
    finally:
        app.shutdown()
        root.destroy()
