"""
Modbus Studio - 主窗口与节流日志视窗 (Main Window & Throttled Log Viewer)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
负责整体窗口布局、全局选项卡、系统托盘/图标，以及基于定时批处理节流的高性能报文日志显示，
彻底根治高频通信下的 UI 假死“未响应”和底层闪退崩溃。
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

if TYPE_CHECKING:
    from ..app import ModbusStudioAppCore
    from ..services import LogEntry


class MainWindow(ttk.Frame):
    """主窗口视图容器"""

    def __init__(self, root: tk.Tk, app: ModbusStudioAppCore):
        super().__init__(root)
        self.root = root
        self.app = app

        self._init_window()
        self._init_style()
        self._build_layout()

        # 启动日志批量节流刷新引擎 (每 80ms 刷新一次缓冲池)
        self.root.after(80, self._flush_log_loop)

    def _init_window(self) -> None:
        self.root.title("Modbus Studio - 业务配置与变位仿真调试工作站")
        self.root.geometry("1240x840")
        self.root.minsize(1020, 640)

        # 设置图标
        icon_path = os.path.abspath("app.ico")
        if os.path.exists(icon_path):
            try:
                self.root.iconbitmap(icon_path)
            except Exception:
                pass

        self.pack(fill="both", expand=True)

    def _init_style(self) -> None:
        self.style = getattr(self.root, "style", None)
        if self.style is None:
            self.style = ttk.Style(self.root)
            try:
                self.style.theme_use("clam")
            except Exception:
                pass

        # 全局字体与紧凑内边距，防高 DPI 截断
        default_font = ("Microsoft YaHei UI", 9)
        self.root.option_add("*Font", default_font)

        self.style.configure(".", font=default_font)
        self.style.configure("TButton", padding=(3, 1))
        self.style.configure("Small.TButton", font=("Microsoft YaHei UI", 8), padding=(2, 1))
        self.style.configure("TLabel", padding=1)
        self.style.configure("TCheckbutton", padding=1)
        self.style.configure("TRadiobutton", padding=1)
        self.style.configure("Treeview", rowheight=24, font=("Microsoft YaHei UI", 9))
        self.style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_layout(self) -> None:
        # 0. 顶部应用导航与主题控制栏
        self._build_header_bar()

        # 1. 主分割窗格 (上下分割：上方功能面板，下方日志监视)
        self.main_paned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        self.main_paned.pack(fill="both", expand=True, padx=4, pady=4)

        # 上半部分：选项卡容器
        self.notebook = ttk.Notebook(self.main_paned)
        self.main_paned.add(self.notebook, weight=7)

        # 延迟载入 SlavePanel 和 PollPanel
        from .slave_panel import SlavePanel
        from .poll_panel import PollPanel

        self.slave_panel = SlavePanel(self.notebook, self.app)
        self.poll_panel = PollPanel(self.notebook, self.app)

        self.notebook.add(self.slave_panel, text="  💻 Modbus Slave 从机模拟器 (业务库导入与仿真)  ")
        self.notebook.add(self.poll_panel, text="  🔍 Modbus Poll 主机轮询调试器 (业务规则导入)  ")

        # 下半部分：高可靠性日志面板
        self.log_container = ttk.Frame(self.main_paned)
        self.main_paned.add(self.log_container, weight=3)

        self._build_log_viewer(self.log_container)

    def _build_header_bar(self) -> None:
        """构建应用顶部标语与动态主题切换控制栏"""
        header_bar = ttk.Frame(self)
        header_bar.pack(fill="x", padx=6, pady=(3, 2))

        # 左侧：品牌与版本标语
        lbl_brand = ttk.Label(
            header_bar,
            text="⚡ Modbus Studio  ·  现代化多实例仿真与轮询调试平台",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        lbl_brand.pack(side="left", padx=2)

        # 右侧：实时主题切换控制器
        theme_box = ttk.Frame(header_bar)
        theme_box.pack(side="right", padx=2)

        lbl_theme = ttk.Label(theme_box, text="🎨 界面主题:")
        lbl_theme.pack(side="left", padx=(4, 2))

        # 探测当前环境支持的主题列表
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
        theme = self.combo_theme.get()
        if theme:
            self._apply_theme(theme)

    def _toggle_light_dark(self) -> None:
        current = self.combo_theme.get() or "bootstrap-light"
        if "dark" in current.lower():
            target = current.replace("dark", "light")
            if self.style and hasattr(self.style, "theme_names") and target not in self.style.theme_names():
                target = "bootstrap-light"
        else:
            target = current.replace("light", "dark")
            if self.style and hasattr(self.style, "theme_names") and target not in self.style.theme_names():
                target = "bootstrap-dark"
        self._apply_theme(target)

    def _apply_theme(self, theme_name: str) -> None:
        try:
            if self.style:
                self.style.theme_use(theme_name)
            self.combo_theme.set(theme_name)

            # 自适应优化终端日志底色与高亮对比度
            is_dark = "dark" in theme_name.lower()
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

            # 持久化存储主题偏好
            self._save_theme_preference(theme_name)
        except Exception as e:
            messagebox.showwarning("主题切换失败", f"无法切换至主题 [{theme_name}]: {e}")

    def _save_theme_preference(self, theme_name: str) -> None:
        settings_file = os.path.abspath("settings.json")
        try:
            data = {}
            if os.path.exists(settings_file):
                try:
                    with open(settings_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data["theme"] = theme_name
            with open(settings_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _build_log_viewer(self, parent: ttk.Frame) -> None:
        # 日志标题栏与操作按钮条 (单行自适应紧凑排布)
        title_bar = ttk.Frame(parent)
        title_bar.pack(fill="x", padx=2, pady=2)

        lbl_title = ttk.Label(title_bar, text="实时系统与通信报文日志 (可上下拖动分割栏调整高度)", font=("Microsoft YaHei UI", 9, "bold"))
        lbl_title.pack(side="left", padx=2)

        # 工具栏容器
        tool_frame = ttk.Frame(parent)
        tool_frame.pack(fill="x", padx=2, pady=1)

        # 按钮统一紧凑内边距，去除硬编码字符宽，自适应内容
        btn_copy = ttk.Button(tool_frame, text="📋 复制", style="Small.TButton", command=self._copy_log)
        btn_copy.pack(side="left", padx=2)

        btn_select_all = ttk.Button(tool_frame, text="☑ 全选", style="Small.TButton", command=self._select_all_log)
        btn_select_all.pack(side="left", padx=2)

        btn_clear = ttk.Button(tool_frame, text="🧹 清空", style="Small.TButton", command=self._clear_log)
        btn_clear.pack(side="left", padx=2)

        btn_export = ttk.Button(tool_frame, text="💾 导出CSV", style="Small.TButton", command=self._export_log_to_csv)
        btn_export.pack(side="left", padx=2)

        btn_dir = ttk.Button(tool_frame, text="📂 日志目录", style="Small.TButton", command=self._open_log_dir)
        btn_dir.pack(side="left", padx=2)

        self.auto_scroll_var = tk.BooleanVar(value=True)
        chk_scroll = ttk.Checkbutton(tool_frame, text="自动滚屏", variable=self.auto_scroll_var)
        chk_scroll.pack(side="left", padx=6)

        self.show_traffic_var = tk.BooleanVar(value=True)
        chk_traffic = ttk.Checkbutton(tool_frame, text="显示问询与响应报文", variable=self.show_traffic_var)
        chk_traffic.pack(side="left", padx=4)

        # 文本框与滚动条
        text_frame = ttk.Frame(parent)
        text_frame.pack(fill="both", expand=True, padx=2, pady=1)

        self.log_scroll = ttk.Scrollbar(text_frame, orient="vertical")
        self.log_scroll.pack(side="right", fill="y")

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

        # 配置日志颜色高亮标签
        self.log_text.tag_config("SYS", foreground="#61afef")      # 浅蓝
        self.log_text.tag_config("RX", foreground="#98c379")       # 浅绿
        self.log_text.tag_config("TX", foreground="#e5c07b")       # 浅黄
        self.log_text.tag_config("ERROR", foreground="#e06c75")    # 红色

        self._max_log_lines = 1500  # 最大内存保留行数，超限自动修剪，防止内存泄漏和卡顿

    def _flush_log_loop(self) -> None:
        """主线程定时批量刷新日志（批量节流合并写入，从根本上防止高频通信导致界面卡死闪退）"""
        try:
            entries: List[LogEntry] = self.app.logging_service.drain_batch(max_count=200)
            if entries:
                show_traffic = self.show_traffic_var.get()
                self.log_text.config(state="normal")

                # 批量拼接与批量插入
                for entry in entries:
                    if not show_traffic and entry.direction in ("RX", "TX"):
                        continue
                    line_str = entry.format_line()
                    self.log_text.insert("end", line_str, entry.direction)

                # 行数超限清理（修剪最前面的旧行）
                try:
                    num_lines = int(float(self.log_text.index("end-1c")))
                    if num_lines > self._max_log_lines:
                        trim_to = num_lines - self._max_log_lines
                        self.log_text.delete("1.0", f"{trim_to}.0")
                except Exception:
                    pass

                # 自动滚屏
                if self.auto_scroll_var.get():
                    self.log_text.see("end")

                self.log_text.config(state="normal")
        except Exception as e:
            pass
        finally:
            # 持续周期循环，保持 80ms 刷新率
            self.root.after(80, self._flush_log_loop)

    def _copy_log(self) -> None:
        try:
            sel = self.log_text.get("sel.first", "sel.last")
            if sel:
                self.root.clipboard_clear()
                self.root.clipboard_append(sel)
        except tk.TclError:
            # 未选中文本时全量复制
            all_txt = self.log_text.get("1.0", "end-1c")
            if all_txt:
                self.root.clipboard_clear()
                self.root.clipboard_append(all_txt)

    def _select_all_log(self) -> None:
        self.log_text.tag_add("sel", "1.0", "end")

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", "end")

    def _open_log_dir(self) -> None:
        log_dir = self.app.logging_service.log_dir
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(log_dir)
        else:
            subprocess.Popen(["xdg-open", log_dir])

    def _export_log_to_csv(self) -> None:
        content = self.log_text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showinfo("提示", "当前日志视窗中无数据可导出")
            return

        save_path = filedialog.asksaveasfilename(
            title="导出当前视窗日志",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            initialfile=f"modbus_log_export_{time.strftime('%Y%m%d_%H%M%S')}.csv",
        )
        if not save_path:
            return

        try:
            with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["时间戳", "方向", "日志明细"])
                for line in content.splitlines():
                    if line.strip():
                        if line.startswith("[") and "]" in line:
                            bracket_end = line.find("]")
                            ts = line[1:bracket_end]
                            detail = line[bracket_end + 1:].strip()
                            writer.writerow([ts, "", detail])
                        else:
                            writer.writerow(["", "", line])

            messagebox.showinfo("导出成功", f"日志已成功导出至：\n{save_path}")
        except Exception as e:
            messagebox.showerror("导出失败", f"写入文件失败: {e}")
