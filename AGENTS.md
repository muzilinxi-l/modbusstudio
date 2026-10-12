# AGENTS.md — 项目协作规则与记忆协议（F:/GitHub/python）

> 适用于本仓库（`F:\GitHub\python`）。
> 请 AI 编程工具（仅限 **Claude Code** 与 **Antigravity**，不含 Codex）与开发者共同遵守。

## 1. 语言与沟通
- 所有面向用户的回复、注释、提交信息、文档，一律使用简体中文（代码标识符、命令保留原样）。
- 术语保持统一：EMS（能源管理系统）、BMS（电池）、PCS（储能变流器）、一体机（现场 Linux ARM 设备）。
- 个人开发工作流：**“明确需求 → 计划 → 实施 → 验证 → 人工决定发布”**。

## 2. 仓库范围认知
- 本仓库为独立的 `F:/GitHub/python`（Modbus Studio 2.0 Python 工程），可构建、可测试。
- 严禁外延修改外层 `F:/GitHub` 资料区（如 `PE一体机/`、`软件/` 等现场交付快照）。

## 3. 通用代码规范
- 遵循现有文件的代码风格、命名与注释密度，不引入与项目无关的重构。
- 命名清晰、避免无意义缩写；注释只解释"为什么"，不解释"做了什么"。
- 修改最小化：只改完成当前任务所必需的内容，不顺手改无关代码。
- 不删除注释掉的旧代码、`*.legacy.py`、备份目录等历史文件（它们是现场交付/回退依据），除非明确要求。

## 4. 测试与验证
- Python 项目：改动涉及逻辑时，使用 `F:/GitHub/python/.venv` 运行 `pytest`，保证测试通过再交付。
- 新增功能尽量补测试；修复 bug 先补一个能复现该 bug 的测试。
- 构建产物（`build/`、`dist/`、`__pycache__`、`.venv`）不应被手动编辑或提交。

## 5. 安全与数据
- 现场 `.db` / `.cdb` / 日志 / 固件等属于客户交付数据，不要改动其内容或结构。
- 涉及凭证、密钥、API Key、令牌、密码、Cookie 的内容**一律不写进记忆、日志、报告，也不提交到版本库**。

## 6. 提交规范
- 提交信息使用中文，描述"做了什么、为什么"，遵循 Conventional 风格（如 `feat: …` / `fix: …` / `docs: …`）。
- 一次提交只做一件事；不要把不相关的改动混在一起。

## 7. 谨慎操作
- 删除文件、覆盖已有配置、全局安装、迁移记忆库、修改 git 历史前，必须先停下来与用户确认。
- 对不确定的改动，宁可先说明再动手。

## 8. AI 协作分工与文档输出约定（`docs/`）
两个 AI 工具**各管一端**，产物按归属落到 `docs/` 下对应子目录，便于对方直接读取文件交互：

- **Claude Code（`docs/Claude code/`）= 只出"约束 / 需求"**
  - 产出：`*_PROMPT.md`（任务指令、代码审查发现的缺陷清单、验收标准）。
  - **Claude 不产出执行报告、不改业务代码**——执行交给 Antigravity。
- **Antigravity（`docs/Antigravity/`）= 出"执行 / 修改"**
  - 产出：`*_REPORT.md`（按 Claude 的 prompt 改完代码后，输出修改摘要 + 测试验收结果）。

## 9. 记忆协议（Memorix 多 Agent 共享记忆与防失真规范）

### 9.1 任务启动与结束流程
1. **启动必读**：任务开始前，必须先读取 `memory/INDEX.md` 及相关核心记忆文件（`PROJECT.md`、`ARCHITECTURE.md`、`INTERFACES.md`、`CONSTRAINTS.md`、`TASKS.md`）。
2. **检索历史**：调用 Memorix（MCP `memorix_project_context` / `memorix_search` 或 CLI `memorix search`）检索相关历史记忆。
3. **代码核验**：**关键结论必须对照当前代码验证**，严禁把推测当事实，严禁未经核实直接采信旧记忆。
4. **交接记录**：任务结束时，必须在 `memory/handoffs/` 记录本次任务归属与交接详情，并调用 `memorix_session_end`。

### 9.2 严格分区并发策略
1. **Worktree 隔离**：Claude Code（worktree A）与 Antigravity（worktree B）在各自独立工作区修改，**禁止两 Agent 无约束同时修改同一文件**。
2. **文件锁（File Lock）**：写入共享 `memory/` 前，必须先通过 `memorix orchestrate` 获取 file lock，写完释放。
3. **合并门禁**：合并前必须运行 `.venv\Scripts\pytest.exe` 与 `python memory/verify.py`，测试失败或校验报错即停止合并；Git 冲突必须保留冲突标记并报告，**不得自动覆盖或静默解决**。

### 9.3 记忆可信度状态与元数据规范
每条重要记忆必须包含如下标准元数据：
```yaml
id: MEM-XXXX
status: proposed | verified | disputed | outdated
source: <项目内相对文件路径>
commit: <有效 git sha>
author: <claude | antigravity | human>
verified_by: <claude | antigravity | human | pending>
updated: <ISO-8601 时间>
scope: F:/GitHub/python @ 2.0.0
```
- **状态规则**：
  - 新记忆先进 `memory/proposals/`，状态设为 `proposed`，**不得自动升级为 `verified`**。
  - 矛盾记忆进 `memory/conflicts/`，状态设为 `disputed`，保留双方证据并报告。
  - 过期记忆状态设为 `outdated`，**不删除**（留痕）。
  - **不得未经审核修改正式记忆（`verified`）**。
