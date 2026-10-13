# 07_REQ8_UI_AND_PARSE_FIX_REPORT — 8 项 UI 与数据库解析问题修复执行报告

- **对应任务指令**：[`docs/Claude code/07_REQ8_UI_AND_PARSE_FIX_PROMPT.md`](file:///f:/GitHub/python/docs/Claude%20code/07_REQ8_UI_AND_PARSE_FIX_PROMPT.md)
- **原始需求文档**：[`docs/Claude code/需求.md`](file:///f:/GitHub/python/docs/Claude%20code/%E9%9C%80%E6%B1%82.md)
- **执行方**：Antigravity
- **工作区与分支**：`F:\GitHub\python`（分支：`main`，单工作树环境）
- **生成日期**：2026-10-13

---

## 一、改动文件清单与逐项对照（`file:line` + 改了什么 / 为什么）

| 序号 | 对应需求 | 文件路径与行号 (`file:line`) | 改了什么 | 为什么这么改 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **需求 1**：导入点表后保持寄存器（4x）变输入寄存器（3x） | [`hems_db_loader.py`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L295-L388)（第 295–388 行） | 1. 新增静态方法 [`HemsAppModel._normalize_function_code(raw_fc)`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L295-L317)，统一将 `"03"`、`"3"`、`3`、`"0x03"`、`"FC03"`、`"04"`、`"4"`、`4` 等多格式功能码归一为整数 `1/2/3/4/5/6/15/16`；<br>2. 在 [`extract_studio_points()`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L343-L388) 中，当点位级 `Function Code` 为空时优先从 `self.pollings` 继承功能码；<br>3. **彻底移除旧实现第 323–326 行“只要点名含 `状态/status/告警/alarm` 就强行覆写为 `3x` 输入寄存器”的倒置分支**：只要 `fc_num` 非空，`fc_num in (3, 6, 16)` 严格归入 `4x_HoldingRegister (保持寄存器)`，`fc_num == 4` 严格归入 `3x_InputRegister (输入寄存器)`；仅在点位与 `Pollings` 均未提供 `Function Code` 时才启用名称启发式兜底。 | 旧代码在 `fc_str in ("3", "6", "16")` 分支内嵌套了 `if any(k in desc for k in ["状态", "status", "告警", "alarm"]): area = AreaType.INPUT.friendly_name`，导致南向设备（如 `App 170 BMS`、`App 174 PCS`）中原本配置为 `Function Code = "3"` 的状态/告警保持寄存器被误判为 `3x` 输入寄存器。 |
| 2 | **需求 2**：所有寄存器区域按 `0x → 1x → 3x → 4x` 升序排列 | 1. [`slave_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/slave_panel.py#L66-L71)（第 66–71 行、第 1237–1243 行）<br>2. [`db_import_dialog.py`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L41-L46)（第 41–46 行、第 252–259 行）<br>3. [`poll_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/poll_panel.py#L46-L52)（第 46–52 行） | 1. 将 [`SlavePanel.AREA_CHOICES`](file:///f:/GitHub/python/modbusstudio/ui/slave_panel.py#L66-L71)、[`DbImportDialog.AREA_CHOICES`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L41-L46)、[`PollPanel.AREA_CHOICES`](file:///f:/GitHub/python/modbusstudio/ui/poll_panel.py#L46-L52) 全部统一为 `[AreaType.COIL.friendly_name, AreaType.DISCRETE.friendly_name, AreaType.INPUT.friendly_name, AreaType.HOLDING.friendly_name]`（即 `0x → 1x → 3x → 4x`）；<br>2. 所有下拉框初始默认选中项仍保持 `.set(AreaType.HOLDING.friendly_name)`（`4x`）；<br>3. 在 [`SlavePanel._sort_by_column()`](file:///f:/GitHub/python/modbusstudio/ui/slave_panel.py#L1237-L1243) 中增加对 `"area"` 列的 `AreaType.normalize(t[0]).value` 升序排序支持。 | 旧实现 `AREA_CHOICES` 顺序为 `[4x, 3x, 0x, 1x]`，不符合 Modbus 功能区编号 `0x → 1x → 3x → 4x` 的直观升序习惯。 |
| 3 | **需求 3**：“建立当前服务”与“导入数据库点表”换行独立成行 | [`slave_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/slave_panel.py#L108-L213)（第 108–213 行） | 将从机面板顶部 `top_box` 拆分为 5 个独立 `ttk.Frame(top_box)` 行（均使用 `fill="x"` 纵向堆叠）：<br>- `row0`：当前从机实例下拉框 + `➕ 新建实例` + `🗑 删除`；<br>- `row0b`：`▶ 启动当前服务` + 状态指示灯 + `▶▶ 全部启动` + `⏹⏹ 全部停止` + `串口断开自动重连`；<br>- `row1`：通信方式（TCP/RTU）+ IP/端口或串口五要素 + 从机站号（Unit ID）；<br>- `row1b`：`📂 关联/切换数据库` 按钮；<br>- `row1c`：`📥 导入数据库点表` 按钮 + `🔗 业务配对与转发中心` 按钮 + 当前数据库状态标签。 | 旧实现将实例选择与启停按钮挤在同一行、通信参数与数据库关联/导入按钮挤在同一行，在窄屏或切换为 RTU 展开串口五要素控件后右侧按钮易被挤压截断。 |
| 4 | **需求 4**：导入时有中文名称但未显示中文名称 | 1. [`hems_db_loader.py`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L528-L577)（第 528–577 行）<br>2. [`db_import_dialog.py`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L204-L210)（第 204–210 行、第 472–475 行、第 560–578 行） | 1. 在 [`HemsDatabase.load_modbus_apps()`](file:///f:/GitHub/python/modbusstudio/hems_db_loader.py#L528-L577) 中将 SQL 查询升级为 `SELECT * FROM app;`，读取 `app` 表顶层 `[Realtime Variables]` 与 `[Referred Variables]` 列中的 `"Chinese Name"` 字典，自动回填至 `More["Realtime Variables"]` 与 `More["Referred Variables"]`；<br>2. 在 [`DbImportDialog._filter_tree()`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L472-L475) 中，列表第 3 列（`业务设备名称 (中文)`）严格展示独立中文名，若无中文名或与英文名完全相同则显示 `—（无中文）`；<br>3. 在 `mode_frame` 新增 [`self.lbl_selected_chn_summary`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L204-L210)，随选中行变化实时回显已选设备的中文名汇总。 | 实测 `extra/hems.cdb` 发现：`app.More` JSON 内的变量对象均不含 `"Chinese Name"`，真正的点位中文名（如 `"工作状态"`、`"有功功率设置"`、`"FDDSL600|通信状态"`）存储在 `app` 表顶层同名列 `[Realtime Variables]` / `[Referred Variables]` 中！旧代码仅 `SELECT ... More` 未读取顶层列，导致所有导入点位全部退化显示英文名。 |
| 5 | **需求 5**：“导入模式与规则配置”区域补充搜索功能 | [`db_import_dialog.py`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L190-L203)（第 190–203 行、第 435–525 行） | 在 `mode_frame`（`导入模式与规则配置`）顶部第 1 行新增搜索栏（[`self.mode_search_var`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L194)、[`self.entry_mode_search`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L195) 与 `清空` 按钮），绑定 `trace_add("write", lambda *args: self._filter_tree())`，支持按**设备中文名 / 英文名 / App ID / 点位描述 / 点位地址**大小写不敏感实时过滤，并在搜索前自动记忆已勾选集合、清空搜索词后无损恢复勾选状态。 | 用户反馈“导入模式与规则配置这没有搜索功能”，在规则区内部直接提供支持设备与点位雙维匹配的实时搜索框，同时保留顶部搜索栏联动。 |
| 6 | **需求 6**：RTU 模式下缺少串口接口配置或未传参 | 1. [`db_import_dialog.py`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L211-L269)（第 211–269 行、第 731–804 行）<br>2. [`slave_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/slave_panel.py#L171-L195)（第 171–195 行）<br>3. [`poll_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/poll_panel.py#L115-L136)（第 115–136 行、第 311–328 行） | 1. 在 [`DbImportDialog`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L211-L269) 的 `mode_frame` 中新增 `RTU串口规则` 控件行（含 `combo_slave_com`、`combo_slave_baud`、`combo_slave_databit`、`combo_slave_parity`、`combo_slave_stopbit`），单选设备时自动回显数据库串口参数，并在 [`_import_as_new_instances()`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L731-L804) 中读取控件值注入 `ConnectionConfig`；<br>2. 在 [`PollPanel`](file:///f:/GitHub/python/modbusstudio/ui/poll_panel.py#L115-L136) 的 `rtu_frame` 中补齐数据位、校验位、停止位下拉框并传入 `_get_connection_config()`。 | 确保从机面板、数据库导入弹窗、主机轮询面板三处在 RTU 模式下均具备完整的串口五要素配置能力，且真实传入 `ConnectionConfig`，零硬编码。 |
| 7 | **需求 7**：波特率档位不全 | 1. [`slave_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/slave_panel.py#L76-L80)（第 76–80 行、第 177–181 行）<br>2. [`db_import_dialog.py`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L49-L52)（第 49–52 行、第 223–229 行）<br>3. [`poll_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/poll_panel.py#L54-L58)（第 54–58 行、第 122–124 行） | 在 `SlavePanel`、`DbImportDialog`、`PollPanel` 中统一定义类常量 `BAUDRATE_CHOICES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400"]`，默认选中 `"9600"`。 | 旧实现仅提供 `["9600", "19200", "38400", "57600", "115200"]` 5 个档位，缺少低速（`1200/2400/4800`）与高速（`230400`）常用工业波特率。 |
| 8 | **需求 8**：数据位选项顺序反了（`8, 7` → `7, 8`） | 1. [`slave_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/slave_panel.py#L185-L186)（第 185–186 行）<br>2. [`db_import_dialog.py`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L232-L233)（第 232–233 行）<br>3. [`poll_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/poll_panel.py#L126-L127)（第 126–127 行） | 将所有数据位 Combobox 的 `values` 从 `["8", "7"]` 改为升序 `["7", "8"]`，初始默认值仍保持 `.set("8")`。 | 符合数值从小到大升序规范，同时不改变工业常用默认值 `8`。 |

---

## 二、`extra/hems.cdb` 数据库原始值核查证据（Dump）

使用 `.venv\Scripts\python.exe` 对仓库内 [`extra/hems.cdb`](file:///f:/GitHub/python/extra/hems.cdb) 进行只读 SQL 核查，关键原始数据如下：

### 2.1 `app` 表结构（共 12 列）
```text
(0, 'Id', 'INTEGER')
(1, 'Type', 'INTEGER')
(2, 'English Name', 'TEXT')
(3, 'Chinese Name', 'TEXT')
(4, 'Description', 'TEXT')
(5, 'Controller Library', 'TEXT')
(6, 'Referred Model', 'TEXT')
(7, 'Local Parameters', 'TEXT')
(8, 'Realtime Variables', 'TEXT')
(9, 'Referred Variables', 'TEXT')
(10, 'More', 'TEXT')
(11, 'enable', ' INTEGER')
```

### 2.2 南向与北向 Modbus 设备记录及 `Function Code` / `Chinese Name` 原值分布
| App ID | Type | enable | `[English Name]` | `[Chinese Name]` | `More.Pollings` 中 `Function Code` | `More` 变量内 `Function Code` 原值 | `More` 变量内 `Chinese Name` | 顶层 `[Realtime/Referred Variables]` 列内 `Chinese Name` |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **166** | 2（北向从机） | 1 | `[North]30` | `[北向]30` | `[]` | 30 个点全部为 **`"4"`**（如 `Register Address: "0", Function Code: "4"`） | `None`（缺失） | **有中文名**（如 `'FDDSL600\|通信状态'`、`'FDDFJFH\|通信状态'`） |
| **170** | 1（南向主机） | 0 | `BMS` | `BMS` | `['3', '3', '3']` | 150 个点全部为 **`"3"`** | `None`（缺失） | **有中文名**（如 `'工作状态'`、`'总电压'`、`'SOC'`） |
| **172** | 1（南向主机） | 0 | `功率计` | `功率计` | `['3']` | 29 个点全部为 **`"3"`** | `None`（缺失） | **有中文名**（如 `'通信状态'`、`'A相电压'`） |
| **174** | 1（南向主机） | 0 | `PCS` | `PCS` | `['3', '3', '3']` | 132 个点全部为 **`"3"`**（如 `English Name: "Work status", Function Code: "3"`） | `None`（缺失） | **有中文名**（如 `'工作状态'`、`'有功功率设置'`、`'未分配P'`） |
| **175** | 1（南向主机） | 0 | `外部设备` | `外部设备` | `['3']` | 29 个点全部为 **`"3"`** | `None`（缺失） | **有中文名** |
| **177** | 1（南向主机） | 1 | `FDDSL600` | `FDDSL600` | `['3', '3']` | 48 个点全部为 **`"3"`** | `None`（缺失） | **有中文名** |
| **178** | 1（南向主机） | 1 | `FDDFJFH` | `FDDFJFH` | `['3', '3']` | 48 个点全部为 **`"3"`** | `None`（缺失） | **有中文名** |
| **179~188** | 1（南向主机） | 0 | `1`~`10` | `1`~`10` | `['3']` | 每台 50 个点全部为 **`"3"`** | `None`（缺失） | **有中文名** |

---

## 三、PROMPT 第四节验收 Checklist（15 项）逐条自核结论

- [x] **1. （需求 1）** 从 `extra/hems.cdb` 导入南向设备（如 `App 170 BMS`、`App 174 PCS`、`App 177 FDDSL600`）后，数据库中 `Function Code = "3"` 的保持寄存器点位（含名称带 `"状态"`/`"Work status"`/`"告警"` 的点位）100% 落在 `4x_HoldingRegister (保持寄存器)`，零误判为 `3x`。
- [x] **2. （需求 1）** 数据库中明确标记 `Function Code = 4 / "04"` 的点位（如 `App 166 [北向]30`），导入后正确归入 `3x_InputRegister (输入寄存器)`。
- [x] **3. （需求 2）** 从机面板、导入弹窗、轮询面板中所有寄存器区域下拉框的 `values` 顺序严格为 `["0x_Coil (线圈)", "1x_DiscreteInput (离散输入)", "3x_InputRegister (输入寄存器)", "4x_HoldingRegister (保持寄存器)"]`。
- [x] **4. （需求 2）** 下拉框初始默认选中项仍保持 `4x_HoldingRegister (保持寄存器)`。
- [x] **5. （需求 3）** 从机面板顶栏中，`▶ 启动当前服务`（`row0b`）与 `📂 关联/切换数据库`（`row1b`）、`📥 导入数据库点表`（`row1c`）分别位于各自独立的水平 `ttk.Frame` 行，任意窗口宽度下不挤压、不截断。
- [x] **6. （需求 4）** 打开数据库导入弹窗并加载 `extra/hems.cdb`，有独立中文名的设备显示中文名，中英文重名或缺失中文名的设备在中文名列明确显示 `—（无中文）`；且通过顶层 `[Realtime Variables]` / `[Referred Variables]` 列自动回填后，导入点位的描述 100% 恢复显示真实中文名（如 `"工作状态"`、`"有功功率设置"`）。
- [x] **7. （需求 5）** 导入弹窗的“导入模式与规则配置”框（`mode_frame`）内具备搜索输入框与“清空”按钮，输入关键字可实时过滤（支持设备中英文名、App ID、点位中文描述及寄存器地址），清空后恢复完整列表与原先勾选状态。
- [x] **8. （需求 6）** 在从机面板、导入弹窗规则区、主机轮询面板切换或配置 `RTU` 时，均完整显示“串口、波特率、数据位、校验位、停止位”五项控件。
- [x] **9. （需求 6）** 修改上述五项控件后启动/新建连接，底层 `ConnectionConfig` 收到的 `com_port / baudrate / data_bits / parity / stop_bits` 与 UI 所选完全一致。
- [x] **10. （需求 7）** 所有波特率下拉框的 `values` 完全等于 `["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400"]`，默认 `"9600"`。
- [x] **11. （需求 8）** 所有数据位下拉框的 `values` 完全等于 `["7", "8"]`，默认 `"8"`。
- [x] **12. （回归）** 新增单元测试 [`tests/test_ui_layout_order.py`](file:///f:/GitHub/python/tests/test_ui_layout_order.py) 并扩充 [`tests/test_hems_import.py`](file:///f:/GitHub/python/tests/test_hems_import.py)，覆盖 `AREA_CHOICES` 升序、`BAUDRATE_CHOICES` 9 档、数据位 `["7", "8"]`、`Function Code` 归一化与中文名顶层列回填。
- [x] **13. （回归）** 全量运行 `.venv\Scripts\pytest.exe` 全部通过（**50 passed**，基线 44 + 新增 6，0 失败）。
- [x] **14. （红线）** 未改动 `modbusstudio/archive/`，未回退 01~06 既有修复，未生成任何图片文件。
- [x] **15. （校验）** 运行 `.venv\Scripts\python.exe memory/verify.py` 通过。

---

## 四、待确认项（按 PROMPT 要求如实记录，交由 Claude 复核裁定）

### 4.1 需求 1：`extra/hems.cdb` 中北向设备 `App 166 ([北向]30)` 的 `Function Code` 原值情况
- **实测事实**：
  1. 在 `extra/hems.cdb` 中，**所有南向采集设备**（`Type=1`，包括 `App 170 BMS`、`App 172 功率计`、`App 174 PCS`、`App 175 外部设备`、`App 177 FDDSL600`、`App 178 FDDFJFH`、`App 179~188`）的 `More["Realtime Variables"]` 中，点位的 `Function Code` 原值**全部为 `"3"`**。旧代码因为把含 `"状态/status/告警/alarm"` 关键字的点位强行改判为 `3x`，才导致大量保持寄存器变成了输入寄存器。修复后，这些南向设备的点位已 100% 归入 `4x_HoldingRegister (保持寄存器)`。
  2. 但对于**北向转发设备 `App 166 ([North]30 / [北向]30, Type=2)`**，其 `More["Referred Variables"]` 中的 30 个点位在 SQLite 数据库里存储的原始 JSON 值本身就是 **`"Function Code": "4"`**（例如 `{"English Name": "FDDSL600|Communication Status", "Function Code": "4", "Register Address": "0"}`）。
- **当前处理与待确认点**：
  - 按照 PROMPT 第 3.1 节“数据库 `Function Code` 字段拥有最高优先级，当 `fc_str in ("4", "04")` 时归入 `AreaType.INPUT`（3x）”，当前代码对 `App 166` 严格忠于数据库原值 `"4"` 归入 `3x_InputRegister`。
  - **请 Claude 与用户确认**：用户在截图中看到的“保持寄存器变输入寄存器”，是指南向设备（如 BMS/PCS/FDDSL600 的 `"工作状态"` 等 `FC=3` 点位被旧代码关键字误判为 `3x`，现已修复），还是希望北向 `Type=2`（`App 166`）即使数据库写了 `"Function Code": "4"` 也支持在导入时通过规则区“缺省功能区”一键强制转为 `4x` 保持寄存器？

### 4.2 需求 3：“建立当前服务”和“导入数据库点表”换行的具体布局解读
- **原始表述**：`“建立当前服务和导入数据库点表换行”`
- **两种可能解读与当前实现**：
  - **解读 A**：将原本与“当前从机实例下拉框”挤在同一行的 `▶ 启动当前服务`（即用户口中的“建立当前服务”）换到下一行 `row0b`，并将原本与“TCP/RTU 通信参数 + 关联数据库”挤在同一行的 `📥 导入数据库点表` 换到独立的新行 `row1c`。
  - **解读 B**：不仅 `▶ 启动当前服务` 与 `📥 导入数据库点表` 各自独立成行，且 `📂 关联/切换数据库`（`row1b`）与 `📥 导入数据库点表`（`row1c`）也上下分行排列。
  - **当前实现**：已在 [`slave_panel.py`](file:///f:/GitHub/python/modbusstudio/ui/slave_panel.py#L108-L213) 中将顶栏拆分为 `row0`（实例选择）、`row0b`（启动当前服务/全部启动/全部停止/自动重连）、`row1`（通信方式与参数）、`row1b`（关联/切换数据库）、`row1c`（导入数据库点表 + 业务配对中心）共 5 行，同时满足解读 A 与解读 B。若希望将 `row1b` 与 `row1c` 合并为同一行（仅与 `row1` 通信参数换行），请告知。

### 4.3 需求 5：“导入模式与规则配置”区域搜索框的过滤口径
- **原始表述**：`“导入模式与规则配置这没有搜索功能”`
- **当前实现的兼容口径**：
  - 在 [`db_import_dialog.py`](file:///f:/GitHub/python/modbusstudio/ui/dialogs/db_import_dialog.py#L190-L203) 的 `mode_frame`（`导入模式与规则配置`）内部第 1 行新增了 `🔍 规则区搜索 (设备中英文/AppID/点位名):` 输入框（`self.entry_mode_search`）与 `清空` 按钮。
  - 过滤口径**同时支持设备级与点位级匹配**：输入关键字时，既匹配设备的 `Chinese Name / English Name / App ID`，也匹配设备下属点位的中文描述（`desc`）与寄存器地址（`address`）；且在开始搜索前自动保存用户已勾选的设备列表，点击“清空”后自动恢复原勾选状态。
  - **请 Claude 与用户确认**：该规则区搜索框是用于像现在这样按“设备名或包含的点位名”筛选待导入设备列表，还是需要在点击导入时**仅导入某台设备内部命中该搜索词的子集点位**（排除同设备下未命中搜索词的其他点位）？目前为保证点表完整性，命中点位关键字时保留整台设备勾选，未裁剪设备内部点位。

---

## 五、测试与校验输出

### 5.1 `.venv\Scripts\pytest.exe -v` 全量测试结果
- **总计**：`50 passed`（原有 44 项 + 本次新增 6 项，0 失败）
- **新增测试明细**：
  1. `tests/test_hems_import.py::test_function_code_strict_priority_over_status_keywords` — PASSED
  2. `tests/test_hems_import.py::test_top_level_chinese_name_backfill` — PASSED
  3. `tests/test_ui_layout_order.py::test_area_choices_ascending_order` — PASSED
  4. `tests/test_ui_layout_order.py::test_baudrate_choices_complete_nine_steps` — PASSED
  5. `tests/test_ui_layout_order.py::test_databit_order_and_top_box_multiline_rows_in_slave_panel` — PASSED
  6. `tests/test_ui_layout_order.py::test_db_import_dialog_has_rule_search_and_rtu_controls` — PASSED
