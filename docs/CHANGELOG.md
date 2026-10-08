# Modbus Studio 项目开发与增删改记录 (CHANGELOG)

本文档系统记录了 **Modbus Studio（工业级 Modbus 仿真调试工作站与 HEMS 业务数据库联调工具）** 的完整开发演进过程、架构设计决策及代码增删改明细。

---

## 📅 版本演进总览

| 阶段 / 版本 | 核心主题 | 关键交付物 |
| :--- | :--- | :--- |
| **v1.0.0** | Modbus 通信基础与核心编解码 | `modbus_codec.py`, `modbus_engine.py`, 基础测试脚本 |
| **v1.1.0** | 桌面 GUI 工作站与批量变位控制 | `modbus_studio.py` (Slave + Poll 双模式, 批量规则生成) |
| **v1.2.0** | HEMS 业务数据库深度融合与南/北向配对 | `hems_db_loader.py`, `hems_pairing.py`, 数据库导入面板 |
| **v1.3.0** | 独立可执行程序打包与定制图标 | `app.ico`, `build_exe.bat`, `dist/ModbusStudio.exe` |

---

## 🛠️ 模块级增删改明细

### 1. 核心编解码与变位系统 (`modbus_codec.py`)
> **背景**：工业现场中不同 PLC/RTU/电表（如施耐德、西门子、ABB、各类储能 PCS/BMS）采用不同的浮点数和长整型字节序存储方式，必须原生支持 12 种数据类型以及 4 种核心变位格式。

- **[新增] 数据类型枚举与寄存器长度映射 (`ModbusDataType`)**：
  - `BOOL` (1-bit / 单线圈)
  - `INT16`, `UINT16` (1 个保持寄存器 / 16 位)
  - `INT32`, `UINT32` (2 个保持寄存器 / 32 位)
  - `INT64`, `UINT64` (4 个保持寄存器 / 64 位)
  - `FLOAT32` (2 个寄存器 / 单精度 IEEE 754 浮点数)
  - `DOUBLE64` (4 个寄存器 / 双精度浮点数)
  - `HEX`, `BINARY`, `STRING` (原始与字符串类型)
- **[新增] 4 种变位模式支持 (`ByteOrderMode`)**：
  - `ABCD`：大端模式 (Big-Endian, 大端字序 + 大端字节序)
  - `CDAB`：小端字序 / 大端字节序 (Word Swap，国内电表与逆变器最常用)
  - `BADC`：大端字序 / 小端字节序 (Byte Swap)
  - `DCBA`：纯小端模式 (Little-Endian)
- **[新增] 核心算法函数**：
  - `encode_value(value, data_type, byte_order)`: 将工程数值转为寄存器无符号 16 位整型数组。
  - `decode_value(registers, data_type, byte_order)`: 将寄存器列表还原为工程浮点或整数。
  - `transform_bytes(raw_bytes, byte_order)`: 字节级别重排算法，支持 2 字节、4 字节、8 字节对称重排。

---

### 2. 从机模拟与主机轮询通信引擎 (`modbus_engine.py`)
> **背景**：基于 `pymodbus` 工业级通信框架封装轻量、健壮且支持多线程并发的 Slave 与 Poll 服务。

- **[新增] `ModbusSlaveEngine` (从机模拟引擎)**：
  - 封装 `ModbusSimulatorContext` 与 `ModbusServerFactory`。
  - 支持 4 大 Modbus 存储区：线圈 (`Coils`, 0x)、离散输入 (`DiscreteInputs`, 1x)、保持寄存器 (`HoldingRegisters`, 4x)、输入寄存器 (`InputRegisters`, 3x)。
  - 支持动态点位更新：`set_value()` 自动调用编解码器将多寄存器数据写入 Context。
  - 异步后台线程监听，支持平滑启动与关闭，防止端口占用崩溃。
- **[新增] `ModbusPollEngine` (主机轮询引擎)**：
  - 支持 Modbus TCP 主机客户端连接，内置连接心跳与自动重连机制。
  - 周期性多任务轮询调度器（Worker 线程池），支持多点位批量打包轮询与独立周期调度。
  - 支持单点及批量写入命令（0x05, 0x06, 0x0F, 0x10 功能码）。

---

### 3. 图形交互工作站与批量操作体系 (`modbus_studio.py`)
> **背景**：针对工业调试痛点，不仅提供单点改值，更强化多选批量变位和批量点表规则生成。

- **[新增] 双模式架构**：
  - **Slave 模式**：提供完整的点表管理、值变位预览、客户端访问日志。
  - **Poll 模式**：提供目标从机连接、数据读取、实时曲线监控与写入调试。
- **[新增] 批量点位规则生成器 (`BatchRuleDialog`)**：
  - 支持用户指定存储区、起始地址、点位数量、地址递增步长、数据类型与初始变位。
  - 一键批量生成标准点表，免去逐个添加点位的繁琐操作。
- **[新增] 多选批量修改变位功能**：
  - 表格支持 `Ctrl+点击`、`Shift+点击` 以及全选（`Ctrl+A`）多行选择。
  - 顶部工具栏与右键菜单提供【批量变位】选项，一键将所选的所有点位切换为指定的字节序模式（如一键从 ABCD 变位为 CDAB）。
  - 数值自动以新变位模式重新编解码并更新至模拟从机。
- **[新增] 界面右键快捷菜单**：
  - 支持右键修改值、右键切换变位、右键删除、右键复制点位信息。
- **[新增] 图标资源自动适应加载 (`get_resource_path`)**：
  - 自动识别源码环境与 PyInstaller 单文件打包临时目录（`sys._MEIPASS`），无缝加载程序图标。

---

### 4. HEMS 业务数据库深度集成 (`hems_db_loader.py` & `hems_pairing.py`)
> **背景**：业务场景在 `extra/hems.cdb` SQLite 数据库的 `app` 配置层中维护了完整的物联网南向设备与北向转发关系，用户需要在测试平台中直接复用与验证这些配置。

- **[新增] 数据库解析器 (`hems_db_loader.py`)**：
  - 自动定位与连接 `extra/hems.cdb`。
  - 扫描并结构化解析 `app` 表中存储的全部设备：
    - 南向设备：电表 (`FDDSL600`)、房间负荷控制器 (`FDDFJFH`)、储能电池 (`BMS`)、储能变流器 (`PCS`) 等。
    - 北向服务：`[North]30` 统一上报从机服务。
  - 解析设备字段中的通信规约、从机 ID、寄存器起始地址、轮询周期、数据格式及缩放系数（Scale）。
- **[新增] 配对推理引擎 (`hems_pairing.py`)**：
  - 深度解析设备配置中 `More` 字段内的映射关系。
  - 推理南向物理采集点如何映射到北向转发从机的寄存器点位。
  - 支持将解析出的业务点表一键注入到 Slave 从机模拟器（模拟实际硬件对外提供数据）或 Poll 主机（验证网关轮询下级设备）。

---

### 5. 编译构建与打包发布 (`build_exe.bat`, `app.ico`)
> **背景**：提供零 Python 环境依赖的独立运行包，并满足用户对个性化软件图标的需求。

- **[新增] 高清 Windows 图标 (`app.ico`)**：
  - 由用户提供的图片通过多尺寸缩放生成标准 `.ico` 文件。
  - 包含 16x16、24x24、32x32、48x48、64x64、128x128、256x256 完整分辨率。
- **[修改] 一键打包批处理脚本 (`build_exe.bat`)**：
  - 优化 PyInstaller 打包指令，添加 `--icon "app.ico"` 与 `--add-data "app.ico;."`。
  - 成功编译出单文件绿色版免安装可执行程序：[`dist/ModbusStudio.exe`](file:///f:/GitHub/python/dist/ModbusStudio.exe)。
