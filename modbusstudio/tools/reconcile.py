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

from ..modbus_codec import ByteOrderMode, decode_value, encode_value
from ..models import ModbusPoint, SlaveDevice
from .point_value import is_scalable, point_key, scale_value, to_codec_type


def load_slaves(session_path: str) -> Tuple[List[SlaveDevice], List[str]]:
    """从 session_state.json 恢复全部从机（仅内存模型，不启动任何监听）。

    参数：session_path（存档路径）。返回：(slaves, 恢复告警列表)。
    设计原则：对账是只读静态还原，不走服务层（SlaveService），
    避免触发 get_occupied_tcp_ports 对同端口多从机（Server Hub，
    不同 unit_id）的端口占用拦截与 100 台配额截断；逐台调用纯领域模型
    SlaveDevice.from_dict(item)，坏条目收集进 warnings 不中断。
    副作用：无（不碰网络）。
    """
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [], [f"读取存档失败 ({session_path}): {e}"]

    if not isinstance(payload, dict):
        return [], [f"存档格式非法 ({session_path}): 顶层必须是 JSON 对象"]

    slaves: List[SlaveDevice] = []
    warnings: List[str] = []
    raw_slaves = payload.get("slaves", [])
    if not isinstance(raw_slaves, list):
        return [], [f"存档格式非法 ({session_path}): slaves 字段必须是数组"]
    for item in raw_slaves:
        if not isinstance(item, dict):
            warnings.append("无效的从机项 (非字典)，跳过")
            continue
        try:
            device = SlaveDevice.from_dict(item)
            slaves.append(device)
        except Exception as e:
            name = item.get("name", "未知") if isinstance(item, dict) else "未知"
            warnings.append(f"反序列化从机失败 [{name}]: {e}")
    return slaves, warnings


def _parse_raw_hex_registers(raw_hex: str) -> Optional[List[int]]:
    """解析类似 '0x089D' 或 '0x435C 0x8000' 的 raw_hex 字符串为 16 位寄存器列表。"""
    if not raw_hex or not isinstance(raw_hex, str):
        return None
    parts = raw_hex.strip().split()
    if not parts:
        return None
    regs: List[int] = []
    for token in parts:
        try:
            regs.append(int(token, 16) & 0xFFFF)
        except ValueError:
            return None
    return regs


def compute_local_rows(slave: SlaveDevice) -> List[Tuple[str, Any, Any]]:
    """计算一台从机所有点位的 (point_key, 解算值, 换算后工程值)。

    兼容两种存档语义：
    1. ModbusStudio 新版保存的会话存档：pt.value 存工程源数据、pt.raw_hex 存反算后的真实寄存器字，
       若 raw_hex 反解并套 scale+offset 后与 pt.value 一致，则直接输出 (raw_from_hex, eng)，避免双重缩放；
    2. 传统/测试最小存档（无匹配 raw_hex）：按 pt.value 视为原始值，编码再解算并套 scale_value(raw, pt)。
    """
    rows: List[Tuple[str, Any, Any]] = []
    for pt in slave.points.values():
        key = point_key(pt)
        if not is_scalable(pt):
            rows.append((key, pt.value, pt.value))
            continue
        try:
            data_type = to_codec_type(pt.data_type)
            byte_order = ByteOrderMode(pt.byte_order.value)
            # 优先识别已带真实 raw_hex 的工程值存档，避免对已换算的 pt.value 二次乘 scale 加 offset
            hex_regs = _parse_raw_hex_registers(getattr(pt, "raw_hex", ""))
            matched_from_hex = False
            if hex_regs and len(hex_regs) >= pt.register_count:
                raw_from_hex = decode_value(hex_regs[: pt.register_count], data_type, byte_order, pt.string_length)
                scaled_from_hex = scale_value(raw_from_hex, pt)
                if isinstance(scaled_from_hex, (int, float)) and isinstance(pt.value, (int, float)) and not isinstance(pt.value, bool):
                    if abs(float(scaled_from_hex) - float(pt.value)) <= 1e-5:
                        raw_value = raw_from_hex
                        scaled_value = scaled_from_hex
                        matched_from_hex = True

            if not matched_from_hex:
                regs = encode_value(pt.value, data_type, byte_order, pt.string_length)
                raw_value = decode_value(regs, data_type, byte_order, pt.string_length)
                scaled_value = scale_value(raw_value, pt)
        except Exception:
            raw_value = pt.value
            scaled_value = scale_value(pt.value, pt)
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

    比对规则：
    1. 布尔点位（isinstance(scaled, bool)）：严格要求云端也是布尔且值相等，
       严禁进 float 容差（防止 float(True)=1.0 把云端 0/1/1.0005 等数值吸收为 OK）；
    2. 字符串/十六进制（isinstance(scaled, str)）：严格全等比对；
    3. 数值型点位：在云端非 bool 时进入 float 绝对容差比对 abs(float(a)-float(b))<=tol，
       异常时退回严格相等。
    """
    results: List[Tuple[str, Any, Any, str]] = []
    seen = set()
    for key, _raw, scaled in local:
        seen.add(key)
        if key not in cloud:
            results.append((key, scaled, None, "缺失"))
            continue
        cloud_val = cloud[key]
        if isinstance(scaled, bool):
            ok = isinstance(cloud_val, bool) and scaled == cloud_val
        elif isinstance(scaled, str):
            ok = scaled == cloud_val
        elif isinstance(cloud_val, bool):
            # 本地是数值但云端是 bool，类型不符直接判定 FAIL
            ok = False
        else:
            try:
                ok = abs(float(scaled) - float(cloud_val)) <= tol
            except (TypeError, ValueError):
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
                cloud_raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[错误] 云端文件读取失败: {e}", file=sys.stderr)
            return 2

        if not isinstance(cloud_raw, dict):
            print("[错误] 云端文件顶层必须是 JSON 对象 ({point_key: 值})", file=sys.stderr)
            return 2
        cloud: Dict[str, Any] = cloud_raw

        # 多从机同 point_key 防覆盖：若存在多台从机共享同名 point_key，
        # 支持云端按 "slave_name|point_key" 精确区分，未带前缀时回退 plain point_key 且保留逐从机归属
        multi_slave = len(slaves) > 1
        key_counts: Dict[str, int] = {}
        for _s, k, _raw, _sc in rows_all:
            key_counts[k] = key_counts.get(k, 0) + 1
        has_dup_key = multi_slave and any(c > 1 for c in key_counts.values())

        if has_dup_key:
            # 逐从机构建带归属的对账记录，避免 dict 键覆盖丢失早先从机信息
            matched_cloud_keys = set()
            multi_diff: List[Tuple[str, str, Any, Any, str]] = []
            for s_name, k, _raw, scaled in rows_all:
                prefixed = f"{s_name}|{k}"
                if prefixed in cloud:
                    matched_cloud_keys.add(prefixed)
                    sub = diff_with_cloud([(prefixed, _raw, scaled)], {prefixed: cloud[prefixed]}, args.tolerance)
                    _, loc, cl, st = sub[0]
                    multi_diff.append((s_name, k, loc, cl, st))
                elif k in cloud:
                    matched_cloud_keys.add(k)
                    sub = diff_with_cloud([(k, _raw, scaled)], {k: cloud[k]}, args.tolerance)
                    _, loc, cl, st = sub[0]
                    multi_diff.append((s_name, k, loc, cl, st))
                else:
                    multi_diff.append((s_name, k, scaled, None, "缺失"))
            for ck, cval in cloud.items():
                if ck not in matched_cloud_keys:
                    multi_diff.append(("", ck, None, cval, "云端多"))

            if args.json:
                payload = [
                    {"slave": s_name, "point_key": k, "local": loc, "cloud": cl, "status": st}
                    for s_name, k, loc, cl, st in multi_diff
                ]
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            else:
                for s_name, k, loc, cl, st in multi_diff:
                    prefix = f"[{s_name}] " if s_name else ""
                    print(f"{prefix}{k}\t本地={loc}\t云端={cl}\t[{st}]")
            fail = [r for r in multi_diff if r[4] != "OK"]
            return 1 if fail else 0

        diff = diff_with_cloud(
            [(r[1], r[2], r[3]) for r in rows_all], cloud, args.tolerance)
        if args.json:
            key_to_slave = {r[1]: r[0] for r in rows_all}
            payload = [
                {"slave": key_to_slave.get(k, ""),
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
