# RECONCILE_REVIEW — 点位换算与对账工具代码审查 PROMPT（给 Gemini）

> 归属：Claude Code（约束/需求方）。执行方：Gemini（只审查，不改代码）。
> 审查报告请输出到 `python/docs/Antigravity/RECONCILE_REVIEW_REPORT.md`（沿用现有归档目录）。

## 0. 背景

任务 12（一期）：把 ModbusStudio 缺失的"寄存器 → 解码 → 乘 scale 系数"换算链
补上，并做无网络、无 GUI 的点位对账 CLI（本地基准值 vs 云端 dump 值）。
实现已合入 main 分支：

- `735e4e3` feat: 点位换算与对账核心（新增 3 文件 + 17 项测试）
- `d91a4f2` fix: 兜底分支坏 scale 二次抛错 + 测试子进程管道编码

**请审查已实现代码，不要重新实现。** 二期 EMQX/MQTT 接入另有
`MQTT_RECONCILE_PROMPT.md`，不在本次审查范围内。

## 1. 审查对象（全部在 `F:/GitHub/python` 仓库）

| 文件 | 角色 |
|---|---|
| `modbusstudio/tools/point_value.py` | 换算核心（纯函数） |
| `modbusstudio/tools/reconcile.py` | 对账 CLI（`python -m modbusstudio.tools.reconcile`） |
| `modbusstudio/tools/__init__.py` | 子包声明 |
| `tests/test_reconcile.py` | 18 项测试 |

用 `git show 735e4e3` / `git show d91a4f2` 看完整 diff。

## 2. 需求基线（逐条判断满足与否）

1. **换算链补全**：`encode_value` → `decode_value` → `× scale`；scale 字段
   此前有落盘但运行时零消费者，本次必须让数值型点位真正生效。
2. **类型策略**：只有 INT16/UINT16/INT32/UINT32/FLOAT32/FLOAT64 参与
   scale 换算；STRING/BOOL/HEX16 原样返回（乘系数无意义）。
3. **对账 CLI**：从 `session_state.json` 恢复点表（走 `SlaveService.restore_devices`
   通道），输出本地基准值（raw 解算值 + scaled 换算值）；可选
   `--cloud-file`（`{point_key: 云端值}` 的 JSON）按 `--tolerance` 逐点比对；
   退出码 0=一致 / 1=有差异（含缺失、云端多）/ 2=环境错误。
4. **红线**：纯本地计算、无网络、无 tkinter；遵守任务 11 约定
   "恢复从机绝不自动启动监听"（`restore_devices` 不触发 `start_slave`）。
5. **不改既有核心**：`modbus_codec.py` 的 `decode_value` 内部、
   `services.py` 既有方法不得被本次改动波及。

## 3. 重点核对清单（每项给结论）

- [ ] **point_key 一致性**：`point_key()` 生成的 `4x_HoldingRegister|0`
      是否真与 `session_state.json` 的 `to_dict` 序列化键、以及未来
      MQTT payload 约定同构。抽查 `dist/session_state.json` 实际键。
- [ ] **换算正确性**：`encode_value → decode_value` 往返是否对
      FLOAT32 CDAB 字序、UINT32 跨双寄存器无损？`decode_point` 的
      业务枚举（models.DataType）与底层枚举（codec.ModbusDataType）
      桥接是否所有 9 种类型都对得上？
- [ ] **兜底分支**：`compute_local_rows` 的 except 分支用
      `scale_value()` 收口（commit d91a4f2 修复点），确认坏 scale
      （None/字符串）与值类型不符时既不抛错、也不静默给错值。
- [ ] **diff_with_cloud 边界**：容差比对数值用 `abs(a-b)<=tol`，
      非数值退回严格相等；"缺失"（本地有云端无）与"云端多"
      （云端有本地无）状态是否互不混淆；云端值字符串数字
      （"12.3"）能否被 float() 吸收。
- [ ] **无头构造**：`SlaveService.__new__` + 手工补 `_devices` 的
      绕过方式是否有隐患（restore_devices 内部还引用了哪些属性？
      未来 services.py 改动会不会让这里悄悄断掉？）
- [ ] **越界检查**：`git diff ecf0990..d91a4f2 --stat`，确认除
      tools/、tests/、docs/ 外零改动。
- [ ] **测试质量**：18 项是否覆盖退出码 0/1/2 三条路径、坏 scale、
      CDAB 字序、缺失/云端多；有没有断言写反（比如把 FAIL 当 OK 判）。
- [ ] **可执行性**：`python -m modbusstudio.tools.reconcile --session
      dist/session_state.json` 实跑一遍，确认退出码 0、表格正常。

## 4. 验收命令（报告里贴结果）

```powershell
cd F:/GitHub/python
.venv/Scripts/python.exe -m pytest tests/test_reconcile.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m modbusstudio.tools.reconcile --session dist/session_state.json
echo "退出码: $?"
```

（注意：不要杀用户正在运行的 ModbusStudio 进程。）

## 5. 输出格式（REPORT 要求）

`docs/Antigravity/RECONCILE_REVIEW_REPORT.md`：
1. 逐项核对清单结果（通过 / 问题）；
2. 发现的问题按严重度排序：**缺陷只列清单，不直接改代码**
   （含 文件:行号 + 现象 + 建议修法）；
3. 验收命令原始输出；
4. 一段总体结论（可合入 / 需修复后复核 / 有红线违反）。
