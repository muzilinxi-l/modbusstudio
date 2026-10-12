# Architecture Memory — Modbus Studio 2.0

## MEM-0002: Clean Architecture 四层解耦架构

```yaml
id: MEM-0002
status: verified
source: modbusstudio/app.py
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: antigravity
verified_by: human
updated: 2026-10-12T11:20:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

- **UI 视图层（`modbusstudio/ui/`）**：包含 `main_window.py`、`slave_panel.py`、`poll_panel.py`。只负责收集用户输入与展示结果，严禁在 UI 层直接编写协议字节拼装或底层通信逻辑。
- **业务服务层（`modbusstudio/services.py`）**：包含 `PointService`、`SlaveService`、`PollService`、`LoggingService`，作为单点增改、数据库导入、批量生成的统一业务入口。
- **领域模型层（`modbusstudio/models.py`）**：纯 Python 数据实体（`Point`、`SlaveDevice`、`PollTask` 等），零 UI 与网络库强耦合。
- **通信与适配层**：
  - `modbusstudio/modbus_engine.py`：负责 Server Hub 多从机并发通信、主机轮询与报文钩子。
  - `modbusstudio/modbus_codec.py`：负责全数据类型与 4 种字节序（`ABCD`、`CDAB`、`BADC`、`DCBA`）编解码。
  - `modbusstudio/hems_db_loader.py` & `modbusstudio/hems_pairing.py`：负责 `extra/hems.cdb` 数据库解析与设备变量配对。

## MEM-0003: 多从机同端口并发架构（Server Hub 模式）

```yaml
id: MEM-0003
status: verified
source: MULTI_STATION_ARCHITECTURE.md
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: antigravity
verified_by: human
updated: 2026-10-12T11:20:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

- **传输层与逻辑从机解耦**：同一 `IP:Port` 或同一串口总线可同时挂载多个不同站地址（Unit ID）的从机实例。
- **动态路由**：底层通过 MBAP Header Unit ID 寻址，各从机点表与波形模拟完全隔离，独立启停互不干扰。
