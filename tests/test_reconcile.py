"""
点位换算与对账 CLI 单元测试（无头、无网络、不依赖 tkinter）
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
覆盖：
1. 换算核心 point_value：数值型乘 scale / 非数值型原样 / 坏 scale 兜底；
2. 对账逻辑 diff_with_cloud：OK / FAIL / 缺失 / 云端多 四种状态；
3. CLI 主流程：本地基准值表输出、与云端比对、退出码 0/1/2。
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# 直接 import 包内模块（pytest 配置了 pythonpath=["."]）
from modbusstudio.models import (
    AreaType, ByteOrder, ConnectionConfig, CommType, DataType,
    ModbusPoint, SlaveDevice,
)
from modbusstudio.tools.point_value import (
    compute_point_value, is_scalable, point_key, scale_value,
)
from modbusstudio.tools.reconcile import diff_with_cloud


def _make_point(data_type=DataType.UINT16, scale=1.0, value=100,
                area=AreaType.HOLDING, address=0, byte_order=ByteOrder.ABCD,
                string_length=16):
    return ModbusPoint(
        address=address,
        area=area,
        data_type=data_type,
        byte_order=byte_order,
        value=value,
        scale=scale,
        string_length=string_length,
    )


# ---------------------------------------------------------------- 换算核心
def test_is_scalable_numeric_vs_non_numeric():
    """数值型参与 scale 换算；STRING/HEX16/BOOL 不参与（乘系数无意义）。"""
    assert is_scalable(_make_point(DataType.UINT16)) is True
    assert is_scalable(_make_point(DataType.FLOAT32)) is True
    assert is_scalable(_make_point(DataType.STRING)) is False
    assert is_scalable(_make_point(DataType.BOOL)) is False
    assert is_scalable(_make_point(DataType.HEX16)) is False


def test_scale_value_numeric_multiplies():
    """数值型：解算值 × scale = 换算后工程值。"""
    pt = _make_point(DataType.UINT16, scale=2.5, value=4)
    assert scale_value(4, pt) == 10.0


def test_scale_value_non_numeric_unchanged():
    """非数值型（STRING）：乘 scale 无意义，原样返回。"""
    pt = _make_point(DataType.STRING, scale=2.0, value="AB")
    assert scale_value("AB", pt) == "AB"


def test_scale_value_bad_input_falls_back():
    """坏 scale（如 None）不阻断整条链路，退回未换算值。"""
    pt = _make_point(DataType.UINT16, value=4)
    pt.scale = None  # 模拟坏配置
    assert scale_value(4, pt) == 4


def test_compute_point_value_uint16_with_scale():
    """端到端：原始寄存器 → 解算 → 乘 scale 一步到位。"""
    pt = _make_point(DataType.UINT16, scale=3.0, address=0)
    raw, scaled = compute_point_value([42], pt)
    assert raw == 42
    assert scaled == 126.0


def test_compute_point_value_float32_scale():
    """FLOAT32 + scale 完整链：原始寄存器 → float → ×scale。"""
    import struct
    from modbusstudio.modbus_codec import ByteOrderMode, ModbusDataType
    # 4.0f 的 IEEE 754 ABCD 双寄存器表示
    packed = struct.pack(">f", 4.0)
    regs = [int.from_bytes(packed[:2], "big"), int.from_bytes(packed[2:], "big")]
    pt = _make_point(DataType.FLOAT32, scale=0.5, byte_order=ByteOrder.ABCD)
    raw, scaled = compute_point_value(regs, pt)
    assert abs(raw - 4.0) < 1e-6
    assert abs(scaled - 2.0) < 1e-6


def test_compute_point_value_float32_cdab():
    """CDAB 字交换：同一 FLOAT32 值按不同字序解算结果一致。"""
    import struct
    from modbusstudio.modbus_codec import ByteOrderMode, transform_bytes
    packed = struct.pack(">f", 4.0)
    swapped = transform_bytes(packed, ByteOrderMode.CDAB)
    regs = [int.from_bytes(swapped[:2], "big"), int.from_bytes(swapped[2:], "big")]
    pt = _make_point(DataType.FLOAT32, scale=1.0, byte_order=ByteOrder.CDAB)
    raw, scaled = compute_point_value(regs, pt)
    assert abs(raw - 4.0) < 1e-6
    assert abs(scaled - 4.0) < 1e-6


def test_point_key_matches_session_state_layout():
    """复合键格式与 session_state.json / 未来 MQTT payload 对齐。"""
    pt = _make_point(area=AreaType.HOLDING, address=0)
    assert point_key(pt) == "4x_HoldingRegister|0"
    pt2 = _make_point(area=AreaType.INPUT, address=40001)
    assert point_key(pt2) == "3x_InputRegister|40001"


# ---------------------------------------------------------------- 对账逻辑
def test_diff_with_cloud_all_ok():
    local = [("4x_HoldingRegister|0", 100, 100.0),
             ("4x_HoldingRegister|1", 2, 4.0)]
    cloud = {"4x_HoldingRegister|0": 100.0, "4x_HoldingRegister|1": 4.0}
    result = diff_with_cloud(local, cloud, tol=0.001)
    assert all(r[3] == "OK" for r in result)


def test_diff_with_cloud_fail_out_of_tolerance():
    local = [("4x_HoldingRegister|0", 100, 100.0)]
    cloud = {"4x_HoldingRegister|0": 105.0}
    result = diff_with_cloud(local, cloud, tol=0.001)
    assert result[0][3] == "FAIL"


def test_diff_with_cloud_missing_and_extra():
    local = [("4x_HoldingRegister|0", 1, 1.0),
             ("4x_HoldingRegister|1", 2, 2.0)]
    cloud = {"4x_HoldingRegister|0": 1.0, "4x_HoldingRegister|9": 9.0}
    result = diff_with_cloud(local, cloud, tol=0.001)
    status = {r[0]: r[3] for r in result}
    assert status["4x_HoldingRegister|0"] == "OK"
    assert status["4x_HoldingRegister|1"] == "缺失"
    assert status["4x_HoldingRegister|9"] == "云端多"


def test_diff_with_cloud_string_strict_equality():
    """非数值型走严格相等，不走 float 容差。"""
    local = [("4x_HoldingRegister|2", "AB", "AB")]
    cloud = {"4x_HoldingRegister|2": "AB"}
    assert diff_with_cloud(local, cloud, 0.0)[0][3] == "OK"


# ---------------------------------------------------------------- CLI 主流程
def _make_session(tmp_path: Path, slaves_payload) -> Path:
    """造一个最小 session_state.json。"""
    session = tmp_path / "session_state.json"
    session.write_text(
        json.dumps({"schema_version": 1, "slaves": slaves_payload}),
        encoding="utf-8",
    )
    return session


def _slave_payload(name="s1", port=15502, points=None):
    """与 session_state.json 既有存档结构一致：points 为 "area|address" 键字典。"""
    if points is None:
        points = {
            "4x_HoldingRegister|0": {
                "area": "4x_HoldingRegister", "address": 0,
                "data_type": "UINT16", "byte_order": "ABCD",
                "value": 100, "scale": 2.5,
            },
            "4x_HoldingRegister|1": {
                "area": "4x_HoldingRegister", "address": 1,
                "data_type": "STRING", "byte_order": "ABCD",
                "value": "AB", "scale": 1.0,
            },
        }
    return {
        "id": "slave_test_1", "name": name,
        "conn_config": {
            "comm_type": "TCP", "host": "127.0.0.1",
            "port": port, "unit_id": 1,
        },
        "points": points,
    }


def _run_cli(tmp_path, *args):
    """在临时目录跑 CLI 子进程，避免污染工作区日志目录。

    为什么手动按 UTF-8 解码：reconcile.main 会把 stdout/stderr 重配置为
    UTF-8（防 Windows GBK 控制台乱码），父进程若用默认 GBK 读管道会
    UnicodeDecodeError，故捕获字节流后统一 utf-8 解码（errors 容错）。
    """
    code = textwrap.dedent(
        f"from modbusstudio.tools.reconcile import main; raise SystemExit(main({list(args)!r}))"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path), env=env, capture_output=True,
    )
    return type("R", (), {
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", errors="replace"),
        "stderr": proc.stderr.decode("utf-8", errors="replace"),
    })()


def test_cli_local_table_and_exit_zero(tmp_path):
    """无云端文件：输出本地基准值表，退出码 0。"""
    session = _make_session(tmp_path, [_slave_payload()])
    r = _run_cli(tmp_path, "--session", str(session))
    assert r.returncode == 0, r.stderr
    # 数值型点位 100 × 2.5 = 250 出现在换算列
    assert "250" in r.stdout


def test_cli_cloud_compare_exit_one(tmp_path):
    """云端值与本地换算值超容差 → 退出码 1。"""
    session = _make_session(tmp_path, [_slave_payload()])
    cloud = tmp_path / "cloud.json"
    cloud.write_text(
        json.dumps({"4x_HoldingRegister|0": 999.0, "4x_HoldingRegister|1": "AB"}),
        encoding="utf-8",
    )
    r = _run_cli(tmp_path, "--session", str(session),
                 "--cloud-file", str(cloud), "--tolerance", "0.01")
    assert r.returncode == 1
    assert "FAIL" in r.stdout


def test_cli_cloud_match_exit_zero(tmp_path):
    """云端值全部命中 → 退出码 0。"""
    session = _make_session(tmp_path, [_slave_payload()])
    cloud = tmp_path / "cloud.json"
    cloud.write_text(
        json.dumps({"4x_HoldingRegister|0": 250.0, "4x_HoldingRegister|1": "AB"}),
        encoding="utf-8",
    )
    r = _run_cli(tmp_path, "--session", str(session),
                 "--cloud-file", str(cloud), "--tolerance", "0.01")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout


def test_cli_missing_session_exit_two(tmp_path):
    """存档不存在 → 退出码 2。"""
    r = _run_cli(tmp_path, "--session", str(tmp_path / "nope.json"))
    assert r.returncode == 2


def test_cli_json_output(tmp_path):
    """--json 模式输出可解析的 JSON 数组。"""
    session = _make_session(tmp_path, [_slave_payload()])
    r = _run_cli(tmp_path, "--session", str(session), "--json")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert isinstance(data, list)
    assert any(row["point_key"] == "4x_HoldingRegister|0" for row in data)
    row = next(row for row in data if row["point_key"] == "4x_HoldingRegister|0")
    assert row["scaled"] == 250.0


def test_compute_local_rows_broken_scale_fallback():
    """坏点位兜底：scale=None 的数值型点位走 except 分支，不抛 TypeError。

    覆盖审查发现的 reconcile.py 兜底行缺陷（旧代码 pt.value * pt.scale
    在 scale=None 时会二次炸掉），修复后用 scale_value 收口。
    """
    from modbusstudio.tools.reconcile import compute_local_rows
    pt = _make_point(DataType.UINT16, scale=1.0, value=100)
    slave = SlaveDevice(id="s", name="s", conn_config=ConnectionConfig())
    slave.add_point(pt)
    slave.points[("4x_HoldingRegister", 0)].scale = None  # 模拟坏配置
    rows = compute_local_rows(slave)
    assert len(rows) == 1
    key, raw, scaled = rows[0]
    assert key == "4x_HoldingRegister|0"
    assert raw == 100
    assert scaled == 100  # 兜底原样返回，不因坏 scale 抛错


def test_compute_point_value_float64_roundtrip():
    """P0-① 回归：业务层 DataType.FLOAT64 集中映射底层 DOUBLE64，走通 64-bit 编解码与换算。"""
    from modbusstudio.modbus_codec import ModbusDataType, encode_value
    pt = _make_point(DataType.FLOAT64, scale=2.0, value=4.25, byte_order=ByteOrder.ABCD)
    # 4.25 双精度 IEEE 754 对应 8 字节 4 个 16 位寄存器
    regs = encode_value(4.25, ModbusDataType.DOUBLE64, ByteOrder.ABCD, 0)
    assert len(regs) == 4
    raw, scaled = compute_point_value(regs, pt)
    assert abs(raw - 4.25) < 1e-9
    assert abs(scaled - 8.5) < 1e-9


def test_compute_local_rows_float64():
    """P0-① 回归：compute_local_rows 正确识别 FLOAT64 并调用 DOUBLE64 编解码主路径。"""
    from modbusstudio.tools.reconcile import compute_local_rows
    pt = _make_point(DataType.FLOAT64, scale=2.0, value=4.25)
    slave = SlaveDevice(id="s_f64", name="s_f64", conn_config=ConnectionConfig())
    slave.add_point(pt)
    rows = compute_local_rows(slave)
    assert len(rows) == 1
    key, raw, scaled = rows[0]
    assert abs(raw - 4.25) < 1e-9
    assert abs(scaled - 8.5) < 1e-9


def test_load_slaves_multi_unit_same_port(tmp_path):
    """P0-② 回归：同端口多 Unit ID 从机不被端口预检误杀，全量正常恢复。"""
    from modbusstudio.tools.reconcile import load_slaves
    slaves_payload = [
        _slave_payload(name=f"s{i}", port=15502) for i in range(1, 4)
    ]
    for i, s in enumerate(slaves_payload, start=1):
        s["id"] = f"slave_test_{i}"
        s["conn_config"]["unit_id"] = i
    session = _make_session(tmp_path, slaves_payload)
    slaves, warnings = load_slaves(str(session))
    assert len(slaves) == 3
    assert warnings == []
    assert [s.conn_config.unit_id for s in slaves] == [1, 2, 3]


def test_bad_scale_string_all_three_paths():
    """P0-③ 回归：scale='bad' 字符串在三条路径均安全原样返回，不产生重复垃圾串。"""
    from modbusstudio.tools.reconcile import compute_local_rows
    # 路径 1：直接调 scale_value
    pt1 = _make_point(DataType.UINT16, scale="bad", value=100)
    assert scale_value(100, pt1) == 100

    # 路径 2：compute_local_rows 主路径（value=100 符合 UINT16，但 scale='bad'）
    slave_main = SlaveDevice(id="sm", name="sm", conn_config=ConnectionConfig())
    slave_main.add_point(pt1)
    rows_main = compute_local_rows(slave_main)
    assert len(rows_main) == 1
    assert rows_main[0][1] == 100
    assert rows_main[0][2] == 100  # 主路径使用 scale_value 保护，原样返回 100，而非 300 字符 'bad...'

    # 路径 3：compute_local_rows 兜底路径（字节序异常触发 encode 流程 except 兜底）
    pt_bad_order = _make_point(DataType.UINT16, scale="bad", value=100)
    pt_bad_order.byte_order = type("BO", (), {"value": "BAD_BYTE_ORDER"})()
    slave_fallback = SlaveDevice(id="sf", name="sf", conn_config=ConnectionConfig())
    slave_fallback.add_point(pt_bad_order)
    rows_fallback = compute_local_rows(slave_fallback)
    assert len(rows_fallback) == 1
    assert rows_fallback[0][1] == 100
    assert rows_fallback[0][2] == 100  # 兜底通过 scale_value 保护，原样返回 100


def test_diff_with_cloud_bool_strict_equality():
    """P1-⑤ 回归：布尔点位严格比对，防止 float(True)=1.0 把云端 1/1.0/1.0005 误吸收。"""
    local = [("0x_Coil|0", True, True)]
    # 云端是数值 1、1.0、1.0005 时，必须判定为 FAIL
    assert diff_with_cloud(local, {"0x_Coil|0": 1}, tol=0.01)[0][3] == "FAIL"
    assert diff_with_cloud(local, {"0x_Coil|0": 1.0}, tol=0.01)[0][3] == "FAIL"
    assert diff_with_cloud(local, {"0x_Coil|0": 1.0005}, tol=0.01)[0][3] == "FAIL"
    # 云端是 bool True 时判定为 OK
    assert diff_with_cloud(local, {"0x_Coil|0": True}, tol=0.01)[0][3] == "OK"
    # 云端是 bool False 时判定为 FAIL
    assert diff_with_cloud(local, {"0x_Coil|0": False}, tol=0.01)[0][3] == "FAIL"
