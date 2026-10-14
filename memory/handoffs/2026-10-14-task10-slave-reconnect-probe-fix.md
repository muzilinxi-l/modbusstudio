# Handoff: 任务 10（"自动重连"探活漏判修复）交接记录

```yaml
id: MEM-0210
status: proposed
source: docs/Claude code/10_SLAVE_RECONNECT_PROBE_FIX_PROMPT.md
commit: 019048b
author: claude
verified_by: pending
updated: 2026-10-14T09:30:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

## 0. 执行方式说明（偏差记录 + 撤回）
- 按 AGENTS.md §8 分工，Claude 只出约束/需求（`*_PROMPT.md`），执行由 Antigravity 完成并出 `*_REPORT.md`。
- 本单为现场应急缺陷（任务 09 验收后用户当天反馈"自动重连无效"），用户口头授权 Claude 直接落地修复，Antigravity 尚未复核。
- **用户随后要求撤回该代码、恢复"上午改动前"的版本继续过渡使用，让 Antigravity 按规范分工重新实施本单。** Claude 已执行：
  - `modbusstudio/modbus_engine.py` 已还原到任务 10 修改前基线（不含 `_rebuild_lock` / `is_active()` 判据 / `_log_rebuild_outcome()` / `import socket`），已确认导入正常、无残留新标识符。
  - `dist/ModbusStudio.exe` 已基于还原后代码重新打包（2026-10-14 10:38），当前可运行软件为任务 10 改动前的行为。
  - `docs/Claude code/10_SLAVE_RECONNECT_PROBE_FIX_PROMPT.md` 已重写为正式交给 Antigravity 的分步实施指令，替代原先"已修复说明"口吻。
- 因此 `status` 按协议强制为 `proposed`，`verified_by: pending`；若 Antigravity 复现验证通过，应推进为 `verified`。

## 1. 工作区与分支定位
- **Worktree 路径**：`F:/GitHub/python`
- **Git 分支名**：`main`
- **当前基线 Commit**：`7c806f8`（撤回提交，`modbus_engine.py` 已回到任务 10 修改前状态）
- **对应需求/指令文档**：`docs/Claude code/10_SLAVE_RECONNECT_PROBE_FIX_PROMPT.md`（已重写，交 Antigravity 执行）

## 2. 待实施改动文件清单（仅 1 个 .py，供 Antigravity 参考）
1. `modbusstudio/modbus_engine.py`：
   - `__init__`：新增 `self._rebuild_lock`（细粒度互斥锁，仅保护本实例重建过程）
   - `is_healthy()`：追加第 4 项判据 `_server.is_active()`（底层监听端口真实存活）+ 0.3s 启动竞态窗口豁免（与 `_server is None` 分支同口径）
   - `restart_if_broken()`：`_rebuild_lock` 串行化 + 双重检查 + 重建后调用 `_log_rebuild_outcome()`
   - 新增 `_log_rebuild_outcome()`：TCP 端点做 127.0.0.1 回环 socket 自检，成功/失败均通过 `on_packet_log` 回传 UI 日志面板（SYS/ERROR）
   - 顶部新增 `import socket`（仅用于回环自检）
   - 未改：`restart_all_broken()`（Hub 层）、`auto_reconnect_if_needed()`（Service 层）、`_on_auto_reconnect()`（UI 层按钮回调），任务 09 已验收的 H1/H2/H3/M1/M3/L1/L2/T1 逻辑全部未动

## 3. 验证结果（Claude 撤回前实跑过，供 Antigravity 复核对照）
- **已完成验证（Claude 实跑）**：
  - 单端点模拟僵死：`ModbusTransportServer` + `ModbusSlaveEngine` 挂载 → `start()` → `is_healthy()==True`；用 pymodbus 内部 `srv._ModbusProtocol__close(reconnect=True)` 模拟 OS 层网络中断（不杀线程/loop）→ `sleep 1.2s` → 修复后 `is_healthy()==False`（旧代码恒 `True`，漏判）；`restart_if_broken()==True` → `is_healthy()==True`；`ModbusTcpClient("127.0.0.1",port).connect()` 成功，读回 4x:5==4321（context 保留，数据未丢）。
  - 全量回归：`pytest -q` → 59 passed, 0 failed（含 09 全修 8 个回归 + 多站并发 7 个 + `test_full_ui_import_e2e` + `test_review_fix_09`；已知 `test_ui_theme` 竞态本轮未触发）。
  - 门禁：`python memory/verify.py` → PASS（错误: 0, 警告: 7，全部为已知 code-drift 提示，非阻断）。
- **Antigravity 待复核（proposed → verified 的关键证据，代码需按 PROMPT 重新实施，不能直接沿用 019048b 版结论）**：
  1. 原样复现"单端点模拟僵死并修复"端到端流程（见 `docs/Claude code/10_SLAVE_RECONNECT_PROBE_FIX_PROMPT.md` "验证"一节）。
  2. 现场侧复现路径：起从机服务（如 127.0.0.1:502）→ 真实客户端持续轮询 → 制造网络中断（拔线/防火墙/网卡休眠）→ 点"自动重连"：修复前应显示"未检测到僵死 listener"；修复后应显示"已重建 1 个僵死 listener"，且日志面板追加 SYS/ERROR 行（见 PROMPT 文档"复现步骤"一节）。
- **已知 flaky（非本单范围，沿自 07b/09）**：`test_multi_station_concurrency.py::test_slave_listener_restart_after_stop_restarts`，历史偶发 Tcl/socket handle 竞态；本次全量回归 59 passed 中该项为 passed，未复现。

## 4. 遗留 / 下一步
- 按 09 遗留待办（用户上次明确"下次"处理）：
  - H2 端口探测无上限（可能探到 65535 以上），`services.py get_next_available_tcp_port` 仍缺边界保护。
  - M2 warning 文案"默认 INT16/CDAB"易被误读。
  以上不属于任务 10 范围，未在本次修复中处理，记录备查。
