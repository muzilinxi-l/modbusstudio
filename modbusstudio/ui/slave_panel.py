"""
Modbus Studio - 从机模拟工作台面板 (Slave Panel)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
纯视图组件：仅负责收集用户输入与渲染结果，所有参数校验、模型构建、
生命周期管理均委托给业务服务层 (PointService / SlaveService)。
彻底解决高 DPI 下按钮截断、文字溢出与操作状态不一致问题。
"""

from __future__ import annotations
import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

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

        btn_new_inst = ttk.Button(left0, text="➕ 新建服务", style="Small.TButton", command=self._add_new_slave_instance)
        btn_new_inst.pack(side="left", padx=2)

        btn_del_inst = ttk.Button(left0, text="🗑️ 删除服务", style="Small.TButton", command=self._delete_current_slave_instance)
        btn_del_inst.pack(side="left", padx=2)

        right0 = ttk.Frame(row0)
        right0.pack(side="right")

        self.btn_toggle_slave = ttk.Button(right0, text="▶️ 启动服务", command=self._toggle_current_slave_server)
        self.btn_toggle_slave.pack(side="left", padx=3)

        self.lbl_slave_status = ttk.Label(right0, text="未运行", foreground="#6c757d", font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_slave_status.pack(side="left", padx=4)

        btn_start_all = ttk.Button(right0, text="⚡ 全部启动", style="Small.TButton", command=self._start_all_slaves)
        btn_start_all.pack(side="left", padx=2)

        btn_stop_all = ttk.Button(right0, text="⏹ 全部停止", style="Small.TButton", command=self._stop_all_slaves)
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

        # RTU 控件
        self.rtu_frame = ttk.Frame(self.slave_conn_container)
        ttk.Label(self.rtu_frame, text="串口:").pack(side="left", padx=1)
        self.combo_slave_com = ttk.Combobox(self.rtu_frame, width=8)
        self.combo_slave_com.pack(side="left", padx=1)
        btn_com_refresh = ttk.Button(self.rtu_frame, text="🔄", width=3, style="Small.TButton", command=self._refresh_slave_com_ports)
        btn_com_refresh.pack(side="left", padx=1)
        ttk.Label(self.rtu_frame, text="波特率:").pack(side="left", padx=1)
        self.combo_slave_baud = ttk.Combobox(self.rtu_frame, width=7, values=["9600", "19200", "38400", "57600", "115200"])
        self.combo_slave_baud.pack(side="left", padx=1)

        ttk.Label(left1, text="从机 ID:").pack(side="left", padx=(4, 1))
        self.entry_slave_id = ttk.Entry(left1, width=4)
        self.entry_slave_id.pack(side="left", padx=1)

        right1 = ttk.Frame(row1)
        right1.pack(side="right")

        btn_select_db = ttk.Button(right1, text="🗃️ 关联/切换数据库...", style="Small.TButton", command=self._select_database_file)
        btn_select_db.pack(side="left", padx=2)

        btn_import_db = ttk.Button(right1, text="📂 导入设备点表...", style="Small.TButton", command=self._open_import_db_dialog)
        btn_import_db.pack(side="left", padx=2)

        btn_pair_center = ttk.Button(right1, text="🔗 业务配对中心 (基于 More)...", style="Small.TButton", command=self._open_pairing_dialog)
        btn_pair_center.pack(side="left", padx=2)

        # 2. 单点配置与快速生成栏
        pt_box = ttk.LabelFrame(self, text="点位规则生成 & 快捷操作")
        pt_box.pack(fill="x", padx=2, pady=2)

        row_pt = ttk.Frame(pt_box)
        row_pt.pack(fill="x", padx=2, pady=2)

        btn_batch_gen = ttk.Button(row_pt, text="⚡ 批量输入规则生成点表...", style="Small.TButton", command=self._open_batch_generate_dialog)
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

        btn_add_pt = ttk.Button(row_pt, text="➕ 单点添加", style="Small.TButton", command=self._on_click_add_point)
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

        btn_apply_sim = ttk.Button(b_row0, text="⚡ 批量修改规则", style="Small.TButton", command=self._apply_batch_sim)
        btn_apply_sim.pack(side="left", padx=3)

        btn_sel_all = ttk.Button(b_row0, text="☑ 全选点位", style="Small.TButton", command=self._select_all_points)
        btn_sel_all.pack(side="left", padx=3)

        btn_desel_all = ttk.Button(b_row0, text="☐ 取消选择", style="Small.TButton", command=self._deselect_all_points)
        btn_desel_all.pack(side="left", padx=3)

        btn_del_sel = ttk.Button(b_row0, text="🗑️ 批量删除选中", style="Small.TButton", command=self._batch_delete_selected)
        btn_del_sel.pack(side="left", padx=3)

        # 批量操作第 2 行：变位、类型、数值
        b_row1 = ttk.Frame(batch_box)
        b_row1.pack(fill="x", padx=2, pady=1)

        ttk.Label(b_row1, text="目标变位:").pack(side="left", padx=(2, 1))
        self.combo_batch_order = ttk.Combobox(b_row1, state="readonly", width=6, values=self.BYTE_ORDER_CHOICES)
        self.combo_batch_order.set("ABCD")
        self.combo_batch_order.pack(side="left", padx=1)
        btn_apply_order = ttk.Button(b_row1, text="批量修改变位", style="Small.TButton", command=self._apply_batch_order)
        btn_apply_order.pack(side="left", padx=2)

        ttk.Label(b_row1, text="目标类型:").pack(side="left", padx=(6, 1))
        self.combo_batch_type = ttk.Combobox(b_row1, state="readonly", width=8, values=self.DATA_TYPE_CHOICES)
        self.combo_batch_type.set("FLOAT32")
        self.combo_batch_type.pack(side="left", padx=1)
        btn_apply_type = ttk.Button(b_row1, text="批量修改类型", style="Small.TButton", command=self._apply_batch_type)
        btn_apply_type.pack(side="left", padx=2)

        ttk.Label(b_row1, text="统一设值:").pack(side="left", padx=(6, 1))
        self.entry_batch_val = ttk.Entry(b_row1, width=7)
        self.entry_batch_val.insert(0, "0")
        self.entry_batch_val.pack(side="left", padx=1)
        btn_apply_val = ttk.Button(b_row1, text="批量修改数值", style="Small.TButton", command=self._apply_batch_value)
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

        col_configs = [
            ("addr", "起始地址", 70, "center"),
            ("desc", "点位描述 / 业务字段", 180, "w"),
            ("area", "存储区域", 110, "center"),
            ("type", "数据类型", 80, "center"),
            ("order", "变位模式", 70, "center"),
            ("val", "当前解析数值", 100, "center"),
            ("raw_hex", "原始寄存器(Hex)", 140, "center"),
            ("sim", "动态模拟", 80, "center"),
            ("reg_cnt", "占用字数", 60, "center"),
        ]
        for col_id, heading, width, align in col_configs:
            self.tree.heading(col_id, text=heading)
            self.tree.column(col_id, width=width, anchor=align)

        self.tree.bind("<Double-1>", self._on_tree_double_click)

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
        self.entry_slave_id.delete(0, "end")
        self.entry_slave_id.insert(0, str(cfg.unit_id))

        self._update_status_indicator(device)
        self._refresh_tree(device)

    def _update_status_indicator(self, device: SlaveDevice) -> None:
        if device.is_running:
            self.lbl_slave_status.config(text=f"● 运行中 ({device.conn_config.summary})", foreground="#28a745")
            self.btn_toggle_slave.config(text="⏹ 停止服务")
        else:
            self.lbl_slave_status.config(text="● 已停止", foreground="#dc3545")
            self.btn_toggle_slave.config(text="▶️ 启动服务")

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

        sorted_points = sorted(device.points.values(), key=lambda p: (p.area.value, p.address))
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
        self.app.slave_service.register_device(new_dev)
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
        db_path = filedialog.askopenfilename(
            title="选择 HEMS 数据库文件",
            filetypes=[("HEMS 数据库", "*.cdb;*.sqlite;*.db"), ("所有文件", "*.*")],
            initialdir=os.path.abspath("extra"),
        )
        if db_path and os.path.exists(db_path):
            self.app.db_path = db_path
            self.app.logging_service.post("SYS", 0, f"已成功关联业务数据库: {db_path}")
            messagebox.showinfo("数据库关联成功", f"当前数据库：\n{db_path}")

    def _open_import_db_dialog(self) -> None:
        from ..hems_db_loader import HemsDatabase
        if not self.app.db_path or not os.path.exists(self.app.db_path):
            self._select_database_file()
            if not self.app.db_path or not os.path.exists(self.app.db_path):
                return

        try:
            db = HemsDatabase(self.app.db_path)
            apps = db.load_modbus_apps()
        except Exception as e:
            messagebox.showerror("读取数据库失败", f"无法加载数据库: {e}")
            return

        if not apps:
            messagebox.showinfo("提示", "该数据库中未查询到任何 Modbus 业务模块")
            return

        dlg = tk.Toplevel(self)
        dlg.title("从业务数据库导入设备点表")
        dlg.geometry("560x420")
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="请选择要导入的业务设备模型 (app 表):", font=("Microsoft YaHei UI", 10, "bold")).pack(pady=6)
        dev_listbox = tk.Listbox(dlg, height=12)
        dev_listbox.pack(fill="both", expand=True, padx=12, pady=4)

        for a in apps:
            tag = "南向" if a.is_south_master else "北向"
            dev_listbox.insert("end", f"[{a.app_id}] [{tag}] {a.chinese_name or a.english_name} ({a.english_name})")
        dev_listbox.select_set(0)

        def do_import():
            sel_idx = dev_listbox.curselection()
            if not sel_idx or not self._current_device_id:
                return
            sel_app = apps[sel_idx[0]]
            device = self.app.slave_service.get_device(self._current_device_id)
            if not device:
                return

            points_data = sel_app.extract_studio_points()
            imported = 0
            for pt in points_data:
                addr = pt.get("address", 0)
                desc = pt.get("desc", "")
                dt_str = pt.get("data_type").value if hasattr(pt.get("data_type"), "value") else str(pt.get("data_type"))
                bo_str = pt.get("byte_order").value if hasattr(pt.get("byte_order"), "value") else str(pt.get("byte_order"))
                area_str = pt.get("area", AreaType.HOLDING.value)
                val = pt.get("current_val", 0)
                sim = pt.get("sim_mode", "固定")

                ok, point, _ = PointService.validate_and_build_point(
                    addr, desc, area_str, dt_str, bo_str, val, sim
                )
                if ok and point:
                    device.add_point(point)
                    self.app.slave_service.sync_point_to_runtime(device.id, point)
                    imported += 1

            self._refresh_tree(device)
            dlg.destroy()
            messagebox.showinfo("导入完成", f"已成功将设备 [{sel_app.chinese_name or sel_app.english_name}] 的 {imported} 个点位导入至当前从机！")

        btn_f = ttk.Frame(dlg)
        btn_f.pack(fill="x", padx=12, pady=8)
        ttk.Button(btn_f, text="确定导入", command=do_import).pack(side="right", padx=4)
        ttk.Button(btn_f, text="取消", command=dlg.destroy).pack(side="right", padx=4)


    def _open_pairing_dialog(self) -> None:
        from ..hems_pairing import HemsPairingEngine
        if not self.app.db_path or not os.path.exists(self.app.db_path):
            self._select_database_file()
            if not self.app.db_path or not os.path.exists(self.app.db_path):
                return

        dlg = tk.Toplevel(self)
        dlg.title("业务配对关系中心 (基于 app.More)")
        dlg.geometry("700x480")
        dlg.transient(self)

        ttk.Label(dlg, text="业务配对关系中心 (基于 app 表 More 字段)", font=("Microsoft YaHei UI", 10, "bold")).pack(pady=6)
        tree_pair = ttk.Treeview(
            dlg,
            columns=("rule_id", "rule_type", "src", "target", "desc"),
            show="headings",
        )
        tree_pair.heading("rule_id", text="规则ID")
        tree_pair.heading("rule_type", text="业务类型")
        tree_pair.heading("src", text="源变量 / 地址")
        tree_pair.heading("target", text="目标变量 / 地址")
        tree_pair.heading("desc", text="配对说明")
        tree_pair.pack(fill="both", expand=True, padx=8, pady=4)

        try:
            engine = HemsPairingEngine(self.app.db_path)
            rules = engine.load_rules()
            for r in rules:
                tree_pair.insert("", "end", values=(r.rule_id, r.rule_type, r.source_var, r.target_var, r.description))
        except Exception as e:
            messagebox.showerror("加载配对规则失败", f"{e}", parent=dlg)

        ttk.Button(dlg, text="关闭", command=dlg.destroy).pack(pady=6)
