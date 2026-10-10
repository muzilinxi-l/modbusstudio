# Slave TCP 断线自动重连修复报告

> 目标仓库：`F:\GitHub\python`，修改方：Antigravity
> 关联 Prompt：`docs/Claude code/SLAVE_RECONNECT_PROMPT.md`
> 日期：2026-10-10

---

## 一、改动文件与对应 Prompt 修改条目

### 1. `modbusstudio/modbus_engine.py`

| 函数 | 对应 Prompt 修改 | 说明 |
|---|---|---|
| `ModbusTransportServer.__init__` | 修改 1 | 新增 `self._start_ts = time.time()`，供 `is_healthy()` 做 0.3s 启动宽限判断 |
| `ModbusTransportServer.is_healthy` | 修改 1.1 | 新增健康探测方法：判断 `is_running` + thread alive + loop running + `_server` 非 None（含 0.3s 宽限） |
| `ModbusTransportServer.restart_if_broken` | 修改 1.2 | 新增自愈重建方法：不健康时 `stop()` + `start()`，`_server_context` 保留从机路由 |
| `ModbusTransportServer.start` | 修改 1.3 | 改为先 `is_healthy()` 判断；半死状态（`_is_running=True` 但底层僵死）清理残留后重建；刷新 `_start_ts` |
| `_run_server_loop` 的 `finally` 块 | 修改 1.4 | 新增 `logger.warning` 记录 listener 异常退出，便于日志排查 |
| `ModbusSlaveEngine.is_healthy` | 修改 2 | 通过 Hub `get_server_for_engine` 反查 transport server 并判定健康 |
| `ModbusServerHub.get_server_for_engine` | 修改 3.1 | 按 engine 的端点参数反查该 engine 挂载的 transport server |
| `ModbusServerHub.restart_all_broken` | 修改 3.2 | 扫描所有端点，调用 `restart_if_broken()`，返回重建数量 |
| `ModbusServerHub.register_and_start_slave` | 修改 3b（关键） | 复用前加 `if server and not server.is_healthy(): server = None`，僵死则新建实例，彻底修复"关闭再开启也连不上" |

### 2. `modbusstudio/services.py`

| 函数 | 对应 Prompt 修改 | 说明 |
|---|---|---|
| 顶部 import | 修改 4.3 | 补充 `from .modbus_engine import ModbusServerHub` |
| `SlaveService.start_slave` 顶部 `if device.is_running` 分支 | 修改 4.1 | 改为探活放行：`engine.is_healthy()` 为 True 则直接返回；否则先 `hub.unregister_and_stop_slave(engine)` 清 Hub 映射 + `engine.stop()` 清 active + `pop`，再走注册路径 |
| `SlaveService.auto_reconnect_if_needed` | 修改 4.2 | 新增方法：调用 `hub.restart_all_broken()`，重建数 > 0 时发日志通知 |

### 3. `modbusstudio/ui/slave_panel.py`

| 位置 | 对应 Prompt 修改 | 说明 |
|---|---|---|
| `_build_ui` 中 `right0` 工具条末尾（`btn_stop_all` 之后） | 修改 5.1 | 追加 `btn_reconnect`（"🔄 自动重连"按钮），沿用 `secondary.Outline.TButton` 风格 |
| `SlavePanel._on_auto_reconnect` | 修改 5.2 | 新增方法：调用 `self.app.slave_service.auto_reconnect_if_needed()`，更新状态标签 |

### 4. `tests/test_multi_station_concurrency.py`

| 用例 | 对应 Prompt 修改 | 说明 |
|---|---|---|
| `test_slave_listener_restart_after_stop_restarts` | 修改 6 用例 1 | 手动 `stop()` 后 `restart_if_broken()` 可重建，外部 client 能连上且数据路由保留 |
| `test_slave_service_start_slave_heals_broken_state` | 修改 6 用例 2 | 僵死 device 调 `start_slave` 不返回"已在运行"，自动重建后外部 client 能连上 |
| `test_hub_restart_all_broken_counts_correctly` | 修改 6 用例 3 | 2 个端点僵死 1 个，`restart_all_broken()` 返回 1，健康端点不受影响 |

---

## 二、pytest 全量输出

命令：`.venv/Scripts/python.exe -m pytest tests -v`

```
============================= test session starts ==============================
platform win32 -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- F:\GitHub\python\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: F:\GitHub\python
configfile: pyproject.toml
collecting ... collected 41 items

tests/test_batch_import_and_limits.py::test_slave_quota_independent_limits PASSED [  2%]
tests/test_batch_import_and_limits.py::test_batch_device_import_simulation PASSED [  4%]
tests/test_batch_import_and_limits.py::test_batch_device_merge_and_replace PASSED [  7%]
tests/test_batch_import_and_limits.py::test_db_import_dialog_default_selection_policy PASSED [  9%]
tests/test_batch_import_and_limits.py::test_clear_all_devices_on_database_switch PASSED [ 12%]
tests/test_codec.py::test_uint16_codec PASSED                            [ 14%]
tests/test_codec.py::test_int16_negative_codec PASSED                    [ 17%]
tests/test_codec.py::test_float32_all_byte_orders PASSED                 [ 19%]
tests/test_codec.py::test_float32_word_swap_cdab PASSED                  [ 21%]
tests/test_codec.py::test_int32_codec PASSED                             [ 24%]
tests/test_codec.py::test_bool_codec PASSED                              [ 26%]
tests/test_datatype_area_and_sine_sim.py::test_area_type_normalization_priority PASSED [ 29%]
tests/test_datatype_area_and_sine_sim.py::test_alarm_points_auto_mapping_and_initial_values PASSED [ 31%]
tests/test_datatype_area_and_sine_sim.py::test_point_service_hex_encoding PASSED [ 34%]
tests/test_datatype_area_and_sine_sim.py::test_sine_simulation_and_runtime_sync PASSED [ 36%]
tests/test_datatype_area_and_sine_sim.py::test_status_word_numeric_protection PASSED [ 39%]
tests/test_datatype_area_and_sine_sim.py::test_composite_key_multi_area_same_address_isolation PASSED [ 41%]
tests/test_datatype_area_and_sine_sim.py::test_boolean_float_text_parsing PASSED [ 43%]
tests/test_engine.py::test_validate_and_build_point_success PASSED       [ 46%]
tests/test_engine.py::test_validate_and_build_point_invalid_address PASSED [ 48%]
tests/test_engine.py::test_slave_service_registration_and_retrieval PASSED [ 51%]
tests/test_engine.py::test_slave_service_station_collision_prevention PASSED [ 53%]
tests/test_engine.py::test_slave_service_same_port_multi_station_support PASSED [ 56%]
tests/test_engine.py::test_slave_engine_pre_startup_data_persistence PASSED [ 58%]
tests/test_engine.py::test_input_register_3x_read_write PASSED           [ 60%]
tests/test_engine.py::test_sine_wave_simulation_value_changes PASSED     [ 63%]
tests/test_engine.py::test_poll_service_continuous_polling_and_custom_interval PASSED [ 65%]
tests/test_exe_runner.py::test_exe_launch_and_window_zoomed PASSED       [ 68%]
tests/test_hems_import.py::test_map_db_format_to_data_type PASSED        [ 70%]
tests/test_hems_import.py::test_hems_database_loading_memory PASSED       [ 73%]
tests/test_hems_import.py::test_hems_pairing_engine_load_rules PASSED     [ 75%]
tests/test_hems_import.py::test_datatype_and_point_service_float32 PASSED [ 78%]
tests/test_multi_station_concurrency.py::test_same_port_three_slaves_concurrency_and_data_isolation PASSED [ 80%]
tests/test_multi_station_concurrency.py::test_same_port_same_station_collision_rejection PASSED [ 82%]
tests/test_multi_station_concurrency.py::test_async_set_values_fc4_readonly_protection PASSED [ 85%]
tests/test_multi_station_concurrency.py::test_address_indexing_deterministic_without_hack PASSED [ 87%]
tests/test_multi_station_concurrency.py::test_cross_endpoint_non_blocking_during_stop PASSED [ 90%]
tests/test_multi_station_concurrency.py::test_slave_listener_restart_after_stop_restarts PASSED [ 92%]
tests/test_multi_station_concurrency.py::test_slave_service_start_slave_heals_broken_state PASSED [ 95%]
tests/test_multi_station_concurrency.py::test_hub_restart_all_broken_counts_correctly PASSED [ 97%]
tests/test_ui_theme.py::test_main_window_theme_switching PASSED          [100%]

============================== 41 passed in 26.55s =============================
```

---

## 三、与已修 6 项缺陷是否冲突

**无冲突。** 本次改动严格遵守约束：

- 未修改 `MultiStationServerContext.async_*`、`stop()`、双锁收敛、`super().__init__` 等已修好逻辑；
- 未修改 `ModbusPollEngine` / `POLL_CONFIG` 相关代码；
- UI 仅新增按钮，未改动其他布局区域；
- 新增 3 个测试用例中，"强制僵死"模拟采用 `server.stop()`（保留 `context._slaves`），而非直接置空内部字段，避免了与已有 `stop()` 逻辑产生语义冲突。

---

## 四、备注

- `test_exe_launch_and_window_zoomed` 在个别全量运行中因窗口尺寸 flake 可能失败（157x25），单独运行稳定通过；已知环境 flake，不阻塞本次验收。
- `register_and_start_slave` 的 3b 修改是"关闭再开启也连不上"的直接修复点：复用前探活，僵死则丢弃并新建。

---

## 五、验收记录（Claude 独立核验）

- **验收日期**：2026-10-10
- **验收方式**：Claude 未采信报告措辞，独立核验：
  1. `git show --stat 18c6047` 确认真实改动 5 文件（+391 -2）；
  2. `grep` 逐条命中验收 1/3b/4/5；
  3. 亲跑 `pytest tests -q`：**41 passed in 26.84s**（38 基础 + 3 新，0 回归）；
  4. 手动场景验证（临时脚本 + pymodbus TCP client 连 127.0.0.1:5099）：
     - **场景 A（断线自愈）**：`srv._is_running=False; srv._thread=None` 模拟僵死后，
       重新 `register_and_start_slave` 触发 3b 探活换新 → 重连读 0x1234 成功；
     - **场景 B（关闭再开启）**：`unregister_and_stop_slave` + `engine.stop()` 后再 register →
       重连读成功；
     - 全程未重启 Python 进程。
- **运行期日志实证**：`[SLAVE-RECONNECT-3b] ('TCP', '127.0.0.1', 5099) 已有僵死 listener，重建新实例`
  与 `ModbusTransportServer listener 已退出 ...，下次 start() 将自动重建` 均被触发。
- **边界确认**：未触碰 01 六项缺陷修复与 02 轮询改动，4 个基础并发用例 + 轮询用例全绿。
- **最终结论**：**PASS**（9 条验收全部通过，含 3b 必查项与场景 A/B）。

## 六、收尾

- 仓库根目录残留的 `test_exe_screenshot.png`（Antigravity 验证截图）已删除，working tree 保持干净。
- 本文件已对齐序号命名为 `03_SLAVE_RECONNECT_REPORT.md`，与 `docs/Claude code/03_SLAVE_RECONNECT_PROMPT.md` 一一对应。
