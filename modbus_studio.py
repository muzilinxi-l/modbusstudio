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

from hems_db_loader import HemsAppModel, HemsDatabase
from hems_pairing import HemsPairingEngine, PairingRule
from modbus_codec import (
    ByteOrderMode,
    ModbusDataType,
    TYPE_REGISTER_COUNT,
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


class ModbusStudioApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Modbus Studio - 业务配置与变位仿真调试工作站 (支持 extra/hems.cdb)")
        self.root.geometry("1180x800")
        self.root.minsize(980, 620)

        # 窗口图标
        icon_path = get_resource_path("app.ico")
        if os.path.exists(icon_path):
            try:
                self.root.iconbitmap(icon_path)
            except Exception as e:
                logger.warning(f"设置窗口图标失败: {e}")

        # 引擎实例
        self.slave_engine: ModbusSlaveEngine = ModbusSlaveEngine()
        self.poll_engine: ModbusPollEngine = ModbusPollEngine()
        self.hems_db = HemsDatabase(os.path.join("extra", "hems.cdb"))
        self.pairing_engine = HemsPairingEngine(self.hems_db.db_path)

        self._init_style()
        self._build_ui()
        self._load_default_slave_points()

        # 定时刷新 UI 定时器
        self.root.after(500, self._ui_heartbeat)

    def _init_style(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 9, "bold"), background="#e1e4e8")
        style.configure("Treeview", font=("Microsoft YaHei", 9), rowheight=24)
        style.map("Treeview", background=[("selected", "#0078d7")])

    def _build_ui(self):
        # 顶部选项卡
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        # Tab 1: Slave 从机模拟器
        self.slave_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.slave_frame, text="  🖥️ Modbus Slave 从机模拟器 (支持业务库导入与批量变位)  ")
        self._build_slave_tab(self.slave_frame)

        # Tab 2: Poll 主机调试器
        self.poll_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.poll_frame, text="  📡 Modbus Poll 主机轮询调试器 (支持业务规则导入)  ")
        self._build_poll_tab(self.poll_frame)

        # 底部日志面板
        log_frame = ttk.LabelFrame(self.root, text=" 实时系统与通信日志 ")
        log_frame.pack(fill=tk.X, padx=6, pady=4)

        self.log_text = tk.Text(log_frame, height=5, font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)

        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=log_scroll.set)

    def log(self, msg: str, level: str = "INFO"):
        """向底部输出时间戳日志."""
        now = time.strftime("%H:%M:%S")
        line = f"[{now}] [{level}] {msg}\n"
        self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)

    # =================================================================
    # Tab 1: Slave 从机模拟器 UI
    # =================================================================
    def _build_slave_tab(self, parent: ttk.Frame):
        # 1. 顶部控制栏
        top_bar = ttk.LabelFrame(parent, text=" 从机服务连接配置与业务库导入 ")
        top_bar.pack(fill=tk.X, padx=6, pady=3)

        ttk.Label(top_bar, text="监听 IP:").grid(row=0, column=0, padx=4, pady=3, sticky=tk.W)
        self.slave_ip_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(top_bar, textvariable=self.slave_ip_var, width=11).grid(row=0, column=1, padx=4, pady=3)

        ttk.Label(top_bar, text="端口:").grid(row=0, column=2, padx=4, pady=3, sticky=tk.W)
        self.slave_port_var = tk.IntVar(value=5020)
        ttk.Entry(top_bar, textvariable=self.slave_port_var, width=7).grid(row=0, column=3, padx=4, pady=3)

        ttk.Label(top_bar, text="从机 ID:").grid(row=0, column=4, padx=4, pady=3, sticky=tk.W)
        self.slave_id_var = tk.IntVar(value=1)
        ttk.Entry(top_bar, textvariable=self.slave_id_var, width=5).grid(row=0, column=5, padx=4, pady=3)

        self.btn_slave_start = ttk.Button(top_bar, text="▶ 启动服务", command=self._toggle_slave_server)
        self.btn_slave_start.grid(row=0, column=6, padx=8, pady=3)

        self.slave_status_lbl = ttk.Label(top_bar, text="状态: 已停止 🔴", font=("Microsoft YaHei", 9, "bold"), foreground="red")
        self.slave_status_lbl.grid(row=0, column=7, padx=6, pady=3)

        ttk.Separator(top_bar, orient=tk.VERTICAL).grid(row=0, column=8, sticky="ns", padx=8, pady=2)

        # 核心业务数据库导入按钮
        btn_import_db = ttk.Button(
            top_bar,
            text="📂 导入设备点表...",
            command=self._open_import_db_dialog,
        )
        btn_import_db.grid(row=0, column=9, padx=4, pady=3)

        btn_pairing = ttk.Button(
            top_bar,
            text="🔗 业务配对中心 (基于 More 自动配对)...",
            command=self._open_pairing_dialog,
        )
        btn_pairing.grid(row=0, column=10, padx=4, pady=3)

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
        self.new_addr_var = tk.IntVar(value=0)
        ttk.Entry(point_ctrl_bar, textvariable=self.new_addr_var, width=6).grid(row=0, column=3, padx=2, pady=2)

        ttk.Label(point_ctrl_bar, text="描述:").grid(row=0, column=4, padx=2, pady=2)
        self.new_desc_var = tk.StringVar(value="温度变送器")
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
        self.new_val_var = tk.StringVar(value="25.5")
        ttk.Entry(point_ctrl_bar, textvariable=self.new_val_var, width=7).grid(row=0, column=13, padx=2, pady=2)

        ttk.Label(point_ctrl_bar, text="模拟:").grid(row=0, column=14, padx=2, pady=2)
        self.new_sim_var = tk.StringVar(value="随机波动")
        sim_combo = ttk.Combobox(point_ctrl_bar, textvariable=self.new_sim_var, width=8, state="readonly")
        sim_combo["values"] = ["固定", "随机波动", "累加递增", "正弦波"]
        sim_combo.grid(row=0, column=15, padx=2, pady=2)

        ttk.Button(point_ctrl_bar, text="➕ 单点添加", command=self._add_or_update_slave_point).grid(row=0, column=16, padx=4, pady=2)

        # 3. 批量操作工具栏（对多选行批量生效）
        batch_bar = ttk.LabelFrame(parent, text=" 🛠️ 批量操作选中的点位 (支持 Ctrl / Shift 任意多选) ")
        batch_bar.pack(fill=tk.X, padx=6, pady=3)

        ttk.Button(batch_bar, text="☑️ 全选", command=self._select_all_slave_points).grid(row=0, column=0, padx=3, pady=2)
        ttk.Button(batch_bar, text="⬜ 取消选择", command=self._deselect_all_slave_points).grid(row=0, column=1, padx=3, pady=2)

        ttk.Label(batch_bar, text="目标变位:").grid(row=0, column=2, padx=4, pady=2)
        self.batch_order_var = tk.StringVar(value=ByteOrderMode.ABCD.value)
        batch_order_cb = ttk.Combobox(batch_bar, textvariable=self.batch_order_var, width=7, state="readonly")
        batch_order_cb["values"] = [m.value for m in ByteOrderMode]
        batch_order_cb.grid(row=0, column=3, padx=2, pady=2)

        ttk.Button(
            batch_bar,
            text="⚡ 批量修改变位",
            command=self._apply_batch_order,
        ).grid(row=0, column=4, padx=3, pady=2)

        ttk.Label(batch_bar, text="目标类型:").grid(row=0, column=5, padx=4, pady=2)
        self.batch_type_var = tk.StringVar(value=ModbusDataType.FLOAT32.value)
        batch_type_cb = ttk.Combobox(batch_bar, textvariable=self.batch_type_var, width=9, state="readonly")
        batch_type_cb["values"] = [t.value for t in ModbusDataType]
        batch_type_cb.grid(row=0, column=6, padx=2, pady=2)

        ttk.Button(batch_bar, text="批量修改类型", command=self._apply_batch_type).grid(row=0, column=7, padx=3, pady=2)

        ttk.Label(batch_bar, text="统一设值:").grid(row=0, column=8, padx=4, pady=2)
        self.batch_val_var = tk.StringVar(value="0")
        ttk.Entry(batch_bar, textvariable=self.batch_val_var, width=7).grid(row=0, column=9, padx=2, pady=2)
        ttk.Button(batch_bar, text="批量修改数值", command=self._apply_batch_value).grid(row=0, column=10, padx=3, pady=2)

        ttk.Label(batch_bar, text="模拟规则:").grid(row=0, column=11, padx=4, pady=2)
        self.batch_sim_var = tk.StringVar(value="固定")
        batch_sim_cb = ttk.Combobox(batch_bar, textvariable=self.batch_sim_var, width=8, state="readonly")
        batch_sim_cb["values"] = ["固定", "随机波动", "累加递增", "正弦波"]
        batch_sim_cb.grid(row=0, column=12, padx=2, pady=2)
        ttk.Button(batch_bar, text="批量修改规则", command=self._apply_batch_sim).grid(row=0, column=13, padx=3, pady=2)

        ttk.Button(batch_bar, text="🗑️ 批量删除选中", command=self._delete_slave_point).grid(row=0, column=14, padx=8, pady=2)

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

    # -----------------------------------------------------------------
    # 核心：从 extra/hems.cdb 业务数据库导入设备点表
    # -----------------------------------------------------------------
    def _open_import_db_dialog(self):
        apps = self.hems_db.load_modbus_apps()
        if not apps:
            messagebox.showwarning(
                "未找到数据",
                f"未能从 {self.hems_db.db_path} 中读取到 Modbus 应用，请确认文件路径是否正确。",
            )
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("从业务数据库 (extra/hems.cdb) 导入 Modbus 设备业务配置")
        dlg.geometry("780x520")
        dlg.transient(self.root)
        dlg.grab_set()

        top_desc = ttk.Frame(dlg, padding=8)
        top_desc.pack(fill=tk.X)
        ttk.Label(
            top_desc,
            text=f"📂 当前数据库: {self.hems_db.db_path} | 共识别到 {len(apps)} 个 Modbus 业务模块 (app 表配置)",
            font=("Microsoft YaHei", 9, "bold"),
            foreground="#0066cc",
        ).pack(anchor=tk.W)
        ttk.Label(
            top_desc,
            text="请在下方选择目标设备，可一键将其业务点位/轮询规则导入至 Slave 模拟器或 Poll 调试器中：",
        ).pack(anchor=tk.W, pady=2)

        # 列表表格
        table_frame = ttk.Frame(dlg, padding=8)
        table_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("id", "type", "eng", "chn", "polls", "vars", "enable")
        tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")

        tree.heading("id", text="App ID")
        tree.heading("type", text="设备方向")
        tree.heading("eng", text="英文标识")
        tree.heading("chn", text="业务名称")
        tree.heading("polls", text="轮询规则数")
        tree.heading("vars", text="业务点位数")
        tree.heading("enable", text="启用状态")

        tree.column("id", width=60, anchor=tk.CENTER)
        tree.column("type", width=110, anchor=tk.CENTER)
        tree.column("eng", width=140, anchor=tk.W)
        tree.column("chn", width=130, anchor=tk.W)
        tree.column("polls", width=90, anchor=tk.CENTER)
        tree.column("vars", width=90, anchor=tk.CENTER)
        tree.column("enable", width=70, anchor=tk.CENTER)

        for a in apps:
            dir_str = "南向采集 (Master)" if a.is_south_master else "北向从机 (Slave)" if a.is_north_slave else "策略模块"
            tree.insert(
                "",
                tk.END,
                values=(
                    a.app_id,
                    dir_str,
                    a.english_name,
                    a.chinese_name,
                    len(a.pollings),
                    len(a.points),
                    "已启用" if a.enable else "停用",
                ),
            )

        scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        tree.config(yscrollcommand=scroll_y.set)
        tree.pack(fill=tk.BOTH, expand=True)

        # 底部操作栏
        btn_bar = ttk.Frame(dlg, padding=10)
        btn_bar.pack(fill=tk.X)

        def _get_selected_app() -> Optional[HemsAppModel]:
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("提示", "请先在上方列表中选中一个业务设备")
                return None
            app_id = int(tree.item(sel[0])["values"][0])
            for a in apps:
                if a.app_id == app_id:
                    return a
            return None

        def _do_import_to_slave():
            app_obj = _get_selected_app()
            if not app_obj:
                return

            points = app_obj.extract_studio_points()
            if not points:
                messagebox.showwarning("无点位", f"设备 {app_obj.english_name} 中未配置有效点位")
                return

            # 如果设备包含从机站号信息，则自动设置从机 ID
            if app_obj.pollings:
                sid = app_obj.pollings[0].get("Slave Id")
                if sid:
                    try:
                        self.slave_id_var.set(int(sid))
                    except ValueError:
                        pass

            # 导入点位（默认清空或追加）
            if messagebox.askyesno("导入确认", f"是否将 [{app_obj.chinese_name}] 的 {len(points)} 个业务点位导入到 Slave 模拟器中？\n(是：覆盖当前点表；否：追加到当前点表)"):
                self.slave_engine.points.clear()

            for p in points:
                addr = p["address"]
                self.slave_engine.points[addr] = p
                self.slave_engine.write_typed_value(
                    p["area"], addr, p["current_val"], p["data_type"], p["byte_order"]
                )

            self._refresh_slave_tree()
            self.log(f"成功从业务数据库导入设备 [{app_obj.chinese_name}]，加载了 {len(points)} 个业务点位！")
            dlg.destroy()

        def _do_import_to_poll():
            app_obj = _get_selected_app()
            if not app_obj:
                return

            if not app_obj.pollings:
                messagebox.showwarning("无轮询规则", f"设备 {app_obj.english_name} 中未配置 Pollings 规则")
                return

            rule = app_obj.pollings[0]
            sid = rule.get("Slave Id", "1")
            fc = rule.get("Function Code", "3")
            start = rule.get("Register Start Address", "0")
            num = rule.get("Register Number", "10")

            try:
                self.poll_id_var.set(int(sid))
                self.poll_start_var.set(int(start))
                self.poll_count_var.set(int(num))
                area_val = AreaType.HOLDING_REGISTER if str(fc) in ("3", "16") else AreaType.INPUT_REGISTER
                self.poll_area_var.set(area_val)
                self.notebook.select(self.poll_frame)
                self.log(f"已将业务设备 [{app_obj.chinese_name}] 轮询规则导入至 Poll: 站号={sid}, 起始地址={start}, 数量={num}")
                dlg.destroy()
            except Exception as ex:
                messagebox.showerror("错误", f"解析轮询规则失败: {ex}")

        ttk.Button(btn_bar, text="📥 导入为 Slave 从机仿真点表", command=_do_import_to_slave).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_bar, text="📡 导入为 Poll 主机轮询目标", command=_do_import_to_poll).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_bar, text="关闭", command=dlg.destroy).pack(side=tk.RIGHT, padx=6)

    def _open_pairing_dialog(self):
        """打开基于 app.More 配置的业务配对中心."""
        rules = self.pairing_engine.rules
        if not rules:
            messagebox.showinfo("提示", "未在数据库中解析到配对规则")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("业务配对中心 (基于 app.More 自动配对 & 批量变位)")
        dlg.geometry("980x600")
        dlg.transient(self.root)
        dlg.grab_set()

        top_desc = ttk.Frame(dlg, padding=8)
        top_desc.pack(fill=tk.X)
        ttk.Label(
            top_desc,
            text=f"⚡ 智能业务配对引擎 | 共识别到 {len(rules)} 条从南向采集到北向转发的配对规则",
            font=("Microsoft YaHei", 9, "bold"),
            foreground="#0066cc",
        ).pack(anchor=tk.W)
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
        addrs = self._get_selected_addresses()
        if not addrs:
            messagebox.showinfo("提示", "请先在表格中选中至少一个点位（支持按住 Ctrl / Shift 多选）")
            return

        new_order = ByteOrderMode(self.batch_order_var.get())
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
        self.log(f"已批量将 {modified_count} 个点位的变位模式修改为: {new_order.value}")

    def _apply_batch_type(self):
        addrs = self._get_selected_addresses()
        if not addrs:
            messagebox.showinfo("提示", "请先选中要批量修改的点位")
            return

        new_type = ModbusDataType(self.batch_type_var.get())
        for addr in addrs:
            p = self.slave_engine.points.get(addr)
            if p:
                p["data_type"] = new_type
                self.slave_engine.write_typed_value(
                    p["area"], addr, p["current_val"], new_type, p["byte_order"]
                )

        self._refresh_slave_tree()
        self.log(f"已批量将 {len(addrs)} 个点位的数据类型修改为: {new_type.value}")

    def _apply_batch_value(self):
        addrs = self._get_selected_addresses()
        if not addrs:
            messagebox.showinfo("提示", "请先选中点位")
            return

        raw_str = self.batch_val_var.get().strip()
        try:
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
            self.log(f"已批量修改 {len(addrs)} 个点位的值为: {raw_str}")
        except Exception as e:
            messagebox.showerror("格式错误", f"输入数值格式有误: {e}")

    def _apply_batch_sim(self):
        addrs = self._get_selected_addresses()
        if not addrs:
            messagebox.showinfo("提示", "请先选中点位")
            return

        rule = self.batch_sim_var.get()
        for addr in addrs:
            p = self.slave_engine.points.get(addr)
            if p:
                p["sim_mode"] = rule
        self._refresh_slave_tree()
        self.log(f"已批量将 {len(addrs)} 个点位的模拟规则修改为: {rule}")

    def _add_or_update_slave_point(self):
        try:
            addr = self.new_addr_var.get()
            desc = self.new_desc_var.get().strip()
            area = self.new_area_var.get()
            dtype = ModbusDataType(self.new_type_var.get())
            order = ByteOrderMode(self.new_order_var.get())
            val_str = self.new_val_var.get().strip()
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
                self.slave_engine.write_typed_value(p["area"], addr, new_val, dtype, p["byte_order"])
                self._refresh_slave_tree()
                self.log(f"手动修改点位 {addr} 成功: {new_val}")
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
            area = p["area"]
            dtype = p["data_type"]
            order = p["byte_order"]
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
                    p["desc"],
                    area.split()[0],
                    dtype.value,
                    order.value,
                    val,
                    hex_str,
                    p["sim_mode"],
                    cnt,
                ),
            )
            if addr in selected_addrs:
                self.slave_tree.selection_add(item_id)

    def _load_default_slave_points(self):
        defaults = [
            (0, "进气温度", AreaType.HOLDING_REGISTER, ModbusDataType.FLOAT32, ByteOrderMode.CDAB, 28.5, "随机波动"),
            (2, "排气压力", AreaType.HOLDING_REGISTER, ModbusDataType.FLOAT32, ByteOrderMode.ABCD, 1.25, "正弦波"),
            (4, "电表累计电量", AreaType.HOLDING_REGISTER, ModbusDataType.DOUBLE64, ByteOrderMode.ABCD, 102456.78, "累加递增"),
            (8, "生产总件数", AreaType.HOLDING_REGISTER, ModbusDataType.INT32, ByteOrderMode.CDAB, 58200, "累加递增"),
            (10, "设备运行频率", AreaType.HOLDING_REGISTER, ModbusDataType.INT16, ByteOrderMode.ABCD, 50, "固定"),
            (11, "故障代码", AreaType.HOLDING_REGISTER, ModbusDataType.HEX16, ByteOrderMode.ABCD, "0x00A0", "固定"),
            (12, "设备状态字", AreaType.HOLDING_REGISTER, ModbusDataType.BINARY16, ByteOrderMode.ABCD, "0000 0000 0000 0001", "固定"),
            (13, "批次编号", AreaType.HOLDING_REGISTER, ModbusDataType.STRING, ByteOrderMode.ABCD, "PROD-A1", "固定"),
            (0, "主循环泵启停", AreaType.COIL, ModbusDataType.BOOL, ByteOrderMode.ABCD, True, "固定"),
            (1, "急停复位按钮", AreaType.COIL, ModbusDataType.BOOL, ByteOrderMode.ABCD, False, "固定"),
            (0, "安全门限位", AreaType.DISCRETE_INPUT, ModbusDataType.BOOL, ByteOrderMode.ABCD, True, "固定"),
            (0, "母线电压采样", AreaType.INPUT_REGISTER, ModbusDataType.UINT16, ByteOrderMode.ABCD, 385, "随机波动"),
        ]
        for addr, desc, area, dtype, order, val, sim in defaults:
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

    def _toggle_slave_server(self):
        if not self.slave_engine.is_running:
            self.slave_engine.host = self.slave_ip_var.get().strip()
            self.slave_engine.port = self.slave_port_var.get()
            self.slave_engine.slave_id = self.slave_id_var.get()
            try:
                self.slave_engine.start()
                self.slave_status_lbl.config(
                    text=f"状态: 运行中 🟢 ({self.slave_engine.host}:{self.slave_engine.port})",
                    foreground="green",
                )
                self.btn_slave_start.config(text="⏹ 停止服务")
                self.log(
                    f"Modbus Slave 服务已成功启动在 {self.slave_engine.host}:{self.slave_engine.port} (Slave ID: {self.slave_engine.slave_id})"
                )
            except Exception as e:
                messagebox.showerror("启动失败", f"无法启动 Slave: {e}")
        else:
            self.slave_engine.stop()
            self.slave_status_lbl.config(text="状态: 已停止 🔴", foreground="red")
            self.btn_slave_start.config(text="▶ 启动服务")
            self.log("Modbus Slave 服务已停止。")

    # =================================================================
    # Tab 2: Poll 主机调试器 UI
    # =================================================================
    def _build_poll_tab(self, parent: ttk.Frame):
        top_bar = ttk.LabelFrame(parent, text=" 目标从机与轮询参数 (支持业务库规则导入) ")
        top_bar.pack(fill=tk.X, padx=6, pady=3)

        ttk.Label(top_bar, text="目标 IP:").grid(row=0, column=0, padx=3, pady=2)
        self.poll_ip_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(top_bar, textvariable=self.poll_ip_var, width=12).grid(row=0, column=1, padx=3, pady=2)

        ttk.Label(top_bar, text="端口:").grid(row=0, column=2, padx=3, pady=2)
        self.poll_port_var = tk.IntVar(value=5020)
        ttk.Entry(top_bar, textvariable=self.poll_port_var, width=8).grid(row=0, column=3, padx=3, pady=2)

        ttk.Label(top_bar, text="站号 ID:").grid(row=0, column=4, padx=3, pady=2)
        self.poll_id_var = tk.IntVar(value=1)
        ttk.Entry(top_bar, textvariable=self.poll_id_var, width=5).grid(row=0, column=5, padx=3, pady=2)

        self.btn_poll_conn = ttk.Button(top_bar, text="🔗 连接从机", command=self._toggle_poll_connect)
        self.btn_poll_conn.grid(row=0, column=6, padx=6, pady=2)

        self.poll_conn_status = ttk.Label(top_bar, text="未连接 ⚪", foreground="gray", font=("Microsoft YaHei", 9, "bold"))
        self.poll_conn_status.grid(row=0, column=7, padx=4, pady=2)

        self.poll_stat_lbl = ttk.Label(top_bar, text="Tx: 0 | Rx: 0 | Err: 0 | RTT: 0.0ms")
        self.poll_stat_lbl.grid(row=0, column=8, padx=10, pady=2)

        # 快速导入业务库按钮
        ttk.Button(
            top_bar,
            text="📂 导入业务设备轮询规则...",
            command=self._open_import_db_dialog,
        ).grid(row=0, column=9, padx=6, pady=2)

        # 第二行
        ttk.Label(top_bar, text="功能区域:").grid(row=1, column=0, padx=3, pady=3)
        self.poll_area_var = tk.StringVar(value=AreaType.HOLDING_REGISTER)
        area_cb = ttk.Combobox(top_bar, textvariable=self.poll_area_var, width=18, state="readonly")
        area_cb["values"] = [
            AreaType.HOLDING_REGISTER,
            AreaType.INPUT_REGISTER,
            AreaType.COIL,
            AreaType.DISCRETE_INPUT,
        ]
        area_cb.grid(row=1, column=1, padx=3, pady=3)

        ttk.Label(top_bar, text="起始地址:").grid(row=1, column=2, padx=3, pady=3)
        self.poll_start_var = tk.IntVar(value=0)
        ttk.Entry(top_bar, textvariable=self.poll_start_var, width=8).grid(row=1, column=3, padx=3, pady=3)

        ttk.Label(top_bar, text="读取字数:").grid(row=1, column=4, padx=3, pady=3)
        self.poll_count_var = tk.IntVar(value=20)
        ttk.Entry(top_bar, textvariable=self.poll_count_var, width=6).grid(row=1, column=5, padx=3, pady=3)

        ttk.Label(top_bar, text="全局变位模式:").grid(row=1, column=6, padx=3, pady=3)
        self.poll_order_var = tk.StringVar(value=ByteOrderMode.CDAB.value)
        order_cb = ttk.Combobox(top_bar, textvariable=self.poll_order_var, width=7, state="readonly")
        order_cb["values"] = [m.value for m in ByteOrderMode]
        order_cb.grid(row=1, column=7, padx=3, pady=3)
        order_cb.bind("<<ComboboxSelected>>", lambda e: self._update_poll_table())

        ttk.Button(top_bar, text="⚡ 单次读取", command=self._poll_once).grid(row=1, column=8, padx=4, pady=3)
        self.btn_poll_loop = ttk.Button(top_bar, text="🔄 启动轮询 (1s)", command=self._toggle_poll_loop)
        self.btn_poll_loop.grid(row=1, column=9, padx=4, pady=3)

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

    def _toggle_poll_connect(self):
        if not self.poll_engine.is_connected:
            self.poll_engine.host = self.poll_ip_var.get().strip()
            self.poll_engine.port = self.poll_port_var.get()
            self.poll_engine.slave_id = self.poll_id_var.get()
            if self.poll_engine.connect():
                self.poll_conn_status.config(text="已连接 🟢", foreground="green")
                self.btn_poll_conn.config(text="❌ 断开连接")
                self.log(f"已连接到 Modbus 从机: {self.poll_engine.host}:{self.poll_engine.port}")
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
    root = tk.Tk()
    app = ModbusStudioApp(root)

    def _on_close():
        app.slave_engine.stop()
        app.poll_engine.disconnect()
        root.destroy()
        sys.exit(0)

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
