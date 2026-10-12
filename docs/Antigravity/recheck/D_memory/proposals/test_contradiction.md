# Contradictory Proposals & Unreviewed Upgrade Test (Recheck D)

## Proposal A
```yaml
id: MEM-9001
status: proposed
topic: default_tcp_port
claim: default_port_is_502
source: modbusstudio/models.py
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: antigravity
verified_by: pending
updated: 2026-10-12T13:37:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

## Proposal B (Contradicts Proposal A on same topic)
```yaml
id: MEM-9002
status: proposed
topic: default_tcp_port
claim: default_port_is_15020
source: modbusstudio/models.py
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: claude
verified_by: pending
updated: 2026-10-12T13:37:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

## Proposal C (Illegal auto-upgrade to verified inside proposals/)
```yaml
id: MEM-9003
status: verified
source: modbusstudio/models.py
commit: 9bb9932359db5cec3f10b2c9d67f1761ae132bf6
author: antigravity
verified_by: auto
updated: 2026-10-12T13:37:00+08:00
scope: F:/GitHub/python @ 2.0.0
```
