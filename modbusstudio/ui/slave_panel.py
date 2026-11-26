"""
模块职责：Modbus Studio 从机模拟工作台主面板组件（纯视图层 View）。
    仅负责收集用户在界面上的通信参数、点位规则输入并渲染点表 Treeview 与运行状态指示灯；
    所有参数合法性校验、领域模型构建、底层通信引擎启停与波形同步均委托给业务服务层（`PointService` / `SlaveService`）。
关键依赖：
    - tkinter / ttk：构建分栏布局、参数输入框、下拉框与多列点表 `Treeview`。
    - serial.tools.list_ports（可选）：动态枚举宿主操作系统当前可用的物理/虚拟 COM 串口。
    - ..models：领域枚举（`AreaType`/`DataType`/`ByteOrder`/`SimRule`/`CommType`）与实体（`SlaveDevice`/`ModbusPoint`/`ConnectionConfig`）。
    - ..services：`PointService`（点位校验与重编码）与 `SlaveService`（从机实例生命周期管理）。
对外暴露：
    - SlavePanel：挂载于主窗口左侧工作区的从机仿真配置与监控面板类。
"""
# 【从机面板视图层设计要点】
# 1. 职责单一：仅负责收集 UI 控件输入与渲染 Treeview，严禁在视图层直接实例化 pymodbus 引擎或编写协议编码逻辑。
# 2. 异步批量启停：全部启动/全部停止放入后台守护线程执行，配合 _busy 字典防重入与 root.after(0, _on_done) 回主线程更新 UI。
# 3. 局部增量刷新：1s 周期波形同步仅对非固定规则点位调用 tree.set 原位更新单元格，保留用户滚动条位置与选中高亮状态。
# 4. 平滑防抖重排：监听容器 <Configure> 事件时采用 6px 宽度变化阈值 + 20ms after_cancel 防抖，杜绝拖拽窗口卡顿。

from __future__ import annotations
import logging
import os
# 为什么导入 threading：“全部启动”与“全部停止”可能遍历多达数十个从机实例并操作底层网络/串口，
# 必须放入后台守护线程异步执行，避免阻塞 Tkinter GUI 主事件循环造成界面未响应假死
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("SlavePanel")

# 为什么用 try-except 导入 pyserial 串口枚举工具：在未安装 pyserial 的精简环境中优雅降级为默认 COM1~COM4 列表
try:
    import serial.tools.list_ports
except ImportError:
    serial = None

if TYPE_CHECKING:
    from ..app import ModbusStudioAppCore

from ..models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    ModbusPoint,
    SimRule,
    SlaveDevice,
)
from ..services import PointService, SlaveService


def save_session_state(app: Any) -> bool:
    """包装调用 app.py 中的 save_session_state，持久化当前会话状态。
    参数：app（ModbusStudioAppCore 实例）。返回：bool。
    """
    try:
        from ..app import save_session_state as _save
        return _save(app)
    except Exception as e:
        logger.warning(f"保存会话状态异常: {e}")
        return False


class SlavePanel(ttk.Frame):
    """职责：从机仿真配置与监控工作台面板（继承自 `ttk.Frame`）。"""
    # 成员变量：
    # - app (ModbusStudioAppCore): 应用核心容器引用，提供 `slave_service`、`logging_service` 及当前 `db_path`。
    # - _current_device_id (Optional[str]): 当前下拉框选中的从机实例唯一 ID。
    # - _busy (Dict[str, bool]): 并发重入守卫字典（`{"start": False, "stop": False}`），防止用户高频连点“全部启动/全部停止”拉起多个冲突后台线程。
    # - tree (ttk.Treeview): 展示当前从机点表（地址、描述、区域、类型、字序、工程值、原始 Hex、模拟规则、字数）的核心表格控件。
    # 使用场景：
    # 由 `MainWindow` 实例化并嵌入主工作区左侧面板，支持单实例/多实例启停、SQLite/CDB 数据库点表导入、More 配对与实时波形监控。

    # 四大 Modbus 存储区域下拉选项列表（按 0x -> 1x -> 3x -> 4x 数值升序排列）
    AREA_CHOICES = [
        AreaType.COIL.friendly_name,
        AreaType.DISCRETE.friendly_name,
        AreaType.INPUT.friendly_name,
        AreaType.HOLDING.friendly_name,
    ]

    # 支持的数据类型、字节序与动态模拟规则下拉选项列表（BOOL(比特) 展示名写回时还原为 "BOOL"，FLOAT64 对齐为 "DOUBLE64"）
    DATA_TYPE_CHOICES = ["INT16", "UINT16", "INT32", "UINT32", "FLOAT32", "DOUBLE64", "BOOL(比特)", "STRING", "HEX16"]
    BYTE_ORDER_CHOICES = ["ABCD", "CDAB", "BADC", "DCBA"]
    SIM_RULE_CHOICES = ["固定", "随机波动", "步进递增", "正弦波", "方波"]
    BAUDRATE_CHOICES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400"]

    @staticmethod
    def normalize_data_type_choice(choice: str) -> str:
        """将界面下拉框中的展示名称（如 'BOOL(比特)'、'DOUBLE64(双精度)'）还原为底层标准枚举值字符串。"""
        s = str(choice).strip()
        if s.startswith("BOOL") or "比特" in s:
            return "BOOL"
        if s.startswith("DOUBLE64"):
            return "DOUBLE64"
        return s

    def __init__(self, parent: ttk.Notebook, app: ModbusStudioAppCore):
        """初始化从机模拟工作台面板，构建控件树、加载初始设备数据并启动 1s 周期的动态数值 UI 同步定时器。"""
        # 参数：
        # parent: 父级 Tkinter 容器控件。
        # app: 全局应用核心容器 `ModbusStudioAppCore` 实例。
        super().__init__(parent)
        self.app = app
        self._current_device_id: Optional[str] = None
        # 当前数据库筛选键："__ALL__" 表示全部数据库，"__NONE__" 表示未关联库，其余为规范化后的绝对路径
        self._selected_db_filter: str = "__ALL__"
        self._db_filter_keys: List[str] = ["__ALL__"]
        self._db_filter_paths: Dict[str, Optional[str]] = {"__ALL__": None}
        # [SLAVE-SPEED-4] 防止全部启动/全部停止/自动重连重复点击并发重入的守卫标志
        self._busy: Dict[str, bool] = {"start": False, "stop": False, "reconnect": False}
        self._destroyed: bool = False
        self._after_ids: set = set()

        self._build_ui()
        self._load_initial_data()
        # 延迟 1000ms 启动主线程定时刷新循环，将后台仿真线程产生的波形数值平滑同步到 Treeview 单元格
        self._schedule_after(1000, self._start_runtime_sync_loop)

    def _schedule_after(self, delay_ms: int, callback) -> Optional[str]:
        """在主线程注册受追踪的 after 回调，destroy 时统一 after_cancel 避免销毁后 Tcl 竞态。"""
        if self._destroyed or not (hasattr(self, "winfo_exists") and self.winfo_exists()):
            return None

        aid_holder: List[Optional[str]] = [None]

        def _wrapped():
            if aid_holder[0] is not None:
                self._after_ids.discard(aid_holder[0])
            if not self._destroyed and hasattr(self, "winfo_exists") and self.winfo_exists():
                callback()

        try:
            aid = self.after(delay_ms, _wrapped)
            aid_holder[0] = aid
            self._after_ids.add(aid)
            return aid
        except Exception:
            return None

    def destroy(self) -> None:
        """销毁面板前显式取消所有已挂 after 定时器，杜绝销毁后 Tcl 回调竞态。"""
        self._destroyed = True
        for aid in list(self._after_ids):
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self._after_ids.clear()
        super().destroy()

    # =========================================================================
    # UI 布局构建 (采用两行配置栏与紧凑自适应排版，杜绝高 DPI 截断)
    # =========================================================================

    def _build_ui(self) -> None:
        """构建从机面板的四层纵向布局："""
        # 1. 顶部从机实例切换、启停调度、TCP/RTU 通信配置与数据库操作栏；
        # 2. 单点参数配置与批量规则生成快捷栏；
        # 3. 批量修改模拟规则/字节序/数据类型/数值工具栏；
        # 4. 底部自适应列宽的主点表 `Treeview` 表格区。
        self.pack(fill="both", expand=True, padx=4, pady=4)

        # 1. 顶部控制栏 (分行独立布局，防止高 DPI 小屏幕下右侧按钮被挤出视野)
        top_box = ttk.LabelFrame(self, text="从机服务实例与通信配置 (支持多实例独立监听不同 IP:Port 或 COM 串口)")
        top_box.pack(fill="x", padx=2, pady=2)

        # --- 第 0 行：当前数据库切换方框 + 改标签 + 关联新数据库 ---
        row_db = ttk.Frame(top_box)
        row_db.pack(fill="x", padx=4, pady=2)

        ttk.Label(row_db, text="当前数据库:").pack(side="left", padx=(2, 4))
        self.combo_db_filter = ttk.Combobox(row_db, state="readonly", width=30)
        self.combo_db_filter.pack(side="left", padx=2)
        self.combo_db_filter.bind("<<ComboboxSelected>>", self._on_switch_db_filter)

        btn_rename_db = ttk.Button(row_db, text="改标签", style="Small.TButton", command=self._rename_current_db_label)
        btn_rename_db.pack(side="left", padx=2)

        btn_select_db = ttk.Button(row_db, text="关联/添加数据库...", style="secondary.Outline.TButton", command=self._select_database_file)
        btn_select_db.pack(side="left", padx=2)

        btn_remove_db = ttk.Button(row_db, text="移除当前库", style="danger.Outline.TButton", command=self._remove_current_db)
        btn_remove_db.pack(side="left", padx=2)

        # --- 第 0a 行：从机服务实例切换与新建/删除（独占一行） ---
        row0 = ttk.Frame(top_box)
        row0.pack(fill="x", padx=4, pady=2)

        left0 = ttk.Frame(row0)
        left0.pack(side="left", fill="x", expand=True)

        ttk.Label(left0, text="从机服务实例:").pack(side="left", padx=(2, 4))
        self.combo_instances = ttk.Combobox(left0, state="readonly", width=34)
        self.combo_instances.pack(side="left", padx=2)
        self.combo_instances.bind("<<ComboboxSelected>>", self._on_switch_slave_instance)

        btn_new_inst = ttk.Button(left0, text="新建服务", style="primary.Outline.TButton", command=self._add_new_slave_instance)
        btn_new_inst.pack(side="left", padx=2)

        btn_del_inst = ttk.Button(left0, text="删除服务", style="danger.Outline.TButton", command=self._delete_current_slave_instance)
        btn_del_inst.pack(side="left", padx=2)

        btn_import_db = ttk.Button(left0, text="导入设备点表...", style="info.TButton", command=self._open_import_db_dialog)
        btn_import_db.pack(side="left", padx=(8, 2))

        btn_pair_center = ttk.Button(left0, text="业务配对中心 (基于 More)...", style="primary.Outline.TButton", command=self._open_pairing_dialog)
        btn_pair_center.pack(side="left", padx=2)

        # --- 第 0b 行：服务控制按钮组（启动/重启服务 + 状态标签 + 全部启停 + 自动重连，拆出独占一行） ---
        row0b = ttk.Frame(top_box)
        row0b.pack(fill="x", padx=4, pady=2)

        right0 = ttk.Frame(row0b)
        right0.pack(side="left")

        self.btn_toggle_slave = ttk.Button(right0, text="启动服务", style="success.TButton", command=self._toggle_current_slave_server)
        self.btn_toggle_slave.pack(side="left", padx=3)

        self.lbl_slave_status = ttk.Label(right0, text="未运行", foreground="#6c757d", font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_slave_status.pack(side="left", padx=4)

        btn_start_all = ttk.Button(right0, text="全部启动", style="success.Outline.TButton", command=self._start_all_slaves)
        btn_start_all.pack(side="left", padx=2)

        btn_stop_all = ttk.Button(right0, text="全部停止", style="danger.Outline.TButton", command=self._stop_all_slaves)
        btn_stop_all.pack(side="left", padx=2)

        # [SLAVE-RECONNECT] 追加"自动重连"按钮，用于触发底层僵死 listener 探活自愈重建
        btn_reconnect = ttk.Button(right0, text="自动重连", style="secondary.Outline.TButton", command=self._on_auto_reconnect)
        btn_reconnect.pack(side="left", padx=2)

        ttk.Label(right0, text="仿真周期:").pack(side="left", padx=(8, 2))
        self.combo_sim_interval = ttk.Combobox(
            right0,
            state="readonly",
            width=4,
            values=["1s", "2s", "3s", "5s"],
        )
        self.combo_sim_interval.set("1s")
        self.combo_sim_interval.pack(side="left", padx=2)
        self.combo_sim_interval.bind("<<ComboboxSelected>>", self._on_sim_interval_change)

        # --- 第 1 行：通讯方式与连接参数（独占一行） ---
        row1 = ttk.Frame(top_box)
        row1.pack(fill="x", padx=4, pady=2)

        left1 = ttk.Frame(row1)
        left1.pack(side="left", fill="x", expand=True)

        ttk.Label(left1, text="通讯方式:").pack(side="left", padx=(2, 2))
        self.slave_comm_type_var = tk.StringVar(value="TCP")
        rb_tcp = ttk.Radiobutton(left1, text="以太网 (TCP)", variable=self.slave_comm_type_var, value="TCP", command=self._on_slave_comm_type_change)
        rb_tcp.pack(side="left", padx=2)
        rb_rtu = ttk.Radiobutton(left1, text="串口 (RTU / 485 / 232)", variable=self.slave_comm_type_var, value="RTU", command=self._on_slave_comm_type_change)
        rb_rtu.pack(side="left", padx=2)

        self.slave_conn_container = ttk.Frame(left1)
        self.slave_conn_container.pack(side="left", padx=4)

        # TCP 控件
        self.tcp_frame = ttk.Frame(self.slave_conn_container)
        ttk.Label(self.tcp_frame, text="监听IP:").pack(side="left", padx=1)
        self.entry_slave_ip = ttk.Entry(self.tcp_frame, width=12)
        self.entry_slave_ip.pack(side="left", padx=1)
        ttk.Label(self.tcp_frame, text="端口:").pack(side="left", padx=1)
        self.entry_slave_port = ttk.Entry(self.tcp_frame, width=6)
        self.entry_slave_port.pack(side="left", padx=1)

        # RTU 控件 (完整工业级串口五要素配置)
        self.rtu_frame = ttk.Frame(self.slave_conn_container)
        ttk.Label(self.rtu_frame, text="串口:").pack(side="left", padx=1)
        self.combo_slave_com = ttk.Combobox(self.rtu_frame, width=7)
        self.combo_slave_com.pack(side="left", padx=1)
        btn_com_refresh = ttk.Button(self.rtu_frame, text="刷新", width=4, style="Small.TButton", command=self._refresh_slave_com_ports)
        btn_com_refresh.pack(side="left", padx=1)

        ttk.Label(self.rtu_frame, text="波特率:").pack(side="left", padx=(3, 1))
        self.combo_slave_baud = ttk.Combobox(self.rtu_frame, width=7, values=self.BAUDRATE_CHOICES)
        self.combo_slave_baud.set("9600")
        self.combo_slave_baud.pack(side="left", padx=1)

        ttk.Label(self.rtu_frame, text="校验:").pack(side="left", padx=(3, 1))
        self.combo_slave_parity = ttk.Combobox(self.rtu_frame, width=6, state="readonly", values=["None (N)", "Even (E)", "Odd (O)"])
        self.combo_slave_parity.set("None (N)")
        self.combo_slave_parity.pack(side="left", padx=1)

        ttk.Label(self.rtu_frame, text="数据位:").pack(side="left", padx=(3, 1))
        self.combo_slave_databit = ttk.Combobox(self.rtu_frame, width=3, state="readonly", values=["7", "8"])
        self.combo_slave_databit.set("8")
        self.combo_slave_databit.pack(side="left", padx=1)

        ttk.Label(self.rtu_frame, text="停止位:").pack(side="left", padx=(3, 1))
        self.combo_slave_stopbit = ttk.Combobox(self.rtu_frame, width=3, state="readonly", values=["1", "2"])
        self.combo_slave_stopbit.set("1")
        self.combo_slave_stopbit.pack(side="left", padx=1)

        ttk.Label(left1, text="从机 ID:").pack(side="left", padx=(4, 1))
        self.entry_slave_id = ttk.Entry(left1, width=4)
        self.entry_slave_id.pack(side="left", padx=1)

        # 2. 单点配置与快速生成栏
        pt_box = ttk.LabelFrame(self, text="点位规则生成 & 快捷操作")
        pt_box.pack(fill="x", padx=2, pady=2)

        row_pt = ttk.Frame(pt_box)
        row_pt.pack(fill="x", padx=2, pady=2)

        btn_batch_gen = ttk.Button(row_pt, text="批量输入规则生成点表...", style="primary.Outline.TButton", command=self._open_batch_generate_dialog)
        btn_batch_gen.pack(side="left", padx=(2, 6))

        ttk.Label(row_pt, text="地址:").pack(side="left")
        self.entry_pt_addr = ttk.Entry(row_pt, width=5)
        self.entry_pt_addr.pack(side="left", padx=2)

        ttk.Label(row_pt, text="描述:").pack(side="left")
        self.entry_pt_desc = ttk.Entry(row_pt, width=10)
        self.entry_pt_desc.pack(side="left", padx=2)

        ttk.Label(row_pt, text="区域:").pack(side="left")
        self.combo_pt_area = ttk.Combobox(row_pt, state="readonly", width=12, values=self.AREA_CHOICES)
        self.combo_pt_area.set(AreaType.HOLDING.friendly_name)
        self.combo_pt_area.pack(side="left", padx=2)

        ttk.Label(row_pt, text="类型:").pack(side="left")
        self.combo_pt_type = ttk.Combobox(row_pt, state="readonly", width=8, values=self.DATA_TYPE_CHOICES)
        self.combo_pt_type.set("UINT16")
        self.combo_pt_type.pack(side="left", padx=2)

        ttk.Label(row_pt, text="变位:").pack(side="left")
        self.combo_pt_order = ttk.Combobox(row_pt, state="readonly", width=6, values=self.BYTE_ORDER_CHOICES)
        self.combo_pt_order.set("ABCD")
        self.combo_pt_order.pack(side="left", padx=2)

        ttk.Label(row_pt, text="初值:").pack(side="left")
        self.entry_pt_val = ttk.Entry(row_pt, width=6)
        self.entry_pt_val.insert(0, "0")
        self.entry_pt_val.pack(side="left", padx=2)

        ttk.Label(row_pt, text="系数:").pack(side="left")
        self.entry_pt_scale = ttk.Entry(row_pt, width=5)
        self.entry_pt_scale.insert(0, "1")
        self.entry_pt_scale.pack(side="left", padx=2)

        ttk.Label(row_pt, text="偏移:").pack(side="left")
        self.entry_pt_offset = ttk.Entry(row_pt, width=5)
        self.entry_pt_offset.insert(0, "0")
        self.entry_pt_offset.pack(side="left", padx=2)

        ttk.Label(row_pt, text="模拟:").pack(side="left")
        self.combo_pt_sim = ttk.Combobox(row_pt, state="readonly", width=7, values=self.SIM_RULE_CHOICES)
        self.combo_pt_sim.set("固定")
        self.combo_pt_sim.pack(side="left", padx=2)

        btn_add_pt = ttk.Button(row_pt, text="单点添加", style="success.TButton", command=self._on_click_add_point)
        btn_add_pt.pack(side="left", padx=4)

        # 3. 批量操作工具条
        batch_box = ttk.LabelFrame(self, text="批量操作与规则配置 (支持选择当前从机或全部从机生效)")
        batch_box.pack(fill="x", padx=2, pady=2)

        b_row0 = ttk.Frame(batch_box)
        b_row0.pack(fill="x", padx=2, pady=1)

        ttk.Label(b_row0, text="适用范围:").pack(side="left", padx=(2, 2))
        self.batch_scope_var = tk.StringVar(value="current")
        rb_cur = ttk.Radiobutton(b_row0, text="仅当前从机", variable=self.batch_scope_var, value="current")
        rb_cur.pack(side="left", padx=2)
        rb_all = ttk.Radiobutton(b_row0, text="全部从机服务", variable=self.batch_scope_var, value="all")
        rb_all.pack(side="left", padx=2)

        ttk.Separator(b_row0, orient="vertical").pack(side="left", fill="y", padx=6)

        ttk.Label(b_row0, text="模拟规则:").pack(side="left", padx=1)
        self.combo_batch_sim = ttk.Combobox(b_row0, state="readonly", width=8, values=self.SIM_RULE_CHOICES)
        self.combo_batch_sim.set("固定")
        self.combo_batch_sim.pack(side="left", padx=2)

        btn_apply_sim = ttk.Button(b_row0, text="批量修改规则", style="secondary.TButton", command=self._apply_batch_sim)
        btn_apply_sim.pack(side="left", padx=3)

        btn_sel_all = ttk.Button(b_row0, text="全选点位", style="Small.TButton", command=self._select_all_points)
        btn_sel_all.pack(side="left", padx=3)

        btn_desel_all = ttk.Button(b_row0, text="取消选择", style="Small.TButton", command=self._deselect_all_points)
        btn_desel_all.pack(side="left", padx=3)

        btn_del_sel = ttk.Button(b_row0, text="批量删除选中", style="danger.Outline.TButton", command=self._batch_delete_selected)
        btn_del_sel.pack(side="left", padx=3)

        # 批量操作第 2 行：变位、类型、数值
        b_row1 = ttk.Frame(batch_box)
        b_row1.pack(fill="x", padx=2, pady=1)

        ttk.Label(b_row1, text="目标变位:").pack(side="left", padx=(2, 1))
        self.combo_batch_order = ttk.Combobox(b_row1, state="readonly", width=6, values=self.BYTE_ORDER_CHOICES)
        self.combo_batch_order.set("ABCD")
        self.combo_batch_order.pack(side="left", padx=1)
        btn_apply_order = ttk.Button(b_row1, text="批量修改变位", style="secondary.TButton", command=self._apply_batch_order)
        btn_apply_order.pack(side="left", padx=2)

        ttk.Label(b_row1, text="目标类型:").pack(side="left", padx=(6, 1))
        self.combo_batch_type = ttk.Combobox(b_row1, state="readonly", width=8, values=self.DATA_TYPE_CHOICES)
        self.combo_batch_type.set("FLOAT32")
        self.combo_batch_type.pack(side="left", padx=1)
        btn_apply_type = ttk.Button(b_row1, text="批量修改类型", style="secondary.TButton", command=self._apply_batch_type)
        btn_apply_type.pack(side="left", padx=2)

        ttk.Label(b_row1, text="统一设值:").pack(side="left", padx=(6, 1))
        self.entry_batch_val = ttk.Entry(b_row1, width=7)
        self.entry_batch_val.insert(0, "0")
        self.entry_batch_val.pack(side="left", padx=1)
        btn_apply_val = ttk.Button(b_row1, text="批量修改数值", style="warning.TButton", command=self._apply_batch_value)
        btn_apply_val.pack(side="left", padx=2)

        # 批量操作第 3 行：系数与偏移量（与 HEMS 数据库 Scale Factor / Offset 一一对应）
        b_row2 = ttk.Frame(batch_box)
        b_row2.pack(fill="x", padx=2, pady=1)

        ttk.Label(b_row2, text="统一系数:").pack(side="left", padx=(2, 1))
        self.entry_batch_scale = ttk.Entry(b_row2, width=7)
        self.entry_batch_scale.insert(0, "1")
        self.entry_batch_scale.pack(side="left", padx=1)
        btn_apply_scale = ttk.Button(b_row2, text="批量修改系数", style="secondary.TButton", command=self._apply_batch_scale)
        btn_apply_scale.pack(side="left", padx=2)

        ttk.Label(b_row2, text="统一偏移:").pack(side="left", padx=(6, 1))
        self.entry_batch_offset = ttk.Entry(b_row2, width=7)
        self.entry_batch_offset.insert(0, "0")
        self.entry_batch_offset.pack(side="left", padx=1)
        btn_apply_offset = ttk.Button(b_row2, text="批量修改偏移", style="secondary.TButton", command=self._apply_batch_offset)
        btn_apply_offset.pack(side="left", padx=2)

        ttk.Separator(b_row2, orient="vertical").pack(side="left", fill="y", padx=6)

        ttk.Label(b_row2, text="波动范围:").pack(side="left", padx=(2, 1))
        self.entry_batch_sim_min = ttk.Entry(b_row2, width=6)
        self.entry_batch_sim_min.insert(0, "-1000")
        self.entry_batch_sim_min.pack(side="left", padx=1)
        ttk.Label(b_row2, text="~").pack(side="left")
        self.entry_batch_sim_max = ttk.Entry(b_row2, width=6)
        self.entry_batch_sim_max.insert(0, "1000")
        self.entry_batch_sim_max.pack(side="left", padx=1)
        btn_apply_sim_range = ttk.Button(b_row2, text="批量修改范围", style="secondary.TButton", command=self._apply_batch_sim_range)
        btn_apply_sim_range.pack(side="left", padx=2)

        # 4. 点表表格区域
        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=2, pady=2)

        self.tree_scroll_y = ttk.Scrollbar(table_frame, orient="vertical")
        self.tree_scroll_y.pack(side="right", fill="y")
        self.tree_scroll_x = ttk.Scrollbar(table_frame, orient="horizontal")
        self.tree_scroll_x.pack(side="bottom", fill="x")

        self.columns = ("addr", "desc", "area", "type", "order", "scale", "offset", "val", "raw_hex", "sim", "reg_cnt")
        self.tree = ttk.Treeview(
            table_frame,
            columns=self.columns,
            show="headings",
            selectmode="extended",
            yscrollcommand=self.tree_scroll_y.set,
            xscrollcommand=self.tree_scroll_x.set,
        )
        self.tree_scroll_y.config(command=self.tree.yview)
        self.tree_scroll_x.config(command=self.tree.xview)
        self.tree.pack(side="left", fill="both", expand=True)

        # 列配置：(列ID, 表头标题, 宽度占比, 最小像素宽度, 对齐方式)
        self._table_col_configs = [
            ("addr", "起始地址", 0.07, 72, "center"),
            ("desc", "点位描述 / 业务字段", 0.22, 180, "w"),
            ("area", "存储区域", 0.11, 108, "center"),
            ("type", "数据类型", 0.08, 80, "center"),
            ("order", "变位模式", 0.07, 68, "center"),
            ("scale", "系数", 0.06, 58, "center"),
            ("offset", "偏移量", 0.06, 62, "center"),
            ("val", "当前解析数值", 0.11, 95, "center"),
            ("raw_hex", "原始寄存器(Hex)", 0.11, 115, "center"),
            ("sim", "动态模拟", 0.06, 72, "center"),
            ("reg_cnt", "占用字数", 0.05, 56, "center"),
        ]
        for col_id, heading, ratio, min_w, align in self._table_col_configs:
            head_align = align if col_id == "desc" else "center"
            self.tree.heading(col_id, text=heading, anchor=head_align, command=lambda c=col_id: self._sort_by_column(c, False))
            self.tree.column(col_id, width=max(min_w, int(1100 * ratio)), minwidth=min_w, anchor=align, stretch=False)

        self.tree.bind("<Double-1>", self._on_tree_double_click)
        table_frame.bind("<Configure>", self._on_table_resize)

    def _on_table_resize(self, event=None) -> None:
        """动态感知表格容器宽度变化，采用“6px 阈值过滤 + 20ms 防抖”调度列宽重算。"""
        # 参数：
        # event: Tkinter `<Configure>` 事件对象。
        # 设计说明（为什么需要防抖与像素阈值）：
        # 用户拖拽窗口边框或分割条时，操作系统每秒会触发数十次 `<Configure>` 事件。
        # 若每次事件都立即遍历 9 列调用 `self.tree.column()`，会导致明显的重绘闪烁与拖动卡顿。
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return
        width = event.width if event else self.tree.winfo_width()
        if width < 450:
            return

        # 阈值过滤：微小像素变动跳过
        if hasattr(self, "_last_rendered_width") and abs(width - self._last_rendered_width) < 6:
            return

        # 20ms 防抖调度
        if hasattr(self, "_resize_job") and self._resize_job is not None:
            self.after_cancel(self._resize_job)

        self._pending_width = width
        self._resize_job = self.after(20, self._apply_table_resize)

    def _apply_table_resize(self) -> None:
        """执行防抖后的主点表各列宽度按比例重分配。"""
        self._resize_job = None
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return
        avail_w = getattr(self, "_pending_width", self.tree.winfo_width()) - 25
        if avail_w < 450:
            return
        self._last_rendered_width = self._pending_width
        for col_id, _, ratio, min_w, _ in self._table_col_configs:
            dyn_w = max(min_w, int(avail_w * ratio))
            self.tree.column(col_id, width=dyn_w)

    # =========================================================================
    # 数据加载与视图响应
    # =========================================================================

    def _load_initial_data(self) -> None:
        """面板首次挂载时刷新本机可用 COM 串口列表并初始化从机实例下拉框。"""
        self._refresh_slave_com_ports()
        self._update_instance_combobox()

    def _refresh_slave_com_ports(self) -> None:
        """扫描宿主系统物理/虚拟串口并更新 `combo_slave_com` 下拉选项（无可用串口时回退至 COM1~COM4）。"""
        ports = []
        if serial:
            try:
                for p in serial.tools.list_ports.comports():
                    ports.append(p.device)
            except Exception:
                pass
        if not ports:
            ports = ["COM1", "COM2", "COM3", "COM4"]
        self.combo_slave_com["values"] = ports
        if not self.combo_slave_com.get() and ports:
            self.combo_slave_com.set(ports[0])

    @staticmethod
    def _norm_db_key(db_path: Optional[str]) -> str:
        """将数据库路径归一化为筛选键；空路径归入 '__NONE__'。"""
        if not db_path:
            return "__NONE__"
        return os.path.normcase(os.path.abspath(str(db_path)))

    def _get_scoped_devices(self) -> List[SlaveDevice]:
        """按顶部「当前数据库」下拉框返回作用域内的从机列表。"""
        all_devices = self.app.slave_service.get_devices()
        if self._selected_db_filter == "__ALL__":
            return all_devices
        if self._selected_db_filter == "__NONE__":
            return [d for d in all_devices if not getattr(d, "db_file", None)]
        return [
            d for d in all_devices
            if getattr(d, "db_file", None) and self._norm_db_key(d.db_file) == self._selected_db_filter
        ]

    def _refresh_db_filter_combobox(self) -> None:
        """刷新顶部「当前数据库」下拉框选项，汇总所有已关联与已导入的数据库及其从机数量。"""
        if not hasattr(self, "combo_db_filter"):
            return
        all_devices = self.app.slave_service.get_devices()
        get_label = getattr(self.app, "get_db_label", None)

        counts: Dict[str, int] = {}
        raw_paths: Dict[str, Optional[str]] = {"__ALL__": None}
        ordered_keys: List[str] = []

        # 先收集已有从机绑定的数据库
        unassigned_cnt = 0
        for dev in all_devices:
            db_f = getattr(dev, "db_file", None)
            if db_f:
                k = self._norm_db_key(db_f)
                if k not in counts:
                    ordered_keys.append(k)
                    raw_paths[k] = os.path.abspath(db_f)
                counts[k] = counts.get(k, 0) + 1
            else:
                unassigned_cnt += 1

        # 再确保当前 app.db_path 即使尚未导入从机也出现在下拉框中，方便直接切换并导入
        if getattr(self.app, "db_path", None) and os.path.exists(self.app.db_path):
            cur_k = self._norm_db_key(self.app.db_path)
            if cur_k not in counts:
                ordered_keys.append(cur_k)
                raw_paths[cur_k] = os.path.abspath(self.app.db_path)
                counts[cur_k] = 0

        keys: List[str] = ["__ALL__"]
        labels: List[str] = [f"全部数据库 (共 {len(all_devices)} 台)"]
        for k in ordered_keys:
            p = raw_paths[k]
            tag = get_label(p) if callable(get_label) else (os.path.basename(p) if p else "")
            keys.append(k)
            labels.append(f"{tag} ({counts.get(k, 0)} 台)")
        if unassigned_cnt > 0:
            keys.append("__NONE__")
            raw_paths["__NONE__"] = None
            labels.append(f"手动新建/未关联库 ({unassigned_cnt} 台)")

        self._db_filter_keys = keys
        self._db_filter_paths = raw_paths
        self.combo_db_filter["values"] = labels

        if self._selected_db_filter not in keys:
            # 首次启动或原筛选项失效时：若存在具体数据库则默认聚焦第一个库，否则显示全部
            if len(ordered_keys) == 1 and unassigned_cnt == 0:
                self._selected_db_filter = ordered_keys[0]
            else:
                self._selected_db_filter = "__ALL__"
        idx = keys.index(self._selected_db_filter)
        self.combo_db_filter.current(idx)

    def _on_switch_db_filter(self, event=None) -> None:
        """响应顶部「当前数据库」下拉框切换：同步当前导入源数据库并过滤下方从机下拉列表。"""
        idx = self.combo_db_filter.current()
        if 0 <= idx < len(self._db_filter_keys):
            key = self._db_filter_keys[idx]
            self._selected_db_filter = key
            target_path = self._db_filter_paths.get(key)
            if target_path and os.path.exists(target_path):
                self.app.db_path = target_path
            self._update_instance_combobox()

    def _rename_current_db_label(self) -> None:
        """为当前选中的数据库修改自定义标签，并持久化至 session_state.json。"""
        target_path = self._db_filter_paths.get(self._selected_db_filter)
        if not target_path:
            target_path = getattr(self.app, "db_path", None)
        if not target_path:
            messagebox.showinfo("提示", "请先在「当前数据库」中选择一个具体数据库，或点击【关联/添加数据库...】")
            return
        cur_label = self.app.get_db_label(target_path) if hasattr(self.app, "get_db_label") else os.path.basename(target_path)
        new_label = simpledialog.askstring(
            "修改数据库标签",
            f"请输入该数据库的新标签名称（留空则恢复默认文件名）：\n{target_path}",
            initialvalue=cur_label,
            parent=self,
        )
        if new_label is None:
            return
        if hasattr(self.app, "set_db_label"):
            self.app.set_db_label(target_path, new_label)
        self._update_instance_combobox()
        save_session_state(self.app)

    def _remove_current_db(self) -> None:
        """一键停止并移除当前选中的数据库及其名下全部从机（选中“全部数据库”时清空全部）。"""
        key = self._selected_db_filter
        if key == "__ALL__":
            all_devs = self.app.slave_service.get_devices()
            if not all_devs and not getattr(self.app, "db_path", None):
                messagebox.showinfo("提示", "当前已为空，无需清空。")
                return
            if not messagebox.askyesno(
                "确认清空全部",
                f"确定要停止并清空所有数据库及全部 {len(all_devs)} 台从机实例吗？",
                parent=self,
            ):
                return
            self.app.db_path = None
            if hasattr(self.app, "db_labels"):
                self.app.db_labels.clear()
            self._clear_all_slaves_and_reset_ui()
            save_session_state(self.app)
            self.app.logging_service.post("SYS", 0, f"已清空全部数据库与 {len(all_devs)} 台从机实例")
            return

        target_path = self._db_filter_paths.get(key)
        scoped_devs = self._get_scoped_devices()
        if key == "__NONE__":
            disp_name = "手动新建/未关联库"
        else:
            disp_name = (
                self.app.get_db_label(target_path)
                if (target_path and hasattr(self.app, "get_db_label"))
                else os.path.basename(target_path or "")
            )

        if not messagebox.askyesno(
            "确认移除当前库",
            f"确定要移除 [{disp_name}] 并停止删除其名下全部 {len(scoped_devs)} 台从机实例吗？\n（仅从软件中移除，不会删除磁盘上的数据库文件）",
            parent=self,
        ):
            return

        for dev in scoped_devs:
            self.app.slave_service.remove_device(dev.id)

        if target_path:
            if hasattr(self.app, "set_db_label"):
                self.app.set_db_label(target_path, "")
            if getattr(self.app, "db_path", None) and self._norm_db_key(self.app.db_path) == key:
                # 若当前导入源正是被删库，则切到剩余的第一个库，否则置空
                remain_db = None
                for d in self.app.slave_service.get_devices():
                    if getattr(d, "db_file", None):
                        remain_db = d.db_file
                        break
                self.app.db_path = remain_db

        self._selected_db_filter = "__ALL__"
        self._current_device_id = None
        self._update_instance_combobox()
        save_session_state(self.app)
        self.app.logging_service.post("SYS", 0, f"已移除数据库 [{disp_name}] 及名下 {len(scoped_devs)} 台从机实例")

    def _update_instance_combobox(self) -> None:
        """
        先刷新顶部「当前数据库」筛选框，再按所选数据库刷新「从机服务实例」下拉框并加载当前从机。
        选定具体数据库时，从机名称不再拼数据库前缀，完整展示设备名称。
        """
        self._refresh_db_filter_combobox()
        devices = self._get_scoped_devices()
        get_label = getattr(self.app, "get_db_label", None)
        show_db_suffix = (self._selected_db_filter == "__ALL__") and (
            len({self._norm_db_key(getattr(d, "db_file", None)) for d in devices if getattr(d, "db_file", None)}) > 1
        )
        device_names = []
        for dev in devices:
            base_label = (
                dev.name
                if str(dev.name).startswith(f"从机{dev.conn_config.unit_id}")
                else f"从机{dev.conn_config.unit_id} [{dev.name}]"
            )
            if show_db_suffix and getattr(dev, "db_file", None):
                tag = get_label(dev.db_file) if callable(get_label) else os.path.basename(dev.db_file)
                if tag:
                    base_label = f"{base_label} · {tag}"
            device_names.append(base_label)
        self.combo_instances["values"] = device_names
        if devices:
            if not self._current_device_id or self._current_device_id not in [d.id for d in devices]:
                self._current_device_id = devices[0].id
                self.combo_instances.current(0)
            else:
                idx = [d.id for d in devices].index(self._current_device_id)
                self.combo_instances.current(idx)
            self._load_device_to_ui(self._current_device_id)
        else:
            self._current_device_id = None
            self.combo_instances.set("（当前数据库暂无从机，请点击【导入设备点表】或【新建服务】）")
            for item in self.tree.get_children():
                self.tree.delete(item)
            self.lbl_slave_status.config(text="● 未就绪 (无活动从机)", foreground="#6c757d")
            self.btn_toggle_slave.config(text="启动服务", style="secondary.TButton")

    def _clear_all_slaves_and_reset_ui(self) -> None:
        """清空所有从机服务实例并重置主工作台 UI 界面"""
        self.app.slave_service.clear_all_devices()
        self._current_device_id = None
        self._update_instance_combobox()

    def _load_device_to_ui(self, device_id: str) -> None:
        """将指定 `device_id` 对应的 `SlaveDevice` 通信参数、运行状态与点表数据完整回填到面板控件。"""
        # 参数：
        # device_id: 目标从机实例唯一标识。
        device = self.app.slave_service.get_device(device_id)
        if not device:
            return
        # 通知服务层仅对当前展示从机实时拼装 raw_hex，其余后台从机零字符串开销；切换时对齐一次当前表 Hex
        if hasattr(self.app.slave_service, "set_ui_active_device"):
            self.app.slave_service.set_ui_active_device(device_id)
        for pt in device.points.values():
            if pt.sim_rule != "固定":
                PointService.re_encode_point(pt)
        cfg = device.conn_config
        self.slave_comm_type_var.set(cfg.comm_type.value)
        self._on_slave_comm_type_change()

        self.entry_slave_ip.delete(0, "end")
        self.entry_slave_ip.insert(0, cfg.host)
        self.entry_slave_port.delete(0, "end")
        self.entry_slave_port.insert(0, str(cfg.port))
        self.combo_slave_com.set(cfg.com_port)
        self.combo_slave_baud.set(str(cfg.baudrate))
        parity_map = {"N": "None (N)", "E": "Even (E)", "O": "Odd (O)"}
        self.combo_slave_parity.set(parity_map.get(cfg.parity, "None (N)"))
        self.combo_slave_databit.set(str(cfg.data_bits))
        self.combo_slave_stopbit.set(str(cfg.stop_bits))
        self.entry_slave_id.delete(0, "end")
        self.entry_slave_id.insert(0, str(cfg.unit_id))

        self._update_status_indicator(device)
        self._refresh_tree(device)

    def _update_status_indicator(self, device: SlaveDevice) -> None:
        """根据 `device.is_running` 更新顶部状态指示灯颜色文本及启停切换按钮样式。"""
        # 参数：
        # device: 当前选中的从机领域实体。
        if device.is_running:
            self.lbl_slave_status.config(text=f"● 运行中 ({device.conn_config.summary})", foreground="#28a745")
            self.btn_toggle_slave.config(text="停止服务", style="danger.TButton")
        else:
            self.lbl_slave_status.config(text="● 已停止", foreground="#dc3545")
            self.btn_toggle_slave.config(text="启动服务", style="success.TButton")

    def _on_sim_interval_change(self, event=None) -> None:
        """响应顶部「仿真周期」下拉框切换，实时调整全局波形仿真步进间隔。"""
        raw = self.combo_sim_interval.get().strip().lower().rstrip("s")
        try:
            sec = float(raw)
            if hasattr(self.app.slave_service, "set_simulation_interval"):
                self.app.slave_service.set_simulation_interval(sec)
                self.app.logging_service.post("SYS", 0, f"已调整全局动态仿真周期为 {sec:g} 秒/拍")
        except Exception:
            pass

    def _on_slave_comm_type_change(self) -> None:
        """当用户切换 TCP / RTU 单选框时，动态隐藏或显示对应的 IP:Port 输入框与串口五要素控件组。"""
        mode = self.slave_comm_type_var.get()
        if mode == "RTU":
            self.tcp_frame.pack_forget()
            self.rtu_frame.pack(side="left")
        else:
            self.rtu_frame.pack_forget()
            self.tcp_frame.pack(side="left")

    def _on_switch_slave_instance(self, event=None) -> None:
        """当用户在下拉框切换从机服务实例时，按当前数据库筛选范围更新 `_current_device_id` 并重新渲染。"""
        idx = self.combo_instances.current()
        devices = self._get_scoped_devices()
        if 0 <= idx < len(devices):
            self._current_device_id = devices[idx].id
            self._load_device_to_ui(self._current_device_id)

    def _refresh_tree(self, device: Optional[SlaveDevice] = None) -> None:
        """清空并按 `(address, area)` 升序重新渲染当前从机的全部点位行到 `self.tree`。"""
        # 参数：
        # device: 可选直接传入的 `SlaveDevice` 实例；若未传入则按 `self._current_device_id` 查询。
        if not device:
            if not self._current_device_id:
                return
            device = self.app.slave_service.get_device(self._current_device_id)
            if not device:
                return

        for row in self.tree.get_children():
            self.tree.delete(row)

        sorted_points = sorted(device.points.values(), key=lambda p: (p.address, p.area.value))
        for p in sorted_points:
            scale_disp = f"{getattr(p, 'scale', 1.0):g}"
            offset_disp = f"{getattr(p, 'offset', 0.0):g}"
            self.tree.insert(
                "",
                "end",
                values=(
                    p.address,
                    p.description,
                    p.area.friendly_name,
                    p.data_type.value,
                    p.byte_order.value,
                    scale_disp,
                    offset_disp,
                    p.value,
                    p.raw_hex,
                    p.sim_rule,
                    p.register_count,
                ),
            )

    def _start_runtime_sync_loop(self) -> None:
        """主线程定时循环（1s 周期）：将运行中从机的动态波形模拟数值（`val` 与 `raw_hex` 列）原位刷新到表格界面。"""
        try:
            if self._current_device_id and hasattr(self, "tree") and self.tree.winfo_exists():
                device = self.app.slave_service.get_device(self._current_device_id)
                if device and device.is_running:
                    # 差异刷新所有行：既同步动态仿真点位，也实时展示外部主站写入的“固定”点位，值未变时不触发重绘
                    for row_id in self.tree.get_children():
                        vals = self.tree.item(row_id, "values")
                        if vals and len(vals) >= 11:
                            addr = int(vals[0])
                            area_name = vals[2]
                            p = device.get_point(area_name, addr)
                            if p and (str(vals[7]) != str(p.value) or str(vals[8]) != str(p.raw_hex)):
                                self.tree.set(row_id, "val", p.value)
                                self.tree.set(row_id, "raw_hex", p.raw_hex)
        except Exception:
            pass
        finally:
            if not self._destroyed and hasattr(self, "winfo_exists") and self.winfo_exists():
                self._schedule_after(1000, self._start_runtime_sync_loop)

    # =========================================================================
    # 核心业务操作入口：完全遵循用户指令设计
    # 界面收集输入 -> 业务模块校验与转换 -> 引擎同步 -> 界面更新
    # =========================================================================

    def _on_click_add_point(self) -> None:
        """
        响应“单点添加”按钮点击事件：收集输入参数、委托 `PointService` 校验并构建点位、写入从机模型与运行时引擎并刷新表格。
        """
        if not self._current_device_id:
            messagebox.showwarning("提示", "请先选择或新建一个从机服务实例")
            return

        device = self.app.slave_service.get_device(self._current_device_id)
        if not device:
            return

        # 1. 界面读取输入
        addr_str = self.entry_pt_addr.get()
        desc = self.entry_pt_desc.get()
        area_str = self.combo_pt_area.get()
        dt_str = self.normalize_data_type_choice(self.combo_pt_type.get())
        bo_str = self.combo_pt_order.get()
        val_str = self.entry_pt_val.get()
        sim_str = self.combo_pt_sim.get()
        scale_str = self.entry_pt_scale.get().strip() if hasattr(self, "entry_pt_scale") else "1"
        offset_str = self.entry_pt_offset.get().strip() if hasattr(self, "entry_pt_offset") else "0"

        try:
            import math
            scale_val = float(scale_str or "1")
            if not math.isfinite(scale_val) or scale_val == 0.0:
                raise ValueError("系数必须为非零有限数字")
            offset_val = float(offset_str or "0")
            if not math.isfinite(offset_val):
                raise ValueError("偏移量必须为有限数字")
        except Exception as ex:
            messagebox.showerror("添加失败", f"系数或偏移量格式有误: {ex}")
            return

        # 2. 统一交给业务模块 PointService 校验与构建模型
        ok, point, err = PointService.validate_and_build_point(
            address=addr_str,
            description=desc,
            area=area_str,
            data_type=dt_str,
            byte_order=bo_str,
            val_input=val_str,
            sim_rule=sim_str,
            scale=scale_val,
            offset=offset_val,
        )
        if not ok or not point:
            messagebox.showerror("添加失败", f"参数错误: {err}")
            return

        # 3. 更新模型
        device.add_point(point)

        # 4. 若从机正在运行，同步给底层通信引擎
        self.app.slave_service.sync_point_to_runtime(device.id, point)

        # 5. 界面展示结果
        self._refresh_tree(device)
        self.app.logging_service.post("SYS", device.conn_config.unit_id, f"成功配置点位: 地址:{point.address} ({point.description}) 原始值:{point.raw_hex}")
        save_session_state(self.app)

    def _toggle_current_slave_server(self) -> None:
        """
        响应“启动服务 / 停止服务”切换按钮：停止时不覆盖模型配置；启动前原子校验并应用通信参数且刷新下拉框。
        """
        if not self._current_device_id:
            return
        device = self.app.slave_service.get_device(self._current_device_id)
        if not device:
            return

        if device.is_running:
            self.app.slave_service.stop_slave(device.id)
            self._update_status_indicator(device)
            save_session_state(self.app)
            return

        # 启动前先全部解析并严格校验参数边界到局部变量，校验失败绝不半污染 device.conn_config
        try:
            new_comm = CommType.from_str(self.slave_comm_type_var.get())
            new_host = self.entry_slave_ip.get().strip() or "127.0.0.1"
            new_port = int(self.entry_slave_port.get().strip() or "502")
            if not (1 <= new_port <= 65535):
                raise ValueError(f"TCP 端口超出范围 (1~65535): {new_port}")
            new_com = self.combo_slave_com.get().strip() or "COM1"
            new_baud = int(self.combo_slave_baud.get().strip() or "9600")
            if new_baud <= 0:
                raise ValueError(f"串口波特率必须大于 0: {new_baud}")
            parity_str = self.combo_slave_parity.get().strip()
            new_parity = "E" if "Even" in parity_str or "E" in parity_str else ("O" if "Odd" in parity_str or "O" in parity_str else "N")
            new_dbits = int(self.combo_slave_databit.get().strip() or "8")
            if new_dbits not in (5, 6, 7, 8):
                raise ValueError(f"数据位须为 5/6/7/8: {new_dbits}")
            new_sbits = int(self.combo_slave_stopbit.get().strip() or "1")
            if new_sbits not in (1, 2):
                raise ValueError(f"停止位须为 1 或 2: {new_sbits}")
            new_uid = int(self.entry_slave_id.get().strip() or "1")
            if not (1 <= new_uid <= 247):
                raise ValueError(f"从机站号超出范围 (1~247): {new_uid}")
        except Exception as e:
            messagebox.showerror("配置错误", f"通信参数格式有误: {e}")
            return

        old_uid = device.conn_config.unit_id
        device.conn_config.comm_type = new_comm
        device.conn_config.host = new_host
        device.conn_config.port = new_port
        device.conn_config.com_port = new_com
        device.conn_config.baudrate = new_baud
        device.conn_config.parity = new_parity
        device.conn_config.data_bits = new_dbits
        device.conn_config.stop_bits = new_sbits
        device.conn_config.unit_id = new_uid

        ok, msg = self.app.slave_service.start_slave(device.id)
        if not ok:
            messagebox.showerror("启动从机失败", msg)

        if old_uid != new_uid:
            self._update_instance_combobox()
        else:
            self._update_status_indicator(device)
        save_session_state(self.app)

    def _start_all_slaves(self) -> None:
        """响应“全部启动”按钮：仅对当前选中数据库范围内的未运行从机执行后台批量启动。"""
        if self._busy.get("start"):
            return
        self._busy["start"] = True
        devices = list(self._get_scoped_devices())
        state: Dict[str, Any] = {"done": False, "fail_msgs": []}

        def _worker() -> None:
            try:
                for dev in devices:
                    if not dev.is_running:
                        ok, msg = self.app.slave_service.start_slave(dev.id)
                        if not ok:
                            state["fail_msgs"].append(f"[{dev.name}]: {msg}")
            except Exception as ex:
                state["fail_msgs"].append(f"批量启动异常: {ex}")
            finally:
                state["done"] = True

        def _poll_done() -> None:
            if self._destroyed or not (hasattr(self, "winfo_exists") and self.winfo_exists()):
                self._busy["start"] = False
                return
            if not state["done"]:
                self._schedule_after(30, _poll_done)
                return
            self._busy["start"] = False
            if self._current_device_id:
                dev = self.app.slave_service.get_device(self._current_device_id)
                if dev:
                    self._update_status_indicator(dev)
            if state["fail_msgs"]:
                messagebox.showwarning("部分从机启动提示", "以下从机未能成功启动：\n" + "\n".join(state["fail_msgs"][:5]))

        threading.Thread(target=_worker, daemon=True).start()
        self._schedule_after(30, _poll_done)

    def _stop_all_slaves(self) -> None:
        """响应“全部停止”按钮：仅对当前选中数据库范围内的运行中从机执行后台批量停止。"""
        if self._busy.get("stop"):
            return
        self._busy["stop"] = True
        devices = list(self._get_scoped_devices())
        state: Dict[str, Any] = {"done": False}

        def _worker() -> None:
            try:
                for dev in devices:
                    if dev.is_running:
                        self.app.slave_service.stop_slave(dev.id)
            finally:
                state["done"] = True

        def _poll_done() -> None:
            if self._destroyed or not (hasattr(self, "winfo_exists") and self.winfo_exists()):
                self._busy["stop"] = False
                return
            if not state["done"]:
                self._schedule_after(30, _poll_done)
                return
            self._busy["stop"] = False
            if self._current_device_id:
                dev = self.app.slave_service.get_device(self._current_device_id)
                if dev:
                    self._update_status_indicator(dev)

        threading.Thread(target=_worker, daemon=True).start()
        self._schedule_after(30, _poll_done)

    # [SLAVE-RECONNECT] 自动重连按钮回调：触发底层僵死 listener 探活自愈重建
    def _on_auto_reconnect(self) -> None:
        """响应“自动重连”按钮：在后台线程异步探活并重建僵死 listener，主线程轮询结果更新 UI（零跨线程 Tk 调用）。"""
        if self._busy.get("reconnect"):
            return
        self._busy["reconnect"] = True
        self.lbl_slave_status.config(text="● 正在检测并重建僵死 listener...", foreground="#ffc107")
        result: Dict[str, Any] = {"done": False, "rebuilt": 0, "err": None}

        def _worker() -> None:
            try:
                result["rebuilt"] = self.app.slave_service.auto_reconnect_if_needed()
            except Exception as ex:
                result["err"] = str(ex)
            finally:
                result["done"] = True

        def _poll_result() -> None:
            if self._destroyed or not (hasattr(self, "winfo_exists") and self.winfo_exists()):
                self._busy["reconnect"] = False
                return
            if not result["done"]:
                self._schedule_after(30, _poll_result)
                return
            self._busy["reconnect"] = False
            if result["err"]:
                self.lbl_slave_status.config(text=f"● 自动重连异常: {result['err']}", foreground="#dc3545")
            elif result["rebuilt"] > 0:
                self.lbl_slave_status.config(text=f"● 已重建 {result['rebuilt']} 个僵死 listener", foreground="#28a745")
            else:
                self.lbl_slave_status.config(text="● 未检测到僵死 listener", foreground="#28a745")

            # 3 秒后还原当前选中从机的真实运行状态指示灯，避免“已停止”从机永久显示绿色误导
            def _restore_real_status() -> None:
                if self._current_device_id:
                    cur_dev = self.app.slave_service.get_device(self._current_device_id)
                    if cur_dev:
                        self._update_status_indicator(cur_dev)

            self._schedule_after(3000, _restore_real_status)

        threading.Thread(target=_worker, daemon=True).start()
        self._schedule_after(30, _poll_result)

    def _add_new_slave_instance(self) -> None:
        """手动添加并配置新的 Modbus 从机服务实例（受 100 实例容量配额限制）"""
        ok, quota_err = self.app.slave_service.can_register_device(CommType.TCP)
        if not ok:
            messagebox.showerror("实例配额限制", quota_err)
            return

        devices = self.app.slave_service.get_devices()
        new_unit = min(247, len(devices) + 1)
        name = simpledialog.askstring("新建从机", "请输入从机服务名称:", initialvalue=f"新建服务_{new_unit}")
        if not name or not name.strip():
            return
        name = name.strip()
        new_id = f"slave_{time.time_ns()}"
        # 自动探测从 502 起步的首个未占用 TCP 端口，避免与已有从机或活跃 Hub 端点冲突
        avail_port = self.app.slave_service.get_next_available_tcp_port(start_port=502)
        cfg = ConnectionConfig(
            comm_type=CommType.TCP,
            host="127.0.0.1",
            port=avail_port,
            com_port="COM1",
            baudrate=9600,
            unit_id=new_unit,
        )
        new_dev = SlaveDevice(id=new_id, name=name, conn_config=cfg)
        ok, msg = self.app.slave_service.register_device(new_dev)
        if not ok:
            messagebox.showerror("注册失败", msg)
            return
        self._current_device_id = new_id
        if self._selected_db_filter not in ("__ALL__", "__NONE__"):
            self._selected_db_filter = "__ALL__"
        self._update_instance_combobox()
        save_session_state(self.app)

    def _delete_current_slave_instance(self) -> None:
        """删除当前选中的从机服务实例（至少保留 1 个实例防止工作台完全悬空）。"""
        devices = self.app.slave_service.get_devices()
        if len(devices) <= 1:
            messagebox.showwarning("提示", "至少保留一个从机服务实例！")
            return
        if not self._current_device_id:
            return
        device = self.app.slave_service.get_device(self._current_device_id)
        if not device:
            return
        if messagebox.askyesno("确认删除", f"确定要删除从机实例 [{device.name}] 吗？"):
            self.app.slave_service.remove_device(device.id)
            self._current_device_id = None
            self._update_instance_combobox()
            save_session_state(self.app)

    def _select_all_points(self) -> None:
        """全选当前点表 Treeview 中的所有点位行（单次批量 Tcl 调用防大表卡顿）。"""
        self.tree.selection_set(self.tree.get_children())

    def _deselect_all_points(self) -> None:
        """取消当前点表 Treeview 中所有点位行的选中状态。"""
        self.tree.selection_set(())

    def _batch_delete_selected(self) -> None:
        """从当前从机实例中批量删除用户在点表 Treeview 中选中的点位行。"""
        selected = self.tree.selection()
        if not selected or not self._current_device_id:
            messagebox.showinfo("提示", "请先在点表中选中要删除的点位行")
            return
        device = self.app.slave_service.get_device(self._current_device_id)
        if not device:
            return
        for item in selected:
            vals = self.tree.item(item, "values")
            if vals:
                addr = int(vals[0])
                area = vals[2]
                device.remove_point(area, addr)
                self.app.slave_service.remove_point_from_runtime(device.id, area, addr)
        self._refresh_tree(device)
        save_session_state(self.app)

    def _apply_batch_sim(self) -> None:
        """将下拉框选中的动态模拟规则批量应用到目标范围内的点位。"""
        sim = self.combo_batch_sim.get()
        self._apply_batch_point_property("sim_rule", sim)

    def _apply_batch_order(self) -> None:
        """将下拉框选中的字节序（变位模式）批量应用到目标范围内的点位并重算原始寄存器值。"""
        order = self.combo_batch_order.get()
        self._apply_batch_point_property("byte_order", order)

    def _apply_batch_type(self) -> None:
        """将下拉框选中的数据类型批量应用到目标范围内的点位并重新校验编码。"""
        dt = self.normalize_data_type_choice(self.combo_batch_type.get())
        self._apply_batch_point_property("data_type", dt)

    def _apply_batch_value(self) -> None:
        """将输入框中的工程数值批量赋给目标范围内的点位并同步到底层运行引擎。"""
        val = self.entry_batch_val.get()
        self._apply_batch_point_property("value", val)

    def _apply_batch_scale(self) -> None:
        """将输入框中的缩放系数批量应用到目标范围内的数值型点位并按新系数重算寄存器。"""
        import math
        raw = self.entry_batch_scale.get().strip()
        try:
            s_val = float(raw)
            if not math.isfinite(s_val) or s_val == 0.0:
                raise ValueError("系数必须为非零有限数字")
        except Exception as ex:
            messagebox.showerror("参数错误", f"无效的系数输入: {ex}")
            return
        self._apply_batch_point_property("scale", s_val)

    def _apply_batch_offset(self) -> None:
        """将输入框中的偏移量批量应用到目标范围内的数值型点位并按新偏移重算寄存器。"""
        import math
        raw = self.entry_batch_offset.get().strip()
        try:
            o_val = float(raw)
            if not math.isfinite(o_val):
                raise ValueError("偏移量必须为有限数字")
        except Exception as ex:
            messagebox.showerror("参数错误", f"无效的偏移量输入: {ex}")
            return
        self._apply_batch_point_property("offset", o_val)

    def _apply_batch_sim_range(self) -> None:
        """将输入框中的动态波动下限与上限批量应用到目标范围内的数值型点位并同步运行时引擎。"""
        import math
        try:
            min_v = float(self.entry_batch_sim_min.get().strip())
            max_v = float(self.entry_batch_sim_max.get().strip())
            if not (math.isfinite(min_v) and math.isfinite(max_v)):
                raise ValueError("上下限必须为有限数字")
            if min_v > max_v:
                raise ValueError(f"波动下限 ({min_v:g}) 不能大于上限 ({max_v:g})")
        except Exception as ex:
            messagebox.showerror("参数错误", f"无效的波动范围输入: {ex}")
            return
        self._apply_batch_point_property("sim_range", (min_v, max_v))

    def _apply_batch_point_property(self, prop_name: str, prop_val: Any) -> None:
        """通用批量属性修改器：支持“仅当前从机（若选中行则仅限选中行）”或“全部从机服务”两级作用范围。"""
        # 参数：
        # prop_name: 待修改的属性名（`"sim_rule"` / `"byte_order"` / `"data_type"` / `"value"`）。
        # prop_val: 目标属性设定值。
        scope = self.batch_scope_var.get()
        target_devices = (
            self._get_scoped_devices()
            if scope == "all"
            else ([self.app.slave_service.get_device(self._current_device_id)] if self._current_device_id else [])
        )

        selected_items = self.tree.selection()
        selected_addrs = set()
        if selected_items and scope == "current":
            for itm in selected_items:
                vals = self.tree.item(itm, "values")
                if vals:
                    selected_addrs.add((vals[2], int(vals[0])))

        count = 0
        skipped = 0
        for dev in target_devices:
            if not dev:
                continue
            # 使用列表快照遍历：当批量切换为 BOOL 触发存储区迁移并重设 dev.points 主键时，避免迭代期改字典大小报错
            for point in list(dev.points.values()):
                if selected_addrs and (point.area.friendly_name, point.address) not in selected_addrs:
                    continue

                if prop_name == "sim_rule":
                    point.sim_rule = SimRule.normalize(prop_val)
                elif prop_name == "byte_order":
                    point.byte_order = ByteOrder.normalize(prop_val)
                    PointService.re_encode_point(point)
                elif prop_name == "data_type":
                    old_area = point.area
                    ok, new_pt, _ = PointService.validate_and_build_point(
                        point.address,
                        point.description,
                        point.area,
                        prop_val,
                        point.byte_order,
                        point.value,
                        point.sim_rule,
                        string_length=point.string_length,
                        field_name=point.field_name,
                        scale=point.scale,
                        offset=point.offset,
                        unit=point.unit,
                    )
                    if ok and new_pt:
                        if new_pt.area != old_area:
                            # 冲突防护：目标区已存在同地址点位时跳过迁移，避免静默覆盖丢失数据
                            existing_target = dev.get_point(new_pt.area, point.address)
                            if existing_target is not None and existing_target is not point:
                                skipped += 1
                                continue
                            dev.remove_point(old_area, point.address)
                            self.app.slave_service.remove_point_from_runtime(dev.id, old_area, point.address)
                            point.area = new_pt.area
                            dev.add_point(point)
                        point.data_type = new_pt.data_type
                        point.value = new_pt.value
                        point.raw_hex = new_pt.raw_hex
                    else:
                        skipped += 1
                        continue
                elif prop_name == "value":
                    ok, new_pt, _ = PointService.validate_and_build_point(
                        point.address,
                        point.description,
                        point.area,
                        point.data_type,
                        point.byte_order,
                        prop_val,
                        point.sim_rule,
                        string_length=point.string_length,
                        field_name=point.field_name,
                        scale=point.scale,
                        offset=point.offset,
                        unit=point.unit,
                    )
                    if ok and new_pt:
                        point.value = new_pt.value
                        point.raw_hex = new_pt.raw_hex
                    else:
                        skipped += 1
                        continue
                elif prop_name in ("scale", "offset"):
                    from ..modbus_codec import is_numeric_codec_type
                    if not is_numeric_codec_type(point.data_type.value):
                        skipped += 1
                        continue
                    new_scale = float(prop_val) if prop_name == "scale" else point.scale
                    new_offset = float(prop_val) if prop_name == "offset" else point.offset
                    ok, new_pt, _ = PointService.validate_and_build_point(
                        point.address,
                        point.description,
                        point.area,
                        point.data_type,
                        point.byte_order,
                        point.value,
                        point.sim_rule,
                        string_length=point.string_length,
                        field_name=point.field_name,
                        scale=new_scale,
                        offset=new_offset,
                        unit=point.unit,
                    )
                    if ok and new_pt:
                        point.scale = new_pt.scale
                        point.offset = new_pt.offset
                        point.value = new_pt.value
                        point.raw_hex = new_pt.raw_hex
                    else:
                        skipped += 1
                        continue
                elif prop_name == "sim_range":
                    from ..modbus_codec import is_numeric_codec_type
                    if not is_numeric_codec_type(point.data_type.value):
                        skipped += 1
                        continue
                    min_v, max_v = prop_val
                    point.sim_min = float(min_v)
                    point.sim_max = float(max_v)

                self.app.slave_service.sync_point_to_runtime(dev.id, point)
                count += 1

        if self._current_device_id:
            dev = self.app.slave_service.get_device(self._current_device_id)
            if dev:
                self._refresh_tree(dev)
        save_session_state(self.app)
        if skipped > 0:
            messagebox.showinfo("批量操作完成", f"已成功更新 {count} 个点位属性，跳过 {skipped} 个校验失败/非数值或目标地址冲突的点位")
        else:
            messagebox.showinfo("批量操作完成", f"已成功更新 {count} 个点位属性")

    def _on_tree_double_click(self, event=None) -> None:
        """
        响应点表 Treeview 双击事件：双击“系数”列改系数、双击“偏移量”列改偏移量、双击其他列改目标工程数值，
        校验通过后按公式反算原始寄存器并同步至底层运行引擎。
        """
        if event is None:
            return
        item = self.tree.identify_row(event.y)
        if not item or not self._current_device_id:
            return
        vals = self.tree.item(item, "values")
        if not vals:
            return

        addr, desc, area = int(vals[0]), vals[1], vals[2]
        device = self.app.slave_service.get_device(self._current_device_id)
        if not device:
            return
        point = device.get_point(area, addr)
        if not point:
            return

        col_token = ""
        if hasattr(event, "x"):
            try:
                col_token = self.tree.identify_column(event.x)
            except Exception:
                col_token = ""

        # 列索引：#6=scale(系数), #7=offset(偏移量), 其余列默认修改目标工程数值
        if col_token in ("#6", "#7"):
            from ..modbus_codec import is_numeric_codec_type
            if not is_numeric_codec_type(point.data_type.value):
                messagebox.showinfo("提示", f"{point.data_type.value} 类型点位不参与系数与偏移量换算")
                return
            is_scale_col = (col_token == "#6")
            label = "系数 (Scale Factor)" if is_scale_col else "偏移量 (Offset)"
            cur_param = point.scale if is_scale_col else point.offset
            raw_input = simpledialog.askstring(
                f"修改{label}",
                f"修改点位 [地址:{addr} ({desc})]\n当前{label}: {cur_param:g}\n请输入新{label}:",
                initialvalue=f"{cur_param:g}",
            )
            if raw_input is None:
                return
            try:
                import math
                parsed_num = float(raw_input.strip())
                if not math.isfinite(parsed_num) or (is_scale_col and parsed_num == 0.0):
                    raise ValueError("系数必须为非零有限数字" if is_scale_col else "偏移量必须为有限数字")
            except Exception as ex:
                messagebox.showerror("修改失败", f"{label}格式有误: {ex}")
                return
            new_scale = parsed_num if is_scale_col else point.scale
            new_offset = parsed_num if not is_scale_col else point.offset
            ok, updated_pt, err = PointService.validate_and_build_point(
                addr,
                desc,
                point.area,
                point.data_type,
                point.byte_order,
                point.value,
                point.sim_rule,
                string_length=point.string_length,
                field_name=point.field_name,
                scale=new_scale,
                offset=new_offset,
                unit=point.unit,
            )
            if not ok or not updated_pt:
                messagebox.showerror("修改失败", err)
                return
            point.scale = updated_pt.scale
            point.offset = updated_pt.offset
            point.value = updated_pt.value
            point.raw_hex = updated_pt.raw_hex
            self.app.slave_service.sync_point_to_runtime(device.id, point)
            self._refresh_tree(device)
            save_session_state(self.app)
            return

        cur_val = point.value
        new_val = simpledialog.askstring(
            "修改点位数值",
            f"修改点位 [地址:{addr} ({desc})]\n当前值: {cur_val} (系数:{point.scale:g}, 偏移:{point.offset:g})\n请输入新数值:",
            initialvalue=str(cur_val),
        )
        if new_val is None:
            return

        ok, updated_pt, err = PointService.validate_and_build_point(
            addr,
            desc,
            point.area,
            point.data_type,
            point.byte_order,
            new_val,
            point.sim_rule,
            string_length=point.string_length,
            field_name=point.field_name,
            scale=point.scale,
            offset=point.offset,
            unit=point.unit,
        )
        if not ok or not updated_pt:
            messagebox.showerror("修改失败", err)
            return

        point.value = updated_pt.value
        point.raw_hex = updated_pt.raw_hex
        self.app.slave_service.sync_point_to_runtime(device.id, point)
        self._refresh_tree(device)
        save_session_state(self.app)

    def _sort_by_column(self, col: str, reverse: bool = False) -> None:
        """点击表头自动正序/倒序排列"""
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        if col == "area":
            items.sort(key=lambda t: AreaType.normalize(t[0]).value, reverse=reverse)
        else:
            try:
                items.sort(key=lambda t: float(t[0]), reverse=reverse)
            except (ValueError, TypeError):
                items.sort(key=lambda t: t[0], reverse=reverse)

        for index, (_, k) in enumerate(items):
            self.tree.move(k, "", index)

        self.tree.heading(col, command=lambda: self._sort_by_column(col, not reverse))

    # =========================================================================
    # 外部对话框桥接 (批量规则生成、数据库导入与业务配对)
    # =========================================================================

    def _open_batch_generate_dialog(self) -> None:
        """弹出批量规则点表生成对话框"""
        if not self._current_device_id or not self.app.slave_service.get_device(self._current_device_id):
            messagebox.showwarning("提示", "请先选择或新建一个从机服务实例")
            return

        dlg = tk.Toplevel(self)
        dlg.title("批量输入规则生成点表")
        dlg.geometry("450x510")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()

        ttk.Label(dlg, text="批量点表规则生成向导", font=("Microsoft YaHei UI", 10, "bold")).pack(pady=8)
        f = ttk.Frame(dlg)
        f.pack(fill="x", padx=16, pady=4)

        ttk.Label(f, text="起始地址:").grid(row=0, column=0, sticky="w", pady=4)
        e_addr = ttk.Entry(f, width=12)
        e_addr.insert(0, "1")
        e_addr.grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(f, text="点位数量:").grid(row=1, column=0, sticky="w", pady=4)
        e_cnt = ttk.Entry(f, width=12)
        e_cnt.insert(0, "10")
        e_cnt.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(f, text="存储区域:").grid(row=2, column=0, sticky="w", pady=4)
        c_area = ttk.Combobox(f, state="readonly", width=16, values=self.AREA_CHOICES)
        c_area.set(AreaType.HOLDING.friendly_name)
        c_area.grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(f, text="数据类型:").grid(row=3, column=0, sticky="w", pady=4)
        c_type = ttk.Combobox(f, state="readonly", width=12, values=self.DATA_TYPE_CHOICES)
        c_type.set("FLOAT32")
        c_type.grid(row=3, column=1, sticky="w", pady=4)

        ttk.Label(f, text="变位模式:").grid(row=4, column=0, sticky="w", pady=4)
        c_order = ttk.Combobox(f, state="readonly", width=10, values=self.BYTE_ORDER_CHOICES)
        c_order.set("ABCD")
        c_order.grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(f, text="模拟规则:").grid(row=5, column=0, sticky="w", pady=4)
        c_sim = ttk.Combobox(f, state="readonly", width=10, values=self.SIM_RULE_CHOICES)
        c_sim.set("随机波动")
        c_sim.grid(row=5, column=1, sticky="w", pady=4)

        ttk.Label(f, text="缩放系数:").grid(row=6, column=0, sticky="w", pady=4)
        e_scale = ttk.Entry(f, width=12)
        e_scale.insert(0, "1")
        e_scale.grid(row=6, column=1, sticky="w", pady=4)

        ttk.Label(f, text="偏移量:").grid(row=7, column=0, sticky="w", pady=4)
        e_offset = ttk.Entry(f, width=12)
        e_offset.insert(0, "0")
        e_offset.grid(row=7, column=1, sticky="w", pady=4)

        ttk.Label(f, text="波动下限:").grid(row=8, column=0, sticky="w", pady=4)
        e_sim_min = ttk.Entry(f, width=12)
        e_sim_min.insert(0, "-1000")
        e_sim_min.grid(row=8, column=1, sticky="w", pady=4)

        ttk.Label(f, text="波动上限:").grid(row=9, column=0, sticky="w", pady=4)
        e_sim_max = ttk.Entry(f, width=12)
        e_sim_max.insert(0, "1000")
        e_sim_max.grid(row=9, column=1, sticky="w", pady=4)

        def do_generate():
            """根据向导输入的起始地址、数量、系数、偏移量与波动范围，批量构建点位并同步到当前从机。"""
            if not self._current_device_id:
                return
            device = self.app.slave_service.get_device(self._current_device_id)
            if not device:
                return
            try:
                import math
                start_a = int(e_addr.get().strip())
                count = int(e_cnt.get().strip())
                area = c_area.get()
                dt = self.normalize_data_type_choice(c_type.get())
                bo = c_order.get()
                sim = c_sim.get()
                scale_v = float(e_scale.get().strip() or "1")
                if not math.isfinite(scale_v) or scale_v == 0.0:
                    raise ValueError("缩放系数必须为非零有限数字")
                offset_v = float(e_offset.get().strip() or "0")
                if not math.isfinite(offset_v):
                    raise ValueError("偏移量必须为有限数字")
                sim_min_v = float(e_sim_min.get().strip() or "-1000")
                sim_max_v = float(e_sim_max.get().strip() or "1000")
                if not (math.isfinite(sim_min_v) and math.isfinite(sim_max_v)):
                    raise ValueError("波动上下限必须为有限数字")
                if sim_min_v > sim_max_v:
                    raise ValueError("波动下限不能大于上限")
            except Exception as ex:
                messagebox.showerror("错误", f"输入格式有误: {ex}", parent=dlg)
                return

            dt_enum = DataType.normalize(dt)
            step = dt_enum.register_count
            if not (0 <= start_a <= 65535):
                messagebox.showerror("参数错误", f"起始地址必须在 0~65535 之间: {start_a}", parent=dlg)
                return
            if not (1 <= count <= 2000):
                messagebox.showerror("参数错误", f"单次批量生成数量必须在 1~2000 之间: {count}", parent=dlg)
                return
            end_a = start_a + (count - 1) * step
            if end_a > 65535:
                messagebox.showerror("参数错误", f"末点位地址 ({end_a}) 超出 65535 上限，请调小起始地址或数量", parent=dlg)
                return

            cur_a = start_a
            created_cnt = 0
            for i in range(count):
                desc = f"AutoPoint_{cur_a}"
                ok, pt, _ = PointService.validate_and_build_point(
                    cur_a, desc, area, dt, bo, i + 1, sim,
                    scale=scale_v, offset=offset_v, sim_min=sim_min_v, sim_max=sim_max_v,
                )
                if ok and pt:
                    device.add_point(pt)
                    self.app.slave_service.sync_point_to_runtime(device.id, pt)
                    created_cnt += 1
                cur_a += step

            self._refresh_tree(device)
            save_session_state(self.app)
            dlg.destroy()
            messagebox.showinfo("生成成功", f"成功批量生成 {created_cnt} 个点位")

        btn_box = ttk.Frame(dlg)
        btn_box.pack(fill="x", padx=16, pady=12)
        ttk.Button(btn_box, text="立即生成", command=do_generate).pack(side="right", padx=4)
        ttk.Button(btn_box, text="取消", command=dlg.destroy).pack(side="right", padx=4)

    def _select_database_file(self, auto_open_import: bool = True) -> None:
        """关联或切换业务数据库文件：支持保留现有从机叠加多库，也支持清空现有从机全新切换。"""
        if self.app.db_path and os.path.exists(self.app.db_path):
            init_dir = os.path.dirname(self.app.db_path)
        else:
            from ..app import resolve_runtime_path
            init_dir = resolve_runtime_path("extra")

        db_path = filedialog.askopenfilename(
            title="选择业务数据库文件 (支持多库叠加或清空切换)",
            filetypes=[("HEMS 数据库", "*.cdb;*.sqlite;*.db"), ("所有文件", "*.*")],
            initialdir=init_dir,
        )
        if not (db_path and os.path.exists(db_path)):
            return

        # 引导用户为该数据库设置自定义标签（如“1号站-储能”），多库共存且文件名均为 hems.cdb 时一目了然
        parent_name = os.path.basename(os.path.dirname(os.path.abspath(db_path)))
        base_name = os.path.basename(db_path)
        default_tag = (
            self.app.get_db_label(db_path)
            if hasattr(self.app, "has_custom_db_label") and self.app.has_custom_db_label(db_path)
            else (f"{parent_name}/{base_name}" if parent_name else base_name)
        )
        custom_tag = simpledialog.askstring(
            "设置数据库标签",
            f"请为该数据库设置便于识别的标签名称（留空则使用文件名）：\n{db_path}",
            initialvalue=default_tag,
            parent=self,
        )
        pending_tag = custom_tag.strip() if custom_tag is not None else ""
        display_tag = (
            pending_tag
            if (custom_tag is not None and pending_tag)
            else (self.app.get_db_label(db_path) if hasattr(self.app, "get_db_label") else base_name)
        )

        old_count = len(self.app.slave_service.get_devices())
        keep_existing = True
        if old_count > 0:
            choice = messagebox.askyesnocancel(
                "切换数据库方式",
                f"已选择数据库 [{display_tag}]：\n{db_path}\n\n"
                f"当前已有 {old_count} 个从机实例，请选择处理方式：\n"
                "• 【是(推荐)】：保留现有从机，直接叠加导入新数据库（无需多开软件）\n"
                "• 【否】：停止并清空现有从机后，全新切换到该数据库\n"
                "• 【取消】：放弃本次切换",
                parent=self,
            )
            if choice is None:
                return
            keep_existing = bool(choice)

        # 确认不取消后再写入自定义标签，避免取消切库时脏写 db_labels
        if custom_tag is not None and hasattr(self.app, "set_db_label"):
            self.app.set_db_label(db_path, custom_tag)
        display_tag = self.app.get_db_label(db_path) if hasattr(self.app, "get_db_label") else base_name

        self.app.db_path = db_path
        self._selected_db_filter = self._norm_db_key(db_path)
        if not keep_existing and old_count > 0:
            self._clear_all_slaves_and_reset_ui()
            save_session_state(self.app)
            self.app.logging_service.post(
                "SYS",
                0,
                f"已切换业务数据库 [{display_tag}]: {db_path}，并已停止清空原有的 {old_count} 个从机服务实例",
            )
        else:
            self._update_instance_combobox()
            save_session_state(self.app)
            self.app.logging_service.post(
                "SYS",
                0,
                f"已切换当前导入源数据库 [{display_tag}]: {db_path}（已保留现有 {old_count} 个从机实例，准备导入点表）",
            )

        # 仅当用户主动点击“关联/添加数据库”时自动弹出“导入设备点表”弹窗，避免被其他弹窗兜底调用时重复弹窗
        if auto_open_import:
            self._open_import_db_dialog()

    def _open_import_db_dialog(self) -> None:
        """打开从业务数据库导入设备模型与点表弹窗 (支持多选批量与容量配额限制)"""
        from .dialogs import DbImportDialog

        # 若顶部数据库下拉框选中了具体库，优先以该库为导入源
        selected_db_path = self._db_filter_paths.get(self._selected_db_filter)
        if selected_db_path and os.path.exists(selected_db_path):
            self.app.db_path = selected_db_path

        if not self.app.db_path or not os.path.exists(self.app.db_path):
            self._select_database_file(auto_open_import=False)
            if not self.app.db_path or not os.path.exists(self.app.db_path):
                return

        def on_imported(target_dev: SlaveDevice) -> None:
            """当数据库导入对话框完成设备与点表导入后，自动聚焦至该设备所属数据库并刷新界面。"""
            if getattr(target_dev, "db_file", None):
                self._selected_db_filter = self._norm_db_key(target_dev.db_file)
            self._current_device_id = target_dev.id
            self._update_instance_combobox()
            self._load_device_to_ui(target_dev.id)
            self._refresh_tree(target_dev)
            save_session_state(self.app)

        DbImportDialog(
            parent=self,
            app=self.app,
            on_imported_callback=on_imported,
            current_device_id=self._current_device_id,
        )

    def _open_pairing_dialog(self) -> None:
        """打开业务配对关系中心对话框（优先使用当前选中从机的来源数据库，实现多库共存独立查询）"""
        from .dialogs import PairingDialog

        target_db = None
        if self._current_device_id:
            dev = self.app.slave_service.get_device(self._current_device_id)
            if dev and getattr(dev, "db_file", None) and os.path.exists(dev.db_file):
                target_db = dev.db_file
        if not target_db:
            target_db = self.app.db_path

        if not target_db or not os.path.exists(target_db):
            self._select_database_file(auto_open_import=False)
            target_db = self.app.db_path
            if not target_db or not os.path.exists(target_db):
                return

        PairingDialog(parent=self, app=self.app, db_path=target_db)

