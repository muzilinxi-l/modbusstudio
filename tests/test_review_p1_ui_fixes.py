"""深度 Review 第 3 批核心 P1（UI 状态同步、参数范围校验、导入弹窗防误覆写与配对检索对齐）回归测试。"""

import gc
import json
import sqlite3
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

from modbusstudio.app import ModbusStudioAppCore
from modbusstudio.models import AreaType, ByteOrder, DataType
from modbusstudio.services import PointService
from modbusstudio.ui.dialogs.db_import_dialog import DbImportDialog
from modbusstudio.ui.dialogs.pairing_dialog import PairingDialog
from modbusstudio.ui.main_window import MainWindow


def _make_hidden_root():
    gc.collect()
    last_err = None
    for _ in range(3):
        try:
            root = tk.Tk()
            root.withdraw()
            return root
        except tk.TclError as ex:
            last_err = ex
            gc.collect()
            time.sleep(0.05)
    raise last_err


def test_p1_ui_slave_panel_validation_batch_skip_and_string_len(tmp_path):
    """验证 SlavePanel 参数边界原子校验、批量改值失败不虚报、BOOL 迁移冲突跳过、双击透传 string_length。"""
    root = _make_hidden_root()
    app = ModbusStudioAppCore(restore_session=False, session_path=str(tmp_path / "state.json"))
    try:
        win = MainWindow(root, app)
        panel = win.slave_panel
        dev = app.slave_service.get_device(panel._current_device_id)
        assert dev is not None

        # 1. 非法端口/站号拒绝启动且不污染原 conn_config
        orig_port = dev.conn_config.port
        orig_uid = dev.conn_config.unit_id
        panel.entry_slave_port.delete(0, "end")
        panel.entry_slave_port.insert(0, "99999")
        panel.entry_slave_id.delete(0, "end")
        panel.entry_slave_id.insert(0, "300")
        with patch("modbusstudio.ui.slave_panel.messagebox.showerror") as mock_err:
            panel._toggle_current_slave_server()
            assert mock_err.called
        assert dev.conn_config.port == orig_port
        assert dev.conn_config.unit_id == orig_uid

        # 2. 批量改值非法字符串不虚报成功数；BOOL 迁移遇到同地址已有 0x 点位时不覆写原 0x 点位
        dev.clear_points()
        ok1, p_hold, _ = PointService.validate_and_build_point(
            10, "保持10", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 123
        )
        ok2, p_coil, _ = PointService.validate_and_build_point(
            10, "线圈10", AreaType.COIL, DataType.BOOL, ByteOrder.ABCD, 1
        )
        ok3, p_str, _ = PointService.validate_and_build_point(
            20, "长序列号", AreaType.HOLDING, DataType.STRING, ByteOrder.ABCD, "SN_12345678901234567890",
            string_length=24,
        )
        assert ok1 and ok2 and ok3
        dev.add_point(p_hold)
        dev.add_point(p_coil)
        dev.add_point(p_str)
        panel._refresh_tree(dev)

        with patch("modbusstudio.ui.slave_panel.messagebox.showinfo") as mock_info:
            panel._apply_batch_point_property("value", "not_a_number")
            msg = mock_info.call_args[0][1]
            assert "跳过" in msg

        # 选中保持10，尝试改为 BOOL：目标 0x 地址 10 已存在线圈10，应跳过且原线圈10不被覆盖
        for iid in panel.tree.get_children():
            vals = panel.tree.item(iid, "values")
            if int(vals[0]) == 10 and "保持" in vals[2]:
                panel.tree.selection_set((iid,))
                break
        with patch("modbusstudio.ui.slave_panel.messagebox.showinfo"):
            panel._apply_batch_point_property("data_type", "BOOL")
        assert dev.get_point(AreaType.COIL, 10).description == "线圈10"
        assert dev.get_point(AreaType.HOLDING, 10) is not None

        # 3. 双击修改自定义 24 字节 STRING 点位：不截断为默认 16 字节（raw_hex 仍为 12 个寄存器）
        str_iid = None
        for iid in panel.tree.get_children():
            vals = panel.tree.item(iid, "values")
            if int(vals[0]) == 20:
                str_iid = iid
                break
        assert str_iid is not None
        with patch.object(panel.tree, "identify_row", return_value=str_iid), \
             patch("modbusstudio.ui.slave_panel.simpledialog.askstring", return_value="ABCDEFGHIJKLMNOPQRSTUVWX"):
            panel._on_tree_double_click(SimpleNamespace(y=10))
        updated_str = dev.get_point(AreaType.HOLDING, 20)
        assert len(updated_str.raw_hex.split()) == 12
    finally:
        app.shutdown()
        root.destroy()


def test_p1_ui_poll_panel_range_guard(tmp_path):
    """验证 PollPanel 拒绝非法起始地址与超单帧上限 count，防止冻结/越界。"""
    root = _make_hidden_root()
    app = ModbusStudioAppCore(restore_session=False, session_path=str(tmp_path / "state.json"))
    try:
        win = MainWindow(root, app)
        panel = win.poll_panel

        panel.entry_start_addr.delete(0, "end")
        panel.entry_start_addr.insert(0, "1")
        panel.entry_count.delete(0, "end")
        panel.entry_count.insert(0, "50000")
        with patch("modbusstudio.ui.poll_panel.messagebox.showerror") as mock_err:
            panel._poll_once()
            assert mock_err.called
            assert panel._is_poll_busy is False
    finally:
        app.shutdown()
        root.destroy()


def test_p1_ui_db_import_nav_key_and_pairing_search(tmp_path):
    """验证 DbImportDialog 忽略导航键防误覆写 RTU、补填 db_file，且 PairingDialog 支持搜 R_001 与源地址。"""
    db_file = tmp_path / "ui_test.cdb"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            'CREATE TABLE app (Id INTEGER PRIMARY KEY, Type INTEGER, "English Name" TEXT, '
            '"Chinese Name" TEXT, "Referred Model" TEXT, "Controller Library" TEXT, '
            'enable INTEGER, More TEXT, "Local Parameters" TEXT)'
        )
        south_more = {
            "Realtime Variables": [
                {"English Name": "V_dc", "Chinese Name": "直流电压", "Register Start Address": "42", "Function Code": "3", "Variable Transfer Format": "Float 3"},
            ]
        }
        north_more = {
            "Referred Variables": [
                {"English Name": "PCS1|V_dc", "Register Address": "100", "Function Code": "3"},
            ]
        }
        conn.execute("INSERT INTO app VALUES (1, 1, 'PCS1', '储能PCS', 'PCS', 'Modbus', 1, ?, '[]')", (json.dumps(south_more),))
        conn.execute("INSERT INTO app VALUES (2, 2, 'North1', '北向转发', 'North', 'Modbus', 1, ?, '[]')", (json.dumps(north_more),))
        conn.commit()
    finally:
        conn.close()

    root = _make_hidden_root()
    app = ModbusStudioAppCore(restore_session=False, session_path=str(tmp_path / "state.json"))
    app.db_path = str(db_file)
    try:
        win = MainWindow(root, app)

        # 1. DbImportDialog: 按 Tab / 方向键不得把 _rtu_user_overridden 置 True
        dlg = DbImportDialog(win.slave_panel, app, win.slave_panel._current_device_id, lambda d: None)
        try:
            dlg._rtu_user_overridden = False
            dlg._on_rtu_rule_user_edited(SimpleNamespace(keysym="Tab"))
            dlg._on_rtu_rule_user_edited(SimpleNamespace(keysym="Right"))
            assert dlg._rtu_user_overridden is False
            dlg._on_rtu_rule_user_edited(SimpleNamespace(keysym="9"))
            assert dlg._rtu_user_overridden is True
        finally:
            if dlg.dlg.winfo_exists():
                dlg.dlg.destroy()

        # 2. PairingDialog: 输入展示序号 R_001 或源物理地址 42 均能精准命中并更新筛选计数
        pdlg = PairingDialog(win.slave_panel, app)
        try:
            assert len(pdlg.tree_pair.get_children()) == 1
            pdlg.search_var.set("R_001")
            pdlg._filter_rules()
            assert len(pdlg.tree_pair.get_children()) == 1
            assert "1 / 共 1" in pdlg.lbl_info.cget("text")

            pdlg.search_var.set("42")
            pdlg._filter_rules()
            assert len(pdlg.tree_pair.get_children()) == 1
        finally:
            if pdlg.dlg.winfo_exists():
                pdlg.dlg.destroy()
    finally:
        app.shutdown()
        root.destroy()
