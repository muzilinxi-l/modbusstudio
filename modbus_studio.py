"""Modbus Studio - 工业级 Modbus Slave 模拟器与 Poll 调试助手 (业务数据库深度集成版).

功能特性：
1. 双模式切换：Modbus Slave (从机模拟器) + Modbus Poll (主机轮询调试器)
2. 深度集成 extra/hems.cdb 业务数据库 (app 配置层)：
   - 自动扫描解析 app 表中的所有南向采集设备 (BMS, PCS, 电表, 外部设备) 与北向服务 ([North]30)
   - 一键将业务设备点表、轮询规则、数据类型与地址映射导入到 Slave 或 Poll 中
   - 支持将软件中调校好的点表与变位配置反向写回 app 数据库
3. 工业变位模式 (字节序与字序): ABCD, CDAB, BADC, DCBA
4. 强大批量功能：
   - 批量规则生成器：输入规则自动生成点表
   - 多选批量修改变位：支持 Ctrl/Shift/全选，一键批量切换目标变位
   - 批量修改类型、模拟规则与数值
   - 表格右键菜单快捷批量操作
"""

import csv
import ctypes
import json
import logging
import math
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageTk

from hems_db_loader import HemsAppModel, HemsDatabase
from hems_pairing import HemsPairingEngine, PairingRule
from modbus_codec import (
    ByteOrderMode,
    ModbusDataType,
    TYPE_REGISTER_COUNT,
    clamp_value_to_type,
    decode_value,
    encode_value,
    transform_bytes,
)
from modbus_engine import AreaType, ModbusPollEngine, ModbusSlaveEngine

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ModbusStudio")


def get_resource_path(relative_path: str) -> str:
    """获取程序运行时的静态资源绝对路径 (兼容 PyInstaller 冻结环境)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, relative_path)


def get_available_com_ports() -> List[str]:
    """获取当前系统所有物理和虚拟 COM 端口列表 (包含 USB 转串口与 com0com/MOXA 虚拟串口)."""
    try:
        import serial.tools.list_ports
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if not ports:
            return [f"COM{i}" for i in range(1, 9)]
        return ports
    except Exception:
        return [f"COM{i}" for i in range(1, 9)]


AVAILABLE_BAUDRATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
AVAILABLE_PARITIES = ["N (无校验)", "O (奇校验)", "E (偶校验)"]
SERIAL_BUS_NAMES = [
    "RS-485(A1/B1)",
    "RS-485(A2/B2)",
    "RS-485(A3/B3)",
    "RS-485(A4/B4)",
    "RS-485(A5/B5)",
    "RS-485(A6/B6)",
    "RS-485(A7/B7)",
    "RS-232",
]


class SlaveServiceInstance:
    """代表一个独立的 Modbus Slave 从机服务实例 (支持以太网 TCP 与 串行端口 RTU 485/232 独立监听)."""

    def __init__(
        self,
        name: str,
        host: str = "0.0.0.0",
        port: int = 5020,
        slave_id: int = 1,
        comm_type: str = "TCP",
        serial_port: str = "COM1",
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        serial_bus_label: str = "RS-485(A1/B1)",
    ):
        self.name = name
        self.comm_type = comm_type.upper()  # "TCP" 或 "RTU"
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.serial_bus_label = serial_bus_label

        self.engine = ModbusSlaveEngine(
            comm_type=self.comm_type,
            host=host,
            port=port,
            serial_port=serial_port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            slave_id=slave_id,
        )


class ModbusStudioApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Modbus Studio - 业务配置与变位仿真调试工作站 (支持 extra/hems.cdb)")
        self.root.geometry("1180x800")
        self.root.minsize(980, 620)

        # 窗口图标与任务栏图标双重绑定 (彻底规避 Windows 任务栏退化为默认羽毛图标)
        icon_path = get_resource_path("app.ico")
        if os.path.exists(icon_path):
            try:
                self.root.iconbitmap(icon_path)
            except Exception as e:
                logger.debug(f"设置窗口 iconbitmap 失败: {e}")
            try:
                img = Image.open(icon_path)
                self._app_icon_photo = ImageTk.PhotoImage(img)
                self.root.iconphoto(True, self._app_icon_photo)
            except Exception as e:
                logger.debug(f"设置窗口 iconphoto 失败: {e}")

        # 多从机服务实例管理 (支持监听不同 IP 和端口号)
        self.slave_instances: Dict[str, SlaveServiceInstance] = {}
        # 通信与系统日志结构化存储列表 (用于 CSV 导出)
        self.log_records: List[Dict[str, str]] = []

        # 实时自动日志落盘 (按日期滚动写入 logs 目录，带缓冲区 flush 保证断电/崩溃不丢数据)
        self.log_dir = "logs"
        os.makedirs(self.log_dir, exist_ok=True)
        self.runtime_csv_file = os.path.join(
            self.log_dir, f"modbus_runtime_{time.strftime('%Y%m%d')}.csv"
        )
        self._csv_lock = threading.Lock()
        self._init_runtime_csv()

        default_inst = SlaveServiceInstance("默认从机服务 (5020)", "0.0.0.0", 5020, 1)
        self._register_slave_instance(default_inst)
        self.current_slave_name: str = default_inst.name

        self.poll_engine: ModbusPollEngine = ModbusPollEngine()
        # 绑定 Poll 引擎的报文日志回调
        self.poll_engine.on_packet_log = lambda msg, level: self.root.after(0, lambda: self.log(msg, level))

        self.hems_db = HemsDatabase(os.path.join("extra", "hems.cdb"))
        self.pairing_engine = HemsPairingEngine(self.hems_db.db_path)

        self._init_style()
        self._build_ui()
        self._load_default_slave_points()

        # 定时刷新 UI 定时器
        self.root.after(500, self._ui_heartbeat)

    def _register_slave_instance(self, inst: SlaveServiceInstance):
        """向实例管理器注册从机服务，并统一绑定报文实时日志回调."""
        inst.engine.on_packet_log = lambda msg, level: self.root.after(0, lambda: self.log(msg, level))
        self.slave_instances[inst.name] = inst

    @property
    def current_slave_inst(self) -> SlaveServiceInstance:
        """获取当前选中的从机服务实例."""
        if self.current_slave_name not in self.slave_instances:
            self.current_slave_name = next(iter(self.slave_instances.keys()))
        return self.slave_instances[self.current_slave_name]

    @property
    def slave_engine(self) -> ModbusSlaveEngine:
        """保持向后兼容：返回当前从机服务实例的通信引擎."""
        return self.current_slave_inst.engine

    def _init_style(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 9, "bold"), background="#e1e4e8")
        style.configure("Treeview", font=("Microsoft YaHei", 9), rowheight=24)
        style.map("Treeview", background=[("selected", "#0078d7")])

    def _build_ui(self):
        # 垂直窗格分割器 (支持上下自由拖拽拉伸调整日志区域大小)
        self.main_paned = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        self.main_paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        # 顶部选项卡区域 (放入 PanedWindow 上半部分)
        self.notebook = ttk.Notebook(self.main_paned)
        self.main_paned.add(self.notebook, weight=4)

        # Tab 1: Slave 从机模拟器
        self.slave_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.slave_frame, text="  🖥️ Modbus Slave 从机模拟器 (支持业务库导入与批量变位)  ")
        self._build_slave_tab(self.slave_frame)

        # Tab 2: Poll 主机调试器
        self.poll_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.poll_frame, text="  📡 Modbus Poll 主机轮询调试器 (支持业务规则导入)  ")
        self._build_poll_tab(self.poll_frame)

        # 底部日志面板 (放入 PanedWindow 下半部分，可自由上下拉伸)
        log_frame = ttk.LabelFrame(self.main_paned, text=" 实时系统与通信报文日志 (可上下拖动分割栏调整高度) ")
        self.main_paned.add(log_frame, weight=1)

        # 快捷工具栏 (复制、粘贴、全选、清空、导出CSV、自动滚屏)
        log_tools = ttk.Frame(log_frame)
        log_tools.pack(fill=tk.X, padx=4, pady=2)

        ttk.Button(log_tools, text="📋 复制", width=6, command=self._copy_log).pack(side=tk.LEFT, padx=2)
        ttk.Button(log_tools, text="📋 粘贴", width=6, command=self._paste_log).pack(side=tk.LEFT, padx=2)
        ttk.Button(log_tools, text="🔲 全选", width=6, command=self._select_all_log).pack(side=tk.LEFT, padx=2)
        ttk.Button(log_tools, text="🧹 清空", width=6, command=self._clear_log).pack(side=tk.LEFT, padx=2)
        ttk.Button(log_tools, text="💾 导出CSV", width=9, command=self._export_log_to_csv).pack(side=tk.LEFT, padx=2)
        ttk.Button(log_tools, text="📂 打开日志目录", width=12, command=self._open_log_dir).pack(side=tk.LEFT, padx=2)

        self.log_autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_tools, text="自动滚屏", variable=self.log_autoscroll_var).pack(side=tk.LEFT, padx=(10, 4))

        self.log_show_packets_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_tools, text="显示问询与响应报文", variable=self.log_show_packets_var).pack(side=tk.LEFT, padx=4)

        # 日志文本容器
        log_container = ttk.Frame(log_frame)
        log_container.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self.log_text = tk.Text(
            log_container,
            height=6,
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#ffffff",
            wrap=tk.WORD,
            undo=True,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        log_scroll = ttk.Scrollbar(log_container, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=log_scroll.set)

        # 配置日志颜色高亮标签
        self.log_text.tag_config("INFO", foreground="#d4d4d4")
        self.log_text.tag_config("TX", foreground="#4fc1ff")      # 问询/发送报文亮蓝
        self.log_text.tag_config("RX", foreground="#b5cea8")      # 响应/接收报文亮绿
        self.log_text.tag_config("WARN", foreground="#dcdcaa")    # 警告金黄
        self.log_text.tag_config("ERROR", foreground="#f44747")   # 错误鲜红

        self._setup_log_menu()

    def _setup_log_menu(self):
        """配置日志区域的右键菜单与快捷键."""
        # 快捷键绑定
        self.log_text.bind("<Control-c>", lambda e: self._copy_log())
        self.log_text.bind("<Control-C>", lambda e: self._copy_log())
        self.log_text.bind("<Control-v>", lambda e: self._paste_log())
        self.log_text.bind("<Control-V>", lambda e: self._paste_log())
        self.log_text.bind("<Control-a>", lambda e: self._select_all_log())
        self.log_text.bind("<Control-A>", lambda e: self._select_all_log())

        self.log_menu = tk.Menu(self.root, tearoff=0)
        self.log_menu.add_command(label="📋 复制 (Ctrl+C)", command=self._copy_log)
        self.log_menu.add_command(label="📋 粘贴 (Ctrl+V)", command=self._paste_log)
        self.log_menu.add_separator()
        self.log_menu.add_command(label="🔲 全选 (Ctrl+A)", command=self._select_all_log)
        self.log_menu.add_command(label="🧹 清空日志", command=self._clear_log)
        self.log_menu.add_separator()
        self.log_menu.add_command(label="💾 导出为 CSV 文件...", command=self._export_log_to_csv)
        self.log_menu.add_command(label="📂 打开本地实时日志目录...", command=self._open_log_dir)

        def _popup_menu(event):
            try:
                self.log_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.log_menu.grab_release()

        self.log_text.bind("<Button-3>", _popup_menu)

    def _copy_log(self, event=None):
        try:
            sel = self.log_text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            sel = self.log_text.get("1.0", tk.END).strip()
        if sel:
            self.root.clipboard_clear()
            self.root.clipboard_append(sel)
        return "break"

    def _paste_log(self, event=None):
        try:
            text = self.root.clipboard_get()
            if text:
                self.log_text.insert(tk.INSERT, text)
                self.log_text.see(tk.INSERT)
        except Exception:
            pass
        return "break"

    def _select_all_log(self, event=None):
        self.log_text.tag_add(tk.SEL, "1.0", tk.END)
        self.log_text.mark_set(tk.INSERT, tk.END)
        return "break"

    def _init_runtime_csv(self):
        """初始化后台实时追加落盘 CSV 文件 (按天滚动，UTF-8-BOM)."""
        try:
            with self._csv_lock:
                if not os.path.exists(self.runtime_csv_file) or os.path.getsize(self.runtime_csv_file) == 0:
                    with open(self.runtime_csv_file, "w", newline="", encoding="utf-8-sig") as f:
                        writer = csv.writer(f)
                        writer.writerow(["序号", "时间戳", "方向", "日志级别", "报文与详情内容"])
        except Exception as e:
            logger.debug(f"初始化实时 CSV 日志失败: {e}")

    def _append_runtime_csv(self, rec: Dict[str, str]):
        """线程安全地向本地 CSV 追加一条实时日志，带自动 flush 保证异常断电不丢日志."""
        try:
            with self._csv_lock:
                # 检查跨天按日期切换文件
                today_file = os.path.join(self.log_dir, f"modbus_runtime_{time.strftime('%Y%m%d')}.csv")
                if today_file != self.runtime_csv_file:
                    self.runtime_csv_file = today_file

                file_exists = os.path.exists(self.runtime_csv_file) and os.path.getsize(self.runtime_csv_file) > 0
                with open(self.runtime_csv_file, "a", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    if not file_exists:
                        writer.writerow(["序号", "时间戳", "方向", "日志级别", "报文与详情内容"])
                    writer.writerow([
                        len(self.log_records),
                        rec.get("time", ""),
                        rec.get("direction", ""),
                        rec.get("type", ""),
                        rec.get("message", ""),
                    ])
                    f.flush()
        except Exception as e:
            logger.debug(f"实时写入 CSV 日志失败: {e}")

    def _open_log_dir(self):
        """打开本地实时日志目录."""
        os.makedirs(self.log_dir, exist_ok=True)
        abs_path = os.path.abspath(self.log_dir)
        try:
            if sys.platform.startswith("win"):
                os.startfile(abs_path)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", abs_path])
        except Exception as e:
            messagebox.showinfo("日志目录", f"本地实时日志目录路径：\n{abs_path}")

    def _clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def _export_log_to_csv(self):
        """将通信报文与系统日志手动导出保存为指定的 CSV 文件 (包含时间、方向 RX/TX、报文及详情)."""
        if not self.log_records:
            messagebox.showinfo("提示", "当前没有可导出的日志记录！")
            return

        default_filename = f"modbus_log_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        file_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出通信报文与日志为 CSV",
            initialfile=default_filename,
            defaultextension=".csv",
            filetypes=[("CSV 表格文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["序号", "时间戳", "方向", "日志级别", "报文与详情内容"])
                for idx, r in enumerate(self.log_records, 1):
                    writer.writerow([
                        idx,
                        r.get("time", ""),
                        r.get("direction", ""),
                        r.get("type", ""),
                        r.get("message", ""),
                    ])

            messagebox.showinfo("导出成功", f"日志已成功导出至：\n{file_path}\n共导出 {len(self.log_records)} 条记录！")
            self.log(f"已手动导出 {len(self.log_records)} 条日志到 CSV: {file_path}")
        except Exception as e:
            messagebox.showerror("导出失败", f"写入 CSV 文件失败: {e}")

    def log(self, msg: str, level: str = "INFO"):
        """向底部输出带颜色标签的时间戳日志，记录内存缓存并自动实时追加落盘到本地 CSV."""
        now_dt = time.strftime("%Y-%m-%d %H:%M:%S")
        now_time = time.strftime("%H:%M:%S")

        direction = "-"
        if level == "RX" or "[RX" in msg:
            direction = "RX"
        elif level == "TX" or "[TX" in msg:
            direction = "TX"

        rec = {
            "time": now_dt,
            "direction": direction,
            "type": level,
            "message": msg,
        }
        self.log_records.append(rec)
        if len(self.log_records) > 20000:
            self.log_records.pop(0)

        # 实时自动追加写入本地 CSV 文件 (保证异常断电/崩溃不丢数据)
        self._append_runtime_csv(rec)

        if (level in ("TX", "RX")) and hasattr(self, "log_show_packets_var") and not self.log_show_packets_var.get():
            return
        line = f"[{now_time}] [{level}] {msg}\n"
        self.log_text.insert(tk.END, line, level)
        if not hasattr(self, "log_autoscroll_var") or self.log_autoscroll_var.get():
            self.log_text.see(tk.END)

    # =================================================================
    # Tab 1: Slave 从机模拟器 UI
    # =================================================================
    def _build_slave_tab(self, parent: ttk.Frame):
        # 1. 顶部控制栏
        top_bar = ttk.LabelFrame(parent, text=" 从机服务实例与连接配置 (支持多实例独立监听不同 IP:Port 或 COM 串口) ")
        top_bar.pack(fill=tk.X, padx=6, pady=3)

        # 第 0 行：服务实例选择与业务数据库操作
        ttk.Label(top_bar, text="从机服务实例:").grid(row=0, column=0, padx=4, pady=3, sticky=tk.W)
        self.slave_inst_combo = ttk.Combobox(top_bar, width=28, state="readonly")
        self.slave_inst_combo.grid(row=0, column=1, columnspan=2, padx=4, pady=3, sticky=tk.W)
        self.slave_inst_combo.bind("<<ComboboxSelected>>", self._on_switch_slave_instance)

        ttk.Button(top_bar, text="➕ 新建服务", command=self._add_new_slave_instance).grid(row=0, column=3, padx=2, pady=3)
        ttk.Button(top_bar, text="🗑️ 删除服务", command=self._delete_current_slave_instance).grid(row=0, column=4, padx=2, pady=3)

        ttk.Separator(top_bar, orient=tk.VERTICAL).grid(row=0, column=5, rowspan=2, sticky="ns", padx=8, pady=2)

        # 核心业务数据库管理按钮组
        btn_switch_db = ttk.Button(
            top_bar,
            text="🗃️ 关联/切换数据库...",
            command=self._select_database_file,
        )
        btn_switch_db.grid(row=0, column=6, padx=3, pady=3)

        btn_import_db = ttk.Button(
            top_bar,
            text="📂 导入设备点表...",
            command=self._open_import_db_dialog,
        )
        btn_import_db.grid(row=0, column=7, padx=3, pady=3)

        btn_pairing = ttk.Button(
            top_bar,
            text="🔗 业务配对中心 (基于 More)...",
            command=self._open_pairing_dialog,
        )
        btn_pairing.grid(row=0, column=8, padx=3, pady=3)

        # 第 1 行：通讯方式单选 + 动态参数容器 + 启停操作
        cfg_row = ttk.Frame(top_bar)
        cfg_row.grid(row=1, column=0, columnspan=10, sticky="ew", padx=2, pady=3)

        ttk.Label(cfg_row, text="通讯方式:").pack(side=tk.LEFT, padx=(4, 2))
        self.slave_comm_type_var = tk.StringVar(value="TCP")
        ttk.Radiobutton(
            cfg_row,
            text="以太网 (TCP)",
            value="TCP",
            variable=self.slave_comm_type_var,
            command=self._on_slave_comm_type_change,
        ).pack(side=tk.LEFT, padx=3)
        ttk.Radiobutton(
            cfg_row,
            text="串行端口 (RTU / 485 / 232)",
            value="RTU",
            variable=self.slave_comm_type_var,
            command=self._on_slave_comm_type_change,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Separator(cfg_row, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=2)

        # TCP 参数子容器
        self.slave_tcp_frame = ttk.Frame(cfg_row)
        ttk.Label(self.slave_tcp_frame, text="监听 IP:").pack(side=tk.LEFT, padx=(2, 2))
        self.slave_ip_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(self.slave_tcp_frame, textvariable=self.slave_ip_var, width=11).pack(side=tk.LEFT, padx=2)

        ttk.Label(self.slave_tcp_frame, text="端口:").pack(side=tk.LEFT, padx=(6, 2))
        self.slave_port_var = tk.IntVar(value=5020)
        ttk.Entry(self.slave_tcp_frame, textvariable=self.slave_port_var, width=6).pack(side=tk.LEFT, padx=2)

        # RTU 参数子容器
        self.slave_rtu_frame = ttk.Frame(cfg_row)
        ttk.Label(self.slave_rtu_frame, text="串口 (COM):").pack(side=tk.LEFT, padx=(2, 2))
        self.slave_serial_port_var = tk.StringVar(value="COM1")
        self.slave_com_combo = ttk.Combobox(
            self.slave_rtu_frame,
            textvariable=self.slave_serial_port_var,
            width=8,
            values=get_available_com_ports(),
        )
        self.slave_com_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(
            self.slave_rtu_frame,
            text="🔄",
            width=3,
            command=self._refresh_slave_com_ports,
        ).pack(side=tk.LEFT, padx=1)

        ttk.Label(self.slave_rtu_frame, text="总线:").pack(side=tk.LEFT, padx=(6, 2))
        self.slave_bus_label_var = tk.StringVar(value="RS-485(A1/B1)")
        self.slave_bus_combo = ttk.Combobox(
            self.slave_rtu_frame,
            textvariable=self.slave_bus_label_var,
            width=14,
            values=SERIAL_BUS_NAMES,
        )
        self.slave_bus_combo.pack(side=tk.LEFT, padx=2)

        ttk.Label(self.slave_rtu_frame, text="波特率:").pack(side=tk.LEFT, padx=(6, 2))
        self.slave_baud_var = tk.IntVar(value=9600)
        self.slave_baud_combo = ttk.Combobox(
            self.slave_rtu_frame,
            textvariable=self.slave_baud_var,
            width=7,
            state="readonly",
            values=AVAILABLE_BAUDRATES,
        )
        self.slave_baud_combo.pack(side=tk.LEFT, padx=2)

        ttk.Label(self.slave_rtu_frame, text="校验:").pack(side=tk.LEFT, padx=(6, 2))
        self.slave_parity_var = tk.StringVar(value="N (无校验)")
        self.slave_parity_combo = ttk.Combobox(
            self.slave_rtu_frame,
            textvariable=self.slave_parity_var,
            width=10,
            state="readonly",
            values=AVAILABLE_PARITIES,
        )
        self.slave_parity_combo.pack(side=tk.LEFT, padx=2)

        # 默认先打包 TCP 容器
        self.slave_tcp_frame.pack(side=tk.LEFT)

        # 公共参数：从机 ID
        self.slave_common_frame = ttk.Frame(cfg_row)
        self.slave_common_frame.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(self.slave_common_frame, text="从机 ID:").pack(side=tk.LEFT, padx=(2, 2))
        self.slave_id_var = tk.IntVar(value=1)
        ttk.Entry(self.slave_common_frame, textvariable=self.slave_id_var, width=4).pack(side=tk.LEFT, padx=2)

        # 右侧操作按钮
        action_box = ttk.Frame(cfg_row)
        action_box.pack(side=tk.RIGHT, padx=4)

        ttk.Button(action_box, text="⏹ 全部停止", command=self._stop_all_slaves).pack(side=tk.RIGHT, padx=2)
        ttk.Button(action_box, text="⚡ 全部启动", command=self._start_all_slaves).pack(side=tk.RIGHT, padx=2)
        self.slave_status_lbl = ttk.Label(
            action_box,
            text="状态: 已停止 🔴",
            font=("Microsoft YaHei", 9, "bold"),
            foreground="red",
        )
        self.slave_status_lbl.pack(side=tk.RIGHT, padx=6)
        self.btn_slave_start = ttk.Button(
            action_box,
            text="▶ 启动当前服务",
            command=self._toggle_slave_server,
        )
        self.btn_slave_start.pack(side=tk.RIGHT, padx=2)

        # 2. 批量生成与单个添加控制栏
        point_ctrl_bar = ttk.LabelFrame(parent, text=" 点位规则生成 & 快捷操作 ")
        point_ctrl_bar.pack(fill=tk.X, padx=6, pady=3)

        btn_batch_dlg = ttk.Button(
            point_ctrl_bar,
            text="⚡ 批量输入规则生成点表...",
            command=self._open_batch_generate_dialog,
        )
        btn_batch_dlg.grid(row=0, column=0, padx=6, pady=3)

        ttk.Separator(point_ctrl_bar, orient=tk.VERTICAL).grid(row=0, column=1, sticky="ns", padx=4, pady=2)

        ttk.Label(point_ctrl_bar, text="地址:").grid(row=0, column=2, padx=2, pady=2)
        self.new_addr_var = tk.StringVar(value="")
        ttk.Entry(point_ctrl_bar, textvariable=self.new_addr_var, width=6).grid(row=0, column=3, padx=2, pady=2)

        ttk.Label(point_ctrl_bar, text="描述:").grid(row=0, column=4, padx=2, pady=2)
        self.new_desc_var = tk.StringVar(value="")
        ttk.Entry(point_ctrl_bar, textvariable=self.new_desc_var, width=12).grid(row=0, column=5, padx=2, pady=2)

        ttk.Label(point_ctrl_bar, text="区域:").grid(row=0, column=6, padx=2, pady=2)
        self.new_area_var = tk.StringVar(value=AreaType.HOLDING_REGISTER)
        area_combo = ttk.Combobox(point_ctrl_bar, textvariable=self.new_area_var, width=17, state="readonly")
        area_combo["values"] = [
            AreaType.HOLDING_REGISTER,
            AreaType.INPUT_REGISTER,
            AreaType.COIL,
            AreaType.DISCRETE_INPUT,
        ]
        area_combo.grid(row=0, column=7, padx=2, pady=2)

        ttk.Label(point_ctrl_bar, text="类型:").grid(row=0, column=8, padx=2, pady=2)
        self.new_type_var = tk.StringVar(value=ModbusDataType.FLOAT32.value)
        type_combo = ttk.Combobox(point_ctrl_bar, textvariable=self.new_type_var, width=9, state="readonly")
        type_combo["values"] = [t.value for t in ModbusDataType]
        type_combo.grid(row=0, column=9, padx=2, pady=2)

        ttk.Label(point_ctrl_bar, text="变位:").grid(row=0, column=10, padx=2, pady=2)
        self.new_order_var = tk.StringVar(value=ByteOrderMode.CDAB.value)
        order_combo = ttk.Combobox(point_ctrl_bar, textvariable=self.new_order_var, width=7, state="readonly")
        order_combo["values"] = [m.value for m in ByteOrderMode]
        order_combo.grid(row=0, column=11, padx=2, pady=2)

        ttk.Label(point_ctrl_bar, text="初始值:").grid(row=0, column=12, padx=2, pady=2)
        self.new_val_var = tk.StringVar(value="")
        ttk.Entry(point_ctrl_bar, textvariable=self.new_val_var, width=7).grid(row=0, column=13, padx=2, pady=2)

        ttk.Label(point_ctrl_bar, text="模拟:").grid(row=0, column=14, padx=2, pady=2)
        self.new_sim_var = tk.StringVar(value="固定")
        sim_combo = ttk.Combobox(point_ctrl_bar, textvariable=self.new_sim_var, width=8, state="readonly")
        sim_combo["values"] = ["固定", "随机波动", "累加递增", "正弦波"]
        sim_combo.grid(row=0, column=15, padx=2, pady=2)

        ttk.Button(point_ctrl_bar, text="➕ 单点添加", command=self._add_or_update_slave_point).grid(row=0, column=16, padx=4, pady=2)

        # 3. 批量操作工具栏（支持选择当前从机或全部从机服务生效）
        batch_bar = ttk.LabelFrame(parent, text=" 🛠️ 批量操作与规则配置 (支持选择当前从机或全部从机生效) ")
        batch_bar.pack(fill=tk.X, padx=6, pady=3)

        # 适用范围: "current" = 仅当前从机, "all" = 全部从机服务
        self.batch_scope_var = tk.StringVar(value="current")

        # --- 第 0 行：适用范围与模拟规则配置 (高频使用) ---
        ttk.Label(batch_bar, text="适用范围:").grid(row=0, column=0, padx=4, pady=2, sticky=tk.W)
        ttk.Radiobutton(batch_bar, text="仅当前从机", value="current", variable=self.batch_scope_var).grid(row=0, column=1, padx=2, pady=2)
        ttk.Radiobutton(batch_bar, text="全部从机服务", value="all", variable=self.batch_scope_var).grid(row=0, column=2, padx=4, pady=2)

        ttk.Separator(batch_bar, orient=tk.VERTICAL).grid(row=0, column=3, rowspan=2, sticky="ns", padx=8, pady=2)

        ttk.Label(batch_bar, text="模拟规则:").grid(row=0, column=4, padx=4, pady=2)
        self.batch_sim_var = tk.StringVar(value="固定")
        batch_sim_cb = ttk.Combobox(batch_bar, textvariable=self.batch_sim_var, width=8, state="readonly")
        batch_sim_cb["values"] = ["固定", "随机波动", "累加递增", "正弦波"]
        batch_sim_cb.grid(row=0, column=5, padx=2, pady=2)
        ttk.Button(batch_bar, text="⚡ 批量修改规则", command=self._apply_batch_sim).grid(row=0, column=6, padx=4, pady=2)

        ttk.Separator(batch_bar, orient=tk.VERTICAL).grid(row=0, column=7, rowspan=2, sticky="ns", padx=8, pady=2)

        ttk.Button(batch_bar, text="☑️ 全选点位", command=self._select_all_slave_points).grid(row=0, column=8, padx=3, pady=2)
        ttk.Button(batch_bar, text="⬜ 取消选择", command=self._deselect_all_slave_points).grid(row=0, column=9, padx=3, pady=2)
        ttk.Button(batch_bar, text="🗑️ 批量删除选中", command=self._delete_slave_point).grid(row=0, column=10, padx=6, pady=2)

        # --- 第 1 行：变位模式、数据类型、统一赋值 ---
        ttk.Label(batch_bar, text="目标变位:").grid(row=1, column=0, padx=4, pady=2, sticky=tk.W)
        self.batch_order_var = tk.StringVar(value=ByteOrderMode.ABCD.value)
        batch_order_cb = ttk.Combobox(batch_bar, textvariable=self.batch_order_var, width=8, state="readonly")
        batch_order_cb["values"] = [m.value for m in ByteOrderMode]
        batch_order_cb.grid(row=1, column=1, columnspan=2, padx=2, pady=2, sticky=tk.W)
        ttk.Button(batch_bar, text="批量修改变位", command=self._apply_batch_order).grid(row=1, column=4, padx=4, pady=2)

        ttk.Label(batch_bar, text="目标类型:").grid(row=1, column=5, padx=4, pady=2)
        self.batch_type_var = tk.StringVar(value=ModbusDataType.FLOAT32.value)
        batch_type_cb = ttk.Combobox(batch_bar, textvariable=self.batch_type_var, width=9, state="readonly")
        batch_type_cb["values"] = [t.value for t in ModbusDataType]
        batch_type_cb.grid(row=1, column=6, padx=2, pady=2)
        ttk.Button(batch_bar, text="批量修改类型", command=self._apply_batch_type).grid(row=1, column=8, padx=3, pady=2)

        ttk.Label(batch_bar, text="统一设值:").grid(row=1, column=9, padx=4, pady=2)
        self.batch_val_var = tk.StringVar(value="0")
        ttk.Entry(batch_bar, textvariable=self.batch_val_var, width=7).grid(row=1, column=10, padx=2, pady=2)
        ttk.Button(batch_bar, text="批量修改数值", command=self._apply_batch_value).grid(row=1, column=11, padx=3, pady=2)

        # 4. 点位表格视图
        table_frame = ttk.Frame(parent)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=3)

        cols = ("addr", "desc", "area", "type", "order", "val", "hex", "sim", "reg_cnt")
        self.slave_tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended")

        self.slave_tree.heading("addr", text="起始地址")
        self.slave_tree.heading("desc", text="点位描述 / 业务字段")
        self.slave_tree.heading("area", text="存储区域")
        self.slave_tree.heading("type", text="数据类型")
        self.slave_tree.heading("order", text="变位模式")
        self.slave_tree.heading("val", text="当前解析数值")
        self.slave_tree.heading("hex", text="原始寄存器(Hex)")
        self.slave_tree.heading("sim", text="动态模拟")
        self.slave_tree.heading("reg_cnt", text="占用字数")

        self.slave_tree.column("addr", width=70, anchor=tk.CENTER)
        self.slave_tree.column("desc", width=190, anchor=tk.W)
        self.slave_tree.column("area", width=130, anchor=tk.W)
        self.slave_tree.column("type", width=90, anchor=tk.CENTER)
        self.slave_tree.column("order", width=80, anchor=tk.CENTER)
        self.slave_tree.column("val", width=120, anchor=tk.E)
        self.slave_tree.column("hex", width=170, anchor=tk.W)
        self.slave_tree.column("sim", width=80, anchor=tk.CENTER)
        self.slave_tree.column("reg_cnt", width=65, anchor=tk.CENTER)

        tree_scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.slave_tree.yview)
        tree_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.slave_tree.config(yscrollcommand=tree_scroll_y.set)
        self.slave_tree.pack(fill=tk.BOTH, expand=True)

        self.slave_tree.bind("<Double-1>", self._on_slave_double_click)
        self._build_slave_context_menu()
        self.slave_tree.bind("<Button-3>", self._show_slave_context_menu)
        self._update_slave_instance_combo()

    # -----------------------------------------------------------------
    # 多从机实例管理方法 (支持独立 IP 和端口监听)
    # -----------------------------------------------------------------
    def _update_slave_instance_combo(self):
        """刷新从机实例下拉列表."""
        names = list(self.slave_instances.keys())
        self.slave_inst_combo["values"] = names
        if self.current_slave_name in names:
            self.slave_inst_combo.set(self.current_slave_name)
        elif names:
            self.current_slave_name = names[0]
            self.slave_inst_combo.set(self.current_slave_name)
        self._on_switch_slave_instance()

    def _on_slave_comm_type_change(self):
        """用户切换当前从机通讯方式单选框."""
        c_type = self.slave_comm_type_var.get()
        if self.current_slave_name in self.slave_instances:
            self.current_slave_inst.comm_type = c_type
        self._update_slave_conn_mode_ui()
        self._update_slave_status_ui()

    def _update_slave_conn_mode_ui(self):
        """根据当前从机通讯方式切换输入框容器."""
        c_type = self.slave_comm_type_var.get()
        if c_type == "RTU":
            self.slave_tcp_frame.pack_forget()
            self.slave_rtu_frame.pack(side=tk.LEFT, before=self.slave_common_frame)
        else:
            self.slave_rtu_frame.pack_forget()
            self.slave_tcp_frame.pack(side=tk.LEFT, before=self.slave_common_frame)

    def _refresh_slave_com_ports(self):
        """刷新从机串口端口列表."""
        ports = get_available_com_ports()
        self.slave_com_combo["values"] = ports
        if ports and self.slave_serial_port_var.get() not in ports:
            self.slave_serial_port_var.set(ports[0])

    def _on_switch_slave_instance(self, event=None):
        """切换当前激活的从机服务实例."""
        target_name = self.slave_inst_combo.get()
        if target_name in self.slave_instances:
            self.current_slave_name = target_name
            inst = self.current_slave_inst
            self.slave_comm_type_var.set(inst.comm_type)
            self.slave_ip_var.set(inst.host)
            self.slave_port_var.set(inst.port)
            self.slave_serial_port_var.set(inst.serial_port)
            self.slave_bus_label_var.set(inst.serial_bus_label)
            self.slave_baud_var.set(inst.baudrate)
            p_display = "无校验 (None, N)" if inst.parity == "N" else ("奇校验 (Odd, O)" if inst.parity == "O" else "偶校验 (Even, E)")
            self.slave_parity_var.set(p_display)
            self.slave_id_var.set(inst.slave_id)
            self._update_slave_conn_mode_ui()
            self._update_slave_status_ui()
            self._refresh_slave_tree()

    def _update_slave_status_ui(self):
        """更新当前从机服务的 UI 状态显示."""
        inst = self.current_slave_inst
        if inst.comm_type == "RTU":
            loc_str = f"RTU {inst.serial_port} [{inst.serial_bus_label}]"
        else:
            loc_str = f"TCP {inst.host}:{inst.port}"

        if inst.engine.is_running:
            self.slave_status_lbl.config(
                text=f"运行中 🟢 ({loc_str})",
                foreground="green",
            )
            self.btn_slave_start.config(text="⏹ 停止服务")
        else:
            self.slave_status_lbl.config(
                text=f"已停止 🔴 ({loc_str})",
                foreground="red",
            )
            self.btn_slave_start.config(text="▶ 启动当前服务")

    def _add_new_slave_instance(self):
        """弹出窗口手动新建一个独立监听的从机服务实例 (支持 TCP / RTU 串口)."""
        dlg = tk.Toplevel(self.root)
        dlg.title("新建独立 Modbus Slave 服务实例")
        dlg.geometry("420x330")
        dlg.transient(self.root)
        dlg.grab_set()

        form = ttk.Frame(dlg, padding=15)
        form.pack(fill=tk.BOTH, expand=True)

        ttk.Label(form, text="服务实例名称:").grid(row=0, column=0, sticky=tk.W, pady=4)
        name_var = tk.StringVar(value=f"从机服务_{len(self.slave_instances)+1}")
        ttk.Entry(form, textvariable=name_var, width=24).grid(row=0, column=1, pady=4)

        ttk.Label(form, text="通讯协议方式:").grid(row=1, column=0, sticky=tk.W, pady=4)
        ctype_var = tk.StringVar(value="TCP")
        ct_row = ttk.Frame(form)
        ct_row.grid(row=1, column=1, sticky=tk.W, pady=4)
        ttk.Radiobutton(ct_row, text="以太网 TCP", value="TCP", variable=ctype_var).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Radiobutton(ct_row, text="串行端口 RTU", value="RTU", variable=ctype_var).pack(side=tk.LEFT)

        # 动态容器
        dyn_frame = ttk.Frame(form)
        dyn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)

        tcp_box = ttk.Frame(dyn_frame)
        ttk.Label(tcp_box, text="监听 IP 地址:").grid(row=0, column=0, sticky=tk.W, pady=4)
        ip_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(tcp_box, textvariable=ip_var, width=24).grid(row=0, column=1, pady=4)

        ttk.Label(tcp_box, text="监听端口号:").grid(row=1, column=0, sticky=tk.W, pady=4)
        used_ports = [inst.port for inst in self.slave_instances.values() if inst.comm_type == "TCP"]
        suggested_port = 5020 + len(self.slave_instances)
        while suggested_port in used_ports:
            suggested_port += 1
        port_var = tk.IntVar(value=suggested_port)
        ttk.Entry(tcp_box, textvariable=port_var, width=24).grid(row=1, column=1, pady=4)

        rtu_box = ttk.Frame(dyn_frame)
        ttk.Label(rtu_box, text="串口端口 (COM):").grid(row=0, column=0, sticky=tk.W, pady=4)
        c_ports = get_available_com_ports()
        default_com = f"COM{len(self.slave_instances)+1}" if f"COM{len(self.slave_instances)+1}" in c_ports else (c_ports[0] if c_ports else "COM1")
        com_var = tk.StringVar(value=default_com)
        ttk.Combobox(rtu_box, textvariable=com_var, values=c_ports, width=22).grid(row=0, column=1, pady=4)

        ttk.Label(rtu_box, text="总线硬件标识:").grid(row=1, column=0, sticky=tk.W, pady=4)
        bus_var = tk.StringVar(value="RS-485(A1/B1)")
        ttk.Combobox(rtu_box, textvariable=bus_var, values=SERIAL_BUS_NAMES, width=22).grid(row=1, column=1, pady=4)

        ttk.Label(rtu_box, text="波特率与校验:").grid(row=2, column=0, sticky=tk.W, pady=4)
        baud_p_row = ttk.Frame(rtu_box)
        baud_p_row.grid(row=2, column=1, sticky=tk.W, pady=4)
        dlg_baud_var = tk.IntVar(value=9600)
        ttk.Combobox(baud_p_row, textvariable=dlg_baud_var, values=AVAILABLE_BAUDRATES, width=7, state="readonly").pack(side=tk.LEFT)
        dlg_p_var = tk.StringVar(value="N (无校验)")
        ttk.Combobox(baud_p_row, textvariable=dlg_p_var, values=AVAILABLE_PARITIES, width=10, state="readonly").pack(side=tk.LEFT, padx=3)

        tcp_box.pack(fill=tk.X)

        def _on_dlg_ct_change(*args):
            if ctype_var.get() == "RTU":
                tcp_box.pack_forget()
                rtu_box.pack(fill=tk.X)
            else:
                rtu_box.pack_forget()
                tcp_box.pack(fill=tk.X)

        ctype_var.trace_add("write", _on_dlg_ct_change)

        ttk.Label(form, text="从机站号 (Unit ID):").grid(row=3, column=0, sticky=tk.W, pady=4)
        sid_var = tk.IntVar(value=len(self.slave_instances) + 1)
        ttk.Entry(form, textvariable=sid_var, width=24).grid(row=3, column=1, pady=4)

        def _confirm_add():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("错误", "服务名称不能为空")
                return
            if name in self.slave_instances:
                messagebox.showerror("错误", f"已存在同名服务实例: {name}")
                return
            sid = sid_var.get()
            selected_ct = ctype_var.get()
            if selected_ct == "RTU":
                sport = com_var.get().strip() or "COM1"
                sbus = bus_var.get().strip() or "RS-485(A1/B1)"
                sbaud = dlg_baud_var.get()
                sp_raw = dlg_p_var.get().strip().upper()
                sparity = sp_raw[0] if sp_raw else "N"
                new_inst = SlaveServiceInstance(
                    name,
                    slave_id=sid,
                    comm_type="RTU",
                    serial_port=sport,
                    baudrate=sbaud,
                    parity=sparity,
                    serial_bus_label=sbus,
                )
                self.log(f"新建串口从机服务 [{name}]，配置: {sport} [{sbus}] (波特率: {sbaud}, 8-{sparity}-1, 从机ID: {sid})")
            else:
                host = ip_var.get().strip() or "0.0.0.0"
                port = port_var.get()
                new_inst = SlaveServiceInstance(name, host=host, port=port, slave_id=sid, comm_type="TCP")
                self.log(f"新建以太网从机服务 [{name}]，监听配置: {host}:{port} (从机ID: {sid})")

            self._register_slave_instance(new_inst)
            self.current_slave_name = name
            self._update_slave_instance_combo()
            dlg.destroy()

        btn_row = ttk.Frame(form)
        btn_row.grid(row=4, column=0, columnspan=2, pady=12)
        ttk.Button(btn_row, text="确定创建", command=_confirm_add).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_row, text="取消", command=dlg.destroy).pack(side=tk.RIGHT, padx=8)

    def _delete_current_slave_instance(self):
        """删除当前选中的从机服务实例."""
        if len(self.slave_instances) <= 1:
            messagebox.showwarning("无法删除", "系统至少需要保留一个从机服务实例！")
            return
        inst = self.current_slave_inst
        if not messagebox.askyesno("删除确认", f"确定删除从机服务实例 [{inst.name}] 吗？\n如果该服务正在运行，将自动终止其监听。"):
            return
        if inst.engine.is_running:
            inst.engine.stop()
        del self.slave_instances[inst.name]
        self.current_slave_name = next(iter(self.slave_instances.keys()))
        self._update_slave_instance_combo()
        self.log(f"已删除从机服务实例 [{inst.name}]")

    def _start_all_slaves(self):
        """一键启动所有配置的不同 IP:Port 或不同串口从机服务."""
        started_cnt = 0
        for name, inst in self.slave_instances.items():
            if not inst.engine.is_running:
                try:
                    inst.engine.comm_type = inst.comm_type
                    inst.engine.slave_id = inst.slave_id
                    if inst.comm_type == "RTU":
                        inst.engine.serial_port = inst.serial_port
                        inst.engine.baudrate = inst.baudrate
                        inst.engine.bytesize = inst.bytesize
                        inst.engine.parity = inst.parity
                        inst.engine.stopbits = inst.stopbits
                        inst.engine.start()
                        self.log(f"串口从机 [{name}] 已启动监听: {inst.serial_port} [{inst.serial_bus_label}] (波特率: {inst.baudrate}, 8-{inst.parity}-1)")
                    else:
                        inst.engine.host = inst.host
                        inst.engine.port = inst.port
                        inst.engine.start()
                        self.log(f"以太网从机 [{name}] 已启动监听: {inst.host}:{inst.port}")
                    started_cnt += 1
                except Exception as e:
                    self.log(f"从机 [{name}] 启动失败: {e}", "ERROR")
        self._update_slave_status_ui()
        messagebox.showinfo("全部启动完成", f"已成功启动 {started_cnt} 个独立的从机监听服务！")

    def _stop_all_slaves(self):
        """一键停止所有从机服务."""
        stopped_cnt = 0
        for name, inst in self.slave_instances.items():
            if inst.engine.is_running:
                inst.engine.stop()
                stopped_cnt += 1
                self.log(f"从机 [{name}] 已停止监听")
        self._update_slave_status_ui()
        messagebox.showinfo("全部停止完成", f"已停止 {stopped_cnt} 个从机服务。")

    # -----------------------------------------------------------------
    # 业务数据库管理：选择/切换关联的 HEMS 数据库文件
    # -----------------------------------------------------------------
    def _select_database_file(self, on_success_callback=None):
        """弹出文件选择对话框，动态修改/切换关联的 HEMS 业务数据库."""
        initial_dir = os.path.dirname(os.path.abspath(self.hems_db.db_path)) if os.path.exists(self.hems_db.db_path) else os.getcwd()
        file_path = filedialog.askopenfilename(
            title="选择关联的业务数据库 (HEMS SQLite 格式)",
            initialdir=initial_dir,
            filetypes=[
                ("SQLite 数据库文件 (*.cdb, *.sqlite, *.db)", "*.cdb;*.sqlite;*.db"),
                ("所有文件 (*.*)", "*.*"),
            ],
        )
        if not file_path:
            return False

        try:
            new_db = HemsDatabase(file_path)
            apps = new_db.load_modbus_apps()
            self.hems_db = new_db
            self.pairing_engine = HemsPairingEngine(file_path)
            self.log(f"已成功关联新业务数据库: {file_path} (识别到 {len(apps)} 个 Modbus 业务模块)", "INFO")
            messagebox.showinfo("关联成功", f"已成功切换关联数据库：\n{file_path}\n\n共识别到 {len(apps)} 个 Modbus 业务模块。")
            if on_success_callback:
                on_success_callback()
            return True
        except Exception as e:
            logger.error(f"切换数据库失败: {e}")
            messagebox.showerror("切换数据库失败", f"无法打开或解析选中的数据库文件：\n{e}")
            return False

    # -----------------------------------------------------------------
    # 核心：从 HEMS 业务数据库导入设备点表
    # -----------------------------------------------------------------
    def _open_import_db_dialog(self):
        apps = self.hems_db.load_modbus_apps()
        if not apps:
            ret = messagebox.askyesno(
                "未找到数据",
                f"当前数据库路径：\n{self.hems_db.db_path}\n\n未读取到有效的 Modbus 应用配置。\n是否立即浏览选择其他业务数据库文件？",
            )
            if ret:
                self._select_database_file(on_success_callback=self._open_import_db_dialog)
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"从业务数据库导入 Modbus 设备业务配置 - [{os.path.basename(self.hems_db.db_path)}]")
        dlg.geometry("820x540")
        dlg.transient(self.root)
        dlg.grab_set()

        top_desc = ttk.Frame(dlg, padding=(10, 6))
        top_desc.pack(fill=tk.X, expand=False)

        title_line = ttk.Frame(top_desc)
        title_line.pack(fill=tk.X)

        ttk.Label(
            title_line,
            text=f"📂 当前数据库: {self.hems_db.db_path} | 共识别到 {len(apps)} 个 Modbus 业务模块 (app 表配置)",
            font=("Microsoft YaHei", 9, "bold"),
            foreground="#0066cc",
        ).pack(side=tk.LEFT)

        def _change_db_in_dialog():
            dlg.destroy()
            self._select_database_file(on_success_callback=self._open_import_db_dialog)

        ttk.Button(
            title_line,
            text="📂 浏览更换数据库...",
            command=_change_db_in_dialog,
        ).pack(side=tk.RIGHT, padx=4)

        ttk.Label(
            top_desc,
            text="请在下方选择目标设备，可一键将其业务点位/轮询规则导入至 Slave 模拟器或 Poll 调试器中：",
        ).pack(anchor=tk.W, pady=(4, 0))

        # 快捷全选控制条 (固定在顶部，不随拉伸改变高度)
        select_bar = ttk.Frame(dlg, padding=(10, 2))
        select_bar.pack(fill=tk.X, expand=False)

        ttk.Label(select_bar, text="快捷多选:", font=("Microsoft YaHei", 9, "bold")).pack(side=tk.LEFT, padx=(0, 6))

        def _select_all_enabled_slaves():
            tree.selection_remove(tree.selection())
            to_sel = []
            for item_id in tree.get_children():
                aid = int(tree.item(item_id)["values"][0])
                app_obj = app_map.get(aid)
                if app_obj and app_obj.app_type == 1 and app_obj.enable:
                    to_sel.append(item_id)
            if to_sel:
                tree.selection_set(to_sel)
                tree.see(to_sel[0])
            else:
                messagebox.showinfo("提示", "未找到状态为【已启用】的 Type=1 (Slave 从机) 设备！")

        def _select_all_enabled_polls():
            tree.selection_remove(tree.selection())
            to_sel = []
            for item_id in tree.get_children():
                aid = int(tree.item(item_id)["values"][0])
                app_obj = app_map.get(aid)
                if app_obj and app_obj.app_type == 2 and app_obj.enable:
                    to_sel.append(item_id)
            if to_sel:
                tree.selection_set(to_sel)
                tree.see(to_sel[0])
            else:
                messagebox.showinfo("提示", "未找到状态为【已启用】的 Type=2 (Poll 主机) 应用！")

        def _clear_all_selection():
            tree.selection_remove(tree.selection())

        ttk.Button(select_bar, text="☑️ 全选已启用 Slave (Type=1)", command=_select_all_enabled_slaves).pack(side=tk.LEFT, padx=3)
        ttk.Button(select_bar, text="☑️ 全选已启用 Poll (Type=2)", command=_select_all_enabled_polls).pack(side=tk.LEFT, padx=3)
        ttk.Button(select_bar, text="⬜ 清空选择", command=_clear_all_selection).pack(side=tk.LEFT, padx=3)

        # 单独的中间表格容器 (垂直水平自适应拉伸，向下拉伸只拉伸此表格)
        table_frame = ttk.Frame(dlg, padding=(10, 4))
        table_frame.pack(fill=tk.BOTH, expand=True)

        app_map = {a.app_id: a for a in apps}

        cols = ("id", "type", "perm", "eng", "chn", "net", "polls", "vars", "enable")
        tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended")

        tree.heading("id", text="App ID")
        tree.heading("type", text="业务类型")
        tree.heading("perm", text="导入权限限制")
        tree.heading("eng", text="英文标识")
        tree.heading("chn", text="业务名称")
        tree.heading("net", text="通讯配置 (以太网 / 串口)")
        tree.heading("polls", text="轮询数")
        tree.heading("vars", text="测点数")
        tree.heading("enable", text="状态")

        tree.column("id", width=55, anchor=tk.CENTER)
        tree.column("type", width=70, anchor=tk.CENTER)
        tree.column("perm", width=140, anchor=tk.CENTER)
        tree.column("eng", width=130, anchor=tk.W)
        tree.column("chn", width=120, anchor=tk.W)
        tree.column("net", width=190, anchor=tk.W)
        tree.column("polls", width=55, anchor=tk.CENTER)
        tree.column("vars", width=55, anchor=tk.CENTER)
        tree.column("enable", width=55, anchor=tk.CENTER)

        for a in apps:
            status_text = "已启用" if a.enable else "停用"
            if a.comm_type == "RTU":
                net_str = f"🔌串口 [{a.serial_info.get('port_name', 'RS-485')}] {a.serial_info.get('baudrate', 9600)}"
            else:
                net_str = f"🌐以太网 {a.ip}:{a.port}"

            tree.insert(
                "",
                tk.END,
                values=(
                    a.app_id,
                    f"Type={a.app_type}",
                    a.permission_tag,
                    a.english_name,
                    a.chinese_name,
                    net_str,
                    len(a.pollings),
                    len(a.points),
                    status_text,
                ),
            )

        scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        tree.config(yscrollcommand=scroll_y.set)
        tree.pack(fill=tk.BOTH, expand=True)

        # 底部操作栏 (固定高度，不垂直拉伸)
        btn_bar = ttk.Frame(dlg, padding=(10, 8))
        btn_bar.pack(fill=tk.X, expand=False)

        lbl_perm_hint = ttk.Label(btn_bar, text="💡 业务规范提示：请在上方列表中选择设备以查看导入权限...", font=("Microsoft YaHei", 9), foreground="#555555")
        lbl_perm_hint.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        btn_row = ttk.Frame(btn_bar)
        btn_row.pack(fill=tk.X)

        def _get_selected_apps() -> List[HemsAppModel]:
            sel = tree.selection()
            return [app_map[int(tree.item(s)["values"][0])] for s in sel if int(tree.item(s)["values"][0]) in app_map]

        def _do_import_to_slave():
            sel_apps = _get_selected_apps()
            if not sel_apps:
                messagebox.showinfo("提示", "请先在上方列表中选中至少一个业务设备")
                return

            # 严格权限校验：选中的所有设备必须全部是 Type=1
            invalid_apps = [a for a in sel_apps if not a.can_import_to_slave]
            if invalid_apps:
                invalid_names = "、".join([f"{a.chinese_name}(Type={a.app_type})" for a in invalid_apps])
                messagebox.showerror(
                    "权限受限",
                    f"业务规则拦截：\n只有数据库 app 表中 Type=1 (南向采集设备) 才能导入 Slave 模块！\n"
                    f"您选中的设备包含非 Type=1 的项目：\n{invalid_names}\n操作已被全部拦截。",
                )
                return

            avail_com_ports = get_available_com_ports()

            if len(sel_apps) == 1:
                # 单设备导入
                app_obj = sel_apps[0]
                points = app_obj.extract_studio_points()
                if not points:
                    messagebox.showwarning("无点位", f"设备 [{app_obj.chinese_name}] 中未配置有效点位")
                    return

                target_ip = app_obj.ip
                target_port = app_obj.port
                sid = 1
                if app_obj.pollings:
                    sid_str = app_obj.pollings[0].get("Slave Id")
                    if sid_str and str(sid_str).isdigit():
                        sid = int(sid_str)

                is_rtu = (app_obj.comm_type == "RTU")
                s_info = app_obj.serial_info
                s_bus = s_info.get("port_name", "RS-485(A1/B1)")
                s_baud = s_info.get("baudrate", 9600)
                s_parity = s_info.get("parity", "N")
                s_databit = s_info.get("databit", 8)
                s_stopbit = s_info.get("stopbit", 1)
                s_port = avail_com_ports[0] if avail_com_ports else "COM1"

                if is_rtu:
                    cfg_desc = f"• 通信方式: 串行端口 (Modbus RTU)\n• 硬件总线: {s_bus}\n• 波特率与校验: {s_baud}, 8-{s_parity}-{s_stopbit}\n• 推荐串口: {s_port}"
                    inst_default_name = f"从机[{app_obj.chinese_name}] ({s_bus})"
                else:
                    cfg_desc = f"• 通信方式: 以太网 (Modbus TCP)\n• 监听地址: {target_ip}:{target_port}"
                    inst_default_name = f"从机[{app_obj.chinese_name}] ({target_port})"

                resp = messagebox.askyesnocancel(
                    "导入从机模式选择",
                    f"检测到设备 [{app_obj.chinese_name}] (Type=1) 的配置：\n"
                    f"{cfg_desc}\n"
                    f"• 从机站号: {sid}\n"
                    f"• 点位数量: {len(points)} 个\n\n"
                    f"是否为其创建【独立的从机服务实例】？\n\n"
                    f"【是 (Yes)】：新建独立服务实例 (可与其它从机并发在不同 IP:Port 或不同串口监听)\n"
                    f"【否 (No)】：覆盖当前激活的从机服务配置与点表\n"
                    f"【取消 (Cancel)】：取消本次导入",
                )
                if resp is None:
                    return

                if resp:
                    unique_name = inst_default_name
                    idx = 1
                    while unique_name in self.slave_instances:
                        unique_name = f"{inst_default_name} (#{idx})"
                        idx += 1
                    new_inst = SlaveServiceInstance(
                        unique_name,
                        host=target_ip,
                        port=target_port,
                        slave_id=sid,
                        comm_type="RTU" if is_rtu else "TCP",
                        serial_port=s_port,
                        baudrate=s_baud,
                        bytesize=s_databit,
                        parity=s_parity,
                        stopbits=s_stopbit,
                        serial_bus_label=s_bus,
                    )
                    for p in points:
                        addr = p["address"]
                        new_inst.engine.points[addr] = p
                        new_inst.engine.write_typed_value(p["area"], addr, p["current_val"], p["data_type"], p["byte_order"])
                    self._register_slave_instance(new_inst)
                    self.current_slave_name = unique_name
                    self._update_slave_instance_combo()
                    self.log(f"已新建从机服务实例 [{unique_name}]，加载了 {len(points)} 个业务点位！")
                else:
                    inst = self.current_slave_inst
                    inst.comm_type = "RTU" if is_rtu else "TCP"
                    inst.slave_id = sid
                    inst.engine.comm_type = inst.comm_type
                    inst.engine.slave_id = sid
                    if is_rtu:
                        inst.serial_port = self.slave_serial_port_var.get().strip() or s_port
                        inst.baudrate = s_baud
                        inst.parity = s_parity
                        inst.bytesize = s_databit
                        inst.stopbits = s_stopbit
                        inst.serial_bus_label = s_bus
                        inst.engine.serial_port = inst.serial_port
                        inst.engine.baudrate = s_baud
                        inst.engine.parity = s_parity
                        self.slave_serial_port_var.set(inst.serial_port)
                        self.slave_baud_var.set(s_baud)
                        self.slave_parity_var.set("无校验 (None, N)" if s_parity == "N" else ("奇校验 (Odd, O)" if s_parity == "O" else "偶校验 (Even, E)"))
                        self.slave_bus_label_var.set(s_bus)
                    else:
                        inst.host = target_ip
                        inst.port = target_port
                        inst.engine.host = target_ip
                        inst.engine.port = target_port
                        self.slave_ip_var.set(target_ip)
                        self.slave_port_var.set(target_port)
                    self.slave_comm_type_var.set(inst.comm_type)
                    self.slave_id_var.set(sid)
                    inst.engine.points.clear()
                    for p in points:
                        addr = p["address"]
                        inst.engine.points[addr] = p
                        inst.engine.write_typed_value(p["area"], addr, p["current_val"], p["data_type"], p["byte_order"])
                    self._update_slave_conn_mode_ui()
                    self._update_slave_status_ui()
                    self.log(f"已更新从机服务 [{inst.name}]，加载了 {len(points)} 个业务点位！")
            else:
                # 多设备批量导入
                total_pts = sum(len(a.extract_studio_points()) for a in sel_apps)
                dev_summary = "\n".join([f"• [{a.chinese_name}] -> {a.comm_type} ({len(a.extract_studio_points())}点)" for a in sel_apps[:6]])
                if len(sel_apps) > 6:
                    dev_summary += f"\n... 等共计 {len(sel_apps)} 个设备"

                resp = messagebox.askyesnocancel(
                    "批量导入从机服务",
                    f"检测到您多选了 {len(sel_apps)} 个从机设备 (Type=1)，共 {total_pts} 个测点：\n\n"
                    f"{dev_summary}\n\n"
                    f"【是 (Yes)】：为每一个设备分别创建独立的从机服务实例 (推荐，支持独立监听不同网口或分配独立串口)\n"
                    f"【否 (No)】：将所有设备点位合并追加导入至当前激活的从机服务\n"
                    f"【取消 (Cancel)】：取消本次操作",
                )
                if resp is None:
                    return

                if resp:
                    created_count = 0
                    for a in sel_apps:
                        pts = a.extract_studio_points()
                        target_ip = a.ip
                        target_port = a.port
                        sid = 1
                        if a.pollings:
                            sid_str = a.pollings[0].get("Slave Id")
                            if sid_str and str(sid_str).isdigit():
                                sid = int(sid_str)

                        is_rtu = (a.comm_type == "RTU")
                        s_info = a.serial_info
                        s_bus = s_info.get("port_name", "RS-485(A1/B1)")
                        s_baud = s_info.get("baudrate", 9600)
                        s_parity = s_info.get("parity", "N")
                        s_databit = s_info.get("databit", 8)
                        s_stopbit = s_info.get("stopbit", 1)
                        s_port = avail_com_ports[created_count % len(avail_com_ports)] if avail_com_ports else f"COM{created_count + 1}"

                        inst_name = f"从机[{a.chinese_name}] ({s_bus if is_rtu else target_port})"
                        unique_name = inst_name
                        idx = 1
                        while unique_name in self.slave_instances:
                            unique_name = f"{inst_name} (#{idx})"
                            idx += 1

                        new_inst = SlaveServiceInstance(
                            unique_name,
                            host=target_ip,
                            port=target_port,
                            slave_id=sid,
                            comm_type="RTU" if is_rtu else "TCP",
                            serial_port=s_port,
                            baudrate=s_baud,
                            bytesize=s_databit,
                            parity=s_parity,
                            stopbits=s_stopbit,
                            serial_bus_label=s_bus,
                        )
                        for p in pts:
                            addr = p["address"]
                            new_inst.engine.points[addr] = p
                            new_inst.engine.write_typed_value(p["area"], addr, p["current_val"], p["data_type"], p["byte_order"])
                        self._register_slave_instance(new_inst)
                        created_count += 1
                        self.log(f"批量创建从机实例: [{unique_name}] -> 方式={new_inst.comm_type} (点位数: {len(pts)})")

                    self.current_slave_name = unique_name
                    self._update_slave_instance_combo()
                    messagebox.showinfo("批量导入成功", f"已成功为 {created_count} 个设备生成了独立的从机服务实例！\n您可在主界面下拉框中切换，或点击【全部启动】并发监听所有端口。")
                else:
                    inst = self.current_slave_inst
                    merged_pts = 0
                    for a in sel_apps:
                        for p in a.extract_studio_points():
                            addr = p["address"]
                            inst.engine.points[addr] = p
                            inst.engine.write_typed_value(p["area"], addr, p["current_val"], p["data_type"], p["byte_order"])
                            merged_pts += 1
                    self.log(f"已将 {len(sel_apps)} 个设备共 {merged_pts} 个测点合并导入至从机 [{inst.name}]")

            self._refresh_slave_tree()
            self.notebook.select(self.slave_frame)
            dlg.destroy()

        def _do_import_to_poll():
            sel_apps = _get_selected_apps()
            if not sel_apps:
                messagebox.showinfo("提示", "请先在上方列表中选中至少一个业务设备")
                return

            invalid_apps = [a for a in sel_apps if not a.can_import_to_poll]
            if invalid_apps:
                invalid_names = "、".join([f"{a.chinese_name}(Type={a.app_type})" for a in invalid_apps])
                messagebox.showerror(
                    "权限受限",
                    f"业务规则拦截：\n只有数据库 app 表中 Type=2 (北向通信应用) 才能导入 Poll 模块！\n"
                    f"您选中的设备包含非 Type=2 的项目：\n{invalid_names}\n操作已被拦截。",
                )
                return

            app_obj = sel_apps[0]
            if not app_obj.pollings:
                messagebox.showwarning("无轮询规则", f"应用 [{app_obj.chinese_name}] 中未配置 Pollings 规则")
                return

            rule = app_obj.pollings[0]
            sid = rule.get("Slave Id", "1")
            fc = rule.get("Function Code", "3")
            start = rule.get("Register Start Address", "0")
            num = rule.get("Register Number", "10")

            try:
                if app_obj.comm_type == "RTU":
                    self.poll_comm_type_var.set("RTU")
                    s_info = app_obj.serial_info
                    avail_ports = get_available_com_ports()
                    self.poll_serial_port_var.set(avail_ports[0] if avail_ports else "COM1")
                    self.poll_baud_var.set(s_info.get("baudrate", 9600))
                    p_code = s_info.get("parity", "N")
                    self.poll_parity_var.set("无校验 (None, N)" if p_code == "N" else ("奇校验 (Odd, O)" if p_code == "O" else "偶校验 (Even, E)"))
                    self._update_poll_conn_mode_ui()
                    self.log(f"已将串口应用 [{app_obj.chinese_name}] 导入至 Poll 主机: 总线={s_info.get('port_name')}, 波特率={s_info.get('baudrate')}, 站号={sid}, 功能码={fc}")
                else:
                    self.poll_comm_type_var.set("TCP")
                    self.poll_ip_var.set(app_obj.ip)
                    self.poll_port_var.set(app_obj.port)
                    self._update_poll_conn_mode_ui()
                    self.log(f"已将以太网应用 [{app_obj.chinese_name}] 导入至 Poll 主机: 目标={app_obj.ip}:{app_obj.port}, 站号={sid}, 功能码={fc}")

                self.poll_id_var.set(int(sid))
                self.poll_start_var.set(int(start))
                self.poll_count_var.set(int(num))
                area_val = AreaType.HOLDING_REGISTER if str(fc) in ("3", "16") else AreaType.INPUT_REGISTER
                self.poll_area_var.set(area_val)
                self.notebook.select(self.poll_frame)
                if len(sel_apps) > 1:
                    messagebox.showinfo("提示", f"已成功将首个选中应用 [{app_obj.chinese_name}] 的轮询规则应用至 Poll 主机！")
                dlg.destroy()
            except Exception as ex:
                messagebox.showerror("错误", f"解析轮询规则失败: {ex}")

        btn_slave = ttk.Button(btn_row, text="📥 导入为 Slave 从机仿真服务", command=_do_import_to_slave, state="disabled")
        btn_slave.pack(side=tk.LEFT, padx=6)

        btn_poll = ttk.Button(btn_row, text="📡 导入为 Poll 主机轮询目标", command=_do_import_to_poll, state="disabled")
        btn_poll.pack(side=tk.LEFT, padx=6)

        ttk.Button(btn_row, text="关闭", command=dlg.destroy).pack(side=tk.RIGHT, padx=6)

        def _on_tree_select(event=None):
            sel_apps = _get_selected_apps()
            if not sel_apps:
                btn_slave.config(state="disabled", text="📥 导入为 Slave 从机仿真服务")
                btn_poll.config(state="disabled", text="📡 导入为 Poll 主机轮询目标")
                lbl_perm_hint.config(text="💡 请在上方列表中勾选设备（支持 Ctrl/Shift 多选，或使用上方一键全选）...", foreground="#555555")
                return

            types = set(a.app_type for a in sel_apps)
            if types == {1}:
                btn_slave.config(state="normal", text=f"📥 批量导入为 Slave 从机服务 ({len(sel_apps)} 个设备)")
                btn_poll.config(state="disabled", text="📡 导入为 Poll 主机轮询目标")
                names_str = "、".join([a.chinese_name for a in sel_apps[:3]])
                if len(sel_apps) > 3:
                    names_str += f" 等 {len(sel_apps)} 个设备"
                lbl_perm_hint.config(
                    text=f"✅ 已选中 {len(sel_apps)} 个 Slave 从机设备 [{names_str}] -> 支持一键生成各自专属 IP:Port 从机服务实例！",
                    foreground="green",
                )
            elif types == {2}:
                btn_slave.config(state="disabled", text="📥 导入为 Slave 从机仿真服务")
                btn_poll.config(state="normal", text=f"📡 批量导入为 Poll 主机目标 ({len(sel_apps)} 个应用)")
                names_str = "、".join([a.chinese_name for a in sel_apps[:3]])
                if len(sel_apps) > 3:
                    names_str += f" 等 {len(sel_apps)} 个应用"
                lbl_perm_hint.config(
                    text=f"✅ 已选中 {len(sel_apps)} 个 Poll 主机应用 [{names_str}] -> 支持一键导入其轮询配置与点位！",
                    foreground="#0066cc",
                )
            else:
                btn_slave.config(state="disabled", text="📥 导入为 Slave 从机仿真服务")
                btn_poll.config(state="disabled", text="📡 导入为 Poll 主机轮询目标")
                lbl_perm_hint.config(
                    text=f"⚠️ 规则拦截：当前选中的设备中包含了不同类型 (类型集合: {types})，系统要求每次只能导入同一类型 (全部为 Slave 或全部为 Poll)！",
                    foreground="red",
                )

        tree.bind("<<TreeviewSelect>>", _on_tree_select)

    def _open_pairing_dialog(self):
        """打开基于 app.More 配置的业务配对中心."""
        rules = self.pairing_engine.rules
        if not rules:
            ret = messagebox.askyesno(
                "提示",
                f"当前数据库 [{self.hems_db.db_path}] 中未解析到配对规则。\n是否立即浏览选择其他业务数据库文件？",
            )
            if ret:
                self._select_database_file(on_success_callback=self._open_pairing_dialog)
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"业务配对中心 (基于 app.More 自动配对 & 批量变位) - [{os.path.basename(self.hems_db.db_path)}]")
        dlg.geometry("980x600")
        dlg.transient(self.root)
        dlg.grab_set()

        top_desc = ttk.Frame(dlg, padding=8)
        top_desc.pack(fill=tk.X)

        title_line = ttk.Frame(top_desc)
        title_line.pack(fill=tk.X)

        ttk.Label(
            title_line,
            text=f"⚡ 智能业务配对引擎 | 当前库: {os.path.basename(self.hems_db.db_path)} | 共识别到 {len(rules)} 条配对规则",
            font=("Microsoft YaHei", 9, "bold"),
            foreground="#0066cc",
        ).pack(side=tk.LEFT)

        def _change_db_in_pairing():
            dlg.destroy()
            self._select_database_file(on_success_callback=self._open_pairing_dialog)

        ttk.Button(
            title_line,
            text="📂 浏览更换数据库...",
            command=_change_db_in_pairing,
        ).pack(side=tk.RIGHT, padx=4)

        ttk.Label(
            top_desc,
            text="系统已自动根据 app.More 配置完成配对映射！支持多选配对点位，一键批量修改变位模式，或一键应用端到端仿真：",
        ).pack(anchor=tk.W, pady=2)

        # 批量操作条
        batch_bar = ttk.LabelFrame(dlg, text=" 批量修改选中配对项的变位模式 (支持按住 Ctrl / Shift 多选) ", padding=4)
        batch_bar.pack(fill=tk.X, padx=8, pady=2)

        ttk.Button(batch_bar, text="☑️ 全选", command=lambda: p_tree.selection_set(p_tree.get_children())).pack(side=tk.LEFT, padx=3)
        ttk.Button(batch_bar, text="⬜ 清空选择", command=lambda: p_tree.selection_remove(p_tree.selection())).pack(side=tk.LEFT, padx=3)

        ttk.Label(batch_bar, text="目标变位:").pack(side=tk.LEFT, padx=6)
        batch_p_order_var = tk.StringVar(value=ByteOrderMode.CDAB.value)
        p_order_cb = ttk.Combobox(batch_bar, textvariable=batch_p_order_var, width=8, state="readonly")
        p_order_cb["values"] = [m.value for m in ByteOrderMode]
        p_order_cb.pack(side=tk.LEFT, padx=2)

        def _batch_apply_pairing_order():
            sel = p_tree.selection()
            if not sel:
                messagebox.showinfo("提示", "请先在下方表格中选中要修改变位的配对项")
                return
            new_m = ByteOrderMode(batch_p_order_var.get())
            count = 0
            for item_id in sel:
                idx = int(p_tree.item(item_id)["values"][0]) - 1
                if 0 <= idx < len(rules):
                    rules[idx].byte_order = new_m
                    count += 1
            _refresh_pairing_tree()
            self.log(f"配对中心：已将 {count} 条配对规则的变位模式批量修改为: {new_m.value}")

        ttk.Button(batch_bar, text="⚡ 批量应用变位到选中项", command=_batch_apply_pairing_order).pack(side=tk.LEFT, padx=6)

        # 表格
        table_frame = ttk.Frame(dlg, padding=8)
        table_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("idx", "src_dev", "src_var", "src_addr", "target_app", "target_reg", "fc", "slave_id", "type", "order")
        p_tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended")

        p_tree.heading("idx", text="#")
        p_tree.heading("src_dev", text="源设备 (南向)")
        p_tree.heading("src_var", text="源物理点位名")
        p_tree.heading("src_addr", text="源地址")
        p_tree.heading("target_app", text="目标应用 (北向)")
        p_tree.heading("target_reg", text="映射北向寄存器")
        p_tree.heading("fc", text="功能码")
        p_tree.heading("slave_id", text="站号")
        p_tree.heading("type", text="数据类型")
        p_tree.heading("order", text="变位模式")

        p_tree.column("idx", width=40, anchor=tk.CENTER)
        p_tree.column("src_dev", width=120, anchor=tk.W)
        p_tree.column("src_var", width=170, anchor=tk.W)
        p_tree.column("src_addr", width=65, anchor=tk.CENTER)
        p_tree.column("target_app", width=110, anchor=tk.W)
        p_tree.column("target_reg", width=95, anchor=tk.CENTER)
        p_tree.column("fc", width=55, anchor=tk.CENTER)
        p_tree.column("slave_id", width=50, anchor=tk.CENTER)
        p_tree.column("type", width=80, anchor=tk.CENTER)
        p_tree.column("order", width=75, anchor=tk.CENTER)

        def _refresh_pairing_tree():
            p_tree.delete(*p_tree.get_children())
            for i, r in enumerate(rules, 1):
                p_tree.insert(
                    "",
                    tk.END,
                    values=(
                        i,
                        r.src_app_name,
                        r.src_var_name,
                        r.src_address if r.src_address is not None else "-",
                        r.target_app_name,
                        r.target_register,
                        r.target_fc,
                        r.target_slave_id,
                        r.data_type.value,
                        r.byte_order.value,
                    ),
                )

        _refresh_pairing_tree()

        scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=p_tree.yview)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        p_tree.config(yscrollcommand=scroll_y.set)
        p_tree.pack(fill=tk.BOTH, expand=True)

        # 底部操作栏
        btn_bar = ttk.Frame(dlg, padding=10)
        btn_bar.pack(fill=tk.X)

        def _apply_pairing_to_simulation():
            """一键将配对拓扑加载到系统仿真中 (Slave 载入北向点表)."""
            self.slave_engine.points.clear()
            for r in rules:
                area = AreaType.INPUT_REGISTER if r.target_fc == 4 else AreaType.HOLDING_REGISTER
                desc = f"{r.src_app_name}|{r.src_var_name}"
                self.slave_engine.points[r.target_register] = {
                    "address": r.target_register,
                    "desc": desc,
                    "area": area,
                    "data_type": r.data_type,
                    "byte_order": r.byte_order,
                    "current_val": 0,
                    "sim_mode": "随机波动",
                }
                self.slave_engine.write_typed_value(
                    area, r.target_register, 0, r.data_type, r.byte_order
                )

            self.slave_id_var.set(rules[0].target_slave_id)
            self._refresh_slave_tree()
            self.log(f"已根据配对关系自动将 {len(rules)} 个北向映射点表注入 Slave 从机模拟器！")
            messagebox.showinfo("应用成功", f"已成功将 {len(rules)} 个配对点表加载到 Slave 从机模拟器中！")
            dlg.destroy()

        def _export_pairing_report():
            summary = self.pairing_engine.get_summary_text()
            file_path = filedialog.asksaveasfilename(
                title="导出配对关系报表",
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("CSV files", "*.csv"), ("All files", "*.*")],
                initialfile="hems_pairing_report.txt",
            )
            if file_path:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(summary)
                self.log(f"配对报表已成功导出至: {file_path}")
                messagebox.showinfo("导出成功", f"配对报表已保存至: {file_path}")

        ttk.Button(btn_bar, text="⚡ 一键将配对拓扑应用到系统 (自动配置 Slave)", command=_apply_pairing_to_simulation).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_bar, text="📋 导出配对关系报表 (TXT/CSV)", command=_export_pairing_report).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_bar, text="关闭", command=dlg.destroy).pack(side=tk.RIGHT, padx=6)

    # -----------------------------------------------------------------
    # 右键快捷菜单与批量操作
    # -----------------------------------------------------------------
    def _build_slave_context_menu(self):
        self.slave_menu = tk.Menu(self.root, tearoff=0)
        order_submenu = tk.Menu(self.slave_menu, tearoff=0)
        for m in [ByteOrderMode.ABCD, ByteOrderMode.CDAB, ByteOrderMode.BADC, ByteOrderMode.DCBA]:
            order_submenu.add_command(
                label=f"设为 {m.value}",
                command=lambda mode=m.value: self._quick_set_batch_order(mode),
            )
        self.slave_menu.add_cascade(label="⚡ 批量修改变位模式", menu=order_submenu)

        type_submenu = tk.Menu(self.slave_menu, tearoff=0)
        for t in [ModbusDataType.FLOAT32, ModbusDataType.INT32, ModbusDataType.UINT32, ModbusDataType.INT16, ModbusDataType.DOUBLE64]:
            type_submenu.add_command(
                label=f"设为 {t.value}",
                command=lambda dt=t.value: self._quick_set_batch_type(dt),
            )
        self.slave_menu.add_cascade(label="批量修改数据类型", menu=type_submenu)
        self.slave_menu.add_separator()
        self.slave_menu.add_command(label="🗑️ 删除选中的点位", command=self._delete_slave_point)

    def _show_slave_context_menu(self, event):
        item = self.slave_tree.identify_row(event.y)
        if item:
            if item not in self.slave_tree.selection():
                self.slave_tree.selection_set(item)
            self.slave_menu.post(event.x_root, event.y_root)

    def _open_batch_generate_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("批量规则点位生成器")
        dlg.geometry("450x420")
        dlg.transient(self.root)
        dlg.grab_set()

        form = ttk.Frame(dlg, padding=16)
        form.pack(fill=tk.BOTH, expand=True)

        ttk.Label(form, text="💡 输入轮询规则，自动批量输出对应点位：", font=("Microsoft YaHei", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=6
        )

        ttk.Label(form, text="存储区域:").grid(row=1, column=0, sticky=tk.W, pady=4)
        area_var = tk.StringVar(value=AreaType.HOLDING_REGISTER)
        area_cb = ttk.Combobox(form, textvariable=area_var, state="readonly", width=24)
        area_cb["values"] = [
            AreaType.HOLDING_REGISTER,
            AreaType.INPUT_REGISTER,
            AreaType.COIL,
            AreaType.DISCRETE_INPUT,
        ]
        area_cb.grid(row=1, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text="起始地址:").grid(row=2, column=0, sticky=tk.W, pady=4)
        start_addr_var = tk.IntVar(value=0)
        ttk.Entry(form, textvariable=start_addr_var, width=12).grid(row=2, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text="生成点位数量:").grid(row=3, column=0, sticky=tk.W, pady=4)
        count_var = tk.IntVar(value=10)
        ttk.Entry(form, textvariable=count_var, width=12).grid(row=3, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text="数据类型:").grid(row=4, column=0, sticky=tk.W, pady=4)
        dtype_var = tk.StringVar(value=ModbusDataType.FLOAT32.value)
        dtype_cb = ttk.Combobox(form, textvariable=dtype_var, state="readonly", width=18)
        dtype_cb["values"] = [t.value for t in ModbusDataType]
        dtype_cb.grid(row=4, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text="默认变位模式:").grid(row=5, column=0, sticky=tk.W, pady=4)
        order_var = tk.StringVar(value=ByteOrderMode.CDAB.value)
        order_cb = ttk.Combobox(form, textvariable=order_var, state="readonly", width=12)
        order_cb["values"] = [m.value for m in ByteOrderMode]
        order_cb.grid(row=5, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text="点位命名前缀:").grid(row=6, column=0, sticky=tk.W, pady=4)
        prefix_var = tk.StringVar(value="测点通道_")
        ttk.Entry(form, textvariable=prefix_var, width=20).grid(row=6, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text="初始基准值:").grid(row=7, column=0, sticky=tk.W, pady=4)
        init_val_var = tk.StringVar(value="20.0")
        ttk.Entry(form, textvariable=init_val_var, width=12).grid(row=7, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text="动态模拟规则:").grid(row=8, column=0, sticky=tk.W, pady=4)
        sim_rule_var = tk.StringVar(value="随机波动")
        sim_cb = ttk.Combobox(form, textvariable=sim_rule_var, state="readonly", width=12)
        sim_cb["values"] = ["固定", "随机波动", "累加递增", "正弦波"]
        sim_cb.grid(row=8, column=1, sticky=tk.W, pady=4)

        replace_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="清空现有点表 (不勾选则在末尾追加)", variable=replace_var).grid(
            row=9, column=0, columnspan=2, sticky=tk.W, pady=6
        )

        def _do_generate():
            try:
                area = area_var.get()
                start_addr = start_addr_var.get()
                total_count = count_var.get()
                dtype = ModbusDataType(dtype_var.get())
                order = ByteOrderMode(order_var.get())
                prefix = prefix_var.get().strip()
                sim_rule = sim_rule_var.get()
                base_val = init_val_var.get().strip()

                if total_count <= 0 or total_count > 500:
                    messagebox.showerror("错误", "点位数量请在 1 ~ 500 之间")
                    return

                step = 1 if (AreaType.COIL in area or AreaType.DISCRETE_INPUT in area) else TYPE_REGISTER_COUNT.get(dtype, 1)

                if replace_var.get():
                    self.slave_engine.points.clear()

                curr_addr = start_addr
                for i in range(1, total_count + 1):
                    name = f"{prefix}{i:02d}"
                    if dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64):
                        val = round(float(base_val) + (i - 1) * 0.5, 2)
                    elif dtype == ModbusDataType.BOOL:
                        val = bool((i - 1) % 2)
                    else:
                        val = int(float(base_val)) + (i - 1)

                    self.slave_engine.points[curr_addr] = {
                        "address": curr_addr,
                        "desc": name,
                        "area": area,
                        "data_type": dtype,
                        "byte_order": order,
                        "current_val": val,
                        "sim_mode": sim_rule,
                    }
                    self.slave_engine.write_typed_value(area, curr_addr, val, dtype, order)
                    curr_addr += step

                self._refresh_slave_tree()
                self.log(f"成功批量生成 {total_count} 个点位 (起始地址: {start_addr}, 类型: {dtype.value}, 变位: {order.value})")
                dlg.destroy()
            except Exception as e:
                messagebox.showerror("生成失败", f"错误: {e}")

        btn_box = ttk.Frame(dlg)
        btn_box.pack(pady=10)
        ttk.Button(btn_box, text="⚡ 立即批量生成", command=_do_generate).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_box, text="取消", command=dlg.destroy).pack(side=tk.LEFT, padx=10)

    def _get_selected_addresses(self) -> List[int]:
        sel = self.slave_tree.selection()
        addrs = []
        for item_id in sel:
            item = self.slave_tree.item(item_id)
            if item and item.get("values"):
                addrs.append(int(item["values"][0]))
        return addrs

    def _select_all_slave_points(self):
        self.slave_tree.selection_set(self.slave_tree.get_children())

    def _deselect_all_slave_points(self):
        self.slave_tree.selection_remove(self.slave_tree.selection())

    def _quick_set_batch_order(self, mode_str: str):
        self.batch_order_var.set(mode_str)
        self._apply_batch_order()

    def _quick_set_batch_type(self, type_str: str):
        self.batch_type_var.set(type_str)
        self._apply_batch_type()

    def _apply_batch_order(self):
        new_order = ByteOrderMode(self.batch_order_var.get())
        scope = self.batch_scope_var.get() if hasattr(self, "batch_scope_var") else "current"

        if scope == "all":
            total_pts = 0
            for inst in self.slave_instances.values():
                for addr, p in inst.engine.points.items():
                    p["byte_order"] = new_order
                    inst.engine.write_typed_value(
                        p["area"], addr, p["current_val"], p["data_type"], new_order
                    )
                    total_pts += 1
            self._refresh_slave_tree()
            self.log(f"已批量将全部 {len(self.slave_instances)} 个从机服务的共 {total_pts} 个点位变位模式修改为: {new_order.value}")
            messagebox.showinfo("批量设置成功", f"已成功将全部 {len(self.slave_instances)} 个从机服务的共 {total_pts} 个点位变位模式修改为：【{new_order.value}】！")
            return

        addrs = self._get_selected_addresses()
        if not addrs:
            if messagebox.askyesno("批量修改提示", f"当前未选中具体点位行。\n是否将变位模式【{new_order.value}】应用到【当前从机】的全部点位？"):
                addrs = list(self.slave_engine.points.keys())
            else:
                return

        modified_count = 0
        for addr in addrs:
            p = self.slave_engine.points.get(addr)
            if p:
                p["byte_order"] = new_order
                self.slave_engine.write_typed_value(
                    p["area"], addr, p["current_val"], p["data_type"], new_order
                )
                modified_count += 1

        self._refresh_slave_tree()
        self.log(f"已批量将当前从机 {modified_count} 个点位的变位模式修改为: {new_order.value}")

    def _apply_batch_type(self):
        new_type = ModbusDataType(self.batch_type_var.get())
        scope = self.batch_scope_var.get() if hasattr(self, "batch_scope_var") else "current"

        if scope == "all":
            total_pts = 0
            for inst in self.slave_instances.values():
                for addr, p in inst.engine.points.items():
                    p["data_type"] = new_type
                    inst.engine.write_typed_value(
                        p["area"], addr, p["current_val"], new_type, p["byte_order"]
                    )
                    total_pts += 1
            self._refresh_slave_tree()
            self.log(f"已批量将全部 {len(self.slave_instances)} 个从机服务的共 {total_pts} 个点位数据类型修改为: {new_type.value}")
            messagebox.showinfo("批量设置成功", f"已成功将全部 {len(self.slave_instances)} 个从机服务的共 {total_pts} 个点位数据类型修改为：【{new_type.value}】！")
            return

        addrs = self._get_selected_addresses()
        if not addrs:
            if messagebox.askyesno("批量修改提示", f"当前未选中具体点位行。\n是否将数据类型【{new_type.value}】应用到【当前从机】的全部点位？"):
                addrs = list(self.slave_engine.points.keys())
            else:
                return

        for addr in addrs:
            p = self.slave_engine.points.get(addr)
            if p:
                p["data_type"] = new_type
                self.slave_engine.write_typed_value(
                    p["area"], addr, p["current_val"], new_type, p["byte_order"]
                )

        self._refresh_slave_tree()
        self.log(f"已批量将当前从机 {len(addrs)} 个点位的数据类型修改为: {new_type.value}")

    def _apply_batch_value(self):
        raw_str = self.batch_val_var.get().strip()
        scope = self.batch_scope_var.get() if hasattr(self, "batch_scope_var") else "current"

        try:
            if scope == "all":
                total_pts = 0
                for inst in self.slave_instances.values():
                    for addr, p in inst.engine.points.items():
                        dtype = p["data_type"]
                        if dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64):
                            val = float(raw_str)
                        elif dtype == ModbusDataType.BOOL:
                            val = raw_str.lower() in ("true", "1", "yes", "on")
                        elif dtype in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
                            val = raw_str
                        else:
                            val = int(raw_str)

                        p["current_val"] = val
                        inst.engine.write_typed_value(
                            p["area"], addr, val, dtype, p["byte_order"]
                        )
                        total_pts += 1
                self._refresh_slave_tree()
                self.log(f"已批量将全部 {len(self.slave_instances)} 个从机服务的共 {total_pts} 个点位值统一修改为: {raw_str}")
                messagebox.showinfo("批量设置成功", f"已成功将全部 {len(self.slave_instances)} 个从机服务的共 {total_pts} 个点位值修改为：【{raw_str}】！")
                return

            addrs = self._get_selected_addresses()
            if not addrs:
                if messagebox.askyesno("批量修改提示", f"当前未选中具体点位行。\n是否将统一值【{raw_str}】应用到【当前从机】的全部点位？"):
                    addrs = list(self.slave_engine.points.keys())
                else:
                    return

            for addr in addrs:
                p = self.slave_engine.points.get(addr)
                if p:
                    dtype = p["data_type"]
                    if dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64):
                        val = float(raw_str)
                    elif dtype == ModbusDataType.BOOL:
                        val = raw_str.lower() in ("true", "1", "yes", "on")
                    elif dtype in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
                        val = raw_str
                    else:
                        val = int(raw_str)

                    p["current_val"] = val
                    self.slave_engine.write_typed_value(
                        p["area"], addr, val, dtype, p["byte_order"]
                    )
            self._refresh_slave_tree()
            self.log(f"已批量修改当前从机 {len(addrs)} 个点位的值为: {raw_str}")
        except Exception as e:
            messagebox.showerror("格式错误", f"输入数值格式有误: {e}")

    def _apply_batch_sim(self):
        rule = self.batch_sim_var.get()
        scope = self.batch_scope_var.get() if hasattr(self, "batch_scope_var") else "current"

        if scope == "all":
            total_pts = 0
            inst_count = len(self.slave_instances)
            for inst in self.slave_instances.values():
                for p in inst.engine.points.values():
                    p["sim_mode"] = rule
                    total_pts += 1
            self._refresh_slave_tree()
            self.log(f"已将全部 {inst_count} 个从机服务的共 {total_pts} 个点位模拟规则批量修改为: {rule}")
            messagebox.showinfo("批量设置成功", f"已成功将全部 {inst_count} 个从机服务的所有点位（共 {total_pts} 个）模拟规则修改为：【{rule}】！")
            return

        addrs = self._get_selected_addresses()
        if not addrs:
            if messagebox.askyesno("批量修改提示", f"当前从机未选中具体点位行。\n是否将模拟规则【{rule}】应用到【当前从机】的全部点位？"):
                addrs = list(self.slave_engine.points.keys())
            else:
                return

        for addr in addrs:
            p = self.slave_engine.points.get(addr)
            if p:
                p["sim_mode"] = rule
        self._refresh_slave_tree()
        self.log(f"已批量将当前从机（{self.current_slave_name}）的 {len(addrs)} 个点位模拟规则修改为: {rule}")

    def _add_or_update_slave_point(self):
        try:
            addr_str = str(self.new_addr_var.get()).strip()
            if not addr_str:
                messagebox.showwarning("提示", "请输入点位起始地址！")
                return
            addr = int(addr_str)
            desc = self.new_desc_var.get().strip() or f"点位_{addr}"
            area = self.new_area_var.get()
            dtype = ModbusDataType(self.new_type_var.get())
            order = ByteOrderMode(self.new_order_var.get())
            val_str = self.new_val_var.get().strip()
            if not val_str:
                val_str = "0"
            sim = self.new_sim_var.get()

            if dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64):
                val = float(val_str)
            elif dtype == ModbusDataType.BOOL:
                val = val_str.lower() in ("true", "1", "yes", "on")
            elif dtype in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
                val = val_str
            else:
                val = int(val_str)

            self.slave_engine.points[addr] = {
                "address": addr,
                "desc": desc,
                "area": area,
                "data_type": dtype,
                "byte_order": order,
                "current_val": val,
                "sim_mode": sim,
            }
            self.slave_engine.write_typed_value(area, addr, val, dtype, order)
            self._refresh_slave_tree()
            self.log(f"单点添加/更新成功: 地址={addr}, 类型={dtype.value}, 变位={order.value}, 值={val}")
        except Exception as e:
            messagebox.showerror("输入错误", f"参数有误: {e}")

    def _delete_slave_point(self):
        addrs = self._get_selected_addresses()
        if not addrs:
            return
        for addr in addrs:
            if addr in self.slave_engine.points:
                del self.slave_engine.points[addr]
        self._refresh_slave_tree()
        self.log(f"已批量删除 {len(addrs)} 个点位")

    def _on_slave_double_click(self, event):
        item = self.slave_tree.identify_row(event.y)
        if not item:
            return
        addr = int(self.slave_tree.item(item)["values"][0])
        p = self.slave_engine.points.get(addr)
        if not p:
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"修改点位值 (地址: {addr} - {p['desc']})")
        dialog.geometry("340x160")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text=f"类型: {p['data_type'].value} | 变位: {p['byte_order'].value}").pack(pady=8)
        ttk.Label(dialog, text="请输入新值:").pack(anchor=tk.W, padx=20)
        entry_val = ttk.Entry(dialog, width=28)
        entry_val.pack(padx=20, pady=4)
        entry_val.insert(0, str(p["current_val"]))
        entry_val.focus()

        def _do_save():
            new_text = entry_val.get().strip()
            try:
                dtype = p["data_type"]
                if dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64):
                    new_val = float(new_text)
                elif dtype == ModbusDataType.BOOL:
                    new_val = new_text.lower() in ("true", "1", "yes", "on")
                elif dtype in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
                    new_val = new_text
                else:
                    new_val = int(new_text)

                p["current_val"] = new_val
                p["sim_mode"] = "固定"
                # 强类型安全钳位
                safe_val = clamp_value_to_type(new_val, dtype)
                p["current_val"] = safe_val
                self.slave_engine.write_typed_value(p["area"], addr, safe_val, dtype, p["byte_order"])
                self._refresh_slave_tree()
                self.log(f"手动修改点位 {addr} 成功: {safe_val}")
                dialog.destroy()
            except Exception as ex:
                messagebox.showerror("格式错误", f"无效数值: {ex}")

        btn_box = ttk.Frame(dialog)
        btn_box.pack(pady=10)
        ttk.Button(btn_box, text="确定", command=_do_save).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_box, text="取消", command=dialog.destroy).pack(side=tk.LEFT, padx=6)

    def _refresh_slave_tree(self):
        selected_addrs = set(self._get_selected_addresses())
        self.slave_tree.delete(*self.slave_tree.get_children())

        for addr in sorted(self.slave_engine.points.keys()):
            p = self.slave_engine.points[addr]
            area = p.get("area", AreaType.HOLDING_REGISTER)
            dtype = p.get("data_type", ModbusDataType.INT16)
            order = p.get("byte_order", ByteOrderMode.ABCD)
            cnt = TYPE_REGISTER_COUNT.get(dtype, 1)

            raw = self.slave_engine.read_raw_values(area, addr, cnt)
            if AreaType.COIL in area or AreaType.DISCRETE_INPUT in area:
                val = bool(raw[0])
                hex_str = "0x01" if val else "0x00"
            else:
                val = decode_value(raw, dtype, order)
                hex_str = " ".join([f"0x{r:04X}" for r in raw])

            item_id = self.slave_tree.insert(
                "",
                tk.END,
                values=(
                    addr,
                    p.get("desc", f"点位_{addr}"),
                    area.split()[0],
                    dtype.value,
                    order.value,
                    val,
                    hex_str,
                    p.get("sim_mode", "固定"),
                    cnt,
                ),
            )
            if addr in selected_addrs:
                self.slave_tree.selection_add(item_id)

    def _load_default_slave_points(self):
        """图四需求：启动时默认点表完全置空."""
        self.slave_engine.points.clear()
        self._refresh_slave_tree()

    def _toggle_slave_server(self):
        inst = self.current_slave_inst
        if not inst.engine.is_running:
            inst.comm_type = self.slave_comm_type_var.get()
            inst.engine.comm_type = inst.comm_type
            inst.slave_id = self.slave_id_var.get()
            inst.engine.slave_id = inst.slave_id

            if inst.comm_type == "RTU":
                inst.serial_port = self.slave_serial_port_var.get().strip() or "COM1"
                inst.baudrate = int(self.slave_baud_var.get())
                p_raw = self.slave_parity_var.get().strip().upper()
                inst.parity = p_raw[0] if p_raw else "N"
                inst.serial_bus_label = self.slave_bus_label_var.get().strip()
                inst.engine.serial_port = inst.serial_port
                inst.engine.baudrate = inst.baudrate
                inst.engine.bytesize = 8
                inst.engine.parity = inst.parity
                inst.engine.stopbits = 1
                try:
                    inst.engine.start()
                    self._update_slave_status_ui()
                    self.log(
                        f"串口从机服务 [{inst.name}] 已成功启动，监听于 {inst.serial_port} "
                        f"[{inst.serial_bus_label}] (波特率: {inst.baudrate}, 8-{inst.parity}-1, Slave ID: {inst.slave_id})"
                    )
                except Exception as e:
                    messagebox.showerror("启动失败", f"无法启动串口从机服务 [{inst.name}] ({inst.serial_port}):\n{e}")
            else:
                inst.host = self.slave_ip_var.get().strip() or "0.0.0.0"
                inst.port = self.slave_port_var.get()
                inst.engine.host = inst.host
                inst.engine.port = inst.port
                try:
                    inst.engine.start()
                    self._update_slave_status_ui()
                    self.log(
                        f"以太网从机服务 [{inst.name}] 已成功启动，监听于 {inst.host}:{inst.port} (Slave ID: {inst.slave_id})"
                    )
                except Exception as e:
                    messagebox.showerror("启动失败", f"无法启动从机服务 [{inst.name}]: {e}")
        else:
            inst.engine.stop()
            self._update_slave_status_ui()
            self.log(f"从机服务 [{inst.name}] 已停止监听。")

    # =================================================================
    # Tab 2: Poll 主机调试器 UI
    # =================================================================
    def _build_poll_tab(self, parent: ttk.Frame):
        top_bar = ttk.LabelFrame(parent, text=" 目标从机与轮询参数 (支持以太网 TCP 与 串行端口 RTU 485/232) ")
        top_bar.pack(fill=tk.X, padx=6, pady=3)

        # 第 0 行：通讯方式单选 + 动态参数容器 + 启停操作
        cfg_row = ttk.Frame(top_bar)
        cfg_row.pack(fill=tk.X, padx=2, pady=3)

        ttk.Label(cfg_row, text="通讯方式:").pack(side=tk.LEFT, padx=(4, 2))
        self.poll_comm_type_var = tk.StringVar(value="TCP")
        ttk.Radiobutton(
            cfg_row,
            text="以太网 (TCP)",
            value="TCP",
            variable=self.poll_comm_type_var,
            command=self._on_poll_comm_type_change,
        ).pack(side=tk.LEFT, padx=3)
        ttk.Radiobutton(
            cfg_row,
            text="串行端口 (RTU / 485 / 232)",
            value="RTU",
            variable=self.poll_comm_type_var,
            command=self._on_poll_comm_type_change,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Separator(cfg_row, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=2)

        # TCP 参数子容器
        self.poll_tcp_frame = ttk.Frame(cfg_row)
        ttk.Label(self.poll_tcp_frame, text="目标 IP:").pack(side=tk.LEFT, padx=(2, 2))
        self.poll_ip_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(self.poll_tcp_frame, textvariable=self.poll_ip_var, width=12).pack(side=tk.LEFT, padx=2)

        ttk.Label(self.poll_tcp_frame, text="端口:").pack(side=tk.LEFT, padx=(6, 2))
        self.poll_port_var = tk.IntVar(value=5020)
        ttk.Entry(self.poll_tcp_frame, textvariable=self.poll_port_var, width=7).pack(side=tk.LEFT, padx=2)

        # RTU 参数子容器
        self.poll_rtu_frame = ttk.Frame(cfg_row)
        ttk.Label(self.poll_rtu_frame, text="串口 (COM):").pack(side=tk.LEFT, padx=(2, 2))
        self.poll_serial_port_var = tk.StringVar(value="COM1")
        self.poll_com_combo = ttk.Combobox(
            self.poll_rtu_frame,
            textvariable=self.poll_serial_port_var,
            width=8,
            values=get_available_com_ports(),
        )
        self.poll_com_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(
            self.poll_rtu_frame,
            text="🔄",
            width=3,
            command=self._refresh_poll_com_ports,
        ).pack(side=tk.LEFT, padx=1)

        ttk.Label(self.poll_rtu_frame, text="波特率:").pack(side=tk.LEFT, padx=(6, 2))
        self.poll_baud_var = tk.IntVar(value=9600)
        self.poll_baud_combo = ttk.Combobox(
            self.poll_rtu_frame,
            textvariable=self.poll_baud_var,
            width=7,
            state="readonly",
            values=AVAILABLE_BAUDRATES,
        )
        self.poll_baud_combo.pack(side=tk.LEFT, padx=2)

        ttk.Label(self.poll_rtu_frame, text="校验:").pack(side=tk.LEFT, padx=(6, 2))
        self.poll_parity_var = tk.StringVar(value="N (无校验)")
        self.poll_parity_combo = ttk.Combobox(
            self.poll_rtu_frame,
            textvariable=self.poll_parity_var,
            width=10,
            state="readonly",
            values=AVAILABLE_PARITIES,
        )
        self.poll_parity_combo.pack(side=tk.LEFT, padx=2)

        # 默认先打包 TCP
        self.poll_tcp_frame.pack(side=tk.LEFT)

        # 公共参数：站号 ID
        self.poll_common_frame = ttk.Frame(cfg_row)
        self.poll_common_frame.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(self.poll_common_frame, text="站号 ID:").pack(side=tk.LEFT, padx=(2, 2))
        self.poll_id_var = tk.IntVar(value=1)
        ttk.Entry(self.poll_common_frame, textvariable=self.poll_id_var, width=4).pack(side=tk.LEFT, padx=2)

        # 右侧操作区
        poll_action_box = ttk.Frame(cfg_row)
        poll_action_box.pack(side=tk.RIGHT, padx=4)

        ttk.Button(
            poll_action_box,
            text="📂 导入业务设备轮询规则...",
            command=self._open_import_db_dialog,
        ).pack(side=tk.RIGHT, padx=4)

        self.poll_stat_lbl = ttk.Label(poll_action_box, text="Tx: 0 | Rx: 0 | Err: 0 | RTT: 0.0ms")
        self.poll_stat_lbl.pack(side=tk.RIGHT, padx=6)

        self.poll_conn_status = ttk.Label(
            poll_action_box,
            text="未连接 ⚪",
            foreground="gray",
            font=("Microsoft YaHei", 9, "bold"),
        )
        self.poll_conn_status.pack(side=tk.RIGHT, padx=4)

        self.btn_poll_conn = ttk.Button(
            poll_action_box,
            text="🔗 连接从机",
            command=self._toggle_poll_connect,
        )
        self.btn_poll_conn.pack(side=tk.RIGHT, padx=4)

        # 第 2 行：功能区域、起始地址、读取字数、变位模式、单次读取、启动轮询
        line2 = ttk.Frame(top_bar)
        line2.pack(fill=tk.X, padx=2, pady=3)

        ttk.Label(line2, text="功能区域:").grid(row=0, column=0, padx=3, pady=2)
        self.poll_area_var = tk.StringVar(value=AreaType.HOLDING_REGISTER)
        area_cb = ttk.Combobox(line2, textvariable=self.poll_area_var, width=18, state="readonly")
        area_cb["values"] = [
            AreaType.HOLDING_REGISTER,
            AreaType.INPUT_REGISTER,
            AreaType.COIL,
            AreaType.DISCRETE_INPUT,
        ]
        area_cb.grid(row=0, column=1, padx=3, pady=2)

        ttk.Label(line2, text="起始地址:").grid(row=0, column=2, padx=3, pady=2)
        self.poll_start_var = tk.IntVar(value=0)
        ttk.Entry(line2, textvariable=self.poll_start_var, width=8).grid(row=0, column=3, padx=3, pady=2)

        ttk.Label(line2, text="读取字数:").grid(row=0, column=4, padx=3, pady=2)
        self.poll_count_var = tk.IntVar(value=20)
        ttk.Entry(line2, textvariable=self.poll_count_var, width=6).grid(row=0, column=5, padx=3, pady=2)

        ttk.Label(line2, text="全局变位模式:").grid(row=0, column=6, padx=3, pady=2)
        self.poll_order_var = tk.StringVar(value=ByteOrderMode.CDAB.value)
        order_cb = ttk.Combobox(line2, textvariable=self.poll_order_var, width=7, state="readonly")
        order_cb["values"] = [m.value for m in ByteOrderMode]
        order_cb.grid(row=0, column=7, padx=3, pady=2)
        order_cb.bind("<<ComboboxSelected>>", lambda e: self._update_poll_table())

        ttk.Button(line2, text="⚡ 单次读取", command=self._poll_once).grid(row=0, column=8, padx=4, pady=2)
        self.btn_poll_loop = ttk.Button(line2, text="🔄 启动轮询 (1s)", command=self._toggle_poll_loop)
        self.btn_poll_loop.grid(row=0, column=9, padx=4, pady=2)

        # 快捷写入栏
        write_bar = ttk.LabelFrame(parent, text=" 快捷写入测试 (写寄存器 FC 06/16 或写线圈 FC 05) ")
        write_bar.pack(fill=tk.X, padx=6, pady=2)

        ttk.Label(write_bar, text="目标地址:").grid(row=0, column=0, padx=2, pady=2)
        self.w_addr_var = tk.IntVar(value=0)
        ttk.Entry(write_bar, textvariable=self.w_addr_var, width=6).grid(row=0, column=1, padx=2, pady=2)

        ttk.Label(write_bar, text="数据类型:").grid(row=0, column=2, padx=2, pady=2)
        self.w_type_var = tk.StringVar(value=ModbusDataType.FLOAT32.value)
        w_type_cb = ttk.Combobox(write_bar, textvariable=self.w_type_var, width=9, state="readonly")
        w_type_cb["values"] = [t.value for t in ModbusDataType]
        w_type_cb.grid(row=0, column=3, padx=2, pady=2)

        ttk.Label(write_bar, text="变位模式:").grid(row=0, column=4, padx=2, pady=2)
        self.w_order_var = tk.StringVar(value=ByteOrderMode.CDAB.value)
        w_order_cb = ttk.Combobox(write_bar, textvariable=self.w_order_var, width=7, state="readonly")
        w_order_cb["values"] = [m.value for m in ByteOrderMode]
        w_order_cb.grid(row=0, column=5, padx=2, pady=2)

        ttk.Label(write_bar, text="下发数值:").grid(row=0, column=6, padx=2, pady=2)
        self.w_val_var = tk.StringVar(value="88.88")
        ttk.Entry(write_bar, textvariable=self.w_val_var, width=12).grid(row=0, column=7, padx=2, pady=2)

        ttk.Button(write_bar, text="🚀 发送写入请求", command=self._send_poll_write).grid(row=0, column=8, padx=12, pady=2)

        # 监视表格
        table_frame = ttk.Frame(parent)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=3)

        cols = ("addr", "dec", "hex", "int16", "uint16", "float32", "int32", "bin")
        self.poll_tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended")

        self.poll_tree.heading("addr", text="寄存器地址")
        self.poll_tree.heading("dec", text="原始值 (Dec)")
        self.poll_tree.heading("hex", text="原始值 (Hex)")
        self.poll_tree.heading("int16", text="有符号 Int16")
        self.poll_tree.heading("uint16", text="无符号 UInt16")
        self.poll_tree.heading("float32", text="Float32 (当前变位)")
        self.poll_tree.heading("int32", text="Int32 (当前变位)")
        self.poll_tree.heading("bin", text="二进制位 (16-bit)")

        self.poll_tree.column("addr", width=80, anchor=tk.CENTER)
        self.poll_tree.column("dec", width=90, anchor=tk.E)
        self.poll_tree.column("hex", width=90, anchor=tk.CENTER)
        self.poll_tree.column("int16", width=100, anchor=tk.E)
        self.poll_tree.column("uint16", width=100, anchor=tk.E)
        self.poll_tree.column("float32", width=140, anchor=tk.E)
        self.poll_tree.column("int32", width=130, anchor=tk.E)
        self.poll_tree.column("bin", width=180, anchor=tk.W)

        tree_scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.poll_tree.yview)
        tree_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.poll_tree.config(yscrollcommand=tree_scroll_y.set)
        self.poll_tree.pack(fill=tk.BOTH, expand=True)

    def _on_poll_comm_type_change(self):
        """用户切换当前 Poll 通讯方式单选框."""
        self._update_poll_conn_mode_ui()

    def _update_poll_conn_mode_ui(self):
        """根据当前 Poll 通讯方式切换输入框容器."""
        c_type = self.poll_comm_type_var.get()
        if c_type == "RTU":
            self.poll_tcp_frame.pack_forget()
            self.poll_rtu_frame.pack(side=tk.LEFT, before=self.poll_common_frame)
        else:
            self.poll_rtu_frame.pack_forget()
            self.poll_tcp_frame.pack(side=tk.LEFT, before=self.poll_common_frame)

    def _refresh_poll_com_ports(self):
        """刷新 Poll 串口端口列表."""
        ports = get_available_com_ports()
        self.poll_com_combo["values"] = ports
        if ports and self.poll_serial_port_var.get() not in ports:
            self.poll_serial_port_var.set(ports[0])

    def _toggle_poll_connect(self):
        comm_type = self.poll_comm_type_var.get().upper()
        if not self.poll_engine.is_connected:
            sid = self.poll_id_var.get()
            self.poll_engine.slave_id = sid
            if comm_type == "RTU":
                port = self.poll_serial_port_var.get().strip() or "COM1"
                baud = int(self.poll_baud_var.get())
                p_raw = self.poll_parity_var.get().strip().upper()
                parity = p_raw[0] if p_raw else "N"
                if self.poll_engine.connect_rtu(serial_port=port, baudrate=baud, parity=parity):
                    self.poll_conn_status.config(text=f"已连接 🟢 ({port})", foreground="green")
                    self.btn_poll_conn.config(text="❌ 断开连接")
                    self.log(f"已连接到 Modbus RTU 从机: 串口={port}, 波特率={baud}, 校验={parity}, 站号={sid}")
                else:
                    messagebox.showerror("连接失败", f"无法打开或连接串口: {port}\n请确认端口未被占用且虚拟/物理端口正常。")
            else:
                self.poll_engine.host = self.poll_ip_var.get().strip()
                self.poll_engine.port = self.poll_port_var.get()
                if self.poll_engine.connect_tcp():
                    self.poll_conn_status.config(text=f"已连接 🟢 ({self.poll_engine.host}:{self.poll_engine.port})", foreground="green")
                    self.btn_poll_conn.config(text="❌ 断开连接")
                    self.log(f"已连接到 Modbus TCP 从机: {self.poll_engine.host}:{self.poll_engine.port} (站号={sid})")
                else:
                    messagebox.showerror("连接失败", f"无法连接到 {self.poll_engine.host}:{self.poll_engine.port}")
        else:
            self.poll_engine.disconnect()
            self.poll_conn_status.config(text="未连接 ⚪", foreground="gray")
            self.btn_poll_conn.config(text="🔗 连接从机")
            self.btn_poll_loop.config(text="🔄 启动轮询 (1s)")
            self.log("已断开与从机的连接。")

    def _poll_once(self):
        area = self.poll_area_var.get()
        start = self.poll_start_var.get()
        count = self.poll_count_var.get()
        ok, vals, err = self.poll_engine.read_block(area, start, count)
        if ok:
            self._update_poll_table()
            self._update_poll_stats()
            self.log(f"单次读取成功: 地址 {start}~{start+count-1}, 共 {len(vals)} 项")
        else:
            self._update_poll_stats()
            self.log(f"读取失败: {err}", level="ERROR")

    def _toggle_poll_loop(self):
        if not self.poll_engine.is_polling:
            area = self.poll_area_var.get()
            start = self.poll_start_var.get()
            count = self.poll_count_var.get()
            self.poll_engine.on_poll_success = lambda cache: self.root.after(0, self._on_poll_update)
            self.poll_engine.on_poll_error = lambda err: self.root.after(
                0, lambda: self.log(f"轮询错误: {err}", level="WARN")
            )
            self.poll_engine.start_polling(area, start, count, interval_ms=1000)
            self.btn_poll_loop.config(text="⏹ 停止轮询")
            self.log(f"已开启周期性轮询: 起始地址={start}, 数量={count}")
        else:
            self.poll_engine.stop_polling()
            self.btn_poll_loop.config(text="🔄 启动轮询 (1s)")
            self.log("已停止周期性轮询。")

    def _on_poll_update(self):
        self._update_poll_table()
        self._update_poll_stats()

    def _update_poll_stats(self):
        self.poll_stat_lbl.config(
            text=f"Tx: {self.poll_engine.tx_count} | Rx: {self.poll_engine.rx_count} | "
            f"Err: {self.poll_engine.err_count} | RTT: {self.poll_engine.last_rtt_ms:.1f}ms"
        )

    def _update_poll_table(self):
        self.poll_tree.delete(*self.poll_tree.get_children())
        cache = self.poll_engine.cached_registers
        if not cache:
            return

        order_str = self.poll_order_var.get()
        mode = ByteOrderMode(order_str)

        addresses = sorted(cache.keys())
        for addr in addresses:
            raw = cache[addr]
            int16_val = decode_value([raw], ModbusDataType.INT16, mode)
            uint16_val = decode_value([raw], ModbusDataType.UINT16, mode)
            bin_val = f"{raw:016b}"
            bin_fmt = f"{bin_val[0:4]} {bin_val[4:8]} {bin_val[8:12]} {bin_val[12:16]}"

            if addr + 1 in cache:
                regs32 = [raw, cache[addr + 1]]
                f32_val = decode_value(regs32, ModbusDataType.FLOAT32, mode)
                i32_val = decode_value(regs32, ModbusDataType.INT32, mode)
            else:
                f32_val = "-"
                i32_val = "-"

            self.poll_tree.insert(
                "",
                tk.END,
                values=(
                    addr,
                    raw,
                    f"0x{raw:04X}",
                    int16_val,
                    uint16_val,
                    f32_val,
                    i32_val,
                    bin_fmt,
                ),
            )

    def _send_poll_write(self):
        addr = self.w_addr_var.get()
        dtype = ModbusDataType(self.w_type_var.get())
        order = ByteOrderMode(self.w_order_var.get())
        val_str = self.w_val_var.get().strip()

        try:
            if dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64):
                val = float(val_str)
            elif dtype == ModbusDataType.BOOL:
                val = val_str.lower() in ("true", "1", "yes", "on")
            elif dtype in (ModbusDataType.HEX16, ModbusDataType.HEX32, ModbusDataType.BINARY16, ModbusDataType.STRING):
                val = val_str
            else:
                val = int(val_str)

            ok, msg = self.poll_engine.write_typed(
                AreaType.HOLDING_REGISTER, addr, val, dtype, order
            )
            if ok:
                self.log(f"主机写入成功: 地址={addr}, 类型={dtype.value}, 变位={order.value}, 值={val}")
                self._poll_once()
            else:
                messagebox.showerror("写入失败", f"通信错误: {msg}")
        except Exception as e:
            messagebox.showerror("数值格式错误", f"解析失败: {e}")

    def _ui_heartbeat(self):
        if self.slave_engine.is_running:
            self._refresh_slave_tree()
        self.root.after(1000, self._ui_heartbeat)


def main():
    # 注册 Windows 专属 AppUserModelID (彻底解决任务栏退化显示默认蓝色羽毛图标的系统机制)
    if sys.platform.startswith("win"):
        try:
            myappid = "muzilinxi.modbusstudio.workstation.v1"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    root = tk.Tk()
    app = ModbusStudioApp(root)

    def _on_close():
        for inst in app.slave_instances.values():
            if inst.engine.is_running:
                inst.engine.stop()
        app.poll_engine.disconnect()
        root.destroy()
        sys.exit(0)

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
