"""
测试 UI 主题初始化与动态热切换
"""
import pytest
import ttkbootstrap as tb
from modbusstudio.app import ModbusStudioAppCore
from modbusstudio.ui import MainWindow


def test_main_window_theme_switching():
    try:
        root = tb.Window(themename="bootstrap-light")
    except Exception as e:
        pytest.skip(f"当前环境不支持初始化 Tk 顶层窗口或缺少 Tcl 组件: {e}")
    try:
        app = ModbusStudioAppCore()
        win = MainWindow(root, app)

        assert win.combo_theme.get() == "bootstrap-light"

        # 动态热切换到暗色主题
        win._apply_theme("dracula-dark")
        assert win.combo_theme.get() == "dracula-dark"

        # 动态热切换到清新明亮主题
        win._apply_theme("minty-light")
        assert win.combo_theme.get() == "minty-light"

        # 测试明暗快捷翻转
        win._toggle_light_dark()
        assert "dark" in win.combo_theme.get().lower()

        app.shutdown()
    finally:
        root.destroy()
