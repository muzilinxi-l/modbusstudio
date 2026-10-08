# Modbus Studio - 工业级 Modbus Slave 模拟器与 Poll 调试助手

参考并超越 GitHub 开源项目 [Lance-He/Modbus-Salve-Simulator](https://github.com/Lance-He/Modbus-Salve-Simulator)，专为现代工业协议调试打造的 **Modbus Slave 从机模拟 + Modbus Poll 主机轮询（二合一）** 工业工作站。

---

## 🌟 核心特性与技术亮点

1. **二合一双模式支持**：
   - **Modbus Slave 模拟器**：支持 0x (Coil)、1x (Discrete Input)、3x (Input Register)、4x (Holding Register) 四大区，支持独立启停、后台数据动态模拟（正弦波、随机波动、累加计数）。
   - **Modbus Poll 主机调试器**：支持连接任意目标 Modbus 从机，支持单次读取、周期循环轮询、快捷写入（FC 05/06/16），统计 Tx/Rx/Error 与 RTT 通信延迟。
2. **全工业数据类型全面支持**：
   - `BOOL` (1-bit，开关量/继电器)
   - `INT16` / `UINT16` (1 个寄存器，16 位有符号/无符号整数)
   - `INT32` / `UINT32` (2 个寄存器，32 位有符号/无符号整数)
   - `INT64` / `UINT64` (4 个寄存器，64 位大整数)
   - `FLOAT32` (2 个寄存器，IEEE 754 单精度浮点数)
   - `DOUBLE64` (4 个寄存器，IEEE 754 双精度浮点数)
   - `HEX16` / `HEX32` (十六进制原码)
   - `BINARY16` (16 位二进制位串显示)
   - `STRING` (ASCII 工业字符串)
3. **完整的 4 种工业变位（字节序 / 字序 Endianness）转换**：
   - **`ABCD`**：标准大端 (Big-Endian)，高字节高字在前（默认通用标准）。
   - **`CDAB`**：字交换模式 (Word Swap / Mid-Little Endian)，在**欧姆龙、三菱、各类智能电表/多功能仪表**中极其常见。
   - **`BADC`**：字节交换模式 (Byte Swap)。
   - **`DCBA`**：纯小端模式 (Little-Endian)，所有字节全逆序。
   - 切换变位模式时，数据表格将实时自适应重新解码，无需重新发起网络请求。
4. **可视化 GUI 交互**：
   - 基于 Python 标准内置 `tkinter` + `ttk`，开箱即用，免额外臃肿 GUI 依赖。
   - **表格直接就地双击编辑**：双击任意点位单元格即可修改数值，系统自动根据选择的数据类型与变位规则打包成 16 位寄存器切片下发。

---

## 📁 文件架构

- [`modbus_codec.py`](file:///f:/GitHub/python/modbus_codec.py)：**核心编解码与变位转换引擎**。包含 `transform_bytes`、`encode_value`、`decode_value`，100% 单元测试覆盖。
- [`modbus_engine.py`](file:///f:/GitHub/python/modbus_engine.py)：**协议与通信底层封装**。包含 `ModbusSlaveEngine`（异步服务+线程安全）与 `ModbusPollEngine`（主机轮询+连接监控）。
- [`modbus_studio.py`](file:///f:/GitHub/python/modbus_studio.py)：**工业级图形界面应用入口**。提供 Slave / Poll 双选项卡与实时监控日志。
- [`modbus_slave.py`](file:///f:/GitHub/python/modbus_slave.py)：轻量级命令行版从机模拟示例。
- [`modbus_master.py`](file:///f:/GitHub/python/modbus_master.py)：轻量级命令行版客户端示例。
- [`.vscode/launch.json`](file:///f:/GitHub/python/.vscode/launch.json)：VS Code 一键 F5 启动配置。

---

## 🚀 启动与使用指南

### 1. 启动图形化客户端 (Modbus Studio)
在 VS Code 界面中按 **F5**（配置选择 `Python: 启动 Modbus Studio (GUI 双模式客户端/从机)`），或在终端执行：
```bash
python modbus_studio.py
```

### 2. 玩转 Slave 与 Poll 联动自测
1. 在 **🖥️ Modbus Slave 从机模拟器** 界面：
   - 点击 **▶ 启动服务**（默认端口 `5020`，Slave ID `1`）。
   - 此时表格中的预置点位（温度、压力、电量、状态字）已开始运行并根据规则实时波动。
2. 切换到 **📡 Modbus Poll 主机轮询调试器** 界面：
   - 点击 **🔗 连接从机**。
   - 点击 **🔄 启动轮询 (1s)**，即可看到下方表格实时同步显示从机的数据！
   - 切换顶部的 **全局变位模式**（例如在 `ABCD` 和 `CDAB` 之间切换），观察 Float32 和 Int32 解释出的数值如何实时自适应转换。
   - 在“快捷写入测试”中输入任意目标值并点击 **🚀 发送写入请求**，切回从机界面即可观察到值已同步被修改。
