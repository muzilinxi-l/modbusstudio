# 代码审查缺陷修复执行报告 (09_REVIEW_FIX_REPORT)

- **执行方**: Antigravity
- **验收方**: Claude Code
- **执行日期**: 2026-10-13
- **基线版本**: `46a7fdb`
- **遵循协议**: `AGENTS.md`、`GEMINI.md`、`09_REVIEW_FIX_PROMPT.md`
- **门禁状态**: pytest 全量通过（58 passed, 1 skipped / verify 59 passed）、`memory/verify.py` 全绿通过（0 errors）

---

## 一、改动清单与逐项映射 (file:line)

| 编号 | 严重度 | 涉及文件与行号 | 改了什么 / 为什么 |
|---|---|---|---|
| **H1** | 必修 | `modbusstudio/hems_pairing.py:48-60` | 将硬编码的 `ByteOrderMode.CDAB` 改为调用 `map_db_format_to_byte_order(chosen_format, default_mode=ByteOrderMode.CDAB)` 动态提取字节序；消除浮点/双精度字节序错位隐患。 |
| **H2** | 必修 | `modbusstudio/services.py:616-635`<br>`modbusstudio/ui/slave_panel.py:101-118`<br>`modbusstudio/ui/dialogs/db_import_dialog.py:273-308` | 1. `SlaveService` 新增 `get_occupied_tcp_ports()` 与 `get_next_available_tcp_port(start_port=502)`；<br>2. `_add_new_slave_instance` 与 `_import_as_new_instances` 改为首个未占用探测，避免手动指定端口后 `502+len` 冲突；<br>3. `db_import_dialog` 导入前建立 `used_tcp_ports` 集合防同批冲突，基于 `(ep_key, unit_id)` 严格去重，同 `ip:port` 不同 `unit_id` 独立建实例不静默合并。 |
| **H3** | 必修 | `modbusstudio/models.py:348-360`<br>`modbusstudio/models.py:417`<br>`modbusstudio/ui/poll_panel.py:312` | 抽离 `_parse_port(val: Any, default: int = 502) -> int`，对非数字、浮点、越界 `[1, 65535]` 输入 log warning 并兜底默认 502；`models.ConnectionConfig.from_dict` 与 `poll_panel._get_connection_config` 均改用 `_parse_port`，消除裸 `int()` 导致的 GUI 崩溃与加载中断。 |
| **M1** | 应修 | `modbusstudio/modbus_engine.py:1325-1327` | 将 `is_bit = (AreaType.COIL in area or ...)` 改为健壮判定：`s_area = str(area).strip().upper(); is_bit = ("0X" in s_area or "COIL" in s_area or "1X" in s_area or "DISCRETE" in s_area)`；彻底解决派生串入参（如 `"0x_Coil (线圈) @ 1"`）在 `write_raw_values` 中判错导致布尔区被按整数字写入的问题。 |
| **M3** | 应修 | `modbusstudio/services.py:219-223` | 将 HEX16 的双分支重复解析改为稳妥口径：`s_hex = str(val_input).strip().lower(); s_hex_clean = s_hex[2:] if s_hex.startswith("0x") else s_hex; val_converted = int(s_hex_clean, 16)`；支持 `0x1A2B` 与 `1A2B`，非法字符抛出 ValueError 正常返回不匹配提示。 |
| **T1** | 应修 | `tests/test_full_ui_import_e2e.py:169-173` | 将 `run_full_ui_import_self_test` 封装为 `test_full_ui_import_self_test()`，使 pytest 正常收集并纳入 CI 自动化覆盖，且全绿通过。 |
| **M2** | 可选 | `modbusstudio/hems_pairing.py:56-60` | `target_format` 与 `src_format` 均为空时，增加 `logger.warning` 明确提示「未指定数据格式，按默认 INT16/CDAB 处理」，不再完全静默。 |
| **L1** | 可选 | `modbusstudio/ui/poll_panel.py:11` | 导入清理：补齐缺失的 `Dict, List` 泛型类型导入，清理未使用的 `Optional`。 |
| **L2** | 可选 | `tests/test_exe_runner.py:6-14` | 增加 `if sys.platform != "win32": pytest.skip(...)` 及 `pytest.importorskip("PIL")`，避免非 Windows 环境或缺失 PIL 时导入崩溃。 |

---

## 二、验收标准对照自核清单

- [x] **H1：`PairingRule.byte_order` 随 `target_format` 变**
  - 实测断言 1（格式 32 - Float 1 ABCD）：
    ```python
    assert rule_32.data_type == ModbusDataType.FLOAT32
    assert rule_32.byte_order == ByteOrderMode.ABCD
    ```
  - 实测断言 2（格式 37 - Double 2 BADC）：
    ```python
    assert rule_37.data_type == ModbusDataType.DOUBLE64
    assert rule_37.byte_order == ByteOrderMode.BADC
    ```
  - 实测断言 3（格式 34 - Float 3 CDAB）：
    ```python
    assert rule_34.data_type == ModbusDataType.FLOAT32
    assert rule_34.byte_order == ByteOrderMode.CDAB
    ```
- [x] **H2：新增实例端口从 502 起探测首个未占用**
  - 预置 502 和 504 占用时，`get_next_available_tcp_port(502)` 分配 503（非 `502+len` 误撞 504）；
  - `db_import_dialog` 中同批导入建立 `used_tcp_ports` 去重；以 `(ep_key, unit_id)` 严格去重，同 `ip:port` 不同 `unit_id` 独立建实例不静默合并。
- [x] **H3：`_parse_port` 防御异常端口**
  - 对 `"abc"`、`"502.5"`、`"0"`、`"65536"`、`"-1"`、`"502 "`、`None` 全兜底回退 502；
  - 正常 `"502"` 仍为 502，`503` 仍为 503；`poll_panel` 不再发生裸 `int()` 崩溃。
- [x] **M1：`write_raw_values` 派生串判定**
  - 对派生串 `"0x_Coil (线圈) @ 1"` 与裸 `"0x_Coil"`，`is_bit` 判定均为 True，布尔区写入 `True`/`False`；保持寄存器写入 99 保持 `int`。
- [x] **M3：HEX16 校验**
  - 采用稳妥口径：剥离 `0x` 前缀后 `int(s, 16)`；
  - 实测断言：`"0x1A2B"`→通过（value=6699）、`"1A2B"`→通过（value=6699）、`"ZZ"`→不匹配（ok=False）。
- [x] **T1：`test_full_ui_import_e2e` 收集运行**
  - pytest 成功收集 `test_full_ui_import_self_test` 并顺利通过（`PASSED`），无静默 skip。
- [x] **可选加固项**
  - M2、L1、L2 均已加固完成。
- [x] **第四节"无问题"条目保护**
  - pymodbus `base_addr` 与 `start_idx = address - 1 - base_addr` 保持一致，**未做任何偏移改动**。
- [x] **红线合规**
  - 0 图片产物、未中止运行中的 ModbusStudio 进程、未触碰 `archive/` 历史归档。

---

## 三、门禁与测试执行结果

### 1. 回归测试 (`tests/test_review_fix_09.py` + `tests/test_full_ui_import_e2e.py`)
```text
============================= test session starts =============================
platform win32 -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0
collected 6 items

tests/test_review_fix_09.py::test_hems_pairing_byte_orders PASSED        [ 16%]
tests/test_review_fix_09.py::test_slave_service_tcp_port_probing PASSED  [ 33%]
tests/test_review_fix_09.py::test_parse_port_safety PASSED               [ 50%]
tests/test_review_fix_09.py::test_is_bit_derivation_robustness PASSED    [ 66%]
tests/test_review_fix_09.py::test_validate_and_build_point_hex16 PASSED  [ 83%]
tests/test_full_ui_import_e2e.py::test_full_ui_import_self_test PASSED   [100%]

============================== 6 passed in 1.65s ==============================
```

### 2. 全量测试 (`.venv\Scripts\pytest.exe -q`)
```text
..........................................................s              [100%]
58 passed, 1 skipped in 16.19s
```

### 3. 记忆系统校验 (`python memory/verify.py`)
```text
========================================================================
Memorix 共享记忆防失真自动验证报告 (memory/verify.py)
========================================================================
生成时间    : 2026-10-13T16:55:33.342886+08:00
仓库根目录  : F:\GitHub\python
记忆目录    : F:\GitHub\python\memory
总体结论    : PASS (错误: 0, 警告: 6)
------------------------------------------------------------------------
【分项检查摘要】
  - 1_required_structure: PASS
  - 2_unique_id: PASS
  - 3_has_source_field: PASS
  - 4_valid_git_sha: PASS
  - 5_source_file_exists: PASS
  - 6_code_drift_since_commit: WARN
  - 7_unreviewed_formal_memory_guard: PASS
  - 8_conflict_and_status_check: PASS
  - 9_worktree_and_unmerged_changes: PASS
  - 10_pytest_execution: PASS (59 passed in 15.89s)
  - 11_needs_human_review: INFO
```
