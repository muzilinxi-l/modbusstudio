# Memorix 多 Agent 共享记忆系统 — 部署 Prompt（F:/GitHub/python 项目）

> 目标：让 **Claude Code** 与 **Antigravity** 在同一个本地 Git 项目（`F:/GitHub/python`）内，通过 Memorix 共享长期记忆、检索历史、协调任务、恢复上下文；同时防止错误记忆传播、并发修改冲突、已有功能损坏。
> 修改方：Antigravity。验收方：Claude Code。
> 日期：2026-10-12
> 部署范围：**仅限 `F:/GitHub/python` 项目**（不外延到 `F:/GitHub` 外层资料区）。

---

## 〇、用户已拍板的决策（本轮基线，Antigravity 照此执行）

1. **只接两个 agent**：Claude Code + Antigravity。**无 Codex**，删掉所有 codex 集成路径。
2. **安装 Memorix 本体**（`npm i -g memorix` + 对两个 agent 做 setup）。
3. **记忆介质 = Memorix SQLite**（project-scoped，落在 python 项目内）。
4. **隔离强度 = 严格分区**（worktree + file lock + 任务分配记录 + 合并门禁）。
5. **verify.py 深度 = 含自动跑测试**（静态检查 + 跑 pytest + 标 needs_human_review）。
6. 全程简体中文；配置中必要英文标识符、命令保留原样。
7. 个人开发工作流："明确需求 → 计划 → 实施 → 验证 → 人工决定发布"。

> 若执行中发现上述某项与本机现实冲突（如 antigravity 配置路径不存在），**停下报给 Claude，不要自行改方向**。

---

## 一、环境审计（Claude 已只读核查，Antigravity 接手先复核）

| 项 | 实测值 | 说明 |
|---|---|---|
| OS | Windows 11 Home 10.0.26300 | 满足 |
| PowerShell | 7.6.6 | 满足 |
| Node.js | v24.21.0 | Memorix 要求 `>=22.18.0`，数字满足；**24.x 是否经上游测试未知，装完必跑 `memorix doctor` 验证** |
| npm | 12.0.2 | — |
| Python | 3.14.7 | pytest 用（venv 在 `python/.venv`） |
| Git | 2.55.0.windows.4 | — |
| Claude Code 配置 | `C:/Users/123/.claude.json`（mcpServers 全空）+ `C:/Users/123/.claude/settings.json` | 可追加 MCP/hooks，**不得覆盖已有内容，先备份** |
| Antigravity 配置 | **尚未定位**（本机未见 antigravity 专属配置目录；Memorix 官方 `--agent antigravity` 默认写 `~/.gemini/config/plugins/memorix`） | **Antigravity 第一步：自报它本机真实配置路径**；找不到就停下报 Claude，不许瞎猜 |
| Memorix | 未安装（全局 npm 空） | 本轮要装 |
| 项目 git | `F:/GitHub/python` 独立仓库，5 个本地提交未推 | 记忆宿主 = 此仓库 |
| 根 md | `F:/GitHub/python` 内 `AGENTS.md` 已存在 | **只增量，不覆盖** |

## 二、Memorix 官方集成要点（Claude 已核 README；落地前 Antigravity 按装到版本再核对一遍）

- 安装/初始化：`npm install -g memorix`；`memorix init --global`；`memorix setup --agent <agent> --global`。
- Claude Code：`memorix setup --agent claude --global`（装插件 + 写 CLAUDE.md 指引 + 开 hooks）；手动 MCP 条目 `"command":"memorix","args":["serve"]`。
- Antigravity：`memorix setup --agent antigravity --global`（官方说落 `~/.gemini/config/plugins/memorix`，**本机无此目录，须先确认真实位置**）。
- 隔离：默认 project-scoped（按 git 项目，SQLite 存项目本地）。
- Hooks 默认开：`--noHooks` 关，或 `memorix uninstall --hooks` 移除（**本轮用户要 hooks，保留；但要在报告里写清装了哪些 hooks**）。
- 并发：`memorix orchestrate` / `memorix team`（任务规划、worker 交接、file locks）。
- 诊断/备份/迁移：`memorix doctor agents`；`memorix transfer export --format json`；`memorix transfer import --file`；`memorix dashboard`。

## 三、目标架构（严格分区）

```
Claude Code   (worktree A)  ┐
                            ├── 同一 git 项目 F:/GitHub/python
Antigravity   (worktree B) ─┘         │
                                      │ Memorix SQLite（project-scoped）
                                      │ + 共享 memory/ Markdown 索引区
                     项目级隔离 · 状态 proposed/verified/disputed/outdated
```

- 每个 agent 在自己 worktree 工作；**记忆读取共享，写入分区 + file lock**。
- 合并前人工审查 + pytest 通过；**禁止两 agent 无约束同时改同一文件**。

## 四、并发策略（严格分区，默认采用）

1. 工作区隔离：Claude worktree A、Antigravity worktree B，各改各的。
2. 写共享 `memory/` 前取 `memorix orchestrate` file lock，写完释放。
3. `memory/handoffs/` 记录"哪个 agent 负责哪段"。
4. 合并前跑 `pytest`，失败即停不合并。
5. Git 冲突保留标记 + 报告，**不得自动覆盖/静默解决**。

## 五、记忆可信度状态（Memorix 若原生不区分，用 Markdown 元数据补）

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
- 新记忆先进 `proposed`，**不得自动升级** `verified`。
- 矛盾记忆进 `disputed`，保留双方证据并报告。
- 过期进 `outdated`，**不删除**（留痕）。

## 六、统一记忆协议（`F:/GitHub/python/` 内；已有文件只增量，不覆盖）

须存在并维护（无则建，有则 diff 后只加必要项）：
- `AGENTS.md`（已存在 → 只加"记忆协议"一节）
- `CLAUDE.md`（Memorix 会写；确认是增量而非覆盖）
- `memory/INDEX.md`、`memory/PROJECT.md`、`memory/ARCHITECTURE.md`、`memory/INTERFACES.md`、`memory/CONSTRAINTS.md`、`memory/TASKS.md`
- `memory/decisions/`、`memory/proposals/`、`memory/conflicts/`、`memory/handoffs/`、`memory/agents/`

协议要点：任务开始先读 `memory/INDEX.md` + 相关核心记忆 → 用 Memorix 检索历史 → **关键结论对照当前代码验证** → 禁止把推测当事实 → 新记忆先进 `proposed` → 冲突保留证据并报告 → 任务结束写 `memory/handoffs/` → **不得未经审核改正式记忆**。两个 agent 必须**实际加载**对应指令（Claude 读 CLAUDE.md、Antigravity 读 AGENTS.md），不是只建文件。

## 七、防失真自动验证 `memory/verify.py`（Python 标准库 + Git，深度=含自动跑测试）

用 `python/.venv` 的 Python；不引复杂第三方依赖。检查项：
1. 关键记忆文件是否存在
2. 候选记忆编号唯一
3. 重要记忆含 `source` 字段
4. `commit` 是否为有效 git sha（`git cat-file -e`）
5. `source` 文件是否存在
6. 记忆引用的代码是否已变化（与该 commit 比对）
7. 是否有未审核的正式记忆被改（diff 非 verified 改动）
8. 重复编号 / 冲突状态检测
9. 其他 agent 的未合并修改
10. **自动跑一次 `pytest`（python/.venv），结果并入校验**
11. 无法自动验证的事实 → 标 `needs_human_review`

**脚本通过 ≠ 语义正确**。输出结果 JSON/文本到 `memory/verify-report.txt`。

## 八、备份与回滚（先备份，可恢复，全部记录进报告）

装 Memorix 前备份并记位置：
- `C:/Users/123/.claude.json`、`C:/Users/123/.claude/settings.json`
- antigravity 配置（定位到后备份）
- `F:/GitHub/python` git 状态（记录当前 commit，`git stash` 未提交改动）

恢复：git reset 到记录点 + 还原配置备份 + `memorix uninstall --hooks` + 需要时 `memorix transfer import`。

## 九、验收测试（Antigravity 执行并给真实输出，Claude 独立复核；用专门测试文件/项目，不污染正式记忆）

- A 基础：`memorix doctor agents` 正常；两 CLI 能起；原 MCP/Skills/Hooks 未被破坏（备份 diff 佐证）。
- B 共享：Antigravity 写一条测试记忆 → Claude 检索到；重启 CLI 后仍可检索。
- C 项目隔离：另一测试项目读不到本项目专属记忆。
- D 失真：`proposed` 不被自动升级 `verified`；两条矛盾 `proposed` 被识别为 `disputed`。
- E git 冲突：模拟两 agent 改同一文件 → 不静默覆盖，冲突需人工审查。
- F verify.py：对一份含坏数据（无效 sha、缺 source、重复编号）的测试记忆跑 `verify.py`，确认能揪出；对干净数据全绿。

**未执行/失败一律不标"通过"**，逐条贴真实输出。

## 十、交付物（`F:/GitHub/python/docs/Antigravity/`）

`05_MEMORIX_DEPLOY_REPORT.md` 含：
- 实际安装 Memorix 版本、Node 兼容性 doctor 结果
- 每个 CLI 的配置路径、已改文件、备份位置
- setup 时装了哪些 hooks（逐一列出）
- 执行过的每条命令 + 结果
- 验收 A–F 真实输出
- verify.py 的 F 项结果
- 未解决问题（尤其 antigravity 配置路径若没定位到）
- 恢复原配置方法
- 日常维护命令（诊断/备份/迁移/加记忆/跑 verify）

## 十一、执行控制（硬性）

- 分阶段：检查 → 说明 → 执行 → 验证 → 报告。
- **遇到以下必须先停下问 Claude/用户**：覆盖已有配置、删文件、升级影响现有环境的软件、改全局 MCP/Hooks、迁移记忆库、改 git 历史、开自动合并、发现重大兼容性问题（如 Node 24 与 Memorix 冲突）。
- 普通只读检查不必反复问。
- 敏感信息（API key/令牌/密码/Cookie）**一律不写进记忆、日志、报告**。
- 不推任何远程。

---

## 给用户的说明（Claude 视角）
- 我只出这份约束/prompt，**没有装 Memorix、没改任何配置**。
- Antigravity 读本文件执行，出 `docs/Antigravity/05_MEMORIX_DEPLOY_REPORT.md`，我独立复核。
- 关键待办：Antigravity 第一步须**自报 antigravity 配置路径**（本机尚未定位），定位不到就停下报我，不许瞎接 `--agent antigravity`。
