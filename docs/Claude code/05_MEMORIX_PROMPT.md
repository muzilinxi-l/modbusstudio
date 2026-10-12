# Memorix 多 Agent 共享记忆系统 — 部署 Prompt（F:/GitHub/python 项目）

> 目标：让 Claude Code 与 Antigravity 在同一个本地 Git 项目（`F:/GitHub/python`）内共享长期记忆、检索历史、协调任务、恢复上下文，同时防止错误记忆传播、并发修改冲突以及已有功能损坏。
> 修改方：Antigravity。验收方：Claude Code。
> 日期：2026-10-12
> 部署范围：**仅限 `F:/GitHub/python` 项目**（不外延到 `F:/GitHub` 外层资料区）。

---

## 一、用户已确认的约束

- 用户机器上只有 **Claude Code CLI** 与 **Antigravity** 两个 agent（**无 Codex**）。
- 部署只发生在 `F:/GitHub/python` 这一个项目内。
- 隔离强度用户未定，**默认采用"严格分区"方案**（下方"五、并发策略"），用户验收时再决定收紧/放松。
- 用户目标：个人开发、AI/编程新手工作流，"明确需求 → 计划 → 实施 → 验证 → 人工决定发布"。
- 全程简体中文；配置中的英文标识符、命令保留原样。

## 二、环境审计（Claude 已只读核查，Antigravity 接手时请复核）

| 项 | 实测值 | 说明 |
|---|---|---|
| OS | Windows 11 Home 10.0.26300 | 满足 |
| PowerShell | 7.6.6 | 满足 |
| Node.js | v24.21.0 | Memorix 要求 `>=22.18.0`，数字满足；**24.x 是否经上游测试未知，安装后须跑 doctor 验证** |
| npm | 12.0.2 | — |
| Python | 3.14.7 | — |
| Git | 2.55.0.windows.4 | — |
| Claude Code 配置 | `C:/Users/123/.claude.json`（mcpServers 全空）+ `C:/Users/123/.claude/settings.json` | 可安全追加 MCP，**不得覆盖现有内容** |
| Antigravity 配置 | 尚未定位到独立目录（未见 `.codex/` 之外的 antigravity 专属配置） | **Antigravity 接手第一步：先定位本机 antigravity 配置路径**，Memorix 官方 `--agent antigravity` 实际落到 `~/.gemini/config/plugins/memorix`，需确认是否适用 |
| Memorix | **未安装**（全局 npm 为空） | 待装 |
| 项目 git | `F:/GitHub/python` 是独立 git 仓库（当前 5 个本地提交未推） | 记忆宿主 = 此仓库 |
| 根目录 md | `F:/GitHub/python` 内现有 `AGENTS.md` 已存在 | **不得覆盖**，只可增量 |

> 注意：用户明确"只在 F:github 这个文件夹里面进行，现在只进行 python 这个项目"。因此 Memorix 初始化、记忆 SQLite、共享目录都**只落在 `F:/GitHub/python` 内**，不污染外层资料区。

## 三、Memorix 官方集成要点（Claude 已核实 README，Antigravity 落地前须按当前版本再核对）

- 安装：`npm install -g memorix`；初始化 `memorix init --global`；按 agent 接入 `memorix setup --agent <agent> --global`。
- Claude Code：`memorix setup --agent claude --global`（会装插件 + 写 `CLAUDE.md` 指引 + 开 hooks）；手动 MCP 条目为 `"command": "memorix", "args": ["serve"]`。
- Antigravity：`memorix setup --agent antigravity --global`（官方文档说落到 `~/.gemini/config/plugins/memorix`）。**本机无该目录，Antigravity 须先确认 antigravity 实际读取配置的位置，找不到就停下报给我。**
- 记忆隔离：默认 **project-scoped**（按 git 项目隔离，SQLite 存项目本地）。
- Hooks 默认开：用 `--noHooks` 关，或 `memorix uninstall --hooks` 移除。
- 并发：`memorix orchestrate` / `memorix team`（任务规划、worker 交接、file locks）。
- 诊断/备份：`memorix doctor agents`；`memorix transfer export --format json`（备份）；`memorix transfer import --file`（恢复）；`memorix dashboard`。

## 四、目标架构（严格分区方案，默认采用）

```
Claude Code (独立 worktree A)  ┐
                                ├── 同一 git 项目 F:/GitHub/python
Antigravity (独立 worktree B) ─┘        │
                                        │ Memorix SQLite + 共享 memory/ 目录
                                项目级记忆隔离（按 git 项目）
                                记忆状态: proposed / verified / disputed / outdated
```

- 每个 agent 在自己 worktree 里工作，**记忆读取共享，写入分区**。
- 谁在哪个区段写，用 file lock（`memorix orchestrate` 的锁）+ 任务分配记录约束。
- 合并前必须人工审查 + 测试通过；**禁止两个 agent 无约束同时改同一份文件**。

## 五、并发策略（隔离强度=严格分区，待用户验收确认）

1. **工作区隔离**：Claude 用 worktree A，Antigravity 用 worktree B；各自只改自己那份。
2. **文件锁**：写共享 `memory/` 文件前取 `memorix orchestrate` file lock，写完释放。
3. **任务分配记录**：统一 `memory/handoffs/` 记录"哪个 agent 负责哪段"。
4. **合并门禁**：合并前跑 pytest；失败即停，不合并。
5. **冲突不静默**：Git 冲突保留标记 + 报告，**不得自动覆盖**。

> 若用户验收后觉得"软协调"（B 方案）够用，可降级为：不强制 worktree，同一 worktree 靠提交顺序 + 审查。默认按 A（严格）。

## 六、记忆可信度状态（Memorix 原生不区分时，用 Markdown 元数据实现）

每条重要记忆记录：
```
status: proposed | verified | disputed | outdated
source: <文件路径>
commit: <git sha>
author: <agent/人>
verified_by: <agent/人>
updated: <ISO 时间>
scope: F:/GitHub/python @ <版本>
```
- 新记忆先进 `proposed`，**不得自动升级**为 `verified`。
- 矛盾记忆进 `disputed`，保留双方证据，报告给人。
- 过期进 `outdated`，不删除（留痕）。

## 七、统一记忆协议（`F:/GitHub/python/` 内，已有文件只增量、不覆盖）

必须存在并维护（无则建，有则 diff 后只加必要项）：
- `AGENTS.md`（已存在 → 只增量加"记忆协议"一节）
- `CLAUDE.md`（Claude Code 仓库记忆）
- `memory/INDEX.md`（记忆目录）
- `memory/PROJECT.md`、`memory/ARCHITECTURE.md`、`memory/INTERFACES.md`、`memory/CONSTRAINTS.md`、`memory/TASKS.md`
- `memory/decisions/`、`memory/proposals/`、`memory/conflicts/`、`memory/handoffs/`、`memory/agents/`

协议要点：
- 任务开始先读 `memory/INDEX.md` + 相关核心记忆。
- 用 `memorix` 检索历史，**关键结论必须对照当前代码验证**。
- 禁止把推测当事实；新记忆先进 `proposed`。
- 冲突保留证据并报告；任务结束写 `memory/handoffs/` 交接。
- **不得未经审核改正式记忆**。
- 两个 agent 必须**实际加载**对应指令（CLAUDE.md 对 Claude、AGENTS.md 对 Antigravity），不是只建文件。

## 八、防失真自动验证（Python 标准库 + Git，不引复杂依赖）

一个轻量校验脚本 `memory/verify.py`（Antigravity 写），至少检查：
- 关键记忆文件是否存在
- 候选记忆是否有唯一编号
- 重要记忆是否含 `source`
- `commit` 是否为有效 git sha
- `source` 文件是否存在
- 记忆引用的代码是否已变化（比对）
- 是否有未审核的正式记忆被改
- 重复编号 / 冲突状态
- 其他 agent 的未合并修改
- 需人工复核的记忆列表

**脚本通过 ≠ 语义正确**；无法自动验证的事实标 `needs_human_review`。

## 九、备份与回滚（先备份，可恢复）

- 装 Memorix 前：备份 `C:/Users/123/.claude.json`、`C:/Users/123/.claude/settings.json`、antigravity 配置（定位到后）、`F:/GitHub/python` 当前 git 状态（`git stash`/记录 commit）。
- 记录所有备份位置到报告。
- 恢复：`git reset` 到记录点 + 还原备份配置 + `memorix uninstall --hooks` / `memorix transfer import`。

## 十、验收测试（Antigravity 执行，Claude 复核；用专门测试项目/文件，不污染正式项目）

- A 基础：`memorix doctor agents` 正常；两 CLI 能起；原 MCP/Skills/Hooks 未破坏。
- B 共享：Antigravity 写一条测试记忆 → Claude 检索到，重启后仍可检索。
- C 项目隔离：另一测试项目读不到本项目专属记忆。
- D 失真：`proposed` 记录不被自动升级为 `verified`；两条矛盾 `proposed` 被识别为 `disputed`。
- E git 冲突：模拟两 agent 改同一文件 → 不静默覆盖，冲突需人工审查。

**未执行/失败的测试不得标"通过"**，逐条给真实输出。

## 十一、交付物（`F:/GitHub/python/docs/Antigravity/`）

`05_MEMORIX_DEPLOY_REPORT.md`，含：
- 实际安装版本、配置路径、已改文件、备份位置
- 执行过的每条命令 + 结果
- 验收测试 A–E 真实输出
- 未解决问题
- 恢复原配置的方法
- 日常维护命令（诊断/备份/迁移/加记忆）

## 十二、执行控制（硬性）

- 分阶段：检查 → 说明 → 执行 → 验证 → 报告。
- **遇到以下情况必须停下问我**：覆盖已有配置、删文件、装/升级影响现有环境的软件、改全局 MCP/Hooks、迁移记忆库、改 git 历史、开自动合并、发现重大兼容性问题。
- 普通只读检查不必反复问。
- 敏感信息（API key/令牌/密码/Cookie）**一律不写进记忆、日志、报告**。

## 十三、待用户确认项（Claude 记录，验收时再定）

1. **隔离强度**：默认严格分区（五、A），验收后你可降级为软协调（B）。
2. **Antigravity 配置路径**：本机未定位到，Antigravity 先报路径再动手。
3. 是否现在装 Memorix（`npm i -g memorix`）——用户此前说"现在只进行 python 项目"，我理解为**先做协议与验证脚本，Memorix 本体安装待用户点头**。

---

### 给用户的说明（Claude 视角）
- 我只出了这份约束/prompt，**没有下载 Memorix、没有改任何配置**。
- Antigravity 读 `docs/Claude code/05_MEMORIX_PROMPT.md` 执行，出 `docs/Antigravity/05_MEMORIX_DEPLOY_REPORT.md`，我来独立复核。
- 你先拍板"第十三、待确认项"里第 1、3 条，我再让 Antigravity 开工；第 2 条由 Antigravity 自报。
