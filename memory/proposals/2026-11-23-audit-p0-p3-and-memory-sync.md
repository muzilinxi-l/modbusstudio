# Proposal: 全面代码审查修复（P0~P3）与共享记忆漂移同步提案

```yaml
id: MEM-0301
status: proposed
source: AGENTS.md
commit: 3080e95
author: antigravity
verified_by: pending
updated: 2026-11-23T14:25:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

## 1. 提案背景
经对 `modbusstudio/` 全量代码、测试基线与 `memory/verify.py` 核查，发现以下两类待人工/交叉审核后同步至正式记忆（`verified`）的漂移项；严格遵守记忆协议，本提案仅落盘 `memory/proposals/`，不直接篡改既有 `verified` 正式记忆。

## 2. 待审核同步项

### 2.1 协同 Agent 切换漂移（待更新 `memory/INDEX.md`、`memory/CONSTRAINTS.md`）
- Commit `982c1ae` 已将 `AGENTS.md` 与 `GEMINI.md` 中的协同 Agent 由 **Claude Code + Antigravity** 切换为 **Codex + Antigravity**。
- 当前 `memory/INDEX.md`（L3/L23/L24）与 `memory/CONSTRAINTS.md`（`MEM-0005` L21）仍写“仅接入 Claude Code 与 Antigravity（无 Codex）”，建议审核后统一更新并刷新对应 `commit` SHA。

### 2.2 已完成里程碑补录（待更新 `memory/TASKS.md` 与核心记忆 `commit` 锚点）
- **任务 12**：点位换算与本地对账 CLI（`735e4e3` / `d91a4f2` / `60d27ae`）
- **任务 13**：`ModbusServerHub` 混存 RTU 键通配解包防护（`216be4e`）
- **任务 14**：测试数据与会话状态解耦（`d8bb0a4`）
- **全面审查修复 P0（`bf2e5b2`）**：主机轮询站号/区域路由、HEX16 双重解析、传输服务端启停竞态、同端口多从机会话恢复、FLOAT64/INT64/UINT64 类型链路。
- **全面审查修复 P1（`e0a2c82`）**：主站外写反向同步与数据块并发锁、`LoggingService` CSV 线程锁与按日滚动、运行中删点同步、HEMS 0 地址/写回保序、编解码 NaN/STRING 边界、会话原子保存与 `.bak` 自动回退。
- **全面审查修复 P2（`3080e95`）**：UI 自动重连与连续轮询异步化、空转控件接通、批量改类型区域迁移、窗口居中/只读日志/CSV 方向列、网卡探活地址与对账多从机防覆盖。
- **全面审查治理 P3（待提交）**：运行期路径基准统一（`get_app_base_dir`/`resolve_runtime_path`）、`pymodbus` 模块级告警过滤收敛、清理 1,183 行机械样板注释与无用导入。

## 3. 审核后动作建议
1. 人工或 Codex 交叉复核 `MEM-0002`/`MEM-0004`/`MEM-0005`/`MEM-0006` 与上述变更的语义一致性；
2. 审核通过后更新对应正式记忆文件的 `commit` 锚点与文案，并将本提案归档为 `verified` 或 `outdated`。
