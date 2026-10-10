# 任务：Slave TCP 断线后 EMS 重连静默失败 — 自动重连修复 Prompt

> 目标仓库：`F:\GitHub\python`，核心文件 `modbusstudio/modbus_engine.py` + `modbusstudio/services.py`。
> 由 Antigravity 执行修改，由 Claude Code 验收。

---

## 背景与现象

Modbus Studio 2.0 的 Slave 从机侧：软件启动后，物理端口上的 TCP listener
（`ModbusTransportServer`）开始监听，EMS（主站/Poll）通过 Modbus TCP 主动连入。

**Bug 现象**：
- EMS 侧通信中断（网络抖、EMS 重启等），之后 EMS 恢复、携带**完全相同的 IP:Port
  和 Unit ID** 重连 Modbus Studio，**连不上**（TCP connect 直接失败或 60s 超时）。
- 界面上那个从机仍显示"运行中"（假在线），Python 侧**没有任何报错日志**
  （或偶发看到 `[服务异常崩溃]` / `ModbusTransportServer 运行异常 (...)`）。
- 唯一能恢复的办法：**重新导入数据库 + 重新启动整个 Modbus Studio 程序**。

## 根因（Claude 审查已确认）

两层 state 僵死：

1. **`ModbusTransportServer.start()`**（`modbus_engine.py:408`）：
   ```python
   if self._is_running:
       return
   ```
   一旦底层 asyncio listener（`_serve` task）因异常退出、或 socket 半死，
   `self._is_running` 仍停在 `True`，后续任何"重启该端点"的请求都被这条
   **无脑 return** 拦掉。listener 永远起不来。

2. **`SlaveService.start_slave()`**（`services.py:319`）：
   ```python
   if device.is_running:
       return True, f"从机 {device.name} 已经在运行中"
   ```
   上层业务也照 `device.is_running` 放行，不探活，不重建。

两层僵死叠加，只有重启进程能清掉 `_active_engines` / `device.is_running` /
Hub 单例的 `_servers` 映射表。

## 修复目标（用户确认）

**静默自愈**（方案 A）：
- 端点/Unit ID 完全一致，不需要端点迁移。
- 用户无感：EMS 重连时（或在某个周期性探活里），检测到底层 listener 已僵死，
  **自动重建**（起新的事件循环 + 新的 TCP server + 重新挂载该端点上所有已注册
  的从机），让 EMS 下一次 connect 直接成功。
- 无需用户手动重启程序、无需手动重导数据库。

---

## 需要做的修改（按依赖顺序）

### 修改 1：`ModbusTransportServer` 增加健康探测 + 自愈重建（`modbus_engine.py`）

**位置**：`ModbusTransportServer` 类（约 354 行起）。

1. 新增方法 `is_healthy() -> bool`：
   ```python
   def is_healthy(self) -> bool:
       """判断该端点的底层 listener 是否真正在监听."""
       if not self._is_running:
           return False
       # 事件循环线程还活着、loop 还在跑、_server 已被创建
       if not (self._thread and self._thread.is_alive()):
           return False
       if not (self._loop and self._loop.is_running()):
           return False
       if self._server is None:
           # 可能仍在启动过程中，给 0.3s 宽限
           return self._is_running and (time.time() - self._start_ts) < 0.3 if hasattr(self, '_start_ts') else False
       return True
   ```
   为做时间窗判断，`__init__` 里加 `self._start_ts = time.time()`，
   `start()` 成功起线程后刷新 `self._start_ts`。

2. 新增方法 `restart_if_broken() -> bool`：
   ```python
   def restart_if_broken(self) -> bool:
       """若底层 listener 僵死（不健康），停止并重建；返回是否执行了重建."""
       if self.is_healthy():
           return False
       # 僵死：走一次 stop + start，重建 loop / server / 线程
       self.stop()
       # stop 后 _is_running 已清、_loop/_server/_thread 已置 None
       self.start()
       # start 后 5 个已注册的从机（_server_context._slaves）仍需保持挂载，
       # 因为 _server_context 是 self 的属性，未被 stop 清除；
       # 新起的 TCP server 仍用同一个 context，路由表保留，从机数据块保留。
       return True
   ```
   **注意**：`stop()` 会调 `server_context.detach_slave` 吗？——当前实现里
   `stop()` 只 `loop.close()` / `thread.join()` / 引用置 None，**不会 detach 从机**，
   `_server_context._slaves` 保留，这是自愈能"无感重建"的关键。请在实现里
   确认这一点（若 `stop()` 会清 `_server_context`，必须调整为保留从机路由）。

3. 修改 `start()`（当前 408-417）：
   ```python
   def start(self) -> None:
       # 已在健康运行 → 直接返回
       if self.is_healthy():
           return
       # 半死状态（_is_running=True 但底层僵死）→ 清理残留再重建
       if self._is_running:
           logger.warning(f"[POLL/SLAVE-RESTART] {self.endpoint_key} 检测到半死状态，强制重建 listener")
           self._is_running = False
           # 不 detach 从机，只清理 loop / thread / server 引用
           if self._loop:
               try: self._loop.close()
               except: pass
               self._loop = None
           if self._thread:
               self._thread.join(timeout=0.5)
               self._thread = None
           self._server = None
           self._server_task = None
       self._is_running = True
       self._start_ts = time.time()
       self._thread = threading.Thread(target=self._run_server_loop, daemon=True)
       self._thread.start()
       for _ in range(30):
           if self._server:
               break
           time.sleep(0.03)
   ```
   目的：把"健康才 return、半死则清理重建"的判定从"裸 `_is_running`"改成"探活"。

4. 在 `_serve()` 的异常分支（当前 485-488）里，异常导致 `run_until_complete`
   正常返回后，线程退出前把 `self._is_running` 置 False 并打一条 warning：
   ```python
   finally:
       try: self._loop.close()
       except: pass
       self._is_running = False
       # 新增：异常退出时显式提醒，便于日志排查
       if self._server:
           logger.warning(f"ModbusTransportServer listener 已退出 ({self.endpoint_key})，"
                          f"下次 start() 将自动重建")
   ```

**完成判据**：
- 手动构造"健康态"：`start()` 后 `is_healthy() == True`；再次 `start()` 立即 return。
- 手动构造"僵死态"：`stop()` 后 `is_healthy() == False`；`start()` 后 listener 重新起。
- `restart_if_broken()` 在半死时能成功重建，且 `_server_context._slaves` 不丢。

---

### 修改 2：`ModbusSlaveEngine` 增加健康探测（`modbus_engine.py`）

**位置**：`ModbusSlaveEngine` 类（约 616 行起）。

新增 `is_healthy() -> bool`：
```python
def is_healthy(self) -> bool:
    """该从机对应的底层 transport 是否仍正常监听."""
    if not self._is_running:
        return False
    # 找到本从机所在的 ModbusTransportServer（通过 endpoint_key 反查 Hub）
    hub = ModbusServerHub.get_instance()
    server = hub.get_server_for_engine(self)   # 见修改 3
    return server.is_healthy() if server else False
```

---

### 修改 3：`ModbusServerHub` 增加反查接口（`modbus_engine.py`）

**位置**：`ModbusServerHub` 类（约 504 行起）。

1. 新增方法 `get_server_for_engine(engine) -> Optional[ModbusTransportServer]`：
   ```python
   def get_server_for_engine(self, engine) -> Optional[ModbusTransportServer]:
       """按 engine 的端点参数反查该 engine 挂载的 transport server."""
       with self._lock:
           key = self.make_endpoint_key(
               comm_type=engine.comm_type, host=engine.host, port=engine.port,
               serial_port=engine.serial_port, baudrate=engine.baudrate,
               bytesize=engine.bytesize, parity=engine.parity, stopbits=engine.stopbits,
           )
           return self._servers.get(key)
   ```

2. 新增方法 `restart_all_broken() -> int`：
   ```python
   def restart_all_broken(self) -> int:
       """扫描所有端点，重建所有僵死 listener；返回重建数量."""
       restarted = 0
       with self._lock:
           servers = list(self._servers.values())
       for srv in servers:
           if srv.restart_if_broken():
               restarted += 1
       return restarted
   ```
   **注意**：`restart_if_broken` 内部会调 `start()`，而 `start()` 里若需要
   `attach_slave` 重新挂载——但 `_server_context` 是 `self` 的属性未被清，
   所以只要 `_slaves` 还在，重建 listener 就保留路由，无需重挂载。

3. **【关键新增 · 必改】`register_and_start_slave` 复用 server 前先探活**：
   这是"关闭再开启也连不上"的直接根因。当前 `register_and_start_slave`
   （modbus_engine.py:552-556）里：
   ```python
   server = self._servers.get(key)
   ...
   if server is None:
       server = ModbusTransportServer(key)
       self._servers[key] = server
       server.start()
   ```
   Hub 单例的 `_servers` 映射**常驻**，一旦建了 `[key] → server` 就永远复用。
   若该 server 已僵死（stop 过但映射未清），复用后 `server.start()` 又被
   `if _is_running: return` 拦掉 → 永远起不来。改为**复用前探活，僵死则换新**：
   ```python
   server = self._servers.get(key)
   if server is None and key[0] == "TCP":
       for (c, h, p), s in self._servers.items():
           if c == "TCP" and p == key[2] and (h == "0.0.0.0" or key[1] == "0.0.0.0" or h == key[1]):
               server = s
               key = (c, h, p)
               break

   # [SLAVE-RECONNECT-3b] 复用前探活：僵死则丢弃并新建，避免复用已死的 listener
   if server and not server.is_healthy():
       logger.warning(f"[SLAVE-RECONNECT-3b] {key} 已有僵死 listener，重建新实例")
       server = None

   if server is None:
       server = ModbusTransportServer(key)
       self._servers[key] = server
       server.start()
   ```
   **务必保留下方 `server.get_slave(engine.slave_id)` 冲突检查与 `attach_slave`
   不变**——新建的 server 因 `_server_context` 是全新实例，该端点旧的从机
   数据块已随旧 server 丢失；但 `stop_slave` 路径（见修改 4）在清映射前
   会先 `unregister_and_stop_slave` 彻底停旧 server，所以"正常 stop-再-start"
   与"僵死自愈"两条路径都收敛到"干净重建"，语义一致。

---

### 修改 4：`SlaveService` 里探活放行 + 兜底自动重连（`services.py`）

**位置**：`SlaveService.start_slave`（当前 319 行起）。

1. 顶部改 `if device.is_running` 分支为**探活放行**：
   ```python
   if device.is_running:
       engine = self._active_engines.get(device_id)
       if engine and engine.is_healthy():
           return True, f"从机 {device.name} 运行中"
       # 僵死：先彻底 unregister（清 Hub 映射 + 停旧 transport）再清 active，
       # 保证下方 register 走"新建"路径，不会复用僵死实例
       if engine:
           hub = ModbusServerHub.get_instance()
           try:
               hub.unregister_and_stop_slave(engine)   # 清 _servers[key] + stop 旧 transport
           except Exception as ex:
               logger.warning(f"unregister 僵死 engine 失败: {ex}")
           try:
               engine.stop()
           except Exception as ex:
               logger.warning(f"清理僵死 engine 失败: {ex}")
           self._active_engines.pop(device_id, None)
       device.is_running = False   # 让下方注册 / 启动逻辑重跑
   ```
   这样用户点"启动"时，软件会**自动重建**该从机的底层 transport，
   而不是傻放 "已在运行"。配合修改 3b，保证 `register` 阶段不会复用僵死实例。

   **⚠️ 务必同时改 `stop_slave`**（当前 services.py:423）：现有
   `stop_slave` 直接 `self._active_engines.pop(device_id)` + `engine.stop()`，
   但 `engine.stop()` 内部会走 `hub.unregister_and_stop_slave` 清映射——
   正常 stop 路径是通的。问题在于**EMS 断线后底层僵死**，此时 `engine.stop()`
   里的 `unregister_and_stop_slave` 因 `remaining == 0` 会 `self._servers.pop(key)`
   + `server.stop()`，是 OK 的。所以 `stop_slave` 正常路径不用改，
   **但 `start_slave` 里"僵死清理"分支必须调 `hub.unregister_and_stop_slave`**
   （不是只 `engine.stop()`），确保映射被清、不会复用旧实例。

2. 新增方法 `auto_reconnect_if_needed() -> int`（供 UI 的"重连"按钮或定时器调）：
   ```python
   def auto_reconnect_if_needed(self) -> int:
       """对所有在跑从机做一次探活，僵死的自动重建；返回重建数."""
       hub = ModbusServerHub.get_instance()
       n = hub.restart_all_broken()
       if n > 0:
           self.logging_service.post("SYS", 0, f"[自动重连] 检测到 {n} 个僵死 listener，已重建")
       return n
   ```

3. 在 `services.py` 模块顶部 import 里补 `from .modbus_engine import ModbusServerHub`
   （确认是否已导入，避免重复）。

---

### 修改 5：UI 增加"重连"按钮 + 可选自动探活定时器（`ui/slave_panel.py`）

> **精确插入点（Claude 已指认）**：按钮加在 `ui/slave_panel.py` 的 **`right0` 工具条**
> （`_build_ui` 里 `right0 = ttk.Frame(row0)` 那段，约 **93-106 行**），和现有
> `▶️ 启动服务` / `⚡ 全部启动` / `⏹ 全部停止` 同一排（`side="left"`）。
> 在该排末尾（`btn_stop_all` 之后）追加一个 `🔄 自动重连` 按钮即可，
> 沿用现有按钮的 `ttk.Button(right0, text=..., style=..., command=...)` +
> `pack(side="left", padx=2)` 风格，**不要改其他行的布局**。
>
> 方法体加在 `SlavePanel` 类里（如 `_stop_all_slaves` 附近），
> 通过 `self.app.slave_service.auto_reconnect_if_needed()` 调用（app 是
> `ModbusStudioAppCore`，`slave_service` 属性已存在——请确认属性名，
> 若不是 `slave_service` 则用实际装配名，见 `app.py`）。

1. 在 `right0` 末尾加按钮：
   ```python
   btn_reconnect = ttk.Button(right0, text="🔄 自动重连", style="secondary.Outline.TButton", command=self._on_auto_reconnect)
   btn_reconnect.pack(side="left", padx=2)
   ```

2. 新增方法 `_on_auto_reconnect`：
   ```python
   def _on_auto_reconnect(self) -> None:
       n = self.app.slave_service.auto_reconnect_if_needed()
       # 在状态区 self.lbl_slave_status 或日志区提示
       self.lbl_slave_status.config(text=f"已重建 {n} 个僵死 listener" if n > 0 else "未检测到僵死 listener")
   ```

3. （可选，**默认不启用**）加一个 10s 周期的自动探活定时器，
   在用户手动开启后后台自动 `hub.restart_all_broken()`，彻底消除 EMS 重连失败。
   若实现此项，请：
   - 用 `self.after(10000, ...)` 递归调，不阻塞主线程；
   - 加开关按钮（放 `right0` 旁，默认关闭），
   - 日志记录每次重建。

---

### 修改 6：测试（`tests/test_multi_station_concurrency.py` 末尾追加）

新增 3 个用例：

```python
def test_slave_listener_restart_after_stop_restarts():
    """手动 stop 后 start 可重建，从机路由保留."""
    # 起 1 个从机 UID=1 @ 127.0.0.1:5066
    # 验证 health = True
    # stop 该端点的 transport（模拟 EMS 断线导致 listener 退出）
    # 验证 health = False
    # 调 restart_if_broken() 或 start()
    # 验证 health = True，且外部 Modbus TCP Client 读 UID=1 的 holding register
    # 成功返回正确值（证明路由保留、从机数据块未丢）

def test_slave_service_start_slave_heals_broken_state():
    """上层 start_slave 探活放行：僵死 device 不会傻返回 '已在运行'，会自动重建."""
    # 起 1 个从机，mark device.is_running = True
    # 强制把底层 transport 弄成僵死（stop 但不 detach 从机）
    # 调 SlaveService.start_slave(device_id)
    # 应返回 True 且底层 listener 已重建（外部 Client 能连上）

def test_hub_restart_all_broken_counts_correctly():
    """Hub.restart_all_broken 能正确统计并重建所有僵死端点."""
    # 起 2 个端点（不同 IP:Port），各自 1 个从机
    # 手动僵死 1 个，另一个保持健康
    # 调 restart_all_broken() → 应返回 1
    # 验证被僵死的那个恢复健康，另一个不受影响
```

**注意**：测试里"强制弄僵死"不能走 `stop()` 全清（会丢 context），要模拟
"listener 线程死但 context 还在"的状态。做法：直接 `transport._is_running=False`
+ `transport._thread=None`（不 detach 从机），等价于"listener 异常退出"。

---

## 通用约束

- **只改**：`modbus_engine.py`（`ModbusTransportServer` / `ModbusSlaveEngine` /
  `ModbusServerHub` 三处）、`services.py`（`SlaveService` 顶部探活 + 新增
  `auto_reconnect_if_needed`）、从机面板 UI（加按钮，不动 Poll 面板，
  避免与 `POLL_CONFIG_PROMPT.md` 冲突）、`tests/test_multi_station_concurrency.py`
  末尾追加 3 个用例。
- **不改**：`ModbusPollEngine`（Poll 主机轮询）、`POLL_CONFIG` 相关改动、
  已修好的 6 项缺陷（`MultiStationServerContext.async_*`、`stop()`、双锁收敛、
  `super().__init__`）—— 它们与本次"断线重连"互不冲突，但**不要顺手改**。
- 每处改动上方加注释 `# [SLAVE-RECONNECT] 说明`。
- 全部改完跑 `.venv\Scripts\python.exe -m pytest tests -v`，
  须 **38+ passed**（本次追加 3 个新用例），且 6 项缺陷修复的 4 个并发用例不回归。
- `test_exe_launch_and_window_zoomed` 仍属环境 flake，单跑即 PASS，不阻塞。

## 验收标准（Claude 据此核验）

1. `grep -n "def is_healthy\|def restart_if_broken\|def get_server_for_engine\|def restart_all_broken\|def auto_reconnect_if_needed" modbusstudio/modbus_engine.py modbusstudio/services.py`
   全部命中。
2. `start()` 里不再有无脑 `if self._is_running: return`，改为
   `if self.is_healthy(): return`。
3. `_serve` 异常分支 finally 里有 `logger.warning` 记录 listener 退出。
3b. **【必查】`register_and_start_slave` 复用前探活**：
   `grep -n "if server and not server.is_healthy()" modbusstudio/modbus_engine.py` 命中，
   证明复用了僵死 listener 会换新（这是"关闭再开启也连不上"的直接修复点）。
4. `SlaveService.start_slave` 里 `if device.is_running` 分支不再直接返回，
   而是**探活 → 僵死则先 `hub.unregister_and_stop_slave` 清映射 + `engine.stop()` +
   `pop` 再走注册路径**；
   `grep -n "hub.unregister_and_stop_slave" modbusstudio/services.py` 在 start_slave 内命中。
5. `grep -n "self._active_engines.pop\|auto_reconnect_if_needed" modbusstudio/services.py` 命中。
6. UI 从机面板有"🔄 自动重连"按钮（或等效文本），点击调 `auto_reconnect_if_needed`。
7. `tests/test_multi_station_concurrency.py` 有 3 个新用例，全部 PASS。
8. `pytest tests -v` 全绿（≥41 passed：38 基础 + 3 新）。
9. **手动验证**（Claude 负责，不要求 Antigravity 做）：
   - 启一个 Slave UID=1 @ 127.0.0.1:5099；
   - 用 `examples/modbus_master.py`（或临时脚本）连上、读 1 个保持寄存器成功；
   - 手动 `transport._is_running=False; transport._thread=None`（模拟 listener 僵死）；
   - **场景 A（断线自愈）**：直接再连一次 → 应**成功**（`start()` 探活放行 + 3b 换新）；
   - **场景 B（关闭再开启）**：调 `slave_service.stop_slave(device_id)` 再
     `start_slave(device_id)` → 应**成功**（验证 3b + start_slave 清理路径）；
   - 全程不重启 Python 进程。

## 改动摘要（Antigravity 执行后输出，放入 `docs/Antigravity/SLAVE_RECONNECT_REPORT.md`）

- 改了哪些文件 / 哪些函数，对应上面 1~5 哪条修改
- `pytest tests -v` 全量输出
- 手动验证结果（若有环境）：是 / 否 成功自动重连，耗时
- 是否发现与已修 6 项缺陷冲突或需要联动修改的地方（若有，列出）
