"""
Modbus Studio - 从机模拟工作台面板 (Slave Panel)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
纯视图组件：仅负责收集用户输入与渲染结果，所有参数校验、模型构建、
生命周期管理均委托给业务服务层 (PointService / SlaveService)。
彻底解决高 DPI 下按钮截断、文字溢出与操作状态不一致问题。
"""

from __future__ import annotations
import logging
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import traceback
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("SlavePanel")

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
    """从机仿真配置与监控工作台"""

    AREA_CHOICES = [
        AreaType.HOLDING.friendly_name,
        AreaType.INPUT.friendly_name,
        AreaType.COIL.friendly_name,
        AreaType.DISCRETE.friendly_name,
    ]

    DATA_TYPE_CHOICES = ["INT16", "UINT16", "INT32", "UINT32", "FLOAT32", "FLOAT64", "BOOL", "STRING", "HEX16"]
    BYTE_ORDER_CHOICES = ["ABCD", "CDAB", "BADC", "DCBA"]
    SIM_RULE_CHOICES = ["固定", "随机波动", "步进递增", "正弦波", "方波"]

    def __init__(self, parent: ttk.Notebook, app: ModbusStudioAppCore):
        super().__init__(parent)
        self.app = app
        self._current_device_id: Optional[str] = None

        self._build_ui()
        self._load_initial_data()

    # =========================================================================
    # UI 布局构建 (采用两行配置栏与紧凑自适应排版，杜绝高 DPI 截断)
    # =========================================================================

    def _build_ui(self) -> None:
        self.pack(fill="both", expand=True, padx=4, pady=4)

        # 1. 顶部控制栏 (分两行布局)
        top_box = ttk.LabelFrame(self, text="从机服务实例与通信配置 (支持多实例独立监听不同 IP:Port 或 COM 串口)")
        top_box.pack(fill="x", padx=2, pady=2)

        # --- 第 0 行：实例切换与启动调度 ---
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

        right0 = ttk.Frame(row0)
        right0.pack(side="right")

        self.btn_toggle_slave = ttk.Button(right0, text="▶️ 启动服务", style="success.TButton", command=self._toggle_current_slave_server)
        self.btn_toggle_slave.pack(side="left", padx=3)

        self.lbl_slave_status = ttk.Label(right0, text="未运行", foreground="#6c757d", font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_slave_status.pack(side="left", padx=4)

        btn_start_all = ttk.Button(right0, text="⚡ 全部启动", style="success.Outline.TButton", command=self._start_all_slaves)
        btn_start_all.pack(side="left", padx=2)

        btn_stop_all = ttk.Button(right0, text="⏹ 全部停止", style="danger.Outline.TButton", command=self._stop_all_slaves)
        btn_stop_all.pack(side="left", padx=2)

        # --- 第 1 行：通信模式与数据库操作 ---
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
        self.combo_slave_baud = ttk.Combobox(self.rtu_frame, width=7, values=["9600", "19200", "38400", "57600", "115200"])
        self.combo_slave_baud.pack(side="left", padx=1)

        ttk.Label(self.rtu_frame, text="校验:").pack(side="left", padx=(3, 1))
        self.combo_slave_parity = ttk.Combobox(self.rtu_frame, width=6, state="readonly", values=["None (N)", "Even (E)", "Odd (O)"])
        self.combo_slave_parity.set("None (N)")
        self.combo_slave_parity.pack(side="left", padx=1)

        ttk.Label(self.rtu_frame, text="数据位:").pack(side="left", padx=(3, 1))
        self.combo_slave_databit = ttk.Combobox(self.rtu_frame, width=3, state="readonly", values=["8", "7"])
        self.combo_slave_databit.set("8")
        self.combo_slave_databit.pack(side="left", padx=1)

        ttk.Label(self.rtu_frame, text="停止位:").pack(side="left", padx=(3, 1))
        self.combo_slave_stopbit = ttk.Combobox(self.rtu_frame, width=3, state="readonly", values=["1", "2"])
        self.combo_slave_stopbit.set("1")
        self.combo_slave_stopbit.pack(side="left", padx=1)

        ttk.Label(left1, text="从机 ID:").pack(side="left", padx=(4, 1))
        self.entry_slave_id = ttk.Entry(left1, width=4)
        self.entry_slave_id.pack(side="left", padx=1)

        right1 = ttk.Frame(row1)
        right1.pack(side="right")

        btn_select_db = ttk.Button(right1, text="🗃️ 关联/切换数据库...", style="secondary.Outline.TButton", command=self._select_database_file)
        btn_select_db.pack(side="left", padx=2)

        btn_import_db = ttk.Button(right1, text="📂 导入设备点表...", style="info.TButton", command=self._open_import_db_dialog)
        btn_import_db.pack(side="left", padx=2)

        btn_pair_center = ttk.Button(right1, text="🔗 业务配对中心 (基于 More)...", style="primary.Outline.TButton", command=self._open_pairing_dialog)
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
        """动态感知容器宽度变化，等比例自适应分配每一列宽 (防抖平滑优化，杜绝拖动丢帧卡顿)"""
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
        """执行实际主点表列宽分配"""
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
        self._refresh_slave_com_ports()
        self._update_instance_combobox()

    def _refresh_slave_com_ports(self) -> None:
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

    def _update_instance_combobox(self) -> None:
        devices = self.app.slave_service.get_devices()
        device_names = [f"从机{dev.conn_config.unit_id} [{dev.name}]" for dev in devices]
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
            self.combo_instances.set("（暂无从机实例，请点击【导入设备点表】或【新建服务】）")
            for item in self.tree.get_children():
                self.tree.delete(item)
            self.lbl_slave_status.config(text="● 未就绪 (无活动从机)", foreground="#6c757d")
            self.btn_toggle_slave.config(text="▶️ 启动服务", style="secondary.TButton")

    def _clear_all_slaves_and_reset_ui(self) -> None:
        """清空所有从机服务实例并重置主工作台 UI 界面"""
        self.app.slave_service.clear_all_devices()
        self._current_device_id = None
        self._update_instance_combobox()

    def _load_device_to_ui(self, device_id: str) -> None:
        device = self.app.slave_service.get_device(device_id)
        if not device:
            return
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
        if device.is_running:
            self.lbl_slave_status.config(text=f"● 运行中 ({device.conn_config.summary})", foreground="#28a745")
            self.btn_toggle_slave.config(text="⏹ 停止服务", style="danger.TButton")
        else:
            self.lbl_slave_status.config(text="● 已停止", foreground="#dc3545")
            self.btn_toggle_slave.config(text="▶️ 启动服务", style="success.TButton")

    def _on_slave_comm_type_change(self) -> None:
        mode = self.slave_comm_type_var.get()
        if mode == "RTU":
            self.tcp_frame.pack_forget()
            self.rtu_frame.pack(side="left")
        else:
            self.rtu_frame.pack_forget()
            self.tcp_frame.pack(side="left")

    def _on_switch_slave_instance(self, event=None) -> None:
        idx = self.combo_instances.current()
        devices = self.app.slave_service.get_devices()
        if 0 <= idx < len(devices):
            self._current_device_id = devices[idx].id
            self._load_device_to_ui(self._current_device_id)

    def _refresh_tree(self, device: Optional[SlaveDevice] = None) -> None:
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

    # =========================================================================
    # 核心业务操作入口：完全遵循用户指令设计
    # 界面收集输入 -> 业务模块校验与转换 -> 引擎同步 -> 界面更新
    # =========================================================================

    def _on_click_add_point(self) -> None:
        """用户点击添加单点"""
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
        dt_str = self.combo_pt_type.get()
        bo_str = self.combo_pt_order.get()
        val_str = self.entry_pt_val.get()
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

    def _toggle_current_slave_server(self) -> None:
        """用户点击“启动/停止服务”"""
        if not self._current_device_id:
            return
        device = self.app.slave_service.get_device(self._current_device_id)
        if not device:
            return

        # 同步界面当前输入的通信配置到模型
        try:
            device.conn_config.comm_type = CommType.from_str(self.slave_comm_type_var.get())
            device.conn_config.host = self.entry_slave_ip.get().strip() or "127.0.0.1"
            device.conn_config.port = int(self.entry_slave_port.get().strip() or "502")
            device.conn_config.com_port = self.combo_slave_com.get().strip() or "COM1"
            device.conn_config.baudrate = int(self.combo_slave_baud.get().strip() or "9600")
            parity_str = self.combo_slave_parity.get().strip()
            device.conn_config.parity = "E" if "Even" in parity_str or "E" in parity_str else ("O" if "Odd" in parity_str or "O" in parity_str else "N")
            device.conn_config.data_bits = int(self.combo_slave_databit.get().strip() or "8")
            device.conn_config.stop_bits = int(self.combo_slave_stopbit.get().strip() or "1")
            device.conn_config.unit_id = int(self.entry_slave_id.get().strip() or "1")
        except Exception as e:
            messagebox.showerror("配置错误", f"通信参数格式有误: {e}")
            return

        # 界面把配置交给业务模块 SlaveService 调度
        if not device.is_running:
            ok, msg = self.app.slave_service.start_slave(device.id)
            if not ok:
                messagebox.showerror("启动从机失败", msg)
        else:
            ok, msg = self.app.slave_service.stop_slave(device.id)

        # 界面仅据此更新状态
        self._update_status_indicator(device)

    def _start_all_slaves(self) -> None:
        devices = self.app.slave_service.get_devices()
        for dev in devices:
            if not dev.is_running:
                self.app.slave_service.start_slave(dev.id)
        if self._current_device_id:
            dev = self.app.slave_service.get_device(self._current_device_id)
            if dev:
                self._update_status_indicator(dev)

    def _stop_all_slaves(self) -> None:
        devices = self.app.slave_service.get_devices()
        for dev in devices:
            if dev.is_running:
                self.app.slave_service.stop_slave(dev.id)
        if self._current_device_id:
            dev = self.app.slave_service.get_device(self._current_device_id)
            if dev:
                self._update_status_indicator(dev)

    def _add_new_slave_instance(self) -> None:
        """手动添加并配置新的 Modbus 从机服务实例（受 100 实例容量配额限制）"""
        ok, quota_err = self.app.slave_service.can_register_device(CommType.TCP)
        if not ok:
            messagebox.showerror("实例配额限制", quota_err)
            return

        devices = self.app.slave_service.get_devices()
        new_unit = len(devices) + 1
        name = simpledialog.askstring("新建从机", "请输入从机服务名称:", initialvalue=f"从机{new_unit} [新建服务]")
        if not name:
            return
        new_id = f"slave_{time.time_ns()}"
        cfg = ConnectionConfig(
            comm_type=CommType.TCP,
            host="127.0.0.1",
            port=502 + len(devices),
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
        self._update_instance_combobox()

    def _delete_current_slave_instance(self) -> None:
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

    def _select_all_points(self) -> None:
        for item in self.tree.get_children():
            self.tree.selection_add(item)

    def _deselect_all_points(self) -> None:
        for item in self.tree.get_children():
            self.tree.selection_remove(item)

    def _batch_delete_selected(self) -> None:
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
        self._refresh_tree(device)

    def _apply_batch_sim(self) -> None:
        sim = self.combo_batch_sim.get()
        self._apply_batch_point_property("sim_rule", sim)

    def _apply_batch_order(self) -> None:
        order = self.combo_batch_order.get()
        self._apply_batch_point_property("byte_order", order)

    def _apply_batch_type(self) -> None:
        dt = self.combo_batch_type.get()
        self._apply_batch_point_property("data_type", dt)

    def _apply_batch_value(self) -> None:
        val = self.entry_batch_val.get()
        self._apply_batch_point_property("value", val)

    def _apply_batch_point_property(self, prop_name: str, prop_val: Any) -> None:
        scope = self.batch_scope_var.get()
        target_devices = (
            self.app.slave_service.get_devices()
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
        for dev in target_devices:
            if not dev:
                continue
            for point in dev.points.values():
                if selected_addrs and (point.area.friendly_name, point.address) not in selected_addrs:
                    continue

                if prop_name == "sim_rule":
                    point.sim_rule = SimRule.normalize(prop_val)
                elif prop_name == "byte_order":
                    point.byte_order = ByteOrder.normalize(prop_val)
                    PointService.re_encode_point(point)
                elif prop_name == "data_type":
                    point.data_type = DataType.normalize(prop_val)
                    PointService.re_encode_point(point)
                elif prop_name == "value":
                    ok, new_pt, _ = PointService.validate_and_build_point(
                        point.address, point.description, point.area, point.data_type, point.byte_order, prop_val, point.sim_rule
                    )
                    if ok and new_pt:
                        point.value = new_pt.value
                        point.raw_hex = new_pt.raw_hex

                self.app.slave_service.sync_point_to_runtime(dev.id, point)
                count += 1

        if self._current_device_id:
            dev = self.app.slave_service.get_device(self._current_device_id)
            if dev:
                self._refresh_tree(dev)
        messagebox.showinfo("批量操作完成", f"已成功更新 {count} 个点位属性")

    def _on_tree_double_click(self, event=None) -> None:
        item = self.tree.identify_row(event.y)
        if not item or not self._current_device_id:
            return
        vals = self.tree.item(item, "values")
        if not vals:
            return

        addr, desc, area, dt, bo, cur_val = int(vals[0]), vals[1], vals[2], vals[3], vals[4], vals[5]
        new_val = simpledialog.askstring("修改点位数值", f"修改点位 [地址:{addr} ({desc})]\n当前值: {cur_val}\n请输入新数值:", initialvalue=str(cur_val))
        if new_val is None:
            return

        device = self.app.slave_service.get_device(self._current_device_id)
        if not device:
            return
        point = device.get_point(area, addr)
        if not point:
            return

        ok, updated_pt, err = PointService.validate_and_build_point(
            addr, desc, point.area, point.data_type, point.byte_order, new_val, point.sim_rule
        )
        if not ok or not updated_pt:
            messagebox.showerror("修改失败", err)
            return

        point.value = updated_pt.value
        point.raw_hex = updated_pt.raw_hex
        self.app.slave_service.sync_point_to_runtime(device.id, point)
        self._refresh_tree(device)

    def _sort_by_column(self, col: str, reverse: bool = False) -> None:
        """点击表头自动正序/倒序排列"""
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
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
        dlg = tk.Toplevel(self)
        dlg.title("批量输入规则生成点表")
        dlg.geometry("450x380")
        dlg.transient(self)
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

        def do_generate():
            if not self._current_device_id:
                return
            device = self.app.slave_service.get_device(self._current_device_id)
            if not device:
                return
            try:
                start_a = int(e_addr.get().strip())
                count = int(e_cnt.get().strip())
                area = c_area.get()
                dt = c_type.get()
                bo = c_order.get()
                sim = c_sim.get()
            except Exception as ex:
                messagebox.showerror("错误", f"输入格式有误: {ex}", parent=dlg)
                return

            dt_enum = DataType.normalize(dt)
            step = dt_enum.register_count
            cur_a = start_a
            for i in range(count):
                desc = f"AutoPoint_{cur_a}"
                ok, pt, _ = PointService.validate_and_build_point(cur_a, desc, area, dt, bo, i + 1, sim)
                if ok and pt:
                    device.add_point(pt)
                    self.app.slave_service.sync_point_to_runtime(device.id, pt)
                cur_a += step

            self._refresh_tree(device)
            dlg.destroy()
            messagebox.showinfo("生成成功", f"成功批量生成 {count} 个点位")

        btn_box = ttk.Frame(dlg)
        btn_box.pack(fill="x", padx=16, pady=12)
        ttk.Button(btn_box, text="立即生成", command=do_generate).pack(side="right", padx=4)
        ttk.Button(btn_box, text="取消", command=dlg.destroy).pack(side="right", padx=4)

    def _select_database_file(self) -> None:
        """关联或切换业务数据库文件，并自动停止清空所有历史从机实例"""
        db_path = filedialog.askopenfilename(
            title="选择业务数据库文件 (切换将自动清空当前从机实例)",
            filetypes=[("HEMS 数据库", "*.cdb;*.sqlite;*.db"), ("所有文件", "*.*")],
            initialdir=os.path.abspath("extra"),
        )
        if db_path and os.path.exists(db_path):
            old_count = len(self.app.slave_service.get_devices())
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

    def _open_import_db_dialog(self) -> None:
        """打开从业务数据库导入设备模型与点表弹窗 (支持多选批量与容量配额限制)"""
        from .dialogs import DbImportDialog

        if not self.app.db_path or not os.path.exists(self.app.db_path):
            self._select_database_file()
            if not self.app.db_path or not os.path.exists(self.app.db_path):
                return

        def on_imported(target_dev: SlaveDevice) -> None:
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

    def _open_pairing_dialog(self) -> None:
        """打开业务配对关系中心对话框"""
        from .dialogs import PairingDialog

        if not self.app.db_path or not os.path.exists(self.app.db_path):
            self._select_database_file()
            if not self.app.db_path or not os.path.exists(self.app.db_path):
                return

        PairingDialog(parent=self, app=self.app)

