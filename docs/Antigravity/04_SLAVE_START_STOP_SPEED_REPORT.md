# 从机“全部启动 / 全部停止 / 窗口关闭”提速优化报告

> 目标仓库：`F:\GitHub\python`，修改方：Antigravity
> 关联 Prompt：[`docs/Claude code/04_SLAVE_START_STOP_SPEED_PROMPT.md`](file:///F:/GitHub/python/docs/Claude%20code/04_SLAVE_START_STOP_SPEED_PROMPT.md)
> 日期：2026-10-12

---

## 一、改动文件清单与逐条对应 Prompt 修改条目

### 1. [`modbusstudio/modbus_engine.py`](file:///F:/GitHub/python/modbusstudio/modbus_engine.py)

| 函数 / 位置 | 对应 Prompt 修改 | 说明 |
|---|---|---|
| `ModbusTransportServer.__init__` | 修改 1 | 新增 `self._ready = threading.Event()`，用于事件驱动唤醒主线程启动等待 |
| `ModbusTransportServer.start` | 修改 1 | 保留 `is_healthy()` 与半死重建分支及 `[SLAVE-RESTART]` 日志措辞；移除 `for _ in range(30): time.sleep(0.03)` 盲等循环，改为 `self._ready.clear()` + `self._ready.wait(timeout=1.0)` + 未就绪 `logger.warning` |
| `ModbusTransportServer._run_server_loop` | 修改 1 | 在 `self._server_task = asyncio.create_task(self._server.serve_forever())` 后立即调用 `self._ready.set()`；异常分支（`CancelledError`、`Exception`）及 `finally` 块均调用 `self._ready.set()` 防主线程挂起 |
| `ModbusTransportServer.stop` | 修改 1 & 修改 2 | 将 `fut.result(timeout=2.0)` 压缩为 `1.0`；将 `self._thread.join(timeout=3.0)` 压缩为 `1.0`；末尾执行 `self._ready.clear()` 确保可重复启动 |
| `ModbusSlaveEngine.__init__` | 修改 3 | 新增 `self._sim_stop_evt = threading.Event()` |
| `ModbusSlaveEngine.start` | 修改 3 | 启动 `_sim_thread` 前执行 `self._sim_stop_evt.clear()` |
| `ModbusSlaveEngine.stop` | 修改 3 | 置 `self._sim_stop_evt.set()` 立即唤醒仿真线程，并将 `self._sim_thread.join(timeout=1.0)` 压缩为 `timeout=0.3` |
| `ModbusSlaveEngine._run_simulation_loop` | 修改 3 | 循环条件改为 `while self._sim_running and not self._sim_stop_evt.is_set():`，将 `time.sleep(1.0)` 替换为 `if self._sim_stop_evt.wait(1): break`，支持毫秒级唤醒退出 |

### 2. [`modbusstudio/ui/slave_panel.py`](file:///F:/GitHub/python/modbusstudio/ui/slave_panel.py)

| 函数 / 位置 | 对应 Prompt 修改 | 说明 |
|---|---|---|
| 顶部导入 & `SlavePanel.__init__` | 修改 4 | 导入 `threading`；新增 `self._busy = {"start": False, "stop": False}` 防重复点击重入守卫 |
| `SlavePanel._start_all_slaves` | 修改 4 | 保持方法签名不变；通过 `self._busy["start"]` 防重入，将逐个 `start_slave` 与失败收集移入后台 `daemon=True` 线程，完成后经 `root.after(0, _on_done)` 回主线程刷新状态指示器与弹出 `messagebox.showwarning` |
| `SlavePanel._stop_all_slaves` | 修改 4 | 保持方法签名不变；通过 `self._busy["stop"]` 防重入，将逐个 `stop_slave` 移入后台 `daemon=True` 线程，完成后经 `root.after(0, _on_done)` 回主线程刷新状态指示器 |

### 3. [`modbusstudio/app.py`](file:///F:/GitHub/python/modbusstudio/app.py)

| 函数 / 位置 | 对应 Prompt 修改 | 说明 |
|---|---|---|
| 顶部导入 & `ModbusStudioAppCore.shutdown` | 修改 5 | 导入 `logging` 与 `threading`；将遍历从机 `stop_slave(dev.id)` 移入后台 `daemon=True` 线程，配合 `done_evt.wait(timeout=2.0)` 上限兜底（超时打 warning 后继续销毁窗口），`poll_service.disconnect()` 与 `logging_service.close()` 仍在主线程完成 |

### 4. [`tests/test_slave_start_stop_speed.py`](file:///F:/GitHub/python/tests/test_slave_start_stop_speed.py)（新建）

| 用例名称 | 对应 Prompt 修改 | 说明 |
|---|---|---|
| `test_transport_server_start_event_driven_fast` | 修改 6 用例 A | 临时端口 `5097` 启动 `ModbusTransportServer`，断言 `start()` 耗时 `< 0.4s` |
| `test_transport_server_stop_fast` | 修改 6 用例 B | 启动后立即 `stop()`，断言 `stop()` 耗时 `< 1.5s` |
| `test_slave_engine_stop_wakes_sim_thread_immediately` | 修改 6 用例 C | 启动 `ModbusSlaveEngine` 50ms（确保 `_sim_thread` 进入 `wait(1)`）后调用 `stop()`，断言耗时 `< 0.6s` |

---

## 二、A / B / C 三个计时用例实测耗时与旧实现对比

命令：`.venv\Scripts\python.exe -m pytest tests/test_slave_start_stop_speed.py -v -s`

| 计时用例 | 旧实现耗时基线 | Prompt 验收阈值 | 本次优化后实测耗时 | 提速幅度 |
|---|---|---|---|---|
| **用例 A**：`ModbusTransportServer.start()` | `~30 ms – 900 ms`（`30×0.03s` 固定盲等） | `< 0.4000 s` (`400 ms`) | **`0.0043 s` (`4.26 ms`)** | **约 210x**（消除固定轮询盲等） |
| **用例 B**：`ModbusTransportServer.stop()` | 正常数毫秒 / 僵死上限 `5.0 s` (`2.0+3.0`) | `< 1.5000 s` (`1500 ms`) | **`0.0007 s` (`0.73 ms`)**（僵死上限压至 `2.0 s`） | 正常亚毫秒级，僵死上限缩短 **60%** |
| **用例 C**：`ModbusSlaveEngine.stop()`（含仿真线程退出） | **`~0.95 s – 1.00 s`**（`time.sleep(1.0)` 无法提前唤醒） | `< 0.6000 s` (`600 ms`) | **`0.0020 s` (`2.05 ms`)** | **约 460x**（`Event.set()` 立即唤醒退出） |

### 计时用例终端实跑输出
```text
============================= test session starts =============================
platform win32 -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- F:\GitHub\python\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: F:\GitHub\python
configfile: pyproject.toml
collecting ... collected 3 items

tests/test_slave_start_stop_speed.py::test_transport_server_start_event_driven_fast 
[TIMING-A] ModbusTransportServer.start() elapsed = 4.26 ms (0.0043 s)
PASSED
tests/test_slave_start_stop_speed.py::test_transport_server_stop_fast 
[TIMING-B] ModbusTransportServer.stop() elapsed = 0.73 ms (0.0007 s)
PASSED
tests/test_slave_start_stop_speed.py::test_slave_engine_stop_wakes_sim_thread_immediately 
[TIMING-C] ModbusSlaveEngine.stop() elapsed = 2.05 ms (0.0020 s)
PASSED

============================== 3 passed in 0.47s ==============================
```

---

## 三、3 个不同端点（TCP 5091 / 5092 / 5093）冒烟验证记录

针对验收标准 3，构造 3 个独立 TCP 端口（`127.0.0.1:5091`、`5092`、`5093`）的从机实例，实测“全部启动”、“全部停止”与 `app.shutdown()` 耗时：

```text
[SMOKE-3-ENDPOINTS] 3 端点全部启动总耗时: 709.22 ms (0.7092 s)  # < 1.0s，且 UI 已移出主线程零卡顿
[SMOKE-3-ENDPOINTS] 3 端点全部停止总耗时:  39.60 ms (0.0396 s)  # < 2.0s（旧实现 3×1s ≈ 3.0s）
[SMOKE-3-ENDPOINTS] 3 端点 app.shutdown() 耗时: 13.67 ms (0.0137 s) # 窗口关闭瞬间完成，不冻结
```

---

## 四、`pytest` 全量输出（基线 41 + 新增 3 = 44 passed，0 回归）

### 1. `.venv\Scripts\python.exe -m pytest tests -q`
```text
............................................                             [100%]
44 passed in 20.30s
```

### 2. `.venv\Scripts\python.exe -m pytest tests -v`
```text
============================= test session starts =============================
platform win32 -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- F:\GitHub\python\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: F:\GitHub\python
configfile: pyproject.toml
collecting ... collected 44 items

tests/test_batch_import_and_limits.py::test_slave_quota_independent_limits PASSED [  2%]
tests/test_batch_import_and_limits.py::test_batch_device_import_simulation PASSED [  4%]
tests/test_batch_import_and_limits.py::test_batch_device_merge_and_replace PASSED [  6%]
tests/test_batch_import_and_limits.py::test_db_import_dialog_default_selection_policy PASSED [  9%]
tests/test_batch_import_and_limits.py::test_clear_all_devices_on_database_switch PASSED [ 11%]
tests/test_codec.py::test_uint16_codec PASSED                            [ 13%]
tests/test_codec.py::test_int16_negative_codec PASSED                    [ 15%]
tests/test_codec.py::test_float32_all_byte_orders PASSED                 [ 18%]
tests/test_codec.py::test_float32_word_swap_cdab PASSED                  [ 20%]
tests/test_codec.py::test_int32_codec PASSED                             [ 22%]
tests/test_codec.py::test_bool_codec PASSED                              [ 25%]
tests/test_datatype_area_and_sine_sim.py::test_area_type_normalization_priority PASSED [ 27%]
tests/test_datatype_area_and_sine_sim.py::test_alarm_points_auto_mapping_and_initial_values PASSED [ 29%]
tests/test_datatype_area_and_sine_sim.py::test_point_service_hex_encoding PASSED [ 31%]
tests/test_datatype_area_and_sine_sim.py::test_sine_simulation_and_runtime_sync PASSED [ 34%]
tests/test_datatype_area_and_sine_sim.py::test_status_word_numeric_protection PASSED [ 36%]
tests/test_datatype_area_and_sine_sim.py::test_composite_key_multi_area_same_address_isolation PASSED [ 38%]
tests/test_datatype_area_and_sine_sim.py::test_boolean_float_text_parsing PASSED [ 40%]
tests/test_engine.py::test_validate_and_build_point_success PASSED       [ 43%]
tests/test_engine.py::test_validate_and_build_point_invalid_address PASSED [ 45%]
tests/test_engine.py::test_slave_service_registration_and_retrieval PASSED [ 47%]
tests/test_engine.py::test_slave_service_station_collision_prevention PASSED [ 50%]
tests/test_engine.py::test_slave_service_same_port_multi_station_support PASSED [ 52%]
tests/test_engine.py::test_slave_engine_pre_startup_data_persistence PASSED [ 54%]
tests/test_engine.py::test_input_register_3x_read_write PASSED           [ 56%]
tests/test_engine.py::test_sine_wave_simulation_value_changes PASSED     [ 59%]
tests/test_engine.py::test_poll_service_continuous_polling_and_custom_interval PASSED [ 61%]
tests/test_exe_runner.py::test_exe_launch_and_window_zoomed PASSED       [ 63%]
tests/test_hems_import.py::test_map_db_format_to_data_type PASSED        [ 65%]
tests/test_hems_import.py::test_hems_database_loading_memory PASSED      [ 68%]
tests/test_hems_import.py::test_hems_pairing_engine_load_rules PASSED    [ 70%]
tests/test_hems_import.py::test_datatype_and_point_service_float32 PASSED [ 72%]
tests/test_multi_station_concurrency.py::test_same_port_three_slaves_concurrency_and_data_isolation PASSED [ 75%]
tests/test_multi_station_concurrency.py::test_same_port_same_station_collision_rejection PASSED [ 77%]
tests/test_multi_station_concurrency.py::test_async_set_values_fc4_readonly_protection PASSED [ 79%]
tests/test_multi_station_concurrency.py::test_address_indexing_deterministic_without_hack PASSED [ 81%]
tests/test_multi_station_concurrency.py::test_cross_endpoint_non_blocking_during_stop PASSED [ 84%]
tests/test_multi_station_concurrency.py::test_slave_listener_restart_after_stop_restarts PASSED [ 86%]
tests/test_multi_station_concurrency.py::test_slave_service_start_slave_heals_broken_state PASSED [ 88%]
tests/test_multi_station_concurrency.py::test_hub_restart_all_broken_counts_correctly PASSED [ 90%]
tests/test_slave_start_stop_speed.py::test_transport_server_start_event_driven_fast PASSED [ 93%]
tests/test_slave_start_stop_speed.py::test_transport_server_stop_fast PASSED [ 95%]
tests/test_slave_start_stop_speed.py::test_slave_engine_stop_wakes_sim_thread_immediately PASSED [ 97%]
tests/test_ui_theme.py::test_main_window_theme_switching PASSED          [100%]

============================= 44 passed in 20.11s =============================
```

---

## 五、硬约束与 01 / 02 / 03 成果保持核验

- **01 成果保持**：`MultiStationServerContext` 的 `address_base` 显式索引、`FC2/FC4` 写保护、双锁收敛、`super().__init__(devices={0: dummy_dev})`、`test_cross_endpoint_non_blocking_during_stop` 均未改动且测试全绿。
- **02 成果保持**：`POLL_CONFIG`、`PollTask.interval_ms` 与 `_continuous` 轮询循环未触碰。
- **03 成果保持**：`[SLAVE-RECONNECT]` 探活链路（`is_healthy`、`restart_if_broken`、`3b` 复用前探活、`unregister_and_stop_slave` 锁外停）及 `ModbusTransportServer.start()` 的半死重建分支与日志措辞原封不动保留。
- **线程安全与函数签名**：`_ready` 与 `_sim_stop_evt` 均未在 Context `_lock` 内操作；所有公开方法签名与返回语义保持 100% 兼容。
