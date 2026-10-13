# PROMPT：07 任务复核收尾（07b RECHECK）— 偶发并发用例复测 + 注释清理

> 用途：发给 **Antigravity** 执行。
> 执行完成后在 `docs/Antigravity/07b_REQ8_RECHECK_REPORT.md` 出报告，Claude 复核。
> 日期：2026-10-13　|　执行方：Antigravity　|　验收方：Claude Code
> 背景：07 任务（8 项 UI 与解析修复）已执行完毕，Claude 已对照代码逐条核验 8 项需求**全部实现属实**。本单只处理复核发现的两个收尾项 + 用户已裁定事项，**不重做 07 的主体改动**。

---

## 〇、Claude 复核结论与用户裁定（照此执行，不再重新讨论）

### 0.1 8 项需求实现核验（全部通过）
- 需求 1（功能码归一化 + 严格尊重库值，FC03→4x、FC04→3x）：✅ 属实。
- 需求 2/7（三面板 `AREA_CHOICES` 统一 0x→1x→3x→4x 升序，默认仍 4x）：✅ 属实。
- 需求 3（slave_panel 顶栏拆 `row0/row0b/row1/row1b/row1c` 五行）：✅ 属实。
- 需求 4（中文名列严格取值 + 顶层列回填 + 已选中文名汇总）：✅ 属实，且 `load_modbus_apps()` 发现中文名实际存 `app` 表顶层列这一根因修复认可。
- 需求 5（`mode_frame` 内规则区搜索 + 清空恢复勾选）：✅ 属实。
- 需求 6（三处 RTU 五要素齐全并注入 `ConnectionConfig`）：✅ 属实。
- 需求 8（`BAUDRATE_CHOICES` 9 档三处统一，默认 9600）：✅ 属实。

### 0.2 用户裁定（本轮最终结论，Antigravity 不再重新讨论）
1. **待确认项 ①（北向 App 166 的 FC04 点位）**：**选 A——维持现状，不做改动**。依据：
   - `extra/hems.cdb` 实测，App 166（`[North]30`）的 30 个 `Referred Variables` 点位，库中显式写死 `"Function Code": "4"`，且 `Variable Transfer Format` 为 `Unsigned short 1`/`Signed int 1`（数值型，非 BOOL）。
   - 按 Modbus 标准，**FC04 = 读输入寄存器（3x 区）**，不是"本该是 4x 却被误判"；07 代码把这类点落 `3x_InputRegister` 是**忠于库值、协议正确的行为**。
   - 结论：**不新增"强制转 4x"的例外规则，北向 166 的 FC04 点位维持落 3x**。此项关闭，无代码改动。
2. **待确认项 ②（换行粒度）**：确认 5 行拆分（`row0/row0b/row1/row1b/row1c`）方案。此项关闭，不做改动。
3. **待确认项 ③（规则区搜索口径）**：**整台设备导入，不裁剪设备内点位子集**。现状行为正确，此项关闭，**不做代码改动**；仅在 `db_import_dialog.py` 规则区搜索框（`entry_mode_search` 附近）加**一行**说明注释："搜索命中后导入整台设备的全部点位，不裁剪未命中点位"，防止后续误读。

> 以上三条裁定中，①②为"现状保持、零改动"；唯一代码改动是 ③ 的那一行说明注释。

---

## 一、收尾项 1：偶发并发用例复测（重点）

Claude 复核时全量 `pytest` 出现 **一次** `tests/test_multi_station_concurrency.py::test_slave_listener_restart_after_stop_restarts` 失败（49 passed, 1 failed），重跑全量与单独跑该文件均通过（50 passed）。判断为**偶发时序 flaky**，非 07 改动直接引入，但"全绿门禁"要求合并前必须确认稳定。

**要求**：
1. 连续跑 **3 次**全量 `.venv\Scripts\pytest.exe -q`，三次都记录结果；报告逐次列出"通过/失败数 + 失败用例名（如有）"。
2. 若 3 次全绿 → 该收尾项标记"已确认稳定"，不改代码。
3. 若任一次失败 → **不要静默跳过**，在报告里贴出该次完整失败 traceback，并给出你对失败原因的分析（是否 `modbus_engine.py` 的 `ModbusTransportServer` 重启探活/半死重建 `[SLAVE-RESTART]` 分支的时序竞争；是否 07 改动触碰了该路径——注意 07 主体未改 engine，但须用 `git diff` 佐证说明）。分析写清即可，**本单不要求修复 engine 时序问题**，把结论交给 Claude 决定要不要另开单。

---

## 二、收尾项 2：清理 `hems_db_loader.py` 的模板化废话注释

`modbusstudio/hems_db_loader.py`（约 346~390 行，`extract_studio_points()` 区域判定段落及 `load_modbus_apps()` 部分）被 06 号注释任务机械执行后，叠上了一批**模板化、无信息量**的注释，形如：

```
# 在 [extract_studio_points] 中更新局部变量 [area]：为后续协议编码、服务调度或界面渲染准备中间状态
# 在 [extract_studio_points] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
```

这类注释：
- 违反项目"注释只解释**为什么**，不解释**做了什么**"规范；
- "服务调度""界面渲染"等词在此处毫无指代意义，对读代码的新手反而误导。

**清理规则（严格遵守）**：
1. **只删/改废话注释，不动任何代码行**（函数体、分支、变量赋值、import 一律不碰）；保留有实际信息量的注释（如"为什么先把 Function Code 统一归一化为整数…"那段是对的，保留）。
2. 把无信息量的模板注释**替换为该分支真实的原因说明，或整行删除**。例：
   - `elif fc_num == 4:` 分支注释应写成"FC04 是只读输入寄存器区（3x），显式配置时数值点必须落 3x，不因描述含 status/告警 关键词被改到 1x 离散输入"；
   - `elif fc_num in (3, 6, 16):` 分支注释应写成"FC03/FC06/FC16 对应 4x 保持寄存器区，显式配置时数值点保持 4x、显式 BOOL 落 0x 线圈，绝不因描述关键词改到 1x/3x"；
   - `else:`（未配 Function Code 兜底分支）注释应写成"仅当点位与 Pollings 均未提供有效 Function Code 时才走名称启发式兜底；明确数值型落 4x，布尔/告警型落 1x 离散输入"。
3. 清理范围：以 `grep -n "更新局部变量\|执行前置条件分支判断\|准备中间状态\|走对应处理路径" modbusstudio/hems_db_loader.py` 命中的行为准，逐条处理；如在同文件其他方法发现同类废话注释，一并清理并在报告列出。
4. 改完跑 `.venv\Scripts\python.exe -c "import modbusstudio.hems_db_loader"` 确认无语法错误，再跑一次 `.venv\Scripts\pytest.exe -q`（含本次 3 次中的第 3 次）确认全绿。
5. **不生成任何图片**（遵守 CLAUDE.md 输出能力限制）。

---

## 三、红线
1. 不改 `archive/`、`*.legacy.py`、`memory/verify.py` 的 11 项检查逻辑。
2. 不重做 07 主体改动，不顺手重构无关代码、不改函数签名/返回值。
3. 不删 07 已有的有效注释与 `[SLAVE-RESTART]`/`[SLAVE-RECONNECT]`/`[SLAVE-SPEED-4]`/`[POLL-CONFIG]` 等标记。
4. 合并门禁：收尾后跑 `.venv\Scripts\pytest.exe` 全量必须全绿 + `.venv\Scripts\python.exe memory/verify.py` 通过。

---

## 四、报告要求（`07b_REQ8_RECHECK_REPORT.md`）
- 收尾项 1：3 次全量 pytest 的逐次结果；若有失败贴 traceback + 原因分析。
- 收尾项 2：列出所有被清理/改写的注释行（`file:line` 旧→新对照），说明每条为什么这么改；确认"只动注释未动代码"（可附 `git diff --stat` 佐证仅 `hems_db_loader.py` 有注释 diff、`db_import_dialog.py` 仅 +1 行说明注释）。
- 0.2 三条裁定的"关闭确认"：逐条写"已关闭，未改代码"或"已加 1 行说明注释"。
- 附 `git diff --stat` 全量（相对 07 完成时的 `main`），确认本单新增 diff 仅注释类改动，无功能改动。
- 交接记录更新：在 `memory/handoffs/` 新增或更新 07b 记录，把 07 的交接记忆状态从 `proposed` 推进（若 3 次全绿则标 `verified`，若并发用例仍 flaky 则标 `disputed` 并说明）。
