# PROMPT：数据类型编号与 ENERGY 数据库 menu.value 对齐（08）

> 用途：发给 **Antigravity** 执行。
> 执行完成后在 `docs/Antigravity/08_DATATYPE_MENU_ALIGN_REPORT.md` 出报告，Claude 复核。
> 日期：2026-10-13　|　执行方：Antigravity　|　验收方：Claude Code
> 约束：遵循 CLAUDE.md/GEMINI.md「输出能力限制（仅文本）」与「进程保护（不杀运行中的 ModbusStudio 进程）」——全程文本，不生成图片，不结束用户正在运行的软件进程。

---

## 〇、问题根因（Claude 已核验，Antigravity 接手先复核）

用户诉求："数据类型要和我数据库一一对应，加上 bit 类型。"

Claude 已读 `extra/hems.cdb` 的 `menu` 表（权威定义）与 `modbusstudio/modbus_codec.py` 的 `HEMS_TYPE_DEFINITIONS`（程序映射表），确认**程序表的数字 ID 段与 `menu.value` 系统性错位**：

- `menu` 表 `Variable Transfer Format` 的真实编号：**Bit=1、Signed char=2、Unsigned char=3、Signed short 1=4 …**（无 0；`Invalid` 单列为 0）。
- 程序 `HEMS_TYPE_DEFINITIONS` 现状：**1=Invalid、2=Bit、3=Signed char、4=Unsigned char、5=Signed short 1 …**，整体比 menu **偏移 +1**，且缺 `0=Invalid`。
- **后果**：只要库里 `Variable Transfer Format` 存的是**数字（按 menu.value，Bit=1 / Signed short 1=4 / Signed int 1=8 / Float 1=32 / Double 8=43）**，程序查表就全部错位：
  - 库值 `1`（Bit 比特）→ 命中 `1=Invalid` → 兜底 INT16，**bit 永远解析不出来**（用户反馈的"没有 bit 类型"）；
  - 库值 `4`（Signed short 1, int16）→ 命中 `4=Unsigned char` → 错成 UINT16；
  - 库值 `8`（Signed int 1, int32）→ 命中 `8=Unsigned short 2` → 错成 16 位。
- 当前 `extra/hems.cdb` 的 `app.More` 里 VTF 存的是**英文名**（"Signed short 1" 等），走 `parse_hems_type` 英文名分支还能对上，所以**测试库未暴露此 bug**；但**现场真实 `.db` 一旦存数字 ID，就全错**。

**用户已拍板：按 menu.value 数字对齐（"按照数字来，最准确"）。** 这是本轮唯一目标，不再讨论其它方案。

---

## 一、权威基准：`menu` 表 `Variable Transfer Format` 完整编号（照此重写，不得擅改）

Claude 已从 `extra/hems.cdb` 的 `menu` 表 dump 出全部 `menu_name = 'Variable Transfer Format'` 的记录。**这是 ENERGY 数据库类型的唯一权威定义，重写 `HEMS_TYPE_DEFINITIONS` 的数字键必须与下表逐项一致**：

| menu.value | 英文名 (eng_name) | 中文名 (chn_name) | 应映射的 ModbusDataType | 占寄存器数 |
|:---:|---|---|---|:---:|
| 0 | Invalid | 无效 | （无效，兜底默认 INT16 或直接丢弃，报告说明选择） | 1 |
| 1 | **Bit** | 比特 | **BOOL** | 1 |
| 2 | Signed char | 有符号字节 | INT16 | 1 |
| 3 | Unsigned char | 无符号字节 | UINT16 | 1 |
| 4 | Signed short 1 | 有符号短整型1 | INT16 | 1 |
| 5 | Signed short 2 | 有符号短整型2 | INT16 | 1 |
| 6 | Unsigned short 1 | 无符号短整型1 | UINT16 | 1 |
| 7 | Unsigned short 2 | 无符号短整型2 | UINT16 | 1 |
| 8 | Signed int 1 | 有符号整形1 | INT32 | 2 |
| 9 | Signed int 2 | 有符号整形2 | INT32 | 2 |
| 10 | Signed int 3 | 有符号整形3 | INT32 | 2 |
| 11 | Signed int 4 | 有符号整形4 | INT32 | 2 |
| 12 | Unsigned int 1 | 无符号整形1 | UINT32 | 2 |
| 13 | Unsigned int 2 | 无符号整形2 | UINT32 | 2 |
| 14 | Unsigned int 3 | 无符号整形3 | UINT32 | 2 |
| 15 | Unsigned int 4 | 无符号整形4 | UINT32 | 2 |
| 16 | Signed int64 1 | 有符号长整形1 | INT64 | 4 |
| 17 | Signed int64 2 | 有符号长整形2 | INT64 | 4 |
| 18 | Signed int64 3 | 有符号长整形3 | INT64 | 4 |
| 19 | Signed int64 4 | 有符号长整形4 | INT64 | 4 |
| 20 | Signed int64 5 | 有符号长整形5 | INT64 | 4 |
| 21 | Signed int64 6 | 有符号长整形6 | INT64 | 4 |
| 22 | Signed int64 7 | 有符号长整形7 | INT64 | 4 |
| 23 | Signed int64 8 | 有符号长整形8 | INT64 | 4 |
| 24 | Unsigned int64 1 | 无符号长整形1 | UINT64 | 4 |
| 25 | Unsigned int64 2 | 无符号长整形2 | UINT64 | 4 |
| 26 | Unsigned int64 3 | 无符号长整形3 | UINT64 | 4 |
| 27 | Unsigned int64 4 | 无符号长整形4 | UINT64 | 4 |
| 28 | Unsigned int64 5 | 无符号长整形5 | UINT64 | 4 |
| 29 | Unsigned int64 6 | 无符号长整形6 | UINT64 | 4 |
| 30 | Unsigned int64 7 | 无符号长整形7 | UINT64 | 4 |
| 31 | Unsigned int64 8 | 无符号长整形8 | UINT64 | 4 |
| 32 | Float 1 | 浮点数1 | FLOAT32 | 2 |
| 33 | Float 2 | 浮点数2 | FLOAT32 | 2 |
| 34 | Float 3 | 浮点数3 | FLOAT32 | 2 |
| 35 | Float 4 | 浮点数4 | FLOAT32 | 2 |
| 36 | Double 1 | 双精度浮点数1 | DOUBLE64 | 4 |
| 37 | Double 2 | 双精度浮点型2 | DOUBLE64 | 4 |
| 38 | Double 3 | 双精度浮点型3 | DOUBLE64 | 4 |
| 39 | Double 4 | 双精度浮点型4 | DOUBLE64 | 4 |
| 40 | Double 5 | 双精度浮点型5 | DOUBLE64 | 4 |
| 41 | Double 6 | 双精度浮点型6 | DOUBLE64 | 4 |
| 42 | Double 7 | 双精度浮点型7 | DOUBLE64 | 4 |
| 43 | Double 8 | 双精度浮点型8 | DOUBLE64 | 4 |

> 说明：
> - `ModbusDataType` 目标值取自表；`占寄存器数` 决定点位 `address` 步进与解码长度，须与 `parse_hems_type` 返回的第三项一致。
> - **字节序（ByteOrderMode）不能照抄现有表**：现有表对 32/64 位类型按"尾号 1=ABCD/2=BADC/3=CDAB/4=DCBA"编了字节序。menu 表本身不存字节序（尾号只是命名序号），**字节序维持现有表的约定不变**（int32: 1=ABCD,2=BADC,3=CDAB,4=DCBA；int64 同 5~8 循环；float/double 同理），本轮**只改数字键 + 补 Bit/char 段 + 对齐段位**，不动各类型的字节序取值。报告里要逐段写明"字节序沿用原值"。
> - `0=Invalid`：`parse_hems_type` 遇到 0 时返回 `None`（交给 `map_db_format_to_data_type` 的兜底逻辑，默认 INT16），不要映射成有效类型。

---

## 二、改动范围（按此实施）

### 2.1 核心：重写 `modbusstudio/modbus_codec.py` 的 `HEMS_TYPE_DEFINITIONS`
1. 数字键严格按第一节 menu 表：`1=Bit、2=Signed char、…、43=Double 8`，**删掉现有的 `1=Invalid` 错位键，改为 `0=Invalid`**（值可保留 "Invalid" 但键必须是 0）。
2. 每个键的 (英文名, 中文名, ModbusDataType, ByteOrderMode, 占寄存器数) 按第一节表填；字节序沿用原值。
3. **英文名/中文名必须与 menu 表 eng_name/chn_name 逐字一致**（例如 "双精度浮点型2" 在 menu 里"型"是错字，保留原样不要"纠正"成"数"，保证名称匹配分支仍能命中库里的原文）。

### 2.2 `parse_hems_type` 双兼容（数字 + 名称都要能解析）
`parse_hems_type(indicator)` 现在已支持数字键 O(1) 查 + 英文名/中文名遍历匹配。**保持两个分支都可用**：
- 数字分支：indicator 是 int 或纯数字字符串（如 `"4"`）→ 直接查 menu.value（4 命中 "Signed short 1"）。
- 名称分支：indicator 是 "Signed short 1" / "有符号短整型1" 等 → 遍历匹配（当前测试库走这条）。
- 两条路必须**收敛到同一结果**：库值 `"4"` 和库值 `"Signed short 1"` 都要解析成 (INT16, ABCD, 1)。报告里贴一张"数字 vs 名称"对照验证表证明一致。

### 2.3 补 bit/bool 识别 + UI 下拉"比特"显示
1. `modbusstudio/hems_db_loader.py` `map_db_format_to_data_type` 的兜底关键字已有 `"bit"`（`elif "bool" in s or "bit" in s`），确认 `"Bit"`/`"bit"`/`"Bool"`/`"bool"` 各写法都归 `BOOL`（大小写不敏感）。
2. **UI 数据类型下拉加"比特"显示（用户已确认要的那项），改动点仅 `slave_panel.py:74` 这一处**。全项目 `DATA_TYPE_CHOICES` 只有 `slave_panel.py` 有（`db_import_dialog.py`、`poll_panel.py` 均无数据类型下拉，不用动）：
   - **底层 `ModbusDataType.BOOL` 的枚举值 `"BOOL"` 一个字都不能改**——它被 `models.py` 的序列化/反序列化（`ModbusDataType[str(d["data_type"])]`）、`modbus_engine.py` 的位判断依赖，改值会让已存配置反序列化直接崩。
   - 照项目已有先例 `AreaType.friendly_name`（`models.py:82`）做**显示层包装**：给下拉加一个"比特"友好显示（如显示 `BOOL(比特)`），`values` 用友好名、选中后**仍映射回底层 `"BOOL"` 原值**写进 `ModbusPoint.data_type`，序列化链路完全不变。
   - 顺带修一处隐患：`slave_panel.py:74` 的 `DATA_TYPE_CHOICES` 里有 `"FLOAT64"`，但 `ModbusDataType` 枚举值是 `"DOUBLE64"`（**没有 `"FLOAT64"`**），下拉选 `"FLOAT64"` 会在 `ModbusDataType["FLOAT64"]` 反序列化时 `KeyError`。**本单一并修**：把该项改为 `"DOUBLE64"`（显示可写 `DOUBLE64(双精度)`），与枚举对齐。报告里注明此连带修复。

### 2.4 回归测试（注意：`tests/test_codec.py` 目前 0 条 ENERGY 用例，需从零建）
- 在 `tests/test_codec.py` 新建（现无 ENERGY 相关用例）：对第一节 menu 表的**每个数字键**（1~43）逐一断言 `parse_hems_type(n)` 的 (dtype, 寄存器数) 与表一致；重点断言 `1→BOOL`、`4→INT16`、`8→INT32`、`32→FLOAT32`、`43→DOUBLE64`、`0→None`。
- 补"数字 vs 名称一致"用例：`parse_hems_type("4")` 与 `parse_hems_type("Signed short 1")` 结果相同。
- 扩 `tests/test_hems_import.py`：构造 `Variable Transfer Format = "1"`（Bit）的点位，断言导入后 `data_type == BOOL`（这是用户反馈的核心场景，此前会变成 INT16）。

---

## 三、红线
1. **不改字节序取值**，只对齐数字键 + 段位 + 补 Bit/char；字节序沿用现状。
2. 中英文名称逐字保留 menu 原样（含 "双精度浮点型2" 的"型"），不做"纠错"。
3. 不动 `archive/`、`*.legacy.py`、`memory/verify.py` 的 11 项检查逻辑；不删已有 `[SLAVE-*]`/`[POLL-CONFIG]` 标记。
4. **底层 `ModbusDataType.BOOL`/`DOUBLE64` 等枚举 `.value` 一律不改**；UI"比特"显示走显示层包装，写回底层枚举值，序列化链路不变。
5. **不生成图片**；跑测试/验证时**不结束用户正在运行的 ModbusStudio 进程**。
6. 合并门禁：`.venv\Scripts\pytest.exe` 全量全绿 + `.venv\Scripts\python.exe memory/verify.py` PASS。

---

## 四、验收标准（Claude 复核 checklist）
- [ ] `HEMS_TYPE_DEFINITIONS` 数字键 0~43 与 menu 表逐项一致（Bit=1、char=2/3、short=4~7、int32=8~11、uint32=12~15、int64=16~23、uint64=24~31、float=32~35、double=36~43、Invalid=0）。
- [ ] `parse_hems_type("4")` 与 `parse_hems_type("Signed short 1")` 结果一致，均为 (INT16, ABCD, 1)。
- [ ] 库值 `1`（Bit）能解析出 `BOOL`（不再是 INT16）；回归测试通过。
- [ ] 字节序取值与改动前逐段一致（报告附对照）。
- [ ] `map_db_format_to_data_type` 对 `"Bit"/"bit"/"Bool"/"bool"` 均归 BOOL。
- [ ] UI 下拉（仅 `slave_panel.py`）能显示"比特"，选中写回底层 `"BOOL"`；`FLOAT64` 隐患项已改 `DOUBLE64`；底层枚举值未变。
- [ ] `tests/test_codec.py` 从零建 ENERGY 用例（原 0 条），逐数字键断言 + 数字/名称一致性。
- [ ] pytest 全量通过，无 skip 掩盖真实失败；verify.py PASS。
- [ ] 无图片产物、无进程被杀、archive 未动。
