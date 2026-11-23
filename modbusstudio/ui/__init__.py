"""
Modbus Studio UI Package
~~~~~~~~~~~~~~~~~~~~~~~~
包含主窗体框架、从机工作台面板与主机轮询工作台面板。
"""

from .main_window import MainWindow

__all__ = ["MainWindow", "SlavePanel", "PollPanel"]


def __getattr__(name: str):
    if name == "SlavePanel":
        from .slave_panel import SlavePanel
        return SlavePanel
    if name == "PollPanel":
        from .poll_panel import PollPanel
        return PollPanel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
