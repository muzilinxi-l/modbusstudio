# Project Memory — Modbus Studio 2.0

## MEM-0001: 项目基线与运行环境

```yaml
id: MEM-0001
status: verified
source: pyproject.toml
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: antigravity
verified_by: human
updated: 2026-10-12T11:20:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

- **项目名称**：`modbusstudio`（Modbus Studio 2.0）
- **项目定位**：现代多从机/主机仿真工作站与 HEMS 业务数据库联调平台（Modbus Slave 多从机模拟 + Modbus Poll 主机轮询二合一）。
- **Python 环境**：要求 `>=3.10`，本地虚拟环境位于 `F:/GitHub/python/.venv`（实测 Python 3.14.7）。
- **核心依赖**：`pymodbus>=3.8.0`、`pyserial>=3.5`、`pillow>=10.0.0`、`ttkbootstrap>=2.2.0`。
- **启动入口**：`python -m modbusstudio` 或 `modbusstudio/app.py:main`。
- **测试命令**：`F:/GitHub/python/.venv/Scripts/pytest.exe -v`（测试目录 `tests/`）。
