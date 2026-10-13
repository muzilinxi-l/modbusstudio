# PROMPT：全库代码 review 缺陷修复（09）

> 用途：发给 **Antigravity** 执行。
> 执行完成后在 `docs/Antigravity/09_REVIEW_FIX_REPORT.md` 出报告，Claude 复核。
> 日期：2026-10-13　|　执行方：Antigravity　|　验收方：Claude Code
> 约束：遵循 CLAUDE.md/GEMINI.md「输出能力限制（仅文本）」与「进程保护（不杀运行中的 ModbusStudio 进程）」——全程文本、不生成图片、验证时不结束用户正在运行的软件进程。
> 来源：本轮 Claude 驱动两个独立 code-review 子代理，逐模块通读 `modbusstudio/` 全 11 个源码文件 + 全 11 个测试文件，并对每条缺陷用 `.venv\Scripts\python.exe` 实际复现。基线 `pytest -q` = **52 passed, 1 skipped**（skip 为已知 `test_ui_theme` 竞态，非真失败）。

---

## 〇、本轮定位（先读这段）
- 这批缺陷**不是 08 对齐任务改出来的**——08（数据类型 0~43 编号对齐 + UI 比特展示）本身是对的、测试全过。本批要么是 08 配套代码里"该接没接"的漏接（H1、H2），要么是**历史债务**（M1、H3、M3、L1、L2），要么是 07 引入的健壮性缺口（M2）。
- 严重度三档：**必修**（数据/端口正确性）→ **应修**（埋雷 + 测试覆盖）→ **可选**（卫生加固）。Antigravity 按档执行，但**每一档都必须改完再跑门禁**，不许只修高就停。
- 每条都给了 `file:line` 与复现证据，**改前先复核该行现在是否还是描述的样子**（代码可能已微调），不符就以当前代码为准并在报告注明。

---

## 一、必修（数据 / 端口正确性，优先）

### H1 — `hems_pairing.py:83` 字节序硬编码 CDAB，丢失格式自带变位　【08 配套漏接】
- **问题**：`PairingRule.__init__` 里 `self.byte_order = ByteOrderMode.CDAB` 一刀切。而 `map_db_format_to_data_type(target_format or src_format)` 已正确解析出 44 种 HEMS 格式各自的数据类型与字节序，`hems_db_loader.py:69` 也**已经提供**了正确工具函数 `map_db_format_to_byte_order(fmt_str, default_mode)`，单点导入路径 `hems_db_loader.py:344` 用的是它——**唯独 `hems_pairing.py` 没接**。
- **复现证据**（子代理实测）：`target_format=32` → dtype=FLOAT32 但 byte_order=CDAB（定义里 32 应是 ABCD）；`target_format=37` → dtype=DOUBLE64 但 byte_order=CDAB（应是 BADC）。同组寄存器按 ABCD 解 3.14159，按 CDAB 解 ≈0。
- **修复**：`self.byte_order = map_db_format_to_byte_order(target_format or src_format, default_mode=ByteOrderMode.CDAB)`；在 `hems_pairing.py` 顶部 `from modbusstudio.hems_db_loader import map_db_format_to_byte_order`（或从 codec 导，看现有 import 习惯，**保持与 344 行同款来源**避免双份实现）。
- **回归测试**：新建/扩 `tests/test_hems_pairing.py`，构造 3 条 32/64 位 FORWARD 配对（格式 32/37/34），断言 `rule.byte_order` 分别为 ABCD/BADC/CDAB，不再是清一色 CDAB。
- **归属**：08 引入的漏接，本轮新发现。

### H2 — 新增从机默认端口 `502 + len(devices)` 无避让，撞车风险　【本轮新发现】
- **问题**：`slave_panel.py:959`（`_add_new_slave_instance`）与 `db_import_dialog.py:776`（`base_port = 502 + len(existing_devices)`）/`866` 假设"从机总数 = 已用端口数"。用户手动改过某台端口或删过设备后，新设备自动端口会与仍在监听的其他从机/Hub **重叠**。`ModbusServerHub` 按 `(host, port)` 共享物理监听端口，两台同 `ip:port` 不同 unit_id 的从机会被 `db_import_dialog.py:889` 的 `used_endpoint_units.add((ep_key, unit_id))` 误判为"同一端点"去重掉。
- **复现路径**（子代理推演，需 Antigravity 跑一次坐实）：预置 2 台从机（其中一台手动改为 5100），再导入 1 台 → 新台算出 `502+2=504`，若 504 恰被别的 Hub 监听则冲突；`used_endpoint_units` 里两台同 `ip:504` 不同 unit_id 会被 `(ep_key)` 维度去重。
- **修复**：
  1. 新增实例/导入时先查 `ModbusServerHub`（或对应 service）中**已被占用的 `(host, port)` 集合**，候选端口从 `502` 起**逐个探测首个未占用者**，而不是 `502 + len(devices)` 一把算。
  2. `db_import_dialog.py:889` 在 `used_endpoint_units` 之外**再维护一个 `used_ports` 集合**做端口级去重，`(ep_key, unit_id)` 同键时才去重，仅 `ep_key` 相同但 unit_id 不同的**不允许静默合并**，改为报错或提示。
- **回归测试**：`tests/test_ui_*` 或新建用例，构造"已占用 {502,504}"场景，断言新增取 503；同 `ip:port` 不同 unit_id 两台不被静默合并。
- **归属**：本轮新发现（07/08 未涉及端口避让）。

### H3 — 端口解析 `int()` 强转崩溃　【历史债，上轮 #6 复现仍在】
- **问题**：`models.py:406` `port=int(d.get("port", 502))`、`poll_panel.py:311` `port=int(self.entry_port.get().strip() or "502")`。端口填 `"abc"` / `"502.5"` / `"0x1F6"` 直接 `ValueError`；`poll_panel` 场景会让 **GUI 线程崩退**，`models.from_dict` 场景让**整个设备/点表加载链路**失败且无兜底。
- **修复**：抽一个 `def _parse_port(val: Any, default: int = 502) -> int`（放 `models.py`，`poll_panel` 复用 import）：`str(val).strip()` → try `int(s)` 失败或越界 `[1, 65535]` 时 log warning 并回退 `default`。`models.from_dict` 与 `poll_panel:311` 都改用它，**不再裸 `int()`**。
- **回归测试**：`tests/test_models.py`（或对应文件）断言 `port="abc"`→502、`"502.5"`→502、`"0"`→502、`"65536"`→502、`"502 "`→502、正常 `"502"`→502。
- **归属**：历史债。

---

## 二、应修（埋雷 + 测试覆盖）

### M1 — `modbus_engine.py:1324` `is_bit` 用枚举 `in` 枚举，靠"完整串包含自己"巧合恒真　【历史债，上轮 #2/#4 根因】
- **问题**：`is_bit = (AreaType.COIL in area or AreaType.DISCRETE_INPUT in area)`。实测 `AreaType.COIL = "0x_Coil (线圈)"`，`AreaType.COIL in AreaType.COIL` 恒为 True（Python `in` 对"被查询串==查询串"判子串）。当前所有调用点传原始枚举值所以**碰巧对**；一旦调用方传派生串（如日志拼接区名 `"0x_Coil (线圈) @ 100"` 或别的区名写法），`AreaType.COIL in 派生串` 退化为普通子串判断，静默变 False，把寄存器值 `& 0xFFFF` 写进布尔区。
- **修复**：照同文件 `write_typed_value`（约 :1359 起）的健壮写法：`s = str(area).strip().upper()`；`is_bit = ("0X" in s or "COIL" in s or "1X" in s or "DISCRETE" in s)`。**别再用枚举值直接做 `in` 判断**。
- **回归测试**：`tests/test_poll_service.py` 或新建，用派生串 `"0x_Coil (线圈) @ 1"` 与裸 `"0x_Coil"` 两种入参调 `write_raw_values`，断言布尔区都按 bit 写（值 1/0 而非 `&0xFFFF`），证明对派生串也稳健。
- **归属**：历史债（本轮实锤根因）。

### M3 — `services.py:219-221` HEX16 分支两分支表达式相同，`0x` 前缀反被拒　【本轮新发现】
- **问题**：
  ```python
  val_converted = int(s_hex, 16) if s_hex.startswith("0x") else int(s_hex, 16)
  ```
  两个分支**完全一样**：`int("0x1A", 16)` 直接 `ValueError` 被外层 `except` 吞掉，返回 `False, None, "数值格式与类型 HEX16 不匹配"`。用户最常见的 `0x1A2B` 写法**反而校验不过**，纯 `1A2B` 才通。
- **修复**：`int(s_hex, 0)`（`0` 前缀自动识别进制，`0x1A`→26、`1A`→26、`10`→16——注意 `int("10",0)` 因无前缀会按十进制=10，与旧"纯 `1A2B`"语义有差异，**要确认项目里 HEX16 输入约定**：若约定必须十六进制，则改成"剥 `0x` 前缀后 `int(rest, 16)`"更稳妥，Antigravity 二选一并写报告说明口径）。推荐稳妥口径：`s = s_hex.lower(); s = s[2:] if s.startswith("0x") else s; int(s, 16)`，非法字符仍抛给外层兜底。
- **回归测试**：`tests/test_*.py` 断言 `validate_and_build_point`（或对应入口）对 `"0x1A2B"`→通过（值 0x1A2B）、`"1A2B"`→通过、`"ZZ"`→不匹配。
- **归属**：本轮新发现。

### T1 — `tests/test_full_ui_import_e2e.py` 的 `run_full_ui_import_self_test()` 无 `test_` 前缀，pytest 根本不收集　【本轮新发现】
- **问题**：函数名 `run_full_ui_import_self_test`，只在 `__main__` 手动跑；**常规 CI 基线里 UI 批量导入路径零覆盖**。
- **修复**：把该函数改名为 `test_full_ui_import_self_test`（或包一层 `def test_...(): run_full_ui_import_self_test()`），确认 pytest 能收集并跑通；若它需要真实 socket/文件 fixture，加对应 fixture + `pytest.importorskip`/`skipif`，别让它静默 skip 掉。
- **归属**：本轮新发现（测试覆盖缺口）。

---

## 三、可选（卫生 / 加固，时间够再修，不阻塞门禁）

### M2 — `hems_pairing.py:82` `target_format` 为空时静默兜底 INT16，多字派生量错位　【疑似，历史债 + 上轮 #7 关联】
- **问题**：`target_format`/`src_format` 都空时走 `parse_hems_type` 返回 None → 默认 INT16/1 寄存器；若该派生量实际多字，配对引擎少读 1 字致相邻点位错位。**需现场 `.cdb` 坐实，本轮未找到反例**，只能加固。
- **修复（加固，不改默认语义）**：`target_format` 与 `src_format` 都空时 log 一条 warning「点位 XXX 按默认 INT16/1 寄存器处理，若实际为多字数据请人工核对」，**不静默**。

### L1 — `poll_panel.py:11` 导入卫生　【历史债】
- **问题**：`Optional` 导入未用；真实用到的 `Dict`/`List`（:508/556/573）没 import（靠 `from __future__` 惰性才没崩）。
- **修复**：改为 `from typing import TYPE_CHECKING, Dict, List, Optional`（`Optional` 若确认没用就删掉，报告注明）。

### L2 — `tests/test_exe_runner.py:6-11` 模块级 Windows-only import　【历史债】
- **问题**：模块顶层 `import PIL` + `ctypes.windll`，非 Windows CI 会 `ImportError` 中断整个文件加载。
- **修复**：把 PIL/ctypes 相关 import 挪进用到它们的函数体，或文件头 `pytest.importorskip("PIL")`，避免污染跨平台 CI。

---

## 四、已核实"当前无问题"、**不必修**的条目（避免误伤）
- **上轮 #1（`write_raw_values` 与协议层 1 格偏移）：本轮实锤推翻**。实测 pymodbus 3.15 `base_addr` 为 **0**（非上轮假设的 1），`write_raw_values` / `read_raw_values` / 协议层 `async_getValues` 三条路径 `start_idx` 算法一致，**数值当前自洽，没有真实错位**。上轮基于 `base_addr=1` 的假设不成立。**不要按上轮 #1 的"统一为 address-1"去改**（改了反而引入偏移）。
- **上轮 #5（中文名回填键名不匹配）：与 M2 归并**，`hems_db_loader.py` 顶层列名与 `More` JSON 内部键名需人工核对一次，本轮静态审查未找到反例，**暂按 M2 加固处理**（如需硬修，Antigravity 用真实库跑一次回填、打印失败计数，报告贴出）。
- **上轮 #7（`parse_hems_type(0)` 兜底 INT16）：与 M2 归并**，0=Invalid 语义本轮保持 INT16 兜底不变（08 已定），仅补 M2 的 warning。

---

## 五、执行顺序与文件清单（按档，改完一档跑一次门禁）

| 顺序 | 文件 | 涉及条目 | 改动要点 |
|---|---|---|---|
| 1 | `modbusstudio/hems_pairing.py` | H1、M2 | 接 `map_db_format_to_byte_order`；补 warning |
| 2 | `modbusstudio/ui/slave_panel.py` + `modbusstudio/ui/dialogs/db_import_dialog.py` | H2 | 端口避让探测 + `used_ports` 端口级去重 |
| 3 | `modbusstudio/models.py` + `modbusstudio/ui/poll_panel.py` | H3、L1 | `_parse_port` 兜底 + import 卫生 |
| 4 | `modbusstudio/modbus_engine.py` | M1 | `is_bit` 改健壮写法 |
| 5 | `modbusstudio/services.py` | M3 | HEX16 分支修正 |
| 6 | `tests/test_full_ui_import_e2e.py` + `tests/test_exe_runner.py` | T1、L2 | 改名纳入收集；importorskip |
| 7 | `tests/`（按需新建/扩） | H1、H2、H3、M1、M3 回归 | 见各条目"回归测试" |

**每文件改完验证**：`.venv\Scripts\python.exe -c "import modbusstudio.<模块>"` 不报错。

---

## 六、红线
1. **不修"当前无问题"的条目**（第四节列明的），尤其**不要按上轮 #1 假设改 offset**。
2. 最小改动：只动上述条目涉及的行，不顺手重构其它逻辑、不改无关函数签名。
3. 不动 `archive/`、`*.legacy.py`、`memory/verify.py` 的 11 项检查逻辑；不删已有 `[SLAVE-*]`/`[POLL-CONFIG]` 标记。
4. **不生成图片**；跑测试/验证时**不结束用户正在运行的 ModbusStudio 进程**（无 Stop-Process / taskkill）。
5. 合并门禁：`.venv\Scripts\pytest.exe` 全量全绿（区分已知 flaky `test_slave_listener_restart_after_stop_restarts` 与已知 skip `test_ui_theme`，若失败先重跑一次确认非 flaky 再报）+ `.venv\Scripts\python.exe memory/verify.py` PASS。
6. 每处改动在报告给 `file:line` + 一句"改了什么/为什么"；未坐实"疑似"的条目（M2、上轮 #5）写清"仅加固、未改默认语义"。

---

## 七、验收标准（Claude 复核 checklist）
- [ ] H1：`PairingRule.byte_order` 随 `target_format` 变（32→ABCD / 37→BADC / 34→CDAB），回归测试通过；报告贴 3 条断言。
- [ ] H2：新增实例端口从 502 起探测首个未占用（非 `502+len`）；`db_import_dialog` 有端口级去重，同 `ip:port` 不同 unit_id 不静默合并。
- [ ] H3：`_parse_port` 对 `"abc"/"502.5"/"0"/"65536"/"502 "` 全兜底 502、正常 `"502"` 仍 502；`poll_panel` 不再裸 `int()` 崩。
- [ ] M1：`write_raw_values` 对派生串入参的 `is_bit` 判定正确（布尔区按 bit 写），回归测试含派生串用例。
- [ ] M3：HEX16 校验 `0x1A2B` 通过（写清采用的进制口径）。
- [ ] T1：`test_full_ui_import_e2e` 被 pytest 正常收集并跑通（若需 fixture 已加，非静默 skip）。
- [ ] 可选 L1/L2/M2 已加固，报告注明。
- [ ] 第四节"无问题"条目**未被误改**（尤其 offset 未动）。
- [ ] pytest 全量通过、verify.py PASS；无图片产物、无进程被杀、archive 未动。
