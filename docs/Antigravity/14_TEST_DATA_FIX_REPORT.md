# 任务 14 —— 测试数据依赖修复报告

- **执行方**：Antigravity
- **验收方**：Claude Code
- **日期**：2026-10-28
- **依据与指令**：`docs/Claude code/14_TEST_DATA_FIX_PROMPT.md`
- **状态**：实施与自检完成，全量回归与门禁 PASS，等待 Claude Code 最终复核验收。

---

## 一、实施摘要（每条修法定位与文件行号）

严格对照 PROMPT 的红线规范，**零生产代码改动**（未碰 `modbusstudio/` 下任何文件，未碰 `session_state.json` 与现场 `hems.cdb`），改动仅限测试套件 3 个文件：

| 序号 | 缺陷项 | 目标文件与行号 | 核心修改说明 |
|---|---|---|---|
| 1 | **P0-①**<br>断言与现场 cdb 设备数耦合 | [`tests/test_batch_import_and_limits.py:194-257`](file:///F:/GitHub/python/tests/test_batch_import_and_limits.py#L194-L257) | • 头部加入 `pytest.importorskip("tkinter")`，保障无头 CI 环境不挂红；<br>• 在 `tmp_path` 下注入独立的合成 cdb 数据库（3 台 Type=1 使能 + 1 台 Type=1 禁用 + 2 台 Type=2 使能 + 1 台 Type=3 模块）；<br>• 显式传参 `ModbusStudioAppCore(restore_session=False)` 并强制指定 `app.db_path = str(db_file)`，阻断从 `session_state.json` 继承绝对路径；<br>• 断言彻底与现场数据解耦：`assert len(sel_type1) == 3` 且全量校验 `enable == 1 and app_type == 1`；同时补充覆盖 Type=2 使能全选（2 台）校验。 |
| 2 | **P0-②**<br>E2E 导入依赖现场文件与静默回退 | [`tests/test_full_ui_import_e2e.py:41-72`](file:///F:/GitHub/python/tests/test_full_ui_import_e2e.py#L41-L72) | • `app = ModbusStudioAppCore(restore_session=False)` 阻断会话继承；<br>• cdb 路径支持 `os.environ.get("HEMS_TEST_CDB")` 环境变量注入（默认回退 `extra/hems.cdb`），文件不存在时调用 `pytest.skip("Hems cdb not available: ...")`；<br>• 剔除原 `target_app = apps[0]` 静默回退：严格查找首台带点表的 Type=1 设备，未找到则 `pytest.skip`；若该设备点表少于 2 行亦显式 `pytest.skip`，消除数据脆弱性。 |
| 3 | **P1-③**<br>EXE 冒烟测试无构建产物跳过守卫 | [`tests/test_exe_runner.py:15-38`](file:///F:/GitHub/python/tests/test_exe_runner.py#L15-L38) | • 新增模块级守卫：`if not os.path.exists(EXE_PATH): pytest.skip("EXE 构建产物缺失，请先执行 PyInstaller 构建", allow_module_level=True)`，干净 checkout / CI 环境下平稳 skip，不抛断言失败；<br>• 恪守红线：未改动 `.gitignore`，未在测试内引入构建流程。 |
| 4 | **P1-④**<br>测试隐式继承 session_state.json 绝对路径 | [`tests/test_batch_import_and_limits.py`](file:///F:/GitHub/python/tests/test_batch_import_and_limits.py)<br>[`tests/test_full_ui_import_e2e.py`](file:///F:/GitHub/python/tests/test_full_ui_import_e2e.py) | • 凡测试中实例化 `ModbusStudioAppCore` 处，统一显式指定 `restore_session=False` 并立即重设 `app.db_path`，彻底与宿主机 `session_state.json` 的 `last_db_path` 解耦。 |

---

## 二、合成 Fixture 规格与 Skip 守卫触发条件

### 1. P0-① 合成 CDB Fixture 规格 (`tests/test_batch_import_and_limits.py`)
- **存储路径**：`tmp_path / "synthetic_hems.cdb"`（测试生命周期结束后由 pytest 自动清理）；
- **数据结构与设备清单**：
  | Id | Type | 名称 (中/英) | enable | 角色分类 | 预期全选命中行为 |
  |---|---|---|---|---|---|
  | 1 | 1 | 英博PCS / PCS_1 | 1 | 南向物理采集设备 (使能) | `Type=1` 选中 |
  | 2 | 1 | 高压电池箱 / BMS_1 | 1 | 南向物理采集设备 (使能) | `Type=1` 选中 |
  | 3 | 1 | 双向电表 / METER_1 | 1 | 南向物理采集设备 (使能) | `Type=1` 选中 |
  | 4 | 1 | 备用PCS(停用) / PCS_DISABLED | 0 | 南向物理采集设备 (禁用) | **不选中** |
  | 5 | 2 | 北向数据转发 / FORWARD_NORTH | 1 | 北向业务模块 (使能) | `Type=2` 选中 |
  | 6 | 2 | 云端协同服务 / CLOUD_SYNC | 1 | 北向业务模块 (使能) | `Type=2` 选中 |
  | 7 | 3 | 本地能量管理策略 / LOCAL_ENERGY | 1 | 其他业务模块 (使能) | 仅 `all` 视图显示 |
- **断言目标**：
  - `all` 模式：单选首项 (`len <= 1`)；
  - `Type=1` 模式：精确断言 `len(sel_type1) == 3`，选中集合严格为 `{1, 2, 3}`，每个元素断言 `enable == 1 and app_type == 1`；
  - `Type=2` 模式：精确断言 `len(sel_type2) == 2`，选中集合严格为 `{5, 6}`，每个元素断言 `enable == 1 and app_type == 2`。

### 2. Skip 守卫触发条件与语义对照表
| 文件 | 守卫位置 | 触发条件 | 预期行为与原因 |
|---|---|---|---|
| `test_batch_import_and_limits.py` | 函数入口 | `pytest.importorskip("tkinter")` | 在未安装图形环境或 Linux 无头 CI 中优雅 skip，避免 `ModuleNotFoundError: No module named '_tkinter'` |
| `test_full_ui_import_e2e.py` | 步骤 1 | `not os.path.exists(db_path)` | 未提供 `HEMS_TEST_CDB` 且仓库未放置 `extra/hems.cdb` 时 skip，避免现场数据缺失导致挂红 |
| `test_full_ui_import_e2e.py` | 步骤 3 | `not target_app` 或 `len(pts) < 2` | 数据库内无 Type=1 或目标设备有效点表行数不足 2 行时 skip，避免断言伪阳性 |
| `test_exe_runner.py` | 模块顶层 | `not os.path.exists(EXE_PATH)` | 未构建 `dist/ModbusStudio.exe` 时模块级 skip，与 `.gitignore` 交付策略完全正交 |

---

## 三、改动文件核对 (`git diff --stat`)

```text
$ git diff --stat
 tests/test_batch_import_and_limits.py | 68 +++++++++++++++++++++++++++++++----
 tests/test_exe_runner.py              | 11 +++---
 tests/test_full_ui_import_e2e.py      | 16 +++++----
 3 files changed, 79 insertions(+), 16 deletions(-)
```

**红线核对确认**：
- 变动仅限 `tests/` 下允许的 3 个测试文件；
- 零 `modbusstudio/` 源码变动；零现场 `hems.cdb` 与 `session_state.json` 变动。

---

## 四、验证输出与可复现性证明

### 1. 三个目标测试文件执行输出
```powershell
$ .venv\Scripts\pytest.exe tests/test_batch_import_and_limits.py tests/test_full_ui_import_e2e.py tests/test_exe_runner.py -q
.......                                                                  [100%]
7 passed in 4.25s
```

### 2. 脱离宿主机特定数据可复现性证明 (PROMPT §2 验收第 3 条)

#### 验证 A：临时移走 `session_state.json` 测试 P0-①
即使宿主机无 `session_state.json` 或记录为任意现场绝对路径，用例均独立稳定通过：
```powershell
$ Rename-Item session_state.json session_state.json.tmp
$ .venv\Scripts\pytest.exe tests/test_batch_import_and_limits.py -k test_db_import_dialog_default_selection_policy -q
.                                                                        [100%]
1 passed, 4 deselected in 1.08s
$ Rename-Item session_state.json.tmp session_state.json
```

#### 验证 B：通过 `HEMS_TEST_CDB` 环境变量指向缺失路径测试 P0-②
环境变量指定不存在的路径时，触发守卫平稳跳过，无 `AssertionError` 挂红：
```powershell
$ $env:HEMS_TEST_CDB = "non_existent_db.cdb"
$ .venv\Scripts\pytest.exe tests/test_full_ui_import_e2e.py -q
s                                                                        [100%]
1 skipped in 0.26s
$ Remove-Item Env:\HEMS_TEST_CDB
```

### 3. 全量测试回归基线验证
包含任务 13 新增的 `test_hub_unpack_guard.py`（4 项通过）在内，全量 pytest 100% 通过：
```powershell
$ .venv\Scripts\pytest.exe -q
..............................s......................................... [ 80%]
.................                                                        [100%]
88 passed, 1 skipped in 18.35s
```

### 4. 记忆协议防失真校验门禁
```powershell
$ .venv\Scripts\python.exe memory/verify.py
========================================================================
Memorix 共享记忆防失真自动验证报告 (memory/verify.py)
========================================================================
总体结论    : PASS (错误: 0, 警告: 10)
  - 10_pytest_execution: PASS
```

---

## 五、验收准备与下一步

- [x] 所有测试修改落盘，`test_db_import_dialog_default_selection_policy` 挂红彻底修复；
- [x] 3 个关键测试文件 7 passed，全量测试 88 passed, 1 skipped，基线不回归；
- [x] 完成可复现性实证与 skip 守卫有效性验证；
- [x] 严格未执行 `git push`，等待 Claude Code 最终复核验收。
