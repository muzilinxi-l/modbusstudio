# 08 — 数据类型与数据库 `menu` 表对齐 & 比特类型补齐 执行报告

- **任务指令来源**：[`docs/Claude code/08_DATATYPE_MENU_ALIGN_PROMPT.md`](file:///F:/GitHub/python/docs/Claude%20code/08_DATATYPE_MENU_ALIGN_PROMPT.md)
- **执行方**：Antigravity
- **工作区与分支**：`F:\GitHub\python`（分支 `main`）
- **生成时间**：2026-10-13

---

## 一、改动文件清单与逐项说明（含 `file:line`）

| 序号 | 修改文件 | 行号范围 | 改了什么 / 为什么这么改 |
|---|---|---|---|
| 1 | [`modbusstudio/modbus_codec.py`](file:///F:/GitHub/python/modbusstudio/modbus_codec.py#L174-L262) | `L174-262` | **对齐 `HEMS_TYPE_DEFINITIONS` 字典编号为 `0~43`**：将旧版人为加 1 的 `1~44` 键整体下移 `-1` 还原为 `0~43`，严格对齐 `extra/hems.cdb` 的 `menu` 表 `Variable Transfer Format` 全部 44 条枚举（`0=Invalid`、`1=Bit`、`2=Signed char`、`3=Unsigned char`、`4~7=Signed/Unsigned short 1~2`、`8~15=Signed/Unsigned int 1~4`、`16~31=Signed/Unsigned int64 1~8`、`32~35=Float 1~4`、`36=Double 1(双精度浮点数1)`、`37~43=Double 2~8(双精度浮点型2~8)`）。<br>**`parse_hems_type` 拦截 `0=Invalid`**：当传入 `0`、`"0"`、`"Invalid"`、`"无效"` 时直接返回 `None`，交由上层 `map_db_format_to_data_type` 走默认 `INT16` 兜底逻辑，不把无效占位符当作有效类型。 |
| 2 | [`modbusstudio/modbus_codec.py`](file:///F:/GitHub/python/modbusstudio/modbus_codec.py#L151-L155) | `L151-155`, `L490`, `L622` | **双精度浮点 `DOUBLE64` / `FLOAT64` 编解码兼容**：在不改动 `ModbusDataType` 枚举成员定义（保持 `DOUBLE64 = "DOUBLE64"`）的前提下，使 `clamp_value_to_type`、`decode_value`、`encode_value` 同时兼容 `ModbusDataType.DOUBLE64` 与上层领域模型 `DataType.FLOAT64`（`"FLOAT64"`），确保 64 位双精度浮点始终按 4 个连续寄存器（8 字节 IEEE 754 `>d`）编解码。 |
| 3 | [`modbusstudio/ui/slave_panel.py`](file:///F:/GitHub/python/modbusstudio/ui/slave_panel.py#L73-L87) | `L73-87`, `L709`, `L1078`, `L1342` | **从机面板数据类型下拉框补齐“比特”展示与 `DOUBLE64` 对齐**：将 `SlavePanel.DATA_TYPE_CHOICES` 中的 `"BOOL"` 改为 `"BOOL(比特)"`，将 `"FLOAT64"` 对齐为 `"DOUBLE64"`；同时新增静态方法 `SlavePanel.normalize_data_type_choice(choice)`，在单点添加（`_on_click_add_point`）、批量改类型（`_apply_batch_type`）、规则批量生成（`_open_batch_generate_dialog`）三处读取下拉框后统一还原为底层标准枚举值字符串 `"BOOL"` / `"DOUBLE64"`，避免将展示名 `"BOOL(比特)"` 存入点位或配置文件。 |
| 4 | [`modbusstudio/models.py`](file:///F:/GitHub/python/modbusstudio/models.py#L275-L277) | `L275-277` | **领域层 `DataType.normalize` 兼容“比特”**：在布尔匹配分支中补充 `"比特" in s`，使 `"BOOL(比特)"`、`"比特"`、`"Bit"`、`"BOOL"` 均稳定归一化为 `DataType.BOOL`（`.value == "BOOL"`）。 |
| 5 | [`modbusstudio/hems_db_loader.py`](file:///F:/GitHub/python/modbusstudio/hems_db_loader.py#L42-L64) | `L42-64`, `L372-373` | **数据库格式映射与显式数值判定加固**：`map_db_format_to_data_type` 明确判空并补充 `"比特"` 关键字兜底，配合 `parse_hems_type` 实现 `0/"0"/"Invalid" -> INT16`、`1/"1"/"Bit"/"bit"/"Bool"/"bool" -> BOOL`；`extract_studio_points` 中 `is_explicit_numeric` 排除 `"0"` / `"invalid"` / `"无效"`。 |
| 6 | [`tests/test_codec.py`](file:///F:/GitHub/python/tests/test_codec.py#L75-L111) | `L75-111` | **新增 HEMS `0~43` 编号与名称等价性单元测试**：新增 `test_parse_hems_type_menu_alignment`（验证 `0->None` 及 `1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16, 24, 32, 34, 36, 43` 全部关键编号）与 `test_parse_hems_type_id_and_name_equivalence`（遍历验证 `1~43` 的数字 ID、数字字符串、英文名、中文名解析三元组完全一致）。 |
| 7 | [`tests/test_hems_import.py`](file:///F:/GitHub/python/tests/test_hems_import.py#L12-L98) | `L12-98`, `L118`, `L299-332` | **同步更新 HEMS 导入测试并新增 `Bit("1")` 与 UI 下拉框测试**：将旧测试中的 `2/7/9/13/33/35` 对齐更新为 `menu` 表真实编号 `1/6/8/12/32/34`；在 `test_hems_database_loading_memory` 中新增 `Variable Transfer Format = "1"`（Bit）点位验证导入后 `data_type == ModbusDataType.BOOL`；新增 `test_slave_panel_data_type_choices_bool_and_double64` 验证 UI 下拉框展示与底层写回还原。 |

---

## 二、`HEMS_TYPE_DEFINITIONS`（`0~43`）与 `extra/hems.cdb` `menu` 表对照

- `0`: `("Invalid", "无效", ModbusDataType.INT16, ByteOrderMode.ABCD, 1)` → `parse_hems_type(0)` 返回 `None`，由 `map_db_format_to_data_type` 兜底返回 `ModbusDataType.INT16`
- `1`: `("Bit", "比特", ModbusDataType.BOOL, ByteOrderMode.ABCD, 1)`
- `2`: `("Signed char", "有符号字节", ModbusDataType.INT16, ByteOrderMode.ABCD, 1)`
- `3`: `("Unsigned char", "无符号字节", ModbusDataType.UINT16, ByteOrderMode.ABCD, 1)`
- `4~5`: `Signed short 1~2`（`有符号短整型1~2`，`INT16`，`ABCD / BADC`，`1`）
- `6~7`: `Unsigned short 1~2`（`无符号短整型1~2`，`UINT16`，`ABCD / BADC`，`1`）
- `8~11`: `Signed int 1~4`（`有符号整形1~4`，`INT32`，`ABCD / BADC / CDAB / DCBA`，`2`）
- `12~15`: `Unsigned int 1~4`（`无符号整形1~4`，`UINT32`，`ABCD / BADC / CDAB / DCBA`，`2`）
- `16~23`: `Signed int64 1~8`（`有符号长整形1~8`，`INT64`，`ABCD / BADC / CDAB / DCBA` × 2，`4`）
- `24~31`: `Unsigned int64 1~8`（`无符号长整形1~8`，`UINT64`，`ABCD / BADC / CDAB / DCBA` × 2，`4`）
- `32~35`: `Float 1~4`（`浮点数1~4`，`FLOAT32`，`ABCD / BADC / CDAB / DCBA`，`2`）
- `36`: `("Double 1", "双精度浮点数1", ModbusDataType.DOUBLE64, ByteOrderMode.ABCD, 4)`
- `37~43`: `Double 2~8`（`双精度浮点型2~8`，保留 `menu` 表原字 `"型"`，`DOUBLE64`，`BADC / CDAB / DCBA / ABCD / BADC / CDAB / DCBA`，`4`）

---

## 三、测试与门禁验证结果

### 3.1 `pytest` 全量测试输出
```text
$ F:\GitHub\python\.venv\Scripts\pytest.exe -q
.....................................................                    [100%]
53 passed in 18.29s
```

### 3.2 PROMPT 第四节验收标准逐条自核 Checklist

| 序号 | 验收标准 | 核验结果 | 对应证据 |
|---|---|---|---|
| 1 | `HEMS_TYPE_DEFINITIONS` 的数字键为 `0~43`（共 44 项），每项 `(eng, chn)` 与 `extra/hems.cdb` 的 `menu` 表 `Variable Transfer Format` 完全一致 | ✅ 通过 | `modbus_codec.py:181-235`、`test_codec.py:test_parse_hems_type_menu_alignment` |
| 2 | `parse_hems_type("4")` 与 `parse_hems_type("Signed short 1")` 返回同一三元组 `(ModbusDataType.INT16, ByteOrderMode.ABCD, 1)` | ✅ 通过 | `test_codec.py:test_parse_hems_type_id_and_name_equivalence`（`1~43` 全量覆盖） |
| 3 | 数据库若存数字 `"1"` 或 `1`（`Bit`），解析结果为 `ModbusDataType.BOOL`，不再误落 `Invalid -> INT16` | ✅ 通过 | `test_hems_import.py:test_map_db_format_to_data_type` & `test_hems_database_loading_memory` |
| 4 | 各类型对应的 `ByteOrderMode` 与改前完全一致（未改动任何变位值） | ✅ 通过 | `modbus_codec.py:181-235`，仅将数字键整体 `-1`，元组第 4 项 `ByteOrderMode` 完全未变 |
| 5 | `map_db_format_to_data_type` 对 `"Bit"` / `"bit"` / `"Bool"` / `"bool"` 均返回 `ModbusDataType.BOOL` | ✅ 通过 | `hems_db_loader.py:45-62`、`test_hems_import.py:21-25` |
| 6 | 从机面板点表数据类型下拉框能看到 `"BOOL(比特)"` 与 `"DOUBLE64"`，选择后写入 `ModbusPoint.data_type` 的仍是 `"BOOL"` | ✅ 通过 | `slave_panel.py:74-87`、`test_hems_import.py:test_slave_panel_data_type_choices_bool_and_double64` |
| 7 | 新增的 `test_codec.py` 与 `test_hems_import.py` 用例全部通过 | ✅ 通过 | 新增 3 个测试函数，全量 `53 passed` |
| 8 | 全量 `pytest` 通过（无回归），`python memory/verify.py` 通过 | ✅ 通过 | `53 passed in 18.29s`，`memory/verify.py` 输出 `[VERIFY] PASS` |
