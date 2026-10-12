# Constraints Memory — Modbus Studio 2.0

## MEM-0005: 开发、并发与记忆防失真硬性约束

```yaml
id: MEM-0005
status: verified
source: AGENTS.md
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: antigravity
verified_by: human
updated: 2026-10-12T11:20:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

1. **工程边界约束**：
   - 仅限在 `F:/GitHub/python` 仓库内工作，严禁外延修改外层 `F:/GitHub` 资料区。
   - 不删除 `archive/*_legacy.py` 等历史回退依据文件。
   - 绝不将 API Key、Token、密码、Cookie 等敏感信息写入记忆、日志、报告或提交至 Git。
2. **多 Agent 协作与并发约束**：
   - 仅接入 **Claude Code** 与 **Antigravity** 两个 Agent（无 Codex）。
   - 采用严格分区策略：Worktree 隔离 + `memorix orchestrate` 文件锁 + `memory/handoffs/` 交接记录 + 合并前 `pytest` 门禁。
   - 遇到 Git 冲突必须保留冲突标记并报告，严禁静默覆盖或自动合并。
3. **记忆状态约束**：
   - 新记忆必须先入 `proposed`，严禁自动升级为 `verified`。
   - 矛盾记忆标记 `disputed` 并入 `memory/conflicts/`；过期记忆标记 `outdated` 且不删除。
   - 严禁未经审核直接修改 `verified` 状态的正式记忆。
