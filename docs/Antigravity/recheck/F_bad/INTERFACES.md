# Interfaces Memory — Modbus Studio 2.0

## MEM-0004: 业务服务与编解码核心接口

```yaml
id: MEM-0004
status: verified
source: modbusstudio/services.py
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: antigravity
verified_by: human
updated: 2026-10-12T11:20:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

- **服务层入口（`modbusstudio/services.py`）**：
  - `PointService`：统一负责点位校验、单点增改、规则批量生成与数据库点表导入。
  - `SlaveService`：管理从机实例注册、启停生命周期与点表同步。
  - `PollService`：管理主机轮询任务调度与报文结果解析。
  - `LoggingService`：提供线程安全双缓冲节流日志投递与批量拉取接口。
- **编解码器接口（`modbusstudio/modbus_codec.py`）**：
  - 支持数据类型：`BOOL`、`INT16`、`UINT16`、`INT32`、`UINT32`、`FLOAT32`、`FLOAT64`、`STRING`、`HEX16`。
  - 支持字节序变位：`ABCD`、`CDAB`、`BADC`、`DCBA`。
- **HEMS 业务适配接口（`modbusstudio/hems_db_loader.py` & `modbusstudio/hems_pairing.py`）**：
  - 解析 `extra/hems.cdb`（SQLite/CDB 格式）中的设备模型与点表定义，并基于 `More` 字段推导储能 PCS、BMS、光伏逆变器配对关系。
