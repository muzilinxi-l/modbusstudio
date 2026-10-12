# Memory Index — F:/GitHub/python (Modbus Studio 2.0)

> 本目录是 `F:/GitHub/python` 项目的结构化共享记忆索引区，配合 Memorix SQLite（project-scoped）供 **Claude Code** 与 **Antigravity** 共同读取与分区维护。

## 1. 核心记忆文件导航

| 文件路径 | 职责说明 |
|---|---|
| [`memory/PROJECT.md`](file:///F:/GitHub/python/memory/PROJECT.md) | 项目定位、版本、核心技术栈、入口与运行命令 |
| [`memory/ARCHITECTURE.md`](file:///F:/GitHub/python/memory/ARCHITECTURE.md) | 分层架构（UI / Services / Engine / Codec / HEMS）、Server Hub 多从机并发模型 |
| [`memory/INTERFACES.md`](file:///F:/GitHub/python/memory/INTERFACES.md) | 核心服务接口、编解码接口、HEMS 数据库加载与配对接口 |
| [`memory/CONSTRAINTS.md`](file:///F:/GitHub/python/memory/CONSTRAINTS.md) | 架构红线、多 Agent 严格分区规则、安全与提交约束 |
| [`memory/TASKS.md`](file:///F:/GitHub/python/memory/TASKS.md) | 当前活跃任务、已完成里程碑与 Agent 分工状态 |
| [`memory/verify.py`](file:///F:/GitHub/python/memory/verify.py) | 防失真自动验证脚本（含 Git SHA / 文件核验 / 状态检查 / 自动运行 pytest） |

## 2. 分区工作目录

| 子目录 | 用途与状态流转规则 |
|---|---|
| `memory/proposals/` | 存放新建的候选记忆（强制 `status: proposed`，未经核验严禁升为 `verified`） |
| `memory/decisions/` | 存放经代码验证与人工/交叉审核通过的架构与技术决策（`status: verified`） |
| `memory/conflicts/` | 存放相互矛盾或与代码失真的争议记忆（`status: disputed`，保留双方证据） |
| `memory/handoffs/` | 存放 Claude Code 与 Antigravity 的任务分工、锁记录与跨会话交接单 |
| `memory/agents/` | 存放各 Agent（`claude`、`antigravity`）的工作区（worktree）职责声明与状态 |

## 3. Agent 必守协议摘要
1. **先读后做**：每次任务启动先读本 `INDEX.md` 及对应核心文件，再调用 Memorix 检索。
2. **代码为准**：所有记忆引用必须包含真实存在的 `source` 与有效 `commit`（Git SHA），关键结论必须核对当前源码。
3. **写前加锁**：修改共享 `memory/` 前先通过 `memorix orchestrate` 获取文件锁。
4. **门禁校验**：提交或合并前必须运行 `.venv\Scripts\python.exe memory/verify.py` 确保静态校验与 `pytest` 全部通过。
