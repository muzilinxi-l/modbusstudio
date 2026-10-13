"""
任务 07（需求 2/3/4/5/6/7/8）UI 控件顺序、波特率档位、数据位顺序、多行布局与导入规则联动单元测试
"""

import inspect
from modbusstudio.models import AreaType
from modbusstudio.ui.slave_panel import SlavePanel
from modbusstudio.ui.dialogs.db_import_dialog import DbImportDialog
from modbusstudio.ui.poll_panel import PollPanel


EXPECTED_AREA_CHOICES = [
    AreaType.COIL.friendly_name,
    AreaType.DISCRETE.friendly_name,
    AreaType.INPUT.friendly_name,
    AreaType.HOLDING.friendly_name,
]

EXPECTED_BAUDRATE_CHOICES = [
    "1200", "2400", "4800", "9600", "19200",
    "38400", "57600", "115200", "230400",
]


def test_area_choices_ascending_order():
    """测试需求 2：所有面板与导入弹窗的寄存器区域下拉选项严格按 0x → 1x → 3x → 4x 升序排列"""
    assert SlavePanel.AREA_CHOICES == EXPECTED_AREA_CHOICES
    assert DbImportDialog.AREA_CHOICES == EXPECTED_AREA_CHOICES
    assert PollPanel.AREA_CHOICES == EXPECTED_AREA_CHOICES
    assert [AreaType.normalize(a).value.split("_")[0] for a in SlavePanel.AREA_CHOICES] == ["0x", "1x", "3x", "4x"]


def test_baudrate_choices_complete_nine_steps():
    """测试需求 7：从机面板、导入弹窗、轮询面板的波特率档位均包含完整 9 档升序列表"""
    assert SlavePanel.BAUDRATE_CHOICES == EXPECTED_BAUDRATE_CHOICES
    assert DbImportDialog.BAUDRATE_CHOICES == EXPECTED_BAUDRATE_CHOICES
    assert PollPanel.BAUDRATE_CHOICES == EXPECTED_BAUDRATE_CHOICES


def test_databit_order_and_top_box_multiline_rows_in_slave_panel():
    """测试需求 3 与需求 8：SlavePanel 数据位按 ['7', '8'] 升序排列，且顶部控制栏拆分为独立多行 Frame"""
    src = inspect.getsource(SlavePanel._build_ui)
    assert 'values=["7", "8"]' in src
    assert 'values=["8", "7"]' not in src
    # 验证顶部控制栏拆分为 row0 / row0b / row1 / row1b / row1c 独立行
    for row_var in ("row0", "row0b", "row1", "row1b", "row1c"):
        assert f"{row_var} = ttk.Frame(top_box)" in src


def test_db_import_dialog_has_rule_search_and_rtu_controls():
    """测试需求 4/5/6/7/8：DbImportDialog 规则配置区包含搜索框、已选中文名汇总及 RTU 五要素控件"""
    ui_src = inspect.getsource(DbImportDialog._build_ui)
    assert "self.mode_search_var" in ui_src
    assert "self.entry_mode_search" in ui_src
    assert "self.lbl_selected_chn_summary" in ui_src
    assert "self.combo_slave_com" in ui_src
    assert "self.combo_slave_baud" in ui_src
    assert "self.combo_slave_databit" in ui_src
    assert "self.combo_slave_parity" in ui_src
    assert "self.combo_slave_stopbit" in ui_src
    assert 'values=["7", "8"]' in ui_src

    filter_src = inspect.getsource(DbImportDialog._filter_tree)
    assert "—（无中文）" in filter_src
    assert "kw_mode" in filter_src
