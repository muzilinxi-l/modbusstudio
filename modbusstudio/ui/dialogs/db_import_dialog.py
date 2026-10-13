"""
    Modbus Studio 业务数据库设备模型与点表批量导入模态弹窗模块，支持多选导入、独立从机创建及 TCP/RTU 配额守卫。
    关键导入：logging/os/time（时间戳生成与路径检查）、tkinter/ttk/messagebox（模态弹窗与表格组件）、models/PointService（领域实体、100 实例配额常量与点位校验服务）。
对外暴露：DbImportDialog（数据库点表与设备模型批量导入对话框控制器类）。
"""

from __future__ import annotations
import logging
import os
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Callable, Dict, List, Optional, Tuple

# 导入领域层枚举、连接配置、TCP/RTU 独立 100 实例硬上限常量与从机聚合根
from ...models import (
    AreaType,
    CommType,
    ConnectionConfig,
    MAX_RTU_SLAVE_INSTANCES,
    MAX_TCP_SLAVE_INSTANCES,
    SlaveDevice,
)
# 导入点位业务服务，用于在导入时对每个点位执行地址边界校验与初始十六进制编码
from ...services import PointService, SlaveService

# 初始化弹窗模块命名日志器
logger = logging.getLogger("DbImportDialog")


# =====================================================================
# 业务数据库设备模型与点表批量导入对话框 (DbImportDialog)
# 核心职责与设计亮点：
# 1. 解析一体机现场 `.db`/`.cdb` 数据库中的南北向 Modbus 设备与点表模型并结构化呈现；
# 2. 支持单选与 `extended` 多选批量勾选，提供“全选当前列表 / 全选已使能 / 反选 / 清空”快捷工具；
# 3. 支持三种导入策略：新建为独立从机服务实例（推荐）、覆盖替换当前从机点表、追加合并到当前从机点表；
# 4. 严格执行 Modbus TCP (100 个) 与 Modbus RTU (100 个) 独立容量配额硬限制；
# 5. 采用 20ms 防抖 (`after_cancel` + `after(20, ...)`) 动态重算列宽，杜绝拖拽缩放窗口时的重绘卡顿。
# =====================================================================
class DbImportDialog:
    """业务数据库设备模型与点表批量导入模态对话框控制器。"""

    # 寄存器功能区下拉框与默认区域统一按 0x → 1x → 3x → 4x 升序排列（默认选中 4x 保持寄存器）
    AREA_CHOICES = [
        AreaType.COIL.friendly_name,
        AreaType.DISCRETE.friendly_name,
        AreaType.INPUT.friendly_name,
        AreaType.HOLDING.friendly_name,
    ]

    # 工业常用波特率完整升序档位列表（默认选中 9600）
    BAUDRATE_CHOICES = [
        "1200", "2400", "4800", "9600", "19200",
        "38400", "57600", "115200", "230400",
    ]

    def __init__(
        self,
        parent: tk.Widget,
        app: Any,
        on_imported_callback: Callable[[SlaveDevice], None],
        current_device_id: Optional[str] = None,
    ) -> None:
        """初始化批量导入对话框，校验并加载数据库设备模型后构建 UI 与初始列表。"""
        # 参数说明：
        # - parent：父级容器组件（通常为 SlavePanel）；
        # - app：全局应用上下文（含 db_path、slave_service、logging_service）；
        # - on_imported_callback：导入完成后的回调，传入最后创建或更新的 SlaveDevice 以便主面板立即聚焦展示；
        # - current_device_id：主面板当前选中的从机 ID（用于“覆盖替换”与“追加合并”模式定位目标从机）。
        self.parent = parent
        self.app = app
        self.on_imported = on_imported_callback
        self.current_device_id = current_device_id
        self._rtu_user_overridden = False
        self._syncing_rtu_ui = False
        self._saved_selection_before_search: Optional[List[str]] = None

        # 创建模态 Toplevel 窗口，设置默认尺寸 (1060x700) 与最小尺寸 (960x580)，允许用户自由拉伸
        self.dlg = tk.Toplevel(parent)
        self.dlg.title("从业务数据库导入设备模型与点表 (支持多选批量导入)")
        self.dlg.geometry("1060x700")
        self.dlg.minsize(960, 580)
        self.dlg.resizable(True, True)
        # 绑定父窗口层级关系并独占输入焦点，防止导入中途主窗口状态被并发修改
        self.dlg.transient(parent.winfo_toplevel())
        self.dlg.grab_set()

        self._set_dialog_icon()

        # 延迟导入 HemsDatabase 并校验当前关联的数据库文件是否存在
        from ...hems_db_loader import HemsDatabase
        if not self.app.db_path or not os.path.exists(self.app.db_path):
            messagebox.showwarning("提示", "未找到有效的业务数据库，请先关联数据库！", parent=self.dlg)
            self.dlg.destroy()
            return

        # 读取 SQLite app 表中所有 Modbus 设备记录，若文件损坏或表结构不兼容则弹窗报错并安全销毁弹窗
        try:
            db = HemsDatabase(self.app.db_path)
            self.apps = db.load_modbus_apps()
        except Exception as e:
            logger.error(f"加载数据库失败: {e}")
            messagebox.showerror("读取数据库失败", f"无法加载数据库: {e}", parent=self.dlg)
            self.dlg.destroy()
            return

        if not self.apps:
            messagebox.showwarning("提示", "所选数据库中未解析到有效的设备模型记录！", parent=self.dlg)
            self.dlg.destroy()
            return

        # 构建弹窗控件树并预缓存点位数据
        try:
            self._build_ui()
            self._populate_data()
        except Exception as ex:
            import traceback
            logger.error(f"构建导入弹窗界面异常: {ex}\n{traceback.format_exc()}")
            messagebox.showerror("界面加载失败", f"渲染弹窗发生异常:\n{ex}", parent=self.dlg)
            self.dlg.destroy()

    def _set_dialog_icon(self) -> None:
        """为导入弹窗设置与主窗口一致的应用图标。"""
        # 延迟导入 get_app_icon_path 避免模块顶层循环依赖
        from ..main_window import get_app_icon_path
        icon_path = get_app_icon_path()
        if icon_path and os.path.exists(icon_path):
            try:
                self.dlg.iconbitmap(icon_path)
            except Exception:
                pass

    def _build_ui(self) -> None:
        """构建五层弹窗布局：顶部筛选搜索栏、批量勾选工具条、多选设备 Treeview、导入模式单选区及底部操作按钮。"""
        # 1. 顶部控制面板（设备类型筛选单选按钮组 + 右侧快速搜索输入框）
        top_frame = ttk.Frame(self.dlg)
        top_frame.pack(fill="x", padx=14, pady=(10, 4))

        # 筛选单选按钮组：支持按全部模块、Type=1（南向采集设备）、Type=2（北向对外服务）分类过滤
        ttk.Label(top_frame, text="设备分类筛选:", font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 6))
        self.type_var = tk.StringVar(value="all")
        ttk.Radiobutton(top_frame, text="全部业务模块", variable=self.type_var, value="all").pack(side="left", padx=4)
        ttk.Radiobutton(top_frame, text="Type=1 (南向采集设备)", variable=self.type_var, value="1").pack(side="left", padx=4)
        ttk.Radiobutton(top_frame, text="Type=2 (北向对外服务)", variable=self.type_var, value="2").pack(side="left", padx=4)

        # 搜索框：支持按中文名、英文名或 App ID 实时模糊检索
        self.search_var = tk.StringVar()
        entry_search = ttk.Entry(top_frame, textvariable=self.search_var, width=18)
        entry_search.pack(side="right")
        ttk.Label(top_frame, text="🔍 快速搜索:").pack(side="right", padx=(10, 2))

        # 2. 快捷选择操作条：提供一键全选、全选已使能、反选、清空及右侧实时选中计数器
        tools_frame = ttk.Frame(self.dlg)
        tools_frame.pack(fill="x", padx=14, pady=(2, 4))

        ttk.Label(tools_frame, text="批量勾选辅助:", foreground="#6c757d").pack(side="left", padx=(0, 6))
        ttk.Button(tools_frame, text="全选当前列表", style="Small.TButton", command=self._select_all_visible).pack(side="left", padx=3)
        ttk.Button(tools_frame, text="全选已使能设备", style="Small.TButton", command=self._select_all_enabled).pack(side="left", padx=3)
        ttk.Button(tools_frame, text="反向选择", style="Small.TButton", command=self._invert_selection).pack(side="left", padx=3)
        ttk.Button(tools_frame, text="清空选择", style="Small.TButton", command=self._clear_selection).pack(side="left", padx=3)

        self.lbl_selected_status = ttk.Label(tools_frame, text="当前已选中: 0 个设备", foreground="#0d6efd", font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_selected_status.pack(side="right", padx=6)

        # 3. 设备列表表格（使用 selectmode="extended" 支持 Ctrl 跳选、Shift 连选与鼠标拖拽多选）
        self.table_frame = ttk.Frame(self.dlg)
        self.table_frame.pack(fill="both", expand=True, padx=14, pady=4)

        scroll_y = ttk.Scrollbar(self.table_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")
        scroll_x = ttk.Scrollbar(self.table_frame, orient="horizontal")
        scroll_x.pack(side="bottom", fill="x")

        self.tree = ttk.Treeview(
            self.table_frame,
            columns=("id", "enable", "name", "eng_name", "role", "comm", "pt_cnt"),
            show="headings",
            selectmode="extended",  # 核心支持多选：Ctrl、Shift 与鼠标框选
            yscrollcommand=scroll_y.set,
            xscrollcommand=scroll_x.set,
        )
        scroll_y.config(command=self.tree.yview)
        scroll_x.config(command=self.tree.xview)
        self.tree.pack(fill="both", expand=True)

        # 动态自适应列配置：(列ID, 标题, 宽度占比 ratio, 最小像素宽度 min_w, 对齐方式)
        # 为什么显式设置 stretch=False 并由 _on_table_resize 接管列宽分配：
        # Tkinter Treeview 原生 stretch=True 在窗口缩放时经常只拉伸最后一列导致比例失调，手动按比例分配更均匀美观。
        self.col_specs = [
            ("id", "App ID", 0.08, 70, "center"),
            ("enable", "使能状态", 0.11, 95, "center"),
            ("name", "业务设备名称 (中文)", 0.28, 180, "w"),
            ("eng_name", "英文标识 (Name)", 0.24, 160, "w"),
            ("role", "业务方向 / 角色", 0.12, 110, "center"),
            ("comm", "通讯配置参数", 0.11, 110, "center"),
            ("pt_cnt", "点位数量", 0.06, 65, "center"),
        ]
        for cid, chead, ratio, min_w, align in self.col_specs:
            head_align = align if cid in ("name", "eng_name") else "center"
            self.tree.heading(cid, text=chead, anchor=head_align)
            self.tree.column(cid, width=max(min_w, int(960 * ratio)), minwidth=min_w, anchor=align, stretch=False)

        # 绑定容器尺寸变化事件、选中项变化事件与双击快捷导入事件
        self.table_frame.bind("<Configure>", self._on_table_resize)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection_changed)
        self.tree.bind("<Double-1>", lambda event: self._execute_import())

        # 4. 导入模式选择与配额限制提示区
        mode_frame = ttk.LabelFrame(self.dlg, text="导入模式与规则配置 (支持多选一次性批量导入)")
        mode_frame.pack(fill="x", padx=14, pady=6)

        # 4.1 规则配置区内置搜索与已选设备中文名汇总行（满足需求 4 与需求 5）
        mode_search_row = ttk.Frame(mode_frame)
        mode_search_row.pack(fill="x", padx=8, pady=(4, 2))
        ttk.Label(mode_search_row, text="🔍 规则区搜索 (设备中英文/AppID/点位名):").pack(side="left", padx=(0, 4))
        # 搜索命中后导入整台设备的全部点位，不裁剪未命中点位
        self.mode_search_var = tk.StringVar()
        self.entry_mode_search = ttk.Entry(mode_search_row, textvariable=self.mode_search_var, width=24)
        self.entry_mode_search.pack(side="left", padx=(0, 4))
        ttk.Button(
            mode_search_row,
            text="清空",
            style="Small.TButton",
            command=lambda: self.mode_search_var.set(""),
        ).pack(side="left", padx=(0, 12))

        self.lbl_selected_chn_summary = ttk.Label(
            mode_search_row,
            text="已选设备中文名汇总: (未选择)",
            foreground="#198754",
            font=("Microsoft YaHei UI", 9),
        )
        self.lbl_selected_chn_summary.pack(side="left", fill="x", expand=True)

        # 4.2 导入模式单选按钮
        self.import_mode_var = tk.StringVar(value="new_instance")
        ttk.Radiobutton(
            mode_frame,
            text=f"新建为独立从机服务实例 (推荐，支持批量为选中设备各建从机，TCP/RTU 分别上限 {MAX_TCP_SLAVE_INSTANCES} 个)",
            variable=self.import_mode_var,
            value="new_instance",
        ).pack(anchor="w", padx=8, pady=2)

        ttk.Radiobutton(
            mode_frame,
            text="覆盖替换当前从机点表 (清空当前从机点表，完全替换为所选设备的点位合集)",
            variable=self.import_mode_var,
            value="replace",
        ).pack(anchor="w", padx=8, pady=2)

        ttk.Radiobutton(
            mode_frame,
            text="追加合并到当前从机点表 (保留当前从机点表，增量追加所选设备点位)",
            variable=self.import_mode_var,
            value="append",
        ).pack(anchor="w", padx=8, pady=2)

        # 4.3 RTU 串口五要素与缺省寄存器区域规则配置行（满足需求 2、需求 6、需求 7、需求 8）
        rtu_rule_row = ttk.Frame(mode_frame)
        rtu_rule_row.pack(fill="x", padx=8, pady=(4, 6))
        ttk.Label(rtu_rule_row, text="RTU串口规则:", font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 4))

        ttk.Label(rtu_rule_row, text="串口:").pack(side="left")
        avail_ports = SlaveService.list_available_ports() if hasattr(SlaveService, "list_available_ports") else ["COM1", "COM2", "COM3", "COM4"]
        self.combo_slave_com = ttk.Combobox(rtu_rule_row, values=avail_ports, width=10)
        self.combo_slave_com.set(avail_ports[0] if avail_ports else "COM1")
        self.combo_slave_com.pack(side="left", padx=(2, 6))

        ttk.Label(rtu_rule_row, text="波特率:").pack(side="left")
        self.combo_slave_baud = ttk.Combobox(
            rtu_rule_row,
            values=self.BAUDRATE_CHOICES,
            width=7,
        )
        self.combo_slave_baud.set("9600")
        self.combo_slave_baud.pack(side="left", padx=(2, 6))

        ttk.Label(rtu_rule_row, text="数据位:").pack(side="left")
        self.combo_slave_databit = ttk.Combobox(rtu_rule_row, values=["7", "8"], width=3, state="readonly")
        self.combo_slave_databit.set("8")
        self.combo_slave_databit.pack(side="left", padx=(2, 6))

        ttk.Label(rtu_rule_row, text="校验:").pack(side="left")
        self.combo_slave_parity = ttk.Combobox(
            rtu_rule_row,
            values=["None (N)", "Even (E)", "Odd (O)"],
            width=8,
            state="readonly",
        )
        self.combo_slave_parity.set("None (N)")
        self.combo_slave_parity.pack(side="left", padx=(2, 6))

        ttk.Label(rtu_rule_row, text="停止位:").pack(side="left")
        self.combo_slave_stopbit = ttk.Combobox(rtu_rule_row, values=["1", "2"], width=3, state="readonly")
        self.combo_slave_stopbit.set("1")
        self.combo_slave_stopbit.pack(side="left", padx=(2, 10))

        ttk.Label(rtu_rule_row, text="缺省功能区:").pack(side="left")
        self.combo_default_area = ttk.Combobox(
            rtu_rule_row,
            values=self.AREA_CHOICES,
            width=20,
            state="readonly",
        )
        self.combo_default_area.set(AreaType.HOLDING.friendly_name)
        self.combo_default_area.pack(side="left", padx=(2, 0))

        for rtu_w in (
            self.combo_slave_com,
            self.combo_slave_baud,
            self.combo_slave_databit,
            self.combo_slave_parity,
            self.combo_slave_stopbit,
        ):
            rtu_w.bind("<<ComboboxSelected>>", self._on_rtu_rule_user_edited)
            rtu_w.bind("<KeyRelease>", self._on_rtu_rule_user_edited)

        # 5. 底部确认与取消按钮控制区
        btn_frame = ttk.Frame(self.dlg)
        btn_frame.pack(fill="x", padx=14, pady=(6, 12))

        ttk.Button(
            btn_frame,
            text="立即导入点表 (支持批量)",
            style="primary.TButton",
            command=self._execute_import,
        ).pack(side="right", padx=4)

        ttk.Button(
            btn_frame,
            text="取消",
            style="Small.TButton",
            command=self.dlg.destroy,
        ).pack(side="right", padx=4)

        # 监听搜索词（顶部搜索框 + 规则配置区搜索框）与分类单选变量改动，实时触发列表重绘
        self.search_var.trace_add("write", lambda *args: self._filter_tree())
        self.mode_search_var.trace_add("write", lambda *args: self._filter_tree())
        self.type_var.trace_add("write", lambda *args: self._filter_tree())

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_table_resize]（入参: event）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _on_table_resize(self, event=None) -> None:
        """动态感知表格容器宽度变化，通过 6px 死区过滤与 20ms 防抖定时器平滑调度列宽重算。"""
        # 在 [_on_table_resize] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            # 从 [_on_table_resize] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_on_table_resize] 中更新局部变量 [width]：为后续协议编码、服务调度或界面渲染准备中间状态
        width = event.width if event else self.table_frame.winfo_width()
        # 在 [_on_table_resize] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if width < 400:
            # 从 [_on_table_resize] 返回执行结果给调用方，结束当前处理分支
            return

        # 为什么设置 6px 阈值过滤与 20ms after_cancel 防抖：
        # 用户拖拽窗口边缘时每秒会产生数十次 `<Configure>` 事件，过滤微小抖动并合并 20ms 内的重排可彻底消除闪烁卡顿。
        # 在 [_on_table_resize] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if hasattr(self, "_last_rendered_width") and abs(width - self._last_rendered_width) < 6:
            # 从 [_on_table_resize] 返回执行结果给调用方，结束当前处理分支
            return

        # 20ms 防抖调度：丢弃高频连续拖动事件，合并执行，确保窗口拖动丝滑顺畅
        # 在 [_on_table_resize] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if hasattr(self, "_resize_job") and self._resize_job is not None:
            self.dlg.after_cancel(self._resize_job)

        # 在 [_on_table_resize] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._pending_width = width
        # 在 [_on_table_resize] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._resize_job = self.dlg.after(20, self._apply_table_resize)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_apply_table_resize]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _apply_table_resize(self) -> None:
        """根据防抖后确定的容器可用宽度，按预设比例重算并应用每一列的像素宽度。"""
        # 在 [_apply_table_resize] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._resize_job = None
        # 在 [_apply_table_resize] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            # 从 [_apply_table_resize] 返回执行结果给调用方，结束当前处理分支
            return
        # 预留 25px 给右侧垂直滚动条与边框，防止水平滚动条无故出现
        # 在 [_apply_table_resize] 中更新局部变量 [avail_w]：为后续协议编码、服务调度或界面渲染准备中间状态
        avail_w = getattr(self, "_pending_width", self.table_frame.winfo_width()) - 25
        # 在 [_apply_table_resize] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if avail_w < 400:
            # 从 [_apply_table_resize] 返回执行结果给调用方，结束当前处理分支
            return
        # 在 [_apply_table_resize] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._last_rendered_width = self._pending_width
        # 在 [_apply_table_resize] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for cid, _, ratio, min_w, _ in self.col_specs:
            # 在 [_apply_table_resize] 中更新局部变量 [w]：为后续协议编码、服务调度或界面渲染准备中间状态
            w = max(min_w, int(avail_w * ratio))
            self.tree.column(cid, width=w)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_populate_data]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _populate_data(self) -> None:
        """在弹窗打开时一次性预解析所有设备的点位与展示标签并缓存至 `self.app_item_map`。"""
        # 为什么预先构建 app_item_map 缓存而非每次搜索时调用 extract_studio_points()：
        # 避免用户在搜索框连续打字时反复解析数千个 JSON 点位，保证搜索与分类切换达到毫秒级响应。
        self.app_item_map: Dict[int, Tuple[Any, List[Dict[str, Any]], str, str, str, str]] = {}
        # 在 [_populate_data] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for a in self.apps:
            # 在 [_populate_data] 中更新局部变量 [pts]：为后续协议编码、服务调度或界面渲染准备中间状态
            pts = a.extract_studio_points()
            # 在 [_populate_data] 中更新局部变量 [role_tag]：为后续协议编码、服务调度或界面渲染准备中间状态
            role_tag = "南向物理设备 (Type=1)" if a.is_south_master else "北向转发从机 (Type=2)" if a.is_north_slave else f"业务模块 (Type={a.app_type})"
            # 在 [_populate_data] 中更新局部变量 [comm_str]：为后续协议编码、服务调度或界面渲染准备中间状态
            comm_str = f"串口 {a.serial_info.get('port_name', 'RS-485')}" if a.comm_type == "RTU" else f"TCP {a.ip}:{a.port}"
            # 在 [_populate_data] 中更新局部变量 [pt_count_str]：为后续协议编码、服务调度或界面渲染准备中间状态
            pt_count_str = f"{len(pts)} 点"
            # 在 [_populate_data] 中更新局部变量 [enable_tag]：为后续协议编码、服务调度或界面渲染准备中间状态
            enable_tag = "✅ 已使能" if a.enable == 1 else "⚪ 未使能"
            self.app_item_map[a.app_id] = (a, pts, role_tag, comm_str, pt_count_str, enable_tag)

        self._filter_tree()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_rtu_rule_user_edited]（入参: event）：记录用户已手动修改 RTU 规则控件
    # ──────────────────────────────────────────────────────────────────
    def _on_rtu_rule_user_edited(self, event=None) -> None:
        """当用户在导入规则区手动修改串口、波特率、数据位、校验位或停止位时，标记覆盖标志位。"""
        if not getattr(self, "_syncing_rtu_ui", False):
            self._rtu_user_overridden = True

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_filter_tree]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _filter_tree(self) -> None:
        """根据当前设备分类、顶部搜索词与规则区搜索词过滤刷新 Treeview，并在特定分类下智能预选或恢复勾选状态。"""
        current_sel = list(self.tree.selection())
        kw_top = self.search_var.get().strip().lower()
        kw_mode = self.mode_search_var.get().strip().lower() if hasattr(self, "mode_search_var") else ""
        has_active_search = bool(kw_top or kw_mode)

        if has_active_search and self._saved_selection_before_search is None and current_sel:
            self._saved_selection_before_search = current_sel

        # 在 [_filter_tree] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for item in self.tree.get_children():
            self.tree.delete(item)

        # 在 [_filter_tree] 中更新局部变量 [sel_type]：为后续协议编码、服务调度或界面渲染准备中间状态
        sel_type = self.type_var.get()

        # 在 [_filter_tree] 中更新局部变量 [first_item]：为后续协议编码、服务调度或界面渲染准备中间状态
        first_item = None
        # 在 [_filter_tree] 中更新局部变量 [enabled_items]：为后续协议编码、服务调度或界面渲染准备中间状态
        enabled_items = []
        visible_items: List[str] = []
        # 在 [_filter_tree] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for app_id, (a, pts, role_tag, comm_str, pt_count_str, enable_tag) in self.app_item_map.items():
            # 按 Type=1 / Type=2 过滤
            # 在 [_filter_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if sel_type == "1" and a.app_type != 1:
                continue
            # 在 [_filter_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if sel_type == "2" and a.app_type != 2:
                continue

            # 严格区分中文名与英文标识：若无独立中文名则显示占位符“—（无中文）”，不再把英文名塞入中文名列（需求 4）
            raw_chn = (a.chinese_name or "").strip()
            raw_eng = (a.english_name or "").strip()
            disp_name = raw_chn if (raw_chn and raw_chn != raw_eng) else "—（无中文）"

            # 顶部搜索框匹配：支持按中文名、英文名或 App ID 大小写不敏感子串匹配
            if kw_top and (
                kw_top not in raw_chn.lower()
                and kw_top not in raw_eng.lower()
                and kw_top not in str(app_id).lower()
            ):
                continue

            # 规则区搜索框匹配：支持按设备中文名、英文名、App ID 或所属点位描述/地址大小写不敏感子串匹配（需求 5）
            if kw_mode:
                dev_match = (
                    kw_mode in raw_chn.lower()
                    or kw_mode in raw_eng.lower()
                    or kw_mode in str(app_id).lower()
                )
                pt_match = any(
                    kw_mode in str(pt.get("desc", "")).lower()
                    or kw_mode in str(pt.get("address", "")).lower()
                    for pt in pts
                )
                if not (dev_match or pt_match):
                    continue

            # 将 app_id 字符串直接作为 Treeview 行 iid，方便后续 selection() 直接反查 app_item_map
            # 在 [_filter_tree] 中更新局部变量 [item_id]：为后续协议编码、服务调度或界面渲染准备中间状态
            item_id = self.tree.insert(
                "",
                "end",
                iid=str(app_id),
                values=(app_id, enable_tag, disp_name, a.english_name, role_tag, comm_str, pt_count_str),
            )
            visible_items.append(item_id)
            # 在 [_filter_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if first_item is None:
                # 在 [_filter_tree] 中更新局部变量 [first_item]：为后续协议编码、服务调度或界面渲染准备中间状态
                first_item = item_id
            # 在 [_filter_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if a.enable == 1:
                enabled_items.append(item_id)

        # 若刚清空搜索框且存在搜索前保存的勾选集合，优先恢复之前的勾选状态（需求 5）
        if not has_active_search and self._saved_selection_before_search is not None:
            restored = [iid for iid in self._saved_selection_before_search if iid in visible_items]
            self._saved_selection_before_search = None
            if restored:
                self.tree.selection_set(restored)
                self.tree.focus(restored[0])
                self._on_tree_selection_changed()
                return

        # 为什么在 Type=1 或 Type=2 时默认全选“已使能”设备，而在 all 模式下仅单选第一项：
        # 1. 当工程师明确切到“南向采集设备”分类时，通常希望一键把现场启用的所有南向设备批量导入模拟；
        # 2. 而在默认的“全部业务模块”视图下同时包含南北向和未使能模板，仅单选首项可避免误操作一次性导入过多无关模块。
        # 在 [_filter_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if sel_type in ("1", "2"):
            # 在 [_filter_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if enabled_items:
                self.tree.selection_set(enabled_items)
                self.tree.focus(enabled_items[0])
            # 在 [_filter_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            elif first_item:
                self.tree.selection_set(first_item)
                self.tree.focus(first_item)
            else:
                self.tree.selection_set([])
        else:
            # 在 [_filter_tree] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if first_item:
                self.tree.selection_set(first_item)
                self.tree.focus(first_item)
            else:
                self.tree.selection_set([])

        self._on_tree_selection_changed()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_on_tree_selection_changed]（入参: event）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _on_tree_selection_changed(self, event=None) -> None:
        """当 Treeview 选中行集合发生变化时，实时更新已选中设备数量提示、中文名汇总与 RTU 串口规则回显。"""
        selected_ids = list(self.tree.selection())
        # 在 [_on_tree_selection_changed] 中更新局部变量 [selected_count]：为后续协议编码、服务调度或界面渲染准备中间状态
        selected_count = len(selected_ids)
        self.lbl_selected_status.config(text=f"当前已选中: {selected_count} 个设备")

        # 实时刷新规则配置区的“已选设备中文名汇总”（需求 4）
        if hasattr(self, "lbl_selected_chn_summary"):
            chn_names: List[str] = []
            for sel_str in selected_ids:
                try:
                    aid = int(sel_str)
                    if aid in self.app_item_map:
                        app_obj = self.app_item_map[aid][0]
                        c_name = (app_obj.chinese_name or "").strip()
                        e_name = (app_obj.english_name or "").strip()
                        chn_names.append(c_name if (c_name and c_name != e_name) else f"{e_name}(无中文)")
                except Exception:
                    pass
            if not chn_names:
                summary_txt = "已选设备中文名汇总: (未选择)"
            elif len(chn_names) <= 4:
                summary_txt = "已选设备中文名汇总: " + "、".join(chn_names)
            else:
                summary_txt = "已选设备中文名汇总: " + "、".join(chn_names[:4]) + f" 等 {len(chn_names)} 台"
            self.lbl_selected_chn_summary.config(text=summary_txt)

        # 当用户在列表中聚焦/选中单个设备且尚未手动覆盖 RTU 规则时，将数据库的串口参数同步回显到规则区控件
        if len(selected_ids) == 1 and not getattr(self, "_rtu_user_overridden", False):
            try:
                aid = int(selected_ids[0])
                if aid in self.app_item_map:
                    app_obj = self.app_item_map[aid][0]
                    s_info = getattr(app_obj, "serial_info", {}) or {}
                    self._syncing_rtu_ui = True
                    try:
                        if hasattr(self, "combo_slave_com"):
                            p_name = str(s_info.get("port_name", "") or "").strip()
                            if p_name:
                                self.combo_slave_com.set(p_name)
                        if hasattr(self, "combo_slave_baud"):
                            b_val = str(s_info.get("baudrate", "9600") or "9600").strip()
                            self.combo_slave_baud.set(b_val)
                        if hasattr(self, "combo_slave_databit"):
                            d_val = str(s_info.get("databit", "8") or "8").strip()
                            self.combo_slave_databit.set(d_val if d_val in ("7", "8") else "8")
                        if hasattr(self, "combo_slave_parity"):
                            par_raw = str(s_info.get("parity", "N") or "N").strip().upper()
                            par_map = {"N": "None (N)", "E": "Even (E)", "O": "Odd (O)"}
                            self.combo_slave_parity.set(par_map.get(par_raw, "None (N)"))
                        if hasattr(self, "combo_slave_stopbit"):
                            sb_raw = str(int(float(str(s_info.get("stopbit", 1) or 1))))
                            self.combo_slave_stopbit.set(sb_raw if sb_raw in ("1", "2") else "1")
                    finally:
                        self._syncing_rtu_ui = False
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_select_all_visible]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _select_all_visible(self) -> None:
        """一键全选当前过滤列表中可见的所有设备行。"""
        # 在 [_select_all_visible] 中更新局部变量 [all_items]：为后续协议编码、服务调度或界面渲染准备中间状态
        all_items = self.tree.get_children()
        self.tree.selection_set(all_items)
        if self._saved_selection_before_search is not None:
            self._saved_selection_before_search = list(all_items)
        self._on_tree_selection_changed()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_select_all_enabled]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _select_all_enabled(self) -> None:
        """一键选中当前可见列表中所有标记为“已使能 (enable=1)”的设备行。"""
        # 在 [_select_all_enabled] 中更新局部变量 [enabled_items]：为后续协议编码、服务调度或界面渲染准备中间状态
        enabled_items = []
        # 在 [_select_all_enabled] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for item in self.tree.get_children():
            # 在 [_select_all_enabled] 中更新局部变量 [vals]：为后续协议编码、服务调度或界面渲染准备中间状态
            vals = self.tree.item(item, "values")
            # 在 [_select_all_enabled] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if vals and len(vals) > 1 and "已使能" in str(vals[1]):
                enabled_items.append(item)
        self.tree.selection_set(enabled_items)
        if self._saved_selection_before_search is not None:
            self._saved_selection_before_search = list(enabled_items)
        self._on_tree_selection_changed()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_invert_selection]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _invert_selection(self) -> None:
        """对当前可见列表执行集合差集反选操作。"""
        # 在 [_invert_selection] 中更新局部变量 [all_items]：为后续协议编码、服务调度或界面渲染准备中间状态
        all_items = set(self.tree.get_children())
        # 在 [_invert_selection] 中更新局部变量 [selected]：为后续协议编码、服务调度或界面渲染准备中间状态
        selected = set(self.tree.selection())
        # 在 [_invert_selection] 中更新局部变量 [new_sel]：为后续协议编码、服务调度或界面渲染准备中间状态
        new_sel = list(all_items - selected)
        self.tree.selection_set(new_sel)
        if self._saved_selection_before_search is not None:
            self._saved_selection_before_search = list(new_sel)
        self._on_tree_selection_changed()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_clear_selection]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _clear_selection(self) -> None:
        """清空当前 Treeview 中的所有选中项。"""
        self.tree.selection_set([])
        self._saved_selection_before_search = None
        self._on_tree_selection_changed()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_execute_import]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _execute_import(self) -> None:
        """校验当前选中项并根据所选导入模式分发至“批量新建独立从机”或“替换/追加至当前从机”。"""
        # 在 [_execute_import] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [_execute_import] 中更新局部变量 [selected_ids]：为后续协议编码、服务调度或界面渲染准备中间状态
            selected_ids = list(self.tree.selection())
            # 在 [_execute_import] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not selected_ids:
                messagebox.showwarning("提示", "请先在列表中选中至少一个业务设备！", parent=self.dlg)
                # 从 [_execute_import] 返回执行结果给调用方，结束当前处理分支
                return

            # 在 [_execute_import] 中更新局部变量 [mode]：为后续协议编码、服务调度或界面渲染准备中间状态
            mode = self.import_mode_var.get()
            # 在 [_execute_import] 中更新局部变量 [selected_apps_data]：为后续协议编码、服务调度或界面渲染准备中间状态
            selected_apps_data = []
            # 在 [_execute_import] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for sel_str in selected_ids:
                # 在 [_execute_import] 中更新局部变量 [app_id]：为后续协议编码、服务调度或界面渲染准备中间状态
                app_id = int(sel_str)
                # 在 [_execute_import] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if app_id in self.app_item_map:
                    selected_apps_data.append(self.app_item_map[app_id])

            # 在 [_execute_import] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not selected_apps_data:
                # 从 [_execute_import] 返回执行结果给调用方，结束当前处理分支
                return

            # 在 [_execute_import] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if mode == "new_instance":
                self._import_as_new_instances(selected_apps_data)
            else:
                self._import_merge_to_current(selected_apps_data, is_replace=(mode == "replace"))

        except Exception as ex:
            import traceback
            logger.error(f"批量导入执行发生异常: {ex}\n{traceback.format_exc()}")
            messagebox.showerror("导入失败", f"导入过程中发生异常:\n{ex}", parent=self.dlg)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_import_as_new_instances]（入参: apps_data_list）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _import_as_new_instances(self, apps_data_list: List[Tuple[Any, List[Dict[str, Any]], str, str, str, str]]) -> None:
        """为选中的每个业务设备分别创建独立的从机服务实例，严格执行 TCP/RTU 独立 100 实例配额与同端点站号防冲突分配。"""
        # 步骤拆解：
        # 1. 统计现有及拟导入的 TCP 与 RTU 设备数，校验是否超过 MAX_TCP_SLAVE_INSTANCES / MAX_RTU_SLAVE_INSTANCES (100)；
        # 2. 若多选超过 1 台设备，弹出二次确认框；
        # 3. 收集已有设备的 `(物理端点, 站号)` 集合，为每台新设备分配不冲突的 Unit ID；
        # 4. 逐台构建 SlaveDevice、加载点表并注册至 SlaveService，最后销毁弹窗并回调刷新主界面。
        # 在 [_import_as_new_instances] 中更新局部变量 [slave_svc]：为后续协议编码、服务调度或界面渲染准备中间状态
        slave_svc = self.app.slave_service
        # 在 [_import_as_new_instances] 中更新局部变量 [current_tcp_cnt]：为后续协议编码、服务调度或界面渲染准备中间状态
        current_tcp_cnt = slave_svc.count_devices_by_comm(CommType.TCP)
        # 在 [_import_as_new_instances] 中更新局部变量 [current_rtu_cnt]：为后续协议编码、服务调度或界面渲染准备中间状态
        current_rtu_cnt = slave_svc.count_devices_by_comm(CommType.RTU)

        # 在 [_import_as_new_instances] 中更新局部变量 [to_import_tcp]：为后续协议编码、服务调度或界面渲染准备中间状态
        to_import_tcp = sum(1 for (a, _, _, _, _, _) in apps_data_list if str(a.comm_type).upper() != "RTU")
        # 在 [_import_as_new_instances] 中更新局部变量 [to_import_rtu]：为后续协议编码、服务调度或界面渲染准备中间状态
        to_import_rtu = sum(1 for (a, _, _, _, _, _) in apps_data_list if str(a.comm_type).upper() == "RTU")

        # 为什么在创建任何实例前先执行整批配额预检：
        # 保证批量导入的原子性——若拟导入数量加上已有数量超出 100 上限，直接整批拒绝并提示，避免只导入一半造成状态混乱。
        # 在 [_import_as_new_instances] 中更新局部变量 [exceed_messages]：为后续协议编码、服务调度或界面渲染准备中间状态
        exceed_messages = []
        # 在 [_import_as_new_instances] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if current_tcp_cnt + to_import_tcp > MAX_TCP_SLAVE_INSTANCES:
            exceed_messages.append(
                f"Modbus TCP 从机将超限: 当前已有 {current_tcp_cnt} 个，拟导入 {to_import_tcp} 个，上限为 {MAX_TCP_SLAVE_INSTANCES} 个！"
            )
        # 在 [_import_as_new_instances] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if current_rtu_cnt + to_import_rtu > MAX_RTU_SLAVE_INSTANCES:
            exceed_messages.append(
                f"Modbus RTU 从机将超限: 当前已有 {current_rtu_cnt} 个，拟导入 {to_import_rtu} 个，上限为 {MAX_RTU_SLAVE_INSTANCES} 个！"
            )

        # 在 [_import_as_new_instances] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if exceed_messages:
            # 在 [_import_as_new_instances] 中更新局部变量 [msg]：为后续协议编码、服务调度或界面渲染准备中间状态
            msg = "\n".join(exceed_messages)
            msg += "\n\n系统设置每个通信协议最多承载 100 个从机服务实例以确保工业通信稳定性。请缩减所选设备或清理现有从机后再试！"
            messagebox.showerror("实例配额限制", msg, parent=self.dlg)
            # 从 [_import_as_new_instances] 返回执行结果给调用方，结束当前处理分支
            return

        # 多选批量导入前弹出确认提示，列明 TCP 与 RTU 数量明细
        # 在 [_import_as_new_instances] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if len(apps_data_list) > 1:
            # 在 [_import_as_new_instances] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if not messagebox.askyesno(
                "确认批量导入",
                f"确定要一次性为选中的 {len(apps_data_list)} 个设备分别创建独立的从机服务实例吗？\n"
                f"- TCP 从机设备: {to_import_tcp} 个\n"
                f"- RTU 串口设备: {to_import_rtu} 个",
                parent=self.dlg,
            ):
                # 从 [_import_as_new_instances] 返回执行结果给调用方，结束当前处理分支
                return

        created_devices: List[SlaveDevice] = []
        # 在 [_import_as_new_instances] 中更新局部变量 [total_points_imported]：为后续协议编码、服务调度或界面渲染准备中间状态
        total_points_imported = 0
        # 在 [_import_as_new_instances] 中更新局部变量 [existing_devices]：为后续协议编码、服务调度或界面渲染准备中间状态
        existing_devices = slave_svc.get_devices()
        # 在 [_import_as_new_instances] 中更新局部变量 [next_unit]：为后续协议编码、服务调度或界面渲染准备中间状态
        next_unit = len(existing_devices) + 1
        # 在 [_import_as_new_instances] 中更新局部变量 [base_port]：为后续协议编码、服务调度或界面渲染准备中间状态
        base_port = 502 + len(existing_devices)

        # 为什么维护 used_endpoint_units `Set[Tuple[str, int]]`：
        # 同一 RS-485 总线（如 RS-485(A1/B1)）或同一 TCP 端口下可能挂载多台设备；
        # 若数据库中未配站号或两台设备站号意外重号，自动顺延寻找当前端点下最小未占用的站号 `cand`，确保后续“全部启动”零冲突。
        # 在 [_import_as_new_instances] 中更新局部变量 [used_endpoint_units]：为后续协议编码、服务调度或界面渲染准备中间状态
        used_endpoint_units = set()
        # 在 [_import_as_new_instances] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for dev in existing_devices:
            # 在 [_import_as_new_instances] 中更新局部变量 [cfg_e]：为后续协议编码、服务调度或界面渲染准备中间状态
            cfg_e = dev.conn_config
            # 在 [_import_as_new_instances] 中更新局部变量 [ep_tag]：为后续协议编码、服务调度或界面渲染准备中间状态
            ep_tag = cfg_e.com_port.upper().strip() if cfg_e.comm_type == CommType.RTU else f"{cfg_e.host}:{cfg_e.port}"
            used_endpoint_units.add((ep_tag, cfg_e.unit_id))

        # 从导入规则区 UI 控件读取串口五要素配置（需求 6、需求 7、需求 8），严禁在 ConnectionConfig 中写死固定常量
        ui_com = self.combo_slave_com.get().strip() if hasattr(self, "combo_slave_com") else "COM1"
        ui_baud_str = self.combo_slave_baud.get().strip() if hasattr(self, "combo_slave_baud") else "9600"
        ui_databit_str = self.combo_slave_databit.get().strip() if hasattr(self, "combo_slave_databit") else "8"
        ui_parity_str = self.combo_slave_parity.get().strip() if hasattr(self, "combo_slave_parity") else "None (N)"
        ui_stopbit_str = self.combo_slave_stopbit.get().strip() if hasattr(self, "combo_slave_stopbit") else "1"

        try:
            ui_baud = int(ui_baud_str or 9600)
        except Exception:
            ui_baud = 9600
        try:
            ui_databit = int(ui_databit_str or 8)
        except Exception:
            ui_databit = 8
        if "Even" in ui_parity_str or ui_parity_str.upper() == "E":
            ui_parity = "E"
        elif "Odd" in ui_parity_str or ui_parity_str.upper() == "O":
            ui_parity = "O"
        else:
            ui_parity = "N"
        try:
            ui_stopbit = int(float(ui_stopbit_str or 1))
        except Exception:
            ui_stopbit = 1

        use_ui_rtu_override = getattr(self, "_rtu_user_overridden", False) or len(apps_data_list) == 1

        # 在 [_import_as_new_instances] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for idx, (sel_app, points_data, role_tag, comm_str, pt_cnt_str, enable_tag) in enumerate(apps_data_list):
            # 在 [_import_as_new_instances] 中更新局部变量 [is_rtu]：为后续协议编码、服务调度或界面渲染准备中间状态
            is_rtu = (str(sel_app.comm_type).upper() == "RTU")
            # 在 [_import_as_new_instances] 中更新局部变量 [comm_mode]：为后续协议编码、服务调度或界面渲染准备中间状态
            comm_mode = CommType.RTU if is_rtu else CommType.TCP
            # 在 [_import_as_new_instances] 中更新局部变量 [s_info]：为后续协议编码、服务调度或界面渲染准备中间状态
            s_info = getattr(sel_app, "serial_info", {}) or {}

            # 解析串口五要素参数（波特率、数据位、校验位、停止位、串口名）：
            # 当用户手动修改了规则区 RTU 控件或单选导入时直接采用 UI 控件当前值；多选未手动覆盖时以 UI 控件值作为缺省兜底。
            if use_ui_rtu_override:
                baud = ui_baud
                databit = ui_databit
                parity = ui_parity
                stopbit = ui_stopbit
                com_name = ui_com or str(s_info.get("port_name", "COM1") or "COM1")
            else:
                baud = ui_baud
                try:
                    baud = int(s_info.get("baudrate", ui_baud) or ui_baud)
                except Exception:
                    pass

                databit = ui_databit
                try:
                    databit = int(s_info.get("databit", ui_databit) or ui_databit)
                except Exception:
                    pass

                parity = str(s_info.get("parity", ui_parity) or ui_parity).strip().upper()
                if parity not in ("N", "E", "O"):
                    parity = ui_parity

                stopbit = ui_stopbit
                try:
                    stopbit = int(float(str(s_info.get("stopbit", ui_stopbit) or ui_stopbit)))
                except Exception:
                    pass

                com_name = str(s_info.get("port_name", ui_com or "COM1") or ui_com or "COM1")

            # 在 [_import_as_new_instances] 中更新局部变量 [ip_addr]：为后续协议编码、服务调度或界面渲染准备中间状态
            ip_addr = getattr(sel_app, "ip", "127.0.0.1") or "127.0.0.1"
            # 在 [_import_as_new_instances] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
            try:
                # 在 [_import_as_new_instances] 中更新局部变量 [port_num]：为后续协议编码、服务调度或界面渲染准备中间状态
                port_num = int(getattr(sel_app, "port", base_port + idx) or (base_port + idx))
            except Exception:
                # 在 [_import_as_new_instances] 中更新局部变量 [port_num]：为后续协议编码、服务调度或界面渲染准备中间状态
                port_num = base_port + idx

            # 在 [_import_as_new_instances] 中更新局部变量 [ep_key]：为后续协议编码、服务调度或界面渲染准备中间状态
            ep_key = com_name.upper().strip() if comm_mode == CommType.RTU else f"{ip_addr}:{port_num}"

            # 站号分配：优先采用数据库中配置的真实从机站地址；若冲突或未指定，顺延分配当前端点下未被占用的站号
            # 在 [_import_as_new_instances] 中更新局部变量 [db_unit]：为后续协议编码、服务调度或界面渲染准备中间状态
            db_unit = getattr(sel_app, "slave_id", None)
            # 在 [_import_as_new_instances] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if db_unit is not None and (ep_key, db_unit) not in used_endpoint_units:
                # 在 [_import_as_new_instances] 中更新局部变量 [unit_id]：为后续协议编码、服务调度或界面渲染准备中间状态
                unit_id = db_unit
            else:
                # 在 [_import_as_new_instances] 中更新局部变量 [cand]：为后续协议编码、服务调度或界面渲染准备中间状态
                cand = 1
                # 在 [_import_as_new_instances] 中维持受控循环：配合运行标志位或事件信号量实现可持续中断的周期性处理
                while (ep_key, cand) in used_endpoint_units:
                    cand += 1
                # 在 [_import_as_new_instances] 中更新局部变量 [unit_id]：为后续协议编码、服务调度或界面渲染准备中间状态
                unit_id = cand
            used_endpoint_units.add((ep_key, unit_id))

            # 在 [_import_as_new_instances] 中更新局部变量 [disp_name]：为后续协议编码、服务调度或界面渲染准备中间状态
            disp_name = sel_app.chinese_name or sel_app.english_name
            # 在 [_import_as_new_instances] 中更新局部变量 [new_name]：为后续协议编码、服务调度或界面渲染准备中间状态
            new_name = f"从机{unit_id} [{disp_name}]"
            # 在 [_import_as_new_instances] 中更新局部变量 [new_id]：为后续协议编码、服务调度或界面渲染准备中间状态
            new_id = f"slave_{time.time_ns()}_{idx}"

            # 在 [_import_as_new_instances] 中更新局部变量 [cfg]：为后续协议编码、服务调度或界面渲染准备中间状态
            cfg = ConnectionConfig(
                comm_type=comm_mode,
                host=ip_addr,
                port=port_num,
                com_port=com_name,
                baudrate=baud,
                data_bits=databit,
                parity=parity,
                stop_bits=stopbit,
                unit_id=unit_id,
            )

            # 在 [_import_as_new_instances] 中更新局部变量 [device]：为后续协议编码、服务调度或界面渲染准备中间状态
            device = SlaveDevice(
                id=new_id,
                name=new_name,
                conn_config=cfg,
                db_device_id=sel_app.app_id,
                db_device_name=disp_name,
            )

            # 逐点校验并装载到新建的从机实体中
            # 在 [_import_as_new_instances] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for pt in points_data:
                self._load_single_point_to_device(device, pt)

            ok, err_msg = slave_svc.register_device(device)
            # 在 [_import_as_new_instances] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if ok:
                created_devices.append(device)
                total_points_imported += len(device.points)
            else:
                logger.error(f"注册从机实例失败 [{new_name}]: {err_msg}")

        self.dlg.destroy()

        # 在 [_import_as_new_instances] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if created_devices:
            # 在 [_import_as_new_instances] 中更新局部变量 [last_dev]：为后续协议编码、服务调度或界面渲染准备中间状态
            last_dev = created_devices[-1]
            self.on_imported(last_dev)

            # 在 [_import_as_new_instances] 中更新局部变量 [summary_msg]：为后续协议编码、服务调度或界面渲染准备中间状态
            summary_msg = (
                f"批量导入成功完成！\n\n"
                f"已一次性创建 {len(created_devices)} 个独立从机服务实例：\n"
                f"- Modbus TCP 实例: {to_import_tcp} 个 (当前总数: {slave_svc.count_devices_by_comm(CommType.TCP)}/{MAX_TCP_SLAVE_INSTANCES})\n"
                f"- Modbus RTU 实例: {to_import_rtu} 个 (当前总数: {slave_svc.count_devices_by_comm(CommType.RTU)}/{MAX_RTU_SLAVE_INSTANCES})\n"
                f"- 累计成功装载点位: {total_points_imported} 个"
            )
            messagebox.showinfo("批量导入完成", summary_msg, parent=self.parent.winfo_toplevel())
            self.app.logging_service.post(
                "SYS",
                0,
                f"批量导入完成: 一次性创建 {len(created_devices)} 个从机实例，累计装载 {total_points_imported} 个点位",
            )

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_import_merge_to_current]（入参: apps_data_list, is_replace）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _import_merge_to_current(
        self,
        apps_data_list: List[Tuple[Any, List[Dict[str, Any]], str, str, str, str]],
        is_replace: bool,
    ) -> None:
        """将所选设备的点表覆盖替换 (`is_replace=True`) 或增量追加 (`is_replace=False`) 到主界面当前选中的从机实例。"""
        # 在 [_import_merge_to_current] 中更新局部变量 [slave_svc]：为后续协议编码、服务调度或界面渲染准备中间状态
        slave_svc = self.app.slave_service
        # 在 [_import_merge_to_current] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not self.current_device_id:
            messagebox.showwarning("提示", "当前主界面未选定目标从机实例！", parent=self.dlg)
            # 从 [_import_merge_to_current] 返回执行结果给调用方，结束当前处理分支
            return

        # 在 [_import_merge_to_current] 中更新局部变量 [target_dev]：为后续协议编码、服务调度或界面渲染准备中间状态
        target_dev = slave_svc.get_device(self.current_device_id)
        # 在 [_import_merge_to_current] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not target_dev:
            messagebox.showerror("错误", f"未找到当前从机实例: {self.current_device_id}", parent=self.dlg)
            # 从 [_import_merge_to_current] 返回执行结果给调用方，结束当前处理分支
            return

        # 若为覆盖替换模式，先清空目标从机现有点表并按前 3 个导入设备名称自动更新从机显示名
        # 在 [_import_merge_to_current] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if is_replace:
            target_dev.points.clear()
            # 在 [_import_merge_to_current] 中更新局部变量 [names]：为后续协议编码、服务调度或界面渲染准备中间状态
            names = [sel_app.chinese_name or sel_app.english_name for (sel_app, _, _, _, _, _) in apps_data_list]
            # 在 [_import_merge_to_current] 中更新局部变量 [summary_name]：为后续协议编码、服务调度或界面渲染准备中间状态
            summary_name = " + ".join(names[:3]) + (f" 等{len(names)}个设备" if len(names) > 3 else "")
            # 在 [_import_merge_to_current] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            target_dev.name = f"从机{target_dev.conn_config.unit_id} [{summary_name}]"

        # 在 [_import_merge_to_current] 中更新局部变量 [imported_cnt]：为后续协议编码、服务调度或界面渲染准备中间状态
        imported_cnt = 0
        # 在 [_import_merge_to_current] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for sel_app, points_data, role_tag, comm_str, pt_cnt_str, enable_tag in apps_data_list:
            # 在 [_import_merge_to_current] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
            for pt in points_data:
                # 在 [_import_merge_to_current] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if self._load_single_point_to_device(target_dev, pt):
                    imported_cnt += 1

        self.dlg.destroy()
        self.on_imported(target_dev)

        # 在 [_import_merge_to_current] 中更新局部变量 [mode_text]：为后续协议编码、服务调度或界面渲染准备中间状态
        mode_text = "覆盖替换" if is_replace else "追加合并"
        # 在 [_import_merge_to_current] 中更新局部变量 [msg]：为后续协议编码、服务调度或界面渲染准备中间状态
        msg = f"已成功完成【{mode_text}】！\n\n目标从机: [{target_dev.name}]\n当前总点位数: {len(target_dev.points)} 点"
        messagebox.showinfo("导入完成", msg, parent=self.parent.winfo_toplevel())
        self.app.logging_service.post("SYS", target_dev.conn_config.unit_id, f"成功完成点表{mode_text}，当前点位总数: {len(target_dev.points)}")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_load_single_point_to_device]（入参: target_dev, pt）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _load_single_point_to_device(self, target_dev: SlaveDevice, pt: Dict[str, Any]) -> bool:
        """调用 PointService 校验单个点位字典并写入目标 SlaveDevice（若从机正在运行则同步热推至底层引擎）。"""
        # 在 [_load_single_point_to_device] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [_load_single_point_to_device] 中更新局部变量 [addr]：为后续协议编码、服务调度或界面渲染准备中间状态
            addr = int(pt.get("address", 0))
            # 在 [_load_single_point_to_device] 中更新局部变量 [desc]：为后续协议编码、服务调度或界面渲染准备中间状态
            desc = str(pt.get("desc", f"Point_{addr}"))
            # 在 [_load_single_point_to_device] 中更新局部变量 [dt]：为后续协议编码、服务调度或界面渲染准备中间状态
            dt = pt.get("data_type", "INT16")
            # 在 [_load_single_point_to_device] 中更新局部变量 [bo]：为后续协议编码、服务调度或界面渲染准备中间状态
            bo = pt.get("byte_order", "CDAB")
            # 在 [_load_single_point_to_device] 中更新局部变量 [area]：为后续协议编码、服务调度或界面渲染准备中间状态
            area = pt.get("area", "4x")
            # 在 [_load_single_point_to_device] 中更新局部变量 [val]：为后续协议编码、服务调度或界面渲染准备中间状态
            val = pt.get("current_val", 0)
            # 在 [_load_single_point_to_device] 中更新局部变量 [sim]：为后续协议编码、服务调度或界面渲染准备中间状态
            sim = pt.get("sim_mode", "固定")
            # 在 [_load_single_point_to_device] 中更新局部变量 [scale]：为后续协议编码、服务调度或界面渲染准备中间状态
            scale = float(pt.get("scale", 1.0) or 1.0)

            ok, point, _ = PointService.validate_and_build_point(
                address=addr,
                description=desc,
                area=area,
                data_type=dt,
                byte_order=bo,
                val_input=val,
                sim_rule=sim,
                scale=scale,
            )
            # 在 [_load_single_point_to_device] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if ok and point:
                target_dev.add_point(point)
                # 若在覆盖或追加导入时目标从机已处于运行状态，立即将新点位热推入底层 DataBlock
                # 在 [_load_single_point_to_device] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if target_dev.is_running:
                    self.app.slave_service.sync_point_to_runtime(target_dev.id, point)
                # 从 [_load_single_point_to_device] 返回执行结果给调用方，结束当前处理分支
                return True
            # 从 [_load_single_point_to_device] 返回执行结果给调用方，结束当前处理分支
            return False
        except Exception:
            # 从 [_load_single_point_to_device] 返回执行结果给调用方，结束当前处理分支
            return False

