# 任务：Modbus Poll 轮询间隔可配置化

> 目标仓库：`F:\GitHub\python`。
> 本任务与 `REVIEW_FIX_PROMPT.md`（6 项缺陷修复）相互独立，**不依赖、不冲突**。
> 保持最小改动，不动 UI 其他区域、不动 HEMS/导入逻辑、不动已修好的 6 项缺陷。
> 每处改动加注释 `# [POLL-CONFIG] 说明`。

## 需求

当前 Poll（主机轮询）"连续轮询"路径写死 1 秒（`ui/poll_panel.py:315` 的 `self.after(1000, ...)`），
轮询间隔不可改。用户希望**在 Poll 面板加一个"间隔(ms)"输入框，让用户自定义轮询间隔**，
并让该间隔真正生效。

关键事实（动手前已确认）：
- `models.py` 的 `PollTask` **已有 `interval_ms: int = 1000` 字段**（models.py:322），数据结构早预留好了，
  只是 UI 与 `ModbusPollEngine.start_polling` 没用它。
- 底层 `ModbusPollEngine.start_polling(area, start_addr, count, interval_ms)`（engine.py:1214）
  **已支持传入 `interval_ms`**，但 UI 没走这条路径（UI 走的是 `PollService.read_registers` 单次读 + `after(1000)` 循环）。
- 目标：**UI 改用底层 `start_polling` 做连续轮询，间隔来自输入框**。

---

## 必须先修的底层隐患（`start_polling` 直接拿来用会踩坑）

在改 UI 之前，先修 `modbus_engine.py` 里 `ModbusPollEngine` 的 3 个问题，否则切到
`start_polling` 会引入新 bug：

### 隐患 1：`stop_polling` 的 `join(timeout=1.0)` 可能留下"双线程轮询"
**位置**：engine.py:1233-1238
- 现状：`self._poll_thread.join(timeout=1.0)`，超时后直接把 `self._poll_thread = None`，
  但旧 worker 线程**可能仍在跑**（比如某次 `read_block` 在 RTU 慢链路上 >1s）。
  下次 `start_polling` 会再起一条，旧线程继续空读，造成**双重并发读写**。
- **改法**：
  1. 引入 `self._poll_stop_event = threading.Event()`。
  2. `start_polling` 起线程前 `self._poll_stop_event.clear()`。
  3. `_poll_worker` 的 `while` 条件改成 `while self._is_polling and not self._poll_stop_event.is_set()`；
     每轮末尾 `time.sleep(self._poll_interval)` 前检查 event。
  4. `stop_polling` 先 `self._is_polling = False; self._poll_stop_event.set()`，再
     `self._poll_thread.join(timeout=2.0)`；若 `join` 后线程仍 `is_alive()`，记 warning
     但**不强行置 None 让旧线程"消失"**——保留引用到线程真正结束，避免双线程。
     简单做法：`self._poll_thread.join(timeout=2.0)` 后若仍 alive 记 warning，`self._poll_thread = None`
     可保留（旧线程因 event 已 set 会自行退出）。

### 隐患 2：`_poll_worker` 里读的是 `self.cached_registers`，可能拿到旧值
**位置**：engine.py:1221-1231
- 现状：`success, vals, err = self.read_block(...)`；`read_block` 内部（engine.py:1116-1117）
  确实把新值写进了 `self.cached_registers`，**但**是**累加**式（只更新读到的地址，旧地址残留）。
  回调 `self.on_poll_success(self.cached_registers)` 传的是整个 dict 的引用，其中可能残留
  之前读到、本次没读到的旧地址值。
- **改法（二选一，选更稳的）**：
  - 方案 A（推荐）：`start_polling` 维护本地 `fresh: Dict[int,int]`，每轮 `read_block` 成功后
    用 `vals` 重建 `fresh`（遍历 `start_addr..start_addr+count-1`），回调 `on_poll_success(fresh)`。
  - 方案 B：在 `read_block` 成功分支，先清空/重建 `self.cached_registers` 再写本次值。
  选 A，改动面小且不改变 `read_block` 对其他调用方的副作用。

### 隐患 3：`on_poll_success`/`on_poll_error` 由 worker 线程直接调用，UI 回调须切主线程
**位置**：engine.py:1224-1227
- 现状：worker 线程里直接 `self.on_poll_success(...)`。Tkinter **不是线程安全**的，
  UI 若在回调里直接 `tree.insert(...)` 可能崩或假死。
- **改法**：`start_polling` 的回调机制保持不变（仍在 worker 线程调 `on_poll_success`），
  **线程切换放到 UI 层**：UI 侧设置回调时包一层 `self.after(0, lambda r=res: self._on_poll_result(r))`。
  在 engine.py 的 docstring 里注明「`on_poll_success` 由后台线程调用，UI 消费者须自行切主线程」。
  （不强行在 engine 里绑定 Tk，保持 engine 与 UI 解耦。）

---

## 业务层（services.py）：新增连续轮询入口

**位置**：`modbusstudio/services.py` 的 `PollService`（约 472 行起）。

当前 `PollService` 只有单次 `read_registers(task)`。新增：

```python
def start_continuous_polling(self, task: "PollTask", interval_ms: int) -> Tuple[bool, str]:
    """启动后台连续轮询，间隔 = interval_ms。需已连接（或自动连接）。"""
    if self._engine is None:
        ok, msg = self.connect(task.conn_config)
        if not ok:
            return False, f"连接失败: {msg}"
    if self._engine.is_polling:
        return False, "已在轮询中"
    # 把 engine 的 on_poll_success/on_poll_error 接到一个内部队列，UI 从队列取
    ...
    self._engine.start_polling(
        area=task.area.value,
        start_addr=task.start_address,
        count=task.count,
        interval_ms=interval_ms,
    )
    return True, "轮询已启动"

def stop_continuous_polling(self) -> None:
    if self._engine:
        self._engine.stop_polling()
```

**架构注意**：`PollService` 不能直接碰 Tkinter。建议做法：
- `PollService` 里加一个线程安全的 `queue.Queue`（或 `collections.deque` + `threading.Lock`）
  `_poll_results`。
- `engine.on_poll_success = lambda regs: self._poll_results.put(regs)`（worker 线程写入，线程安全）。
- UI 侧用一个 `self.after(...)` 定时器（或复用现有机制）**每 ~100ms 轮询一次队列**，
  有结果就 `self._update_result_tree(...)` 并 `self.after(0, ...)` 切主线程渲染。
  这样 UI 渲染永远在主线程，engine 线程只写队列，彻底解耦。

  或者更简单：让 UI 直接把 `engine.on_poll_success` 设成一个 `lambda r: self.after(0, lambda: self._update_from_regs(r))`，
  UI 侧自己切线程。**二选一，选你认为改动最小的**，并在注释里说明。

---

## UI（poll_panel.py）：加输入框 + 改轮询路径

**位置**：`modbusstudio/ui/poll_panel.py`。

1. **加"间隔(ms)"输入框**：
   - 在连续轮询按钮旁加一个 `ttk.Entry`（或 `ttk.Spinbox`），绑定 `self.poll_interval_var = tk.IntVar(value=1000)`。
   - 校验：最小 100ms，最大 60000ms；非法/越界时给个 `ttk` 提示或 clamp 到边界。
   - 放在现有 UI 布局的合理位置（不要破坏高 DPI 自适应，沿用现有 `padx=2, pady=1` 风格）。

2. **改 `_toggle_poll_loop`**（engine.py:299）：
   - 按下（开始）：读 `int(self.poll_interval_var.get())`，构造 `PollTask`（`is_loop=True, interval_ms=该值`），
     调 `self.app.poll_service.start_continuous_polling(task, interval_ms)`；
     **不要再用 `self.after(1000, self._run_poll_cycle)` 的旧循环**（`_run_poll_cycle` 可保留但不再调用，
     或直接删掉 `_run_poll_cycle` 与 `_poll_timer_id` 旧路径——**推荐删除**，避免两条轮询路径并存）。
   - 抬起（停止）：调 `self.app.poll_service.stop_continuous_polling()`。

3. **改回调渲染**：
   - `_update_result_tree` 现在接收 `PollResult`。改成能从 `cached_registers`（dict）或
     `PollResult` 渲染（看 services 侧回调传的是什么）。若传 dict，加个 `_update_from_regs(regs, start, count, byte_order)`。

4. **按钮文案**（可选）：`"🔁 连续轮询 (1s)"` 改成 `"🔁 连续轮询"`（去掉写死的 1s，因为现在可配）。

---

## 通用约束

- 只改 `modbus_engine.py`（仅 `ModbusPollEngine` 段）、`services.py`（仅 `PollService` 段）、
  `ui/poll_panel.py`、必要时 `models.py`（`PollTask` 已有 `interval_ms`，一般不用动）。
- **不改** 已修好的 6 项缺陷相关代码（engine.py 里 Slave 侧的 `MultiStationServerContext` /
  `ModbusTransportServer` / `ModbusServerHub` / `ModbusSlaveEngine`）。
- 每处改动加 `# [POLL-CONFIG] 说明` 注释。
- 全绿：`.venv\Scripts\python.exe -m pytest tests -v`。
- 注意：`test_exe_launch_and_window_zoomed` 是环境 flake（依赖打包 EXE + 窗口尺寸），
  单跑该文件即 PASS；全量跑时偶发失败属正常，不阻塞本任务。

## 验收标准（审查方核验）

1. `grep -n "after(1000" modbusstudio/ui/poll_panel.py` 无结果（旧写死 1s 循环已移除）。
2. `start_polling` 用 `threading.Event` 控制退出，`stop_polling` 不再裸 `join(timeout=1.0)` 后丢弃线程引用。
3. UI 有"间隔(ms)"输入框，`poll_interval_var` 默认 1000，有上下限 clamp。
4. `PollService.start_continuous_polling(task, interval_ms)` / `stop_continuous_polling()` 存在，
   且 UI 的连续轮询走的是这条路径（不再是 `after(1000)` 循环）。
5. 回调线程切换：UI 渲染发生在主线程（engine worker 线程只写队列 / 调 `after(0)`），无跨线程 Tk 操作。
6. `pytest tests` 全绿（除 `test_exe` 偶发 flake 外无新失败）；不回归 6 项已修缺陷的 4 个并发用例。
7. 手动验证：Poll 面板填 200ms 点"连续轮询"，日志/刷新节奏确实约 200ms；填 3000ms 则约 3s。
   （若环境无真实从机，至少验证输入框 → 回调拿到正确的 `interval_ms` 值这条链路。）
