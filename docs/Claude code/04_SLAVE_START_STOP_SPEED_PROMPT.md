# 从机"全部启动 / 全部停止 / 窗口关闭"提速优化 — 执行 Prompt

> 目标仓库：`F:\GitHub\python`，修改方：Antigravity
> 验收方：Claude Code（检查 + 验收）
> 日期：2026-10-10
> 关联需求：用户反馈"从机侧全部启动连接时较慢、关闭（全部停止 / 点 X 关闭窗口）也较慢"

---

## 背景

从机侧启动/停止链路：

- **启动**：`ui/slave_panel._start_all_slaves` → 逐个 `services.SlaveService.start_slave`
  → `engine.start()` → `ModbusServerHub.register_and_start_slave`
  → 该物理端点没有共享 `ModbusTransportServer` 时**新建** `server.start()`
  → 主线程同步等待 listener 就绪（见下）。
- **停止**：`_stop_all_slaves` / `app.py` `on_closing` → `app.shutdown()`
  → 逐个 `stop_slave` → `engine.stop()`
  → `hub.unregister_and_stop_slave`：最后挂载的从机被 detach 时，同步执行 `server.stop()`
  （`fut.result(timeout=2.0)` + `thread.join(timeout=3.0)`）。

## 根因（Claude 已定位，请复核）

1. **启动等待循环是固定盲等**（`modbus_engine.py` `ModbusTransportServer.start()`）：
   ```python
   for _ in range(30):
       if self._server:
           break
       time.sleep(0.03)   # 未就绪则满等 ~0.9s，已就绪也要等 0.03s 一拍
   ```
   TCP 的 `loop.create_server()` 在后台线程里通常是**毫秒级**就绪的，但主线程被这段
   循环拖住；"全部启动"按从机**串行**执行 `start_slave`，每命中一个新端点就多等
   ~0.9s（N 个端点 ≈ N × 0.9s）。
2. **停止路径每个端点串行等待**（`stop()`）：`fut.result(timeout=2.0)`（正常清理通常
   瞬间完成，但仍有同步等待语义）+ `thread.join(timeout=3.0)`（线程要跑完 finally
   才退出）。"全部停止"与"窗口关闭"都是**逐个从机**串行触发 `stop()`，M 个端点
   最坏 ≈ M × 5s。
3. **`_start_all_slaves` / `_stop_all_slaves` / `shutdown()` 全在主线程循环**，
   UI 在等待期间完全卡死（"慢"的直接体感来源）。
4. `ModbusSlaveEngine.stop()` 末尾的 `self._sim_thread.join(timeout=1.0)`：
   仿真线程 1s 才醒一次（纯 `time.sleep(1)`），且停止标志检查在睡**前**，
   最坏每个从机白等 ~1s——M 个从机 = M 秒的纯睡眠浪费。

> 以上均为设计判断（已读代码定位），Antigravity 动手前请先复核，发现与现状不符
> 以代码为准并在报告里说明。

---

## 任务（改动范围限定）

### 修改 1：`ModbusTransportServer` 启动等待改为事件驱动（modbus_engine.py）

- `__init__` 增加 `self._ready = threading.Event()`；`stop()` 末尾 `_ready.clear()`
  并重置（确保可重复 start）。
- 后台线程 `_run_server_loop` 内：`self._server` 创建并 `asyncio.create_task` 之后，
  立即 `self._ready.set()`；线程异常崩溃路径 `_ready.set()`（避免主线程永久挂起）。
- `start()` 中删除 30×0.03s 盲等循环，改为：
  ```python
  self._ready.wait(timeout=1.0)
  if not self._server:
      logger.warning(f"ModbusTransportServer {self.endpoint_key} 1s 内未就绪")
  ```
  正常情况下就绪耗时从 ~0.9s 降到毫秒级；异常场景 1s 超时兜底、不阻塞更久。
- **保持既有行为不变**：`is_healthy()` 的 0.3s 启动宽限逻辑、半死重建分支、
  日志措辞均不动。

### 修改 2：`ModbusTransportServer.stop()` 缩短等待上限（modbus_engine.py）

- `fut.result(timeout=2.0)` → `fut.result(timeout=1.0)`；
  `self._thread.join(timeout=3.0)` → `self._thread.join(timeout=1.0)`。
- 正常路径（`_cleanup()` 瞬间完成、线程跑完 finally 即退出）耗时不受影响；
  仅把**僵死场景**的最坏等待从 ~5s 压到 ~2s。超时日志保留。
- 该改动对"全部停止"、"窗口关闭"、`stop_all()`、`restart_if_broken` 全部生效。

### 修改 3：`ModbusSlaveEngine.stop()` 仿真线程退出改为事件驱动（modbus_engine.py）

- `_sim_thread` 关联一个 `threading.Event`（如 `self._sim_stop_evt`）；
  `_run_simulation_loop` 由 `time.sleep(1)` 改为 `self._sim_stop_evt.wait(1)`，
  退出条件改为 `not self._sim_running or self._sim_stop_evt.is_set()`。
- `stop()` 中 `self._sim_thread.join(timeout=1.0)` 前 `self._sim_stop_evt.set()`，
  join 超时压到 0.3s；正常路径下仿真线程被立即唤醒退出，不再空等 1s。

### 修改 4：`_start_all_slaves` / `_stop_all_slaves` 移出主线程（ui/slave_panel.py）

- 两个方法保持同步签名不变，但将"逐个 start/stop + 失败收集"整段包进
  `threading.Thread(..., daemon=True).start()`，主线程立即返回（UI 不卡）。
- 成功/失败回执（`self._update_status_indicator`、`messagebox` 弹窗）必须通过
  Tk 线程安全的 `self.app.root.after(0, callback)` 回传（请查看该面板拿到
  root 窗口的现有属性名，以现状为准；没有则用捕获 `self._master`/`winfo_toplevel()`
  之类现有方式，**不要**新造全局变量）。
- 防止重复点击：方法内加"同方向进行中则直接 return"的守卫标志
  （如 `self._busy = {"start": False, "stop": False}`，线程 finally 释放）。
- 线程内失败提示仍需弹出（经 `after` 回主线程弹 `messagebox`）。

### 修改 5：`app.py` `shutdown()` 的从机逐个停止移出主线程（app.py）

- `for dev ... stop_slave(dev.id)` 这段最坏 M×2s（修改 2 之后）的循环放进
  后台线程；`poll_service.disconnect()` 与 `logging_service.close()` 等其余
  轻量收尾仍在主线程完成（不阻塞关闭）。
- 后台线程完成前允许 `root.destroy()` 继续执行（daemon 线程随进程退出即可，
  不要 `join` 主线程导致再次卡顿；如需保证 stop 一定执行完，加 2s 的
  `threading.Event.wait(2.0)` 上限，超时则记录 warning 后继续 destroy）。

### 修改 6：测试补充（tests/）

1. **测试文件 `tests/test_slave_start_stop_speed.py`（新建）**：
   - 用例 A（启动等待不盲等）：新建 `ModbusTransportServer(("TCP","127.0.0.1",PORT))`
     后 `start()`，同一线程内 `time.time()` 计时，断言耗时 < 0.4s（正常 TCP
     监听器毫秒级就绪；旧 0.9s 盲等必失败），随后 `stop()` 清理。
   - 用例 B（停止快速）：start 后立刻 `stop()`，计时断言 < 1.5s（修改 2 后
     上限 1.0+1.0，正常远低于）。
   - 用例 C（仿真线程快速退出）：构造一个最小 `ModbusSlaveEngine`
     （TCP，临时端口，`start()` 后 `stop()`），断言 `stop()` 总耗时 < 0.6s
     （修改 3 后 sim 线程被立即唤醒，旧实现最坏 ~1s）。
   - 三个用例共用临时端口（如 5097），每个用例内自清理（stop + `ModbusServerHub`
     单例残留 server 通过 `hub._servers` 显式清，或用 `unregister_and_stop_slave` 正常路径）。
2. **既有用例 `tests/test_multi_station_concurrency.py::test_cross_endpoint_non_blocking_during_stop`**
   保持通过（01 修复的验收用例，语义不变：跨端点读写不被 stop 无辜阻塞）。
3. 不新增、不修改 `slave_panel` / `app.py` 的 UI 手工用例（Tk 线程调度在
   pytest 环境不可靠；改动 4/5 通过全量回归 + 人工冒烟验收，见验收标准 3）。

> 用例 A/B/C 的计时阈值若在你机器上因环境波动误伤，允许放宽到 0.5s/1.8s/0.8s，
> 但**不得低于**保证"已显著快于旧实现"的下限（旧实现 A≈0.9s、C≈1s）。

### 修改 7：报告输出（docs/Antigravity/）

按 01/02/03 既有格式输出 `docs/Antigravity/04_SLAVE_START_STOP_SPEED_REPORT.md`，
包含：改动文件清单 + 逐条对应本 prompt 修改条目、pytest 全量输出、
计时类用例的实际耗时数字（便于验收对照）。

---

## 硬约束（务必遵守）

1. **不破坏已修 6 项缺陷与 02/03 成果**：
   - `MultiStationServerContext` 的 `address_base` 显式化、FC2/FC4 写保护、
     双锁收敛、`super().__init__(devices={0: dummy_dev})` 一律不动；
   - `POLL_CONFIG` 相关（`PollTask.interval_ms`、`_continuous` 循环）不动；
   - 03 的 `[SLAVE-RECONNECT]` 探活链路（`is_healthy` / `restart_if_broken` /
     3b 复用前探活 / `unregister_and_stop_slave` 锁外停）不动；
   - `ModbusTransportServer.start()` 的半死重建分支与日志措辞保留（仅替换其中
     的等待循环为修改 1 的 Event 等待）。
2. **线程安全**：`ModbusTransportServer` 的所有新增状态（`_ready` 等）只被
   "主线程调用方 + 后台 loop 线程"访问；`threading.Event` 本身线程安全，
   不要在 `self._lock`（Context 双锁）范围内做 Event 操作，避免锁语义变化。
3. **不改函数签名**：`start()` / `stop()` / `register_and_start_slave` /
   `unregister_and_stop_slave` / `start_slave` / `stop_slave` 的签名与返回
   语义不变（外部 41 个既有用例依赖它们）。
4. **UI 改动最小**：`slave_panel.py` 只动 `_start_all_slaves` /
   `_stop_all_slaves` 两个方法（及必要的守卫标志初始化，放在 `__init__` /
   `_build_ui` 附近合适处）；工具条、按钮、其他面板一律不动。
5. **构建产物不提交**（`build/`、`dist/`、`__pycache__`、`.venv`）。
6. 提交信息用中文，Conventional 风格，例：
   `fix: 从机全部启动/停止提速——启动等待改事件驱动，停止超时压缩，UI 停启移出主线程`。

## 验收标准（Claude 将逐条核验）

1. `pytest tests -q` 全量通过（基线 41 + 新增 3 = 44，0 回归）；
   计时用例给出实测数字。
2. 逐条代码核验（grep/读码）：
   - `start()` 中 30×0.03s 循环已移除，替换为 `Event.wait` 且带 1s 超时兜底；
   - `stop()` 两个超时已压至 1.0s；
   - `_run_simulation_loop` 用 `Event.wait(1)` 可唤醒退出；
   - `slave_panel` 两方法体在后台线程执行，回执走 Tk `after`；
   - `app.py shutdown` 的从机循环在后台线程。
3. 手工冒烟（Antigravity 报告里给出操作记录，Claude 复核）：
   - 3 个不同端点（如 TCP 5091/5092/5093）从机：点"⚡ 全部启动"，
     体感 < 1s 且 UI 不卡；点"⏹ 全部停止"体感 < 2s；直接点窗口 X
     关闭，窗口响应不冻结（后台 stop 允许在进程退出前补完）。
4. `test_cross_endpoint_non_blocking_during_stop` 仍然通过（01 语义不回归）。

## 交付节奏

改完 → 跑 pytest → 写报告 → 停。Claude 读 `docs/Antigravity/04_…REPORT.md`
+ 独立核验后给出 PASS/FAIL。
