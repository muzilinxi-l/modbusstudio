"""app.py — Modbus Studio 2.0 核心应用装配器与系统生命周期协调入口。
关键导入：logging/os/sys（系统环境与路径）、threading（异步退出同步）、tkinter/ttkbootstrap（GUI 容器）、models/services/ui（四层架构组件）。
对外暴露：ModbusStudioAppCore（核心协调器类，供 MainWindow 调用）、main()（进程启动入口函数）。
"""

from __future__ import annotations
from datetime import datetime
import json
import logging
import os
import shutil
import sys
import threading
import tkinter as tk
from typing import Any, Dict, Optional

# 导入领域模型层枚举与实体：用于构建软件启动时的默认储能变流器（PCS）从机与示范点表
# 为什么放在顶层显式导入而非使用通配符 *：
#   明确列出本装配器用到的 7 个领域模型，便于静态类型检查器（mypy/pyright）与学习者追踪数据流向
from .models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    ModbusPoint,
    SlaveDevice,
)
# 导入业务服务层四大核心服务：按 Clean Architecture 规范由顶层装配器统一实例化并向下注入
# 为什么 UI 不直接实例化这些服务：
#   由 AppCore 统一持有服务生命周期，才能在窗口关闭时统一调用 shutdown() 安全释放所有端口与文件句柄
from .services import (
    LoggingService,
    PointService,
    PollService,
    SlaveService,
)
# 导入主窗口视图：负责承载从机面板、主机轮询面板与底部实时报文监控区
from .ui import MainWindow


# =============================================================================
# 会话状态持久化配置与工具函数（Session State Persistence）
# 为什么将持久化逻辑置于 app.py：
#   与 main() 里的 settings.json 主题读取风格保持一致，统一由顶层装配器协调，
#   使自动化测试可直接无头调用 save_session_state / load_session_state。
# =============================================================================
SAVE_FILENAME = "session_state.json"
SCHEMA_VERSION = 1


def save_session_state(app: ModbusStudioAppCore, path: Optional[str] = None) -> bool:
    """将应用当前会话状态（业务数据库路径、所有从机配置及点表动态模拟状态）持久化至 JSON 文件。
    参数：app（ModbusStudioAppCore 实例）、path（可选文件路径，默认为 session_state.json）。
    返回：bool（保存是否成功）。
    步骤：1.解析绝对路径；2.若文件已存在则备份为 .bak；3.提取 app.db_path 绝对值与所有从机 to_dict()；4.写入 JSON 文件。
    副作用：在磁盘上写入或覆盖 session_state.json 及 session_state.json.bak。
    """
    target_path = os.path.abspath(path) if path else os.path.abspath(SAVE_FILENAME)
    try:
        if os.path.exists(target_path):
            shutil.copy2(target_path, target_path + ".bak")

        last_db = os.path.abspath(app.db_path) if app.db_path else None
        slaves = [d.to_dict() for d in app.slave_service.get_devices()]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "last_db_path": last_db,
            "slaves": slaves,
            "saved_at": datetime.now().astimezone().isoformat(),
        }
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logging.getLogger("App").warning(f"保存会话状态异常: {e}")
        return False


def load_session_state(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """从磁盘读取并解析会话状态 JSON 文件。
    参数：path（可选文件路径，默认为 session_state.json）。
    返回：Optional[Dict[str, Any]]，成功读取且 schema_version 匹配时返回字典，否则返回 None。
    副作用：无。
    """
    target_path = os.path.abspath(path) if path else os.path.abspath(SAVE_FILENAME)
    if not os.path.exists(target_path):
        return None
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION:
            return data
        return None
    except Exception:
        return None


# =============================================================================
# 核心应用协调器（Application Coordinator）
# 为什么需要这个类：
#   避免让 UI 窗口直接全局变量式地创建底层服务，集中管理 LoggingService、SlaveService、
#   PollService 的构造顺序与优雅关闭（shutdown）生命周期，方便单元测试无头（headless）实例化。
# =============================================================================
class ModbusStudioAppCore:
    """应用程序核心协调器：统一持有业务服务单例并管理系统预载与优雅退出。
    成员变量：logging_service（日志服务）、slave_service（从机服务）、poll_service（主机轮询服务）、db_path（预载现场数据库路径）。
    使用场景：在 main() 启动 GUI 前或自动化测试中实例化，作为上下文注入 MainWindow 及各子面板。
    """

    def __init__(self, restore_session: bool = True, session_path: Optional[str] = None):
        """初始化应用核心协调器，按依赖顺序创建基础服务并装载或恢复从机。
        参数：self（ModbusStudioAppCore 实例）、restore_session（是否自动尝试恢复会话状态，默认 True）、session_path（可选自定义状态文件路径）。
        返回：None。
        步骤：1.初始化日志服务；2.注入构建从机与轮询服务；3.尝试恢复会话状态；4.若无存档或恢复失败，则探测默认库并装配默认 PCS 从机。
        副作用：创建 logs/ 目录及 CSV 日志文件，并在内存中装载从机。
        """
        # 1. 业务与通信基础设施初始化
        # 为什么必须最先创建 LoggingService：
        #   后续 SlaveService 和 PollService 在构造及运行期都需要通过它投递系统与收发报文日志
        self.logging_service = LoggingService(log_dir="logs")
        # 将日志服务注入从机管理服务，使所有从机站点共享同一双缓冲日志队列
        self.slave_service = SlaveService(self.logging_service)
        # 将日志服务注入主机轮询服务，确保主站与从站的 TX/RX 报文能在同一视图按时间序对照分析
        self.poll_service = PollService(self.logging_service)

        # 2. 尝试从会话状态存档恢复（记住上次库路径、从机配置与点表变位模拟规则）
        restored = False
        if restore_session:
            restored = self.restore_session(session_path)

        # 3. 若无可用存档或未启用恢复，则回落到默认探测与默认 PCS 从机
        if not restored:
            self.db_path = self._probe_default_db()
            self._init_default_slaves()

    def restore_session(self, path: Optional[str] = None) -> bool:
        """从 session_state.json 恢复会话状态（业务数据库路径与从机点表）。
        参数：self（ModbusStudioAppCore 实例）、path（可选文件路径）。
        返回：bool，命中并成功恢复返回 True，无存档或解析失败返回 False。
        步骤：1.调用 load_session_state；2.恢复有效库路径或回落 _probe_default_db；3.恢复从机或装载默认从机。
        副作用：更新 self.db_path，向 self.slave_service 注册从机，投递系统审计日志。
        """
        data = load_session_state(path)
        if not data:
            return False

        # 1. 恢复业务数据库路径（库文件存在则直接采用并跳过 _probe_default_db，失效则回落）
        saved_db = data.get("last_db_path")
        if saved_db and os.path.exists(saved_db):
            self.db_path = os.path.abspath(saved_db)
            self.logging_service.post("SYS", 0, f"发现并预载业务数据库: {self.db_path}")
        else:
            if saved_db:
                self.logging_service.post("SYS", 0, f"存档中记录的库文件 [{saved_db}] 已失效，本次按默认库恢复")
            self.db_path = self._probe_default_db()

        # 2. 恢复从机与点表（独立恢复，不依赖库文件是否存在）
        slaves_data = data.get("slaves", [])
        if slaves_data:
            count, warnings = self.slave_service.restore_devices(slaves_data)
            for w in warnings:
                self.logging_service.post("SYS", 0, f"会话恢复告警: {w}")
            if count > 0:
                self.logging_service.post("SYS", 0, f"已恢复会话从机服务实例 (共 {count} 台)")
            else:
                self._init_default_slaves()
        else:
            self._init_default_slaves()

        return True

    def save_session_state(self, path: Optional[str] = None) -> bool:
        """将当前应用会话状态保存至磁盘。
        参数：self（ModbusStudioAppCore 实例）、path（可选文件路径）。
        返回：bool（保存是否成功）。
        """
        return save_session_state(self, path)

    def _probe_default_db(self) -> Optional[str]:
        """按优先级探测项目 extra/ 目录下是否存在可用的现场业务数据库文件。
        参数：self（ModbusStudioAppCore 实例）。返回：Optional[str]，命中时返回绝对路径，否则返回 None。
        步骤：1.构造 .cdb/.sqlite/.db 候选路径；2.逐一校验磁盘存在性；3.命中即记录系统日志并返回。
        副作用：命中数据库文件时向 logging_service 投递一条 SYS 级别提示日志。
        """
        # 为什么按 .cdb -> .sqlite -> .db 顺序排列：
        #   现场 Linux ARM 一体机导出的 EMS 业务配置库统一采用 .cdb 扩展名（底层实为 SQLite3 格式）
        candidates = [
            os.path.abspath("extra/hems.cdb"),
            os.path.abspath("extra/hems.sqlite"),
            os.path.abspath("extra/hems.db"),
        ]
        # 遍历候选路径列表：只需命中第一个真实存在的物理文件即可短路返回
        for path in candidates:
            # 必须先用 os.path.exists 校验，防止 sqlite3.connect 自动在磁盘上创建出 0 字节空数据库文件
            if os.path.exists(path):
                # 站号传 0 表示这是系统级事件，不归属任何具体 Modbus 从机站号
                self.logging_service.post("SYS", 0, f"发现并预载业务数据库: {path}")
                return path
        # 若三个候选文件均不存在，返回 None，后续用户在导入弹窗中可手动选择其它路径
        return None

    def _init_default_slaves(self) -> None:
        """初始化首个默认从机（南向 PCS 储能变流器模拟器）并挂载 11 个典型示范点位。
        参数：self（ModbusStudioAppCore 实例）。返回：None。
        步骤：1.创建默认 TCP 127.0.0.1:502 连接配置；2.实例化 SlaveDevice；3.校验并编码 11 个预设点位；4.注册至从机服务。
        副作用：向 self.slave_service 注册 id='slave_1' 的从机实例（仅就绪，未启动监听）。
        """
        # 为什么同时填写 TCP (127.0.0.1:502) 与 RTU (COM1/9600) 字段：
        #   ConnectionConfig 是统一值对象，预填好串口参数后，用户在界面把下拉框切到 RTU 时已有合法默认值
        #   unit_id=1 代表该从机在 Modbus 总线上的默认站点地址为 1 号站
        cfg = ConnectionConfig(
            comm_type=CommType.TCP,
            host="127.0.0.1",
            port=502,
            com_port="COM1",
            baudrate=9600,
            unit_id=1,
        )
        # 创建从机聚合根：代表一台站号为 1 的南向储能变流器（PCS）从机设备
        # id="slave_1" 是内部唯一业务主键，name 则是展示在 UI 下拉框中的人类可读名称
        slave_1 = SlaveDevice(
            id="slave_1",
            name="[南向]英博PCS_1",
            conn_config=cfg,
        )

        # 预设几个常规点位
        # 为什么精心挑选这 11 个点位作为教学与联调样板：
        #   1) 覆盖保持寄存器（HOLDING，可读可写，对应遥控/遥调）与输入寄存器（INPUT，只读，对应遥测）；
        #   2) 覆盖 UINT16（无符号）、INT16（有符号，可设负功率）与 FLOAT32（占 2 个连续寄存器，注意地址间隔为 2）；
        #   3) 覆盖 ABCD（标准大端）与 CDAB（工业常见字交换），方便直观对比字节序对原始十六进制的影响。
        #   元组结构依次为：(起始地址, 点位中文描述, 寄存器分区, 数据类型, 字节序变位模式, 初始工程值)
        default_points = [
            # 地址 1~6：单寄存器（16-bit）控制参数，每个点位仅占 1 个寄存器地址，因此地址连续递增 1
            (1, "运行模式 (Operation Mode)", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 1),
            (2, "恒压充电电压给定", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 750),
            (3, "恒流充电电流给定", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 100),
            (4, "有功功率设定 (Active Power)", AreaType.HOLDING, DataType.INT16, ByteOrder.ABCD, 50),
            (5, "无功功率设定 (Reactive Power)", AreaType.HOLDING, DataType.INT16, ByteOrder.ABCD, 0),
            (6, "并网/离网模式设置", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 1),
            # 地址 10、12：双寄存器（32-bit FLOAT32）保持寄存器，每个点位占 2 个连续寄存器（10~11、12~13）
            (10, "直流侧总电压 (FLOAT32)", AreaType.HOLDING, DataType.FLOAT32, ByteOrder.CDAB, 752.4),
            (12, "直流侧总电流 (FLOAT32)", AreaType.HOLDING, DataType.FLOAT32, ByteOrder.CDAB, 98.6),
            # 地址 20、22、24：3x 输入寄存器（只读遥测），模拟电网三相交流电压采样值
            (20, "电网A相电压 (FLOAT32)", AreaType.INPUT, DataType.FLOAT32, ByteOrder.ABCD, 220.5),
            (22, "电网B相电压 (FLOAT32)", AreaType.INPUT, DataType.FLOAT32, ByteOrder.ABCD, 221.1),
            (24, "电网C相电压 (FLOAT32)", AreaType.INPUT, DataType.FLOAT32, ByteOrder.ABCD, 219.8),
        ]

        # 为什么通过 PointService.validate_and_build_point 构造而非直接实例化 ModbusPoint：
        #   服务层会自动调用 modbus_codec 完成数值钳位并生成初始 raw_hex 字符串，确保 UI 表格首屏即显示正确报文值
        for addr, desc, area, dt, bo, val in default_points:
            # validate_and_build_point 返回三元组：(是否成功布尔值, 构建好的 ModbusPoint 对象或 None, 错误消息字符串)
            ok, pt, _ = PointService.validate_and_build_point(
                address=addr,
                description=desc,
                area=area,
                data_type=dt,
                byte_order=bo,
                val_input=val,
                sim_rule="固定",
            )
            # 防御性检查：确保通过校验后再挂入从机点表字典
            if ok and pt:
                slave_1.add_point(pt)

        # 将组装完成的默认从机交由 SlaveService 托管（此时仅在内存登记，不主动打开网络端口）
        self.slave_service.register_device(slave_1)

    def shutdown(self) -> None:
        """优雅关闭：在后台守护线程中停止所有从机监听，限时 2.0s 等待，并同步释放轮询连接与日志句柄。
        参数：self（ModbusStudioAppCore 实例）。返回：None。
        步骤：1.写关闭日志；2.快照从机列表并起 daemon 线程逐台停止；3.断开主机轮询；4.最多等 2.0s；5.关闭日志流。
        副作用：停止所有运行中的从机仿真与传输层监听，断开主机轮询客户端，刷盘并关闭 CSV 日志文件。
        """
        # 为什么这么设计（对照旧实现）：
        #   旧实现直接在 Tkinter 主线程同步循环调用 stop_slave()，若开启了多台从机，主线程会串行阻塞数秒，
        #   导致点击关闭窗口按钮时界面假死。现改为 daemon 守护线程后台停止 + Event.wait(timeout=2.0) 兜底：
        #   正常情况下几毫秒即唤醒退出；极端卡死时最多等 2 秒即放行主窗口销毁，守护线程随进程终止由 OS 自动回收。
        self.logging_service.post("SYS", 0, "正在关闭系统服务并释放网络/串口端口...")
        # [SLAVE-SPEED-5] 将从机逐个停止循环移入后台 daemon 线程，防止主线程阻塞卡死窗口关闭
        done_evt = threading.Event()
        # 在主线程先行复制一份设备列表快照，防止后台线程遍历期间字典被修改引发并发迭代异常
        devices = list(self.slave_service.get_devices())

        def _stop_slaves_bg() -> None:
            """后台守护线程工作函数：遍历快照列表停止所有运行中的从机，并在结束时无条件置位 done_evt。
            参数：无（闭包捕获 devices、self.slave_service 与 done_evt）。返回：None。
            步骤：1.遍历设备快照；2.仅对 is_running 的从机调用 stop_slave；3.finally 中调用 done_evt.set() 唤醒主线程。
            副作用：停止各运行中从机的引擎实例，并触发 done_evt 事件信号。
            """
            try:
                # 逐台检查从机运行状态，跳过未启动的从机以节省关闭耗时
                for dev in devices:
                    if dev.is_running:
                        self.slave_service.stop_slave(dev.id)
            finally:
                # 关键兜底：无论循环是否抛异常，必须在 finally 中置位事件，严禁让主线程无休止挂起
                done_evt.set()

        # 标记 daemon=True：保证该线程不会阻止 Python 解释器主进程退出
        threading.Thread(target=_stop_slaves_bg, daemon=True).start()
        # 利用等待从机停止的时间窗口，在主线程并行断开主机轮询客户端连接
        if self.poll_service.is_connected:
            self.poll_service.disconnect()
        # 限时等待后台从机清理完成（上限 2.0 秒）；若超时返回 False，则记录告警日志并果断继续退出流程
        if not done_evt.wait(timeout=2.0):
            logging.getLogger("App").warning("shutdown: 后台停止从机超过 2.0s 上限，继续退出主窗口")
        # 最后一步关闭日志服务：确保前面停止从机与断开轮询产生的所有审计日志都能完整落盘
        self.logging_service.close()


# =============================================================================
# 图形界面主入口函数（GUI Entrypoint）
# 为什么将窗口创建与 ModbusStudioAppCore 分离：
#   保持业务逻辑层（ModbusStudioAppCore）不依赖 Tk 根窗口，使自动化测试可以直接测试 AppCore 而不必弹窗。
# =============================================================================
def main():
    """启动 Modbus Studio 2.0 图形界面主窗口并进入 Tkinter 事件主循环。
    参数：无。返回：None。
    步骤：1.读取 settings.json 主题；2.创建 ttkbootstrap/Tk 根窗口；3.按屏幕 78% 居中布局；4.装配 AppCore 与 MainWindow；5.绑定关闭钩子并启动 mainloop。
    副作用：读取本地 settings.json，创建 GUI 顶层窗口并阻塞主线程直至窗口销毁。
    """
    # 默认使用亮色主题 bootstrap-light，适合工业现场笔记本屏幕阅读
    initial_theme = "bootstrap-light"
    # 将相对路径转为绝对路径，确保从任意工作目录启动都能定位配置文件
    settings_file = os.path.abspath("settings.json")
    # 为什么外包一层宽泛的 try/except：
    #   用户可能手动编辑损坏 settings.json，外观配置读取失败绝不能导致核心调试软件无法启动
    if os.path.exists(settings_file):
        try:
            import json
            with open(settings_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
                # 校验字段存在且非空字符串后再覆盖默认主题名
                if "theme" in saved and saved["theme"]:
                    initial_theme = saved["theme"]
        except Exception:
            # 忽略 JSON 解析或 IO 异常，静默沿用默认主题
            pass

    # 为什么优先尝试 ttkbootstrap 并回退到原生 tk.Tk：
    #   ttkbootstrap 提供现代扁平化组件样式，但在极简离线环境若未安装该库，仍可通过原生 Tk 保障功能可用
    try:
        import ttkbootstrap as tb
        root = tb.Window(
            title="Modbus Studio - 业务配置与变位仿真调试工作站",
            themename=initial_theme,
        )
    except Exception:
        # 降级路径：回退到标准库自带的原生 Tk 根窗口
        root = tk.Tk()
        root.title("Modbus Studio - 业务配置与变位仿真调试工作站")

    # 动态感知当前屏幕尺寸，以优雅黄金比例居中展示 (初次打开不强制最大化，视线舒适且绝不压缩)
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    # 计算自适应宽度：取屏幕宽度 78%，但夹逼在 [1200, 1360] 之间，兼顾 1080p 与 2K/4K 显示器
    win_w = min(1360, max(1200, int(screen_w * 0.78)))
    # 计算自适应高度：取屏幕高度 78%，但夹逼在 [760, 880] 之间，预留操作系统任务栏高度
    win_h = min(880, max(760, int(screen_h * 0.78)))
    # 计算屏幕正中央左上角坐标，max(0, ...) 防止负坐标导致窗口标题栏超出屏幕上边界无法拖动
    pos_x = max(0, (screen_w - win_w) // 2)
    pos_y = max(0, (screen_h - win_h) // 2)
    # 通过标准 X11/Tk 几何字符串 "宽x高+X+Y" 一次性设定窗口大小与屏幕坐标
    root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
    # 限制最小可缩放尺寸，避免用户把窗口缩得太小导致内部 PanedWindow 分栏控件重叠不可见
    root.minsize(1020, 640)

    # 先构建业务协调器，再将其注入主窗口完成视图层与服务层绑定
    app = ModbusStudioAppCore()
    # 实例化主窗口：内部会自动挂载左/上业务面板与底部实时报文日志监控器
    main_window = MainWindow(root, app)

    def on_closing():
        """拦截窗口关闭事件（WM_DELETE_WINDOW），先触发业务层优雅停机再销毁 GUI 根窗口。
        参数：无（闭包捕获 app 与 root）。返回：None。
        步骤：1.调用 app.shutdown() 释放端口与文件；2.捕获异常防止卡死；3.调用 root.destroy() 退出事件循环。
        副作用：停止所有后台通信服务并销毁主窗口。
        """
        try:
            # 先执行系统级停机清理，确保 TCP 端口立即释放，避免下次重启报 Address already in use
            app.shutdown()
        except Exception:
            # 忽略退出清理期的意外异常，保证窗口一定能被销毁
            pass
        # 销毁底层 Tk 窗口组件，使 root.mainloop() 正常退出
        root.destroy()

    # 注册窗口管理器删除协议回调：接管右上角“X”按钮与 Alt+F4 关闭行为
    root.protocol("WM_DELETE_WINDOW", on_closing)
    # 进入 Tkinter 主事件循环，开始响应界面交互与定时器回调
    root.mainloop()


# 模块直接执行入口：支持 `python modbusstudio/app.py` 启动
if __name__ == "__main__":
    main()
