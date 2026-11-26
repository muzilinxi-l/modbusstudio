"""
    历史通信日志检索与按条件导出弹窗模块（基于按日 SQLite WAL 日志库）。
    支持按日期、方向、从机 ID、关键字毫秒级分页查询，并将筛选结果导出为轻量 CSV 供 Excel 查看。
"""

from __future__ import annotations
import csv
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, List, Optional, Tuple


class LogQueryDialog:
    """历史 SQLite 通信日志条件查询、分页浏览与筛选导出弹窗。"""

    PAGE_SIZE = 1000

    def __init__(self, parent: tk.Widget, app: Any) -> None:
        self.parent = parent
        self.app = app
        self._page = 0
        self._total = 0
        self._current_rows: List[Tuple[str, str, int, str, str]] = []

        self.dlg = tk.Toplevel(parent)
        self.dlg.title("历史日志查询 (SQLite 按日分库)")
        self.dlg.geometry("1040x620")
        self.dlg.minsize(820, 460)
        self.dlg.transient(parent.winfo_toplevel())
        self.dlg.grab_set()

        self._set_dialog_icon()
        self._build_ui()
        self._load_dates_and_query()

    def _set_dialog_icon(self) -> None:
        from ..main_window import get_app_icon_path
        icon_path = get_app_icon_path()
        if icon_path and os.path.exists(icon_path):
            try:
                self.dlg.iconbitmap(icon_path)
            except Exception:
                pass

    def _build_ui(self) -> None:
        # 1. 顶部筛选工具栏
        filter_bar = ttk.Frame(self.dlg)
        filter_bar.pack(fill="x", padx=12, pady=(10, 6))

        ttk.Label(filter_bar, text="日期:").pack(side="left", padx=(0, 3))
        self.date_var = tk.StringVar()
        self.combo_date = ttk.Combobox(filter_bar, textvariable=self.date_var, state="readonly", width=11)
        self.combo_date.pack(side="left", padx=(0, 8))

        ttk.Label(filter_bar, text="方向:").pack(side="left", padx=(0, 3))
        self.dir_var = tk.StringVar(value="全部")
        self.combo_dir = ttk.Combobox(
            filter_bar,
            textvariable=self.dir_var,
            state="readonly",
            width=7,
            values=["全部", "RX", "TX", "SYS", "ERROR"],
        )
        self.combo_dir.pack(side="left", padx=(0, 8))

        ttk.Label(filter_bar, text="从机ID:").pack(side="left", padx=(0, 3))
        self.sid_var = tk.StringVar()
        entry_sid = ttk.Entry(filter_bar, textvariable=self.sid_var, width=6)
        entry_sid.pack(side="left", padx=(0, 8))

        ttk.Label(filter_bar, text="关键字:").pack(side="left", padx=(0, 3))
        self.kw_var = tk.StringVar()
        entry_kw = ttk.Entry(filter_bar, textvariable=self.kw_var, width=22)
        entry_kw.pack(side="left", padx=(0, 8))

        btn_search = ttk.Button(filter_bar, text="查询", style="Small.TButton", command=self._on_search_click)
        btn_search.pack(side="left", padx=2)

        btn_reset = ttk.Button(filter_bar, text="重置", style="Small.TButton", command=self._on_reset_click)
        btn_reset.pack(side="left", padx=2)

        btn_export = ttk.Button(filter_bar, text="导出筛选CSV", style="Small.TButton", command=self._export_filtered_csv)
        btn_export.pack(side="right", padx=2)

        entry_sid.bind("<Return>", lambda _e: self._on_search_click())
        entry_kw.bind("<Return>", lambda _e: self._on_search_click())
        self.combo_date.bind("<<ComboboxSelected>>", lambda _e: self._on_search_click())
        self.combo_dir.bind("<<ComboboxSelected>>", lambda _e: self._on_search_click())

        # 2. 中部结果表格
        table_frame = ttk.Frame(self.dlg)
        table_frame.pack(fill="both", expand=True, padx=12, pady=4)

        scroll_y = ttk.Scrollbar(table_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")
        scroll_x = ttk.Scrollbar(table_frame, orient="horizontal")
        scroll_x.pack(side="bottom", fill="x")

        self.tree = ttk.Treeview(
            table_frame,
            columns=("ts", "direction", "slave_id", "message", "raw_hex"),
            show="headings",
            yscrollcommand=scroll_y.set,
            xscrollcommand=scroll_x.set,
        )
        scroll_y.config(command=self.tree.yview)
        scroll_x.config(command=self.tree.xview)
        self.tree.pack(fill="both", expand=True)

        cols = [
            ("ts", "时间戳", 170, "center"),
            ("direction", "方向", 64, "center"),
            ("slave_id", "从机ID", 64, "center"),
            ("message", "概要信息", 480, "w"),
            ("raw_hex", "原始报文Hex", 220, "w"),
        ]
        for cid, chead, cw, calign in cols:
            self.tree.heading(cid, text=chead)
            self.tree.column(cid, width=cw, anchor=calign)

        # 3. 底部分页与状态栏
        bottom_bar = ttk.Frame(self.dlg)
        bottom_bar.pack(fill="x", padx=12, pady=(4, 10))

        self.lbl_status = ttk.Label(bottom_bar, text="就绪", foreground="#6c757d")
        self.lbl_status.pack(side="left")

        btn_close = ttk.Button(bottom_bar, text="关闭", style="Small.TButton", command=self.dlg.destroy)
        btn_close.pack(side="right", padx=(6, 0))

        self.btn_next = ttk.Button(bottom_bar, text="下一页", style="Small.TButton", command=self._next_page)
        self.btn_next.pack(side="right", padx=2)

        self.btn_prev = ttk.Button(bottom_bar, text="上一页", style="Small.TButton", command=self._prev_page)
        self.btn_prev.pack(side="right", padx=2)

    def _load_dates_and_query(self) -> None:
        dates = self.app.logging_service.list_log_dates()
        if not dates:
            today = time.strftime("%Y%m%d")
            dates = [today]
        self.combo_date["values"] = dates
        self.date_var.set(dates[0])
        self._page = 0
        self._run_query()

    def _parse_filters(self) -> Optional[Tuple[str, Optional[str], Optional[int], str]]:
        date_str = self.date_var.get().strip() or time.strftime("%Y%m%d")
        direction = self.dir_var.get().strip()
        sid_raw = self.sid_var.get().strip()
        sid_val: Optional[int] = None
        if sid_raw:
            try:
                sid_val = int(sid_raw)
            except ValueError:
                messagebox.showwarning("参数错误", f"从机ID 必须为整数：{sid_raw}", parent=self.dlg)
                return None
        kw = self.kw_var.get().strip()
        return date_str, direction, sid_val, kw

    def _on_search_click(self) -> None:
        self._page = 0
        self._run_query()

    def _on_reset_click(self) -> None:
        self.dir_var.set("全部")
        self.sid_var.set("")
        self.kw_var.set("")
        self._page = 0
        self._run_query()

    def _prev_page(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._run_query()

    def _next_page(self) -> None:
        if (self._page + 1) * self.PAGE_SIZE < self._total:
            self._page += 1
            self._run_query()

    def _run_query(self) -> None:
        parsed = self._parse_filters()
        if parsed is None:
            return
        date_str, direction, sid_val, kw = parsed
        t0 = time.perf_counter()
        total, rows = self.app.logging_service.query_logs(
            date_str=date_str,
            direction=direction,
            slave_id=sid_val,
            keyword=kw,
            limit=self.PAGE_SIZE,
            offset=self._page * self.PAGE_SIZE,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._total = total
        self._current_rows = rows

        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in rows:
            self.tree.insert("", "end", values=row)

        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.lbl_status.config(
            text=f"共命中 {total} 条 | 第 {self._page + 1}/{total_pages} 页（本页 {len(rows)} 条）| 耗时 {elapsed_ms:.1f} ms"
        )
        self.btn_prev.config(state="normal" if self._page > 0 else "disabled")
        self.btn_next.config(state="normal" if (self._page + 1) * self.PAGE_SIZE < total else "disabled")

    def _export_filtered_csv(self) -> None:
        parsed = self._parse_filters()
        if parsed is None:
            return
        date_str, direction, sid_val, kw = parsed
        if self._total <= 0:
            messagebox.showinfo("提示", "当前筛选条件下没有可导出的日志记录", parent=self.dlg)
            return

        save_path = filedialog.asksaveasfilename(
            title="导出筛选后的日志为 CSV",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            initialfile=f"modbus_log_{date_str}_filtered.csv",
            parent=self.dlg,
        )
        if not save_path:
            return

        # 导出最多 50000 条筛选结果，兼顾完整与 Excel 打开速度
        export_limit = 50000
        total, rows = self.app.logging_service.query_logs(
            date_str=date_str,
            direction=direction,
            slave_id=sid_val,
            keyword=kw,
            limit=export_limit,
            offset=0,
        )
        try:
            with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["时间戳", "方向", "从机ID", "概要信息", "原始报文Hex"])
                writer.writerows(rows)
            extra_tip = f"\n（命中 {total} 条，已导出最新 {len(rows)} 条）" if total > export_limit else ""
            messagebox.showinfo("导出成功", f"已成功导出 {len(rows)} 条记录至：\n{save_path}{extra_tip}", parent=self.dlg)
        except Exception as e:
            messagebox.showerror("导出失败", f"写入 CSV 失败: {e}", parent=self.dlg)
