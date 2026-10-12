#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory/verify.py — Memorix 多 Agent 共享记忆防失真自动验证器
仅使用 Python 标准库 + Git + pytest (python/.venv)

检查项（11 项）：
1. 关键记忆文件与目录是否存在
2. 记忆编号 (id) 全局唯一性
3. 重要记忆是否包含非空 source 字段
4. commit 字段是否为仓库内有效 Git SHA (git cat-file -e)
5. source 指向的文件在仓库中是否存在
6. 记忆引用的代码自该 commit 以来是否已发生变化 (git diff)
7. 是否有未审核的正式记忆被篡改 / proposed 非法升格为 verified
8. 重复编号 / 矛盾候选记忆 (disputed) / 非法状态值检测
9. 其他 Agent 的未合并修改或活跃 Worktree 检查
10. 自动运行一次 pytest (python/.venv)，测试结果并入校验
11. 无法自动验证的语义事实标记为 needs_human_review（脚本通过 != 语义正确）
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REQUIRED_FILES = [
    "INDEX.md",
    "PROJECT.md",
    "ARCHITECTURE.md",
    "INTERFACES.md",
    "CONSTRAINTS.md",
    "TASKS.md",
]

REQUIRED_DIRS = [
    "decisions",
    "proposals",
    "conflicts",
    "handoffs",
    "agents",
]

VALID_STATUSES = {"proposed", "verified", "disputed", "outdated"}

YAML_BLOCK_RE = re.compile(r"```yaml\s*\r?\n(.*?)\r?\n```", re.DOTALL)


def parse_yaml_block(block_text: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for raw_line in block_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        data[key.strip()] = val.strip()
    return data


def extract_memory_records(memory_dir: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for md_file in sorted(memory_dir.rglob("*.md")):
        rel_md = md_file.relative_to(memory_dir).as_posix()
        text = md_file.read_text(encoding="utf-8")
        for idx, match in enumerate(YAML_BLOCK_RE.finditer(text), start=1):
            meta = parse_yaml_block(match.group(1))
            if not any(k in meta for k in ("id", "status", "source", "commit")):
                continue
            records.append(
                {
                    "file": rel_md,
                    "abs_file": str(md_file),
                    "block_index": idx,
                    "meta": meta,
                }
            )
    return records


def run_git(repo_root: Path, args: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["git"] + args,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def is_valid_commit(repo_root: Path, sha: str) -> bool:
    if not sha or not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        return False
    code, _, _ = run_git(repo_root, ["cat-file", "-e", f"{sha}^{{commit}}"])
    return code == 0


def has_code_changed_since_commit(repo_root: Path, sha: str, source_rel: str) -> bool:
    code, out, _ = run_git(repo_root, ["diff", "--name-only", sha, "HEAD", "--", source_rel])
    if code != 0:
        return True
    return bool(out.strip())


def run_pytest(repo_root: Path) -> Dict[str, Any]:
    venv_pytest = repo_root / ".venv" / "Scripts" / "pytest.exe"
    cmd = [str(venv_pytest), "-q"] if venv_pytest.exists() else [sys.executable, "-m", "pytest", "-q"]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    summary_line = ""
    for line in reversed(proc.stdout.splitlines()):
        if line.strip():
            summary_line = line.strip()
            break
    return {
        "command": " ".join(cmd),
        "exit_code": proc.returncode,
        "passed": proc.returncode == 0,
        "summary": summary_line,
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-15:]),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-10:]),
    }


def verify_memory(
    repo_root: Path,
    memory_dir: Path,
    skip_pytest: bool = False,
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    needs_human_review: List[Dict[str, str]] = []
    checks: Dict[str, Dict[str, Any]] = {}

    # 1. 关键记忆文件与子目录是否存在
    missing_files = [f for f in REQUIRED_FILES if not (memory_dir / f).is_file()]
    missing_dirs = [d for d in REQUIRED_DIRS if not (memory_dir / d).is_dir()]
    if missing_files or missing_dirs:
        if missing_files:
            errors.append(f"[Check 1] 缺少关键记忆文件: {', '.join(missing_files)}")
        if missing_dirs:
            errors.append(f"[Check 1] 缺少关键记忆子目录: {', '.join(missing_dirs)}")
        checks["1_required_structure"] = {
            "status": "FAIL",
            "missing_files": missing_files,
            "missing_dirs": missing_dirs,
        }
    else:
        checks["1_required_structure"] = {
            "status": "PASS",
            "detail": f"全部 {len(REQUIRED_FILES)} 个核心文件与 {len(REQUIRED_DIRS)} 个分区目录均存在",
        }

    records = extract_memory_records(memory_dir)

    # 2. 记忆编号唯一性
    seen_ids: Dict[str, str] = {}
    duplicate_ids: List[str] = []
    for rec in records:
        mem_id = rec["meta"].get("id", "").strip()
        if not mem_id:
            errors.append(f"[Check 2] {rec['file']}#block{rec['block_index']} 缺少 id 字段")
            continue
        if mem_id in seen_ids:
            msg = f"重复编号 {mem_id} 同时出现在 {seen_ids[mem_id]} 与 {rec['file']}"
            duplicate_ids.append(msg)
            errors.append(f"[Check 2] {msg}")
        else:
            seen_ids[mem_id] = rec["file"]
    checks["2_unique_id"] = {
        "status": "FAIL" if duplicate_ids or any("Check 2" in e for e in errors) else "PASS",
        "total_records": len(records),
        "duplicates": duplicate_ids,
    }

    # 3. 重要记忆含 source 字段
    missing_sources: List[str] = []
    for rec in records:
        mem_id = rec["meta"].get("id", f"{rec['file']}#block{rec['block_index']}")
        src = rec["meta"].get("source", "").strip()
        if not src:
            missing_sources.append(mem_id)
            errors.append(f"[Check 3] 记忆 {mem_id} ({rec['file']}) 缺少 source 字段")
    checks["3_has_source_field"] = {
        "status": "FAIL" if missing_sources else "PASS",
        "missing_sources": missing_sources,
    }

    # 4. commit 是否为有效 git sha
    invalid_commits: List[str] = []
    for rec in records:
        mem_id = rec["meta"].get("id", f"{rec['file']}#block{rec['block_index']}")
        sha = rec["meta"].get("commit", "").strip()
        if not is_valid_commit(repo_root, sha):
            invalid_commits.append(f"{mem_id}: {sha or '<empty>'}")
            errors.append(f"[Check 4] 记忆 {mem_id} ({rec['file']}) 的 commit 无效: '{sha}'")
    checks["4_valid_git_sha"] = {
        "status": "FAIL" if invalid_commits else "PASS",
        "invalid_commits": invalid_commits,
    }

    # 5. source 文件是否存在
    missing_source_files: List[str] = []
    for rec in records:
        mem_id = rec["meta"].get("id", f"{rec['file']}#block{rec['block_index']}")
        src = rec["meta"].get("source", "").strip()
        if src and not (repo_root / src).exists():
            missing_source_files.append(f"{mem_id}: {src}")
            errors.append(f"[Check 5] 记忆 {mem_id} ({rec['file']}) 引用的 source 文件不存在: {src}")
    checks["5_source_file_exists"] = {
        "status": "FAIL" if missing_source_files else "PASS",
        "missing_source_files": missing_source_files,
    }

    # 6. 记忆引用的代码是否已变化
    drifted_sources: List[str] = []
    for rec in records:
        mem_id = rec["meta"].get("id", f"{rec['file']}#block{rec['block_index']}")
        src = rec["meta"].get("source", "").strip()
        sha = rec["meta"].get("commit", "").strip()
        if src and (repo_root / src).exists() and is_valid_commit(repo_root, sha):
            if has_code_changed_since_commit(repo_root, sha, src):
                drifted_sources.append(f"{mem_id} ({src} @ {sha[:7]} -> HEAD)")
                warnings.append(
                    f"[Check 6] 记忆 {mem_id} 引用的代码 {src} 自 commit {sha[:7]} 后有更新，需人工复核语义一致性"
                )
                needs_human_review.append(
                    {
                        "id": mem_id,
                        "file": rec["file"],
                        "reason": f"引用源码 {src} 自 {sha[:7]} 以来发生变更",
                    }
                )
    checks["6_code_drift_since_commit"] = {
        "status": "WARN" if drifted_sources else "PASS",
        "drifted_sources": drifted_sources,
    }

    # 7. 是否有未审核的正式记忆被改 / proposed 违规升格为 verified
    unreviewed_violations: List[str] = []
    for rec in records:
        mem_id = rec["meta"].get("id", f"{rec['file']}#block{rec['block_index']}")
        status = rec["meta"].get("status", "").strip()
        verified_by = rec["meta"].get("verified_by", "").strip()
        rel_file = rec["file"]

        if rel_file.startswith("proposals/") and status == "verified":
            msg = f"{mem_id} ({rel_file}) 位于 proposals/ 候选区却被标为 verified（严禁自动/原地升格）"
            unreviewed_violations.append(msg)
            errors.append(f"[Check 7] {msg}")

        if status == "verified" and (not verified_by or verified_by.lower() in {"none", "pending", "null", "auto"}):
            msg = f"{mem_id} ({rel_file}) 状态为 verified，但 verified_by 为 '{verified_by}'（未经有效审核）"
            unreviewed_violations.append(msg)
            errors.append(f"[Check 7] {msg}")

    # 检查 git diff 中是否有已追踪的正式记忆被改但状态非 verified
    _, diff_files_out, _ = run_git(repo_root, ["diff", "--name-only", "HEAD", "--", str(memory_dir)])
    if diff_files_out:
        for df in diff_files_out.splitlines():
            warnings.append(f"[Check 7] 检测到工作区存在未提交的记忆文件改动: {df}")

    checks["7_unreviewed_formal_memory_guard"] = {
        "status": "FAIL" if unreviewed_violations else "PASS",
        "violations": unreviewed_violations,
    }

    # 8. 重复编号 / 冲突状态检测（含同 topic 矛盾 proposed 检测）
    conflict_issues: List[str] = []
    topic_proposals: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        mem_id = rec["meta"].get("id", f"{rec['file']}#block{rec['block_index']}")
        status = rec["meta"].get("status", "").strip()
        topic = rec["meta"].get("topic", "").strip()
        claim = rec["meta"].get("claim", "").strip()

        if status not in VALID_STATUSES:
            msg = f"{mem_id} ({rec['file']}) 包含非法 status 值: '{status}'"
            conflict_issues.append(msg)
            errors.append(f"[Check 8] {msg}")

        if status == "disputed" and not rec["file"].startswith("conflicts/"):
            msg = f"{mem_id} ({rec['file']}) 状态为 disputed，需归档至 conflicts/ 并由人工裁决"
            conflict_issues.append(msg)
            warnings.append(f"[Check 8] {msg}")
            needs_human_review.append(
                {
                    "id": mem_id,
                    "file": rec["file"],
                    "reason": "记忆处于 disputed 争议状态，待人工裁决",
                }
            )

        if status == "proposed" and topic:
            topic_proposals.setdefault(topic, []).append(rec)

    for topic, recs in topic_proposals.items():
        if len(recs) > 1:
            claims = {r["meta"].get("claim", r["file"]) for r in recs}
            if len(claims) > 1:
                ids = ", ".join(r["meta"].get("id", "?") for r in recs)
                msg = f"检测到同一主题 '{topic}' 存在多条矛盾的 proposed 记忆 ({ids}) -> 必须标记为 disputed 并移入 conflicts/"
                conflict_issues.append(msg)
                errors.append(f"[Check 8] {msg}")

    checks["8_conflict_and_status_check"] = {
        "status": "FAIL" if any("Check 8" in e for e in errors) else ("WARN" if conflict_issues else "PASS"),
        "issues": conflict_issues,
    }

    # 9. 其他 agent 的未合并修改 / worktree 检查
    _, wt_out, _ = run_git(repo_root, ["worktree", "list", "--porcelain"])
    worktrees = [line[9:] for line in wt_out.splitlines() if line.startswith("worktree ")]
    _, status_out, _ = run_git(repo_root, ["status", "--porcelain"])
    unmerged_lines = [
        line for line in status_out.splitlines() if line[:2] in {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}
    ]
    if unmerged_lines:
        for ul in unmerged_lines:
            errors.append(f"[Check 9] 存在未解决的 Git 合并冲突: {ul}")
    checks["9_worktree_and_unmerged_changes"] = {
        "status": "FAIL" if unmerged_lines else "PASS",
        "worktrees": worktrees,
        "unmerged_conflicts": unmerged_lines,
        "dirty_files_count": len(status_out.splitlines()) if status_out else 0,
    }

    # 10. 自动跑一次 pytest (python/.venv)
    if skip_pytest:
        checks["10_pytest_execution"] = {
            "status": "SKIPPED",
            "detail": "Skipped via --skip-pytest",
        }
    else:
        pytest_res = run_pytest(repo_root)
        if not pytest_res["passed"]:
            errors.append(f"[Check 10] pytest 执行失败 (exit_code={pytest_res['exit_code']}): {pytest_res['summary']}")
        checks["10_pytest_execution"] = {
            "status": "PASS" if pytest_res["passed"] else "FAIL",
            **pytest_res,
        }

    # 11. 无法自动验证的事实 -> 标 needs_human_review
    for rec in records:
        mem_id = rec["meta"].get("id", f"{rec['file']}#block{rec['block_index']}")
        status = rec["meta"].get("status", "").strip()
        needs_review_flag = rec["meta"].get("needs_human_review", "").strip().lower()
        if status == "proposed" or needs_review_flag == "true":
            needs_human_review.append(
                {
                    "id": mem_id,
                    "file": rec["file"],
                    "reason": "候选记忆 (proposed) 或显式标记需人工核验语义真实性",
                }
            )
        else:
            needs_human_review.append(
                {
                    "id": mem_id,
                    "file": rec["file"],
                    "reason": "静态锚点已通过，但业务语义与设计初衷需人工最终把关（脚本通过 ≠ 语义正确）",
                }
            )

    checks["11_needs_human_review"] = {
        "status": "INFO",
        "disclaimer": "脚本通过 ≠ 语义正确。静态检查仅验证文件、Git SHA 与测试通过，语义准确性须人工复核。",
        "items": needs_human_review,
    }

    overall_passed = len(errors) == 0
    return {
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        "repo_root": str(repo_root),
        "memory_dir": str(memory_dir),
        "overall_status": "PASS" if overall_passed else "FAIL",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


def format_report(result: Dict[str, Any]) -> str:
    lines = [
        "=" * 72,
        "Memorix 共享记忆防失真自动验证报告 (memory/verify.py)",
        "=" * 72,
        f"生成时间    : {result['timestamp']}",
        f"仓库根目录  : {result['repo_root']}",
        f"记忆目录    : {result['memory_dir']}",
        f"总体结论    : {result['overall_status']} (错误: {result['error_count']}, 警告: {result['warning_count']})",
        "重要声明    : 脚本通过 ≠ 语义正确。所有无法自动验证的语义判断均已标记为 needs_human_review。",
        "-" * 72,
        "【分项检查摘要】",
    ]
    for key, val in result["checks"].items():
        lines.append(f"  - {key}: {val.get('status')}")

    if result["errors"]:
        lines.append("-" * 72)
        lines.append("【错误详情 (ERRORS)】")
        for err in result["errors"]:
            lines.append(f"  [X] {err}")

    if result["warnings"]:
        lines.append("-" * 72)
        lines.append("【警告详情 (WARNINGS)】")
        for warn in result["warnings"]:
            lines.append(f"  [!] {warn}")

    lines.append("-" * 72)
    lines.append("【JSON 完整载荷】")
    lines.append(json.dumps(result, ensure_ascii=False, indent=2))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Memorix 共享记忆防失真自动验证脚本")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Git 仓库根目录路径 (默认: F:/GitHub/python)",
    )
    parser.add_argument(
        "--memory-dir",
        default=None,
        help="待验证的 memory 目录路径 (默认: <repo-root>/memory)",
    )
    parser.add_argument(
        "--report-file",
        default=None,
        help="输出报告文件路径 (默认: <memory-dir>/verify-report.txt)",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="跳过 pytest 运行（仅用于快速验证脏数据样本）",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    memory_dir = Path(args.memory_dir).resolve() if args.memory_dir else (repo_root / "memory")
    report_file = Path(args.report_file).resolve() if args.report_file else (memory_dir / "verify-report.txt")

    result = verify_memory(repo_root, memory_dir, skip_pytest=args.skip_pytest)
    report_text = format_report(result)

    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(report_text, encoding="utf-8")

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(report_text)
    return 0 if result["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
