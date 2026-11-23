"""系数 (Scale Factor) 与偏移量 (Offset) 全链路端到端回归测试。

覆盖：
1. 换算核心 (eng = raw * scale + offset / raw = (eng - offset) / scale)、scale=0 兜底、四舍五入与非数值跳过；
2. 真实 TCP 从机自动波动一致性：总线读出的原始寄存器套用 raw * scale + offset 后与 ModbusStudio 展示值 100% 相等；
3. 外部主站 FC06 写入原始寄存器后，从机模型自动正向还原为工程源数据；
4. HEMS Type=1 Realtime Variables 导入 Scale Factor & Offset、save_app_points 无损回写、会话持久化、对账无双重缩放；
5. SlavePanel UI：系数/偏移量列展示、单点添加反算、批量修改系数/偏移（自动跳过 BOOL/STRING）、双击列编辑。
"""

import gc
import json
import math
import socket
import sqlite3
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

from modbusstudio.app import ModbusStudioAppCore, load_session_state, save_session_state
from modbusstudio.hems_db_loader import HemsAppModel, HemsDatabase
from modbusstudio.modbus_codec import (
    ByteOrderMode,
    ModbusDataType,
    eng_to_raw,
    normalize_scale_offset,
    raw_to_eng,
)
from modbusstudio.modbus_engine import ModbusPollEngine
from modbusstudio.models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    ModbusPoint,
    SlaveDevice,
)
from modbusstudio.services import LoggingService, PointService, SlaveService
from modbusstudio.tools.point_value import compute_point_value, scale_value
from modbusstudio.tools.reconcile import compute_local_rows
from modbusstudio.ui.main_window import MainWindow


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def test_scale_offset_math_rounding_clamp_and_non_numeric_skip():
    """验证 1A 公式、scale=0 兜底、整型四舍五入/钳位及 BOOL/STRING/HEX16 跳过换算。"""
    assert normalize_scale_offset(0, "bad") == (1.0, 0.0)
    assert normalize_scale_offset("0.1", "-40") == (0.1, -40.0)

    # 温度例：scale=0.1, offset=-40；eng=25.0 -> raw=650；raw=650 -> eng=25.0
    assert eng_to_raw(25.0, ModbusDataType.UINT16, 0.1, -40.0) == 650
    assert raw_to_eng(650, ModbusDataType.UINT16, 0.1, -40.0) == 25.0

    # 四舍五入：220.46 / 0.1 = 2204.6 -> 2205
    assert eng_to_raw(220.46, ModbusDataType.UINT16, 0.1, 0.0) == 2205
    assert raw_to_eng(2205, ModbusDataType.UINT16, 0.1, 0.0) == 220.5

    # 钳位：UINT16 不允许负原始寄存器
    assert eng_to_raw(-100.0, ModbusDataType.UINT16, 0.1, -40.0) == 0

    # BOOL / STRING / HEX16 原样跳过
    assert eng_to_raw("SN123", ModbusDataType.STRING, 0.1, -40.0) == "SN123"
    assert raw_to_eng("SN123", ModbusDataType.STRING, 0.1, -40.0) == "SN123"
    assert eng_to_raw(1, ModbusDataType.BOOL, 10.0, 5.0) == 1
    assert raw_to_eng(0x1A2B, ModbusDataType.HEX16, 0.1, -40.0) == 0x1A2B


def test_slave_auto_simulation_and_external_write_consistency_with_scale_offset(tmp_path):
    """端到端验证：自动波动时总线原始寄存器套 raw*scale+offset 与从机界面值 100% 一致，外部写回正向还原。"""
    port = _pick_free_port()
    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    slave_svc = SlaveService(log_svc)
    dev = SlaveDevice(
        id="dev_scale_sim",
        name="系数偏移仿真从机",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=port, unit_id=1),
    )

    # 点1：UINT16, scale=0.1, offset=-40, 步进递增（初始 eng=25.0 -> raw=650 -> 0x028A）
    ok1, pt1, err1 = PointService.validate_and_build_point(
        address=1, description="电芯温度", area=AreaType.HOLDING,
        data_type=DataType.UINT16, byte_order=ByteOrder.ABCD,
        val_input="25.0", sim_rule="步进递增", scale=0.1, offset=-40.0,
    )
    assert ok1 and pt1 is not None, err1
    assert pt1.value == 25.0
    assert pt1.raw_hex == "0x028A"

    # 点2：UINT16, scale=10.0, offset=5.0, 步进递增（验证 scale>1 时绝不卡死不动）
    ok2, pt2, err2 = PointService.validate_and_build_point(
        address=2, description="放大功率", area=AreaType.HOLDING,
        data_type=DataType.UINT16, byte_order=ByteOrder.ABCD,
        val_input="55.0", sim_rule="步进递增", scale=10.0, offset=5.0,
    )
    assert ok2 and pt2 is not None, err2
    assert pt2.value == 55.0
    assert pt2.raw_hex == "0x0005"  # (55 - 5) / 10 = 5

    # 点3：固定点位，用于测外部主站 FC06 写入原始寄存器 650 -> 正向还原 25.0
    ok3, pt3, err3 = PointService.validate_and_build_point(
        address=3, description="目标给定", area=AreaType.HOLDING,
        data_type=DataType.UINT16, byte_order=ByteOrder.ABCD,
        val_input="0.0", sim_rule="固定", scale=0.1, offset=-40.0,
    )
    assert ok3 and pt3 is not None, err3

    dev.add_point(pt1)
    dev.add_point(pt2)
    dev.add_point(pt3)
    slave_svc.register_device(dev)

    poll = ModbusPollEngine(host="127.0.0.1", port=port, slave_id=1)
    try:
        ok_s, msg_s = slave_svc.start_slave(dev.id)
        assert ok_s, msg_s
        assert poll.connect_tcp("127.0.0.1", port)

        # 等待后台仿真至少推进 1~2 拍，确认两个自动波动点位值均真实前进
        deadline = time.time() + 3.5
        while time.time() < deadline:
            if pt1.value > 25.0 and pt2.value > 55.0:
                break
            time.sleep(0.1)

        assert pt1.value > 25.0, f"pt1 未随自动波动更新: {pt1.value}"
        assert pt2.value > 55.0, f"pt2 (scale=10) 步进卡死未更新: {pt2.value}"

        # 切固定避免读瞬间跨拍，立刻用主站读取总线寄存器，核对 raw * scale + offset == pt.value
        pt1.sim_rule = "固定"
        pt2.sim_rule = "固定"
        slave_svc.sync_point_to_runtime(dev.id, pt1)
        slave_svc.sync_point_to_runtime(dev.id, pt2)

        ok_r, regs, err_r = poll.read_block("4x_HoldingRegister", 1, 2)
        assert ok_r, err_r
        parsed_pt1 = round(regs[0] * pt1.scale + pt1.offset, 6)
        parsed_pt2 = round(regs[1] * pt2.scale + pt2.offset, 6)
        assert math.isclose(parsed_pt1, pt1.value, abs_tol=1e-6), f"总线解析={parsed_pt1} != 界面={pt1.value}"
        assert math.isclose(parsed_pt2, pt2.value, abs_tol=1e-6), f"总线解析={parsed_pt2} != 界面={pt2.value}"

        # 外部主站 FC06 写入原始寄存器 700 到地址 3 -> 应自动正向还原为 700 * 0.1 + (-40) = 30.0
        ok_w, err_w = poll.write_single_register(3, 700)
        assert ok_w, err_w
        time.sleep(0.1)
        assert pt3.value == 30.0
        assert pt3.raw_hex == "0x02BC"
    finally:
        poll.disconnect()
        slave_svc.stop_slave(dev.id)
        log_svc.close()


def test_hems_import_save_session_and_reconcile_with_offset(tmp_path):
    """验证 HEMS Realtime Variables 导入/回写 Offset、会话持久化与对账无双重缩放。"""
    # 1. HEMS Type=1 Realtime Variables 解析 Scale Factor 与 Offset
    app_model = HemsAppModel(
        app_id=101,
        app_type=1,
        english_name="BMS_1",
        chinese_name="电池堆1",
        model_name="BMS",
        controller_lib="Mb",
        enable=1,
        raw_more={
            "Realtime Variables": [
                {
                    "English Name": "CellTemp",
                    "Chinese Name": "单体温度",
                    "Function Code": "3",
                    "Register Start Address": "10",
                    "Variable Transfer Format": "Unsigned short 1",
                    "Scale Factor": "0.1",
                    "Offset": "-40",
                    "Value": "25.0",
                }
            ]
        },
    )
    extracted = app_model.extract_studio_points()
    assert len(extracted) == 1
    assert extracted[0]["scale"] == 0.1
    assert extracted[0]["offset"] == -40.0

    # 2. HEMS save_app_points 回写同时保存 Scale Factor 和 Offset
    db_file = tmp_path / "hems_offset.cdb"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("CREATE TABLE app (Id INTEGER PRIMARY KEY, Type INTEGER, More TEXT)")
        conn.execute("INSERT INTO app VALUES (101, 1, ?)", (json.dumps({"Realtime Variables": []}),))
        conn.commit()
    finally:
        conn.close()

    hdb = HemsDatabase(str(db_file))
    assert hdb.save_app_points(
        101,
        [{
            "address": 10,
            "area": "4x_HoldingRegister",
            "data_type": "UINT16",
            "byte_order": "ABCD",
            "desc": "单体温度",
            "scale": 0.1,
            "offset": -40.0,
        }],
    ) is True
    conn = sqlite3.connect(str(db_file))
    try:
        saved_more = json.loads(conn.execute("SELECT More FROM app WHERE Id = 101").fetchone()[0])
        assert saved_more["Realtime Variables"][0]["Scale Factor"] == "0.1"
        assert saved_more["Realtime Variables"][0]["Offset"] == "-40.0"
    finally:
        conn.close()

    # 3. 会话持久化保存/恢复 offset
    state_file = str(tmp_path / "session_offset.json")
    app = ModbusStudioAppCore(restore_session=False, session_path=state_file)
    try:
        dev = app.slave_service.get_devices()[0]
        dev.clear_points()
        ok, pt, _ = PointService.validate_and_build_point(
            10, "单体温度", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD,
            "25.0", "固定", scale=0.1, offset=-40.0,
        )
        assert ok and pt is not None
        dev.add_point(pt)
        assert save_session_state(app, state_file) is True
    finally:
        app.shutdown()

    loaded = load_session_state(state_file)
    assert loaded is not None
    restored_dev = SlaveDevice.from_dict(loaded["slaves"][0])
    restored_pt = restored_dev.get_point(AreaType.HOLDING, 10)
    assert restored_pt is not None
    assert restored_pt.scale == 0.1
    assert restored_pt.offset == -40.0
    assert restored_pt.value == 25.0
    assert restored_pt.raw_hex == "0x028A"

    # 4. 离线对账工具：point_value 支持 offset 且 reconcile 对新版工程值存档不发生双重缩放
    assert scale_value(650, restored_pt) == 25.0
    raw_v, scaled_v = compute_point_value([650], restored_pt)
    assert raw_v == 650 and scaled_v == 25.0

    rows = compute_local_rows(restored_dev)
    assert len(rows) == 1
    assert rows[0] == ("4x_HoldingRegister|10", 650, 25.0)


def test_slave_panel_ui_scale_offset_add_batch_and_double_click(tmp_path):
    """验证 SlavePanel 表格新增系数/偏移列、单点添加反算、批量修改系数/偏移与双击列编辑。"""
    root = _make_hidden_root()
    app = ModbusStudioAppCore(restore_session=False, session_path=str(tmp_path / "ui_state.json"))
    try:
        win = MainWindow(root, app)
        panel = win.slave_panel
        assert "scale" in panel.columns and "offset" in panel.columns

        dev = app.slave_service.get_device(panel._current_device_id)
        assert dev is not None
        dev.clear_points()

        # 1. 单点添加：地址 10, UINT16, 初值 25.0, 系数 0.1, 偏移 -40 -> 0x028A
        panel.entry_pt_addr.delete(0, "end")
        panel.entry_pt_addr.insert(0, "10")
        panel.entry_pt_desc.delete(0, "end")
        panel.entry_pt_desc.insert(0, "模组温度")
        panel.combo_pt_area.set(AreaType.HOLDING.friendly_name)
        panel.combo_pt_type.set("UINT16")
        panel.entry_pt_val.delete(0, "end")
        panel.entry_pt_val.insert(0, "25.0")
        panel.entry_pt_scale.delete(0, "end")
        panel.entry_pt_scale.insert(0, "0.1")
        panel.entry_pt_offset.delete(0, "end")
        panel.entry_pt_offset.insert(0, "-40")
        panel._on_click_add_point()

        pt10 = dev.get_point(AreaType.HOLDING, 10)
        assert pt10 is not None
        assert pt10.scale == 0.1
        assert pt10.offset == -40.0
        assert pt10.value == 25.0
        assert pt10.raw_hex == "0x028A"

        # 再加一个 BOOL 点位验证批量修改系数时会自动跳过非数值型
        ok_b, pt_b, _ = PointService.validate_and_build_point(
            1, "开关", AreaType.COIL, DataType.BOOL, ByteOrder.ABCD, 1
        )
        assert ok_b and pt_b is not None
        dev.add_point(pt_b)
        panel._refresh_tree(dev)

        # 2. 批量修改系数为 0.5：pt10 (value=25.0, offset=-40) -> raw=(25 - (-40))/0.5 = 130 -> 0x0082；BOOL 跳过
        panel.tree.selection_set(())
        panel.entry_batch_scale.delete(0, "end")
        panel.entry_batch_scale.insert(0, "0.5")
        with patch("modbusstudio.ui.slave_panel.messagebox.showinfo") as mock_info:
            panel._apply_batch_scale()
            assert "跳过 1" in mock_info.call_args[0][1]
        assert pt10.scale == 0.5
        assert pt10.raw_hex == "0x0082"

        # 3. 双击偏移量列 (#7) 将偏移量改为 0 -> raw = 25.0 / 0.5 = 50 -> 0x0032
        target_iid = None
        for iid in panel.tree.get_children():
            vals = panel.tree.item(iid, "values")
            if int(vals[0]) == 10:
                target_iid = iid
                break
        assert target_iid is not None

        with patch.object(panel.tree, "identify_row", return_value=target_iid), \
             patch.object(panel.tree, "identify_column", return_value="#7"), \
             patch("modbusstudio.ui.slave_panel.simpledialog.askstring", return_value="0"):
            panel._on_tree_double_click(SimpleNamespace(x=300, y=10))
        assert pt10.offset == 0.0
        assert pt10.raw_hex == "0x0032"
    finally:
        app.shutdown()
        root.destroy()


def test_sim_range_plan_b_mode_2_smooth_walk_bounds_and_ui_persistence(tmp_path):
    """验证方案 B + 效果 2：默认 ±1000(无符号 0~1000)、自定义范围热生效、平滑 5% 游走、硬件钳位与会话持久化。"""
    # 1. 模型持久化 sim_min / sim_max（含反向自动互换兜底）
    ok, pt, _ = PointService.validate_and_build_point(
        address=10, description="功率因数", area=AreaType.HOLDING,
        data_type=DataType.INT16, byte_order=ByteOrder.ABCD,
        val_input="0.5", sim_rule="随机波动", scale=0.01, offset=0.0,
        sim_min=1.0, sim_max=-1.0,  # 故意反传验证自动校正
    )
    assert ok and pt is not None
    assert pt.sim_min == -1.0 and pt.sim_max == 1.0
    restored = ModbusPoint.from_dict(pt.to_dict())
    assert restored.sim_min == -1.0 and restored.sim_max == 1.0

    # 2. 真实从机验证：默认 [-1000, +1000] 平滑游走（UINT32 scale=0.1 -> 0~1000，单拍步幅 <= 50.0）
    #    以及自定义范围 [-1.0, 1.0]（INT16 scale=0.01 -> 严格落在 [-1.0, 1.0]）
    port = _pick_free_port()
    log_svc = LoggingService(log_dir=str(tmp_path / "logs"))
    slave_svc = SlaveService(log_svc)
    dev = SlaveDevice(
        id="dev_sim_range",
        name="大范围平滑波动从机",
        conn_config=ConnectionConfig(comm_type=CommType.TCP, host="127.0.0.1", port=port, unit_id=1),
    )
    ok_u, pt_u, _ = PointService.validate_and_build_point(
        address=1, description="充电量", area=AreaType.INPUT,
        data_type=DataType.UINT32, byte_order=ByteOrder.ABCD,
        val_input="0.0", sim_rule="随机波动", scale=0.1, offset=0.0,
    )
    ok_pf, pt_pf, _ = PointService.validate_and_build_point(
        address=3, description="功率因数", area=AreaType.HOLDING,
        data_type=DataType.INT16, byte_order=ByteOrder.ABCD,
        val_input="0.0", sim_rule="随机波动", scale=0.01, offset=0.0,
        sim_min=-1.0, sim_max=1.0,
    )
    assert ok_u and pt_u is not None and ok_pf and pt_pf is not None
    dev.add_point(pt_u)
    dev.add_point(pt_pf)
    slave_svc.register_device(dev)

    try:
        ok_s, msg_s = slave_svc.start_slave(dev.id)
        assert ok_s, msg_s

        # 采样 2~3 拍，验证大范围平滑游走明显离开 0 且不越界，自定义 [-1, 1] 绝不越界
        u_samples = []
        pf_samples = []
        deadline = time.time() + 3.5
        while time.time() < deadline and len(u_samples) < 2:
            if pt_u.value != 0.0 and (not u_samples or u_samples[-1] != pt_u.value):
                u_samples.append(pt_u.value)
            if pt_pf.value != 0.0 and (not pf_samples or pf_samples[-1] != pt_pf.value):
                pf_samples.append(pt_pf.value)
            time.sleep(0.1)

        assert u_samples, "UINT32 随机波动未产生变化"
        assert all(0.0 <= v <= 1000.0 for v in u_samples), f"UINT32 超出 [0, 1000]: {u_samples}"
        assert max(u_samples) <= 150.0, f"首 2~3 拍平滑游走步幅过大: {u_samples}"
        assert all(-1.0 <= v <= 1.0 for v in pf_samples), f"自定义范围 [-1, 1] 越界: {pf_samples}"
    finally:
        slave_svc.stop_slave(dev.id)
        log_svc.close()

    # 3. SlavePanel 批量修改波动范围 UI + 会话持久化验证
    root = _make_hidden_root()
    state_file = str(tmp_path / "range_state.json")
    app = ModbusStudioAppCore(restore_session=False, session_path=state_file)
    try:
        win = MainWindow(root, app)
        panel = win.slave_panel
        d = app.slave_service.get_device(panel._current_device_id)
        assert d is not None
        panel.tree.selection_set(())
        panel.entry_batch_sim_min.delete(0, "end")
        panel.entry_batch_sim_min.insert(0, "-500")
        panel.entry_batch_sim_max.delete(0, "end")
        panel.entry_batch_sim_max.insert(0, "2500")
        with patch("modbusstudio.ui.slave_panel.messagebox.showinfo"):
            panel._apply_batch_sim_range()

        for p in d.points.values():
            assert p.sim_min == -500.0 and p.sim_max == 2500.0
    finally:
        app.shutdown()
        root.destroy()

    reloaded = load_session_state(state_file)
    assert reloaded is not None
    reloaded_dev = SlaveDevice.from_dict(reloaded["slaves"][0])
    for p in reloaded_dev.points.values():
        assert p.sim_min == -500.0 and p.sim_max == 2500.0
