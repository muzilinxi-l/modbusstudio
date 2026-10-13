"""
    Modbus Studio 主窗口与 80ms 批量节流日志视窗模块，负责整体窗口布局、全局选项卡、动态换肤及高吞吐日志渲染。
    关键导入：csv/json/os/subprocess/sys/time（配置持久化与 CSV 导出）、tkinter/ttk/filedialog/messagebox（顶层窗口与控件容器）。
对外暴露：get_app_icon_path（跨运行环境图标路径解析函数）、MainWindow（主窗口视图容器类）。
"""

from __future__ import annotations
import csv
import json
import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, List, Optional

# 为什么将 ModbusStudioAppCore 与 LogEntry 放在 TYPE_CHECKING 守卫下：
# 避免 main_window.py 与 app.py 在运行期形成双向模块导入环，同时保留 IDE 与 mypy 静态类型提示。
if TYPE_CHECKING:
    from ..app import ModbusStudioAppCore
    from ..services import LogEntry


# =====================================================================
# 1. 跨环境应用图标定位函数 (get_app_icon_path)
# 设计背景：
# 工程既支持在开发机上通过 `python -m modbusstudio.app` 源码运行，
# 也支持通过 PyInstaller 打包为单文件（`sys._MEIPASS` 临时解压目录）或目录版 exe 分发。
# 按优先级依次探测 6 个候选路径，确保无论从哪个工作目录启动都能正确加载 `app.ico`。
# =====================================================================
def get_app_icon_path() -> Optional[str]:
    """跨源码运行与 PyInstaller 打包环境的图标文件绝对路径定位器。"""
    # 参数：无。返回：匹配到的首个存在的 `app.ico` 绝对路径，若均不存在则返回 None。
    # 步骤：1. 检查 PyInstaller 单文件解压目录 _MEIPASS；2. 检查冻结 exe 所在目录；3. 沿当前文件向上三级目录及当前工作目录逐一探测。
    candidates = []
    # 1. PyInstaller --onefile 模式下资源被解压至 sys._MEIPASS 临时目录
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "app.ico"))
    # 2. PyInstaller --onedir 模式下图标位于可执行文件同级目录
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), "app.ico"))
    # 3. 源码开发模式下依次探测 ui/、modbusstudio/、项目根目录及当前工作目录
    curr_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(curr_dir, "app.ico"))
    candidates.append(os.path.join(os.path.dirname(curr_dir), "app.ico"))
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(curr_dir)), "app.ico"))
    candidates.append(os.path.abspath("app.ico"))

    for p in candidates:
        if os.path.exists(p):
            return p
    return None


# =====================================================================
# 2. 主窗口视图容器 (MainWindow)
# 架构职责：
# 1. 统管顶层窗口尺寸、图标、全局 ttk 字体/行高样式及 Treeview 选中态高对比度映射；
# 2. 采用垂直可拖拽分割窗格 (`PanedWindow`) 将上半部分业务工作台 (`SlavePanel` + `PollPanel`)
#    与下半部分通信报文日志监控区按 7:3 权重解耦布局；
# 3. 通过 `self.root.after(80, self._flush_log_loop)` 定时从 `LoggingService` 批量拉取最多 200 条日志，
#    将高频后台通信与 Tkinter 单线程文本渲染完全解耦，防止界面假死或内存溢出。
# =====================================================================
class MainWindow(ttk.Frame):
    """主窗口视图容器，承载顶部主题栏、中部从机/主机双选项卡及底部节流报文日志面板。"""

    def __init__(self, root: tk.Tk, app: ModbusStudioAppCore):
        """初始化主窗口容器，依次配置窗口属性、全局样式、主分割布局并启动 80ms 日志节流循环。"""
        # 参数：root（Tkinter 根窗口或 ttkbootstrap Window 实例）、app（全局应用控制器）。
        # 副作用：向 root 挂载主 Frame 并在主事件循环注册 80ms 周期定时器。
        super().__init__(root)
        self.root = root
        self.app = app

        # 依次执行窗口标题/尺寸初始化、全局字体与控件样式配置、上下分割窗格构建
        self._init_window()
        self._init_style()
        self._build_layout()

        # 为什么使用 80ms (~12.5 FPS) 定时批量刷新而非每收到一帧报文就写一次 Text 控件：
        # 当几十台从机并发响应或主机 100ms 高频轮询时，后台每秒可产生数百条报文；
        # 若每次直接调用 Text.insert + Text.see("end")，Tkinter 重排与滚屏会瞬间占满主线程导致界面“未响应”。
        self.root.after(80, self._flush_log_loop)

    def _init_window(self) -> None:
        """设置主窗口标题、初始几何尺寸 (1240x840)、最小缩放边界及窗口图标。"""
        # 设定默认窗口大小 1240x840 与最小宽高 1020x640，防止小屏缩放时顶部工具栏按钮被挤压截断
        self.root.title("Modbus Studio - 业务配置与变位仿真调试工作站")
        self.root.geometry("1240x840")
        self.root.minsize(1020, 640)

        # 锁定恢复专属图标，替换 Tkinter / ttkbootstrap 默认羽毛或水滴图标
        icon_path = get_app_icon_path()
        if icon_path:
            try:
                self.root.iconbitmap(icon_path)
            except Exception:
                pass

        self.pack(fill="both", expand=True)

    def _init_style(self) -> None:
        """初始化全局 ttk 样式引擎、微软雅黑字体、紧凑按钮内边距及 Treeview 选中态高亮配色。"""
        # 优先复用 ttkbootstrap Window 自带的 style 对象，若为标准 tk.Tk 则新建 ttk.Style 并启用 clam 主题
        self.style = getattr(self.root, "style", None)
        if self.style is None:
            self.style = ttk.Style(self.root)
            try:
                self.style.theme_use("clam")
            except Exception:
                pass

        # 全局字体与紧凑内边距，防高 DPI 缩放时按钮文字被截断
        default_font = ("Microsoft YaHei UI", 9)
        self.root.option_add("*Font", default_font)

        # 统一设置基础控件内边距与 Treeview 28px 行高，避免多列表格行距过密难以点击
        self.style.configure(".", font=default_font)
        self.style.configure("TButton", padding=(3, 1))
        self.style.configure("Small.TButton", font=("Microsoft YaHei UI", 8), padding=(2, 1))
        self.style.configure("TLabel", padding=1)
        self.style.configure("TCheckbutton", padding=1)
        self.style.configure("TRadiobutton", padding=1)
        self.style.configure("Treeview", rowheight=28, font=("Microsoft YaHei UI", 9))
        self.style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), padding=(4, 2))

        # 为什么显式调用 style.map 覆盖 Treeview 的 selected 状态颜色：
        # 部分暗色或第三方主题下 Treeview 选中行背景色与普通行过于接近，显式指定亮蓝底白字 (#0d6efd / #ffffff) 确保清晰可辨。
        self.style.map(
            "Treeview",
            background=[("selected", "#0d6efd")],
            foreground=[("selected", "#ffffff")],
        )

    def _build_layout(self) -> None:
        """构建主窗口三层垂直结构：顶部品牌与主题栏、中部从机/主机 Notebook 选项卡、底部报文日志面板。"""
        # 0. 顶部应用导航与主题控制栏
        self._build_header_bar()

        # 1. 主分割窗格（上下垂直分割：上方功能工作台权重 7，下方日志监视区权重 3，允许用户自由拖拽分割线）
        self.main_paned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        self.main_paned.pack(fill="both", expand=True, padx=4, pady=4)

        # 上半部分：选项卡容器 (Notebook)
        self.notebook = ttk.Notebook(self.main_paned)
        self.main_paned.add(self.notebook, weight=7)

        # 为什么在方法内延迟导入 SlavePanel 和 PollPanel：
        # 缩短模块顶层导入链路，并避免子面板反向引用 get_app_icon_path 时出现未定义符号。
        from .slave_panel import SlavePanel
        from .poll_panel import PollPanel

        self.slave_panel = SlavePanel(self.notebook, self.app)
        self.poll_panel = PollPanel(self.notebook, self.app)

        self.notebook.add(self.slave_panel, text="  💻 Modbus Slave 从机模拟器 (业务库导入与仿真)  ")
        self.notebook.add(self.poll_panel, text="  🔍 Modbus Poll 主机轮询调试器 (业务规则导入)  ")

        # 下半部分：高可靠性日志面板容器
        self.log_container = ttk.Frame(self.main_paned)
        self.main_paned.add(self.log_container, weight=3)

        self._build_log_viewer(self.log_container)

    def _build_header_bar(self) -> None:
        """构建应用顶部品牌标语与实时明暗主题切换控制栏。"""
        header_bar = ttk.Frame(self)
        header_bar.pack(fill="x", padx=6, pady=(3, 2))

        # 左侧：品牌与版本标语
        lbl_brand = ttk.Label(
            header_bar,
            text="⚡ Modbus Studio  ·  现代化多实例仿真与轮询调试平台",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        lbl_brand.pack(side="left", padx=2)

        # 右侧：实时主题切换控制器（下拉选择任意主题 + 一键明/暗配对翻转）
        theme_box = ttk.Frame(header_bar)
        theme_box.pack(side="right", padx=2)

        lbl_theme = ttk.Label(theme_box, text="🎨 界面主题:")
        lbl_theme.pack(side="left", padx=(4, 2))

        # 探测当前 GUI 环境支持的主题列表；若探测为空则回退提供常用亮/暗主题列表
        available_themes = []
        if self.style and hasattr(self.style, "theme_names"):
            try:
                available_themes = list(self.style.theme_names())
            except Exception:
                pass
        if not available_themes:
            available_themes = [
                "bootstrap-light", "bootstrap-dark", "pydata-light", "pydata-dark",
                "nord-light", "nord-dark", "minty-light", "dracula-dark",
                "one-light", "one-dark", "catppuccin-light", "catppuccin-dark"
            ]

        current_theme = getattr(getattr(self.style, "theme", None), "name", "bootstrap-light")

        self.combo_theme = ttk.Combobox(theme_box, values=available_themes, state="readonly", width=17)
        if current_theme in available_themes:
            self.combo_theme.set(current_theme)
        elif available_themes:
            self.combo_theme.current(0)
        self.combo_theme.pack(side="left", padx=2)
        self.combo_theme.bind("<<ComboboxSelected>>", self._on_theme_selected)

        btn_toggle = ttk.Button(
            theme_box,
            text="🌓 明/暗快速切换",
            style="Small.TButton",
            command=self._toggle_light_dark,
        )
        btn_toggle.pack(side="left", padx=2)

    def _on_theme_selected(self, event=None) -> None:
        """响应主题下拉框选中事件，立即应用用户选择的界面主题。"""
        theme = self.combo_theme.get()
        if theme:
            self._apply_theme(theme)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_toggle_light_dark]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _toggle_light_dark(self) -> None:
        """在当前主题对应的亮色 (light) 与暗色 (dark) 变体之间一键切换。"""
        # 为什么先尝试将当前主题名中的 dark/light 互换再回退 bootstrap-light/dark：
        # 优先保持用户当前所选色系（如 nord-light <-> nord-dark），仅当该色系无配对变体时才回退到默认 bootstrap 主题。
        # 在 [_toggle_light_dark] 中更新局部变量 [current]：为后续协议编码、服务调度或界面渲染准备中间状态
        current = self.combo_theme.get() or "bootstrap-light"
        # 在 [_toggle_light_dark] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if "dark" in current.lower():
            # 在 [_toggle_light_dark] 中更新局部变量 [target]：为后续协议编码、服务调度或界面渲染准备中间状态
            target = current.replace("dark", "light")
            # 在 [_toggle_light_dark] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self.style and hasattr(self.style, "theme_names") and target not in self.style.theme_names():
                # 在 [_toggle_light_dark] 中更新局部变量 [target]：为后续协议编码、服务调度或界面渲染准备中间状态
                target = "bootstrap-light"
        else:
            # 在 [_toggle_light_dark] 中更新局部变量 [target]：为后续协议编码、服务调度或界面渲染准备中间状态
            target = current.replace("light", "dark")
            # 在 [_toggle_light_dark] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self.style and hasattr(self.style, "theme_names") and target not in self.style.theme_names():
                # 在 [_toggle_light_dark] 中更新局部变量 [target]：为后续协议编码、服务调度或界面渲染准备中间状态
                target = "bootstrap-dark"
        self._apply_theme(target)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_apply_theme]（入参: theme_name）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _apply_theme(self, theme_name: str) -> None:
        """切换全局 ttk 主题，恢复自定义 Treeview 行高/选中色与窗口图标，并持久化保存主题偏好。"""
        # 步骤拆解：
        # 1. 调用 style.theme_use(theme_name) 并重设会被新主题覆盖的 Treeview 行高与选中映射；
        # 2. 重新绑定窗口图标，防止部分主题引擎换肤时重置顶层窗口图标；
        # 3. 根据亮/暗主题微调底部日志终端背景色，并将主题名写入 settings.json。
        # 在 [_apply_theme] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [_apply_theme] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if self.style:
                self.style.theme_use(theme_name)
                self.style.configure("Treeview", rowheight=28, font=("Microsoft YaHei UI", 9))
                self.style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), padding=(4, 2))
                self.style.map(
                    "Treeview",
                    background=[("selected", "#0d6efd")],
                    foreground=[("selected", "#ffffff")],
                )
            self.combo_theme.set(theme_name)

            # 换肤后确保自定义图标不被主题库重置
            # 在 [_apply_theme] 中更新局部变量 [icon_path]：为后续协议编码、服务调度或界面渲染准备中间状态
            icon_path = get_app_icon_path()
            # 在 [_apply_theme] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if icon_path:
                # 在 [_apply_theme] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                try:
                    self.root.iconbitmap(icon_path)
                except Exception:
                    pass

            # 自适应优化终端日志底色与文字高亮对比度
            # 在 [_apply_theme] 中更新局部变量 [is_dark]：为后续协议编码、服务调度或界面渲染准备中间状态
            is_dark = "dark" in theme_name.lower()
            # 在 [_apply_theme] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if is_dark:
                self.log_text.config(
                    bg="#181824",
                    fg="#cdd6f4",
                    insertbackground="#ffffff",
                )
            else:
                self.log_text.config(
                    bg="#212529",
                    fg="#f8f9fa",
                    insertbackground="#ffffff",
                )

            # 持久化存储主题偏好至本地 settings.json
            self._save_theme_preference(theme_name)
        except Exception as e:
            messagebox.showwarning("主题切换失败", f"无法切换至主题 [{theme_name}]: {e}")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_save_theme_preference]（入参: theme_name）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _save_theme_preference(self, theme_name: str) -> None:
        """将当前选中的主题名称增量写入项目根目录下的 settings.json 配置文件。"""
        # 为什么先读取原 JSON 字典再更新 "theme" 键：
        # 保留 settings.json 中可能存在的其他用户配置项，避免直接覆盖丢失其它偏好设置。
        # 在 [_save_theme_preference] 中更新局部变量 [settings_file]：为后续协议编码、服务调度或界面渲染准备中间状态
        settings_file = os.path.abspath("settings.json")
        # 在 [_save_theme_preference] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [_save_theme_preference] 中更新局部变量 [data]：为后续协议编码、服务调度或界面渲染准备中间状态
            data = {}
            # 在 [_save_theme_preference] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if os.path.exists(settings_file):
                # 在 [_save_theme_preference] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                try:
                    # 在 [_save_theme_preference] 中使用上下文管理器：确保互斥锁或资源句柄在离开作用域时自动安全释放，杜绝死锁与泄漏
                    with open(settings_file, "r", encoding="utf-8") as f:
                        # 在 [_save_theme_preference] 中更新局部变量 [data]：为后续协议编码、服务调度或界面渲染准备中间状态
                        data = json.load(f)
                except Exception:
                    # 在 [_save_theme_preference] 中更新局部变量 [data]：为后续协议编码、服务调度或界面渲染准备中间状态
                    data = {}
            data["theme"] = theme_name
            # 在 [_save_theme_preference] 中使用上下文管理器：确保互斥锁或资源句柄在离开作用域时自动安全释放，杜绝死锁与泄漏
            with open(settings_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_build_log_viewer]（入参: parent）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _build_log_viewer(self, parent: ttk.Frame) -> None:
        """构建下半部分实时系统与通信报文日志面板（含工具栏、过滤复选框、深色终端文本框及着色标签）。"""
        # 日志标题栏与操作按钮条（单行自适应紧凑排布）
        # 在 [_build_log_viewer] 中更新局部变量 [title_bar]：为后续协议编码、服务调度或界面渲染准备中间状态
        title_bar = ttk.Frame(parent)
        title_bar.pack(fill="x", padx=2, pady=2)

        # 在 [_build_log_viewer] 中更新局部变量 [lbl_title]：为后续协议编码、服务调度或界面渲染准备中间状态
        lbl_title = ttk.Label(title_bar, text="实时系统与通信报文日志 (可上下拖动分割栏调整高度)", font=("Microsoft YaHei UI", 9, "bold"))
        lbl_title.pack(side="left", padx=2)

        # 工具栏容器：提供复制、全选、清空、CSV 导出、打开日志目录以及滚屏/报文显示开关
        # 在 [_build_log_viewer] 中更新局部变量 [tool_frame]：为后续协议编码、服务调度或界面渲染准备中间状态
        tool_frame = ttk.Frame(parent)
        tool_frame.pack(fill="x", padx=2, pady=1)

        # 按钮统一紧凑内边距，去除硬编码字符宽，自适应内容
        # 在 [_build_log_viewer] 中更新局部变量 [btn_copy]：为后续协议编码、服务调度或界面渲染准备中间状态
        btn_copy = ttk.Button(tool_frame, text="📋 复制", style="Small.TButton", command=self._copy_log)
        btn_copy.pack(side="left", padx=2)

        # 在 [_build_log_viewer] 中更新局部变量 [btn_select_all]：为后续协议编码、服务调度或界面渲染准备中间状态
        btn_select_all = ttk.Button(tool_frame, text="☑ 全选", style="Small.TButton", command=self._select_all_log)
        btn_select_all.pack(side="left", padx=2)

        # 在 [_build_log_viewer] 中更新局部变量 [btn_clear]：为后续协议编码、服务调度或界面渲染准备中间状态
        btn_clear = ttk.Button(tool_frame, text="🧹 清空", style="Small.TButton", command=self._clear_log)
        btn_clear.pack(side="left", padx=2)

        # 在 [_build_log_viewer] 中更新局部变量 [btn_export]：为后续协议编码、服务调度或界面渲染准备中间状态
        btn_export = ttk.Button(tool_frame, text="💾 导出CSV", style="Small.TButton", command=self._export_log_to_csv)
        btn_export.pack(side="left", padx=2)

        # 在 [_build_log_viewer] 中更新局部变量 [btn_dir]：为后续协议编码、服务调度或界面渲染准备中间状态
        btn_dir = ttk.Button(tool_frame, text="📂 日志目录", style="Small.TButton", command=self._open_log_dir)
        btn_dir.pack(side="left", padx=2)

        # 在 [_build_log_viewer] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.auto_scroll_var = tk.BooleanVar(value=True)
        # 在 [_build_log_viewer] 中更新局部变量 [chk_scroll]：为后续协议编码、服务调度或界面渲染准备中间状态
        chk_scroll = ttk.Checkbutton(tool_frame, text="自动滚屏", variable=self.auto_scroll_var)
        chk_scroll.pack(side="left", padx=6)

        # 允许用户在排查系统事件时临时关闭高频 RX/TX 问询报文刷屏
        # 在 [_build_log_viewer] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.show_traffic_var = tk.BooleanVar(value=True)
        # 在 [_build_log_viewer] 中更新局部变量 [chk_traffic]：为后续协议编码、服务调度或界面渲染准备中间状态
        chk_traffic = ttk.Checkbutton(tool_frame, text="显示问询与响应报文", variable=self.show_traffic_var)
        chk_traffic.pack(side="left", padx=4)

        # 文本框与垂直滚动条容器
        # 在 [_build_log_viewer] 中更新局部变量 [text_frame]：为后续协议编码、服务调度或界面渲染准备中间状态
        text_frame = ttk.Frame(parent)
        text_frame.pack(fill="both", expand=True, padx=2, pady=1)

        # 在 [_build_log_viewer] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.log_scroll = ttk.Scrollbar(text_frame, orient="vertical")
        self.log_scroll.pack(side="right", fill="y")

        # 使用等宽字体 Consolas 渲染十六进制报文帧，使多行 RX/TX 字节对齐更易读
        # 在 [_build_log_viewer] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self.log_text = tk.Text(
            text_frame,
            wrap="none",
            height=8,
            bg="#181818",
            fg="#dcdcdc",
            insertbackground="#ffffff",
            font=("Consolas", 9),
            yscrollcommand=self.log_scroll.set,
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_scroll.config(command=self.log_text.yview)

        # 配置四类日志方向颜色高亮标签：系统通知浅蓝、接收浅绿、发送浅黄、异常警告红色
        self.log_text.tag_config("SYS", foreground="#61afef")      # 浅蓝
        self.log_text.tag_config("RX", foreground="#98c379")       # 浅绿
        self.log_text.tag_config("TX", foreground="#e5c07b")       # 浅黄
        self.log_text.tag_config("ERROR", foreground="#e06c75")    # 红色

        # 为什么限制 Text 控件最多保留 1500 行：
        # 长时间压测下若无限向 Tkinter Text 追加文本，底层 Tcl B-Tree 节点膨胀会导致滚屏与重绘指数级变慢；
        # 完整历史报文已由 LoggingService 实时刷盘到 logs/ 目录下的 CSV 文件中，UI 仅需保留最近 1500 行滑动窗口。
        # 在 [_build_log_viewer] 中更新局部变量 [状态变量]：为后续协议编码、服务调度或界面渲染准备中间状态
        self._max_log_lines = 1500  # 最大内存保留行数，超限自动修剪，防止内存泄漏和卡顿

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_flush_log_loop]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _flush_log_loop(self) -> None:
        """主线程定时（每 80ms）从日志队列批量拉取最多 200 条记录合并写入文本框并执行超限裁剪。"""
        # 为什么整个方法体包裹在 try...finally 并确保 finally 中重挂 `self.root.after(80, ...)`：
        # 即使某次单批日志渲染遇到罕见编码或控件状态异常，定时刷新链路也绝不能中断。
        # 在 [_flush_log_loop] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            entries: List[LogEntry] = self.app.logging_service.drain_batch(max_count=200)
            # 在 [_flush_log_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if entries:
                # 在 [_flush_log_loop] 中更新局部变量 [show_traffic]：为后续协议编码、服务调度或界面渲染准备中间状态
                show_traffic = self.show_traffic_var.get()
                self.log_text.config(state="normal")

                # 批量遍历插入：若用户取消勾选“显示问询与响应报文”，则跳过 RX/TX 仅显示 SYS/ERROR
                # 在 [_flush_log_loop] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
                for entry in entries:
                    # 在 [_flush_log_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if not show_traffic and entry.direction in ("RX", "TX"):
                        continue
                    # 在 [_flush_log_loop] 中更新局部变量 [line_str]：为后续协议编码、服务调度或界面渲染准备中间状态
                    line_str = entry.format_line()
                    self.log_text.insert("end", line_str, entry.direction)

                # 行数超限清理：当总行数超过 _max_log_lines (1500) 时，一次性删除头部最旧的溢出行
                # 在 [_flush_log_loop] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
                try:
                    # 在 [_flush_log_loop] 中更新局部变量 [num_lines]：为后续协议编码、服务调度或界面渲染准备中间状态
                    num_lines = int(float(self.log_text.index("end-1c")))
                    # 在 [_flush_log_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if num_lines > self._max_log_lines:
                        # 在 [_flush_log_loop] 中更新局部变量 [trim_to]：为后续协议编码、服务调度或界面渲染准备中间状态
                        trim_to = num_lines - self._max_log_lines
                        self.log_text.delete("1.0", f"{trim_to}.0")
                except Exception:
                    pass

                # 仅在勾选“自动滚屏”时将视口滚动到底部，方便用户取消勾选后静态悬停查看历史某帧报文
                # 在 [_flush_log_loop] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                if self.auto_scroll_var.get():
                    self.log_text.see("end")

                self.log_text.config(state="normal")
        except Exception as e:
            pass
        finally:
            # 持续周期循环，保持 80ms 刷新率
            self.root.after(80, self._flush_log_loop)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_copy_log]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _copy_log(self) -> None:
        """将当前选中的日志文本（若未选中则复制全部视窗日志）写入系统剪贴板。"""
        # 为什么捕获 tk.TclError 并回退复制全文：
        # 当用户未在 Text 中拖拽选中文本直接点击“复制”时，`get("sel.first", "sel.last")` 会抛出 TclError，
        # 此时自动复制整个视窗内容更符合一键复制排障日志的直觉。
        # 在 [_copy_log] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [_copy_log] 中更新局部变量 [sel]：为后续协议编码、服务调度或界面渲染准备中间状态
            sel = self.log_text.get("sel.first", "sel.last")
            # 在 [_copy_log] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if sel:
                self.root.clipboard_clear()
                self.root.clipboard_append(sel)
        except tk.TclError:
            # 未选中文本时全量复制
            # 在 [_copy_log] 中更新局部变量 [all_txt]：为后续协议编码、服务调度或界面渲染准备中间状态
            all_txt = self.log_text.get("1.0", "end-1c")
            # 在 [_copy_log] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if all_txt:
                self.root.clipboard_clear()
                self.root.clipboard_append(all_txt)

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_select_all_log]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _select_all_log(self) -> None:
        """全选当前日志文本框中的所有日志行。"""
        self.log_text.tag_add("sel", "1.0", "end")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_clear_log]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _clear_log(self) -> None:
        """清空当前屏幕日志视窗中的显示内容（不影响磁盘已持久化的 CSV 日志文件）。"""
        self.log_text.delete("1.0", "end")

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_open_log_dir]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _open_log_dir(self) -> None:
        """调用操作系统文件资源管理器打开磁盘通信日志归档目录 (`logs/`)。"""
        # 在 [_open_log_dir] 中更新局部变量 [log_dir]：为后续协议编码、服务调度或界面渲染准备中间状态
        log_dir = self.app.logging_service.log_dir
        # 在 [_open_log_dir] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        # 兼容 Windows (`os.startfile`) 与 Linux 桌面 (`xdg-open`) 打开文件夹
        # 在 [_open_log_dir] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if sys.platform == "win32":
            os.startfile(log_dir)
        else:
            subprocess.Popen(["xdg-open", log_dir])

    # ──────────────────────────────────────────────────────────────────
    # 方法实现 [_export_log_to_csv]（无额外入参）：封装该操作的输入校验、状态转换与异常兜底逻辑
    # ──────────────────────────────────────────────────────────────────
    def _export_log_to_csv(self) -> None:
        """弹出文件保存对话框，将当前日志视窗中的内容导出为带 UTF-8 BOM 的 CSV 文件。"""
        # 在 [_export_log_to_csv] 中更新局部变量 [content]：为后续协议编码、服务调度或界面渲染准备中间状态
        content = self.log_text.get("1.0", "end-1c")
        # 在 [_export_log_to_csv] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not content.strip():
            messagebox.showinfo("提示", "当前日志视窗中无数据可导出")
            # 从 [_export_log_to_csv] 返回执行结果给调用方，结束当前处理分支
            return

        # 在 [_export_log_to_csv] 中更新局部变量 [save_path]：为后续协议编码、服务调度或界面渲染准备中间状态
        save_path = filedialog.asksaveasfilename(
            title="导出当前视窗日志",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            initialfile=f"modbus_log_export_{time.strftime('%Y%m%d_%H%M%S')}.csv",
        )
        # 在 [_export_log_to_csv] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
        if not save_path:
            # 从 [_export_log_to_csv] 返回执行结果给调用方，结束当前处理分支
            return

        # 为什么使用 utf-8-sig 编码写入 CSV：
        # 写入 UTF-8 BOM 头可确保在中文 Windows 下直接双击用 Excel 打开导出的 CSV 时中文不乱码。
        # 在 [_export_log_to_csv] 中开启异常防护块：捕获潜在的 I/O、网络、SQLite 或格式转换异常，保障主流程与 GUI 不崩溃
        try:
            # 在 [_export_log_to_csv] 中使用上下文管理器：确保互斥锁或资源句柄在离开作用域时自动安全释放，杜绝死锁与泄漏
            with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
                # 在 [_export_log_to_csv] 中更新局部变量 [writer]：为后续协议编码、服务调度或界面渲染准备中间状态
                writer = csv.writer(f)
                writer.writerow(["时间戳", "方向", "日志明细"])
                # 在 [_export_log_to_csv] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
                for line in content.splitlines():
                    # 在 [_export_log_to_csv] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                    if line.strip():
                        # 在 [_export_log_to_csv] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
                        if line.startswith("[") and "]" in line:
                            # 在 [_export_log_to_csv] 中更新局部变量 [bracket_end]：为后续协议编码、服务调度或界面渲染准备中间状态
                            bracket_end = line.find("]")
                            # 在 [_export_log_to_csv] 中更新局部变量 [ts]：为后续协议编码、服务调度或界面渲染准备中间状态
                            ts = line[1:bracket_end]
                            # 在 [_export_log_to_csv] 中更新局部变量 [detail]：为后续协议编码、服务调度或界面渲染准备中间状态
                            detail = line[bracket_end + 1:].strip()
                            writer.writerow([ts, "", detail])
                        else:
                            writer.writerow(["", "", line])

            messagebox.showinfo("导出成功", f"日志已成功导出至：\n{save_path}")
        except Exception as e:
            messagebox.showerror("导出失败", f"写入文件失败: {e}")

