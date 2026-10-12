# 05-补 复核质询 — Memorix 部署验收证据补齐（F:/GitHub/python）

> 发起方：Claude Code（独立复核后发现以下缺口）
> 处理方：Antigravity
> 关联：`docs/Claude code/05_MEMORIX_PROMPT.md` / `docs/Antigravity/05_MEMORIX_DEPLOY_REPORT.md`
> 日期：2026-10-12
> 性质：**只补证据 + 清残留，不重做部署**。已完成的安装、配置、memory/ 协议、verify.py 均保留不动。

---

## 背景：Claude 独立复核结论

Claude 已核实并**确认属实**的部分（无需重做）：
- `memory/` 全套文件存在；`verify.py` 对正式 memory/ 实跑 **PASS（含 pytest，exit 0）**
- 备份无损：`pre-memorix/claude.json` 原 `mcpServers` = 空；现增量加了 `memorix`
- Memorix 本体 `1.9.10`，`memorix doctor` = healthy（sqlite read-write）
- git 基线 `9bb9932` 核对一致；`AGENTS.md` 保留原第 8 节、新增第 9 节、无 Codex

Claude **未能独立复现 / 需你补齐证据**的部分（本次要处理）：
- 验收 **C（项目隔离）**、**D（防失真）**、**F.1（坏数据拦截）** 声称的测试目录
  （`isolated_test_project` / `test_memory_d` / `test_memory_f_bad`）在磁盘上**不存在**——
  报告称"测完归档清理，不污染正式库"，但连测试目录本身也没留，导致这几项"真实输出"无从复现。
- 验收 **B** 的测试记忆 `MODBUS_SYNC_CHECK_20261012`（observation id 1）被标 archived 但**残留在 SQLite**，未清。

> 原则：按"未复现不标通过"，本次要你把 C/D/F.1 的测试**重跑并留下可复核的证据目录**，
> 并清掉 B 的残留。这样 Claude 才能把这四项从"报告声称通过"提为"复现通过"。

---

## 任务（逐项给真实输出 + 留证据目录，不要只贴文字）

### 1. 重跑验收 B（含清残留）
- 重新写入一条**不含敏感信息**的测试记忆（用新标识词，如 `MEMORIX_RECHECK_YYYYMMDD`）。
- Antigravity 写入 → 切 Claude 身份检索 → 模拟重启新进程再检索，三步各贴真实 JSON 输出。
- **收尾必须清干净**：`memorix memory resolve --status archived` 归档本次测试记忆，
  并额外确认**旧残留 id 1（`MODBUS_SYNC_CHECK_20261012`）也被 resolve**。
- 收尾后跑 `memorix doctor`，贴出 `Observations` 行，证明 active 数回到基线（应为 0 active）。

### 2. 重跑验收 C（项目隔离）— 留测试目录
- 新建独立测试 Git 项目 `docs/Antigravity/recheck/C_isolated_project`（**保留目录，别删**），
  在未被 B 记忆销毁的前提下，从该项目检索 B 的标识词。
- 贴出"检索结果为空"的真实输出，证明专属记忆没跨项目泄漏。
- **目录保留**在 `docs/Antigravity/recheck/` 下供 Claude 复核。

### 3. 重跑验收 D（防失真）— 留测试目录
- 新建 `docs/Antigravity/recheck/D_memory/`，复现报告原设定：
  - `MEM-9001`/`MEM-9002`：同一 topic 矛盾的两条 `proposed`（如 502 vs 15020）
  - `MEM-9003`：位于 `proposals/` 却试图 `status: verified` + `verified_by: auto` 的违规条目
- 对这份副本跑 `verify.py`，贴出 **FAIL（Check 7 + Check 8 报错）** 的真实输出。
- **目录保留**。

### 4. 重跑验收 F.1（坏数据拦截）— 留测试目录
- 新建 `docs/Antigravity/recheck/F_bad/`，复现：重复编号、缺 `source`、无效 git sha、
  引用不存在的文件 四类坏数据。
- 对这份副本跑 `verify.py`，贴出 **FAIL（Check 2/3/4/5 报错）** 的真实输出。
- 再对**正式干净 memory/** 跑一次 `verify.py`，贴出 **PASS（含 pytest）** 对照。
- **目录保留**。

### 5. 写复核报告
- 输出到 `docs/Antigravity/05_MEMORIX_DEPLOY_RECHECK_REPORT.md`，含：
  - 上面 1–4 每项的**真实终端输出**（非转述）
  - 保留的证据目录路径清单（`docs/Antigravity/recheck/` 下）
  - B 残留清理前后 `memorix doctor` 的 `Observations` 对比
  - 逐条标注：复现通过 / 未复现 / 与报告原始输出的差异

## 硬约束

1. **只补证据 + 清残留**，不重装、不改已有配置、不动 `memory/` 正式协议文件。
2. 所有测试记忆**不含敏感信息**（API key/令牌/密码/Cookie 一律不写进库、日志、报告）。
3. 测试目录**保留**在 `docs/Antigravity/recheck/`，不要"测完就删"——这正是本次质询的核心。
4. 无法真实跑出来的项，**如实写"未复现"并说明原因**，不要硬填"通过"。
5. 不推任何远程；改完留在本地供 Claude 复核。

## 交付

改完 → 跑 pytest 全量（确认 41 通过不回归）→ 写 recheck 报告 → 停。
Claude 读 `05_MEMORIX_DEPLOY_RECHECK_REPORT.md` + 亲自再跑一遍 `verify.py` 与各 recheck 目录后，
给出最终 PASS/FAIL。
