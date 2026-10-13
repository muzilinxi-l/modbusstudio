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
import traceback
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

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
        # [SLAVE-SPEED-4] 防止全部启动/全部停止重复点击并发重入的守卫标志
        self._busy: Dict[str, bool] = {"start": False, "stop": False}

        self._build_ui()
        self._load_initial_data()
        # 延迟 1000ms 启动主线程定时刷新循环，将后台仿真线程产生的波形数值平滑同步到 Treeview 单元格
        self.after(1000, self._start_runtime_sync_loop)

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

        # --- 第 0 行：从机服务实例切换与新建/删除（独占一行） ---
        row0 = ttk.Frame(top_box)
        row0.pack(fill="x", padx=4, pady=2)

        left0 = ttk.Frame(row0)
        left0.pack(side="left", fill="x", expand=True)

        ttk.Label(left0, text="从机服务实例:").pack(side="left", padx=(2, 4))
        self.combo_instances = ttk.Combobox(left0, state="readonly", width=22)
        self.combo_instances.pack(side="left", padx=2)
        self.combo_instances.bind("<<ComboboxSelected>>", self._on_switch_slave_instance)

        btn_new_inst = ttk.Button(left0, text="➕ 新建服务", style="primary.Outline.TButton", command=self._add_new_slave_instance)
        btn_new_inst.pack(side="left", padx=2)

        btn_del_inst = ttk.Button(left0, text="🗑️ 删除服务", style="danger.Outline.TButton", command=self._delete_current_slave_instance)
        btn_del_inst.pack(side="left", padx=2)

        # --- 第 0b 行：服务控制按钮组（启动/重启服务 + 状态标签 + 全部启停 + 自动重连，拆出独占一行） ---
        row0b = ttk.Frame(top_box)
        row0b.pack(fill="x", padx=4, pady=2)

        right0 = ttk.Frame(row0b)
        right0.pack(side="left")

        self.btn_toggle_slave = ttk.Button(right0, text="▶️ 启动服务", style="success.TButton", command=self._toggle_current_slave_server)
        self.btn_toggle_slave.pack(side="left", padx=3)

        self.lbl_slave_status = ttk.Label(right0, text="未运行", foreground="#6c757d", font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_slave_status.pack(side="left", padx=4)

        btn_start_all = ttk.Button(right0, text="⚡ 全部启动", style="success.Outline.TButton", command=self._start_all_slaves)
        btn_start_all.pack(side="left", padx=2)

        btn_stop_all = ttk.Button(right0, text="⏹ 全部停止", style="danger.Outline.TButton", command=self._stop_all_slaves)
        btn_stop_all.pack(side="left", padx=2)

        # [SLAVE-RECONNECT] 追加"自动重连"按钮，用于触发底层僵死 listener 探活自愈重建
        btn_reconnect = ttk.Button(right0, text="🔄 自动重连", style="secondary.Outline.TButton", command=self._on_auto_reconnect)
        btn_reconnect.pack(side="left", padx=2)

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
        btn_com_refresh = ttk.Button(self.rtu_frame, text="🔄", width=3, style="Small.TButton", command=self._refresh_slave_com_ports)
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

        # --- 第 1b 行：关联数据库操作行（拆出独占一行） ---
        row1b = ttk.Frame(top_box)
        row1b.pack(fill="x", padx=4, pady=2)

        btn_select_db = ttk.Button(row1b, text="🗃️ 关联/切换数据库...", style="secondary.Outline.TButton", command=self._select_database_file)
        btn_select_db.pack(side="left", padx=2)

        # --- 第 1c 行：导入设备点表与业务配对操作行（依次换行独占一行） ---
        row1c = ttk.Frame(top_box)
        row1c.pack(fill="x", padx=4, pady=2)

        btn_import_db = ttk.Button(row1c, text="📂 导入设备点表...", style="info.TButton", command=self._open_import_db_dialog)
        btn_import_db.pack(side="left", padx=2)

        btn_pair_center = ttk.Button(row1c, text="🔗 业务配对中心 (基于 More)...", style="primary.Outline.TButton", command=self._open_pairing_dialog)
        btn_pair_center.pack(side="left", padx=2)

        # 2. 单点配置与快速生成栏
        pt_box = ttk.LabelFrame(self, text="点位规则生成 & 快捷操作")
        pt_box.pack(fill="x", padx=2, pady=2)

        row_pt = ttk.Frame(pt_box)
        row_pt.pack(fill="x", padx=2, pady=2)

        btn_batch_gen = ttk.Button(row_pt, text="⚡ 批量输入规则生成点表...", style="primary.Outline.TButton", command=self._open_batch_generate_dialog)
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

        ttk.Label(row_pt, text="模拟:").pack(side="left")
        self.combo_pt_sim = ttk.Combobox(row_pt, state="readonly", width=7, values=self.SIM_RULE_CHOICES)
        self.combo_pt_sim.set("固定")
        self.combo_pt_sim.pack(side="left", padx=2)

        btn_add_pt = ttk.Button(row_pt, text="➕ 单点添加", style="success.TButton", command=self._on_click_add_point)
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

        btn_apply_sim = ttk.Button(b_row0, text="⚡ 批量修改规则", style="secondary.TButton", command=self._apply_batch_sim)
        btn_apply_sim.pack(side="left", padx=3)

        btn_sel_all = ttk.Button(b_row0, text="☑ 全选点位", style="Small.TButton", command=self._select_all_points)
        btn_sel_all.pack(side="left", padx=3)

        btn_desel_all = ttk.Button(b_row0, text="☐ 取消选择", style="Small.TButton", command=self._deselect_all_points)
        btn_desel_all.pack(side="left", padx=3)

        btn_del_sel = ttk.Button(b_row0, text="🗑️ 批量删除选中", style="danger.Outline.TButton", command=self._batch_delete_selected)
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

        # 4. 点表表格区域
        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=2, pady=2)

        self.tree_scroll_y = ttk.Scrollbar(table_frame, orient="vertical")
        self.tree_scroll_y.pack(side="right", fill="y")
        self.tree_scroll_x = ttk.Scrollbar(table_frame, orient="horizontal")
        self.tree_scroll_x.pack(side="bottom", fill="x")

        self.columns = ("addr", "desc", "area", "type", "order", "val", "raw_hex", "sim", "reg_cnt")
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
            ("addr", "起始地址", 0.08, 80, "center"),
            ("desc", "点位描述 / 业务字段", 0.28, 220, "w"),
            ("area", "存储区域", 0.12, 115, "center"),
            ("type", "数据类型", 0.09, 85, "center"),
            ("order", "变位模式", 0.08, 75, "center"),
            ("val", "当前解析数值", 0.11, 100, "center"),
            ("raw_hex", "原始寄存器(Hex)", 0.12, 120, "center"),
            ("sim", "动态模拟", 0.07, 80, "center"),
            ("reg_cnt", "占用字数", 0.05, 60, "center"),
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
        # 在 [_apply_table_resize] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._last_rendered_width = self._pending_width
        # 在 [_apply_table_resize] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for col_id, _, ratio, min_w, _ in self._table_col_configs:
            # 在 [_apply_table_resize] 中更新局部变量 [dyn_w]：为后续协议编码、服务调度或界面渲染准备中间状态
            dyn_w = max(min_w, int(avail_w * ratio))
            self.tree.column(col_id, width=dyn_w)

    # =========================================================================
    # 数据加载与视图响应
    # =========================================================================

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_load_initial_data]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _load_initial_data(self) -> None:
        """面板首次挂载时刷新本机可用 COM 串口列表并初始化从机实例下拉框。"""
        self._refresh_slave_com_ports()
        self._update_instance_combobox()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_refresh_slave_com_ports]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _refresh_slave_com_ports(self) -> None:
        """扫描宿主系统物理/虚拟串口并更新 `combo_slave_com` 下拉选项（无可用串口时回退至 COM1~COM4）。"""
        # 在 [_refresh_slave_com_ports] 中更新局部变量 [ports]：为后续协议编码、服务调度或界面渲染准备中间状态
        ports = []
        # 在 [_refresh_slave_com_ports] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if serial:
            # 在 [_refresh_slave_com_ports] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                # 在 [_refresh_slave_com_ports] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
                for p in serial.tools.list_ports.comports():
                    ports.append(p.device)
            except Exception:
                pass
        # 在 [_refresh_slave_com_ports] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not ports:
            # 在 [_refresh_slave_com_ports] 中更新局部变量 [ports]：为后续协议编码、服务调度或界面渲染准备中间状态
            ports = ["COM1", "COM2", "COM3", "COM4"]
        self.combo_slave_com["values"] = ports
        # 在 [_refresh_slave_com_ports] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self.combo_slave_com.get() and ports:
            self.combo_slave_com.set(ports[0])

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_update_instance_combobox]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _update_instance_combobox(self) -> None:
        """
        从 `SlaveService` 获取最新注册的从机列表，刷新顶部实例选择下拉框并加载当前选中从机的配置与点表。
        """
        # 在 [_update_instance_combobox] 中更新局部变量 [devices]：为后续协议编码、服务调度或界面渲染准备中间状态
        devices = self.app.slave_service.get_devices()
        # 在 [_update_instance_combobox] 中更新局部变量 [device_names]：为后续协议编码、服务调度或界面渲染准备中间状态
        device_names = [f"从机{dev.conn_config.unit_id} [{dev.name}]" for dev in devices]
        self.combo_instances["values"] = device_names
        # 在 [_update_instance_combobox] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if devices:
            # 在 [_update_instance_combobox] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not self._current_device_id or self._current_device_id not in [d.id for d in devices]:
                # 在 [_update_instance_combobox] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                self._current_device_id = devices[0].id
                self.combo_instances.current(0)
            else:
                # 在 [_update_instance_combobox] 中更新局部变量 [idx]：为后续协议编码、服务调度或界面渲染准备中间状态
                idx = [d.id for d in devices].index(self._current_device_id)
                self.combo_instances.current(idx)
            self._load_device_to_ui(self._current_device_id)
        else:
            # 在 [_update_instance_combobox] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._current_device_id = None
            self.combo_instances.set("（暂无从机实例，请点击【导入设备点表】或【新建服务】）")
            # 在 [_update_instance_combobox] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for item in self.tree.get_children():
                self.tree.delete(item)
            self.lbl_slave_status.config(text="● 未就绪 (无活动从机)", foreground="#6c757d")
            self.btn_toggle_slave.config(text="▶️ 启动服务", style="secondary.TButton")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_clear_all_slaves_and_reset_ui]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _clear_all_slaves_and_reset_ui(self) -> None:
        """清空所有从机服务实例并重置主工作台 UI 界面"""
        self.app.slave_service.clear_all_devices()
        # 在 [_clear_all_slaves_and_reset_ui] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._current_device_id = None
        self._update_instance_combobox()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_load_device_to_ui]（入参: device_id）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _load_device_to_ui(self, device_id: str) -> None:
        """将指定 `device_id` 对应的 `SlaveDevice` 通信参数、运行状态与点表数据完整回填到面板控件。"""
        # 参数：
        # device_id: 目标从机实例唯一标识。
        # 在 [_load_device_to_ui] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
        device = self.app.slave_service.get_device(device_id)
        # 在 [_load_device_to_ui] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 从 [_load_device_to_ui] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_load_device_to_ui] 中更新局部变量 [cfg]：为后续协议编码、服务调度或界面渲染准备中间状态
        cfg = device.conn_config
        self.slave_comm_type_var.set(cfg.comm_type.value)
        self._on_slave_comm_type_change()

        self.entry_slave_ip.delete(0, "end")
        self.entry_slave_ip.insert(0, cfg.host)
        self.entry_slave_port.delete(0, "end")
        self.entry_slave_port.insert(0, str(cfg.port))
        self.combo_slave_com.set(cfg.com_port)
        self.combo_slave_baud.set(str(cfg.baudrate))
        # 在 [_load_device_to_ui] 中更新局部变量 [parity_map]：为后续协议编码、服务调度或界面渲染准备中间状态
        parity_map = {"N": "None (N)", "E": "Even (E)", "O": "Odd (O)"}
        self.combo_slave_parity.set(parity_map.get(cfg.parity, "None (N)"))
        self.combo_slave_databit.set(str(cfg.data_bits))
        self.combo_slave_stopbit.set(str(cfg.stop_bits))
        self.entry_slave_id.delete(0, "end")
        self.entry_slave_id.insert(0, str(cfg.unit_id))

        self._update_status_indicator(device)
        self._refresh_tree(device)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_update_status_indicator]（入参: device）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _update_status_indicator(self, device: SlaveDevice) -> None:
        """根据 `device.is_running` 更新顶部状态指示灯颜色文本及启停切换按钮样式。"""
        # 参数：
        # device: 当前选中的从机领域实体。
        # 在 [_update_status_indicator] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if device.is_running:
            self.lbl_slave_status.config(text=f"● 运行中 ({device.conn_config.summary})", foreground="#28a745")
            self.btn_toggle_slave.config(text="⏹ 停止服务", style="danger.TButton")
        else:
            self.lbl_slave_status.config(text="● 已停止", foreground="#dc3545")
            self.btn_toggle_slave.config(text="▶️ 启动服务", style="success.TButton")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_slave_comm_type_change]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _on_slave_comm_type_change(self) -> None:
        """当用户切换 TCP / RTU 单选框时，动态隐藏或显示对应的 IP:Port 输入框与串口五要素控件组。"""
        # 在 [_on_slave_comm_type_change] 中更新局部变量 [mode]：为后续协议编码、服务调度或界面渲染准备中间状态
        mode = self.slave_comm_type_var.get()
        # 在 [_on_slave_comm_type_change] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if mode == "RTU":
            self.tcp_frame.pack_forget()
            self.rtu_frame.pack(side="left")
        else:
            self.rtu_frame.pack_forget()
            self.tcp_frame.pack(side="left")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_switch_slave_instance]（入参: event）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _on_switch_slave_instance(self, event=None) -> None:
        """当用户在下拉框切换从机服务实例时，更新 `_current_device_id` 并重新渲染该从机的配置与点表。"""
        # 在 [_on_switch_slave_instance] 中更新局部变量 [idx]：为后续协议编码、服务调度或界面渲染准备中间状态
        idx = self.combo_instances.current()
        # 在 [_on_switch_slave_instance] 中更新局部变量 [devices]：为后续协议编码、服务调度或界面渲染准备中间状态
        devices = self.app.slave_service.get_devices()
        # 在 [_on_switch_slave_instance] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if 0 <= idx < len(devices):
            # 在 [_on_switch_slave_instance] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._current_device_id = devices[idx].id
            self._load_device_to_ui(self._current_device_id)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_refresh_tree]（入参: device）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _refresh_tree(self, device: Optional[SlaveDevice] = None) -> None:
        """清空并按 `(address, area)` 升序重新渲染当前从机的全部点位行到 `self.tree`。"""
        # 参数：
        # device: 可选直接传入的 `SlaveDevice` 实例；若未传入则按 `self._current_device_id` 查询。
        # 在 [_refresh_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 在 [_refresh_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not self._current_device_id:
                # 从 [_refresh_tree] 返回执行结果给调用方，结束当前处理分支
                return
            # 在 [_refresh_tree] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
            device = self.app.slave_service.get_device(self._current_device_id)
            # 在 [_refresh_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not device:
                # 从 [_refresh_tree] 返回执行结果给调用方，结束当前处理分支
                return

        # 在 [_refresh_tree] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for row in self.tree.get_children():
            self.tree.delete(row)

        # 在 [_refresh_tree] 中更新局部变量 [sorted_points]：为后续协议编码、服务调度或界面渲染准备中间状态
        sorted_points = sorted(device.points.values(), key=lambda p: (p.address, p.area.value))
        # 在 [_refresh_tree] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for p in sorted_points:
            self.tree.insert(
                "",
                "end",
                values=(
                    p.address,
                    p.description,
                    p.area.friendly_name,
                    p.data_type.value,
                    p.byte_order.value,
                    p.value,
                    p.raw_hex,
                    p.sim_rule,
                    p.register_count,
                ),
            )

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_start_runtime_sync_loop]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _start_runtime_sync_loop(self) -> None:
        """主线程定时循环（1s 周期）：将运行中从机的动态波形模拟数值（`val` 与 `raw_hex` 列）原位刷新到表格界面。"""
        # 设计说明（为什么使用 `self.tree.set` 局部更新单元格，而不是调用 `_refresh_tree` 全表重建）：
        # 全表删除再重新插入数百行点位不仅消耗大量 CPU，还会丢失用户当前在表格中的滚动条位置与选中高亮状态。
        # 因此本方法仅筛选 `sim_rule != "固定"` 的行，原位更新 `val` 和 `raw_hex` 两个单元格。
        # 在 [_start_runtime_sync_loop] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [_start_runtime_sync_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self._current_device_id and hasattr(self, "tree") and self.tree.winfo_exists():
                # 在 [_start_runtime_sync_loop] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
                device = self.app.slave_service.get_device(self._current_device_id)
                # 在 [_start_runtime_sync_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if device and device.is_running:
                    # 检查是否有点位开启了动态模拟 (固定点位跳过重绘)
                    # 在 [_start_runtime_sync_loop] 中更新局部变量 [has_sim]：为后续协议编码、服务调度或界面渲染准备中间状态
                    has_sim = any(p.sim_rule != "固定" for p in device.points.values())
                    # 在 [_start_runtime_sync_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if has_sim:
                        # 在 [_start_runtime_sync_loop] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
                        for row_id in self.tree.get_children():
                            # 在 [_start_runtime_sync_loop] 中更新局部变量 [vals]：为后续协议编码、服务调度或界面渲染准备中间状态
                            vals = self.tree.item(row_id, "values")
                            # 在 [_start_runtime_sync_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                            if vals and len(vals) >= 8:
                                # 在 [_start_runtime_sync_loop] 中更新局部变量 [addr]：为后续协议编码、服务调度或界面渲染准备中间状态
                                addr = int(vals[0])
                                # 在 [_start_runtime_sync_loop] 中更新局部变量 [area_name]：为后续协议编码、服务调度或界面渲染准备中间状态
                                area_name = vals[2]
                                # 在 [_start_runtime_sync_loop] 中更新局部变量 [sim_mode]：为后续协议编码、服务调度或界面渲染准备中间状态
                                sim_mode = vals[7]
                                # 在 [_start_runtime_sync_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                                if sim_mode != "固定":
                                    # 在 [_start_runtime_sync_loop] 中更新局部变量 [p]：为后续协议编码、服务调度或界面渲染准备中间状态
                                    p = device.get_point(area_name, addr)
                                    # 在 [_start_runtime_sync_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                                    if p:
                                        self.tree.set(row_id, "val", p.value)
                                        self.tree.set(row_id, "raw_hex", p.raw_hex)
        except Exception:
            pass
        finally:
            # 在 [_start_runtime_sync_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if hasattr(self, "winfo_exists") and self.winfo_exists():
                self.after(1000, self._start_runtime_sync_loop)

    # =========================================================================
    # 核心业务操作入口：完全遵循用户指令设计
    # 界面收集输入 -> 业务模块校验与转换 -> 引擎同步 -> 界面更新
    # =========================================================================

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_click_add_point]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _on_click_add_point(self) -> None:
        """
        响应“单点添加”按钮点击事件：收集输入参数、委托 `PointService` 校验并构建点位、写入从机模型与运行时引擎并刷新表格。
        """
        # 在 [_on_click_add_point] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self._current_device_id:
            messagebox.showwarning("提示", "请先选择或新建一个从机服务实例")
            # 从 [_on_click_add_point] 返回执行结果给调用方，结束当前处理分支
            return

        # 在 [_on_click_add_point] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
        device = self.app.slave_service.get_device(self._current_device_id)
        # 在 [_on_click_add_point] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 从 [_on_click_add_point] 返回执行结果给调用方，结束当前处理分支
            return

        # 1. 界面读取输入
        # 在 [_on_click_add_point] 中更新局部变量 [addr_str]：为后续协议编码、服务调度或界面渲染准备中间状态
        addr_str = self.entry_pt_addr.get()
        # 在 [_on_click_add_point] 中更新局部变量 [desc]：为后续协议编码、服务调度或界面渲染准备中间状态
        desc = self.entry_pt_desc.get()
        # 在 [_on_click_add_point] 中更新局部变量 [area_str]：为后续协议编码、服务调度或界面渲染准备中间状态
        area_str = self.combo_pt_area.get()
        # 在 [_on_click_add_point] 中更新局部变量 [dt_str]：为后续协议编码、服务调度或界面渲染准备中间状态
        dt_str = self.normalize_data_type_choice(self.combo_pt_type.get())
        # 在 [_on_click_add_point] 中更新局部变量 [bo_str]：为后续协议编码、服务调度或界面渲染准备中间状态
        bo_str = self.combo_pt_order.get()
        # 在 [_on_click_add_point] 中更新局部变量 [val_str]：为后续协议编码、服务调度或界面渲染准备中间状态
        val_str = self.entry_pt_val.get()
        # 在 [_on_click_add_point] 中更新局部变量 [sim_str]：为后续协议编码、服务调度或界面渲染准备中间状态
        sim_str = self.combo_pt_sim.get()

        # 2. 统一交给业务模块 PointService 校验与构建模型
        ok, point, err = PointService.validate_and_build_point(
            address=addr_str,
            description=desc,
            area=area_str,
            data_type=dt_str,
            byte_order=bo_str,
            val_input=val_str,
            sim_rule=sim_str,
        )
        # 在 [_on_click_add_point] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not ok or not point:
            messagebox.showerror("添加失败", f"参数错误: {err}")
            # 从 [_on_click_add_point] 返回执行结果给调用方，结束当前处理分支
            return

        # 3. 更新模型
        device.add_point(point)

        # 4. 若从机正在运行，同步给底层通信引擎
        self.app.slave_service.sync_point_to_runtime(device.id, point)

        # 5. 界面展示结果
        self._refresh_tree(device)
        self.app.logging_service.post("SYS", device.conn_config.unit_id, f"成功配置点位: 地址:{point.address} ({point.description}) 原始值:{point.raw_hex}")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_toggle_current_slave_server]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _toggle_current_slave_server(self) -> None:
        """
        响应“启动服务 / 停止服务”切换按钮：将界面当前通信参数写回 `device.conn_config` 并调用 `SlaveService` 启动或停止。
        """
        # 在 [_toggle_current_slave_server] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self._current_device_id:
            # 从 [_toggle_current_slave_server] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_toggle_current_slave_server] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
        device = self.app.slave_service.get_device(self._current_device_id)
        # 在 [_toggle_current_slave_server] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 从 [_toggle_current_slave_server] 返回执行结果给调用方，结束当前处理分支
            return

        # 同步界面当前输入的通信配置到模型
        # 在 [_toggle_current_slave_server] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [_toggle_current_slave_server] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.conn_config.comm_type = CommType.from_str(self.slave_comm_type_var.get())
            # 在 [_toggle_current_slave_server] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.conn_config.host = self.entry_slave_ip.get().strip() or "127.0.0.1"
            # 在 [_toggle_current_slave_server] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.conn_config.port = int(self.entry_slave_port.get().strip() or "502")
            # 在 [_toggle_current_slave_server] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.conn_config.com_port = self.combo_slave_com.get().strip() or "COM1"
            # 在 [_toggle_current_slave_server] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.conn_config.baudrate = int(self.combo_slave_baud.get().strip() or "9600")
            # 在 [_toggle_current_slave_server] 中更新局部变量 [parity_str]：为后续协议编码、服务调度或界面渲染准备中间状态
            parity_str = self.combo_slave_parity.get().strip()
            # 在 [_toggle_current_slave_server] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.conn_config.parity = "E" if "Even" in parity_str or "E" in parity_str else ("O" if "Odd" in parity_str or "O" in parity_str else "N")
            # 在 [_toggle_current_slave_server] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.conn_config.data_bits = int(self.combo_slave_databit.get().strip() or "8")
            # 在 [_toggle_current_slave_server] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.conn_config.stop_bits = int(self.combo_slave_stopbit.get().strip() or "1")
            # 在 [_toggle_current_slave_server] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            device.conn_config.unit_id = int(self.entry_slave_id.get().strip() or "1")
        except Exception as e:
            messagebox.showerror("配置错误", f"通信参数格式有误: {e}")
            # 从 [_toggle_current_slave_server] 返回执行结果给调用方，结束当前处理分支
            return

        # 界面把配置交给业务模块 SlaveService 调度
        # 在 [_toggle_current_slave_server] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device.is_running:
            ok, msg = self.app.slave_service.start_slave(device.id)
            # 在 [_toggle_current_slave_server] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not ok:
                messagebox.showerror("启动从机失败", msg)
        else:
            ok, msg = self.app.slave_service.stop_slave(device.id)

        # 界面仅据此更新状态
        self._update_status_indicator(device)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_start_all_slaves]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _start_all_slaves(self) -> None:
        """响应“全部启动”按钮：在后台守护线程中依次启动所有未运行的从机实例，完成后通过 `root.after(0, _on_done)` 切回主线程刷新 UI。"""
        # 关键设计（为什么需要 `self._busy["start"]` + 后台线程 + `root.after(0, ...)`）：
        # 1. 批量启动多台不同端口或串口的从机涉及底层线程拉起与端口绑定，若在主线程同步循环调用，会导致按钮卡住。
        # 2. `self._busy["start"]` 防止用户在后台启动尚未结束时连续多次点击按钮，避免并发重入产生竞态冲突。
        # 3. Tkinter 所有控件与 `messagebox` 必须在主线程调用，因此后台 `_worker` 结束时通过 `root.after(0, _on_done)` 安全投递回主线程。
        # 在 [_start_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._busy.get("start"):
            # 从 [_start_all_slaves] 返回执行结果给调用方，结束当前处理分支
            return
        self._busy["start"] = True
        # 在 [_start_all_slaves] 中更新局部变量 [devices]：为后续协议编码、服务调度或界面渲染准备中间状态
        devices = list(self.app.slave_service.get_devices())
        # 在 [_start_all_slaves] 中更新局部变量 [root]：为后续协议编码、服务调度或界面渲染准备中间状态
        root = self.winfo_toplevel()

        # ──────────────────────────────────────────────────────────────────
        # 方法实现 [_worker]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
        # ──────────────────────────────────────────────────────────────────
        def _worker() -> None:
            """后台批量启动工作函数：逐台启动未运行的从机并在 `finally` 中确保释放 `_busy["start"]` 守卫锁。"""
            fail_msgs: List[str] = []
            # 在 [_start_all_slaves] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                # 在 [_start_all_slaves] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
                for dev in devices:
                    # 在 [_start_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if not dev.is_running:
                        ok, msg = self.app.slave_service.start_slave(dev.id)
                        # 在 [_start_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                        if not ok:
                            fail_msgs.append(f"[{dev.name}]: {msg}")
            finally:
                self._busy["start"] = False

            # ──────────────────────────────────────────────────────────────────
            # 方法实现 [_on_done]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
            # ──────────────────────────────────────────────────────────────────
            def _on_done() -> None:
                """主线程完成回调：刷新当前从机运行状态指示灯，并在存在启动失败项时弹出汇总警告框。"""
                # 在 [_start_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if self._current_device_id:
                    # 在 [_start_all_slaves] 中更新局部变量 [dev]：为后续协议编码、服务调度或界面渲染准备中间状态
                    dev = self.app.slave_service.get_device(self._current_device_id)
                    # 在 [_start_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if dev:
                        self._update_status_indicator(dev)
                # 在 [_start_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if fail_msgs:
                    messagebox.showwarning("部分从机启动提示", "以下从机未能成功启动：\n" + "\n".join(fail_msgs[:5]))

            # 在 [_start_all_slaves] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                root.after(0, _on_done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_stop_all_slaves]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _stop_all_slaves(self) -> None:
        """
        响应“全部停止”按钮：在后台守护线程中依次停止所有运行中的从机实例，完成后切回主线程更新状态指示灯。
        """
        # 在 [_stop_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._busy.get("stop"):
            # 从 [_stop_all_slaves] 返回执行结果给调用方，结束当前处理分支
            return
        self._busy["stop"] = True
        # 在 [_stop_all_slaves] 中更新局部变量 [devices]：为后续协议编码、服务调度或界面渲染准备中间状态
        devices = list(self.app.slave_service.get_devices())
        # 在 [_stop_all_slaves] 中更新局部变量 [root]：为后续协议编码、服务调度或界面渲染准备中间状态
        root = self.winfo_toplevel()

        # ──────────────────────────────────────────────────────────────────
        # 方法实现 [_worker]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
        # ──────────────────────────────────────────────────────────────────
        def _worker() -> None:
            """后台批量停止工作函数：逐台停止运行中的从机并在 `finally` 中重置 `_busy["stop"]` 标志。"""
            # 在 [_stop_all_slaves] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                # 在 [_stop_all_slaves] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
                for dev in devices:
                    # 在 [_stop_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if dev.is_running:
                        self.app.slave_service.stop_slave(dev.id)
            finally:
                self._busy["stop"] = False

            # ──────────────────────────────────────────────────────────────────
            # 方法实现 [_on_done]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
            # ──────────────────────────────────────────────────────────────────
            def _on_done() -> None:
                """主线程完成回调：刷新当前选中从机的停止状态指示灯与按钮样式。"""
                # 在 [_stop_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if self._current_device_id:
                    # 在 [_stop_all_slaves] 中更新局部变量 [dev]：为后续协议编码、服务调度或界面渲染准备中间状态
                    dev = self.app.slave_service.get_device(self._current_device_id)
                    # 在 [_stop_all_slaves] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if dev:
                        self._update_status_indicator(dev)

            # 在 [_stop_all_slaves] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                root.after(0, _on_done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    # [SLAVE-RECONNECT] 自动重连按钮回调：触发底层僵死 listener 探活自愈重建
    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_auto_reconnect]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _on_auto_reconnect(self) -> None:
        """
        响应“自动重连”按钮：调用 `SlaveService.auto_reconnect_if_needed()` 扫描并重建僵死的底层 listener，并在状态栏反馈结果。
        """
        # 在 [_on_auto_reconnect] 中更新局部变量 [n]：为后续协议编码、服务调度或界面渲染准备中间状态
        n = self.app.slave_service.auto_reconnect_if_needed()
        self.lbl_slave_status.config(
            text=f"已重建 {n} 个僵死 listener" if n > 0 else "未检测到僵死 listener"
        )

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_add_new_slave_instance]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _add_new_slave_instance(self) -> None:
        """手动添加并配置新的 Modbus 从机服务实例（受 100 实例容量配额限制）"""
        ok, quota_err = self.app.slave_service.can_register_device(CommType.TCP)
        # 在 [_add_new_slave_instance] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not ok:
            messagebox.showerror("实例配额限制", quota_err)
            # 从 [_add_new_slave_instance] 返回执行结果给调用方，结束当前处理分支
            return

        # 在 [_add_new_slave_instance] 中更新局部变量 [devices]：为后续协议编码、服务调度或界面渲染准备中间状态
        devices = self.app.slave_service.get_devices()
        # 在 [_add_new_slave_instance] 中更新局部变量 [new_unit]：为后续协议编码、服务调度或界面渲染准备中间状态
        new_unit = len(devices) + 1
        # 在 [_add_new_slave_instance] 中更新局部变量 [name]：为后续协议编码、服务调度或界面渲染准备中间状态
        name = simpledialog.askstring("新建从机", "请输入从机服务名称:", initialvalue=f"从机{new_unit} [新建服务]")
        # 在 [_add_new_slave_instance] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not name:
            # 从 [_add_new_slave_instance] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_add_new_slave_instance] 中更新局部变量 [new_id]：为后续协议编码、服务调度或界面渲染准备中间状态
        new_id = f"slave_{time.time_ns()}"
        # 在 [_add_new_slave_instance] 中更新局部变量 [cfg]：为后续协议编码、服务调度或界面渲染准备中间状态
        cfg = ConnectionConfig(
            comm_type=CommType.TCP,
            host="127.0.0.1",
            port=502 + len(devices),
            com_port="COM1",
            baudrate=9600,
            unit_id=new_unit,
        )
        # 在 [_add_new_slave_instance] 中更新局部变量 [new_dev]：为后续协议编码、服务调度或界面渲染准备中间状态
        new_dev = SlaveDevice(id=new_id, name=name, conn_config=cfg)
        ok, msg = self.app.slave_service.register_device(new_dev)
        # 在 [_add_new_slave_instance] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not ok:
            messagebox.showerror("注册失败", msg)
            # 从 [_add_new_slave_instance] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_add_new_slave_instance] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._current_device_id = new_id
        self._update_instance_combobox()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_delete_current_slave_instance]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _delete_current_slave_instance(self) -> None:
        """删除当前选中的从机服务实例（至少保留 1 个实例防止工作台完全悬空）。"""
        # 在 [_delete_current_slave_instance] 中更新局部变量 [devices]：为后续协议编码、服务调度或界面渲染准备中间状态
        devices = self.app.slave_service.get_devices()
        # 在 [_delete_current_slave_instance] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if len(devices) <= 1:
            messagebox.showwarning("提示", "至少保留一个从机服务实例！")
            # 从 [_delete_current_slave_instance] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_delete_current_slave_instance] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self._current_device_id:
            # 从 [_delete_current_slave_instance] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_delete_current_slave_instance] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
        device = self.app.slave_service.get_device(self._current_device_id)
        # 在 [_delete_current_slave_instance] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 从 [_delete_current_slave_instance] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_delete_current_slave_instance] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if messagebox.askyesno("确认删除", f"确定要删除从机实例 [{device.name}] 吗？"):
            self.app.slave_service.remove_device(device.id)
            # 在 [_delete_current_slave_instance] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._current_device_id = None
            self._update_instance_combobox()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_select_all_points]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _select_all_points(self) -> None:
        """全选当前点表 Treeview 中的所有点位行。"""
        # 在 [_select_all_points] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for item in self.tree.get_children():
            self.tree.selection_add(item)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_deselect_all_points]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _deselect_all_points(self) -> None:
        """取消当前点表 Treeview 中所有点位行的选中状态。"""
        # 在 [_deselect_all_points] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for item in self.tree.get_children():
            self.tree.selection_remove(item)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_batch_delete_selected]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _batch_delete_selected(self) -> None:
        """从当前从机实例中批量删除用户在点表 Treeview 中选中的点位行。"""
        # 在 [_batch_delete_selected] 中更新局部变量 [selected]：为后续协议编码、服务调度或界面渲染准备中间状态
        selected = self.tree.selection()
        # 在 [_batch_delete_selected] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not selected or not self._current_device_id:
            messagebox.showinfo("提示", "请先在点表中选中要删除的点位行")
            # 从 [_batch_delete_selected] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_batch_delete_selected] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
        device = self.app.slave_service.get_device(self._current_device_id)
        # 在 [_batch_delete_selected] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 从 [_batch_delete_selected] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_batch_delete_selected] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for item in selected:
            # 在 [_batch_delete_selected] 中更新局部变量 [vals]：为后续协议编码、服务调度或界面渲染准备中间状态
            vals = self.tree.item(item, "values")
            # 在 [_batch_delete_selected] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if vals:
                # 在 [_batch_delete_selected] 中更新局部变量 [addr]：为后续协议编码、服务调度或界面渲染准备中间状态
                addr = int(vals[0])
                # 在 [_batch_delete_selected] 中更新局部变量 [area]：为后续协议编码、服务调度或界面渲染准备中间状态
                area = vals[2]
                device.remove_point(area, addr)
        self._refresh_tree(device)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_apply_batch_sim]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _apply_batch_sim(self) -> None:
        """将下拉框选中的动态模拟规则批量应用到目标范围内的点位。"""
        # 在 [_apply_batch_sim] 中更新局部变量 [sim]：为后续协议编码、服务调度或界面渲染准备中间状态
        sim = self.combo_batch_sim.get()
        self._apply_batch_point_property("sim_rule", sim)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_apply_batch_order]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _apply_batch_order(self) -> None:
        """将下拉框选中的字节序（变位模式）批量应用到目标范围内的点位并重算原始寄存器值。"""
        # 在 [_apply_batch_order] 中更新局部变量 [order]：为后续协议编码、服务调度或界面渲染准备中间状态
        order = self.combo_batch_order.get()
        self._apply_batch_point_property("byte_order", order)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_apply_batch_type]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _apply_batch_type(self) -> None:
        """将下拉框选中的数据类型批量应用到目标范围内的点位并重新校验编码。"""
        # 在 [_apply_batch_type] 中更新局部变量 [dt]：为后续协议编码、服务调度或界面渲染准备中间状态
        dt = self.normalize_data_type_choice(self.combo_batch_type.get())
        self._apply_batch_point_property("data_type", dt)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_apply_batch_value]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _apply_batch_value(self) -> None:
        """将输入框中的工程数值批量赋给目标范围内的点位并同步到底层运行引擎。"""
        # 在 [_apply_batch_value] 中更新局部变量 [val]：为后续协议编码、服务调度或界面渲染准备中间状态
        val = self.entry_batch_val.get()
        self._apply_batch_point_property("value", val)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_apply_batch_point_property]（入参: prop_name, prop_val）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _apply_batch_point_property(self, prop_name: str, prop_val: Any) -> None:
        """通用批量属性修改器：支持“仅当前从机（若选中行则仅限选中行）”或“全部从机服务”两级作用范围。"""
        # 参数：
        # prop_name: 待修改的属性名（`"sim_rule"` / `"byte_order"` / `"data_type"` / `"value"`）。
        # prop_val: 目标属性设定值。
        # 在 [_apply_batch_point_property] 中更新局部变量 [scope]：为后续协议编码、服务调度或界面渲染准备中间状态
        scope = self.batch_scope_var.get()
        # 在 [_apply_batch_point_property] 中更新局部变量 [target_devices]：为后续协议编码、服务调度或界面渲染准备中间状态
        target_devices = (
            self.app.slave_service.get_devices()
            if scope == "all"
            else ([self.app.slave_service.get_device(self._current_device_id)] if self._current_device_id else [])
        )

        # 在 [_apply_batch_point_property] 中更新局部变量 [selected_items]：为后续协议编码、服务调度或界面渲染准备中间状态
        selected_items = self.tree.selection()
        # 在 [_apply_batch_point_property] 中更新局部变量 [selected_addrs]：为后续协议编码、服务调度或界面渲染准备中间状态
        selected_addrs = set()
        # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if selected_items and scope == "current":
            # 在 [_apply_batch_point_property] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for itm in selected_items:
                # 在 [_apply_batch_point_property] 中更新局部变量 [vals]：为后续协议编码、服务调度或界面渲染准备中间状态
                vals = self.tree.item(itm, "values")
                # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if vals:
                    selected_addrs.add((vals[2], int(vals[0])))

        # 在 [_apply_batch_point_property] 中更新局部变量 [count]：为后续协议编码、服务调度或界面渲染准备中间状态
        count = 0
        # 在 [_apply_batch_point_property] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for dev in target_devices:
            # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not dev:
                continue
            # 在 [_apply_batch_point_property] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for point in dev.points.values():
                # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if selected_addrs and (point.area.friendly_name, point.address) not in selected_addrs:
                    continue

                # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if prop_name == "sim_rule":
                    # 在 [_apply_batch_point_property] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                    point.sim_rule = SimRule.normalize(prop_val)
                # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                elif prop_name == "byte_order":
                    # 在 [_apply_batch_point_property] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                    point.byte_order = ByteOrder.normalize(prop_val)
                    PointService.re_encode_point(point)
                # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                elif prop_name == "data_type":
                    ok, new_pt, _ = PointService.validate_and_build_point(
                        point.address, point.description, point.area, prop_val, point.byte_order, point.value, point.sim_rule
                    )
                    # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if ok and new_pt:
                        # 在 [_apply_batch_point_property] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                        point.data_type = new_pt.data_type
                        # 在 [_apply_batch_point_property] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                        point.value = new_pt.value
                        # 在 [_apply_batch_point_property] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                        point.raw_hex = new_pt.raw_hex
                    else:
                        continue
                # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                elif prop_name == "value":
                    ok, new_pt, _ = PointService.validate_and_build_point(
                        point.address, point.description, point.area, point.data_type, point.byte_order, prop_val, point.sim_rule
                    )
                    # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if ok and new_pt:
                        # 在 [_apply_batch_point_property] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                        point.value = new_pt.value
                        # 在 [_apply_batch_point_property] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
                        point.raw_hex = new_pt.raw_hex

                self.app.slave_service.sync_point_to_runtime(dev.id, point)
                count += 1

        # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if self._current_device_id:
            # 在 [_apply_batch_point_property] 中更新局部变量 [dev]：为后续协议编码、服务调度或界面渲染准备中间状态
            dev = self.app.slave_service.get_device(self._current_device_id)
            # 在 [_apply_batch_point_property] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if dev:
                self._refresh_tree(dev)
        messagebox.showinfo("批量操作完成", f"已成功更新 {count} 个点位属性")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_tree_double_click]（入参: event）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _on_tree_double_click(self, event=None) -> None:
        """
        响应点表 Treeview 双击事件：弹出输入框允许用户即时修改双击行的工程数值，校验通过后同步至底层寄存器。
        """
        # 在 [_on_tree_double_click] 中更新局部变量 [item]：为后续协议编码、服务调度或界面渲染准备中间状态
        item = self.tree.identify_row(event.y)
        # 在 [_on_tree_double_click] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not item or not self._current_device_id:
            # 从 [_on_tree_double_click] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_on_tree_double_click] 中更新局部变量 [vals]：为后续协议编码、服务调度或界面渲染准备中间状态
        vals = self.tree.item(item, "values")
        # 在 [_on_tree_double_click] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not vals:
            # 从 [_on_tree_double_click] 返回执行结果给调用方，结束当前处理分支
            return

        addr, desc, area, dt, bo, cur_val = int(vals[0]), vals[1], vals[2], vals[3], vals[4], vals[5]
        # 在 [_on_tree_double_click] 中更新局部变量 [new_val]：为后续协议编码、服务调度或界面渲染准备中间状态
        new_val = simpledialog.askstring("修改点位数值", f"修改点位 [地址:{addr} ({desc})]\n当前值: {cur_val}\n请输入新数值:", initialvalue=str(cur_val))
        # 在 [_on_tree_double_click] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if new_val is None:
            # 从 [_on_tree_double_click] 返回执行结果给调用方，结束当前处理分支
            return

        # 在 [_on_tree_double_click] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
        device = self.app.slave_service.get_device(self._current_device_id)
        # 在 [_on_tree_double_click] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not device:
            # 从 [_on_tree_double_click] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_on_tree_double_click] 中更新局部变量 [point]：为后续协议编码、服务调度或界面渲染准备中间状态
        point = device.get_point(area, addr)
        # 在 [_on_tree_double_click] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not point:
            # 从 [_on_tree_double_click] 返回执行结果给调用方，结束当前处理分支
            return

        ok, updated_pt, err = PointService.validate_and_build_point(
            addr, desc, point.area, point.data_type, point.byte_order, new_val, point.sim_rule
        )
        # 在 [_on_tree_double_click] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not ok or not updated_pt:
            messagebox.showerror("修改失败", err)
            # 从 [_on_tree_double_click] 返回执行结果给调用方，结束当前处理分支
            return

        # 在 [_on_tree_double_click] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        point.value = updated_pt.value
        # 在 [_on_tree_double_click] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        point.raw_hex = updated_pt.raw_hex
        self.app.slave_service.sync_point_to_runtime(device.id, point)
        self._refresh_tree(device)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_sort_by_column]（入参: col, reverse）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _sort_by_column(self, col: str, reverse: bool = False) -> None:
        """点击表头自动正序/倒序排列"""
        # 在 [_sort_by_column] 中更新局部变量 [items]：为后续协议编码、服务调度或界面渲染准备中间状态
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        # 在 [_sort_by_column] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        if col == "area":
            items.sort(key=lambda t: AreaType.normalize(t[0]).value, reverse=reverse)
        else:
            try:
                items.sort(key=lambda t: float(t[0]), reverse=reverse)
            except (ValueError, TypeError):
                items.sort(key=lambda t: t[0], reverse=reverse)

        # 在 [_sort_by_column] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for index, (_, k) in enumerate(items):
            self.tree.move(k, "", index)

        self.tree.heading(col, command=lambda: self._sort_by_column(col, not reverse))

    # =========================================================================
    # 外部对话框桥接 (批量规则生成、数据库导入与业务配对)
    # =========================================================================

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_open_batch_generate_dialog]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _open_batch_generate_dialog(self) -> None:
        """弹出批量规则点表生成对话框"""
        # 在 [_open_batch_generate_dialog] 中更新局部变量 [dlg]：为后续协议编码、服务调度或界面渲染准备中间状态
        dlg = tk.Toplevel(self)
        dlg.title("批量输入规则生成点表")
        dlg.geometry("450x380")
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="批量点表规则生成向导", font=("Microsoft YaHei UI", 10, "bold")).pack(pady=8)
        # 在 [_open_batch_generate_dialog] 中更新局部变量 [f]：为后续协议编码、服务调度或界面渲染准备中间状态
        f = ttk.Frame(dlg)
        f.pack(fill="x", padx=16, pady=4)

        ttk.Label(f, text="起始地址:").grid(row=0, column=0, sticky="w", pady=4)
        # 在 [_open_batch_generate_dialog] 中更新局部变量 [e_addr]：为后续协议编码、服务调度或界面渲染准备中间状态
        e_addr = ttk.Entry(f, width=12)
        e_addr.insert(0, "1")
        e_addr.grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(f, text="点位数量:").grid(row=1, column=0, sticky="w", pady=4)
        # 在 [_open_batch_generate_dialog] 中更新局部变量 [e_cnt]：为后续协议编码、服务调度或界面渲染准备中间状态
        e_cnt = ttk.Entry(f, width=12)
        e_cnt.insert(0, "10")
        e_cnt.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(f, text="存储区域:").grid(row=2, column=0, sticky="w", pady=4)
        # 在 [_open_batch_generate_dialog] 中更新局部变量 [c_area]：为后续协议编码、服务调度或界面渲染准备中间状态
        c_area = ttk.Combobox(f, state="readonly", width=16, values=self.AREA_CHOICES)
        c_area.set(AreaType.HOLDING.friendly_name)
        c_area.grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(f, text="数据类型:").grid(row=3, column=0, sticky="w", pady=4)
        # 在 [_open_batch_generate_dialog] 中更新局部变量 [c_type]：为后续协议编码、服务调度或界面渲染准备中间状态
        c_type = ttk.Combobox(f, state="readonly", width=12, values=self.DATA_TYPE_CHOICES)
        c_type.set("FLOAT32")
        c_type.grid(row=3, column=1, sticky="w", pady=4)

        ttk.Label(f, text="变位模式:").grid(row=4, column=0, sticky="w", pady=4)
        # 在 [_open_batch_generate_dialog] 中更新局部变量 [c_order]：为后续协议编码、服务调度或界面渲染准备中间状态
        c_order = ttk.Combobox(f, state="readonly", width=10, values=self.BYTE_ORDER_CHOICES)
        c_order.set("ABCD")
        c_order.grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(f, text="模拟规则:").grid(row=5, column=0, sticky="w", pady=4)
        # 在 [_open_batch_generate_dialog] 中更新局部变量 [c_sim]：为后续协议编码、服务调度或界面渲染准备中间状态
        c_sim = ttk.Combobox(f, state="readonly", width=10, values=self.SIM_RULE_CHOICES)
        c_sim.set("随机波动")
        c_sim.grid(row=5, column=1, sticky="w", pady=4)

        # ──────────────────────────────────────────────────────────────────
        # 方法实现 [do_generate]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
        # ──────────────────────────────────────────────────────────────────
        def do_generate():
            """根据向导输入的起始地址、数量与数据类型步长，批量构建点位并同步到当前从机。"""
            # 在 [_open_batch_generate_dialog] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not self._current_device_id:
                # 从 [_open_batch_generate_dialog] 返回执行结果给调用方，结束当前处理分支
                return
            # 在 [_open_batch_generate_dialog] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
            device = self.app.slave_service.get_device(self._current_device_id)
            # 在 [_open_batch_generate_dialog] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not device:
                # 从 [_open_batch_generate_dialog] 返回执行结果给调用方，结束当前处理分支
                return
            # 在 [_open_batch_generate_dialog] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                # 在 [_open_batch_generate_dialog] 中更新局部变量 [start_a]：为后续协议编码、服务调度或界面渲染准备中间状态
                start_a = int(e_addr.get().strip())
                # 在 [_open_batch_generate_dialog] 中更新局部变量 [count]：为后续协议编码、服务调度或界面渲染准备中间状态
                count = int(e_cnt.get().strip())
                # 在 [_open_batch_generate_dialog] 中更新局部变量 [area]：为后续协议编码、服务调度或界面渲染准备中间状态
                area = c_area.get()
                # 在 [_open_batch_generate_dialog] 中更新局部变量 [dt]：为后续协议编码、服务调度或界面渲染准备中间状态
                dt = self.normalize_data_type_choice(c_type.get())
                # 在 [_open_batch_generate_dialog] 中更新局部变量 [bo]：为后续协议编码、服务调度或界面渲染准备中间状态
                bo = c_order.get()
                # 在 [_open_batch_generate_dialog] 中更新局部变量 [sim]：为后续协议编码、服务调度或界面渲染准备中间状态
                sim = c_sim.get()
            except Exception as ex:
                messagebox.showerror("错误", f"输入格式有误: {ex}", parent=dlg)
                # 从 [_open_batch_generate_dialog] 返回执行结果给调用方，结束当前处理分支
                return

            # 在 [_open_batch_generate_dialog] 中更新局部变量 [dt_enum]：为后续协议编码、服务调度或界面渲染准备中间状态
            dt_enum = DataType.normalize(dt)
            # 在 [_open_batch_generate_dialog] 中更新局部变量 [step]：为后续协议编码、服务调度或界面渲染准备中间状态
            step = dt_enum.register_count
            # 在 [_open_batch_generate_dialog] 中更新局部变量 [cur_a]：为后续协议编码、服务调度或界面渲染准备中间状态
            cur_a = start_a
            # 在 [_open_batch_generate_dialog] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for i in range(count):
                # 在 [_open_batch_generate_dialog] 中更新局部变量 [desc]：为后续协议编码、服务调度或界面渲染准备中间状态
                desc = f"AutoPoint_{cur_a}"
                ok, pt, _ = PointService.validate_and_build_point(cur_a, desc, area, dt, bo, i + 1, sim)
                # 在 [_open_batch_generate_dialog] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if ok and pt:
                    device.add_point(pt)
                    self.app.slave_service.sync_point_to_runtime(device.id, pt)
                cur_a += step

            self._refresh_tree(device)
            dlg.destroy()
            messagebox.showinfo("生成成功", f"成功批量生成 {count} 个点位")

        # 在 [_open_batch_generate_dialog] 中更新局部变量 [btn_box]：为后续协议编码、服务调度或界面渲染准备中间状态
        btn_box = ttk.Frame(dlg)
        btn_box.pack(fill="x", padx=16, pady=12)
        ttk.Button(btn_box, text="立即生成", command=do_generate).pack(side="right", padx=4)
        ttk.Button(btn_box, text="取消", command=dlg.destroy).pack(side="right", padx=4)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_select_database_file]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _select_database_file(self) -> None:
        """关联或切换业务数据库文件，并自动停止清空所有历史从机实例"""
        # 在 [_select_database_file] 中更新局部变量 [db_path]：为后续协议编码、服务调度或界面渲染准备中间状态
        db_path = filedialog.askopenfilename(
            title="选择业务数据库文件 (切换将自动清空当前从机实例)",
            filetypes=[("HEMS 数据库", "*.cdb;*.sqlite;*.db"), ("所有文件", "*.*")],
            initialdir=os.path.abspath("extra"),
        )
        # 在 [_select_database_file] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if db_path and os.path.exists(db_path):
            # 在 [_select_database_file] 中更新局部变量 [old_count]：为后续协议编码、服务调度或界面渲染准备中间状态
            old_count = len(self.app.slave_service.get_devices())
            # 在 [_select_database_file] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self.app.db_path = db_path

            # 切换数据库时：停止并清空当前已添加的所有从机实例与点表
            self._clear_all_slaves_and_reset_ui()
            self.app.logging_service.post(
                "SYS",
                0,
                f"已成功切换业务数据库: {db_path}，已自动停止并清空原有的 {old_count} 个从机服务实例",
            )
            messagebox.showinfo(
                "数据库切换成功",
                f"当前数据库已更新为：\n{db_path}\n\n已成功停止并清空原有的 {old_count} 个从机服务实例与点表！\n请点击【导入设备点表】载入新数据库中的设备与点位。",
            )

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_open_import_db_dialog]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _open_import_db_dialog(self) -> None:
        """打开从业务数据库导入设备模型与点表弹窗 (支持多选批量与容量配额限制)"""
        from .dialogs import DbImportDialog

        # 在 [_open_import_db_dialog] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self.app.db_path or not os.path.exists(self.app.db_path):
            self._select_database_file()
            # 在 [_open_import_db_dialog] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not self.app.db_path or not os.path.exists(self.app.db_path):
                # 从 [_open_import_db_dialog] 返回执行结果给调用方，结束当前处理分支
                return

        # ──────────────────────────────────────────────────────────────────
        # 方法实现 [on_imported]（入参: target_dev）：封装该操作的输入校验、状态转换与异常兜底逻辑
        # ──────────────────────────────────────────────────────────────────
        def on_imported(target_dev: SlaveDevice) -> None:
            """当数据库导入对话框完成设备与点表导入后，自动切换到新导入的从机实例并刷新界面。"""
            # 在 [_open_import_db_dialog] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self._current_device_id = target_dev.id
            self._update_instance_combobox()
            self._load_device_to_ui(target_dev.id)
            self._refresh_tree(target_dev)

        DbImportDialog(
            parent=self,
            app=self.app,
            on_imported_callback=on_imported,
            current_device_id=self._current_device_id,
        )

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_open_pairing_dialog]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _open_pairing_dialog(self) -> None:
        """打开业务配对关系中心对话框"""
        from .dialogs import PairingDialog

        # 在 [_open_pairing_dialog] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self.app.db_path or not os.path.exists(self.app.db_path):
            self._select_database_file()
            # 在 [_open_pairing_dialog] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not self.app.db_path or not os.path.exists(self.app.db_path):
                # 从 [_open_pairing_dialog] 返回执行结果给调用方，结束当前处理分支
                return

        PairingDialog(parent=self, app=self.app)

