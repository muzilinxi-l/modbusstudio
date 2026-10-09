"""
Modbus Studio - 主机轮询工作台面板 (Poll Panel)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
纯视图组件：负责收集主机查询请求输入，调用 PollService 进行通信，
并展示寄存器读取结果与实时统计。
"""

from __future__ import annotations
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import TYPE_CHECKING, Optional

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
    PollResult,
    PollTask,
)
from ..services import PollService


class PollPanel(ttk.Frame):
    """主机轮询调试与实时监视工作台"""

    AREA_CHOICES = [
        AreaType.HOLDING.friendly_name,
        AreaType.INPUT.friendly_name,
        AreaType.COIL.friendly_name,
        AreaType.DISCRETE.friendly_name,
    ]

    BYTE_ORDER_CHOICES = ["ABCD", "CDAB", "BADC", "DCBA"]

    def __init__(self, parent: ttk.Notebook, app: ModbusStudioAppCore):
        super().__init__(parent)
        self.app = app
        self._is_polling_loop = False
        self._poll_timer_id = None
        self._tx_count = 0
        self._rx_count = 0

        self._build_ui()
        self._refresh_poll_com_ports()

    def _build_ui(self) -> None:
        self.pack(fill="both", expand=True, padx=4, pady=4)

        # 1. 顶部控制栏 (两行紧凑布局)
        top_box = ttk.LabelFrame(self, text="主机连接与读取指令配置")
        top_box.pack(fill="x", padx=2, pady=2)

        # --- 第 0 行：目标通信参数与连接 ---
        row0 = ttk.Frame(top_box)
        row0.pack(fill="x", padx=4, pady=2)

        left0 = ttk.Frame(row0)
        left0.pack(side="left", fill="x", expand=True)

        ttk.Label(left0, text="通讯方式:").pack(side="left", padx=(2, 2))
        self.poll_comm_type_var = tk.StringVar(value="TCP")
        rb_tcp = ttk.Radiobutton(left0, text="以太网 (TCP)", variable=self.poll_comm_type_var, value="TCP", command=self._on_comm_type_change)
        rb_tcp.pack(side="left", padx=2)
        rb_rtu = ttk.Radiobutton(left0, text="串口 (RTU / 485 / 232)", variable=self.poll_comm_type_var, value="RTU", command=self._on_comm_type_change)
        rb_rtu.pack(side="left", padx=2)

        self.conn_container = ttk.Frame(left0)
        self.conn_container.pack(side="left", padx=4)

        # TCP 控件
        self.tcp_frame = ttk.Frame(self.conn_container)
        ttk.Label(self.tcp_frame, text="目标IP:").pack(side="left", padx=1)
        self.entry_ip = ttk.Entry(self.tcp_frame, width=12)
        self.entry_ip.insert(0, "127.0.0.1")
        self.entry_ip.pack(side="left", padx=1)
        ttk.Label(self.tcp_frame, text="端口:").pack(side="left", padx=1)
        self.entry_port = ttk.Entry(self.tcp_frame, width=6)
        self.entry_port.insert(0, "502")
        self.entry_port.pack(side="left", padx=1)

        # RTU 控件
        self.rtu_frame = ttk.Frame(self.conn_container)
        ttk.Label(self.rtu_frame, text="串口:").pack(side="left", padx=1)
        self.combo_com = ttk.Combobox(self.rtu_frame, width=8)
        self.combo_com.pack(side="left", padx=1)
        btn_refresh = ttk.Button(self.rtu_frame, text="🔄", width=3, style="Small.TButton", command=self._refresh_poll_com_ports)
        btn_refresh.pack(side="left", padx=1)
        ttk.Label(self.rtu_frame, text="波特率:").pack(side="left", padx=1)
        self.combo_baud = ttk.Combobox(self.rtu_frame, width=7, values=["9600", "19200", "38400", "57600", "115200"])
        self.combo_baud.set("9600")
        self.combo_baud.pack(side="left", padx=1)

        ttk.Label(left0, text="从机 ID:").pack(side="left", padx=(4, 1))
        self.entry_unit_id = ttk.Entry(left0, width=4)
        self.entry_unit_id.insert(0, "1")
        self.entry_unit_id.pack(side="left", padx=1)

        right0 = ttk.Frame(row0)
        right0.pack(side="right")

        self.btn_connect = ttk.Button(right0, text="🔌 建立连接", style="primary.TButton", command=self._toggle_connect)
        self.btn_connect.pack(side="left", padx=3)

        self.lbl_conn_status = ttk.Label(right0, text="未连接", foreground="#6c757d", font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_conn_status.pack(side="left", padx=4)

        # 默认显示 TCP
        self._on_comm_type_change()

        # --- 第 1 行：读取指令与操作 ---
        row1 = ttk.Frame(top_box)
        row1.pack(fill="x", padx=4, pady=2)

        left1 = ttk.Frame(row1)
        left1.pack(side="left", fill="x", expand=True)

        ttk.Label(left1, text="区域:").pack(side="left", padx=(2, 1))
        self.combo_area = ttk.Combobox(left1, state="readonly", width=12, values=self.AREA_CHOICES)
        self.combo_area.set(AreaType.HOLDING.friendly_name)
        self.combo_area.pack(side="left", padx=1)

        ttk.Label(left1, text="起始地址:").pack(side="left", padx=(4, 1))
        self.entry_start_addr = ttk.Entry(left1, width=5)
        self.entry_start_addr.insert(0, "1")
        self.entry_start_addr.pack(side="left", padx=1)

        ttk.Label(left1, text="数量:").pack(side="left", padx=(4, 1))
        self.entry_count = ttk.Entry(left1, width=4)
        self.entry_count.insert(0, "10")
        self.entry_count.pack(side="left", padx=1)

        ttk.Label(left1, text="变位:").pack(side="left", padx=(4, 1))
        self.combo_order = ttk.Combobox(left1, state="readonly", width=6, values=self.BYTE_ORDER_CHOICES)
        self.combo_order.set("ABCD")
        self.combo_order.pack(side="left", padx=1)

        btn_poll_once = ttk.Button(left1, text="🔍 单次读取", style="info.TButton", command=self._poll_once)
        btn_poll_once.pack(side="left", padx=(6, 2))

        self.btn_poll_loop = ttk.Button(left1, text="🔁 连续轮询 (1s)", style="success.TButton", command=self._toggle_poll_loop)
        self.btn_poll_loop.pack(side="left", padx=2)

        right1 = ttk.Frame(row1)
        right1.pack(side="right")

        self.lbl_stats = ttk.Label(right1, text="Tx: 0 | Rx: 0", font=("Consolas", 9))
        self.lbl_stats.pack(side="right", padx=4)

        # 2. 轮询监视数据表格
        table_box = ttk.Frame(self)
        table_box.pack(fill="both", expand=True, padx=2, pady=2)

        self.tree_scroll_y = ttk.Scrollbar(table_box, orient="vertical")
        self.tree_scroll_y.pack(side="right", fill="y")
        self.tree_scroll_x = ttk.Scrollbar(table_box, orient="horizontal")
        self.tree_scroll_x.pack(side="bottom", fill="x")

        self.columns = ("addr", "raw_hex", "u16", "i16", "f32_abcd", "f32_cdab", "status")
        self.tree = ttk.Treeview(
            table_box,
            columns=self.columns,
            show="headings",
            selectmode="browse",
            yscrollcommand=self.tree_scroll_y.set,
            xscrollcommand=self.tree_scroll_x.set,
        )
        self.tree_scroll_y.config(command=self.tree.yview)
        self.tree_scroll_x.config(command=self.tree.xview)
        self.tree.pack(side="left", fill="both", expand=True)

        col_defs = [
            ("addr", "寄存器地址", 80, "center"),
            ("raw_hex", "原始值 (Hex)", 110, "center"),
            ("u16", "UINT16 解算", 100, "center"),
            ("i16", "INT16 解算", 100, "center"),
            ("f32_abcd", "FLOAT32 (ABCD)", 120, "center"),
            ("f32_cdab", "FLOAT32 (CDAB)", 120, "center"),
            ("status", "状态响应", 100, "center"),
        ]
        for cid, head, w, align in col_defs:
            self.tree.heading(cid, text=head)
            self.tree.column(cid, width=w, anchor=align)

    def _on_comm_type_change(self) -> None:
        mode = self.poll_comm_type_var.get()
        if mode == "RTU":
            self.tcp_frame.pack_forget()
            self.rtu_frame.pack(side="left")
        else:
            self.rtu_frame.pack_forget()
            self.tcp_frame.pack(side="left")

    def _refresh_poll_com_ports(self) -> None:
        ports = []
        if serial:
            try:
                for p in serial.tools.list_ports.comports():
                    ports.append(p.device)
            except Exception:
                pass
        if not ports:
            ports = ["COM1", "COM2", "COM3", "COM4"]
        self.combo_com["values"] = ports
        if not self.combo_com.get() and ports:
            self.combo_com.set(ports[0])

    def _get_connection_config(self) -> ConnectionConfig:
        return ConnectionConfig(
            comm_type=CommType.from_str(self.poll_comm_type_var.get()),
            host=self.entry_ip.get().strip() or "127.0.0.1",
            port=int(self.entry_port.get().strip() or "502"),
            com_port=self.combo_com.get().strip() or "COM1",
            baudrate=int(self.combo_baud.get().strip() or "9600"),
            unit_id=int(self.entry_unit_id.get().strip() or "1"),
        )

    def _toggle_connect(self) -> None:
        if self.app.poll_service.is_connected:
            self.app.poll_service.disconnect()
            self.lbl_conn_status.config(text="● 已断开", foreground="#dc3545")
            self.btn_connect.config(text="🔌 建立连接", style="primary.TButton")
        else:
            cfg = self._get_connection_config()
            ok, msg = self.app.poll_service.connect(cfg)
            if ok:
                self.lbl_conn_status.config(text="● 已连接", foreground="#28a745")
                self.btn_connect.config(text="🔌 断开连接", style="danger.TButton")
            else:
                messagebox.showerror("连接失败", msg)

    def _poll_once(self) -> None:
        """执行单次轮询"""
        cfg = self._get_connection_config()
        try:
            start_addr = int(self.entry_start_addr.get().strip())
            count = int(self.entry_count.get().strip())
            area_enum = AreaType.normalize(self.combo_area.get())
            bo_enum = ByteOrder.normalize(self.combo_order.get())
        except Exception as e:
            messagebox.showerror("参数错误", f"输入格式有误: {e}")
            return

        task = PollTask(
            conn_config=cfg,
            area=area_enum,
            start_address=start_addr,
            count=count,
            byte_order=bo_enum,
        )

        self._tx_count += 1
        res: PollResult = self.app.poll_service.read_registers(task)
        if res.success:
            self._rx_count += 1
            self._update_result_tree(res)
        else:
            self.app.logging_service.post("ERROR", cfg.unit_id, f"读取失败: {res.error_msg}")

        self.lbl_stats.config(text=f"Tx: {self._tx_count} | Rx: {self._rx_count}")

    def _toggle_poll_loop(self) -> None:
        if self._is_polling_loop:
            self._is_polling_loop = False
            if self._poll_timer_id:
                self.after_cancel(self._poll_timer_id)
                self._poll_timer_id = None
            self.btn_poll_loop.config(text="🔁 连续轮询 (1s)", style="success.TButton")
        else:
            self._is_polling_loop = True
            self.btn_poll_loop.config(text="⏹ 停止轮询", style="danger.TButton")
            self._run_poll_cycle()

    def _run_poll_cycle(self) -> None:
        if not self._is_polling_loop:
            return
        self._poll_once()
        self._poll_timer_id = self.after(1000, self._run_poll_cycle)

    def _update_result_tree(self, res: PollResult) -> None:
        from ..modbus_codec import decode_value

        for itm in self.tree.get_children():
            self.tree.delete(itm)

        regs = res.raw_registers
        start = res.start_address
        for i, val in enumerate(regs):
            addr = start + i
            hex_str = f"0x{val:04X}"
            u16 = val
            i16 = val - 65536 if val > 32767 else val

            # 计算连续双字的 FLOAT32 (ABCD 和 CDAB)
            f32_abcd = "-"
            f32_cdab = "-"
            if i + 1 < len(regs):
                pair = [regs[i], regs[i + 1]]
                try:
                    f32_abcd = f"{decode_value(pair, 'FLOAT32', 'ABCD'):.4f}"
                    f32_cdab = f"{decode_value(pair, 'FLOAT32', 'CDAB'):.4f}"
                except Exception:
                    pass

            self.tree.insert("", "end", values=(addr, hex_str, u16, i16, f32_abcd, f32_cdab, "OK (200)"))
