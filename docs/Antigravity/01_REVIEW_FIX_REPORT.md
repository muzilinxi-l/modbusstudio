# Modbus Studio 2.0 多站并发架构 — 代码修复报告与测试验收结果

> 本文档记录依据 `REVIEW_FIX_PROMPT.md` 针对 Modbus Studio 2.0 底层多站并发架构执行的 6 项高/中风险缺陷修复摘要、技术实现分析以及全量自动化测试验收结果。

---

## 一、 修复背景与总体策略

根据外部审查提示文档（`REVIEW_FIX_PROMPT.md`）要求，针对 `modbusstudio/modbus_engine.py` 及相关测试用例，按两批次分步实施最小化改动修复，并进行严谨的 Git 分支版本管理：
- **第 1 批（数据正确性 + 资源回收）**：修复缺陷 1、2、3；
- **第 2 批（状态锁模型 + 协议兼容性）**：修复缺陷 4、5、6。

每处改动均在代码上方标注规范标签注释 `# [REVIEW-FIX-1..6]`，未修改 UI 表现层与业务装配逻辑，保持高内聚低耦合。

---

## 二、 缺陷修复清单与技术实现

| 缺陷编号 | 严重程度 | 修改文件与函数 | 核心改进与技术实现 |
| :--- | :---: | :--- | :--- |
| **[REVIEW-FIX-1]** | 🔥 高危 | `MultiStationServerContext.async_getValues`<br>`MultiStationServerContext.async_setValues`<br>`ModbusSlaveEngine.__init__` | **彻底移除看值推断 Hack，实施确定性基准换算**：<br>1. 删除了全部 `values_list[address] == 0` 的启发式代码。<br>2. 引入显式 `address_base: int = 1` 属性，统一根据 `start_idx = address - (1 if base == 1 else 0)` 换算。<br>3. 读越界安全返回 0/False 填充，写越界安全忽略不抛异常，彻底杜绝 0 值漂移与 FLOAT32 撕裂。 |
| **[REVIEW-FIX-2]** | 🔥 高危 | `MultiStationServerContext.async_setValues` | **功能码白名单校验与只读区写保护**：<br>1. 依据 Modbus 协议规约，FC2（读离散输入）和 FC4（读输入寄存器）为严格只读区。<br>2. 在入口增加白名单 `func_code not in (5, 6, 15, 16)`，拦截非写请求并记录 warning，直接作为 no-op 返回 `None`，防止外部请求篡改 1x/3x 只读区。 |
| **[REVIEW-FIX-3]** | ⚠️ 中危 | `ModbusTransportServer.stop`<br>`ModbusTransportServer._run_server_loop` | **完善物理传输层资源回收，杜绝端口冲突泄漏**：<br>1. 在停止时通过 `run_coroutine_threadsafe` 在 loop 内部安全执行 `await server.shutdown()` 并取消任务。<br>2. 在后台子线程退出时显式调用 `self._loop.close()`。<br>3. `thread.join(timeout=3.0)` 超时记录 warning 并兜底清理。<br>4. 彻底将 `_loop`、`_server`、`_server_task`、`_thread` 置空，防止 `[WinError 10048]`。 |
| **[REVIEW-FIX-4]** | ⚠️ 中危 | `ModbusTransportServer`<br>`MultiStationServerContext` | **统一从机状态单真理源（方案 A），消除嵌套锁**：<br>1. 移除 `ModbusTransportServer` 内部重复维护的 `_slaves` 字典与锁。<br>2. 所有从机挂载、注销、查询全部统一委托给 `MultiStationServerContext`，消除了状态漂移与死锁隐患。 |
| **[REVIEW-FIX-5]** | ⚠️ 中危 | `ModbusServerHub.unregister_and_stop_slave`<br>`ModbusServerHub.stop_all` | **缩减 Hub 全局锁临界区，避免跨端点卡顿**：<br>1. 全局锁 `self._lock` 仅用于映射表增删与查找待停 server。<br>2. 将耗时的 `server.stop()` 移至全局锁外部执行，确保停止单端点从机期间，跨端点从机的读写延迟 < 100ms。 |
| **[REVIEW-FIX-6]** | ⚠️ 中危 | `MultiStationServerContext.__init__` | **规范基类初始化与私有分发依赖记录**：<br>1. 补齐 `super().__init__(devices={0: dummy_dev}, single=False)` 调用。<br>2. 规范记录了对 pymodbus `old_simulator` 分发机制的依赖与后续升级回归要求。 |

---

## 三、 专项验收测试补充

在 `tests/test_multi_station_concurrency.py` 中新增了 3 个专项回归与验收用例：
1. `test_async_set_values_fc4_readonly_protection`：针对 FC4 功能码下发写请求，验证被安全拦截为 no-op，数据区保持原样不变。
2. `test_address_indexing_deterministic_without_hack`：点位 10 为 0、点位 9 为 8888 时，验证读取点位 10 恒定返回 `[0]`，绝对不发生索引错位漂移。
3. `test_cross_endpoint_non_blocking_during_stop`：在 A 端点执行耗时注销停止期间，并发测量 B 端点的读取请求耗时，验证延迟 < 100ms（未受全局锁阻塞）。

---

## 四、 全量 pytest tests -v 测试执行日志

```text
============================= test session starts =============================
platform win32 -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- F:\GitHub\python\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: F:\GitHub\python
configfile: pyproject.toml
collecting ... collected 37 items

tests/test_batch_import_and_limits.py::test_slave_quota_independent_limits PASSED [  2%]
tests/test_batch_import_and_limits.py::test_batch_device_import_simulation PASSED [  5%]
tests/test_batch_import_and_limits.py::test_batch_device_merge_and_replace PASSED [  8%]
tests/test_batch_import_and_limits.py::test_db_import_dialog_default_selection_policy PASSED [ 10%]
tests/test_batch_import_and_limits.py::test_clear_all_devices_on_database_switch PASSED [ 13%]
tests/test_codec.py::test_uint16_codec PASSED                            [ 16%]
tests/test_codec.py::test_int16_negative_codec PASSED                    [ 18%]
tests/test_codec.py::test_float32_all_byte_orders PASSED                 [ 21%]
tests/test_codec.py::test_float32_word_swap_cdab PASSED                  [ 24%]
tests/test_codec.py::test_int32_codec PASSED                             [ 27%]
tests/test_codec.py::test_bool_codec PASSED                              [ 29%]
tests/test_datatype_area_and_sine_sim.py::test_area_type_normalization_priority PASSED [ 32%]
tests/test_datatype_area_and_sine_sim.py::test_alarm_points_auto_mapping_and_initial_values PASSED [ 35%]
tests/test_datatype_area_and_sine_sim.py::test_point_service_hex_encoding PASSED [ 37%]
tests/test_datatype_area_and_sine_sim.py::test_sine_simulation_and_runtime_sync PASSED [ 40%]
tests/test_datatype_area_and_sine_sim.py::test_status_word_numeric_protection PASSED [ 43%]
tests/test_datatype_area_and_sine_sim.py::test_composite_key_multi_area_same_address_isolation PASSED [ 45%]
tests/test_datatype_area_and_sine_sim.py::test_boolean_float_text_parsing PASSED [ 48%]
tests/test_engine.py::test_validate_and_build_point_success PASSED       [ 51%]
tests/test_engine.py::test_validate_and_build_point_invalid_address PASSED [ 54%]
tests/test_engine.py::test_slave_service_registration_and_retrieval PASSED [ 56%]
tests/test_engine.py::test_slave_service_station_collision_prevention PASSED [ 59%]
tests/test_engine.py::test_slave_service_same_port_multi_station_support PASSED [ 62%]
tests/test_engine.py::test_slave_engine_pre_startup_data_persistence PASSED [ 64%]
tests/test_engine.py::test_input_register_3x_read_write PASSED           [ 67%]
tests/test_engine.py::test_sine_wave_simulation_value_changes PASSED     [ 70%]
tests/test_exe_runner.py::test_exe_launch_and_window_zoomed PASSED       [ 72%]
tests/test_hems_import.py::test_map_db_format_to_data_type PASSED        [ 75%]
tests/test_hems_import.py::test_hems_database_loading_memory PASSED      [ 78%]
tests/test_hems_import.py::test_hems_pairing_engine_load_rules PASSED    [ 81%]
tests/test_hems_import.py::test_datatype_and_point_service_float32 PASSED [ 83%]
tests/test_multi_station_concurrency.py::test_same_port_three_slaves_concurrency_and_data_isolation PASSED [ 86%]
tests/test_multi_station_concurrency.py::test_same_port_same_station_collision_rejection PASSED [ 89%]
tests/test_multi_station_concurrency.py::test_async_set_values_fc4_readonly_protection PASSED [ 91%]
tests/test_multi_station_concurrency.py::test_address_indexing_deterministic_without_hack PASSED [ 94%]
tests/test_multi_station_concurrency.py::test_cross_endpoint_non_blocking_during_stop PASSED [ 97%]
tests/test_ui_theme.py::test_main_window_theme_switching SKIPPED (...) [100%]

======================= 36 passed, 1 skipped in 20.21s ========================
```

---

## 五、 Git 提交记录

- `ccf64b1 fix(engine): resolve defects 4, 5, 6 in modbus multi-station architecture`
- `a72c668 fix(engine): resolve defects 1, 2, 3 in modbus engine`
