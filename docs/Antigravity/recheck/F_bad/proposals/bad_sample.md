# Bad Memory Data Sample for Acceptance Test F.1 Recheck

## Bad 1: 重复编号 MEM-0001 + 无效 Git SHA
```yaml
id: MEM-0001
status: proposed
source: pyproject.toml
commit: deadbeef99999999999999999999999999999999
author: antigravity
verified_by: pending
updated: 2026-10-12T13:37:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

## Bad 2: 缺少 source 字段
```yaml
id: MEM-8002
status: proposed
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: antigravity
verified_by: pending
updated: 2026-10-12T13:37:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

## Bad 3: source 文件不存在
```yaml
id: MEM-8003
status: proposed
source: modbusstudio/ghost_non_existent_module.py
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: antigravity
verified_by: pending
updated: 2026-10-12T13:37:00+08:00
scope: F:/GitHub/python @ 2.0.0
```
