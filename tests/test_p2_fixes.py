"""P2 UI 阻塞异步化、空转控件接通、日志/布局修正与探活/对账边界回归测试。"""

import csv
import json
import time
import tkinter as tk
from unittest.mock import patch

import pytest

from modbusstudio.app import ModbusStudioAppCore
from modbusstudio.modbus_codec import ByteOrderMode, ModbusDataType, encode_value
from modbusstudio.modbus_engine import ModbusTransportServer
from modbusstudio.models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    SlaveDevice,
)
from modbusstudio.services import PointService
from modbusstudio.tools import reconcile
from modbusstudio.ui.main_window import MainWindow


def _make_hidden_root():
    root = tk.Tk()
    root.withdraw()
    return root


def test_p2_slave_panel_async_reconnect_and_batch_type_area_migration(tmp_path):
    """验证自动重连不阻塞 UI 主线程，且批量改类型为 BOOL 时同步迁移 area 与字典主键。"""
    root = _make_hidden_root()
    app = ModbusStudioAppCore(restore_session=False, session_path=str(tmp_path / "state.json"))
    try:
        win = MainWindow(root, app)
        panel = win.slave_panel

        # 1. 自动重连异步执行：模拟底层耗时 0.15s，主线程调用必须立即返回 (<0.08s)
        def slow_reconnect():
            time.sleep(0.15)
            return 2

        with patch.object(app.slave_service, "auto_reconnect_if_needed", side_effect=slow_reconnect):
            t0 = time.perf_counter()
            panel._on_auto_reconnect()
            elapsed = time.perf_counter() - t0
            assert elapsed < 0.08, f"自动重连阻塞了 UI 主线程: {elapsed:.3f}s"

            # 驱动事件循环等待后台线程回调落盘
            deadline = time.time() + 1.5
            while time.time() < deadline:
                root.update()
                if "已重建 2 个" in panel.lbl_slave_status.cget("text"):
                    break
                time.sleep(0.02)
            assert "已重建 2 个" in panel.lbl_slave_status.cget("text")

        # 2. 批量修改类型为 BOOL：应从 4x_HoldingRegister 迁移至 0x_Coil 且移除旧主键
        dev = app.slave_service.get_device(panel._current_device_id)
        assert dev is not None
        dev.clear_points()
        ok, pt, _ = PointService.validate_and_build_point(
            address=50, description="开关量测试", area=AreaType.HOLDING,
            data_type=DataType.UINT16, byte_order=ByteOrder.ABCD, val_input=1,
        )
        assert ok and pt is not None
        dev.add_point(pt)
        panel._refresh_tree(dev)

        with patch("modbusstudio.ui.slave_panel.messagebox.showinfo"):
            panel._apply_batch_point_property("data_type", "BOOL")

        assert (AreaType.HOLDING.value, 50) not in dev.points
        migrated = dev.points.get((AreaType.COIL.value, 50))
        assert migrated is not None
        assert migrated.area == AreaType.COIL
        assert migrated.data_type == DataType.BOOL
        assert migrated.raw_hex == "0x01"
    finally:
        app.shutdown()
        root.destroy()


def test_p2_poll_panel_byte_order_nan_input_guard_and_disconnect_reset(tmp_path):
    """验证 PollPanel 变位下拉框生效、NaN 独立展示、非法输入容错与断开复位轮询按钮。"""
    root = _make_hidden_root()
    app = ModbusStudioAppCore(restore_session=False, session_path=str(tmp_path / "state.json"))
    try:
        win = MainWindow(root, app)
        panel = win.poll_panel

        # 1. 非法输入容错
        panel.combo_baud.set("bad_baud")
        panel.entry_unit_id.delete(0, "end")
        panel.entry_unit_id.insert(0, "not_int")
        cfg = panel._get_connection_config()
        assert cfg.baudrate == 9600
        assert cfg.unit_id == 1

        # 2. 选择 DCBA 变位并渲染 FLOAT32：列标题与解码值均应匹配 DCBA
        panel.combo_order.set("DCBA")
        dcba_regs = encode_value(12.5, ModbusDataType.FLOAT32, ByteOrderMode.DCBA)
        panel._render_registers_to_tree(dcba_regs, start=10)
        assert panel.tree.heading("f32_cdab", "text") == "FLOAT32 (DCBA)"
        first_row = panel.tree.item(panel.tree.get_children()[0], "values")
        assert first_row[5] == "12.5000"

        # 3. 寄存器为 0xFFFF/0xFFFF (NaN) 时应展示 "NaN"，不因 :.4f 抛错变成 "-"
        panel._render_registers_to_tree([0xFFFF, 0xFFFF], start=20)
        nan_row = panel.tree.item(panel.tree.get_children()[0], "values")
        assert nan_row[4] == "NaN"
        assert nan_row[5] == "NaN"

        # 4. 断开连接时同步复位连续轮询状态与按钮文案
        panel._is_polling_loop = True
        panel.btn_poll_loop.config(text="⏹ 停止轮询")
        with patch.object(type(app.poll_service), "is_connected", new_callable=lambda: property(lambda self: True)):
            with patch.object(app.poll_service, "disconnect"):
                panel._toggle_connect()
        assert panel._is_polling_loop is False
        assert "连续轮询" in panel.btn_poll_loop.cget("text")
    finally:
        app.shutdown()
        root.destroy()


def test_p2_main_window_geometry_readonly_log_and_csv_direction(tmp_path):
    """验证 MainWindow 保留居中几何尺寸、日志框只读且导出 CSV 正确解析方向列。"""
    root = _make_hidden_root()
    root.geometry("1280x780+50+40")
    app = ModbusStudioAppCore(restore_session=False, session_path=str(tmp_path / "state.json"))
    try:
        win = MainWindow(root, app)
        root.update_idletasks()
        assert win.log_text.cget("state") == "disabled"
        # 验证未强制覆写为 1240x840
        assert not root.geometry().startswith("1240x840")

        # 写入格式化日志并测试 CSV 导出方向列解析
        win.log_text.config(state="normal")
        win.log_text.insert("end", "[14:20:05.123] [RX] 从机:1 | 测试接收报文\n")
        win.log_text.insert("end", "[14:20:06.456] [系统] 服务启动成功\n")
        win.log_text.config(state="disabled")

        out_csv = tmp_path / "export_log.csv"
        with patch("modbusstudio.ui.main_window.filedialog.asksaveasfilename", return_value=str(out_csv)), \
             patch("modbusstudio.ui.main_window.messagebox.showinfo"):
            win._export_log_to_csv()

        with open(out_csv, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["时间戳", "方向", "日志明细"]
        assert rows[1] == ["14:20:05.123", "RX", "从机:1 | 测试接收报文"]
        assert rows[2] == ["14:20:06.456", "系统", "服务启动成功"]
    finally:
        app.shutdown()
        root.destroy()


def test_p2_transport_rebuild_probe_respects_specific_bind_ip():
    """验证 _log_rebuild_outcome 对 0.0.0.0 探测 127.0.0.1，对显式网卡 IP 探测真实绑定 IP。"""
    srv_any = ModbusTransportServer(("TCP", "0.0.0.0", 5020))
    srv_lan = ModbusTransportServer(("TCP", "192.168.10.20", 5020))

    probed_hosts = []

    class _DummySock:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_create_conn(addr, timeout=1.0):
        probed_hosts.append(addr[0])
        return _DummySock()

    with patch("modbusstudio.modbus_engine.socket.create_connection", side_effect=fake_create_conn):
        assert srv_any._log_rebuild_outcome() is True
        assert srv_lan._log_rebuild_outcome() is True

    assert probed_hosts == ["127.0.0.1", "192.168.10.20"]


def test_p2_reconcile_non_dict_cloud_and_multi_slave_same_point_key(tmp_path, capsys):
    """验证 reconcile 拒绝非字典云端 JSON (退出码 2)，且多从机同 point_key 不互相覆盖。"""
    s1 = SlaveDevice(
        id="s1", name="PCS_1",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=502, unit_id=1),
    )
    s2 = SlaveDevice(
        id="s2", name="PCS_2",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=502, unit_id=2),
    )
    _, p1, _ = PointService.validate_and_build_point(0, "V1", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 10)
    _, p2, _ = PointService.validate_and_build_point(0, "V2", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 20)
    s1.add_point(p1)
    s2.add_point(p2)

    session_file = tmp_path / "multi_session.json"
    session_file.write_text(
        json.dumps({"schema_version": 1, "slaves": [s1.to_dict(), s2.to_dict()]}),
        encoding="utf-8",
    )

    # 1. 云端文件为列表而非字典 -> 退出码 2
    bad_cloud = tmp_path / "bad_cloud.json"
    bad_cloud.write_text("[1, 2, 3]", encoding="utf-8")
    rc_bad = reconcile.main(["--session", str(session_file), "--cloud-file", str(bad_cloud)])
    assert rc_bad == 2
    capsys.readouterr()

    # 2. 多从机同 point_key 使用 slave|point_key 前缀精准区分，JSON 保留各自从机名
    good_cloud = tmp_path / "good_cloud.json"
    good_cloud.write_text(
        json.dumps({
            "PCS_1|4x_HoldingRegister|0": 10.0,
            "PCS_2|4x_HoldingRegister|0": 20.0,
        }),
        encoding="utf-8",
    )
    rc_ok = reconcile.main(["--session", str(session_file), "--cloud-file", str(good_cloud), "--json"])
    assert rc_ok == 0
    out = json.loads(capsys.readouterr().out)
    assert {(r["slave"], r["point_key"], r["status"]) for r in out} == {
        ("PCS_1", "4x_HoldingRegister|0", "OK"),
        ("PCS_2", "4x_HoldingRegister|0", "OK"),
    }
