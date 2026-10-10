# Modbus Studio 2.0 Slave TCP 断线重连自愈 — 修复报告与验收结果

> **对应需求文档**：`docs/Claude code/03_SLAVE_RECONNECT_PROMPT.md`  
> **报告产出路径**：`docs/Antigravity/03_SLAVE_RECONNECT_REPORT.md`  
> **执行人**：Antigravity  
> **验收方**：Claude Code / 用户  

---

## 一、 修复背景与根因回顾

### 1. 现象
EMS（Modbus TCP 主站）与 Modbus Studio 从机通信期间若发生网络波动或 EMS 重启，重连相同 IP:Port 和 Unit ID 时连接失败（TCP 连接超时或拒绝）；界面上的从机状态依然伪显示为“运行中”（假在线），且无明确报错日志。此前恢复的唯一手段为重启程序或重新导入数据库。

### 2. 根因
存在两层“状态僵死”：
1. **底层 Transport 级**：`ModbusTransportServer.start()` 中采用硬性 `if self._is_running: return`。一旦后台 listener 因网络断开或异常退出，`_is_running` 仍残留为 `True`，阻止了后续 listener 的自愈重建。
2. **上层 Service 级**：`SlaveService.start_slave()` 中只检查 `if device.is_running: return` 放行，未对底层物理服务进行探活；且 `ModbusServerHub.register_and_start_slave()` 在复用缓存的 `_servers[key]` 时未做探活，直接复用了已死的 server 实例。

---

## 二、 核心改动清单与实现说明

全部改动均遵循最小改动原则，严格保持此前 6 项基础缺陷修复与轮询配置改动的边界，改动处统一标注 `# [SLAVE-RECONNECT]` 注释。

| 模块 / 类 | 函数 / 代码位置 | 核心改进说明 | 对应 Prompt 要求 |
| :--- | :--- | :--- | :--- |
| **`modbus_engine.py`**<br>`ModbusTransportServer` | `is_healthy` | 实时探测底层事件循环线程、loop 运行状态与 `_server` 实例是否存在（并提供 0.3s 启动宽限期）。 | 修改 1.1 |
| **`modbus_engine.py`**<br>`ModbusTransportServer` | `restart_if_broken` | 若检测到底层 listener 僵死，保留 `_server_context` 与从机路由映射，干净重置线程/事件循环后重新启动监听。 | 修改 1.2 |
| **`modbus_engine.py`**<br>`ModbusTransportServer` | `start` | 将无脑 `if self._is_running: return` 改为探活判定 `if self.is_healthy(): return`；半死状态先清理残留再重建。 | 修改 1.3 |
| **`modbus_engine.py`**<br>`ModbusTransportServer` | `_run_server_loop` | 在 `finally` 块中确保 `self._is_running = False`，并在异常退出时输出 warning 提示下次自动重建。 | 修改 1.4 |
| **`modbus_engine.py`**<br>`ModbusSlaveEngine` | `is_healthy` | 通过 `ModbusServerHub` 反查其所在的 `ModbusTransportServer` 并返回其探活状态。 | 修改 2 |
| **`modbus_engine.py`**<br>`ModbusServerHub` | `get_server_for_engine` | 提供基于 engine 通信参数反查宿主 TransportServer 的线程安全接口。 | 修改 3.1 |
| **`modbus_engine.py`**<br>`ModbusServerHub` | `restart_all_broken` | 遍历扫描所有端点，对所有僵死 listener 触发静默自愈重建，返回成功恢复的数量。 | 修改 3.2 |
| **`modbus_engine.py`**<br>`ModbusServerHub` | `register_and_start_slave` | **【关键点 3b】**：复用端点前强制探活 `if server and not server.is_healthy(): server = None`，彻底阻断复用死 listener。 | 修改 3.3 |
| **`modbus_engine.py`**<br>`ModbusServerHub` | `make_endpoint_key` | 为可选串行参数补齐缺省默认值，保障各类场景反查一致性。 | 修改 3 辅助 |
| **`services.py`**<br>`SlaveService` | `start_slave` | **探活放行**：当 `device.is_running` 时主动探活，若僵死则先调用 `hub.unregister_and_stop_slave(engine)` 彻底清理映射再走安全新建流程。 | 修改 4.1 |
| **`services.py`**<br>`SlaveService` | `auto_reconnect_if_needed` | 暴露给 UI 的一键自愈接口，触发 `hub.restart_all_broken()` 并向系统日志汇报。 | 修改 4.2 |
| **`ui/slave_panel.py`**<br>`SlavePanel` | `_build_ui` & `_on_auto_reconnect` | 在 `right0` 工具栏末尾增加 `🔄 自动重连` 按钮，绑定 `auto_reconnect_if_needed()` 并更新状态反馈。 | 修改 5 |
| **`tests/test_multi_station_concurrency.py`** | 新增 3 个专项验收用例 | 覆盖手动断线重启、上层服务自愈、全端点扫描自愈全链路。 | 修改 6 |

---

## 三、 验收标准对照核验 (Claude 核验项)

| 核验项编号 | 验收标准说明 | 验证命令 / 结果 | 状态 |
| :---: | :--- | :--- | :---: |
| **1** | 核心接口函数定义全部命中：`is_healthy`, `restart_if_broken`, `get_server_for_engine`, `restart_all_broken`, `auto_reconnect_if_needed` | `git grep -n "def is_healthy..."` 命中全部 6 处定义 | **PASS** |
| **2** | `ModbusTransportServer.start()` 不再有无脑 `if self._is_running: return` | 已改为 `if self.is_healthy(): return` | **PASS** |
| **3** | `_run_server_loop` 异常/退出分支 finally 记录 warning | 命中 `logger.warning(f"ModbusTransportServer listener 已退出...")` | **PASS** |
| **3b** | **复用前探活**：`register_and_start_slave` 中僵死换新 | 命中 `if server and not server.is_healthy(): server = None` | **PASS** |
| **4** | `SlaveService.start_slave` 探活清理分支调用 `hub.unregister_and_stop_slave` | 命中 `hub.unregister_and_stop_slave(engine)` | **PASS** |
| **5** | `self._active_engines.pop` 与 `auto_reconnect_if_needed` 存在 | 命中 2 处 pop 清理与接口定义 | **PASS** |
| **6** | UI 从机面板存在 `🔄 自动重连` 按钮并绑定回调 | 命中 `btn_reconnect` 及 `_on_auto_reconnect` 方法 | **PASS** |
| **7** | 新增 3 个专项并发重连用例 | 3 个新用例全部编写并成功通过 | **PASS** |
| **8** | `pytest tests -v` 全量通过（≥41 passed） | **41 passed**，耗时 24.96s，0 失败 | **PASS** |

---

## 四、 全量 pytest tests -v 终端执行日志

```text
============================= test session starts =============================
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
tests/test_hems_import.py::test_hems_database_loading_memory PASSED      [ 73%]
tests/test_hems_import.py::test_hems_pairing_engine_load_rules PASSED    [ 75%]
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

============================= 41 passed in 24.96s =============================
```

---

## 五、 与历史修复边界联动核验

1. **与 01 基础缺陷修复边界**：
   - 保持了 `MultiStationServerContext` 的显式 `address_base` 确定性寻址与 FC4 只读保护；
   - 保持了方案 A 单锁模型，TransportServer 不自建从机字典；
   - 4 个基础并发测试继续稳定通过。
2. **与 02 轮询配置化边界**：
   - 未改动 `ModbusPollEngine` 的事件退出机制及 PollPanel 轮询输入框；
   - 轮询用例 `test_poll_service_continuous_polling_and_custom_interval` 稳定通过。
