# Memorix 多 Agent 共享记忆系统 — 部署与验收执行报告（F:/GitHub/python）

> **需求下达 / 验收方**：Claude Code ([`docs/Claude code/05_MEMORIX_PROMPT.md`](file:///F:/GitHub/python/docs/Claude%20code/05_MEMORIX_PROMPT.md))
> **执行实施方**：Antigravity
> **执行日期**：2026-10-12
> **部署范围**：严格限定于 `F:/GitHub/python`（Modbus Studio 2.0 项目），未触碰外层 `F:/GitHub` 资料区。
> **接入 Agent**：仅 **Claude Code** 与 **Antigravity**（已彻底剔除 Codex 集成路径）。

---

## 一、实际安装版本与 Node 24 兼容性 Doctor 结果

### 1.1 软件版本实测
| 组件 | 实测版本 / 状态 | 兼容性结论 |
|---|---|---|
| **OS / Shell** | Windows 11 Home 10.0.26300 / PowerShell 7.6.6 | 正常 |
| **Node.js / npm** | `v24.21.0` / `12.0.2` | 满足 `>=22.18.0`，内置 `node:sqlite` 原生支持 |
| **Memorix** | `1.9.10` (`npm install -g memorix`) | 正常，SQLite 读写与 BM25 全文检索健康 |
| **Claude Code CLI** | `2.1.292 (Claude Code)` | 正常 |
| **Python / pytest** | `3.14.7` (`F:/GitHub/python/.venv`) | 正常（全量 41 个测试全部通过） |

### 1.2 `memorix doctor` 真实终端输出
```text
PS F:\GitHub\python> memorix --version; memorix doctor
1.9.10

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
  [OK] Observations: 0 total, 0 active
  [OK] Sessions: 0 total, 0 active

┌─ Project Context ─────────────────────────────────
  [WARN] Code memory: no project scan found yet
    Code provider: not-indexed semantic layer (0 symbols, 0 edges); external graph not-detected
  [OK] Freshness: 0 current memory link(s)

┌─ Knowledge Lifecycle ─────────────────────────────
  [OK] Maintenance: idle
    Claims: 0 active, 0 need review, 0 unknown
    Knowledge workspace: not initialized
    Workflows: 0/0 active, 0 failed run(s)

┌─ Conflict Check ──────────────────────────────────
  [OK] No conflicts detected

┌─ LLM Enhanced Mode ───────────────────────────────
    LLM mode: off (heuristic dedup only)

┌─ Auto-Update ─────────────────────────────────────
    Current version: v1.9.10
    No update checks recorded yet
    Auto-update: enabled (notify only)

┌─ Summary ─────────────────────────────────────────
  [OK] All systems healthy — no issues detected
```

---

## 二、各 Agent 配置路径、已修改文件与备份位置

### 2.1 Antigravity 本机真实配置路径定位结果
经核查本机文件系统与 Antigravity 内置 `agy-customizations` 规范，完整定位如下：
- **全局配置根目录**：`C:\Users\123\.gemini\config\`
- **全局插件目录**：`C:\Users\123\.gemini\config\plugins\memorix\`（与 Memorix 官方 `--agent antigravity --global` 默认路径完全一致）
- **全局 MCP 配置**：`C:\Users\123\.gemini\config\mcp_config.json`
- **项目级插件目录**：`F:\GitHub\python\.agents\plugins\memorix\`
- **项目级 MCP 配置**：`F:\GitHub\python\.agents\mcp_config.json`
- **项目级规则与记忆协议入口**：[`F:\GitHub\python\AGENTS.md`](file:///F:/GitHub/python/AGENTS.md)、[`F:\GitHub\python\GEMINI.md`](file:///F:/GitHub/python/GEMINI.md)
- **运行时数据目录**：`C:\Users\123\.gemini\antigravity\`

### 2.2 事前备份归档位置
所有原始配置文件均在操作前完整备份至 [`F:\GitHub\python\docs\Antigravity\backups\pre-memorix\`](file:///F:/GitHub/python/docs/Antigravity/backups/pre-memorix)：

| 原始路径 | 备份文件 | 原始大小 | 修改方式 |
|---|---|---|---|
| `C:\Users\123\.claude.json` | `claude.json` | 60,069 B | 仅在 `mcpServers` 下增量添加 `memorix` |
| `C:\Users\123\.claude\settings.json` | `claude_settings.json` | 1,655 B | 仅增量添加 `extraKnownMarketplaces` 与 `enabledPlugins` |
| `C:\Users\123\.gemini\config\config.json` | `gemini_config.json` | 7,106 B | 未修改 |
| `C:\Users\123\.gemini\config\mcp_config.json` | `gemini_mcp_config.json` | 0 B | 增量写入 `mcpServers.memorix` |
| `C:\Users\123\.gemini\config\AGENTS.md` | `gemini_AGENTS.md` | 979 B | 未修改 |
| `F:\GitHub\python` Git 状态 | `git_state.txt` | 87 B | 记录基线 Commit: `9bb9932359db5cec3f10b2c9d67f1761ae132bf6` |

### 2.3 新增与增量修改文件清单
1. **全局层**：
   - `C:\Users\123\.memorix\config.toml`（新建全局配置）
   - `C:\Users\123\.memorix\data\memorix.db`（SQLite 数据库）
   - `C:\Users\123\.claude.json`（增量添加 `mcpServers.memorix`）
   - `C:\Users\123\.claude\settings.json`（增量启用 `memorix@memorix-local` 插件）
   - `C:\Users\123\.claude\CLAUDE.md`（Memorix 全局指引）
   - `C:\Users\123\.claude\plugins\marketplaces\memorix-local\plugins\memorix\`（Claude 插件包）
   - `C:\Users\123\.gemini\config\mcp_config.json`（增量添加 `mcpServers.memorix`）
   - `C:\Users\123\.gemini\config\plugins\memorix\`（Antigravity 全局插件包）
2. **项目层（仅限 `F:\GitHub\python`）**：
   - [`AGENTS.md`](file:///F:/GitHub/python/AGENTS.md)（继承协作规范并增量添加第 9 节记忆协议，剔除 Codex）
   - [`CLAUDE.md`](file:///F:/GitHub/python/CLAUDE.md)（Memorix 指引 + 增量追加统一记忆协议与多 Agent 协作规范）
   - [`GEMINI.md`](file:///F:/GitHub/python/GEMINI.md)（与 `CLAUDE.md` 同步）
   - `.claude/settings.json` 与 `.claude/settings.local.json`
   - `.agents/mcp_config.json` 与 `.agents/plugins/memorix/`
   - [`memory/INDEX.md`](file:///F:/GitHub/python/memory/INDEX.md)、[`memory/PROJECT.md`](file:///F:/GitHub/python/memory/PROJECT.md)、[`memory/ARCHITECTURE.md`](file:///F:/GitHub/python/memory/ARCHITECTURE.md)、[`memory/INTERFACES.md`](file:///F:/GitHub/python/memory/INTERFACES.md)、[`memory/CONSTRAINTS.md`](file:///F:/GitHub/python/memory/CONSTRAINTS.md)、[`memory/TASKS.md`](file:///F:/GitHub/python/memory/TASKS.md)
   - `memory/decisions/`、`memory/proposals/`、`memory/conflicts/`、`memory/handoffs/`、`memory/agents/`
   - [`memory/verify.py`](file:///F:/GitHub/python/memory/verify.py) 及输出报告 [`memory/verify-report.txt`](file:///F:/GitHub/python/memory/verify-report.txt)

---

## 三、Setup 安装的 Hooks 详细清单

### 3.1 Claude Code Hooks（位于 `F:\GitHub\python\.claude\settings.local.json` 及全局插件包）
共安装 **5 个生命周期 Hooks**（超时均为 `10s`）：
1. **`SessionStart`**：执行 `memorix.cmd hook`（会话启动时加载交接上下文与项目记忆提示）
2. **`UserPromptSubmit`**：执行 `memorix.cmd hook`（用户提交 Prompt 时触发记忆状态同步）
3. **`PostToolUse`**：执行 `memorix.cmd hook`（工具调用后捕获关键文件变更与观测记录）
4. **`PreCompact`**：执行 `memorix.cmd hook`（上下文压缩前保存关键连续性检查点）
5. **`Stop`**：执行 `memorix.cmd hook`（会话停止时收尾同步）

### 3.2 Antigravity Hooks（位于 `C:\Users\123\.gemini\config\plugins\memorix\hooks.json` 及 `.agents\plugins\memorix\hooks.json`）
共安装 **5 个生命周期 Hooks**（超时均为 `10s`，且在本次会话中已实时触发验证生效）：
1. **`PreInvocation`**：执行 `memorix hook --agent antigravity --event PreInvocation`
2. **`PreToolUse`** (`matcher: "*"`)：执行 `memorix hook --agent antigravity --event PreToolUse`
3. **`PostToolUse`** (`matcher: "*"`)：执行 `memorix hook --agent antigravity --event PostToolUse`
4. **`PostInvocation`**：执行 `memorix hook --agent antigravity --event PostInvocation`
5. **`Stop`**：执行 `memorix hook --agent antigravity --event Stop`

---

## 四、执行过的核心命令与结果汇总

| 序号 | 执行命令 | 退出码 | 结果摘要 |
|---|---|---|---|
| 1 | `Copy-Item ... docs/Antigravity/backups/pre-memorix/` | `0` | 完整备份 `.claude.json`、`settings.json`、`.gemini/config/*` 与 `git_state.txt` |
| 2 | `npm install -g memorix --fetch-timeout=600000` | `0` | 成功全局安装 `memorix@1.9.10`（311 packages in 18s） |
| 3 | `memorix --version; memorix doctor` | `0` | `1.9.10`，`Backend: sqlite (read-write)`，`All systems healthy` |
| 4 | `memorix setup --agent claude --global --dryRun` | `0` | 只读预览 Claude 全局安装项 |
| 5 | `memorix setup --agent antigravity --global --dryRun` | `0` | 只读预览 Antigravity 全局安装项 |
| 6 | `memorix setup --agent claude --global` | `0` | 安装 Claude 插件、写入 `~/.claude/CLAUDE.md` |
| 7 | `memorix setup --agent antigravity --global` | `0` | 安装 Antigravity 插件至 `~/.gemini/config/plugins/memorix` |
| 8 | `memorix repair agents --agent claude` | `0` | 增量写入 `C:\Users\123\.claude.json` 的 `mcpServers.memorix` |
| 9 | `memorix repair agents --agent antigravity` | `0` | 增量写入 `C:\Users\123\.gemini\config\mcp_config.json` 的 `mcpServers.memorix` |
| 10 | `memorix setup --agent claude` | `0` | 生成项目级 `.claude/settings.json`、`.claude/settings.local.json`、`CLAUDE.md` |
| 11 | `memorix setup --agent antigravity` | `0` | 生成项目级 `.agents/plugins/memorix` |
| 12 | `memorix doctor agents --agent claude` | `0` | `claude: ok (No file repair needed)` |
| 13 | `memorix doctor agents --agent antigravity` | `0` | `antigravity: ok (No file repair needed)` |
| 14 | `memorix identity join --agent-type antigravity --name antigravity --instance-id main` | `0` | 注册 Antigravity 成员 (`64266e40-4eef-4ed6-b781-74ee45805dbb`) |
| 15 | `memorix identity join --agent-type claude --name claude --instance-id main` | `0` | 注册 Claude 成员 (`4978511a-d2cb-4b1d-8cbf-7b2886bc7029`) |
| 16 | `F:\GitHub\python\.venv\Scripts\python.exe memory/verify.py` | `0` | 11 项检查全绿 `PASS`，内置自动执行 `pytest` 41 passed |

---

## 五、验收测试 A–F 逐项真实输出

| 验收项 | 测试内容 | 状态 |
|---|---|---|
| **验收 A（基础）** | `memorix doctor agents` 正常；两 CLI 能起；原 MCP/Skills/Hooks 未被破坏（备份 diff 佐证） | **通过** |
| **验收 B（共享）** | Antigravity 写一条测试记忆 → Claude 检索到；重启 CLI 后仍可检索（测完归档不污染） | **通过** |
| **验收 C（项目隔离）** | 另一独立测试 Git 项目读不到本项目专属记忆 | **通过** |
| **验收 D（防失真）** | `proposed` 不被自动升级 `verified`（违规升格即报错）；两条矛盾 `proposed` 被识别并要求转 `disputed` | **通过** |
| **验收 E（并发/冲突）** | Memorix 文件锁拦截 + Git 双分支同时改同一文件不静默覆盖，保留冲突标记强制人工审查 | **通过** |
| **验收 F（verify.py）** | 对含坏数据（无效 sha、缺 source、重复编号、不存在文件）测试集精准报错；对干净数据全绿通过 | **通过** |

---

### 验收 A 真实输出：Doctor、CLI 启动与配置无损 Diff

```text
=== [A.1] memorix doctor agents ===
Memorix Agent Doctor
Project: F:\GitHub\python
Scope: all

claude: ok

No file repair needed.
Memorix Agent Doctor
Project: F:\GitHub\python
Scope: all

antigravity: ok

No file repair needed.

=== [A.2] CLI versions & skills check ===
2.1.292 (Claude Code)
1.9.10
Antigravity global skills count: 22 (all 22 original skills intact)

=== [A.3] 配置文件备份 Diff 核验 ===
=== .claude.json mcpServers keys (before) ===
(empty)
=== .claude.json mcpServers keys (after) ===
memorix
=== .claude.json mcpServers.memorix ===
{"command":"memorix","args":["serve","--mode","lite"],"alwaysLoad":true}
=== .claude/settings.json keys (before) ===
env, modelSettings, autoCompactWindow, autoCompactEnabled
=== .claude/settings.json keys (after) ===
env, extraKnownMarketplaces, modelSettings, autoCompactWindow, autoCompactEnabled, enabledPlugins
=== enabledPlugins (after) ===
{"memorix@memorix-local":true}
```

---

### 验收 B 真实输出：Antigravity 写入 → Claude 检索 → 重启 CLI 新进程检索

> 注：测试中还额外验证了 Memorix 内置的敏感信息脱敏机制（含 `token: ...` 字样的文本会被自动替换为 `[REDACTED]`）；随后使用正常业务标识词 `MODBUS_SYNC_CHECK_20261012` 完成跨 Agent 写入、检索与重启进程持久化验证，最后通过 `memorix memory resolve --status archived` 归档清理，不污染正式库。

```text
=== [B.1] Antigravity (64266e40-4eef-4ed6-b781-74ee45805dbb) 写入测试记忆 ===
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "observation": {
    "id": 1,
    "entityName": "general",
    "type": "discovery",
    "title": "Acceptance Test B Shared Memory",
    "narrative": "Antigravity shared memory verification marker: MODBUS_SYNC_CHECK_20261012",
    "tokens": 22,
    "createdAt": "2026-10-12T03:28:44.024Z",
    "updatedAt": "2026-10-12T03:29:28.395Z",
    "projectId": "muzilinxi-l/modbusstudio",
    "topicKey": "discovery/acceptance-test-b-shared-memory",
    "revisionCount": 2,
    "status": "active",
    "source": "manual",
    "visibility": "project",
    "writeGeneration": 1
  },
  "upserted": true
}

=== [B.2] Claude (4978511a-d2cb-4b1d-8cbf-7b2886bc7029) 切换身份并检索该测试记忆 ===
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "entries": [
    {
      "id": 1,
      "time": "11:28 AM",
      "type": "discovery",
      "icon": "[DISCOVERY]",
      "title": "Acceptance Test B Shared Memory",
      "tokens": 22,
      "score": 0.11858114693744137,
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

=== [B.3] 模拟重启 CLI 后新进程 (pwsh -NoProfile) 再次检索 ===
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "entries": [
    {
      "id": 1,
      "time": "11:28 AM",
      "type": "discovery",
      "icon": "[DISCOVERY]",
      "title": "Acceptance Test B Shared Memory",
      "tokens": 22,
      "score": 0.11858114693744137,
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

---

### 验收 C 真实输出：跨项目隔离验证

在未销毁 `MODBUS_SYNC_CHECK_20261012` 之前，于另一独立 Git 仓库（`example/other-isolated-project`）中检索该关键词：

```text
=== [C.1] 另一测试项目中检索本项目专属记忆 MODBUS_SYNC_CHECK_20261012 ===
{
  "project": {
    "id": "example/other-isolated-project",
    "name": "other-isolated-project",
    "gitRemote": "https://github.com/example/other-isolated-project.git",
    "rootPath": "F:\\GitHub\\python\\docs\\Antigravity\\backups\\isolated_test_project"
  },
  "entries": []
}

=== [Cleanup] 归档清理测试记忆 (ID: 1)，防止污染正式记忆检索 ===
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "result": {
    "resolved": [
      1
    ],
    "notFound": []
  },
  "status": "archived"
}
```

---

### 验收 D 真实输出：防失真（禁止自动升格 `verified` + 矛盾 `proposed` 识别为 `disputed`）

在隔离测试副本目录 `test_memory_d` 中构造：
1. `MEM-9001` 与 `MEM-9002`：两条针对同一 `topic: default_tcp_port` 但结论矛盾（`502` vs `15020`）的 `proposed` 记忆；
2. `MEM-9003`：位于 `proposals/` 下试图自动升格为 `status: verified`、`verified_by: auto` 的违规条目。

```text
========================================================================
Memorix 共享记忆防失真自动验证报告 (memory/verify.py)
========================================================================
生成时间    : 2026-10-12T11:31:27.670661+08:00
仓库根目录  : F:\GitHub\python
记忆目录    : F:\GitHub\python\docs\Antigravity\backups\test_memory_d
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
ExitCode: 1
```

---

### 验收 E 真实输出：Memorix 文件锁与 Git 冲突不静默覆盖验证

```text
--- [E.1] Memorix Advisory File Lock 冲突拦截测试 ---
# Claude (4978511a-d2cb-4b1d-8cbf-7b2886bc7029) 加锁 memory/ARCHITECTURE.md：
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "file": "memory/ARCHITECTURE.md",
  "lockedBy": "4978511a-d2cb-4b1d-8cbf-7b2886bc7029"
}
# Antigravity (64266e40-4eef-4ed6-b781-74ee45805dbb) 尝试加锁同一文件，返回仍由 Claude 持有：
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "file": "memory/ARCHITECTURE.md",
  "lockedBy": "4978511a-d2cb-4b1d-8cbf-7b2886bc7029"
}
# Claude 释放文件锁：
{
  "project": {
    "id": "muzilinxi-l/modbusstudio",
    "name": "modbusstudio",
    "gitRemote": "https://github.com/muzilinxi-l/modbusstudio.git",
    "rootPath": "F:\\GitHub\\python"
  },
  "file": "memory/ARCHITECTURE.md",
  "released": true
}

--- [E.2] Git Worktree 双 Agent 同时修改同一文件冲突不静默覆盖测试 ---
Auto-merging shared_config.py
CONFLICT (content): Merge conflict in shared_config.py
Automatic merge failed; fix conflicts and then commit the result.
Git merge exit code: 1
--- Git status --porcelain ---
UU shared_config.py
--- 冲突文件内容 (保留双方冲突标记供人工审查) ---
<<<<<<< HEAD
TIMEOUT_MS = 500   # Claude update
=======
TIMEOUT_MS = 3000  # Antigravity update
>>>>>>> worktree-antigravity
```

---

### 验收 F 真实输出：`memory/verify.py` 坏数据拦截 vs 干净数据全绿

#### F.1 对含坏数据（重复编号、缺 `source`、无效 SHA、不存在文件）的测试记忆运行 `verify.py`
```text
========================================================================
Memorix 共享记忆防失真自动验证报告 (memory/verify.py)
========================================================================
生成时间    : 2026-10-12T11:30:57.693321+08:00
仓库根目录  : F:\GitHub\python
记忆目录    : F:\GitHub\python\docs\Antigravity\backups\test_memory_f_bad
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
Verify F (bad data) exit code: 1
```

#### F.2 对正式干净记忆库运行 `verify.py`（含自动执行 `.venv` 下的 `pytest`）
```text
========================================================================
Memorix 共享记忆防失真自动验证报告 (memory/verify.py)
========================================================================
生成时间    : 2026-10-12T11:25:01.749261+08:00
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
  - 10_pytest_execution: PASS (41 passed in 24.33s)
  - 11_needs_human_review: INFO
```

---

## 六、遗留说明与注意事项

1. **Antigravity 配置路径**：已完整定位并验证通过（`C:\Users\123\.gemini\config\plugins\memorix`、`C:\Users\123\.gemini\config\mcp_config.json` 及项目内 `.agents/`），`memorix doctor agents --agent antigravity` 状态为 `ok`，且 Hooks 与 Skills 已在当前会话动态生效。
2. **关于 `npm warn install-scripts` 的说明**：
   - 本机 npm 开启了 `allowScripts` 保护，阻止了 `better-sqlite3`、`onnxruntime-node` 等包的自动安装脚本。
   - 由于本机为 **Node.js v24.21.0**（已内置原生 SQLite 支持），Memorix 自动使用内置 SQLite + Orama BM25 全文检索引擎，`memorix doctor` 全绿无报错。
   - 若未来需要开启本地 ONNX 向量嵌入（`fastembed`），可按需执行：
     `npm install -g memorix --allow-scripts=better-sqlite3,onnxruntime-node,sharp,protobufjs`
3. **`memorix init --global` 在非交互管道中的注意事项**：
   - `memorix init` 使用了 `@clack/prompts` 交互式 TTY 组件，在无伪终端的子进程管道中会报 `uv_tty_init returned EBADF`；已按其源码 `buildInitTomlConfig` 标准模板直接生成了 `C:\Users\123\.memorix\config.toml` 并经 `memorix status` 校验通过。

---

## 七、一键恢复原配置（回滚）方法

如需完全回滚本次部署并恢复至安装前状态，可在 PowerShell 中依次执行：

```powershell
# 1. 卸载 Memorix 全局与项目级 Hooks / 集成
cd F:\GitHub\python
memorix uninstall --hooks

# 2. 还原 Claude Code 与 Antigravity 原始配置文件
$bk = "F:\GitHub\python\docs\Antigravity\backups\pre-memorix"
Copy-Item "$bk\claude.json" "C:\Users\123\.claude.json" -Force
Copy-Item "$bk\claude_settings.json" "C:\Users\123\.claude\settings.json" -Force
Copy-Item "$bk\gemini_config.json" "C:\Users\123\.gemini\config\config.json" -Force
Copy-Item "$bk\gemini_mcp_config.json" "C:\Users\123\.gemini\config\mcp_config.json" -Force
Copy-Item "$bk\gemini_AGENTS.md" "C:\Users\123\.gemini\config\AGENTS.md" -Force

# 3. 移除插件目录与新增项目文件
Remove-Item "C:\Users\123\.gemini\config\plugins\memorix" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "C:\Users\123\.claude\plugins\marketplaces\memorix-local" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "F:\GitHub\python\.agents" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "F:\GitHub\python\.claude" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "F:\GitHub\python\memory" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "F:\GitHub\python\AGENTS.md", "F:\GitHub\python\CLAUDE.md", "F:\GitHub\python\GEMINI.md" -Force -ErrorAction SilentlyContinue

# 4. 如需彻底卸载全局 npm 包
npm uninstall -g memorix
```

---

## 八、日常维护常用命令速查

```powershell
# 1. 健康诊断与 Agent 接入检查
memorix doctor
memorix doctor agents
memorix status

# 2. 检索与写入项目记忆
memorix search "<关键词>"
memorix remember --title "<标题>" "<记忆正文>"
memorix recent --limit 10

# 3. 团队协同与文件锁（写共享 memory/ 前必加锁）
memorix team status --all
memorix lock lock --file "memory/ARCHITECTURE.md" --as <agent-id>
memorix lock unlock --file "memory/ARCHITECTURE.md" --as <agent-id>
memorix lock status

# 4. 备份导出与迁移导入记忆快照
memorix transfer export --format json
memorix transfer import --file <snapshot.json>

# 5. 运行防失真自动验证器（含 Git SHA 校验 + 自动运行 pytest）
F:\GitHub\python\.venv\Scripts\python.exe F:\GitHub\python\memory\verify.py
```
