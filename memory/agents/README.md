# Agent Profile — Claude Code & Antigravity

## 1. Claude Code (`agent: claude`)
- **角色定位**：需求定义、架构约束制定、代码审查与独立验收复核（产出 `docs/Claude code/*_PROMPT.md`）。
- **指令入口**：`F:/GitHub/python/CLAUDE.md`、`F:/GitHub/python/AGENTS.md`
- **MCP 接入**：`C:/Users/123/.claude.json` & `F:/GitHub/python/.claude/settings.json` (`memorix serve --mode lite`)
- **工作区约定**：Worktree A（或主分支只读审查区）

## 2. Antigravity (`agent: antigravity`)
- **角色定位**：代码实现、测试验证、故障修复与执行报告产出（产出 `docs/Antigravity/*_REPORT.md`）。
- **指令入口**：`F:/GitHub/python/AGENTS.md`、`F:/GitHub/python/GEMINI.md`
- **插件与 MCP 接入**：`C:/Users/123/.gemini/config/plugins/memorix`、`C:/Users/123/.gemini/config/mcp_config.json`、`F:/GitHub/python/.agents/plugins/memorix`
- **工作区约定**：Worktree B（或独立功能分支/工作树）
