"""
点位对账 CLI（纯逻辑，无 tkinter）
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
从 session_state.json 恢复从机与点表，用**纯内存引擎**算出每个点位的
本地基准值（原始解算值 + 乘 scale 系数后的工程值），可选地与云端值
（EMQX 报文 dump 成的 JSON，按 point_key 对齐）做逐点容差比对。

为什么用内存引擎而非启动真实监听：
- 对账只需要"寄存器值 → 系数换算"这条纯计算链，不依赖任何网络端口；
- 遵守任务 11 红线（恢复从机绝不自动启动监听）；
- 离线可跑，CI / 云端机器随时能执行。

用法（在 F:/GitHub/python 下）：
    # 仅算本地基准值（表格输出）
    python -m modbusstudio.tools.reconcile --session session_state.json

    # 本地 vs 云端比对（云端值 dump 成 {"4x_HoldingRegister|0": 12.34, ...}）
    python -m modbusstudio.tools.reconcile --session session_state.json \
        --cloud-file cloud_dump.json --tolerance 0.001

退出码：
    0 = 成功（无差异，或未启用比对）
    1 = 比对存在 FAIL / 缺失 / 云端多余
    2 = 参数或环境错误（存档缺失、无从机、无点位等）
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Tuple

from ..modbus_codec import ByteOrderMode, ModbusDataType, decode_value, encode_value
from ..models import ModbusPoint, SlaveDevice
from ..services import SlaveService
from .point_value import is_scalable, point_key


def load_slaves(session_path: str) -> Tuple[List[SlaveDevice], List[str]]:
    """从 session_state.json 恢复全部从机（仅内存模型，不启动任何监听）。

    参数：session_path（存档路径）。返回：(slaves, 恢复告警列表)。
    步骤：走 SlaveService.restore_devices 既有通道（自带配额与端口预检，
          但只注册模型、绝不 start）。副作用：无（不碰网络）。
    """
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [], [f"读取存档失败 ({session_path}): {e}"]

    slave_svc = SlaveService.__new__(SlaveService)
    # 为什么用 __new__：跳过 __init__ 的 LoggingService 依赖（日志写盘），
    # restore_devices 本身不需要日志通道；只补齐它真正用到的内部字典。
    slave_svc._devices = {}
    slave_svc._active_engines = {}
    slave_svc.logging_service = None
    _, warnings = slave_svc.restore_devices(payload.get("slaves", []))
    return slave_svc.get_devices(), warnings


def compute_local_rows(slave: SlaveDevice) -> List[Tuple[str, Any, Any]]:
    """计算一台从机所有点位的 (point_key, 解算值, 换算后工程值)。

    参数：slave（恢复出的从机实例，点表已带 .value）。
    返回：逐点三位组列表。

    为什么不走 ModbusSlaveEngine 而是直接走 codec：
    对账只关心"工程值 ↔ 寄存器"的编码关系，codec 的 encode/decode 是
    这条关系的唯一事实源（引擎内部也是调它）。直接调用可避开引擎的
    1-based 地址基准、端口参数与 pymodbus 废弃告警，纯 CPU 计算。
    步骤：数值型把 .value 编码成寄存器再解算回（等价"现场读寄存器"），
          乘 scale 得工程值；非数值型（STRING/BOOL/HEX16）直接取 .value。
    副作用：无（纯函数，不碰网络/文件）。
    """
    rows: List[Tuple[str, Any, Any]] = []
    for pt in slave.points.values():
        key = point_key(pt)
        if not is_scalable(pt):
            rows.append((key, pt.value, pt.value))
            continue
        try:
            data_type = ModbusDataType(pt.data_type.value)
            byte_order = ByteOrderMode(pt.byte_order.value)
            regs = encode_value(pt.value, data_type, byte_order, pt.string_length)
            raw_value = decode_value(regs, data_type, byte_order, pt.string_length)
            scaled_value = raw_value * pt.scale
        except Exception:
            # 值与类型不匹配的坏点位：退回"存的就是换算值"的兜底口径
            raw_value = pt.value
            scaled_value = pt.value * pt.scale
        rows.append((key, raw_value, scaled_value))
    return rows


def diff_with_cloud(
    local: List[Tuple[str, Any, Any]], cloud: Dict[str, Any], tol: float
) -> List[Tuple[str, Any, Any, str]]:
    """本地换算值 vs 云端值逐点容差比对。

    参数：local（(point_key, raw, scaled) 列表）；cloud（{point_key: 云端值}）；
          tol（绝对容差）。返回：(key, 本地, 云端, 状态) 列表。
    状态含义：OK（差值在容差内或相等）/ FAIL / 缺失（本地有云端没有）/
             云端多（云端有本地没有，本地列填 None）。
    """
    results: List[Tuple[str, Any, Any, str]] = []
    seen = set()
    for key, _raw, scaled in local:
        seen.add(key)
        if key not in cloud:
            results.append((key, scaled, None, "缺失"))
            continue
        cloud_val = cloud[key]
        try:
            ok = abs(float(scaled) - float(cloud_val)) <= tol
        except (TypeError, ValueError):
            # 非数值型（字符串/布尔）退回严格相等比较
            ok = scaled == cloud_val
        results.append((key, scaled, cloud_val, "OK" if ok else "FAIL"))
    for key, val in cloud.items():
        if key not in seen:
            results.append((key, None, val, "云端多"))
    return results


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 入口。返回进程退出码（0 成功 / 1 有差异 / 2 环境错误）。"""
    # 为什么显式切 UTF-8：Windows 控制台默认 GBK，表格里的中文字段
    # （从机名/点位描述）在重定向到文件时容易乱码；统一 UTF-8 输出。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    parser = argparse.ArgumentParser(
        description="ModbusStudio 点位对账工具（本地基准值 vs 云端值）")
    parser.add_argument("--session", default="session_state.json",
                        help="session_state.json 路径（默认当前目录）")
    parser.add_argument("--slave", default="",
                        help="只处理指定从机（按名称或 ID，可空=全部）")
    parser.add_argument("--cloud-file", default="",
                        help="云端值 JSON 文件（形如 {point_key: 值}）")
    parser.add_argument("--tolerance", type=float, default=1e-3,
                        help="数值比对绝对容差（默认 0.001）")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 数组输出（而非表格）")
    args = parser.parse_args(argv)

    slaves, warnings = load_slaves(args.session)
    for w in warnings:
        print(f"[恢复告警] {w}", file=sys.stderr)
    if not slaves:
        if not warnings:
            print(f"[错误] 存档中无从机: {args.session}", file=sys.stderr)
        return 2

    if args.slave:
        slaves = [s for s in slaves
                  if s.name == args.slave or s.id == args.slave]
        if not slaves:
            print(f"[错误] 未找到从机: {args.slave}", file=sys.stderr)
            return 2

    # 逐从机收集本地基准值（内存引擎，无网络无监听）
    rows_all: List[Tuple[str, str, Any, Any]] = []  # (slave_name, key, raw, scaled)
    for slave in slaves:
        for key, raw, scaled in compute_local_rows(slave):
            rows_all.append((slave.name, key, raw, scaled))
    if not rows_all:
        print("[错误] 选中从机无任何点位", file=sys.stderr)
        return 2

    if args.cloud_file:
        try:
            with open(args.cloud_file, "r", encoding="utf-8") as f:
                cloud: Dict[str, Any] = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[错误] 云端文件读取失败: {e}", file=sys.stderr)
            return 2

        diff = diff_with_cloud(
            [(r[1], r[2], r[3]) for r in rows_all], cloud, args.tolerance)
        if args.json:
            payload = [
                {"slave": next((r[0] for r in rows_all if r[1] == k), ""),
                 "point_key": k, "local": loc, "cloud": cl, "status": st}
                for k, loc, cl, st in diff
            ]
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        else:
            for k, loc, cl, st in diff:
                print(f"{k}\t本地={loc}\t云端={cl}\t[{st}]")
        fail = [r for r in diff if r[3] != "OK"]
        return 1 if fail else 0

    # 未启用比对：只输出本地基准值表
    if args.json:
        payload = [
            {"slave": s, "point_key": k, "raw": raw, "scaled": scaled}
            for s, k, raw, scaled in rows_all
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{'从机':<16} {'点位键':<28} {'解算值':<16} {'换算值(乘scale)'}")
        for s, k, raw, scaled in rows_all:
            print(f"{s:<16} {k:<28} {raw!s:<16} {scaled!s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
