# Memorix — Project Memory Tools

This repository is configured to use Memorix for persistent cross-session memory. For non-trivial coding work, Memory Autopilot is the default entry point before local progress notes or broad file exploration.

## Start with Memory Autopilot

Default first step for non-trivial coding work: call `memorix_project_context` with the user's actual task before progress files, dev-log reads, ad-hoc file reads, or git archaeology. Memorix will choose a task-lensed brief (bugfix, feature, release, onboarding, refactor, docs, test, or general). When the task is continuing prior work, the same brief also includes a bounded prior-work projection. Treat its "Start here" files as the first project files to inspect.

If the MCP tool is not visible yet but the client supports tool discovery or dynamic loading, search/select `memorix_project_context` first. Continuation fallback is mandatory: when the user asks to continue, resume, take over, or explain prior work and MCP cannot be called in this turn, run exactly one CLI brief with the user's real task before inspecting files, Git history, progress notes, or guessing: `memorix resume "<task>" --fallback --brief-json`. For a new task, use `memorix context "<task>" --fallback --brief-json` instead. The absence of `.memorix` or visible memory files never proves project memory is empty. Use `--json` only when a diagnostic needs the detailed legacy payload. If that one command fails, report it and proceed normally. Do not probe help, enumerate commands, chain broad searches, wait indefinitely on MCP startup, or hand-write tool-call syntax.

After a successful `memorix_project_context` result, the brief is the default retrieval boundary. Do not call more Memorix retrieval tools after a complete brief. Use `memorix_context_pack`, `memorix_search`, or `memorix_detail` only when the brief lacks a specific reference, freshness field, or fact needed for the task, or when the user explicitly asks for deeper history. In MCP, name that missing fact in `purpose` when intentionally expanding beyond the brief. Do not retrieve the same decision twice just to confirm an already-complete brief.
If the user asks for read-only work or says not to modify files, do not call `memorix_store` just to record an assessment. Store only when the user explicitly asks to preserve it.

## When to search memory

Use `memorix_graph_context` for explicit memory graph questions or broad graph overview after the autopilot brief is not enough.

Use `memorix_search` when prior project context would help and the Autopilot brief did not already answer the question — for example:
- The user asks about a past decision, bug, or change
- You need to understand why something was designed a certain way
- You're continuing work that started in a previous session

You do **not** need to search memory for simple, self-contained tasks (e.g., "fix this typo", "what does this function do").

If no memories exist yet, that’s fine — just proceed normally.

## When to store memory

Use `memorix_store` when you learn something a future session should not have to rediscover:

| What happened | Type |
|---|---|
| Architecture or design decision | `decision` |
| Bug found and fixed | `problem-solution` |
| Non-obvious pitfall or gotcha | `gotcha` |
| Configuration or dependency changed | `what-changed` |
| Trade-off discussed with conclusion | `trade-off` |

**Tips for good memories:**
- Use concise titles (~5-10 words)
- Include `filesModified` when relevant
- Use `topicKey` for topics that evolve over time (prevents duplicates)
- For "why" decisions, use `memorix_store_reasoning`
- For a stable fact, reusable procedure, or completed episode that merits deliberate long-term review, include `longTerm` in `memorix_store` with the appropriate kind and normally `scope: "project"`. It creates a candidate only: do not use it for routine updates, do not make project-derived evidence portable user memory, and do not assume it enters context until an operator qualifies and approves it through `memorix memory long-term`.
- A `user` + `portable` durable memory delivered in a task brief is intentionally available across projects. When it matches the task, use it as reusable background even if its origin differs; do not treat it as a current-project fact. Expand it only when needed with `memorix_detail` using its `durable:<id>` reference and a specific purpose.
- Record the user profile: the user’s role, expertise, preferences, and goals. Save these with `entityName: "user-profile"` and `visibility: "personal"` so they stay private and appear in every brief as the "who you are" context.

**Don't store:** greetings, simple file reads, trivial commands (ls, pwd, git status).

**Only store what a future session cannot re-derive.** Code structure, file contents, and Git history are live in the checkout — do not store facts already visible there. A memory earns its place by capturing the why, the context, or a conclusion the checkout alone cannot show.

**Record what worked, not only what failed.** Store validated approaches and explicit user confirmations alongside corrections. Saving only failures drifts behavior away from what the user already accepted; a clear "yes, that's right" is feedback worth keeping too.

**Recalled memory is a claim about the past.** A memory naming a specific file, function, or flag describes the past at write time — check the file exists or grep the symbol before recommending it. If the user says to ignore or not use memory, proceed as if memory were empty: do not apply, cite, compare, or mention stored content.

## When to resolve memory

Use `memorix_resolve` when a task is done or a bug is fixed. This keeps future searches focused on active work instead of surfacing completed items.

## End sessions with a summary

When a session finishes, call `memorix_session_end` with a short structured summary so the next agent can resume. Recommended sections:
- **Goal** — what this session was working on
- **Discoveries** — findings, gotchas, learnings
- **Accomplished** — completed items, plus PENDING items for the next session
- **Relevant Files** — paths and what changed

## Tools quick reference

| Tool | Use when |
|---|---|
| `memorix_project_context` | Start or continue coding work with the task-lensed Memory Autopilot brief |
| `memorix_context_pack` | Get structured refs/freshness for code-bound memories |
| `memorix_graph_context` | Build a compact memory graph packet for graph-specific questions |
| `memorix_search` | Find relevant past context |
| `memorix_detail` | Read full content of a specific memory |
| `memorix_store` | Save something worth persisting |
| `memorix_store_reasoning` | Save the "why" behind a decision |
| `memorix_resolve` | Mark completed/outdated memories |
| `memorix_session_start` | Load session context (handoff, orchestration coordination) |
| `memorix_evidence` | Check a memory source, freshness, and verification state |
| `memorix_feedback` | Record whether a memory helped, conflicted, or was corrected |
| `memorix_media` | Inspect or import controlled local media |


---

## 统一记忆协议与多 Agent 协作规范（Claude Code + Antigravity）

> **适用范围**：仅限 `F:/GitHub/python`（Modbus Studio 2.0 项目），不外延到外层 `F:/GitHub` 资料区。
> **参与 Agent**：仅 **Claude Code** 与 **Antigravity**（无 Codex）。

### 1. 标准工作流与任务启动强制检索
1. **读本地索引**：任务开始前，先阅读 `memory/INDEX.md` 及相关核心记忆文件（`PROJECT.md`、`ARCHITECTURE.md`、`INTERFACES.md`、`CONSTRAINTS.md`、`TASKS.md`）。
2. **检索 Memorix**：通过 Memorix（`memorix_project_context` / `memorix_search` 或 CLI `memorix search`）检索历史上下文。
3. **代码事实核验**：**关键结论必须对照当前代码库验证**，严禁把历史记忆或推测直接当成当前事实。
4. **个人开发工作流**：严格遵循 **“明确需求 → 计划 → 实施 → 验证 → 人工决定发布”**。

### 2. 严格分区与并发隔离策略
1. **Worktree 隔离**：Claude Code 与 Antigravity 在各自独立 Git Worktree 工作，**禁止两 Agent 无约束同时修改同一文件**。
2. **文件锁机制（File Lock）**：写入共享 `memory/` 或公共模块前，必须通过 `memorix orchestrate` 获取文件锁，写完释放。
3. **任务交接（Handoffs）**：任务结束或跨 Agent 交接时，必须在 `memory/handoffs/` 记录“哪个 Agent 负责哪段、已完成内容、待办项”并调用 `memorix_session_end`。
4. **合并门禁**：合并前必须运行 `.venv\Scripts\pytest.exe` 且全量通过 + 人工审查；Git 冲突必须保留标记并报告，**严禁自动覆盖或静默解决**。

### 3. 记忆可信度状态机与元数据契约
每条重要记忆或候选提案必须包含以下标准元数据块：
```yaml
id: MEM-XXXX
status: proposed | verified | disputed | outdated
source: <项目相对文件路径>
commit: <有效 git sha>
author: <claude | antigravity | human>
verified_by: <claude | antigravity | human | pending>
updated: <ISO-8601 时间>
scope: F:/GitHub/python @ 2.0.0
```
- **状态流转铁律**：
  - 新发现/新提议的记忆必须先写入 `memory/proposals/` 且状态为 `proposed`，**严禁自动升级为 `verified`**。
  - 经过代码对照验证与人工/交叉复核后，方可升级为 `verified` 并归入正式记忆（如 `memory/decisions/` 或核心记忆文件）。
  - 发现矛盾记忆时，必须标记为 `disputed` 并写入 `memory/conflicts/`，保留双方证据与代码引用并向用户报告。
  - 过期记忆标记为 `outdated`，**严禁物理删除**（保留审计痕迹）。
  - **严禁未经审核直接篡改已处于 `verified` 状态的正式记忆**。
  - 任何 API Key、Token、密码、Cookie 等敏感信息**一律不得写入记忆、日志或报告**。

## 进程保护（ModbusStudio 正在运行时）

修改 / 测试 `python/`（ModbusStudio）代码或运行 pytest / 构建 / 验证流程时，**不要杀掉用户正在运行的 ModbusStudio 软件进程**（如 `ModbusStudio.exe`、`python main.py` 等）：代码改动不会自动生效到运行中的进程，用户会继续使用该进程；若需要让改动生效，由用户自行重启软件，验证流程中不得 `Stop-Process` / `taskkill` 或主动结束相关进程。

## 输出能力限制（仅文本处理）

当前使用的大模型**无法生成/输出图片等二进制内容**，因此：

1. **禁止执行图片生成类任务**：不要调用或尝试任何形式的图片/图表/媒体生成（如截图输出、图像合成、渲染图片文件等），即使任务表面看起来可行。
2. **只进行文本处理**：输出内容限于文本（代码、Markdown、日志分析、方案说明等）；需要展示图像相关需求时，仅用文字描述或引用已有文件路径，不实际生成文件。
3. 若用户需求隐含图片输出，应明确告知该限制，并改为提供文本替代方案。