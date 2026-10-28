# TEST_DATA_FIX — 任务 14 测试数据依赖修复 PROMPT（给执行方：Antigravity）

> 归属：Claude Code（约束/需求方）。执行方：Antigravity，改完代码后在
> `python/docs/Antigravity/14_TEST_DATA_FIX_REPORT.md` 出 REPORT。验收人：Claude Code。
> 依据：深度 review（5 维审查 + 逐条对抗性验证，全部高置信坐实）。核心结论：
> UI/E2E 测试把 **gitignored 现场数据**（cdb 行数、exe 构建产物、
> session_state.json 绝对路径）当成了隐式 fixture，导致测试结果随机器/数据漂移，
> 且 `test_db_import_dialog_default_selection_policy` 当前在全量 pytest 中挂红。

## 0. 缺陷清单（按严重度排序）

### P0-① 挂红用例：断言与现场 cdb 设备数耦合
- **文件**：`tests/test_batch_import_and_limits.py:214`
  （`test_db_import_dialog_default_selection_policy`）
- **现象**：`assert len(sel_type1) > 1` 假设"现场 cdb 里 Type=1 使能设备 ≥2 台"，
  但弹窗加载的是 `session_state.json` 里 `last_db_path` 指向的 cdb（当前指向
  `C:\Users\123\Desktop\内部工作人员\张炎\hems.cdb`，Type=1 使能仅 1 台，id=156），
  UI 按"全选当前过滤视图内使能设备"正确选中 1 台 → `1 > 1` 挂红。
  `extra/hems.cdb` 回退默认有 7 台时同代码必绿——**结果随机器数据漂移**。
- **修法约束**：
  1. 给该测试注入**合成 cdb fixture**（用 `HemsDatabase`/`hems_db_loader` 的写入
     能力，在 `tmp_path` 下造 3 台 Type=1 使能设备 + 1 台禁用 + 若干 Type=2），
     通过 `ModbusStudioAppCore` 构造后**显式 `app.db_path = 合成路径`**，
     禁止走 `restore_session=True` 的默认路径（消除对 session_state.json 的继承）；
  2. 断言改为**与设备数解耦**：`assert len(sel_type1) == 使能设备数` 且
     `for item in sel_type1: assert 对应 app.enable == 1`（用 fixture 已知数）；
  3. 测试头部加 `pytest.importorskip("tkinter")`（该文件目前只在 import 处有
     tkinter，保持无头环境可 skip 不挂红）。

### P0-② 同源脆弱：test_full_ui_import_e2e 选中"恰好第一台带点表的设备"
- **文件**：`tests/test_full_ui_import_e2e.py:44-126`
- **现象**：`assert os.path.exists('extra/hems.cdb')` 硬依赖 gitignored 现场文件；
  设备选择 `next(app for app in apps if points)` 静默回退 `apps[0]`，
  若回退目标 0 点表则 `loaded_cnt` 保持 0 → `assert loaded_cnt > 0` 挂。
  当前 cdb 17 台 Type=1 中 3 台 0 点表，选中成功纯属数据巧合。
- **修法约束**：
  1. cdb 路径改为**可注入**（`os.environ.get("HEMS_TEST_CDB")` 或
     `request.param`，默认 `extra/hems.cdb`），文件不存在时
     `pytest.skip("Hems cdb not available")`（复用该文件已有的
     Tk 初始化失败 skip 模式，把守卫扩到 cdb 缺失场景）；
  2. 设备选择**禁止静默回退** `apps[0]`：改为显式选中
     "首台带点表的 Type=1 设备"，若该设备点表行数 <2 则 skip（带 reason）；
  3. 不改动 `dist/extra/hems.cdb` 现场数据内容结构。

### P1-③ 同源脆弱：test_exe_runner 依赖 gitignored 构建产物
- **文件**：`tests/test_exe_runner.py:30-32`
- **现象**：`assert os.path.exists("dist/ModbusStudio.exe")` 在干净
  checkout/CI 必挂（`dist/` 被 .gitignore 第 15 行忽略），无 skip 无构建钩子。
- **修法约束**：
  1. 模块级 `pytest.skip` 守卫：exe 不存在则 `pytest.skip("EXE 构建产物缺失，
     请先执行 PyInstaller 构建")`（该文件已在非 Windows / 无 PIL 时 skip，
     把守卫扩到 exe 缺失场景，保持模块级 skip 语义）；
  2. 禁止改 .gitignore（dist/ 忽略是既定交付策略）；
  3. 禁止在该测试里"顺带跑构建"（构建耗时且与本用例关注点分离）。

### P1-④ 测试继承机器特定 last_db_path，结果不可复现
- **文件**：`modbusstudio/app.py:64`（`os.path.abspath` 绝对路径写入）
  + `tests/test_batch_import_and_limits.py:203`（`ModbusStudioAppCore()` 默认
  `restore_session=True`）
- **现象**：测试直接继承开发者机器上 `session_state.json` 的
  `last_db_path`（绝对路径，跨机器失效），结果随机器漂移。
- **修法约束**：
  1. 凡测试中构造 `ModbusStudioAppCore()` 的地方（至少
     `test_batch_import_and_limits.py` 两例、`test_full_ui_import_e2e.py`），
     统一传 `restore_session=False` 或构造后**立即显式 `app.db_path = 注入路径`**，
     消除对 session_state.json 的隐式继承；
  2. 不修改 `app.py` 生产逻辑（`save_session_state` 写绝对路径是正确行为，
     UI 侧照旧；只约束测试侧的注入方式）。

## 1. 红线（不要越界）

- 改动范围仅限：`tests/test_batch_import_and_limits.py`、
  `tests/test_full_ui_import_e2e.py`、`tests/test_exe_runner.py`
  （+ 如需新增 fixture 辅助，放 `tests/conftest.py` 或新 `tests/fixtures/`，
  仅限测试侧）；
- **零** 生产代码改动：不碰 `modbusstudio/` 下任何文件（`services.py`/
  `app.py`/`modbus_engine.py`/`ui/dialogs/db_import_dialog.py` 一律不动）；
- 不碰 `dist/extra/hems.cdb`、`extra/`、`session_state.json` 内容结构；
- 不碰 `memory/verify.py`、`archive/`、`*.legacy.py`；
- 不杀用户正在运行的 ModbusStudio 进程；
- 不 push——提交后由 Claude Code 验收通过再推。

## 2. 验收标准（Claude Code 复核点）

- [ ] `pytest tests/test_batch_import_and_limits.py tests/test_full_ui_import_e2e.py
      tests/test_exe_runner.py -q` 在当前机器上全绿（贴原始输出）；
- [ ] 全量 `pytest -q` 通过（既有基线不回归，`test_hub_unpack_guard.py`
      4 项不挂）；
- [ ] **可复现性证明**：临时把 `session_state.json` 移走（或改
      `HEMS_TEST_CDB` 环境变量）跑 P0-① 用例仍全绿——证明用例已脱离
      机器特定数据；
- [ ] REPORT（`docs/Antigravity/14_TEST_DATA_FIX_REPORT.md`）含：
      每条修法→改了什么→文件:行号 + 合成 fixture 的设备/点表规格 +
      每个 skip 守卫的触发条件；
- [ ] `git diff --stat` 证明改动仅限 §1 允许的文件。

## 3. 与历史结论的边界

- 216be4e（任务 13）核心改动**无回归**，本次不碰 `modbus_engine.py`/
  `tests/test_hub_unpack_guard.py`；
- `test_db_import_dialog_default_selection_policy` 的挂红是**存量缺陷**
  （08aafa3 引入断言，19 天前），与 216be4e 无关，本次只做测试侧收口，
  不改 UI 生产逻辑（`_select_all_on_current_filter` 行为本身正确，不动）；
- Gemini/Antigravity 此前报告与本 PROMPT 矛盾处，以本 PROMPT 的
  高置信验证结论为准。
