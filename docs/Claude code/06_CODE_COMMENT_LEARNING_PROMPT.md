# PROMPT：为 Modbus Studio 2.0 项目添加完整代码注释（Python 学习向）

> 用途：发给 **Antigravity** 执行。
> 执行完成后在 `docs/Antigravity/06_CODE_COMMENT_LEARNING_REPORT.md` 出报告，Claude 复核。

---

## 一、目标

给 `modbusstudio/` 目录下**所有 .py 文件**添加**中文详细注释**，让编程新手（Python 学习者）能**逐行读懂**。
注释目标：**解释"为什么"和"这块在做什么"**，而不是复述代码本身（"做了什么"注释是废话）。

## 二、注释规则（严格遵守）

### 1. 语言
- 所有注释用**简体中文**；标识符、函数名、类名保留英文原样。

### 2. 注释粒度（由粗到细）
1. **模块级 docstring**：每个 .py 文件顶部一个 `"""..."""`，写清：
   - 这个文件是干嘛的（1 句话）
   - 关键导入是什么（解释为什么导入它们）
   - 对外暴露的主要类/函数（谁调用、什么时候调用）
2. **类级 docstring**：每个类头部 `"""..."""`，写清：
   - 职责（1 句话）
   - 关键成员变量的含义（逐条列出）
   - 典型使用场景
3. **函数/方法 docstring**：每个 `def` 前用 `"""..."""`，含：
   - 一行概述（动词开头，说明做什么、为什么）
   - 参数说明（每行 `param_name: 含义 + 类型 + 约束`）
   - 返回值说明（类型 + 边界/异常时返回什么）
   - 关键步骤拆解（3~7 行，每步一行注释说明"为什么这么做"）
   - 副作用（哪些外部状态被改、哪些事件被发、哪些日志被写）
4. **行内注释**：对**非显而易见**的逻辑（位运算、asyncio 锁、线程 join、异常分支、状态机转换、
   回调线程切换、超时兜底等）必须加 `#` 行内注释，解释**意图**而非"做了什么"。
   例：`# 防重入：同一 endpoint 两次 start() 时跳过，避免起两个 asyncio loop`
5. **复杂条件/循环**：`if` 嵌套 > 2 层、`while` 带多个退出条件、`try/except` 捕获多类异常的块，
   在块前加 2~3 行"为什么这么写"的注释，或把整块抽出来写成带 docstring 的小函数。

### 3. 术语统一（照项目约定）
- EMS（能源管理系统）、BMS（电池）、PCS（储能变流器）、一体机（现场 Linux ARM 设备）
- 从机 = Modbus slave，主机 = Modbus master，端点 = endpoint，站点 = station

### 4. 不做什么（红线）
- **不改任何业务代码**（函数体、类定义、import、常量、测试断言都不动）；
- **不改函数签名/返回值**；
- **不改 `archive/` 目录**（那是历史快照，回退依据）；
- **不改 `memory/verify.py` 的 11 项检查项**（可加 docstring，但不动逻辑）；
- **不删任何现有注释**（已有的 `# [SLAVE-RESTART]` 等标记要保留）；
- **不顺手重构**（哪怕注释让代码显得笨拙，也别改，只加注释）。

## 三、执行范围与文件清单

按重要性排序，逐个处理。每个文件加完注释后跑一次 `python -c "import <模块>"` 验证不报错。

| 顺序 | 文件 | 行数 | 重点注释内容 |
|---|---|---|---|
| 1 | `modbusstudio/app.py` | 178 | 应用启动/关闭流程、slave 停止移到后台线程 + `done_evt.wait(timeout=2.0)` 兜底 |
| 2 | `modbusstudio/models.py` | 356 | 数据模型（Point/Slave/Endpoint 等）字段含义、序列化约定 |
| 3 | `modbusstudio/modbus_codec.py` | 458 | 4 种字节序（ABCD/CDAB/DCBA/BADC）的 `struct` 编码/解码、int16 负数补码、uint16、float32、bool |
| 4 | `modbusstudio/modbus_engine.py` | 1363 | **核心**：`ModbusTransportServer` 事件驱动启停（`_ready`/`_sim_stop_evt`）、`MultiStationServerContext` 按 device_id 路由、`ModbusServerHub` 单例、`ModbusSlaveEngine` 仿真线程、半死重建 `[SLAVE-RESTART]` 分支、`POLL_CONFIG` 轮询循环 |
| 5 | `modbusstudio/services.py` | 650 | 服务层（DataService、SlaveService、PollService 等）职责与调用关系 |
| 6 | `modbusstudio/hems_db_loader.py` | 481 | 现场 .db 数据库的读取与 ENERGY 导入逻辑、字段映射、异常处理 |
| 7 | `modbusstudio/hems_pairing.py` | 202 | 配对规则、引擎加载 |
| 8 | `modbusstudio/ui/main_window.py` | 414 | Tkinter 主窗口、主题切换、面板组合 |
| 9 | `modbusstudio/ui/slave_panel.py` | 986 | 从机面板、`_busy` 防重入、`_start_all_slaves`/`_stop_all_slaves` 后台线程 + `root.after(0, ...)` 回主线程 |
| 10 | `modbusstudio/ui/poll_panel.py` | 400 | 轮询面板、`POLL_CONFIG` 用户交互 |
| 11 | `modbusstudio/ui/dialogs/db_import_dialog.py` | 605 | DB 导入对话框、默认选中策略、批量导入与限制 |
| 12 | `modbusstudio/ui/dialogs/pairing_dialog.py` | 167 | 配对对话框 |

## 四、注释风格示例

**模块 docstring 示例**：
```python
"""modbus_engine.py — Modbus TCP/RTU 从机引擎（核心）。

本文件实现"一端口多站点"的从机服务端：
  - ModbusTransportServer: 管理单个物理端点（IP+端口/COM）的 asyncio 服务
  - MultiStationServerContext: 同一端口按 device_id 路由到不同从机实例
  - ModbusServerHub: 单例，集中管理所有端点的 server 生命周期
  - ModbusSlaveEngine: 单个从机实例（含仿真线程、轮询、重启探活）

关键设计：
  - 启动等待用 threading.Event（_ready）而非固定 sleep 轮询，
    避免 30×0.03s 盲等导致启动慢
  - 停止用 _sim_stop_evt 立即唤醒仿真线程，避免 1s sleep 才能退出
  - UI 面板调用 start/stop 时走 daemon 线程，完成后 root.after(0) 回主线程
    更新状态指示器，避免 UI 卡顿
"""
```

**函数 docstring 示例**：
```python
def start(self):
    """启动该端点的 Modbus 服务（事件驱动，通常 <5ms 返回）。

    职责：
      若 server 尚未健康或线程已死，重建 asyncio loop + serve_forever()；
      否则直接复用；等待 _ready 事件被设置（最多 1s），超时时打 warning 但继续。

    为什么这么设计（对照旧实现）：
      旧版本在 start() 里 `for _ in range(30): time.sleep(0.03)` 盲等 0.9s，
      导致"全部启动"3 个端点累计 2.7s；改成事件驱动后 4ms 级返回。

    副作用：
      - 可能创建/重建 self._thread（守护线程）
      - 可能重新创建 self._loop（asyncio）
      - 在 _run_server_loop 完成后调用 self._ready.set() 唤醒等待方

    线程安全：
      本方法假设调用方（UI 面板）已用 _busy 守卫防止重入。
    """
```

**行内注释示例**：
```python
# 半死检测：线程还活着但 asyncio loop 已关/serve_forever 已退出 → 重建
if self.is_healthy() and self._thread.is_alive():
    logger.debug("[SLAVE-RESTART] already running, skip")
    return
```

## 五、验收标准（Antigravity 报告里必须逐条对应）

1. 12 个文件全部加完注释，每个文件顶部都有模块级 docstring。
2. 所有 `def`/`class` 都有 docstring（可用 `pydoc` 或 `ast` 抽查 3 个文件验证覆盖率）。
3. 复杂逻辑块（asyncio 启停、线程 join、asyncio lock、状态机转换、UI 主线程回调）
   都有行内"为什么"注释。
4. `python -m pytest tests -q` 全量通过（基线 44 passed，0 回归，注释不改变任何行为）。
5. `python memory/verify.py` 通过（脚本通过 ≠ 语义正确，但必须不报新错）。
6. 注释密度：每个 .py 文件注释行数 ≥ 30%（注释行 / 非空行 比例），核心文件（modbus_engine.py）≥ 40%。
7. 术语统一：全文搜不到 "slave" 单独出现（应为"从机"），搜不到 "master" 单独出现（应为"主机"），
   "endpoint" 可保留英文。
8. 报告路径：`docs/Antigravity/06_CODE_COMMENT_LEARNING_REPORT.md`，含
   - 每个文件的注释前后 diff 行数统计
   - 4 个代表性代码片段（注释前 vs 注释后）
   - pytest 全量输出
   - "学习者阅读建议"（按 12 个文件的顺序，每个 2~3 行"应该先理解什么"）

## 六、给学习者的阅读顺序建议（报告里要含这一段）

```
1. app.py → 看应用怎么起来的（启动/关闭）
2. models.py → 看数据长什么样（Point/Slave/Endpoint）
3. modbus_codec.py → 看字节怎么编解码（最"底层"，4 种字节序要画个表）
4. modbus_engine.py → 看核心引擎（分 4 大块读：TransportServer / Context / Hub / SlaveEngine）
5. services.py → 看服务层怎么把引擎包给 UI 用
6. hems_db_loader.py → 看现场 .db 怎么读
7. hems_pairing.py → 看配对规则
8. ui/main_window.py → 看主窗口组装
9. ui/slave_panel.py → 看从机面板 + _busy 防重入 + 后台线程
10. ui/poll_panel.py → 看轮询面板
11. ui/dialogs/db_import_dialog.py → 看对话框细节
12. ui/dialogs/pairing_dialog.py → 看配对对话框
```

每个文件建议"先读 docstring → 再读类 → 再读函数 → 最后读行内注释"，
用 VS Code 把注释行高亮（黄色）看一遍，比直接读代码快 3 倍。

---

## 七、给 Claude Code 的复核指令（报告写完后，Claude 独立验证）

1. 抽查 3 个文件（modbus_engine.py、services.py、slave_panel.py）：
   - 模块/类/函数 docstring 覆盖率 ≥ 95%
   - 核心逻辑块（`_ready.set()`、`_sim_stop_evt.wait(1)`、`root.after(0, ...)`）行内注释完整
2. 跑 `python -m pytest tests -q`，确认 44 passed 0 回归
3. 跑 `python memory/verify.py`，确认无新增错误
4. 统计注释密度：`grep -cE '^\s*#' *.py` 与总行数对比，确认核心文件 ≥ 40%
5. 检查"不改业务代码"：`git diff` 只看注释行变化，函数体/签名/import 无改动
6. 复核结论写到 `docs/Claude code/06_CODE_COMMENT_LEARNING_RECHECK.md`，
   标注"通过 / 打回"及理由

---

## 八、硬性约束

- 不修改 `archive/`、`memory/`、`tests/`、`examples/` 目录
- 不修改任何函数签名、返回值、import 语句
- 不删除现有注释（只追加/完善）
- 不推远程
- 敏感信息（API Key、Token、密码）不写进注释
- 完成后告知 Claude 报告路径，等 Claude 复核，不要自己声明"已通过"
