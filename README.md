# Modbus Studio 2.0 - 现代 Modbus 多从机/主机仿真工作站与 HEMS 业务数据库联调平台

专为现代工业协议调试与 HEMS 能源微电网业务打造的 **Modbus Slave 多从机模拟 + Modbus Poll 主机轮询（二合一）** 工业工作站。

---

## 🌟 核心特性与技术亮点

1. **分层解耦的清晰架构 (Clean Architecture & SOLID)**：
   - **界面只收集输入、展示结果**：界面把配置交给业务服务，连接、初始化点表、失败处理和状态更新由业务与通信模块协同完成。
   - **业务操作统一入口 (`services.py`)**：单点增改、数据库导入、规则批量生成均调用统一的点位操作入口，杜绝各自解释校验位、字节序和地址。
   - **独立数据模型 (`models.py`)**：定义设备、连接、点位与轮询结果，完全独立于 UI (Tkinter) 与网络协议库，零外部硬件强绑定。
   - **通信资源明确管理**：同一个连接或串口的启停、读写与关闭统一调度，严格防范串口冲突与端口竞争。
2. **高吞吐通信稳定性设计 (彻底解决闪退与未响应)**：
   - **双缓冲节流渲染池 (Throttled Log Engine)**：后台通信线程以纳秒级速度将高频报文投递到线程安全队列中，主线程每 80ms 批量合并拉取刷新一次，单次插入降低重绘开销 90% 以上。
   - **日志最大行数修剪保护**：文本缓冲区超过 1500 行自动滚动清理头部旧行，保障内存平稳，杜绝高频报文下的界面卡顿、假死 `(未响应)` 与底层崩溃闪退。
3. **高 DPI 屏幕排版全面自适应**：
   - 彻底废除生硬的字符宽度硬编码，采用紧凑内边距 (`padx=2, pady=1`) 与弹性扩展网格，在 Windows 100%、125%、150% 甚至 200% 缩放比例下，各类功能按钮文本完整展示，绝无截断吞字。
4. **全工业数据类型与 4 种工业变位（字节序 / 字序 Endianness）**：
   - 支持 `BOOL`, `INT16`, `UINT16`, `INT32`, `UINT32`, `FLOAT32`, `FLOAT64`, `STRING`, `HEX16`。
   - 支持 **`ABCD`**（标准大端）、**`CDAB`**（字交换，光储逆变器/电表最常用）、**`BADC`**（字节交换）、**`DCBA`**（纯小端）。
5. **多站地址同端口/串口物理级并发与隔离 (Server Hub 模式)**：
   - 彻底遵循工业级 Modbus 官方协议（MBAP Header Unit ID 动态寻址）：支持同一 IP:Port（如 `0.0.0.0:502`）或同一个串口总线挂载数十个不同站地址（Unit ID）的从机并发通信。
   - 物理传输层与逻辑从机解耦，各从机点表与波形模拟（正弦波等）完全物理隔离，独立启停互不影响。
   - 详见深度架构文档：[MULTI_STATION_ARCHITECTURE.md](file:///F:/GitHub/python/MULTI_STATION_ARCHITECTURE.md)。
6. **HEMS 业务数据库深度联动**：
   - 适配 `extra/hems.cdb`（支持 SQLite/CDB 格式），支持一键载入 `app` 表业务设备模型并抽取真实点表。
   - 内置业务配对中心 (基于 More 字段)，支持储能 PCS、BMS、光伏逆变器变量路由映射。

---

## 📁 目录组织架构

```text
项目根目录/
├── pyproject.toml          # 项目信息、Python要求、依赖、工具配置
├── README.md              # 安装、启动、测试说明
├── .gitignore             # 忽略 .venv、*.cdb、*.sqlite、build、dist 等
│
├── modbusstudio/          # 应用程序核心包
│   ├── __init__.py        # 版本声明与公开接口
│   ├── __main__.py        # 统一启动入口 (python -m modbusstudio)
│   ├── app.py             # 核心协调器，装配各模块与应用生命周期
│   ├── models.py          # 纯数据实体 (Point, SlaveDevice, PollTask 等)
│   ├── services.py        # 业务操作统一入口 (PointService, SlaveService, PollService, LoggingService)
│   │
│   ├── ui/                # UI 视图层 (纯界面收集输入与展示结果)
│   │   ├── __init__.py
│   │   ├── main_window.py # 主窗体容器、高可靠节流日志视窗
│   │   ├── slave_panel.py # 从机模拟工作台 (双行控制栏、点表表格、批量操作栏)
│   │   └── poll_panel.py  # 主机轮询工作台 (目标连接、读取请求、实时监视表)
│   │
│   ├── modbus_engine.py   # 通信引擎 (多从机服务、主机轮询、报文钩子)
│   ├── modbus_codec.py    # 编解码器 (全类型与 4 种变位模式算法)
│   ├── hems_db_loader.py  # HEMS 业务数据库 (extra/hems.cdb) 适配器
│   └── hems_pairing.py    # 业务配对关系推导与解析算法
│
├── tests/                 # 自动化测试用例集
│   ├── test_codec.py      # 编解码往返精度与变位单测
│   ├── test_engine.py     # 业务服务、点位构建与端口防冲突单测
│   └── test_hems_import.py# HEMS 数据库解析与点表抽取单测
│
├── docs/                  # 技术设计文档与规范说明
└── ModbusStudio.spec      # Windows PyInstaller 打包配置
```

---

## 🚀 启动与运行指南

### 1. 环境准备
推荐使用 Python 3.10+ 环境：
```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .
```

### 2. 启动应用程序
在项目根目录下执行以下任一命令即可启动：
```bash
# 推荐：包模块方式启动
python -m modbusstudio

# 或直接运行 app.py
python modbusstudio/app.py
```

### 3. 运行自动化单元测试
运行 pytest 执行全量测试用例：
```bash
pytest -v
```

---

## 📦 打包独立 Windows 可执行程序 (.exe)

项目内置预配置的 `ModbusStudio.spec`，执行以下命令即可在 `dist/` 目录下生成独立单文件 `ModbusStudio.exe`：
```bash
pyinstaller ModbusStudio.spec
```
或直接执行根目录下的批处理脚本：
```bash
.\build_exe.bat
```
