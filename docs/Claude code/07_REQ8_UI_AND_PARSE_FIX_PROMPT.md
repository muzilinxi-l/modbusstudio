# PROMPT：Modbus Studio 2.0 八项 UI 与解析修复（`docs/Claude code/需求.md`）

> 用途：发给 **Antigravity** 执行。
> 执行完成后在 `docs/Antigravity/07_REQ8_UI_AND_PARSE_FIX_REPORT.md` 出报告，Claude 复核。
> 日期：2026-10-12　|　执行方：Antigravity　|　验收方：Claude Code
> 约束：遵循 `CLAUDE.md` 输出能力限制 —— 全程仅文本处理，不生成/输出任何图片、截图、图像文件；报告用文字描述 UI 变化即可。

---

## 〇、本轮用户已拍板的 8 项需求（照 `需求.md` 原文逐条实现，不允许遗漏或自行合并/砍需求）

1. **功能码解析错**：数据库里点位的 `Function Code` 是 `03`（保持寄存器），程序解析后却落成 `04`（输入寄存器）区域。
2. **全局数值升序排序**：所有显示数值（下拉框/列表/表格）的地方都按**数值从小到大**排列，例如点位规则生成向导里的"存储区域"下拉框应按 `01 02 03 04`（即 0x → 1x → 3x → 4x）顺序显示。
3. **界面换行**：给 `slave_panel.py` 里"启动/重启服务 + 自动重连"那一行换行（拆成独立行），其下的行依次下移换行；"关联数据库"和"导入设备点表"这部分也换行；已有的下方各行保持各自成行（不挤一行）。
4. **批量操作与规则配置加中文名列**：`db_import_dialog.py` 的"导入模式与规则配置"区域目前只有英文标识（Name 列），要把数据库里的**中文设备名称（Chinese Name）也解析出来并显示在该区域**（当前设备表格已有"业务设备名称 (中文)"列，需确认取值来源正确并补齐规则配置联动显示）。
5. **批量操作与规则配置加搜索**：在 `db_import_dialog.py` 的"导入模式与规则配置"相关区域新增一个**搜索框**，可按关键字实时过滤该区域内容。
6. **主机串口补齐标准串口参数**：`db_import_dialog.py` 主机/从机新建实例处的 RTU 串口配置要补上**停止位、校验位**等标准串口要素（当前只有 串口 + 波特率 两项，`ConnectionConfig` 已有 `data_bits/parity/stop_bits` 字段，UI 没暴露）。
7. **主机区域选择框升序**：`db_import_dialog.py` 主机的"存储区域"下拉框选项也按数值从小到大排序（与需求 2 同规则）。
8. **波特率补全常用值**：主机/从机的 RTU 波特率下拉框要包含**全部常用波特率**，至少含 `4800`，建议序列：`1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400`。

---

## 一、Claude 已核验的代码事实（执行前 Antigravity 须复核，不得直接假设）

### 1.1 需求 1：功能码 03 → 04 解析错（**重点**）

`modbusstudio/hems_db_loader.py` `extract_studio_points()`（约 273~305 行）现有分支逻辑：

```python
fc_str = str(p.get("Function Code") or "").strip()
is_explicit_numeric = bool(fmt_str and dtype != ModbusDataType.BOOL)
is_alarm_or_status = any(k in desc.lower() or k in eng_name.lower() for k in (...))

if fc_str in ("1", "01"):  area = COIL; dtype = BOOL
elif fc_str in ("2", "02"): area = DISCRETE_INPUT; dtype = BOOL
elif fc_str in ("4", "04"):
    if is_explicit_numeric: area = INPUT_REGISTER
    elif dtype == BOOL or is_alarm_or_status: area = DISCRETE_INPUT; dtype = BOOL
    else: area = INPUT_REGISTER
elif fc_str in ("3", "03"):
    if is_explicit_numeric: area = HOLDING_REGISTER
    elif dtype == BOOL: area = COIL
    else: area = HOLDING_REGISTER
else:  # 未配 Function Code
    if is_explicit_numeric: area = HOLDING_REGISTER
    elif dtype == BOOL or is_alarm_or_status: area = DISCRETE_INPUT; dtype = BOOL
    else: area = HOLDING_REGISTER
```

**可疑点（逐项核验）**：
- **疑点 A（最可能）**：数据库 `Function Code` 字段的实际取值形态未知 —— 可能是 `"0x03"`、`"FC03"`、`"3"` 等带前缀形态，`strip()` 后不在 `("3", "03")` 集合里，掉进 `else` 兜底分支；再叠加 `map_db_format_to_data_type` 把该点位解析成 BOOL / 告警类描述，最终落到 `DISCRETE_INPUT` 或数值被改走 3x 逻辑。用户感知就是"库里是 03，结果变成了 04 区"。**必须先 dump 一条真实库记录的 `Function Code` 原始值再改**，不要按猜的写。
- **疑点 B**：`map_db_format_to_data_type` / `map_db_format_to_byte_order`（同文件）的格式映射表是否会把某种格式强制成 BOOL，导致 `is_explicit_numeric=False`。
- **疑点 C**：`is_alarm_or_status` 关键词匹配（"status"/"状态"等）范围过宽，把 03 区正常数值点误判为告警位而改走 1x。

**修法要求**：
1. 先用 `.venv\Scripts\python.exe -c` 写个一次性脚本（用完即删）打印目标库 3~5 条点位的 `Function Code`、`Variable Transfer Format`、`Chinese Name` 原值，贴进报告。
2. 解析分支的匹配集合要兼容 `"03"/"3"/"0x03"/"FC03"` 全部形态（统一归一化为 int 再判断），并且 **`Function Code` 字段存在且非空时，一律严格尊重库值，不得被 `is_explicit_numeric`/`is_alarm_or_status` 二次改写区域**（数值点保持 4x，布尔点 03 区保持 0x 线圈，但**不得**因描述关键词改到 1x/3x）；只有 `Function Code` 缺失时才走现有兜底逻辑。
3. 补一个回归测试：构造 `Function Code="03"` 且格式为 FLOAT32/INT16 的点位，断言导入后 `area == HOLDING`；再构造 `Function Code="04"` 的点位，断言落 `INPUT`。

### 1.2 需求 2 / 7：区域与数值下拉升序

`modbusstudio/ui/slave_panel.py` `SlavePanel.AREA_CHOICES`（约 45~49 行）当前顺序是：
```python
AREA_CHOICES = [
    AreaType.HOLDING.friendly_name,   # 保持寄存器 (4x)
    AreaType.INPUT.friendly_name,     # 输入寄存器 (3x)
    AreaType.COIL.friendly_name,      # 线圈 (0x)
    AreaType.DISCRETE.friendly_name,  # 离散输入 (1x)
]
```
顺序是 4x→3x→0x→1x，**不是**数值升序。用户要求按数值 `01 02 03 04` 显示 —— 即 **0x_Coil → 1x_Discrete → 3x_Input → 4x_Holding**。

**排序约定（写死进 prompt，防止实现者理解偏差）**：
- 排序依据 = 传统 Modbus 4 区编号：0x→0，1x→1，3x→2，4x→3（按前缀数字），升序：`线圈 (0x) → 离散输入 (1x) → 输入寄存器 (3x) → 保持寄存器 (4x)`。
- 修改点：`SlavePanel.AREA_CHOICES`（slave_panel.py:45）、点位编辑行 `combo_pt_area`（约 203 行，用 `AREA_CHOICES` 即自动跟着改）、批量规则向导 `c_area`（约 875 行，同用 `AREA_CHOICES`）、`db_import_dialog.py` 内如另有独立区域下拉/表格区域列排序，一并改。
- 若 `db_import_dialog.py` 区域列在 Treeview 里按 area 前缀字符串排序，要确认同样满足 0x→1x→3x→4x（字符串序 "0x"<"1x"<"3x"<"4x" 恰好符合，无需特殊处理，但要在报告里确认过）。
- 默认选中项：若原来是默认 HOLDING，改完顺序后默认选中项保持"保持寄存器 (4x)"还是改为列表第一项？——**保持现有默认值不变**（默认仍是 HOLDING），只改选项排列，不改默认。
- **其他数值下拉同规则排查**：`DATA_TYPE_CHOICES`、`BYTE_ORDER_CHOICES`、`SIM_RULE_CHOICES`、波特率下拉（见需求 8）、数据位下拉等，凡是"数值型"选项都要升序（ABCD/CDAB 等无自然大小的保持原样即可，报告里列出你检查过的每处及结论）。

### 1.3 需求 3：slave_panel 换行

`modbusstudio/ui/slave_panel.py` 现状（约 80~170 行）：
- **第 0 行** `row0`：左侧"从机服务实例 + 新建/删除服务"，右侧 `right0` 挤了 4 个按钮：`▶️ 启动服务`、状态标签、`⚡ 全部启动`、`⏹ 全部停止`、`🔄 自动重连`。
- **第 1 行** `row1`：通讯方式 TCP/RTU 单选 + IP/端口或串口五元组。

用户要求："启动重启服务 自动重连服务这一行进行换行，下面的进行依次换行，关联数据库和导入设备点表这部分也进行换行，也按照之前的方式进行之前的下列保持换行。"

**Claude 解读（实现时如认为与 UI 现状冲突，停下报 Claude，不要自行改方向）**：
1. 把 `right0` 里的服务控制按钮组（启动服务 / 全部启动 / 全部停止 / 自动重连 + 状态标签）从 `row0` **拆出为独立一行**（如 `row0b`），位于实例切换行下方；原"实例 + 新建/删除"独占一行。
2. 通讯方式行（TCP/RTU + 连接参数）保持独立一行。
3. "关联数据库"按钮行、"导入设备点表"按钮行（找到它们在 `slave_panel.py` 或 `main_window.py` 中的实际位置，各自独占一行，按当前 pack/grid 节奏依次下移，互不挤占。
4. 已有下方各行（点位编辑、点表表格等）**保持各自独立成行**（"之前的下列保持换行" = 现状已换行的行不要再挤回去）。
5. 换行只调布局（pack/grid 到新的 Frame 行），**不改任何按钮回调、文案、样式、事件绑定**。

### 1.4 需求 4：批量操作与规则配置显示中文名称

`modbusstudio/ui/dialogs/db_import_dialog.py` 设备表格 `col_specs`（约 158~166 行）当前 7 列：`id / enable / name(业务设备名称·中文) / eng_name(英文标识·Name) / role / comm / pt_cnt` —— 中文列其实已有。用户反馈"中文名没解析到这里面"，**Claude 核验出的真实缺口**：

- 表格 `name` 列的值来源需核实：目前填充数据时（`_filter_tree` / 初始填充逻辑）`name` 取的是 `app` 记录的哪个字段？若数据库该设备 `Chinese Name` 为空而回退取了 `Name`（英文），用户看到的就是"中文名其实是英文"——**这才是"没解析出来"的体感来源**。
- 要求：表格"业务设备名称 (中文)"列严格取数据库 `Chinese Name` 字段；**该字段为空或等于英文名时，显示为 `—（无中文）` 或留空并置灰**，不得静默回退英文名冒充中文名；`eng_name` 列保持英文。
- "导入模式与规则配置"（`mode_frame`，约 181 行）区域若是操作**当前选中设备列表**的规则区，要求在区域内加一行"**已选设备中文名汇总**"只读标签，实时显示当前勾选设备的中文名（逗号分隔，未选显示"未选择设备"），随 `_on_tree_selection_changed` 联动。

### 1.5 需求 5：批量操作与规则配置加搜索

现状：`db_import_dialog.py` 顶部**设备列表**区已有"🔍 快速搜索"（`search_var`，约 121~125 行，过滤整个设备表格）。用户要的是给**"批量操作与规则配置"区域本身**加搜索（需求 5 原文"批量操作与规则配置里面新家一个搜索功能"）。

**实现口径（避免做重顶部已有搜索）**：
- 在 `mode_frame`（导入模式与规则配置 LabelFrame）内部顶部加一个搜索 Entry：当勾选了多台设备时，搜索关键字用于**过滤"已选设备清单/规则应用目标"显示**（即需求 4 的中文名汇总列表可搜索过滤）；若实现者判断该区域并无独立可搜索的数据实体，则把搜索框做成"**按已选设备中文名过滤勾选**"（输入即取消不匹配设备的勾选并同步 `lbl_selected_status` 计数），并在报告里写明你采用的口径与理由。
- 搜索实时（`trace_add`），支持清空恢复，`_filter_tree` / 全选/反选逻辑与搜索框兼容（先按搜索缩范围，再操作可见项）。

### 1.6 需求 6：主机 RTU 串口标准参数

现状：`db_import_dialog.py` 第 150~151 行（批量新建独立从机实例时的 RTU 参数区）只有 `combo_slave_com`（串口）+ `combo_slave_baud`（波特率）两项；而 `ConnectionConfig`（`models.py`）已具备 `data_bits / parity / stop_bits` 字段，`slave_panel.py` 的 RTU 区已有完整五要素 UI 可参照。

**要求**：
- 在 `db_import_dialog.py` 该 RTU 区按 slave_panel 同款控件补齐 **数据位（8/7）、校验（None(N)/Even(E)/Odd(O)）、停止位（1/2）** 三个下拉，默认 8N1。
- 导入落库时（`_execute_import` 内构造 `ConnectionConfig` 的地方，约 681 行 `baudrate=9600` 附近）把三项写进去，**不要硬编码默认值**；
- 波特率下拉同步按需求 8 补全。
- 注意：`models.py` 的 `ConnectionConfig.from_dict` 已能解析三个字段，无需改模型；但若 `db_import_dialog` 里目前根本没构造这三个字段，要确认默认 8N1 行为与现网兼容。

### 1.7 需求 8：波特率全量常用值

现状 `db_import_dialog.py:150` 与 `slave_panel.py:150` 波特率下拉均为 `["9600", "19200", "38400", "57600", "115200"]`。

**要求**：两处统一改为升序全量序列 `["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400"]`。默认值保持 `9600` 不变。若两处希望避免重复维护，可抽一个 `SERIAL_BAUD_RATES` 常量放 `models.py`（或 `services.py`）供两处 import —— 允许，但最小改动优先，直接改两处字面量也可以，报告里注明选择。

---

## 二、执行范围与文件清单（按此顺序实施，每改完一个文件跑一次导入验证）

| 顺序 | 文件 | 涉及需求 | 改动要点 |
|---|---|---|---|
| 1 | `modbusstudio/hems_db_loader.py` | 需求 1 | 功能码归一化解析 + 严格尊重库值 + dump 原值佐证 |
| 2 | `modbusstudio/ui/slave_panel.py` | 需求 2、3、7、8 | `AREA_CHOICES` 升序、行布局换行、波特率下拉补全 |
| 3 | `modbusstudio/ui/dialogs/db_import_dialog.py` | 需求 4、5、6、7、8 | 中文名列严格取值 + 已选中文名汇总、规则区搜索框、RTU 三参数补齐、区域下拉升序、波特率补全 |
| 4 | `modbusstudio/models.py`（可选） | 需求 8 | 若抽公共波特率常量才动，否则不动 |
| 5 | `tests/test_hems_import.py` | 需求 1 | 补 03/04 功能码回归断言 |
| 6 | `tests/test_ui_theme.py` 或新建 `tests/test_ui_layout_order.py` | 需求 2、3、7 | 可测部分：AREA_CHOICES 顺序断言、控件 values 升序断言（Tkinter 布局换行不强求 e2e，代码审读确认即可） |

**每文件改完验证**：`.venv\Scripts\python.exe -c "import modbusstudio.<模块>"` 不报错。

---

## 三、红线（与 06 号 prompt 一致 + 本任务专属）

1. **不改业务语义**：除上述 8 项需求涉及的逻辑外，不顺手重构其他代码、不改函数签名/返回值。
2. **不动 `archive/`** 历史快照、`*.legacy.py`、`memory/verify.py` 的 11 项检查逻辑。
3. **不删已有注释**（`[SLAVE-RESTART]`、`[SLAVE-RECONNECT]`、`[SLAVE-SPEED-4]`、`[POLL-CONFIG]` 等标记保留）。
4. **不生成图片**：任何验证一律用 pytest / `python -c` / 打印原值文本完成，报告纯文字（遵守 CLAUDE.md 输出能力限制）。
5. **合并门禁**：完成后跑 `.venv\Scripts\pytest.exe` 全量，必须全绿；有失败逐条列在报告里，不许静默 skip。
6. 每处改动在报告里给 `file:line` 定位 + 一句话"改了什么/为什么"，方便 Claude 复核。

---

## 四、验收标准（Claude 复核 checklist）

- [ ] 需求 1：报告附 dump 出的真实 `Function Code` 原值；03 区数值点导入后 `area==HOLDING`、04 区点 `area==INPUT`，回归测试通过；描述含"status/状态"不再把 03 数值点误挪 1x。
- [ ] 需求 2/7：`AREA_CHOICES` 及所有区域下拉实际顺序 = 0x→1x→3x→4x，默认选中仍为"保持寄存器 (4x)"；报告列出全部数值型下拉的排序核查结论。
- [ ] 需求 3：slave_panel 服务控制按钮组独占一行，实例行、通讯行、关联数据库行、导入点表行依次独立成行；按钮回调/文案未变。
- [ ] 需求 4：表格中文列严格取 `Chinese Name`，无值时不静默回退英文；规则区有已选设备中文名联动显示。
- [ ] 需求 5：规则区搜索框可用、实时、可清空，与全选/反选兼容；报告写明采用的搜索口径。
- [ ] 需求 6：db_import_dialog 的 RTU 区含 数据位/校验/停止位 三控件且导入时落库（非硬编码默认）。
- [ ] 需求 8：两处波特率下拉均含 4800 且升序；默认 9600。
- [ ] pytest 全量通过；`memory/verify.py` 若可用则跑一次无 needs_human_review 新增项。
- [ ] 无图片产物、无 archive 变更、无注释删除。
