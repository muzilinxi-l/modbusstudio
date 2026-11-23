"""P3 工程治理项回归测试：运行期路径基准、告警过滤收敛与机械模板注释清理。"""

import os
import pathlib
import re
import sys
import warnings

from modbusstudio import modbus_engine  # noqa: F401
from modbusstudio.app import get_app_base_dir, resolve_runtime_path


def test_p3_runtime_path_independent_of_cwd(tmp_path, monkeypatch):
    """验证无论当前工作目录切到何处，相对配置路径始终锚定到应用基准目录，绝对路径原样保留。"""
    expected_base = os.path.dirname(os.path.dirname(os.path.abspath(modbus_engine.__file__)))
    assert get_app_base_dir() == expected_base

    # 切换工作目录至临时目录后，默认配置仍应解析到仓库根目录而非 tmp_path
    monkeypatch.chdir(tmp_path)
    assert resolve_runtime_path("settings.json") == os.path.join(expected_base, "settings.json")
    assert resolve_runtime_path("session_state.json") == os.path.join(expected_base, "session_state.json")
    assert resolve_runtime_path("logs") == os.path.join(expected_base, "logs")

    # 显式绝对路径必须原样保留
    custom_abs = str(tmp_path / "custom_session.json")
    assert resolve_runtime_path(custom_abs) == os.path.abspath(custom_abs)


def test_p3_runtime_path_frozen_exe_mode(tmp_path, monkeypatch):
    """验证 PyInstaller 冻结模式下基准目录锚定到可执行文件同级目录。"""
    fake_exe = str(tmp_path / "ModbusStudio.exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", fake_exe)

    assert get_app_base_dir() == os.path.abspath(str(tmp_path))
    assert resolve_runtime_path("settings.json") == os.path.join(os.path.abspath(str(tmp_path)), "settings.json")


def test_p3_deprecation_warning_filter_scoped_to_pymodbus():
    """验证 modbus_engine 仅收敛过滤 pymodbus 模块的 DeprecationWarning，不误吞其他模块告警。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.warn("custom deprecation should remain visible", DeprecationWarning)
    assert any("custom deprecation should remain visible" in str(w.message) for w in caught)


def test_p3_no_mechanical_boilerplate_comments_remaining():
    """验证核心代码库中机械式自动生成样板注释已清理干净。"""
    pat = re.compile(
        r"^\s*#\s*(?:"
        r"在\s*\[[^\]]+\]\s*中(?:更新局部变量|执行前置条件分支判断|遍历集合元素|开启异常防护块|维持受控循环|使用上下文管理器)"
        r"|从\s*\[[^\]]+\]\s*返回执行结果给调用方，结束当前处理分支"
        r"|方法实现\s*\[[^\]]+\].*封装该操作的输入校验、状态转换与异常兜底逻辑"
        r")"
    )
    offenders = []
    for p in sorted(pathlib.Path("modbusstudio").rglob("*.py")):
        for idx, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
            if pat.match(line):
                offenders.append(f"{p}:{idx}")
    assert offenders == [], f"发现残留机械样板注释: {offenders[:5]}"
