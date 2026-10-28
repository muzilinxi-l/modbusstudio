# UNPACK_FIX — 任务 13 启动异常 "too many values to unpack" 修复 PROMPT（给执行方：Antigravity）

> 归属：Claude Code（约束/需求方）。执行方：Antigravity，改完代码后在
> `python/docs/Antigravity/13_UNPACK_FIX_REPORT.md` 出 REPORT。验收人：Claude Code。
> 依据：Claude Code 代码扫描（modbus_engine.py 全文核对）+ 最小脚本实证复现
> （已逐字复现 `ValueError: too many values to unpack (expected 3)`，与 GUI 弹窗文案一致）。

## 0. 缺陷描述（已实证）

### 现象
GUI 弹窗：`启动异常: too many values to unpack (expected 3)`。
传播链：`services.py:606` `engine.start()` → `modbus_engine.py:1208`
`hub.register_and_start_slave` → 循环解包 RTU 键崩溃 → 异常上抛被
`services.py:615` `except Exception` 兜住 → `services.py:622` 返回
`f"启动异常: {e}"` → GUI 弹窗（只有短消息、无堆栈，因为中间被这一层吞掉）。

### 根因
`ModbusServerHub.make_endpoint_key`（modbus_engine.py:902-929）产生**异构键**：

| 协议 | 键形状 | 示例 |
|---|---|---|
| TCP | 3 元组 | `("TCP", "127.0.0.1", 15502)` |
| RTU | **6 元组** | `("RTU", "COM1", 9600, 8, "N", 1)` |

三处通配复用循环 `for (c, h, p), s in self._servers.items()` 对**所有**键
无条件按 3 元组解包：

- **modbus_engine.py:956** `register_and_start_slave`（精确 `get` 未命中且新键
  是 TCP 时，遍历所有已有键做 0.0.0.0 通配匹配）；
- **modbus_engine.py:1003** `unregister_and_stop_slave`（同一逻辑复制份）；
- **modbus_engine.py:1047** `get_server_for_engine`（同一逻辑第三份）。

`key[0] == "TCP"` 只挡了"当前新键是 RTU"，**挡不住字典里的旧键是 RTU**。
触发条件：字典中已存在至少一台 RTU 从机键（它启动时把 6 元组键塞进
`_servers`）之后，任何 TCP 从机启动/停止/健康探测走到"精确键未命中"分支即崩。

### 最小复现（验证脚本，不启动监听）
```python
hub = ModbusServerHub.get_instance()
k = hub.make_endpoint_key(comm_type="RTU", serial_port="COM1", baudrate=9600)
hub._servers[k] = object()          # 模拟已存在的 RTU server
for (c, h, p), s in hub._servers.items():
    pass
# → ValueError: too many values to unpack (expected 3)
```

## 1. 修法约束

- **P0（必做）**：三处循环统一加**键形状守卫**——只处理
  `len(s_key) == 3 and s_key[0] == "TCP"` 的键（写法：`for s_key, s in
  self._servers.items(): if len(s_key) == 3 and s_key[0] == "TCP": ...`，
  循环体改用 `s_key[0]/s_key[1]/s_key[2]` 索引式访问，与
  `services.py:405` `get_occupied_tcp_ports` 的现有索引式风格保持一致）。
  三个方法 956/1003/1047 **必须同步改**（同一逻辑三份拷贝，漏一份仍复发）。
  允许且推荐：把这段通配匹配收敛成一个私有辅助方法（如
  `_find_reusable_tcp_server(self, port, host) -> Optional[ModbusTransportServer]`），
  三处调用它；若选择内联守卫，三处写法必须逐字一致。
- **P1（必做）**：在 `tests/` 下补回归测试，至少覆盖：
  1. 字典内混存 RTU 键 + TCP 键时，`register_and_start_slave`（精确未命中
     的通配分支）、`unregister_and_stop_slave`、`get_server_for_engine`
     三方法均不抛 `ValueError`；
  2. 纯 RTU 键字典中调用 `get_server_for_engine`（TCP 引擎反查）正常返回
     `None` 而非崩溃；
  3. 既有 TCP 通配复用行为不回退：`("TCP","0.0.0.0",502)` 在字典中时，
     注册 `("TCP","127.0.0.1",502)` 仍复用同一 server（用替身对象占位
     `_servers`，**不启动真实监听、不占真实端口、不建串口**）。
- **P2（明写不改）**：`services.py` 的 `get_occupied_tcp_ports`（:396-410）
  用的是 `ep[0]=="TCP" and len(ep)>=3` 索引式访问，**不受本 bug 影响**，
  禁止顺手"统一"它的写法。

## 2. 红线（不要越界）

- 改动范围仅限：`modbusstudio/modbus_engine.py`（仅上述三处循环/新增的
  辅助方法）+ `tests/` 下新增或扩展的测试文件。零 `services.py` 改动、
  零 UI 改动、零 `modbus_codec.py` 改动；
- 不动 `memory/verify.py`、`archive/`、`*.legacy.py`、注释掉的旧代码；
- 不杀用户正在运行的 ModbusStudio 进程（ModbusStudio.exe / python main.py）；
- 不 push——提交后由 Claude Code 验收通过再推。

## 3. 验收标准（Claude Code 复核点）

- [ ] `git diff --stat` 证明改动仅限 §2 允许的文件；
- [ ] `pytest tests/<新测试文件> -q` 全绿（贴原始输出），并全量
      `pytest -q` 通过（既有 84 passed, 1 skipped 基线不回归）；
- [ ] REPORT（`docs/Antigravity/13_UNPACK_FIX_REPORT.md`）含：
      每条修法→改了什么→文件:行号 + 新增测试清单 + 三处是否同步收敛的
      说明（若选内联守卫，贴三处代码证明逐字一致）；
- [ ] 复现脚本（§0 最小复现）修复后跑一遍贴输出：不再抛 ValueError。

## 4. 已知无关项（防误修）

- `dist\extra\` 下没有名为"异常"的文件，该目录当前仅
  `hems.cdb`（现场 CDB）；"启动异常"四个字来自弹窗错误信息文案，
  不是路径，不要去找或生成任何"异常"文件；
- 本缺陷与任务 12 对账（reconcile/point_value）无关，禁止把
  `modbusstudio/tools/` 下的文件纳入本次改动。
