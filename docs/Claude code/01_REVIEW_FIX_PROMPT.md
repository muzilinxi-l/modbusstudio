# 任务：Modbus Studio 多站并发架构 — 代码修复 Prompt

> 复制本文件全文交给负责执行修改的 AI 工具（Claude Code / Cursor / Codex 等）。
> 目标仓库：`F:\GitHub\python`，核心文件 `modbusstudio/modbus_engine.py`。
> 修改完成后，由审查方按文末「验收标准」逐项核验。

---

## 背景

Modbus Studio 2.0 是一个 Modbus 多从机仿真工作站。本次重构引入了「Server Hub 模式」：
同一物理端点（TCP 的 `IP:Port`，或 RTU 的串口+波特率组合）上可并发挂载多个不同 Unit ID
（站地址）的逻辑从机，底层共享一个物理传输层（`ModbusTransportServer`），通过
`MultiStationServerContext` 按 Unit ID 路由读写。

本任务要修复审查发现的 6 处高/中风险缺陷（2 高危 + 4 中危）。**请保持最小改动，
不要重构未点名的部分，不要引入无关依赖，不要改 UI。** 每处改动都要写清「为什么」，
并用注释标注是本次修复。

---

## 必读上下文（动手前先读）

1. `modbusstudio/modbus_engine.py`（全文，尤其 `MultiStationServerContext`、`ModbusTransportServer`、`ModbusSlaveEngine`、`ModbusServerHub`）
2. `modbusstudio/services.py`（`SlaveService.start_slave` / `stop_slave`，冲突判定逻辑）
3. `tests/test_multi_station_concurrency.py`（必须继续全绿）
4. 根目录 `MULTI_STATION_ARCHITECTURE.md` 第六章「供第三方 AI Review 审查的重点核验清单」
5. 数据偏移基准：`modbusstudio/models.py` 里的 `ModbusPoint` / 点位地址定义，确认点表地址是 0-based 还是 1-based 声明的。

---

## 缺陷 1（高危）：0/1-based 地址偏移靠「数据值是否非 0」判断

**位置**：`modbus_engine.py` 中 `MultiStationServerContext.async_getValues` 与 `async_setValues`
（当前实现在 `start_idx` 计算处，逻辑形如：）

```python
start_idx = address
if address >= 1 and address < len(values_list):
    if values_list[address] == 0 and values_list[address - 1] != 0:
        start_idx = address - 1
```

**问题**：
- 用**运行时数据内容**（某寄存器当前是否 == 0）来决定读/写落到哪个索引，是反模式。
- 后果：某格恰为 0、前一格非 0 时，读 10 号地址会错位落到 9 号格；且波形仿真会让值
  持续变化，**同一地址两次读可能落到不同格**，数据不稳定、难复现。
- 地址基准（0-based 还是 1-based）本应由点表/数据块**显式声明**，不应靠看值推断。
- 多寄存器读（如 FLOAT32 = 2 个寄存器）整块会错位 1 格，导致读出的浮点值被「撕裂」。

**要求**：
1. 在 `ModbusSlaveEngine` 或对应的数据块结构上，引入**显式的地址基准属性**
   （例如 `address_base: int`，0 或 1），或沿用数据块已有元数据；默认值应与现有测试期望一致。
2. `async_getValues` / `async_setValues` 的 `start_idx` 一律改为：
   `start_idx = address - address_base`（base=0 时原样，base=1 时减 1），**删掉「看值判断偏移」的整段逻辑**。
3. 边界保护：`start_idx` 越界时，读返回 0/False 填充（与现有行为一致），写越界忽略，**不要抛异常导致连接挂掉**。
4. 同步修改：若 `modbusstudio/models.py` 或 `services.py` 里有向数据块写入点表的入口，
   确认它们写入时使用的索引与新的 `address_base` 基准一致（即"界面写 10 号点 = 写入数据块正确位置"）。
5. 跑 `pytest tests/test_multi_station_concurrency.py -v` 必须全绿；如现有测试对偏移行为有
   隐式依赖（比如测试里就靠那个 hack 才能过），**更新测试使其不再依赖数据值**，并注释说明。

**完成判据**：连续对同一地址读写多次、且底层值在变，结果索引恒定；多寄存器读（FLOAT32）不被撕裂；
无任何「看 values_list 值」来决定 `start_idx` 的代码路径。

---

## 缺陷 2（高危）：`async_setValues` 允许写只读区（FC4 / FC2）

**位置**：`modbus_engine.py` 中 `MultiStationServerContext.async_setValues`。

**问题**：
- `area_str` 按 `func_code in (1,5,15)→Coil, 2→DiscreteInput, 4→InputRegister` 选择，
  然后对 `values_list[idx]` 直接赋值。
- 按 Modbus 协议，**FC2（读离散输入）和 FC4（读输入寄存器）是只读功能码**，主站永远不会
  用它们发写请求。当前代码没有对 FC2/FC4 写路径做任何保护/拒绝，任何把写请求映射到
  FC4 的路径都会**静默篡改用户从未要求修改的输入寄存器区**，破坏数据一致性。

**要求**：
1. 在 `async_setValues` 开头加功能码白名单：只允许 FC5/FC6/FC15/FC16 走写路径；
   对 FC2/FC4（以及任何只读 FC）直接返回 `None`（no-op）或 `ExcCodes`，并在日志里
   记录一条 warning（便于排查非法写来源），**不修改数据区**。
2. 保持现有正常写路径（FC5/6/15/16）行为不变。
3. 补一个测试：对某从机用 FC4 调用 `async_setValues` 应不改变 3x 区数据。

**完成判据**：`async_setValues` 对 FC2/FC4 是 no-op；正常写路径回归测试全绿。

---

## 缺陷 3（中危）：`stop()` 资源回收不彻底

**位置**：`modbus_engine.py` 中 `ModbusTransportServer.stop()`（当前实现只 `join(timeout=1.0)`，
取消 task 后不等待、不 `loop.close()`）。

**问题**：
- `self._loop.call_soon_threadsafe(self._server_task.cancel)` 后没有等 task 真正结束，
  也没有 `self._loop.close()`；反复启停同一端点会累积未关闭的事件循环。
- `join(timeout=1.0)` 超时后仍继续往下走，`self._server` 可能仍被后台线程持有，存在竞态。
- 后果：同端点反复 start/stop 时，新 server 可能撞上旧 loop/socket 未释放，
  **复现文档声称已修掉的 `[WinError 10048]`**。
- `ARCHITECTURE.md` 第六章第 2 点明确自认此处未达标。

**要求**：
1. 在 `stop()` 中，取消 task 后**在事件循环线程内等待 task 结束**（例如提交一个
   `wait_done` 协程并阻塞等待，或用 `threading.Event` 在循环退出时 set）。
2. 在确认 server task 结束后调用 `self._loop.close()`，并把 `self._loop` / `self._server` 置 `None`。
3. `join` 超时后：明确记录 warning，并保持对象状态一致（`_is_running=False`、引用已清），**不要留半清理状态**。
4. 保证「同端点反复 start/stop」N 次后无泄漏的 loop 句柄（可在测试里断言线程数不增长）。

**完成判据**：停止后 `self._loop is None`、`self._server is None`、后台线程已结束；无 `asyncio` 未关闭 loop 的告警；
反复启停同端点不出现 `[WinError 10048]`。

---

## 缺陷 4（中危）：`MultiStationServerContext` 与 `ModbusTransportServer` 双份状态 + 双锁，嵌套加锁未文档化

**位置**：`modbus_engine.py` 中
- `MultiStationServerContext`：`self._slaves`、`self._devices`、`self._lock`
- `ModbusTransportServer`：又一份 `self._slaves`、又一个 `self._lock`
- `attach/detach_slave`：Transport 层先拿自己的锁，再调用 `server_context.attach/detach`，
  后者内部**再拿一次 context 的锁** → 两把不同的锁保护语义重叠的状态。

**问题**：
- 同一份「端点下的从机集合」被两处独立维护，极易漂移（一处改一处没改）。
- 嵌套加锁顺序（先 Transport 锁 → 再 Context 锁）未文档化，是潜在死锁点。

**要求**（二选一，择最小改动方案，并注释说明选了哪种及理由）：
- **方案 A（推荐）**：让 `ModbusTransportServer` **不再自己维护 `_slaves`**，统一委托给
  `MultiStationServerContext`；`attach/detach/active_slave_count` 全部直接调 context，
  **避免 Transport 持锁时再进 context 加锁**（嵌套）。即：Transport 层锁只保护 Transport 自己
  的状态（如 `_is_running`），从机集合由 Context 单锁管理，锁序明确「Context 锁在最内层、独立持有」，
  且禁止在持有某锁时获取另一把。
- **方案 B**：若保留双份，必须**文档化锁偏序**（例如固定 `Transport 锁 → Context 锁`），
  并保证所有路径都遵守，不得出现反向获取。

**完成判据**：不存在「持有锁 X 时获取锁 Y，又存在某路径持有锁 Y 时获取锁 X」的环；从机集合状态来源唯一。

---

## 缺陷 5（中危）：`ModbusServerHub.unregister_and_stop_slave` 在全局锁内做 `server.stop()` 的 1s join

**位置**：`modbus_engine.py` 中 `ModbusServerHub.unregister_and_stop_slave`（约 529 行附近）。

**问题**：
- 当前实现持有 Hub 的**全局单例锁** `self._lock` 期间调用 `server.detach_slave` 和 `server.stop()`
  （后者含 1s 的线程 join）。所有其他在跑从机的线程（UI 主线程、波形仿真线程、`register_and_start_slave`）
  都会在这把全局锁上阻塞最长 1s。
- 在「停掉某端点最后一个从机 → 同端点再启新从机」的场景下，这是一次可感知的 UI 卡顿，
  且 stop 若更慢（见缺陷 3）会接近死锁窗口。

**要求**：
1. 把 `detach_slave` 和 `stop()` 挪到**每个 server 自己的锁/状态**下执行，
   Hub 全局锁只保护「端点 → server」映射表的增删，不保护 server 内部状态。
2. 保证：stop 某端点最后一个从机时，其他端点的从机读写不受 1s 阻塞影响。
3. 补一个测试（或断言）：A 端点 stop 期间，B 端点的 `async_getValues` 仍能在 <100ms 内返回。

**完成判据**：`unregister_and_stop_slave` 不在持有 Hub 全局锁的状态下调 `server.stop()`；
跨端点读写延迟不受单端点 stop 影响。

---

## 缺陷 6（中危）：`MultiStationServerContext.__init__` 未调 `super().__init__()`，依赖 pymodbus 私有分发

**位置**：`modbus_engine.py` 中 `MultiStationServerContext.__init__`（约 242 行）。

**问题**：
- 当前 `__init__` 手工设置 `self.old_simulator = True; self.simdevices = []; self._devices = {}`，
  **没有调用 `super().__init__()`**。我核对过当前安装的 pymodbus 运行时，请求确实经由
  `server/base.py` 的 `context.old_simulator` 分支分发到自定义 context，所以现在能跑。
- 但这是**依赖 pymodbus 私有实现细节**：一旦 pymodbus 升级改变了 `old_simulator` 的选择逻辑，
  所有请求会静默停止到达 `async_getValues`/`async_setValues`，症状是「设备连上但读不到数据」，极难排查。

**要求**：
1. 在 `__init__` 里补 `super().__init__()`（先调用父类再设 `old_simulator` 等属性，遵循 pymodbus
   当前版本的正确初始化顺序；若当前版本父类签名需要参数，按 pymodbus 源码查清再传）。
2. 在注释里写明「本处依赖 pymodbus `old_simulator` 私有分发，升级前需回归本 context 行为」，
   并建议在 `docs/CHANGELOG.md` 加一条提醒。
3. 保持 `old_simulator = True` 等既有行为不变，只补 `super().__init__()`。

**完成判据**：`MultiStationServerContext.__init__` 有 `super().__init__()` 调用；现有 34 个测试仍全绿；
注释里写明了对 pymodbus 私有分发的依赖与升级回归要求。

---

## 通用约束

- 只改 `modbus_engine.py`、必要的 `models.py` / `services.py` 点表写入入口、以及
  `tests/test_multi_station_concurrency.py`（仅当测试隐式依赖了缺陷 1/2 的旧行为时）。
- **不改** UI（`modbusstudio/ui/`）、`app.py` 装配、HEMS 导入逻辑。
- 每处改动上方加一行注释：`# [REVIEW-FIX-1..6] 说明`，便于审查方定位。
- 全部改完后：`.venv\Scripts\python.exe -m pytest tests -v` 必须全绿，不得新增失败。
  注：`test_exe_launch_and_window_zoomed` 属于**偶发 EXE/窗口状态 flake**（基线 34/34 全绿，
  单跑可能因窗口状态 33/34），该用例**不在本次修复范围内**，若它失败请重跑一次确认
  是否为架构问题；反复失败才上报，不要求修它。
- 输出一份简短「改动摘要」：改了哪些函数、每处对应缺陷编号、测试是否全绿、
  `test_exe_launch_and_window_zoomed` 是否因 flake 失败（是/否）。

## 提交策略（分两批，勿一次全改）

改动面从数据正确性 → 资源回收 → 锁粒度 → 兼容性，风险不同，请**分两次提交**，
每批各自跑一次全量 `pytest tests` 确认绿了再进入下一批，便于回归定位：

- **第 1 批（数据正确性 + 资源）**：缺陷 1、2、3 —— 修完跑 `pytest tests -v`，全绿后再做第 2 批。
  第 1 批不影响锁结构与初始化顺序，回归面小，先落地。
- **第 2 批（锁粒度 + 兼容性）**：缺陷 5、6 —— 第 1 批全绿后修；
  改完再跑一次全量 `pytest tests -v`。

> 缺陷 4（Context/Transport 双锁）改动面介于两者之间：若你已顺手把 4 的锁序理清且测试绿，
> 可并入第 1 批；否则放到第 2 批与 5、6 一起，避免同时动多处锁导致死锁难定位。
> 每批结束在改动摘要里写明「第 N 批：修了缺陷 X，pytest 结果」。

## 验收标准（审查方据此核验）

1. `grep -n "values_list\[address\] == 0"` 在 `modbus_engine.py` 中**无结果**（缺陷 1 的看值 hack 已移除）。
2. `async_getValues` / `async_setValues` 的 `start_idx` 由显式 `address_base` 计算，越界安全；
   FLOAT32 等多寄存器读不再被撕裂。
3. `async_setValues` 对 FC2/FC4 为 no-op（缺陷 2），且有对应测试。
4. `stop()` 末尾有 `self._loop.close()` 且 `self._loop = None`（缺陷 3）；`join` 超时有 warning 与状态兜底。
5. 锁：不存在 Transport 锁与 Context 锁的嵌套反向获取（缺陷 4）；`unregister_and_stop_slave`
   不在持有 Hub 全局锁状态下做 `server.stop()` 的 1s join（缺陷 5）。
6. `MultiStationServerContext.__init__` 有 `super().__init__()` 调用（缺陷 6），注释写明对
   pymodbus 私有分发的依赖与升级回归要求。
7. `pytest tests` 全绿（基线 34 passed），`test_multi_station_concurrency.py` 核心用例
   （`test_same_port_three_slaves_concurrency_and_data_isolation`、
   `test_same_port_same_station_collision_rejection`、`test_slave_service_same_port_multi_station_support`）仍 PASS。
8. 改动最小、无 UI 改动、无新依赖、每处带 `[REVIEW-FIX-x]` 注释；
   `test_exe_launch_and_window_zoomed` 若失败须确认是 flake 而非回归。
