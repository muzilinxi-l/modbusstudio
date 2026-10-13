# Handoff: 任务 07 & 07b（8 项 UI 与数据库解析问题修复 + 复核收尾）交接记录

```yaml
id: MEM-0207
status: verified
source: docs/Antigravity/07b_REQ8_RECHECK_REPORT.md
commit: 53b7ea0
author: antigravity
verified_by: claude
updated: 2026-10-13T10:28:00+08:00
scope: F:/GitHub/python @ 2.0.0
```

## 1. 工作区与分支定位
- **Worktree 路径**：`F:/GitHub/python`（当前仓库为单工作树环境）
- **Git 分支名**：`main`
- **基准 Commit**：`53b7ea0`
- **对应需求指令**：`docs/Claude code/07_REQ8_UI_AND_PARSE_FIX_PROMPT.md`、`docs/Claude code/07b_REQ8_RECHECK_PROMPT.md`
- **执行报告路径**：`docs/Antigravity/07_REQ8_UI_AND_PARSE_FIX_REPORT.md`、`docs/Antigravity/07b_REQ8_RECHECK_REPORT.md`

## 2. 07 任务主体改动文件清单（已由 Claude Code 逐项核验通过）
1. `modbusstudio/hems_db_loader.py`：
   - 新增 `HemsAppModel._normalize_function_code(raw_fc)`（兼容 `"3"`/`"03"`/`3`/`"0x03"`/`"FC03"` 等）；
   - 修复 `extract_studio_points()` 中 `Function Code` 优先级，移除将含 `"状态/status/告警/alarm"` 的保持寄存器误判为 `3x` 输入寄存器的缺陷（需求 1）；
   - 在 `HemsDatabase.load_modbus_apps()` 中读取 `app` 表顶层 `[Realtime Variables]` 与 `[Referred Variables]` 列的 `"Chinese Name"` 自动回填至 `More` 变量对象（需求 4）。
2. `modbusstudio/ui/slave_panel.py`：
   - `AREA_CHOICES` 统一改为 `0x → 1x → 3x → 4x` 升序，默认选中 `4x`，并支持点击 `area` 表头按 `0x → 1x → 3x → 4x` 排序（需求 2）；
   - 顶栏 `top_box` 拆分为 `row0` / `row0b` / `row1` / `row1b` / `row1c` 独立水平行，“启动当前服务”与“导入数据库点表”各自独立成行（需求 3）；
   - 新增 `BAUDRATE_CHOICES` 9 档完整波特率列表（`1200` ~ `230400`，默认 `9600`）（需求 7）；
   - 数据位 `combo_slave_databit` 的 `values` 改为升序 `["7", "8"]`，默认 `"8"`（需求 8）。
3. `modbusstudio/ui/dialogs/db_import_dialog.py`：
   - 列表“业务设备名称 (中文)”列严格展示独立中文名，缺失或与英文名相同时显示 `—（无中文）`，并在规则区增加 `lbl_selected_chn_summary` 已选设备中文名汇总（需求 4）；
   - 在 `mode_frame`（`导入模式与规则配置`）内部新增实时搜索框 `entry_mode_search` 与“清空”按钮，支持按设备中英文名/App ID/点位描述/地址过滤且清空后恢复勾选状态（需求 5）；
   - 在 `mode_frame` 新增 RTU 串口五要素规则控件与缺省功能区下拉框 `combo_default_area`，并在 `_import_as_new_instances()` 中注入 `ConnectionConfig`（需求 2、6、7、8）。
4. `modbusstudio/ui/poll_panel.py`：
   - 同步将 `AREA_CHOICES` 改为 `0x → 1x → 3x → 4x` 升序，补全 9 档 `BAUDRATE_CHOICES` 及 RTU 数据位/校验位/停止位控件并注入 `_get_connection_config()`（需求 2、6、7、8）。
5. 测试文件：
   - 扩充 `tests/test_hems_import.py`（新增 2 项回归测试）；
   - 新建 `tests/test_ui_layout_order.py`（新增 4 项 UI 顺序与布局回归测试）。

## 3. 07b 复核收尾与三条待确认项裁定归档
1. **三条待确认项最终裁定（已全部关闭）**：
   - **裁定 ①**：北向 `App 166 ([North]30)` 的 30 个 `Function Code="4"` 点位维持归入 `3x_InputRegister`，忠于库值与协议标准（零改动关闭）；
   - **裁定 ②**：确认 `slave_panel.py` 顶栏 5 行拆分（`row0/row0b/row1/row1b/row1c`）方案（零改动关闭）；
   - **裁定 ③**：规则区搜索命中后导入整台设备全部点位、不裁剪点位子集，已在 `db_import_dialog.py:216` 追加 1 行说明注释（已关闭）。
2. **收尾项 1（偶发并发用例连续 3 次全量复测）**：
   - 连续 3 次全量 `pytest -q` 0 失败（`test_slave_listener_restart_after_stop_restarts` 全部 PASSED），确认稳定。
3. **收尾项 2（`hems_db_loader.py` 模板废话注释清理）**：
   - 已清理/替换 `hems_db_loader.py` 中 74 处机械模板注释为真实原因说明，未改动任何业务代码行。
