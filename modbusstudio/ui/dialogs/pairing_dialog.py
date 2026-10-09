"""
Modbus Studio - 业务配对关系中心对话框 (PairingDialog)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
展示北向对外服务与南向物理采集设备之间的数据映射与路由规则。
"""

from __future__ import annotations
import os
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, List

from ...hems_pairing import HemsPairingEngine, PairingRule


class PairingDialog:
    """业务配对关系中心对话框

    职责：
    1. 动态加载并呈现 HEMS / 业务数据库中的北向服务与南向设备路由配对规则；
    2. 提供规则编号、目标寄存器、数据类型及端到端映射明细的快速搜索与过滤；
    3. 支持窗口动态缩放与图标继承。
    """

    def __init__(self, parent: tk.Widget, app: Any) -> None:
        """初始化配对关系对话框

        Args:
            parent: 父级容器组件
            app: 应用程序全局上下文对象 (包含 db_path 等)
        """
        self.parent = parent
        self.app = app

        if not self.app.db_path or not os.path.exists(self.app.db_path):
            messagebox.showwarning("提示", "尚未关联有效的数据库文件，请先关联数据库！", parent=parent)
            return

        self.dlg = tk.Toplevel(parent)
        self.dlg.title("业务配对关系中心 (基于 app 表 More 字段配置)")
        self.dlg.geometry("900x540")
        self.dlg.minsize(740, 420)
        self.dlg.transient(parent.winfo_toplevel())
        self.dlg.grab_set()

        self._set_dialog_icon()
        self._build_ui()
        self._load_rules()

    def _set_dialog_icon(self) -> None:
        """继承主窗体图标"""
        from ..main_window import get_app_icon_path
        icon_path = get_app_icon_path()
        if icon_path and os.path.exists(icon_path):
            try:
                self.dlg.iconbitmap(icon_path)
            except Exception:
                pass

    def _build_ui(self) -> None:
        """构建 UI 视图结构"""
        # 顶部提示与快速过滤框
        top_frame = ttk.Frame(self.dlg)
        top_frame.pack(fill="x", padx=14, pady=(10, 4))

        ttk.Label(
            top_frame,
            text="业务配对关系中心 (北向引用变量 ➔ 南向物理变量):",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")

        self.search_var = tk.StringVar()
        entry_search = ttk.Entry(top_frame, textvariable=self.search_var, width=18)
        entry_search.pack(side="right")
        ttk.Label(top_frame, text="🔍 过滤:").pack(side="right", padx=(6, 2))

        # 表格区
        table_frame = ttk.Frame(self.dlg)
        table_frame.pack(fill="both", expand=True, padx=14, pady=4)

        scroll_y = ttk.Scrollbar(table_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")
        scroll_x = ttk.Scrollbar(table_frame, orient="horizontal")
        scroll_x.pack(side="bottom", fill="x")

        self.tree_pair = ttk.Treeview(
            table_frame,
            columns=("rule_id", "target_app", "target_reg", "src_app", "src_var", "datatype", "byteorder"),
            show="headings",
            yscrollcommand=scroll_y.set,
            xscrollcommand=scroll_x.set,
        )
        scroll_y.config(command=self.tree_pair.yview)
        scroll_x.config(command=self.tree_pair.xview)
        self.tree_pair.pack(fill="both", expand=True)

        cols = [
            ("rule_id", "规则编号", 90, "center"),
            ("target_app", "目标北向服务", 150, "w"),
            ("target_reg", "目标映射寄存器", 140, "center"),
            ("src_app", "源南向采集设备", 160, "w"),
            ("src_var", "源变量名 / 物理地址", 200, "w"),
            ("datatype", "数据类型", 90, "center"),
            ("byteorder", "变位模式", 80, "center"),
        ]
        for cid, chead, cw, calign in cols:
            self.tree_pair.heading(cid, text=chead)
            self.tree_pair.column(cid, width=cw, anchor=calign)

        # 底部状态栏
        self.lbl_info = ttk.Label(
            self.dlg,
            text="正在加载配对规则...",
            foreground="#6c757d",
            font=("Microsoft YaHei UI", 9),
        )
        self.lbl_info.pack(side="left", padx=14, pady=8)

        btn_close = ttk.Button(self.dlg, text="关闭", style="Small.TButton", command=self.dlg.destroy)
        btn_close.pack(side="right", padx=14, pady=8)

        self.search_var.trace_add("write", lambda *args: self._filter_rules())

    def _load_rules(self) -> None:
        """从数据库解析配对规则并呈现"""
        self.rules: List[PairingRule] = []
        try:
            engine = HemsPairingEngine(self.app.db_path)
            self.rules = engine.load_rules()
        except Exception as e:
            messagebox.showerror("加载配对规则失败", f"无法解析配对规则: {e}", parent=self.dlg)

        self.lbl_info.config(
            text=f"共解析出 {len(self.rules)} 条端到端映射配对规则 (当南向采集点位更新时，自动路由映射到北向寄存器)"
        )
        self._filter_rules()

    def _filter_rules(self) -> None:
        """根据关键字过滤配对规则"""
        for item in self.tree_pair.get_children():
            self.tree_pair.delete(item)

        kw = self.search_var.get().strip().lower()
        for i, r in enumerate(self.rules):
            search_haystack = f"{r.rule_id} {r.target_app_name} {r.src_app_name} {r.src_var_name}".lower()
            if kw and kw not in search_haystack:
                continue

            target_reg_str = f"{r.target_register} (FC:{r.target_fc}, 站号:{r.target_slave_id})"
            src_var_str = (
                f"{r.src_var_name} [Addr:{r.src_address}]"
                if r.src_address is not None
                else f"{r.src_var_name} (内部)"
            )
            self.tree_pair.insert(
                "",
                "end",
                values=(
                    f"R_{i+1:03d}",
                    r.target_app_name,
                    target_reg_str,
                    r.src_app_name,
                    src_var_str,
                    r.data_type.value,
                    r.byte_order.value,
                ),
            )
