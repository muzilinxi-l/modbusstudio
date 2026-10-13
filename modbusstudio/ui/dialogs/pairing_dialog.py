"""
    业务配对关系中心模态对话框模块，可视化展示一体机数据库中的北向服务与南向采集设备映射规则。
    关键导入：tkinter/ttk/messagebox（模态窗口与表格组件）、HemsPairingEngine/PairingRule（业务数据库配对解析引擎与规则模型）。
对外暴露：PairingDialog（业务配对关系中心弹窗控制器类）。
"""

from __future__ import annotations
import os
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, List

# 导入底层业务配对解析引擎与配对规则数据模型：
# UI 层不直接编写 SQL 或解析 app.More JSON，而是委托 HemsPairingEngine 完成跨表配对构建。
from ...hems_pairing import HemsPairingEngine, PairingRule


# =====================================================================
# 业务配对关系中心模态对话框 (PairingDialog)
# 职责说明：
# 1. 接收主窗口已关联的现场 `.db`/`.cdb` 数据库路径并校验文件有效性；
# 2. 调用 HemsPairingEngine 提取全部“南向采集变量 -> 北向转发寄存器”映射规则；
# 3. 提供带双向滚动条的 7 列 Treeview 表格与即时关键字搜索框，方便联调工程师核查数据链路。
# =====================================================================
class PairingDialog:
    """业务配对关系中心模态对话框控制器。"""

    def __init__(self, parent: tk.Widget, app: Any) -> None:
        """初始化业务配对关系对话框，校验数据库路径后构建 UI 并加载规则。"""
        # 保存父级组件引用与全局应用上下文（用于读取 app.db_path）
        self.parent = parent
        self.app = app

        # 为什么在创建 Toplevel 窗口前先检查 db_path：
        # 1. 若用户尚未导入或关联任何 `.db`/`.cdb` 数据库文件，弹窗无数据可展示；
        # 2. 提前拦截并弹出明确警告，可避免在屏幕上闪现空白 Toplevel 窗口再销毁的割裂体验。
        if not self.app.db_path or not os.path.exists(self.app.db_path):
            messagebox.showwarning("提示", "尚未关联有效的数据库文件，请先关联数据库！", parent=parent)
            return

        # 创建模态子窗口并设置默认尺寸 (900x540) 与最小缩放尺寸 (740x420)，保证 7 列宽表格的完整可读性
        self.dlg = tk.Toplevel(parent)
        self.dlg.title("业务配对关系中心 (基于 app 表 More 字段配置)")
        self.dlg.geometry("900x540")
        self.dlg.minsize(740, 420)
        # 为什么同时调用 transient 与 grab_set：
        # 1. transient 将该弹窗绑定为父窗口的附属窗口，使其始终位于主窗口上方且随主窗口一起最小化；
        # 2. grab_set 将鼠标与键盘事件焦点独占锁定在当前对话框内，防止用户在查看配对时误触后台按钮切换数据库。
        self.dlg.transient(parent.winfo_toplevel())
        self.dlg.grab_set()

        # 依次执行图标继承、UI 布局构建与数据库规则加载
        self._set_dialog_icon()
        self._build_ui()
        self._load_rules()

    def _set_dialog_icon(self) -> None:
        """为当前模态对话框设置与主窗口一致的应用图标。"""
        # 为什么在方法内部延迟导入 get_app_icon_path：
        # 避免 main_window.py 与 dialogs/pairing_dialog.py 在模块顶层互相导入造成循环依赖。
        from ..main_window import get_app_icon_path
        icon_path = get_app_icon_path()
        # 仅当图标物理文件存在时尝试设置，捕获异常以兼容无头测试环境或非 Windows 窗口管理器
        if icon_path and os.path.exists(icon_path):
            try:
                self.dlg.iconbitmap(icon_path)
            except Exception:
                # 非 Windows 平台或特殊窗口管理器不支持 .ico 时静默忽略，不影响业务功能
                pass

    def _build_ui(self) -> None:
        """构建顶部搜索过滤栏、中部双向滚动 Treeview 表格及底部状态栏。"""
        # 1. 顶部提示与快速过滤框：左侧展示数据流向说明，右侧提供即时关键字搜索输入框
        top_frame = ttk.Frame(self.dlg)
        top_frame.pack(fill="x", padx=14, pady=(10, 4))

        ttk.Label(
            top_frame,
            text="业务配对关系中心 (北向引用变量 ➔ 南向物理变量):",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")

        # 绑定 StringVar 以便通过 trace_add 实时响应用户输入变化
        self.search_var = tk.StringVar()
        entry_search = ttk.Entry(top_frame, textvariable=self.search_var, width=18)
        entry_search.pack(side="right")
        ttk.Label(top_frame, text="🔍 过滤:").pack(side="right", padx=(6, 2))

        # 2. 表格区：使用独立 Frame 包裹 Treeview 与水平/垂直滚动条，确保窗口拉伸时表格自适应扩展
        table_frame = ttk.Frame(self.dlg)
        table_frame.pack(fill="both", expand=True, padx=14, pady=4)

        # 先停靠右侧垂直滚动条与底部水平滚动条，最后让 Treeview 填充剩余全部空间
        scroll_y = ttk.Scrollbar(table_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")
        scroll_x = ttk.Scrollbar(table_frame, orient="horizontal")
        scroll_x.pack(side="bottom", fill="x")

        # 为什么定义这 7 列结构：
        # 将北向目标信息（服务名、寄存器/FC/站号）与南向源信息（采集设备、变量名/物理地址）及编码格式（数据类型、变位模式）
        # 并排呈现，使现场工程师无需翻查两份点表即可验证端到端数据转发链路。
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

        # 统一配置各列标题、初始像素宽度与对齐方式（标识与数值居中，设备名与变量名左对齐）
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

        # 3. 底部状态栏：左侧显示解析出的规则总数及路由说明，右侧提供关闭弹窗按钮
        # 为什么使用浅灰色 (#6c757d) 9 号字体展示状态说明：
        # 弱化底部辅助信息视觉权重，将用户视觉焦点集中在中部配对关系表格上。
        self.lbl_info = ttk.Label(
            self.dlg,
            text="正在加载配对规则...",
            foreground="#6c757d",
            font=("Microsoft YaHei UI", 9),
        )
        self.lbl_info.pack(side="left", padx=14, pady=8)

        # 关闭按钮直接绑定 self.dlg.destroy，销毁模态窗口并自动释放 grab_set 焦点锁
        btn_close = ttk.Button(self.dlg, text="关闭", style="Small.TButton", command=self.dlg.destroy)
        btn_close.pack(side="right", padx=14, pady=8)

        # 为什么使用 trace_add("write") 监听输入：
        # 1. 用户每输入或删除一个字符即可实时触发 _filter_rules 重绘表格，无需额外点击“搜索”按钮；
        # 2. 相比绑定 `<KeyRelease>` 事件，trace_add 能同时捕获鼠标右键粘贴或程序自动赋值引起的文本变化。
        self.search_var.trace_add("write", lambda *args: self._filter_rules())

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_load_rules]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _load_rules(self) -> None:
        """调用 HemsPairingEngine 从当前关联数据库解析全部配对规则并刷新视图。"""
        # 步骤拆解：
        # 1. 初始化空规则列表 self.rules；
        # 2. 实例化 HemsPairingEngine(self.app.db_path) 并调用 load_rules() 获取全部配对；
        # 3. 更新底部状态栏统计文案并调用 _filter_rules() 执行首次表格渲染。
        self.rules: List[PairingRule] = []
        # 为什么用 try/except 包裹配对引擎初始化：
        # 1. 现场导入的 SQLite 文件可能损坏、被其他进程独占锁死，或并非标准 HEMS `.cdb` 结构（缺少 app 表）；
        # 2. 捕获异常后弹出友好错误提示框，仍保留空表格界面而非直接令主程序未捕获异常退栈。
        # 在 [_load_rules] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [_load_rules] 中更新局部变量 [engine]：为后续协议编码、服务调度或界面渲染准备中间状态
            engine = HemsPairingEngine(self.app.db_path)
            # 在 [_load_rules] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
            self.rules = engine.load_rules()
        except Exception as e:
            messagebox.showerror("加载配对规则失败", f"无法解析配对规则: {e}", parent=self.dlg)

        # 更新底部汇总标签并触发首次全量表格渲染
        self.lbl_info.config(
            text=f"共解析出 {len(self.rules)} 条端到端映射配对规则 (当南向采集点位更新时，自动路由映射到北向寄存器)"
        )
        self._filter_rules()

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_filter_rules]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _filter_rules(self) -> None:
        """根据搜索框关键字过滤配对规则列表并重新渲染 Treeview 行。"""
        # 步骤拆解：
        # 1. 遍历清空 Treeview 当前所有子节点；
        # 2. 提取搜索框关键字并转为小写；
        # 3. 遍历 self.rules 执行子串匹配，将命中的配对规则格式化插入 Treeview。
        # 先清空表格中所有已渲染行，准备按最新关键字重绘
        # 在 [_filter_rules] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for item in self.tree_pair.get_children():
            self.tree_pair.delete(item)

        # 在 [_filter_rules] 中更新局部变量 [kw]：为后续协议编码、服务调度或界面渲染准备中间状态
        kw = self.search_var.get().strip().lower()
        # 为什么将 rule_id、target_app_name、src_app_name、src_var_name 拼接为统一小写检索串：
        # 1. 允许工程师直接输入规则号（如 R_30）、北向服务名（如 North）、南向设备名（如 PCS）或具体信号名快速定位；
        # 2. 统一转小写实现大小写不敏感模糊搜索，提升现场排障效率。
        # 在 [_filter_rules] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for i, r in enumerate(self.rules):
            # 在 [_filter_rules] 中更新局部变量 [search_haystack]：为后续协议编码、服务调度或界面渲染准备中间状态
            search_haystack = f"{r.rule_id} {r.target_app_name} {r.src_app_name} {r.src_var_name}".lower()
            # 在 [_filter_rules] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if kw and kw not in search_haystack:
                continue

            # 组装目标北向寄存器信息（含寄存器地址、功能码与从机站号）
            # 在 [_filter_rules] 中更新局部变量 [target_reg_str]：为后续协议编码、服务调度或界面渲染准备中间状态
            target_reg_str = f"{r.target_register} (FC:{r.target_fc}, 站号:{r.target_slave_id})"
            # 若南向源变量无物理寄存器地址（如系统内部计算量），显式标注“(内部)”以区分硬件采集点位
            # 在 [_filter_rules] 中更新局部变量 [src_var_str]：为后续协议编码、服务调度或界面渲染准备中间状态
            src_var_str = (
                f"{r.src_var_name} [Addr:{r.src_address}]"
                if r.src_address is not None
                else f"{r.src_var_name} (内部)"
            )
            # 按 7 列顺序插入行数据，首列采用连续序号 R_001, R_002... 保持视觉整齐
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


