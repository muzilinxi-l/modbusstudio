# 双 Agent 协同开发工作流（Claude Code + Antigravity）—— 通用版分享文档

> 本文是一套**可迁移到任何项目**（语言不限、框架不限）的 AI 辅助开发工作流：
> 由 **两个 AI Agent 各管一端** 协同完成"写代码 + 测试 + 沉淀记忆"，最终由 **人工决定发布**。
> 前半部分是介绍（方案是什么、为什么这么设计、要装哪些东西），
> 后半部分是**直接可发给两个 Agent 的 Prompt**，朋友拿到后可照着自己的项目落地。
>
> 注意：这套方案与任何特定语言/项目无关。文中出现的示例路径 `F:\GitHub\python`
> 只是"我们自己的项目"占位——朋友使用时把路径替换成她自己的项目根目录即可，方法不变。

---

## 第一部分：方案介绍

### 1. 一句话说明

把「AI 写代码」拆成两个角色，各管一端、通过文件交互：

- **Claude Code**（命令行 AI）：只出**需求/约束**——写任务指令、缺陷清单、验收标准（`*_PROMPT.md`），**不直接改业务代码**；
- **Antigravity**（IDE 类 AI）：负责**执行/修改**——按 prompt 改代码、跑测试，输出修改摘要与验收结果（`*_REPORT.md`）。

两者产物按归属落到 `docs/` 下两个子目录，成对编号（如 `04_…_PROMPT.md` ↔ `04_…_REPORT.md`），
可逐条对照验收；**不靠对话转述**，全部以文件为准。

### 2. 为什么要这么设计

| 痛点 | 方案对策 |
|---|---|
| 单个 AI 又当"出题人"又当"答题人"，容易自我验证走过场 | 两个 Agent 独立工作：**Claude 负责质询/复核证据，Antigravity 负责执行**，Claude 不轻信报告，亲自重跑测试与校验 |
| 多个 AI 同时改一个文件夹容易互相覆盖、冲突 | **严格分区**：各 Agent 独立 Git Worktree + `memorix orchestrate` 文件锁；合并前必须跑测试 + 人工审查，Git 冲突不得静默解决 |
| AI 记忆容易失真（把旧记忆/推测当事实，或私自把候选记忆"升级"为已验证） | **Memorix 共享记忆 + 状态机**（proposed → verified / disputed / outdated）+ 项目自带的 `verify.py` 自动防失真校验（含跑测试） |
| 新手不知道流程该怎么做 | 引入 **ai-native-sdlc 轻量本地版 Skill**（纯流程+文档，无脚本）：明确需求 → 计划 → 实施 → 验证 → 人工决定发布 |
| 新手不一定会用这些工具 | 本文最后一部分给出**逐字可复制的 Prompt**，把"给谁发、发什么"都写死 |

### 3. 工作流总览

```
你（人工）
  │  提出需求
  ▼
Claude Code：写 *_PROMPT.md（需求/约束/验收标准） → 落盘 docs/Claude code/
  │
  ▼
Antigravity：按 prompt 改代码 + 跑测试 → 写 *_REPORT.md → 落盘 docs/Antigravity/
  │
  ▼
Claude Code：独立复核（重跑测试、校验记忆、检查隔离）→ 复核通过/打回
  │
  ▼
你（人工）：决定是否 commit / push（发布权永远在人手里）
```

配套的记忆层（Memorix + `memory/` 协议文件）贯穿始终：每次任务开始前先读记忆、检索历史，
任务结束后写交接记录（`memory/handoffs/`），并跑 `verify.py` 防失真。

### 4. 用到的插件 / 需要安装的软件清单

下面按「必须 → 可选」列出。朋友在**自己的电脑上先装齐这些**，再发 Prompt。
其中「项目自带资产」两行是**跟项目走的模板**（clone 仓库即有），不是要单独安装的东西。

| # | 名称 | 类型 | 用途 | 安装方式 | 说明 |
|---|---|---|---|---|---|
| 1 | **Git** | 基础软件 | 版本管理、Worktree 隔离、合并门禁 | `winget install Git.Git` 或官网安装包 | 全程依赖，Windows 10/11 建议 2.40+ |
| 2 | **Node.js**（18+） | 基础软件 | 运行 Claude Code 与 Memorix 的公共运行时 | `winget install OpenJS.NodeJS.LTS` 或官网 | **先装它，再装第 5、7 项** |
| 3 | **Python 3.12+** | 基础软件 | 运行被测项目、venv、测试框架 | 官网安装包，勾选 "Add to PATH" | 若她的项目不是 Python，可换成项目对应运行时；本方案示例为 Python |
| 4 | **虚拟环境 + 项目依赖 + 测试框架** | 依赖/测试 | 隔离依赖、跑测试（合并门禁） | 视语言而定；Python：`py -m venv .venv` 后 `pip install pytest`（按项目补依赖） | `.venv` 不入 git（`.gitignore` 已忽略） |
| 5 | **Claude Code** | AI Agent（命令行） | 出 prompt / 复核 | `npm install -g @anthropic-ai/claude-code` | 需先装 Node.js |
| 6 | **Antigravity** | AI Agent（IDE 类） | 执行代码修改、出报告 | 官方安装包（能执行 shell 命令与写文件即可） | 谷歌出品的 IDE 类 AI 工具 |
| 7 | **Memorix** v1.9.x | AI 记忆插件（CLI） | 两个 Agent 共享长期记忆 | `npm install -g memorix`，装完 `memorix --version` 应输出 1.9.x | 数据落在 `~/.memorix/data`（SQLite） |
| 8 | **Memorix 集成（Claude 侧）** | 插件/钩子 | Claude Code 自动读写记忆 | `memorix init --agent claude`（写入 `.claude/` 配置 + hooks） | 生成 MCP 工具（如 `memorix_project_context`）与 `CLAUDE.md` 指引；`.claude/` 入 `.gitignore` |
| 9 | **Memorix 集成（Antigravity 侧）** | 插件/钩子 | Antigravity 读写记忆 | `memorix init --agent antigravity`（写入 `.agents/` 配置） | 同上；`.agents/` 入 `.gitignore` |
| 10 | **SQLite** | Memorix 后端 | Memorix 记忆持久化 | 随 Memorix 自带（无额外依赖时用系统 SQLite） | 无需单独装 GUI，Memorix 直接读写 `.db` |
| 11 | **ai-native-sdlc 轻量本地版 Skill** | Claude Code Skill | 结构化开发流程模板（纯文档，无脚本） | 将 `skills/ai-native-sdlc-local/` 放到 `.claude/skills/`（项目级）或 `~/.claude/skills/`（个人级） | 上游 `github.com/bashebr/ai-native-sdlc`（MIT），本地版去掉全部脚本/hooks，仅保留 4 个文档模板 + 触发守卫 |
| 12 | **memory/ 协议文件 + verify.py** | 项目自带资产 | 记忆正文 + 防失真自动校验 | 随项目仓库提供（`memory/INDEX.md`、`PROJECT.md`、…、`verify.py`） | 改动 `memory/` 前先跑 `python memory/verify.py`（按项目语言换成对应解释器） |

> 说明：
> - 「插件」这里指 **Memorix 的两个 Agent 集成（`--agent claude` / `--agent antigravity`）** 与 **Claude Code 的 ai-native-sdlc Skill**；
> - 「安装软件」指 **Git、Node.js、Python（或项目对应运行时）、Memorix（npm 包）、测试框架、Memorix 自带 SQLite 后端**；
> - Node.js 是 Claude Code 与 Memorix 的公共依赖，**先装 Node.js 再装上面两个 npm 包**。

### 5. 目录与文件约定（两个 Agent 都遵守，与语言/项目无关）

```
docs/
  Claude code/     ← Claude 只写 *_PROMPT.md（需求/约束/验收标准）
  Antigravity/     ← Antigravity 只写 *_REPORT.md（执行摘要 + 测试结果）
memory/            ← 共享记忆正文 + verify.py（防失真校验器）
  INDEX.md / PROJECT.md / ARCHITECTURE.md / INTERFACES.md / CONSTRAINTS.md / TASKS.md
  decisions/ proposals/ conflicts/ handoffs/ agents/
AGENTS.md          ← 两个 Agent 都读的协作总则 + 记忆协议
CLAUDE.md          ← Claude Code 读的 MCP 工具指引（memorix_*）
```

命名规则：同一任务的需求与报告**用相同主题词编号**（如 `04_…_PROMPT.md` ↔ `04_…_REPORT.md`），
成对、可对照验收；文件名英文+下划线，正文可中文。

---

## 第二部分：直接可复制的部署 Prompt

> 用法：下面三段分别发给 **Claude Code** 与 **Antigravity**（Antigravity 那段发两次：先发「部署」，
> 部署完成后再发「复核/验收」）。把文中 `<项目根目录>` 全部替换成朋友自己的项目根目录，其余照抄。

### A. 发给 Claude Code（部署 + 写 05 号 Memorix 部署指令）

```text
你是 Claude Code，负责"出约束/需求、写 prompt、复核证据"，不直接改业务代码。
现在帮我在 <项目根目录> 部署 Memorix 多 Agent 共享记忆系统，
只接两个 agent：Claude Code 与 Antigravity（无 Codex），范围仅限该项目。

请新增 docs/Claude code/05_MEMORIX_PROMPT.md，内容覆盖：
1. 安装 Memorix（npm install -g memorix）+ 启用 SQLite 后端；
2. memorix init --agent claude 与 memorix init --agent antigravity，
   各自生成 .claude/ 与 .agents/ 配置并入 .gitignore；
3. 建立 memory/ 协议文件（INDEX/PROJECT/ARCHITECTURE/INTERFACES/CONSTRAINTS/TASKS
   + decisions/proposals/conflicts/handoffs/agents 子目录）；
4. 写 memory/verify.py：Python 标准库 + Git + 自动跑项目虚拟环境的测试（pytest 或等价命令），
   11 项防失真检查（含 4 个坏数据样例必须 FAIL、正式库必须 PASS、项目隔离必须 entries:[]）；
5. 约定严格分区：两 Agent 独立 worktree + memorix orchestrate 文件锁 + 合并前跑测试 + Git 冲突不得静默解决；
6. 记忆状态机：proposed/verified/disputed/outdated，新记忆不得自动升 verified；
7. 把本 prompt 交给 Antigravity 执行，执行报告写 docs/Antigravity/05_MEMORIX_DEPLOY_REPORT.md，
   你负责独立复核（重跑 verify.py、项目隔离、残留清理），不轻信报告。

要求：正文中文，文件名 05_MEMORIX_PROMPT.md，写完后告知我路径。
```

### B. 发给 Antigravity（执行 Memorix 部署 + 出报告）

```text
你是 Antigravity，负责"执行/修改/跑测试/出报告"。
请阅读 docs/Claude code/05_MEMORIX_PROMPT.md，并在 <项目根目录> 下执行：

1. 全局安装 Memorix：npm install -g memorix（确认 memorix --version = 1.9.x）；
2. 初始化两个 agent 集成：memorix init --agent claude、memorix init --agent antigravity；
   把生成的 .claude/ 与 .agents/ 目录加入 .gitignore；
3. 创建 memory/ 协议文件与子目录（INDEX/PROJECT/ARCHITECTURE/INTERFACES/CONSTRAINTS/TASKS，
   decisions/proposals/conflicts/handoffs/agents）；
4. 写 memory/verify.py（Python 标准库 + Git + 自动跑项目虚拟环境的测试，
   11 项检查，含测试执行项与 needs_human_review 标记）；
5. 自测三组样例：正式 memory/ 必须 PASS（含测试全绿）；
   坏数据副本 D_memory 必须 FAIL；坏数据副本 F_bad 必须 FAIL；
   项目隔离 C_isolated_project 检索必须返回 entries:[]；
6. 收尾清理 SQLite 活跃测试残留（memorix doctor 应显示 Observations X total, 0 active）；
7. 把全部执行结果、真实命令输出、实测耗时写入 docs/Antigravity/05_MEMORIX_DEPLOY_REPORT.md。

硬性约束：不修改业务代码、不推远程、
任何 API Key/Token/密码/Cookie 一律不写入记忆/日志/报告。
完成后告知我报告路径，不要自己"通过"，等 Claude 复核。
```

### C. 发给 Claude Code（独立复核，不轻信报告）

```text
你是 Claude Code，现在独立复核 Antigravity 的 05 Memorix 部署。
先读 docs/Antigravity/05_MEMORIX_DEPLOY_REPORT.md，但不要直接采信其中结论，按以下步骤亲手验证：

1. 重跑 memory/verify.py：
   - 正式库必须 PASS（含测试）；
   - 坏数据副本 D_memory 必须 FAIL；
   - 坏数据副本 F_bad 必须 FAIL。
2. 重跑项目隔离：在 C_isolated_project 下 memorix memory search 应返回 entries:[]。
3. 确认残留清理：memorix doctor 的 Observations 应为 0 active。
4. 确认 .claude/ 与 .agents/ 已入 .gitignore，且 git status 无意外新增业务文件。
5. 跑一次完整测试（项目虚拟环境的测试命令）确认无回归。

把复核结论写成 05_MEMORIX_DEPLOY_RECHECK_REPORT.md（若由 Claude 出，则写到
docs/Claude code/ 下同名 05b_MEMORIX_RECHECK_PROMPT.md + 复核结论），
明确标注"复核通过 / 复核打回"及理由。最后把结论报告给我（人工决定是否发布）。
```

---

## 第三部分：附赠 —— ai-native-sdlc 轻量本地版 Skill 安装说明

朋友若想同时套用"结构化开发流程"，把下面这段发给 Claude Code（一次性安装，与项目语言无关）：

```text
请帮我在本机安装 ai-native-sdlc 的轻量本地版（纯流程+文档，无脚本/无 hooks/无自动发布），
上游仓库：https://github.com/bashebr/ai-native-sdlc（MIT）。

要求：
1. 先做只读环境检查（Node.js 版本、npm 全局可写、.claude/skills 目录是否存在）；
2. 克隆上游，删除 scripts/、hooks、gates、evals、band、pre-commit、secrets-ignore、
   PULL_REQUEST_TEMPLATE、managed-settings、agent-evals.yml、incident.md、workflow-graph.yaml、
   bands.yaml、production-gate.sh 等；
3. 保留核心 4 个文档模板 assets/intent.md、spec.md、plan.md、REVIEW.md；
4. SKILL.md 增加触发守卫（仅当用户明确要求完整开发流程时启用）、
   轻量分级（小任务短计划+验收标准，复杂任务才用 intent/spec/plan）、
   人工发布决定（移除自动发布/定时任务/多 agent 组织审批）；
5. 安装位置：项目级 .claude/skills/ai-native-sdlc-local/ 或 ~/.claude/skills/（个人级）；
6. 记录上游 commit、许可、本地改动到 UPSTREAM.md；
7. 用临时环境跑一次最小自测（不回归），不要推远程。
```

装完即可在 Claude Code 里用 `/ai-native-sdlc-local` 触发该流程。

---

## 使用顺序小结（给朋友的一句话指南）

1. 装 **Git、Node.js**（再按需装项目对应的运行时，如 Python），然后 `npm i -g @anthropic-ai/claude-code memorix`；
2. 进**她自己的项目**目录，建虚拟环境并装好测试框架与项目依赖；
3. 把 **本文第二部分的 A/B/C 三段** 依次发给 Claude Code 和 Antigravity（A → B → C），
   把 `<项目根目录>` 替换成她项目的实际路径；
4. 把 **第三部分** 发给 Claude Code 装 ai-native-sdlc 轻量版；
5. 之后每个任务：你提需求 → Claude 写 prompt → Antigravity 执行出报告 → Claude 复核 → **你决定 commit / push**。

> 全程遵守三条铁律：现场数据（.db/日志/固件等）不改；密钥/凭证不写进记忆/日志/报告；
> 一次 commit 只做一件事、用 Conventional 风格注释；**发布权永远在人工手里**。
