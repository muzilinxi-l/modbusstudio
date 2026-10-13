# 06 全仓 Python 代码教学级中文注释补齐执行报告 (06_CODE_COMMENT_LEARNING_REPORT)

- **执行 Agent**：Antigravity
- **任务指令来源**：[`docs/Claude code/06_CODE_COMMENT_LEARNING_PROMPT.md`](file:///F:/GitHub/python/docs/Claude%20code/06_CODE_COMMENT_LEARNING_PROMPT.md)
- **执行时间**：2026-10-12
- **工作仓库**：`F:/GitHub/python` (Modbus Studio 2.0)

---

## 1. 任务目标与红线执行摘要

本次任务面向 `modbusstudio/` 下的 **12 个核心 `.py` 源码文件**（不含 `archive/` 历史快照），系统化补齐 **5 级中文教学注释**（模块级 docstring、类级 docstring、函数/方法级 docstring、行内“为什么”注释、复杂代码块前置导读），让具备基础 Python 语法的工程师能够顺畅通读全仓代码并理解 Modbus TCP/RTU 协议栈、多从机端口复用、异步事件循环与 Tkinter 主线程安全调度的设计精髓。

### 铁律与红线核验结果

| 验收维度 | 目标标准 | 实测结果 | 结论 |
| :--- | :--- | :--- | :--- |
| **1. 模块级 docstring** | 12 个 `.py` 文件顶部全部包含 `模块职责 / 关键依赖 / 对外暴露` | **12 / 12 (100%)** | ✅ PASS |
| **2. 类与函数级 docstring** | `def` / `async def` / `class` 覆盖率 ≥ 95%（含嵌套函数） | **290 / 290 (100.0%)** | ✅ PASS |
| **3. 零业务代码篡改** | 去 docstring 后的 AST 与改动前基线 100% 一致（不改任何可执行语句、签名、控制流） | **12 / 12 `ast_ok=True`** | ✅ PASS |
| **4. 注释密度达标** | 普通文件 `^\s*#` 行数占比 ≥ 30%，核心引擎 `modbus_engine.py` ≥ 40% | 普通文件 **30.1%~32.9%**，`modbus_engine.py` **40.6%**（非空行口径 **43.7%**） | ✅ PASS |
| **5. 术语规范统一** | 统一使用 `EMS`/`BMS`/`PCS`/`一体机`、`从机`/`主机`/`端点`/`站点`；注释与 docstring 中零孤立英文 `slave`/`master` | **`bad_terms=0`**（保留 `[SLAVE-RECONNECT]` 等既有日志标签） | ✅ PASS |
| **6. 自动化测试门禁** | `.venv\Scripts\python.exe -m pytest tests -q` 全量通过 | **44 passed, 0 failed** | ✅ PASS |
| **7. 共享记忆防失真校验** | `.venv\Scripts\python.exe memory/verify.py` 无新增错误 | **`overall_status: PASS` (`error_count: 0`)** | ✅ PASS |
| **8. 范围隔离** | 不动 `archive/`、`memory/`、`tests/`、`examples/` | 仅修改 `modbusstudio/` 下 12 个目标文件 | ✅ PASS |

---

## 2. 12 个文件改动前后行数与注释密度对比表

> **统计口径说明**：
> - **原行数 / 新行数**：文件总行数（含空行）。
> - **`#` 注释行数（占比）**：匹配正则 `^\s*#` 的纯注释行数及其占总行数 / 非空行数的百分比。
> - **Docstring + `#` 总注释占比**：模块/类/函数 docstring 行数与 `#` 注释行数之和占非空总行数的比例。
> - **Docstring 节点覆盖率**：通过 Python `ast.walk()` 统计的 `Module` + `ClassDef` + `FunctionDef` + `AsyncFunctionDef`（含内部嵌套函数）覆盖比例。

| 序号 | 文件路径 | 原行数 | 新行数 | 新增行数 | `#` 注释行数 (占总行 / 占非空行) | Docstring+`#` 占非空行 | Docstring 覆盖率 | AST 零改动校验 |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 1 | [`modbusstudio/app.py`](file:///F:/GitHub/python/modbusstudio/app.py) | 178 | 295 | +117 | 89 (**30.2%** / 32.8%) | 48.7% | **9 / 9 (100%)** | `ast_ok=True` |
| 2 | [`modbusstudio/models.py`](file:///F:/GitHub/python/modbusstudio/models.py) | 356 | 652 | +296 | 198 (**30.4%** / 32.6%) | 52.3% | **30 / 30 (100%)** | `ast_ok=True` |
| 3 | [`modbusstudio/modbus_codec.py`](file:///F:/GitHub/python/modbusstudio/modbus_codec.py) | 458 | 634 | +176 | 191 (**30.1%** / 33.5%) | 40.5% | **10 / 10 (100%)** | `ast_ok=True` |
| 4 | [`modbusstudio/modbus_engine.py`](file:///F:/GitHub/python/modbusstudio/modbus_engine.py) | 1363 | 2209 | +846 | 897 (**40.6%** / 43.7%) | 48.1% | **68 / 68 (100%)** | `ast_ok=True` |
| 5 | [`modbusstudio/services.py`](file:///F:/GitHub/python/modbusstudio/services.py) | 650 | 885 | +235 | 275 (**31.1%** / 33.6%) | 38.0% | **32 / 32 (100%)** | `ast_ok=True` |
| 6 | [`modbusstudio/hems_db_loader.py`](file:///F:/GitHub/python/modbusstudio/hems_db_loader.py) | 481 | 681 | +200 | 211 (**31.0%** / 34.1%) | 37.8% | **19 / 19 (100%)** | `ast_ok=True` |
| 7 | [`modbusstudio/hems_pairing.py`](file:///F:/GitHub/python/modbusstudio/hems_pairing.py) | 202 | 295 | +93 | 92 (**31.2%** / 35.0%) | 41.8% | **14 / 14 (100%)** | `ast_ok=True` |
| 8 | [`modbusstudio/ui/main_window.py`](file:///F:/GitHub/python/modbusstudio/ui/main_window.py) | 414 | 586 | +172 | 183 (**31.2%** / 35.4%) | 39.8% | **19 / 19 (100%)** | `ast_ok=True` |
| 9 | [`modbusstudio/ui/slave_panel.py`](file:///F:/GitHub/python/modbusstudio/ui/slave_panel.py) | 986 | 1434 | +448 | 439 (**30.6%** / 34.4%) | 39.6% | **43 / 43 (100%)** | `ast_ok=True` |
| 10 | [`modbusstudio/ui/poll_panel.py`](file:///F:/GitHub/python/modbusstudio/ui/poll_panel.py) | 400 | 586 | +186 | 182 (**31.1%** / 34.9%) | 39.7% | **21 / 21 (100%)** | `ast_ok=True` |
| 11 | [`modbusstudio/ui/dialogs/db_import_dialog.py`](file:///F:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py) | 605 | 825 | +220 | 255 (**30.9%** / 34.6%) | 37.6% | **18 / 18 (100%)** | `ast_ok=True` |
| 12 | [`modbusstudio/ui/dialogs/pairing_dialog.py`](file:///F:/GitHub/python/modbusstudio/ui/dialogs/pairing_dialog.py) | 167 | 227 | +60 | 72 (**31.7%** / 35.8%) | 41.3% | **7 / 7 (100%)** | `ast_ok=True` |
| **合计** | **12 个核心源码文件** | **6,260** | **9,309** | **+3,049** | **3,084 (33.1% 总行)** | **42.8%** | **290 / 290 (100%)** | **全部通过** |

---

## 3. 各层级教学注释代表性改动片段（改动前 vs 改动后）

### 3.1 模块级与架构导读注释（`modbusstudio/modbus_engine.py`）

**改动前**：
```python
"""Modbus 通信引擎 (Slave 从机服务 + Poll 主机轮询客户端).

结合 pymodbus 与 modbus_codec，支持全数据类型与 4 种变位模式的读写。
"""
```

**改动后**：
```python
"""
模块职责：Modbus 通信底层核心引擎模块，提供从机模拟服务端与主机轮询客户端的完整协议栈实现。
涵盖四大核心层次：
    1. 报文协议帧构建与拦截解析（RTU CRC16 校验、MBAP 报头封装、收发报文解码日志）。
    2. 多从机路由上下文（MultiStationServerContext）：按 MBAP Unit ID / RTU 站地址将请求精准分发到对应从机实例。
    3. 物理传输层监听服务与单例中枢（ModbusTransportServer + ModbusServerHub）：管理端口复用、健康探活自愈与优雅启停。
    4. 从机模拟器引擎（ModbusSlaveEngine）与主机轮询引擎（ModbusPollEngine）。
关键依赖：
    - asyncio / threading：驱动 pymodbus 异步服务端事件循环与后台波形仿真/轮询守护线程。
    - pymodbus (client / datastore / server / constants)：工业标准 Modbus TCP/RTU 协议栈实现。
    - .modbus_codec：寄存器字序重排、强类型编解码与数值钳位工具。
对外暴露：
    - AreaType：引擎层四大功能区常量。
    - crc16_modbus / build_modbus_rtu_req_hex / build_modbus_tcp_req_hex / build_modbus_tcp_resp_hex / format_modbus_trace_packet：报文工具函数。
    - MultiStationServerContext / ModbusTransportServer / ModbusServerHub：多从机物理端点复用与路由架构。
    - ModbusSlaveEngine：单站点从机模拟器引擎。
    - ModbusPollEngine：主机轮询与写入测试客户端引擎。
"""
# ==============================================================================
# 【核心引擎架构与协议准则导读】
# 1. 字节序铁律：Modbus 寄存器在线路传输时固定为大端序（高字节在前），唯独 RTU 尾部的 CRC16 校验码为小端序（低字节在前）。
# 2. 端口共享模型：同一 TCP (IP:Port) 或 RTU 串口仅启动一个 ModbusTransportServer，通过 MultiStationServerContext 按 Unit ID 路由。
# 3. 单一事实源：从机挂载字典仅保存在 MultiStationServerContext._slaves 中，ModbusTransportServer 直接委托查询，消除双锁死锁隐患。
# 4. 确定性地址偏移：统一通过 engine.address_base (默认 1) 计算数组索引 start_idx = address - base，严禁根据运行时数值猜测偏移。
# 5. 只读存储区防篡改：在 async_setValues 入口严格校验功能码白名单 (5, 6, 15, 16)，拦截对 FC2 (离散输入) 与 FC4 (输入寄存器) 的非法写操作。
# 6. 事件驱动极速启动：通过 threading.Event (_ready) 在 _server_task 创建完成瞬间唤醒主调线程，将单端口启动耗时从 0.9s 盲等降至 ~2ms。
# ...
```

---

### 3.2 类级与多站路由机制注释（`MultiStationServerContext` in `modbusstudio/modbus_engine.py`）

**改动前**：
```python
class MultiStationServerContext(ModbusServerContext):
    """支持多从机路由与实时读写的 Modbus 服务端数据上下文 (完全契合 MBAP Unit ID 路由规范)."""

    def __init__(self):
        dummy_dev = ModbusDeviceContext(co=ModbusSequentialDataBlock(1, [False]))
        super().__init__(devices={0: dummy_dev}, single=False)
        self.old_simulator = True
```

**改动后**：
```python
class MultiStationServerContext(ModbusServerContext):
    """职责：支持同端口多从机（多 Unit ID）动态路由与实时读写的服务端数据上下文。"""
    # 成员变量：
    # - old_simulator (bool): 设为 True，启用 pymodbus 3.x 将读写请求转发给 `async_getValues`/`async_setValues` 的分发路径。
    # - _slaves (Dict[int, ModbusSlaveEngine]): 已挂载的站地址到从机引擎实例的映射表（唯一真实数据源）。
    # - _devices (Dict[int, ModbusDeviceContext]): 兼容 pymodbus 基类结构的设备上下文字典。
    # - _lock (threading.Lock): 保护 `_slaves` 字典并发挂载、卸载与查询的互斥锁。
    # 使用场景：
    # 每个 `ModbusTransportServer` 持有一个 `MultiStationServerContext` 实例。当 EMS 一体机向同一 IP:Port（如 502）
    # 或同一 RS485 串口轮询不同站地址（如 BMS=1, PCS=2）时，由本类根据报文中的 `device_id` 路由到对应从机的数据块。
```

---

### 3.3 函数级与协议字节序注释（`reorder_registers_to_bytes` in `modbusstudio/modbus_codec.py`）

**改动前**：
```python
def reorder_registers_to_bytes(registers: List[int], mode: ByteOrderMode) -> bytes:
    """将 16 位寄存器列表按字节序模式转换为标准 Big-Endian 字节流 (供 struct.unpack 解析)."""
    raw_bytes = bytearray()
    for reg in registers:
        reg_u16 = reg & 0xFFFF
        raw_bytes.append((reg_u16 >> 8) & 0xFF)
        raw_bytes.append(reg_u16 & 0xFF)
```

**改动后**：
```python
def reorder_registers_to_bytes(registers: List[int], mode: ByteOrderMode) -> bytes:
    """
    将从机/主机收到的 16 位寄存器列表，按指定的变位模式（字节序）还原为标准大端序（`ABCD`）字节流，供 `struct.unpack(">...")` 解析。

    参数：
        registers: 16 位无符号整数列表（例如 32 位类型传入 2 个寄存器 `[R0, R1]`，64 位传入 4 个寄存器）。
        mode: 字节序枚举 `ByteOrderMode`（支持 `ABCD` / `CDAB` / `BADC` / `DCBA`）。

    返回：
        bytes: 已经重排为标准大端序（高字节在前）的字节串。

    关键步骤（以 32 位数值 `0x12345678`，即 `A=0x12, B=0x34, C=0x56, D=0x78` 为例）：
        1. 先把每个 16 位寄存器按“高字节在前、低字节在后”拆成平铺字节数组 `b = [b0, b1, b2, b3]`。
        2. 根据设备采用的字节序模式重排回 `[A, B, C, D]`：
           - `ABCD`（大端字序）：传输即为 `[A, B, C, D]`，无需换位。
           - `CDAB`（字交换，EMS/PCS 最常见）：传输为 `[C, D, A, B]`，需将前后两个寄存器（16bit 字）对调：`[b2, b3, b0, b1]`。
           - `BADC`（字节内交换）：传输为 `[B, A, D, C]`，每个寄存器内部高低字节对调：`[b1, b0, b3, b2]`。
           - `DCBA`（纯小端序）：传输为 `[D, C, B, A]`，整体 4 字节完全反转：`[b3, b2, b1, b0]`。
    """
```

---

### 3.4 复杂异步/多线程块行内“为什么”注释（`ModbusTransportServer.start` & `SlavePanel._start_all_slaves`）

**片段 A：`ModbusTransportServer.start()` 事件驱动就绪等待（`modbusstudio/modbus_engine.py`）**
```python
    def start(self) -> None:
        """启动底层物理端点监听线程，并使用事件驱动机制等待 `self._server` 就绪。"""
        # 关键步骤（为什么使用 `self._ready.wait(timeout=1.0)` 替代盲等循环）：
        # - 旧实现使用 `for _ in range(30): time.sleep(0.03)`，若后台线程在极短时间（如 2ms）内完成初始化，
        #   主调线程仍可能白白等待至少一个 `0.03s` 粒度；若端口绑定失败立即抛错，更会盲等满 `30 × 0.03s = 0.9s`！
        # - 改为 `threading.Event` 后，一旦 `_serve()` 创建好 `_server_task`（或异常进入 `except`/`finally`）立即调用 `_ready.set()`，
        #   正常启动仅耗时 1~5ms，异常端口占用也能在几毫秒内立即唤醒返回。
```

**片段 B：`SlavePanel._start_all_slaves()` 并发守卫与主线程回调（`modbusstudio/ui/slave_panel.py`）**
```python
    def _start_all_slaves(self) -> None:
        """响应“全部启动”按钮：在后台守护线程中依次启动所有未运行的从机实例，完成后通过 `root.after(0, _on_done)` 切回主线程刷新 UI。"""
        # 关键设计（为什么需要 `self._busy["start"]` + 后台线程 + `root.after(0, ...)`）：
        # 1. 批量启动多台不同端口或串口的从机涉及底层线程拉起与端口绑定，若在主线程同步循环调用，会导致按钮卡住。
        # 2. `self._busy["start"]` 防止用户在后台启动尚未结束时连续多次点击按钮，避免并发重入产生竞态冲突。
        # 3. Tkinter 所有控件与 `messagebox` 必须在主线程调用，因此后台 `_worker` 结束时通过 `root.after(0, _on_done)` 安全投递回主线程。
```

---

## 4. 全量自动化测试（`pytest`）与记忆校验（`memory/verify.py`）输出

### 4.1 `pytest` 全量运行输出（44 passed, 0 failed）

```text
$ F:\GitHub\python\.venv\Scripts\pytest.exe -q
............................................                             [100%]
44 passed in 22.53s

$ F:\GitHub\python\.venv\Scripts\python.exe -m pytest tests -v
============================= test session starts =============================
platform win32 -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- F:\GitHub\python\.venv\Scripts\python.exe
rootdir: F:\GitHub\python
configfile: pyproject.toml
collected 44 items

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
```

### 4.2 `memory/verify.py` 实测输出

```text
========================================================================
Memorix 共享记忆防失真自动验证报告 (memory/verify.py)
========================================================================
总体结论    : PASS (错误: 0, 警告: 2)
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
  - 10_pytest_execution: PASS (44 passed in 22.53s)
  - 11_needs_human_review: INFO
```

---

## 5. 给学习者的 12 文件分阶段阅读顺序指南

为了帮助新加入项目的工程师由浅入深掌握 Modbus Studio 2.0，建议不要直接扎进 2200 行的 `modbus_engine.py`，而是按以下 **5 个阶段** 循序渐进阅读：

### 第一阶段：先看“骨架与词汇表”（约 30 分钟）
1. **[`modbusstudio/models.py`](file:///F:/GitHub/python/modbusstudio/models.py)**
   - **先理解什么**：项目里有哪些核心概念——四大存储区（`AreaType`：0x 线圈、1x 离散输入、3x 输入寄存器、4x 保持寄存器）、9 种数据类型（`DataType`）、4 种字节序（`ByteOrder`）、5 种仿真波形（`SimRule`），以及一个点位（`ModbusPoint`）和一台从机（`SlaveDevice`）由哪些字段组成。
   - **重点关注**：`normalize()` 是如何把数据库里五花八门的别名（如 `"FLOAT"`、`"REAL"`、`"CDAB"`）统一成标准枚举的。
2. **[`modbusstudio/app.py`](file:///F:/GitHub/python/modbusstudio/app.py)**
   - **先理解什么**：程序从哪里启动、命令行参数怎么解析、無头自检模式（`--self-check`）和图形界面模式怎么分流，`ModbusStudioAppCore` 是怎么把三大服务组装在一起的。

### 第二阶段：再看“协议的数学规则”（约 30 分钟）
3. **[`modbusstudio/modbus_codec.py`](file:///F:/GitHub/python/modbusstudio/modbus_codec.py)**
   - **先理解什么**：为什么一个 16 位 Modbus 寄存器装不下一个 `FLOAT32`？`ABCD` / `CDAB` / `BADC` / `DCBA` 四种字节序到底是怎么把两个 16 位整数拼成一个浮点数的？
   - **动手实验**：对照文件中的注释，在 Python 交互式终端里调用一次 `encode_value(23.5, ModbusDataType.FLOAT32, ByteOrderMode.CDAB)`，观察返回的寄存器列表。

### 第三阶段：攻克“通信与并发核心”（约 60 分钟）
4. **[`modbusstudio/modbus_engine.py`](file:///F:/GitHub/python/modbusstudio/modbus_engine.py)**（全项目最核心文件，建议按内部 5 个区块分段阅读）
   - **第 1 段（报文工具）**：看 `crc16_modbus` 和 `format_modbus_trace_packet`，理解 RTU 帧与 TCP MBAP 帧长什么样。
   - **第 2 段（`MultiStationServerContext`）**：理解“同一个端口怎么挂载多台不同站地址的从机”，以及为什么 `address_base` 能防止数据错位、为什么 `FC2/FC4` 要拦截写入。
   - **第 3 段（`ModbusTransportServer` + `ModbusServerHub`）**：理解后台线程怎么跑 `asyncio` 事件循环，`_ready` 事件怎么让启动瞬间完成，`is_healthy()` 怎么检测僵死端口并自动重建。
   - **第 4 段（`ModbusSlaveEngine`）**：理解单台从机的 4 个 65535 容量数据块，以及 `_run_simulation_loop` 怎么每秒生成正弦波/方波并通过 `_sim_stop_evt.wait(1)` 实现秒停。
   - **第 5 段（`ModbusPollEngine`）**：理解主机怎么轮询、怎么写寄存器、怎么防止双线程并发轮询。

### 第四阶段：理解“业务编排与现场数据库”（约 45 分钟）
5. **[`modbusstudio/services.py`](file:///F:/GitHub/python/modbusstudio/services.py)**
   - **先理解什么**：UI 点一个按钮后，服务层是怎么做配额检查（最多 100 台 TCP / 32 台 RTU）、怎么调用引擎、怎么把后台日志安全送回队列的。
6. **[`modbusstudio/hems_db_loader.py`](file:///F:/GitHub/python/modbusstudio/hems_db_loader.py)** 与 **[`modbusstudio/hems_pairing.py`](file:///F:/GitHub/python/modbusstudio/hems_pairing.py)**
   - **先理解什么**：现场 EMS 一体机的 `.cdb` / `.sqlite` 数据库里有哪些表，怎么从中提取 BMS/PCS 设备点表，以及基于 `More` 字段的配对关系是怎么自动识别的。

### 第五阶段：最后看“界面怎么画、事件怎么绑”（约 45 分钟）
7. **[`modbusstudio/ui/main_window.py`](file:///F:/GitHub/python/modbusstudio/ui/main_window.py)** → **[`modbusstudio/ui/slave_panel.py`](file:///F:/GitHub/python/modbusstudio/ui/slave_panel.py)** → **[`modbusstudio/ui/poll_panel.py`](file:///F:/GitHub/python/modbusstudio/ui/poll_panel.py)** → **[`modbusstudio/ui/dialogs/db_import_dialog.py`](file:///F:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py)** & **[`modbusstudio/ui/dialogs/pairing_dialog.py`](file:///F:/GitHub/python/modbusstudio/ui/dialogs/pairing_dialog.py)**
   - **先理解什么**：Tkinter 布局是怎么搭出来的，为什么耗时操作（如“全部启动”、“连续轮询”）必须放在后台线程，然后用 `after(0, ...)` 或 `after(100, ...)` 回到主线程更新界面。

---

## 6. 给 Claude 的复核指令清单

Claude 在复核本报告时，可直接顺序执行以下 5 步完成全面验收：

1. **抽查核心文件头部与关键注释**：
   ```bash
   head -n 35 modbusstudio/modbus_engine.py
   head -n 35 modbusstudio/services.py
   head -n 35 modbusstudio/ui/slave_panel.py
   ```
2. **执行全量测试验证 0 回归**：
   ```bash
   F:/GitHub/python/.venv/Scripts/python.exe -m pytest tests -q
   ```
3. **执行 Memorix 记忆防失真校验**：
   ```bash
   F:/GitHub/python/.venv/Scripts/python.exe memory/verify.py
   ```
4. **统计 12 个文件的总行数与 `^\s*#` 注释行数**：
   ```bash
   for f in modbusstudio/app.py modbusstudio/models.py modbusstudio/modbus_codec.py \
            modbusstudio/modbus_engine.py modbusstudio/services.py \
            modbusstudio/hems_db_loader.py modbusstudio/hems_pairing.py \
            modbusstudio/ui/main_window.py modbusstudio/ui/slave_panel.py \
            modbusstudio/ui/poll_panel.py \
            modbusstudio/ui/dialogs/db_import_dialog.py \
            modbusstudio/ui/dialogs/pairing_dialog.py; do
       total=$(wc -l < "$f")
       comments=$(grep -cE '^\s*#' "$f")
       echo "$f: total=$total comments=$comments"
   done
   ```
5. **运行 AST 零改动、100% docstring 与术语合规校验脚本**：
   ```bash
   F:/GitHub/python/.venv/Scripts/python.exe C:/Users/123/.gemini/antigravity/brain/cd757a85-b292-4f66-8ca1-8d558cbfb49e/scratch/check_comments.py
   ```
