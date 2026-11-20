# AGENTS.md — 项目协作规则与记忆协议（F:/GitHub/python）

> 适用于本仓库（`F:\GitHub\python`）。
> 请 AI 编程工具与开发者共同遵守。

使用 Codex 时，通用协作协议以 `C:/Users/123/.codex/AGENTS.md` 为准；未由当前会话提供时，先读取该文件。以下保留项目专属约束。

## 1. 项目术语
- 术语保持统一：EMS（能源管理系统）、BMS（电池）、PCS（储能变流器）、一体机（现场 Linux ARM 设备）。

## 2. 仓库范围认知
- 本仓库为独立的 `F:/GitHub/python`（Modbus Studio 2.0 Python 工程），可构建、可测试。
- 严禁外延修改外层 `F:/GitHub` 资料区（如 `PE一体机/`、`软件/` 等现场交付快照）。

## 3. 历史交付保护
- 不删除注释掉的旧代码、`*.legacy.py`、备份目录等历史文件（它们是现场交付/回退依据），除非明确要求。

## 4. 生成物
- 构建产物（`build/`、`dist/`、`__pycache__`、`.venv`）不应被手动编辑或提交。

## 5. 安全与数据
- 现场 `.db` / `.cdb` / 日志 / 固件等属于客户交付数据，不要改动其内容或结构。

## 7. 运行中软件
- 不结束用户正在运行的 ModbusStudio（`ModbusStudio.exe`、`python main.py`）；代码更新需要生效时，由用户自行重启。

## 8. 文档输出约定（`docs/`）
- 用户要求保存独立报告时，Codex 报告放入 `docs/Codex/`，文件名用英文和下划线，正文用中文；其他工具的报告按 `docs/<工具名>/` 归档。

## 9. 记忆协议（Memorix 多 Agent 共享记忆与防失真规范）

### 9.2 严格分区并发策略
- 写入已获授权的共享 `memory/` 内容前，必须先通过 `memorix orchestrate` 获取 file lock，写完释放。
- Git 冲突必须保留冲突标记并报告，**不得自动覆盖或静默解决**。

### 9.3 记忆可信度状态与元数据规范
每条重要记忆必须包含如下标准元数据：
```yaml
id: MEM-XXXX
status: proposed | verified | disputed | outdated
source: <项目内相对文件路径>
commit: <有效 git sha>
author: <codex | antigravity | human>
verified_by: <codex | antigravity | human | pending>
updated: <ISO-8601 时间>
scope: F:/GitHub/python @ 2.0.0
```
- **状态规则**：
  - 新记忆先进 `memory/proposals/`，状态设为 `proposed`，**不得自动升级为 `verified`**。
  - 矛盾记忆进 `memory/conflicts/`，状态设为 `disputed`，保留双方证据并报告。
  - 过期记忆状态设为 `outdated`，**不删除**（留痕）。
  - **不得未经审核修改正式记忆（`verified`）**。
