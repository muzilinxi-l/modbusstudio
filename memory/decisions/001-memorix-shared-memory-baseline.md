# Decision 001: Memorix 多 Agent 共享记忆系统架构基线

```yaml
id: MEM-0101
status: verified
source: docs/Claude code/05_MEMORIX_PROMPT.md
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: claude
verified_by: human
updated: 2026-10-12T11:22:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

## 决策内容
1. **接入范围**：仅接入 **Claude Code** 与 **Antigravity** 两个 Agent，剔除所有 Codex 集成路径。
2. **存储介质**：采用 Memorix SQLite（project-scoped）配合仓库内 `memory/` Markdown 索引区。
3. **并发隔离**：采用严格分区（Git worktree + `memorix orchestrate` file lock + `memory/handoffs/` 任务交接 + 合并前 `pytest` 门禁）。
4. **防失真校验**：通过 `memory/verify.py` 执行 11 项静态检查并自动调用 `F:/GitHub/python/.venv` 运行 `pytest`。
