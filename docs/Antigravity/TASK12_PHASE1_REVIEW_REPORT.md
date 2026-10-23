# 代码审查报告：任务 12 一期（换算链 + 对账 CLI）

> **审查对象**：ModbusStudio (`F:/GitHub/python`, `main` 分支)  
> **提交范围**：`735e4e3` ~ `d91a4f2` (基线：`ecf0990`)  
> **审查角色**：Antigravity（项目代码审查方）  
> **审查时间**：2026-10-22  
> **审查结论**：**原则通过，但存在 1 处高危数据类型桥接缺陷与 1 处同端口从机恢复隐患，建议后续补丁修复**。

---

## 1. 需求基线逐条核验结论

| 序号 | 需求基线项 | 判定结果 | 详细核验情况说明 |
|:---:|---|:---:|---|
| 1 | **换算链**：encode → decode → ×scale 真正生效 | **基本达成**（存在类型桥接缺陷） | 在 `point_value.py` 与 `reconcile.py` 中实现了纯函数链路，scale 字段成功参与计算并输出。但因 `ModbusDataType` 中无 `FLOAT64`（底层为 `DOUBLE64`），导致 `FLOAT64` 点位无法编解码并静默退化为兜底值。 |
| 2 | **类型参与范围**：仅 INT/UINT/FLOAT 换算，STRING/BOOL/HEX16 原样返回 | **符合预期** | `_SCALABLE_TYPES` 严格收敛为 6 种数值类型；非数值类型直接原样返回，不参与系数乘算。 |
| 3 | **CLI 功能**：恢复点表，输出 raw + scaled，支持 --cloud-file 对账与 0/1/2 退出码 | **基本达成**（存在多从机端口误杀隐患） | CLI 参数解析完整，支持表格与 JSON 输出，支持容差比对与 0/1/2 状态退出码。但依赖 `restore_devices` 导致同端口多 Unit ID 从机被跳过。 |
| 4 | **红线要求**：纯本地、无网络、无 tkinter；恢复从机绝不启监听 | **完全合规** | 零 UI 依赖、零网络监听绑定；仅作内存模型恢复与 CPU 编解码计算。 |
| 5 | **既有核心保护**：不得波及 modbus_codec / services 既有方法 | **完全合规** | 未改动任何既有核心业务代码，所有新增逻辑完全封装在 `tools/` 与 `tests/` 中。 |

---

## 2. 重点核对项深度分析

### 2.1 `point_key` 格式与 `session_state.json` 实际键一致性
- **实现逻辑**：`point_key(pt)` 输出 `f"{point.area.value}|{point.address}"`。
- **抽样核对**：抽查 `dist/session_state.json` 中 `slaves[0]["points"]` 实际落盘格式，键名均为 `"4x_HoldingRegister|0"`、`"4x_HoldingRegister|1"` 等。
- **结论**：**完全一致**。枚举字符串与存档字典键、点位内部属性严格对齐。

### 2.2 往返编解码无损性与枚举桥接完整性（★ 发现严重缺陷）
- **往返无损性验证**：
  - **FLOAT32 (CDAB 字序)**：经实测，`123.456` 在 CDAB 模式下编解码结果为 `123.456001`，误差小于 $1\times 10^{-6}$，符合 IEEE 754 单精度标准，往返无损。
  - **UINT32 (双寄存器)**：实测 `305419896` 往返解码完全一致，无损。
- **业务枚举与底层枚举桥接核查**：
  - `point_value.py` 与 `reconcile.py` 中直接使用 `ModbusDataType(point.data_type.value)`。
  - 遍历业务层 `DataType` 9 种类型转底层 `ModbusDataType` 发现：
    - `INT16`, `UINT16`, `INT32`, `UINT32`, `FLOAT32`, `BOOL`, `STRING`, `HEX16`：**8 种映射正确**。
    - **`FLOAT64` 发生崩溃**：底层 `modbus_codec.py` 的枚举成员名为 **`DOUBLE64`**（`ModbusDataType.DOUBLE64 = "DOUBLE64"`），不存在 `FLOAT64`！
    - **实际危害**：
      1. 在 `point_value.decode_point` 中直接抛出 `ValueError: 'FLOAT64' is not a valid ModbusDataType`。
      2. 在 `reconcile.py` 的 `compute_local_rows` 中，由于外层有 `except Exception` 兜底，该异常被静默吞掉，导致 **FLOAT64 类型的点位根本没有执行真实的寄存器编解码往返**，而是直接被退回到 `pt.value` 的兜底口径！

### 2.3 `compute_local_rows` 的 except 兜底（d91a4f2 修复项评估）
- **修复有效性**：
  - `735e4e3` 中的 `pt.value * pt.scale` 在 `scale=None` 时会再次抛出 `TypeError` 崩溃。
  - `d91a4f2` 改为 `scale_value(pt.value, pt)`，利用其内部 `try-except (TypeError, ValueError)` 成功捕获并回退 `value`，消除了二次抛错。
- **残存边界隐患**：
  - 若配置出现脏数据导致 `scale` 为字符串（如 `"bad"`）且 `pt.value` 为整数 `100` 时，Python 的 `int * str` 将执行序列乘法，生成 `'badbad...'` 字符串，既不抛错也不回退，而是产生异常字符串。
  - *改进建议*：在 `scale_value` 中增加 `isinstance(point.scale, (int, float))` 校验。

### 2.4 `diff_with_cloud` 容差与状态边界判断
- **数值与非数值比较**：
  - 数值型通过 `abs(float(scaled) - float(cloud_val)) <= tol` 容差比对，判定正确。
  - 非数值型（抛 `TypeError`/`ValueError`）自动回退到 `scaled == cloud_val` 严格相等比对，判定正确。
- **状态不混淆**：
  - 本地有点位但云端无：明确标记为 `"缺失"`。
  - 云端有点位但本地无：明确标记为 `"云端多"`。
  - 两者通过两次循环独立判定，逻辑清晰无交叉污染。
- **云端数字字符串吸收**：
  - 云端传 `"12.3"` 字符串数字时，`float("12.3")` 能够成功解析为 `12.3` 浮点数，与本地 `12.3` 匹配判定为 `OK`，具备良好的协议容错性。
- **微小注意点**：`BOOL` 值的 `float(True)` 为 `1.0`，若云端为数值 `1` 会被容差吸收为 `OK`。

### 2.5 `SlaveService.__new__` 绕过隐患（★ 发现业务架构隐患）
在 `load_slaves` 中通过 `SlaveService.__new__(SlaveService)` 手工注入 `_devices`、`_active_engines` 等属性：
1. **结构脆弱性**：未来若 `services.py` 重构在 `SlaveService` 中引入其它初始化属性（如锁对象、事件总线、缓存服务），此处未完整初始化的实例将发生隐匿的 `AttributeError`。
2. **同端口多从机恢复被阻断**：
   - `restore_devices` 内部会调用 `self.get_occupied_tcp_ports()` 进行 TCP 端口冲突检查。
   - 当 `session_state.json` 中配置了**同端口、不同 Unit ID** 的多台 TCP 从机（Modbus 工业场景下极其常见的 Server Hub 架构）时，第一台从机恢复后，后续同一端口的从机均会被判定为“端口已被占用，跳过恢复”！
   - *改进建议*：对账属于只读静态还原，完全不需要通过 `SlaveService`，直接调用纯领域模型方法 `SlaveDevice.from_dict(item)` 解析即可，既无网络预检副作用，又彻底解耦服务层。

### 2.6 越界检查与提交规范
- 执行 `git diff ecf0990..d91a4f2 --stat`：
  ```text
   docs/Claude code/MQTT_RECONCILE_PROMPT.md | 143 +++++++++++++++
   modbusstudio/tools/__init__.py            |   1 +
   modbusstudio/tools/point_value.py         |  92 ++++++++++
   modbusstudio/tools/reconcile.py           | 214 +++++++++++++++++++++++
   tests/test_reconcile.py                   | 278 ++++++++++++++++++++++++++++++
   5 files changed, 728 insertions(+)
  ```
- **结论**：变动完全限制在 `docs/`、`modbusstudio/tools/` 和 `tests/`，核心业务代码（`modbus_codec.py`、`services.py`、`models.py` 等）**改动量为 0，严格符合架构红线**。

### 2.7 测试用例质量与覆盖度（18 项）
- **覆盖情况**：
  - 覆盖了退出码 0、1、2；
  - 覆盖了坏 scale (`scale=None`) 兜底；
  - 覆盖了 FLOAT32 CDAB 模式字交换往返；
  - 覆盖了云端比对的全部 4 种状态（`OK`、`FAIL`、`缺失`、`云端多`）；
  - 修复了 Windows 控制台 UTF-8 管道解码乱码问题。
- **断言核对**：18 项测试的所有断言均无反向逻辑或无效断言。
- **测试盲区（漏网之鱼）**：
  1. 缺少针对 `DataType.FLOAT64` 的编解码与换算测试，导致 `FLOAT64` 映射崩溃漏检；
  2. 缺少同端口多从机恢复测试，导致 `restore_devices` 端口占用拦截问题漏检。

---

## 3. 验收命令执行记录与原始输出

### 命令 1：专用测试集
```bash
cd F:/GitHub/python
.venv/Scripts/python.exe -m pytest tests/test_reconcile.py -q
```
**原始输出**：
```text
..................                                                       [100%]
18 passed in 1.28s
```

### 命令 2：全量回归测试集
```bash
cd F:/GitHub/python
.venv/Scripts/python.exe -m pytest -q
```
**原始输出**：
```text
........................................................................ [ 90%]
........                                                                 [100%]
80 passed in 21.77s
```

### 命令 3：CLI 真实存档执行
```bash
cd F:/GitHub/python
.venv/Scripts/python.exe -m modbusstudio.tools.reconcile --session dist/session_state.json
```
**原始输出（节选）**：
```text
从机               点位键                          解算值              换算值(乘scale)
从机1 [1]          4x_HoldingRegister|0         0                0.0
从机1 [1]          4x_HoldingRegister|1         0                0.0
从机1 [1]          4x_HoldingRegister|2         0                0.0
从机1 [1]          4x_HoldingRegister|3         0                0.0
从机1 [1]          4x_HoldingRegister|4         0                0.0
...
[退出码: 0，成功解析全部 200 个点位并输出换算表格]
```

---

## 4. 缺陷修复建议（供后续任务参考）

1. **修复 `FLOAT64` 枚举桥接映射**：
   在 `point_value.py` 中建立业务类型到底层类型的转换字典：
   ```python
   _CODEC_TYPE_MAP = {
       "FLOAT64": ModbusDataType.DOUBLE64,
   }
   def to_codec_type(dt: DataType) -> ModbusDataType:
       return _CODEC_TYPE_MAP.get(dt.value, ModbusDataType(dt.value))
   ```
2. **简化 `load_slaves` 解耦 `SlaveService`**：
   直接使用纯模型循环反序列化，避免绕过 `__init__` 与触发端口占用拦截：
   ```python
   slaves = []
   for item in payload.get("slaves", []):
       try:
           slaves.append(SlaveDevice.from_dict(item))
       except Exception as e:
           warnings.append(f"反序列化从机失败: {e}")
   ```
3. **强化 `scale_value` 类型防御**：
   显式判断 `isinstance(point.scale, (int, float))`，防止字符串乘法产生脏数据。
