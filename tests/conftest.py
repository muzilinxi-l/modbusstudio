"""全局 pytest 夹具：在主线程收尾时回收上一用例残留的 Tk Variable，避免在后续后台线程触发 GC 析构告警与 Tcl 竞态。"""

import gc
import pytest


@pytest.fixture(autouse=True)
def _gc_collect_in_main_thread():
    yield
    gc.collect()
