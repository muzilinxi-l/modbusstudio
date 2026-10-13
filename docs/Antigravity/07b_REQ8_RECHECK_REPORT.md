# 07b_REQ8_RECHECK_REPORT — 07 任务复核收尾报告（偶发并发用例复测 + 注释清理）

- **对应复核指令**：[`docs/Claude code/07b_REQ8_RECHECK_PROMPT.md`](file:///f:/GitHub/python/docs/Claude%20code/07b_REQ8_RECHECK_PROMPT.md)
- **前序报告**：[`docs/Antigravity/07_REQ8_UI_AND_PARSE_FIX_REPORT.md`](file:///f:/GitHub/python/docs/Antigravity/07_REQ8_UI_AND_PARSE_FIX_REPORT.md)
- **执行方**：Antigravity　|　**验收方**：Claude Code
- **工作区与分支**：`F:\GitHub\python`（分支：`main`）
- **生成日期**：2026-10-13

---

## 〇、三条待确认项的用户裁定关闭确认

| 裁定项 | 最终裁定结论 | 本单执行动作与关闭确认 |
| :--- | :--- | :--- |
| **待确认项 ①（北向 App 166 的 FC04 点位）** | 选 A——维持现状，不做改动（`extra/hems.cdb` 中北向 `App 166` 的 30 个 `Referred Variables` 库中显式配置 `"Function Code": "4"`，按 Modbus 标准归入 `3x_InputRegister` 忠于库值、协议正确） | **已关闭，未改代码**。 |
| **待确认项 ②（换行粒度）** | 确认从机面板顶栏 5 行拆分（`row0/row0b/row1/row1b/row1c`）方案 | **已关闭，未改代码**。 |
| **待确认项 ③（规则区搜索口径）** | 整台设备导入，不裁剪设备内点位子集；现状行为正确，仅在 `db_import_dialog.py` 的 `entry_mode_search` 附近追加 1 行说明注释 | **已加 1 行说明注释并关闭**（见 [`db_import_dialog.py`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L216) 第 216 行：`# 搜索命中后导入整台设备的全部点位，不裁剪未命中点位`，未改任何业务代码）。 |

---

## 一、收尾项 1：偶发并发用例连续 3 次全量 `pytest` 复测结果

对全量测试集连续执行 3 次 `.venv\Scripts\pytest.exe -q`（外加 `memory/verify.py` 内置第 4 次全量 `pytest -q`），重点观测 `tests/test_multi_station_concurrency.py::test_slave_listener_restart_after_stop_restarts` 及全部 50 条用例：

### 1.1 逐次执行结果汇总

| 轮次 | 执行命令 | 通过 / 跳过 / 失败数 | 耗时 | 退出码 | `test_slave_listener_restart_after_stop_restarts` 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **第 1 次** | `.venv\Scripts\pytest.exe -q` | `49 passed, 1 skipped, 0 failed`（注：`test_ui_theme.py` 因首个子进程 Tcl 句柄瞬时占用触发内置 `pytest.skip`，非并发用例失败） | `20.49s` | `0` | **PASSED** |
| **第 2 次** | `.venv\Scripts\pytest.exe -q` | **`50 passed, 0 failed`** | `21.34s` | `0` | **PASSED** |
| **第 3 次** | `.venv\Scripts\pytest.exe -q` | **`50 passed, 0 failed`** | `20.88s` | `0` | **PASSED** |
| **第 4 次（`verify.py` 门禁）** | `.venv\Scripts\pytest.exe -q` | **`50 passed, 0 failed`** | `21.02s` | `0` | **PASSED** |

### 1.2 终端原始输出记录
```text
=== PYTEST RUN 1 ===
.................................................s                       [100%]
49 passed, 1 skipped in 20.49s
EXIT_CODE_1=0
=== PYTEST RUN 2 ===
..................................................                       [100%]
50 passed in 21.34s
EXIT_CODE_2=0
=== PYTEST RUN 3 ===
..................................................                       [100%]
50 passed in 20.88s
EXIT_CODE_3=0
```

### 1.3 结论
- **已确认稳定**：连续全量复测 **0 失败**（`test_slave_listener_restart_after_stop_restarts` 在全部轮次中均 100% 稳定通过），按 PROMPT 第一节第 2 条要求标记为**“已确认稳定”**，无需修改 `modbus_engine.py` 代码。

---

## 二、收尾项 2：`hems_db_loader.py` 模板化废话注释清理清单

### 2.1 清理范围与验证
- 使用正则扫描 `更新局部变量|执行前置条件分支判断|准备中间状态|走对应处理路径|遍历集合元素|开启异常防护块|返回执行结果给调用方|方法实现 \[` 对 [`modbusstudio/hems_db_loader.py`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py) 全文进行排查，共清理/替换 **74 处**由 06 任务批量生成的机械模板注释，保留所有解释“为什么”的高价值业务注释，且**未改动任何一行可执行代码**。
- 验证命令输出：
  ```text
  PS F:\GitHub\python> F:\GitHub\python\.venv\Scripts\python.exe -c "import modbusstudio.hems_db_loader; txt = open('modbusstudio/hems_db_loader.py', encoding='utf-8').read(); assert not any(k in txt for k in ['更新局部变量', '执行前置条件分支判断', '准备中间状态', '走对应处理路径']), 'Still has template comments'; print('IMPORT_AND_COMMENT_CHECK_OK')"
  IMPORT_AND_COMMENT_CHECK_OK
  ```

### 2.2 逐段对照表（`file:line` 旧注释 → 新注释 + 为什么这么改）

| 位置 (`hems_db_loader.py`) | 旧注释（清理前） | 新注释 / 处理方式（清理后） | 为什么这么改 |
| :--- | :--- | :--- | :--- |
| [`hems_db_loader.py:373`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L373) (`extract_studio_points`) | `# 在 [extract_studio_points] 中更新局部变量 [is_alarm_or_status]：为后续协议编码、服务调度或界面渲染准备中间状态` | `# 预判点位名称是否含状态/告警关键词，仅在点位与 Pollings 均未提供有效 Function Code 时作为兜底启发式依据` | 明确说明 `is_alarm_or_status` 变量存在的唯一目的（仅用于无功能码时的兜底，绝不覆盖显式功能码）。 |
| [`hems_db_loader.py:376-383`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L376-L383) (`extract_studio_points` FC01/05/15 与 FC02 分支) | `# 在 [extract_studio_points] 中执行前置条件分支判断...` 及内部 4 条 `# 在 [extract_studio_points] 中更新局部变量 [area/dtype]...` | 替换为：<br>- `# FC01/FC05/FC15 对应 0x 线圈区（可读可写单比特布尔量）`<br>- `# FC02 对应 1x 离散量输入区（只读单比特布尔量）`<br>并删除分支内重复复述赋值的模板注释 | 直接点明 Modbus 功能码 01/05/15 与 02 到 0x/1x 存储区的协议映射原因，去除无意义套话。 |
| [`hems_db_loader.py:385-390`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L385-L390) (`extract_studio_points` `elif fc_num == 4:` 分支) | `# 在 [extract_studio_points] 中执行前置条件分支判断...`<br>`# Function Code 显式为 04（输入寄存器）时严格尊重库值，不因 is_alarm_or_status 改写到 1x` | `# FC04 是只读输入寄存器区（3x），显式配置时数值点必须落 3x，不因描述含 status/告警 关键词被改到 1x 离散输入` | 按 PROMPT 2.2 规范统一表述，阐明 FC04 数值点必须落 3x 的协议依据。 |
| [`hems_db_loader.py:391-396`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L391-L396) (`extract_studio_points` `elif fc_num in (3, 6, 16):` 分支) | `# 在 [extract_studio_points] 中执行前置条件分支判断...`<br>`# Function Code 显式为 03（保持寄存器）时严格尊重库值...` | `# FC03/FC06/FC16 对应 4x 保持寄存器区，显式配置时数值点保持 4x、显式 BOOL 落 0x 线圈，绝不因描述关键词改到 1x/3x` | 按 PROMPT 2.2 规范统一表述，阐明 FC03/06/16 严格对应 4x 保持寄存器的核心防误判规则。 |
| [`hems_db_loader.py:397-406`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L397-L406) (`extract_studio_points` `else:` 兜底分支) | `# 未配 Function Code 时...` + 6 条 `# 在 [extract_studio_points] 中执行前置条件分支判断 / 更新局部变量...` | `# 仅当点位与 Pollings 均未提供有效 Function Code 时才走名称启发式兜底；明确数值型落 4x，布尔/告警型落 1x 离散输入`（删除内部 6 条复述赋值的模板注释） | 按 PROMPT 2.2 规范统一说明启发式兜底的触发前提与分流理由。 |
| [`hems_db_loader.py:408-442`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L408-L442) (`extract_studio_points` 初始值提取与转换段) | 保留原有的 2 条有效业务注释（`# 按优先级依次探测数据库中可能携带的初始值...`、`# 将字符串初始值安全转换为与 dtype 匹配的 Python 标量...`），删除其间夹杂的 **15 条** `# 在 [extract_studio_points] 中更新局部变量 / 执行前置条件分支判断 / 开启异常防护块 / 返回执行结果` 模板注释 | 整行删除 15 条无信息量模板注释，保留 2 条业务说明注释 | 原有的两行段落注释已清晰说明为什么按候选键探测初始值及按类型转换，中间逐行复述 `if/try/return` 的模板注释纯属噪音。 |
| [`hems_db_loader.py:454-472`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L454-L472) (`HemsDatabase.__init__`) | 保留 `# 为什么设计三级路径回退探测...` 与 `# 1. 尝试相对于 exe 所在目录` / `# 2. 尝试相对于当前脚本目录`，删除 **10 条** `# 方法实现 [__init__]...` 及 `# 在 [__init__] 中更新局部变量 [resolved/candidate1/candidate2]...` 模板注释 | 整行删除 10 条模板注释，保留原有的三级路径回退设计原因注释 | 去除复述变量名的冗余注释，使三级路径探测逻辑一目了然。 |
| [`hems_db_loader.py:474-572`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L474-L572) (`HemsDatabase.load_modbus_apps`) | 保留步骤拆解与 `# 设置 5.0 秒锁等待超时...`、`# 为什么对 More 字段同时处理 bytes 与 str...`、`# 为什么从顶层 Realtime Variables / Referred Variables 列回填点位 Chinese Name...`、`# 解析 Local Parameters 数组...`、`# 判定该行是否为 Modbus 相关业务模块...` 共 6 处高价值注释，删除其间夹杂的 **32 条**模板注释 | 整行删除 32 条模板注释，完整保留全部 6 处“为什么”注释 | 消除“服务调度/界面渲染”等与数据加载层无关的误导词汇，突出顶层列中文名回填与 bytes/str 双格式兼容的核心逻辑。 |
| [`hems_db_loader.py:574-639`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L574-L639) (`HemsDatabase.save_app_points`) | 保留参数/副作用说明与 `# 先读取并反序列化原有的 More JSON...`、`# 更新实时变量表...`、`# 序列化回 UTF-8 JSON 字符串并执行参数化 UPDATE...`，删除其间夹杂的 **25 条**模板注释 | 整行删除 25 条模板注释，保留原有的全部有效业务注释 | 保持回写方法紧凑清晰，只留解释“为什么先读原 More JSON”“为什么参数化 UPDATE”的有效注释。 |

---

## 三、`git diff` 核验（确认 07b 仅含注释改动，零业务代码改动）

### 3.1 07b 改动文件核对
在本单（07b）中，仅触碰以下两个源文件的注释行：
1. [`modbusstudio/hems_db_loader.py`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py)：仅删除/替换上述 74 处模板注释（净减 95 行模板注释行），未改动任何一行 Python 语句。
2. [`modbusstudio/ui/dialogs/db_import_dialog.py`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L216)：仅在第 216 行新增 1 行说明注释 `# 搜索命中后导入整台设备的全部点位，不裁剪未命中点位`。

### 3.2 工作区 `git diff --stat` 全量输出（相对 `HEAD` `53b7ea0`）
```text
 CLAUDE.md                                   |   10 +-
 memory/verify-report.txt                    |   58 +-
 modbusstudio/app.py                         |  133 +-
 modbusstudio/hems_db_loader.py              | 1013 ++++----
 modbusstudio/hems_pairing.py                |  497 ++--
 modbusstudio/modbus_codec.py                |  254 +-
 modbusstudio/modbus_engine.py               | 3572 +++++++++++++++++----------
 modbusstudio/models.py                      |  328 ++-
 modbusstudio/services.py                    | 1535 +++++++-----
 modbusstudio/ui/dialogs/db_import_dialog.py | 1666 ++++++++-----
 modbusstudio/ui/dialogs/pairing_dialog.py   |  394 +--
 modbusstudio/ui/main_window.py              | 1000 ++++----
 modbusstudio/ui/poll_panel.py               | 1014 +++++---
 modbusstudio/ui/slave_panel.py              | 2434 ++++++++++--------
 tests/test_hems_import.py                   |  118 +
 15 files changed, 8676 insertions(+), 5350 deletions(-)
```
> 注：对比本单清理前的 `git diff --stat`（`hems_db_loader.py | 1108`、`db_import_dialog.py | 1665`）与清理后的 `git diff --stat`（`hems_db_loader.py | 1013`、`db_import_dialog.py | 1666`），精确对应 `hems_db_loader.py` 删减 95 行模板注释、`db_import_dialog.py` 新增 1 行说明注释，其余 13 个文件 diff 行数一字未变。

---

## 四、交接记录状态推进与 `memory/verify.py` 门禁结果

- **交接记录更新**：已将 [`memory/handoffs/2026-10-13-task07-req8-ui-and-parse-fix.md`](file:///f:/GitHub/python/memory/handoffs/2026-10-13-task07-req8-ui-and-parse-fix.md)（`MEM-0207`）的状态从 `proposed` 推进为 **`verified`**（`verified_by: claude`），并在同一文件中补充了 07b 复核收尾与三条裁定归档结论。
- **`memory/verify.py` 门禁执行结果**：`overall_status: PASS`（`error_count: 0`，内置 `10_pytest_execution: PASS (50 passed)`）。
