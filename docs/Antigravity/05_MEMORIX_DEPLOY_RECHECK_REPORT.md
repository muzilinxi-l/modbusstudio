# 05-补 Memorix 部署验收证据补齐复核报告（F:/GitHub/python）

> **关联质询单**：[`docs/Claude code/05b_MEMORIX_RECHECK_PROMPT.md`](file:///F:/GitHub/python/docs/Claude%20code/05b_MEMORIX_RECHECK_PROMPT.md)
> **原始部署报告**：[`docs/Antigravity/05_MEMORIX_DEPLOY_REPORT.md`](file:///F:/GitHub/python/docs/Antigravity/05_MEMORIX_DEPLOY_REPORT.md)
> **执行方**：Antigravity
> **复核方**：Claude Code
> **日期**：2026-10-12
> **执行性质**：只补齐可复现证据目录 + 清理 SQLite 活跃测试残留，未改动已通过的部署配置、`memory/` 正式协议文件及 `memory/verify.py`。

---

## 〇、任务 1–4 复核结论与原始报告差异对比总表

| 任务项 | 对应验收项 | 本次复现状态 | 与首次报告 (`05_MEMORIX_DEPLOY_REPORT.md`) 的差异说明 |
|---|---|---|---|
| **任务 1** | **验收 B（共享 + 清残留）** | **复现通过** | 1. 首次报告用了 `MODBUS_SYNC_CHECK_20261012` (id=1)，本次改用新标识词 `MEMORIX_RECHECK_20261012` (id=3)，三步 JSON 均真实输出。<br>2. 收尾将历史残留 `id=1,2` 与本次 `id=3` 全部 `resolve --status archived`，`memorix doctor` 的 `Observations` 从 `2 total, 1 active` 变为 **`3 total, 0 active`**（活跃残留彻底清零）。 |
| **任务 2** | **验收 C（项目隔离）** | **复现通过** | 首次报告测完删除了临时仓库；本次在 B 记忆（`MEMORIX_RECHECK_20261012`，id=3）尚处于 `active` 状态时，于 [`docs/Antigravity/recheck/C_isolated_project`](file:///F:/GitHub/python/docs/Antigravity/recheck/C_isolated_project) 检索返回 `"entries": []`，且**永久保留该测试目录**供 Claude 随时复核。 |
| **任务 3** | **验收 D（防失真拦截）** | **复现通过** | 首次报告测完删除了副本；本次新建并**永久保留** [`docs/Antigravity/recheck/D_memory`](file:///F:/GitHub/python/docs/Antigravity/recheck/D_memory)，内含 `MEM-9001`/`MEM-9002`（同 topic 矛盾 `proposed`）与 `MEM-9003`（`proposals/` 内违规升格 `verified`），实跑 `verify.py` 输出与首次报告完全一致（`FAIL`，Check 7 + Check 8 共 3 项错误）。 |
| **任务 4** | **验收 F.1（坏数据拦截）+ F.2（正式库 PASS 对照）** | **复现通过** | 首次报告测完删除了坏数据目录；本次新建并**永久保留** [`docs/Antigravity/recheck/F_bad`](file:///F:/GitHub/python/docs/Antigravity/recheck/F_bad)，实跑 `verify.py` 精准报出 Check 2/3/4/5 共 4 项错误（`FAIL`）；同时对正式 `memory/` 实跑全量 `verify.py`（含 `pytest` 41 passed）全绿 `PASS`。 |

---

## 一、保留的证据目录路径清单（供 Claude 直接复核）

以下证据目录及文件均已永久保留在 [`F:\GitHub\python\docs\Antigravity\recheck\`](file:///F:/GitHub/python/docs/Antigravity/recheck) 下，未删除：

| 证据目录 / 文件路径 | 用途说明 | Claude 独立复核可直接执行的命令 |
|---|---|---|
| [`docs/Antigravity/recheck/C_isolated_project/`](file:///F:/GitHub/python/docs/Antigravity/recheck/C_isolated_project) | 独立 Git 测试仓库（`origin: https://github.com/example/c-isolated-project.git`） | `memorix memory search --query "MEMORIX_RECHECK_20261012" --cwd "F:\GitHub\python\docs\Antigravity\recheck\C_isolated_project" --json` |
| [`docs/Antigravity/recheck/D_memory/`](file:///F:/GitHub/python/docs/Antigravity/recheck/D_memory) | 验收 D 记忆副本（含 [`proposals/test_contradiction.md`](file:///F:/GitHub/python/docs/Antigravity/recheck/D_memory/proposals/test_contradiction.md) 与 [`verify-report.txt`](file:///F:/GitHub/python/docs/Antigravity/recheck/D_memory/verify-report.txt)） | `F:\GitHub\python\.venv\Scripts\python.exe F:\GitHub\python\memory\verify.py --memory-dir "F:\GitHub\python\docs\Antigravity\recheck\D_memory" --skip-pytest` |
| [`docs/Antigravity/recheck/F_bad/`](file:///F:/GitHub/python/docs/Antigravity/recheck/F_bad) | 验收 F.1 坏数据副本（含 [`proposals/bad_sample.md`](file:///F:/GitHub/python/docs/Antigravity/recheck/F_bad/proposals/bad_sample.md) 与 [`verify-report.txt`](file:///F:/GitHub/python/docs/Antigravity/recheck/F_bad/verify-report.txt)） | `F:\GitHub\python\.venv\Scripts\python.exe F:\GitHub\python\memory\verify.py --memory-dir "F:\GitHub\python\docs\Antigravity\recheck\F_bad" --skip-pytest` |
| [`memory/`](file:///F:/GitHub/python/memory) | 正式干净记忆库（含 [`memory/verify-report.txt`](file:///F:/GitHub/python/memory/verify-report.txt)） | `F:\GitHub\python\.venv\Scripts\python.exe F:\GitHub\python\memory\verify.py` |

### 磁盘保留文件实测列表（`Get-ChildItem` 真实输出）
```text
FullName                                                               Mode  Length
--------                                                               ----  ------
F:\GitHub\python\docs\Antigravity\recheck\C_isolated_project           d---- 
F:\GitHub\python\docs\Antigravity\recheck\D_memory                     d---- 
F:\GitHub\python\docs\Antigravity\recheck\F_bad                        d---- 
F:\GitHub\python\docs\Antigravity\recheck\C_isolated_project\README.md -a--- 51
F:\GitHub\python\docs\Antigravity\recheck\D_memory\agents              d---- 
F:\GitHub\python\docs\Antigravity\recheck\D_memory\conflicts           d---- 
F:\GitHub\python\docs\Antigravity\recheck\D_memory\decisions           d---- 
F:\GitHub\python\docs\Antigravity\recheck\D_memory\handoffs            d---- 
F:\GitHub\python\docs\Antigravity\recheck\D_memory\proposals           d---- 
F:\GitHub\python\docs\Antigravity\recheck\D_memory\ARCHITECTURE.md     -a--- 1902
F:\GitHub\python\docs\Antigravity\recheck\D_memory\CONSTRAINTS.md      -a--- 1286
F:\GitHub\python\docs\Antigravity\recheck\D_memory\INDEX.md            -a--- 2442
F:\GitHub\python\docs\Antigravity\recheck\D_memory\INTERFACES.md       -a--- 1264
F:\GitHub\python\docs\Antigravity\recheck\D_memory\PROJECT.md          -a--- 930
F:\GitHub\python\docs\Antigravity\recheck\D_memory\TASKS.md            -a--- 911
F:\GitHub\python\docs\Antigravity\recheck\D_memory\verify-report.txt   -a--- 6427
F:\GitHub\python\docs\Antigravity\recheck\F_bad\agents                 d---- 
F:\GitHub\python\docs\Antigravity\recheck\F_bad\conflicts              d---- 
F:\GitHub\python\docs\Antigravity\recheck\F_bad\decisions              d---- 
F:\GitHub\python\docs\Antigravity\recheck\F_bad\handoffs               d---- 
F:\GitHub\python\docs\Antigravity\recheck\F_bad\proposals              d---- 
F:\GitHub\python\docs\Antigravity\recheck\F_bad\ARCHITECTURE.md        -a--- 1902
F:\GitHub\python\docs\Antigravity\recheck\F_bad\CONSTRAINTS.md         -a--- 1286
F:\GitHub\python\docs\Antigravity\recheck\F_bad\INDEX.md               -a--- 2442
F:\GitHub\python\docs\Antigravity\recheck\F_bad\INTERFACES.md          -a--- 1264
F:\GitHub\python\docs\Antigravity\recheck\F_bad\PROJECT.md             -a--- 930
F:\GitHub\python\docs\Antigravity\recheck\F_bad\TASKS.md               -a--- 911
F:\GitHub\python\docs\Antigravity\recheck\F_bad\verify-report.txt      -a--- 6169
```

---

## 二、任务 1：重跑验收 B（含清残留与 `memorix doctor` 前后对比）

### 2.1 清理前 `memorix doctor` 的 `Data Status` 真实输出
```text
┌─ Data Status ─────────────────────────────────────
  [OK] Backend: sqlite (read-write)
  [OK] Observations: 2 total, 1 active
  [OK] Sessions: 0 total, 0 active
```

### 2.2 三步跨 Agent 写入与检索真实 JSON 输出（标识词：`MEMORIX_RECHECK_20261012`）
```text
=== [Task 1 - B.1] Antigravity 写入测试记忆 MEMORIX_RECHECK_20261012 ===
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "observation": {
    "id": 3,
    "entityName": "general",
    "type": "discovery",
    "title": "Recheck Test B Shared Memory",
    "narrative": "Antigravity recheck shared memory marker: MEMORIX_RECHECK_20261012",
    "facts": [],
    "filesModified": [],
    "concepts": [],
    "tokens": 24,
    "createdAt": "2026-10-12T05:37:06.157Z",
    "projectId": "muzilinxi-l/modbusstudio",
    "hasCausalLanguage": false,
    "topicKey": "discovery/recheck-test-b-shared-memory",
    "revisionCount": 1,
    "status": "active",
    "source": "manual",
    "visibility": "project",
    "createdByAgentId": "64266e40-4eef-4ed6-b781-74ee45805dbb",
    "writeGeneration": 6
  },
  "upserted": false
}

=== [Task 1 - B.2] 切换 Claude 身份检索 MEMORIX_RECHECK_20261012 ===
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "entries": [
    {
      "id": 3,
      "time": "1:37 PM",
      "type": "discovery",
      "icon": "[DISCOVERY]",
      "title": "Recheck Test B Shared Memory",
      "tokens": 24,
      "score": 0.5488746482085489,
      "projectId": "muzilinxi-l/modbusstudio",
      "source": "manual",
      "visibility": "project",
      "entityName": "general",
      "documentType": "observation",
      "knowledgeLayer": "project-truth",
      "matchedFields": [
        "narrative"
      ]
    }
  ]
}

=== [Task 1 - B.3] 模拟重启 CLI 新进程检索 MEMORIX_RECHECK_20261012 ===
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "entries": [
    {
      "id": 3,
      "time": "1:37 PM",
      "type": "discovery",
      "icon": "[DISCOVERY]",
      "title": "Recheck Test B Shared Memory",
      "tokens": 24,
      "score": 0.5488746482085489,
      "projectId": "muzilinxi-l/modbusstudio",
      "source": "manual",
      "visibility": "project",
      "entityName": "general",
      "documentType": "observation",
      "knowledgeLayer": "project-truth",
      "matchedFields": [
        "narrative"
      ]
    }
  ]
}
```

### 2.3 收尾归档全部残留（含旧 `id=1, 2` 与本次 `id=3`）及清理后 `memorix doctor` 真实输出
```text
=== [Task 1 - Cleanup] 归档全部测试与残留记忆 (IDs: 1, 2, 3) ===
[memorix] Embedding disabled (MEMORIX_EMBEDDING=off) — using BM25 fulltext search
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "result": {
    "resolved": [
      1,
      2,
      3
    ],
    "notFound": []
  },
  "status": "archived"
}

=== [After Cleanup] 清理后 memorix doctor 验证 Observations 回到 0 active ===

┌─ Project Identity ─────────────────────────────────
  [OK] Project ID: muzilinxi-l/modbusstudio
  [OK] Name: modbusstudio
  [OK] Root: F:\GitHub\python
  [OK] Git remote: https://github.com/muzilinxi-l/modbusstudio.git
  [OK] Data dir: C:\Users\123\.memorix\data

┌─ Runtime Mode ─────────────────────────────────────
    Background control plane: not running
    Current invocation: CLI (stdio)

┌─ Embedding / Provider ─────────────────────────────
    Mode: off (BM25 fulltext only)
    Search: fulltext keyword matching

┌─ Data Status ─────────────────────────────────────
  [OK] Backend: sqlite (read-write)
  [OK] Observations: 3 total, 0 active
  [OK] Sessions: 0 total, 0 active
```

---

## 三、任务 2：重跑验收 C（项目隔离，保留 `docs/Antigravity/recheck/C_isolated_project`）

在任务 1 的测试记忆 `MEMORIX_RECHECK_20261012`（`id: 3`）尚未归档销毁前，在独立 Git 仓库 `F:\GitHub\python\docs\Antigravity\recheck\C_isolated_project` 下执行检索，真实输出如下：

```text
=== [Task 2 - C] 新建并保留 docs/Antigravity/recheck/C_isolated_project 并在 B 未销毁前检索 ===
{
  "project": {
    "id": "example/c-isolated-project",
    "name": "c-isolated-project",
    "gitRemote": "https://github.com/example/c-isolated-project.git",
    "rootPath": "F:\\GitHub\\python\\docs\\Antigravity\\recheck\\C_isolated_project"
  },
  "entries": []
}
```

---

## 四、任务 3：重跑验收 D（防失真，保留 `docs/Antigravity/recheck/D_memory`）

对保留副本 [`docs/Antigravity/recheck/D_memory`](file:///F:/GitHub/python/docs/Antigravity/recheck/D_memory) 执行 `verify.py`，真实终端输出如下：

```text
========================================================================
Memorix 共享记忆防失真自动验证报告 (memory/verify.py)
========================================================================
生成时间    : 2026-10-12T13:38:00.043624+08:00
仓库根目录  : F:\GitHub\python
记忆目录    : F:\GitHub\python\docs\Antigravity\recheck\D_memory
总体结论    : FAIL (错误: 3, 警告: 0)
重要声明    : 脚本通过 ≠ 语义正确。所有无法自动验证的语义判断均已标记为 needs_human_review。
------------------------------------------------------------------------
【分项检查摘要】
  - 1_required_structure: PASS
  - 2_unique_id: PASS
  - 3_has_source_field: PASS
  - 4_valid_git_sha: PASS
  - 5_source_file_exists: PASS
  - 6_code_drift_since_commit: PASS
  - 7_unreviewed_formal_memory_guard: FAIL
  - 8_conflict_and_status_check: FAIL
  - 9_worktree_and_unmerged_changes: PASS
  - 10_pytest_execution: SKIPPED
  - 11_needs_human_review: INFO
------------------------------------------------------------------------
【错误详情 (ERRORS)】
  [X] [Check 7] MEM-9003 (proposals/test_contradiction.md) 位于 proposals/ 候选区却被标为 verified（严禁自动/原地升格）
  [X] [Check 7] MEM-9003 (proposals/test_contradiction.md) 状态为 verified，但 verified_by 为 'auto'（未经有效审核）
  [X] [Check 8] 检测到同一主题 'default_tcp_port' 存在多条矛盾的 proposed 记忆 (MEM-9001, MEM-9002) -> 必须标记为 disputed 并移入 conflicts/
```

---

## 五、任务 4：重跑验收 F.1（坏数据拦截，保留 `docs/Antigravity/recheck/F_bad`）与正式 `memory/` 对照

### 5.1 对坏数据副本 `docs/Antigravity/recheck/F_bad` 运行 `verify.py` 真实输出
```text
========================================================================
Memorix 共享记忆防失真自动验证报告 (memory/verify.py)
========================================================================
生成时间    : 2026-10-12T13:38:01.286835+08:00
仓库根目录  : F:\GitHub\python
记忆目录    : F:\GitHub\python\docs\Antigravity\recheck\F_bad
总体结论    : FAIL (错误: 4, 警告: 0)
重要声明    : 脚本通过 ≠ 语义正确。所有无法自动验证的语义判断均已标记为 needs_human_review。
------------------------------------------------------------------------
【分项检查摘要】
  - 1_required_structure: PASS
  - 2_unique_id: FAIL
  - 3_has_source_field: FAIL
  - 4_valid_git_sha: FAIL
  - 5_source_file_exists: FAIL
  - 6_code_drift_since_commit: PASS
  - 7_unreviewed_formal_memory_guard: PASS
  - 8_conflict_and_status_check: PASS
  - 9_worktree_and_unmerged_changes: PASS
  - 10_pytest_execution: SKIPPED
  - 11_needs_human_review: INFO
------------------------------------------------------------------------
【错误详情 (ERRORS)】
  [X] [Check 2] 重复编号 MEM-0001 同时出现在 PROJECT.md 与 proposals/bad_sample.md
  [X] [Check 3] 记忆 MEM-8002 (proposals/bad_sample.md) 缺少 source 字段
  [X] [Check 4] 记忆 MEM-0001 (proposals/bad_sample.md) 的 commit 无效: 'deadbeef99999999999999999999999999999999'
  [X] [Check 5] 记忆 MEM-8003 (proposals/bad_sample.md) 引用的 source 文件不存在: modbusstudio/ghost_non_existent_module.py
```

### 5.2 对正式干净 `memory/` 运行全量 `verify.py`（含 `pytest` 41 passed）真实输出
```text
========================================================================
Memorix 共享记忆防失真自动验证报告 (memory/verify.py)
========================================================================
生成时间    : 2026-10-12T13:38:52.751192+08:00
仓库根目录  : F:\GitHub\python
记忆目录    : F:\GitHub\python\memory
总体结论    : PASS (错误: 0, 警告: 0)
重要声明    : 脚本通过 ≠ 语义正确。所有无法自动验证的语义判断均已标记为 needs_human_review。
------------------------------------------------------------------------
【分项检查摘要】
  - 1_required_structure: PASS
  - 2_unique_id: PASS
  - 3_has_source_field: PASS
  - 4_valid_git_sha: PASS
  - 5_source_file_exists: PASS
  - 6_code_drift_since_commit: PASS
  - 7_unreviewed_formal_memory_guard: PASS
  - 8_conflict_and_status_check: PASS
  - 9_worktree_and_unmerged_changes: PASS
  - 10_pytest_execution: PASS
  - 11_needs_human_review: INFO
------------------------------------------------------------------------
【JSON 完整载荷（节选 pytest 项）】
    "10_pytest_execution": {
      "status": "PASS",
      "command": "F:\\GitHub\\python\\.venv\\Scripts\\pytest.exe -q",
      "exit_code": 0,
      "passed": true,
      "summary": "41 passed in 24.24s",
      "stdout_tail": ".........................................                                [100%]\n41 passed in 24.24s",
      "stderr_tail": ""
    }
Formal memory/ verify.py exit code: 0
```
