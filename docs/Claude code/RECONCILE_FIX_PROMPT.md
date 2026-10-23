# RECONCILE_FIX — 任务 12 一期缺陷修复 PROMPT（给执行方：Antigravity）

> 归属：Claude Code（约束/需求方）。执行方：Antigravity，改完代码后在
> `python/docs/Antigravity/RECONCILE_FIX_REPORT.md` 出 REPORT。
> 验收人：Claude Code。
> 依据：`TASK12_PHASE1_REVIEW_REPORT.md`（Antigravity 审查）+ 我方 code-review
> 技能逐条实证复核（3 条缺陷全部属实，另揪出 2 处报告漏检的关联点）。

## 0. 缺陷清单（按严重度排序，全部已实证）

### P0-① FLOAT64 桥接崩溃（功能失真，最高优先）
- **现象**：业务层 `DataType.FLOAT64`（models.py:226，值 `"FLOAT64"`）直接喂
  底层 `ModbusDataType("FLOAT64")` 抛 `ValueError`（底层成员名是
  `DOUBLE64`，modbus_codec.py:76，全表无 FLOAT64）。
- **后果**：`reconcile.py:82` 抛错被 :87 的 `except Exception` 静默吞掉，
  FLOAT64 点位永不走真实寄存器编解码往返，raw/scaled 全退化为 `pt.value`
  兜底口径——对账结果无声失真。真实入口：CDB 的 "Double" 经
  `DataType.normalize`（models.py:277）归一为 FLOAT64。
- **修法约束**：在 `point_value.py` 集中提供 `to_codec_type()` 桥接
  （`{"FLOAT64": ModbusDataType.DOUBLE64}`，其余同名直通），
  `reconcile.py:82` 与 `point_value.py:48` 两处**必须复用同一函数**
  （现在两处各自内联，修一处漏一处是根因）；不要碰 modbus_codec.py。

### P0-② 同端口多 Unit ID 从机被端口预检误杀（数据缺失）
- **现象**：`reconcile.py load_slaves` 走 `SlaveService.restore_devices`，
  内部 `get_occupied_tcp_ports()` 包含本批已注册端口，第 2 台同端口
  从机起被判"端口已占用"跳过（实测 3 台同端口不同 unit_id 只恢复 1 台），
  点表整体缺席对账；>100 台时配额检查还会静默砍尾（services.py:382）。
- **修法约束**：对账是**只读静态还原**，不走服务层。`load_slaves` 改为
  直接逐台 `SlaveDevice.from_dict(item)`（models.py 纯领域模型，无网络、
  无配额、无端口预检），坏条目收集进 warnings 不中断。**禁止**为绕开
  端口检查去改 services.py 的 `restore_devices`/`get_occupied_tcp_ports`
  ——那是任务 11 的既有行为（带 UI 存档恢复要拦截真冲突），红线。

### P0-③ 字符串 scale 静默脏值（污染比对结果）
- **现象**：`scale="bad"` 时 Python 序列乘法不抛错：`100 × "bad"` =
  300 字符垃圾串。`point_value.py:64` 的 `except (TypeError, ValueError)`
  拦不住；且 `reconcile.py:86` **主路径裸乘根本没走 except**
  （审查报告只定位了兜底行，漏了主路径——本次一并修）。
- **修法约束**：`scale_value()` 入口显式
  `isinstance(point.scale, (int, float))` 判定（bool 视为合法数值可放行
  或拒绝，二选一并写注释说明），非数值 scale 原样返回；
  `reconcile.py:86` 主路径改用 `scale_value(raw_value, pt)`，全链路
  唯一出口。

### P1-④ 测试盲区（修复必须带回归，否则重构可无声回退）
- 补用例：FLOAT64 点位走通 encode→decode→scale（用 DOUBLE64 编解码
  实测 4.25×2.0）；同端口 3 台从机 `load_slaves` 全量恢复；
  `scale="bad"` 三条路径（主路径/兜底/直接调 scale_value）均原样返回。
- 现状：18 项测试里 grep FLOAT64 零命中，①③修复无 CI 拦截。

### P1-⑤ BOOL 跨类型吸收（低优先，顺手修）
- `diff_with_cloud` 中 `float(True)=1.0` 会把云端 0/1/1.0005 都吸收成
  OK，布尔点位事实性错误查不出来。修法：`is_scalable` 为 False 的点位
  一律走严格相等分支（不进 float 容差），加 1 项测试。

### P2-⑥ JSON 分支性能（可选，不阻塞合入）
- `reconcile.py:188` 每个云端 key 全表线性扫描找 slave 名，
  建一次 `{point_key: slave_name}` 字典预索引即可；大存档（200×200）
  下才有感，改不改都行，改了要说明。

## 1. 红线（不要越界）

- 不碰 `modbus_codec.py`（底层 "FLOAT64" 字符串兼容 band-aid 留在原处
  不动，本次只在 tools/ 层收口映射）；
- 不碰 `services.py` 既有方法（`restore_devices`/`get_occupied_tcp_ports`
  是任务 11 行为，对账侧绕开而不是改）；
- 不碰 `memory/verify.py`、`archive/`、`*.legacy.py`；
- 不杀用户正在运行的 ModbusStudio 进程；
- 改动范围：`modbusstudio/tools/point_value.py`、
  `modbusstudio/tools/reconcile.py`、`tests/test_reconcile.py`
  （+ 可选 P2 仅 reconcile.py）。**零** core 改动。

## 2. 验收标准（Claude Code 复核点）

- [ ] P0 三条 + P1 两条逐条有对应新测试且全绿；
- [ ] `pytest tests/test_reconcile.py -q` 与全量 `pytest -q` 通过（贴原始输出）；
- [ ] `python -m modbusstudio.tools.reconcile --session dist/session_state.json`
      实跑贴 3 个点位（含 1 个 FLOAT64 若存档有）表格输出；
- [ ] REPORT（`RECONCILE_FIX_REPORT.md`）含：每条缺陷→改了什么→文件:行号
      + 新增测试清单 + P2 是否做及理由；
- [ ] `git diff --stat` 证明改动仅限 §1 列出的 3 个文件。

## 3. 与 Gemini 审查的边界

Gemini 已按 `RECONCILE_REVIEW_PROMPT.md` 出过一轮意见，本 PROMPT 是
实证复核后的**修复**任务，只认本文缺陷清单；Gemini 报告中与本文
矛盾处，以本文（附代码行号证据）为准。
