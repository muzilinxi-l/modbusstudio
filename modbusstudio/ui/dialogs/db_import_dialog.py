"""
Modbus Studio - 数据库点表与设备模型批量导入弹窗 (DbImportDialog)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
遵循行业顶级规范构建，支持多选批量设备导入、独立实例创建与严格配额控制。
"""

from __future__ import annotations
import logging
import os
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...models import (
    CommType,
    ConnectionConfig,
    MAX_RTU_SLAVE_INSTANCES,
    MAX_TCP_SLAVE_INSTANCES,
    SlaveDevice,
)
from ...services import PointService

logger = logging.getLogger("DbImportDialog")


class DbImportDialog:
    """业务数据库设备模型与点表批量导入对话框

    职责：
    1. 解析业务数据库中的设备与点表模型并呈现结构化视图；
    2. 支持单选与多选批量选择，提供一键全选/已使能快捷选择工具；
    3. 支持创建独立从机服务实例、覆盖替换、追加合并三种导入策略；
    4. 严格执行 Modbus TCP (100个) 与 Modbus RTU (100个) 独立容量配额硬限制；
    5. 具备窗口尺寸动态自适应感知机制，适配各类屏幕分辨率与 DPI。
    """

    def __init__(
        self,
        parent: tk.Widget,
        app: Any,
        on_imported_callback: Callable[[SlaveDevice], None],
        current_device_id: Optional[str] = None,
    ) -> None:
        """初始化导入对话框

        Args:
            parent: 父级窗口或控件容器
            app: 应用程序全局上下文对象 (包含 slave_service, logging_service 等)
            on_imported_callback: 导入成功后的回调通知函数 (传递最后选中或激活的从机设备)
            current_device_id: 当前正在查看的从机设备 ID (用于替换/追加模式)
        """
        self.parent = parent
        self.app = app
        self.on_imported = on_imported_callback
        self.current_device_id = current_device_id

        self.dlg = tk.Toplevel(parent)
        self.dlg.title("从业务数据库导入设备模型与点表 (支持多选批量导入)")
        self.dlg.geometry("1060x640")
        self.dlg.minsize(960, 540)
        self.dlg.resizable(True, True)
        self.dlg.transient(parent.winfo_toplevel())
        self.dlg.grab_set()

        self._set_dialog_icon()

        # 加载数据库设备数据
        from ...hems_db_loader import HemsDatabase
        if not self.app.db_path or not os.path.exists(self.app.db_path):
            messagebox.showwarning("提示", "未找到有效的业务数据库，请先关联数据库！", parent=self.dlg)
            self.dlg.destroy()
            return

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

        try:
            self._build_ui()
            self._populate_data()
        except Exception as ex:
            import traceback
            logger.error(f"构建导入弹窗界面异常: {ex}\n{traceback.format_exc()}")
            messagebox.showerror("界面加载失败", f"渲染弹窗发生异常:\n{ex}", parent=self.dlg)
            self.dlg.destroy()

    def _set_dialog_icon(self) -> None:
        """设置弹窗左上角与任务栏图标"""
        from ..main_window import get_app_icon_path
        icon_path = get_app_icon_path()
        if icon_path and os.path.exists(icon_path):
            try:
                self.dlg.iconbitmap(icon_path)
            except Exception:
                pass

    def _build_ui(self) -> None:
        """构建弹窗 UI 组件结构"""
        # 1. 顶部控制面板（筛选、搜索与批量选择工具）
        top_frame = ttk.Frame(self.dlg)
        top_frame.pack(fill="x", padx=14, pady=(10, 4))

        # 筛选单选按钮组
        ttk.Label(top_frame, text="设备分类筛选:", font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 6))
        self.type_var = tk.StringVar(value="all")
        ttk.Radiobutton(top_frame, text="全部业务模块", variable=self.type_var, value="all").pack(side="left", padx=4)
        ttk.Radiobutton(top_frame, text="Type=1 (南向采集设备)", variable=self.type_var, value="1").pack(side="left", padx=4)
        ttk.Radiobutton(top_frame, text="Type=2 (北向对外服务)", variable=self.type_var, value="2").pack(side="left", padx=4)

        # 搜索框
        self.search_var = tk.StringVar()
        entry_search = ttk.Entry(top_frame, textvariable=self.search_var, width=18)
        entry_search.pack(side="right")
        ttk.Label(top_frame, text="🔍 快速搜索:").pack(side="right", padx=(10, 2))

        # 2. 快捷选择操作条 (提高多选效率)
        tools_frame = ttk.Frame(self.dlg)
        tools_frame.pack(fill="x", padx=14, pady=(2, 4))

        ttk.Label(tools_frame, text="批量勾选辅助:", foreground="#6c757d").pack(side="left", padx=(0, 6))
        ttk.Button(tools_frame, text="全选当前列表", style="Small.TButton", command=self._select_all_visible).pack(side="left", padx=3)
        ttk.Button(tools_frame, text="全选已使能设备", style="Small.TButton", command=self._select_all_enabled).pack(side="left", padx=3)
        ttk.Button(tools_frame, text="反向选择", style="Small.TButton", command=self._invert_selection).pack(side="left", padx=3)
        ttk.Button(tools_frame, text="清空选择", style="Small.TButton", command=self._clear_selection).pack(side="left", padx=3)

        self.lbl_selected_status = ttk.Label(tools_frame, text="当前已选中: 0 个设备", foreground="#0d6efd", font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_selected_status.pack(side="right", padx=6)

        # 3. 设备列表表格 (使用 extended 多选模式)
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

        # 动态自适应列配置
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

        self.table_frame.bind("<Configure>", self._on_table_resize)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection_changed)
        self.tree.bind("<Double-1>", lambda event: self._execute_import())

        # 4. 导入模式选择与限制提示
        mode_frame = ttk.LabelFrame(self.dlg, text="导入模式与规则配置 (支持多选一次性批量导入)")
        mode_frame.pack(fill="x", padx=14, pady=6)

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

        # 5. 底部按钮控制区
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

        # 事件监听
        self.search_var.trace_add("write", lambda *args: self._filter_tree())
        self.type_var.trace_add("write", lambda *args: self._filter_tree())

    def _on_table_resize(self, event=None) -> None:
        """动态感知容器宽度变化，等比例调整列宽"""
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return
        width = event.width if event else self.table_frame.winfo_width()
        avail_w = width - 25
        if avail_w < 400:
            return
        for cid, chead, ratio, min_w, align in self.col_specs:
            w = max(min_w, int(avail_w * ratio))
            self.tree.column(cid, width=w)

    def _populate_data(self) -> None:
        """预先解析并缓存业务设备模型与点位数据"""
        self.app_item_map: Dict[int, Tuple[Any, List[Dict[str, Any]], str, str, str, str]] = {}
        for a in self.apps:
            pts = a.extract_studio_points()
            role_tag = "南向物理设备 (Type=1)" if a.is_south_master else "北向转发从机 (Type=2)" if a.is_north_slave else f"业务模块 (Type={a.app_type})"
            comm_str = f"串口 {a.serial_info.get('port_name', 'RS-485')}" if a.comm_type == "RTU" else f"TCP {a.ip}:{a.port}"
            pt_count_str = f"{len(pts)} 点"
            enable_tag = "✅ 已使能" if a.enable == 1 else "⚪ 未使能"
            self.app_item_map[a.app_id] = (a, pts, role_tag, comm_str, pt_count_str, enable_tag)

        self._filter_tree()

    def _filter_tree(self) -> None:
        """根据分类和搜索关键字过滤呈现设备"""
        for item in self.tree.get_children():
            self.tree.delete(item)

        kw = self.search_var.get().strip().lower()
        sel_type = self.type_var.get()

        first_item = None
        for app_id, (a, pts, role_tag, comm_str, pt_count_str, enable_tag) in self.app_item_map.items():
            if sel_type == "1" and a.app_type != 1:
                continue
            if sel_type == "2" and a.app_type != 2:
                continue

            disp_name = a.chinese_name or a.english_name
            if kw and (kw not in disp_name.lower() and kw not in a.english_name.lower() and kw not in str(app_id)):
                continue

            item_id = self.tree.insert(
                "",
                "end",
                iid=str(app_id),
                values=(app_id, enable_tag, disp_name, a.english_name, role_tag, comm_str, pt_count_str),
            )
            if first_item is None:
                first_item = item_id

        if first_item:
            self.tree.selection_set(first_item)
            self.tree.focus(first_item)
        self._on_tree_selection_changed()

    def _on_tree_selection_changed(self, event=None) -> None:
        """选定项变化时更新状态栏计数"""
        selected_count = len(self.tree.selection())
        self.lbl_selected_status.config(text=f"当前已选中: {selected_count} 个设备")

    def _select_all_visible(self) -> None:
        """全选当前过滤出的所有设备"""
        all_items = self.tree.get_children()
        self.tree.selection_set(all_items)
        self._on_tree_selection_changed()

    def _select_all_enabled(self) -> None:
        """仅全选当前列表中处于已使能状态的设备"""
        enabled_items = []
        for item in self.tree.get_children():
            vals = self.tree.item(item, "values")
            if vals and len(vals) > 1 and "已使能" in str(vals[1]):
                enabled_items.append(item)
        self.tree.selection_set(enabled_items)
        self._on_tree_selection_changed()

    def _invert_selection(self) -> None:
        """反选操作"""
        all_items = set(self.tree.get_children())
        selected = set(self.tree.selection())
        new_sel = list(all_items - selected)
        self.tree.selection_set(new_sel)
        self._on_tree_selection_changed()

    def _clear_selection(self) -> None:
        """清空所有选中"""
        self.tree.selection_set([])
        self._on_tree_selection_changed()

    def _execute_import(self) -> None:
        """执行导入流程：涵盖多选批量导入与 TCP/RTU 独立上限限制"""
        try:
            selected_ids = list(self.tree.selection())
            if not selected_ids:
                messagebox.showwarning("提示", "请先在列表中选中至少一个业务设备！", parent=self.dlg)
                return

            mode = self.import_mode_var.get()
            selected_apps_data = []
            for sel_str in selected_ids:
                app_id = int(sel_str)
                if app_id in self.app_item_map:
                    selected_apps_data.append(self.app_item_map[app_id])

            if not selected_apps_data:
                return

            if mode == "new_instance":
                self._import_as_new_instances(selected_apps_data)
            else:
                self._import_merge_to_current(selected_apps_data, is_replace=(mode == "replace"))

        except Exception as ex:
            import traceback
            logger.error(f"批量导入执行发生异常: {ex}\n{traceback.format_exc()}")
            messagebox.showerror("导入失败", f"导入过程中发生异常:\n{ex}", parent=self.dlg)

    def _import_as_new_instances(self, apps_data_list: List[Tuple[Any, List[Dict[str, Any]], str, str, str, str]]) -> None:
        """批量新建独立从机服务实例，严格实施 TCP / RTU 独立 100 个配额管理"""
        slave_svc = self.app.slave_service
        current_tcp_cnt = slave_svc.count_devices_by_comm(CommType.TCP)
        current_rtu_cnt = slave_svc.count_devices_by_comm(CommType.RTU)

        to_import_tcp = sum(1 for (a, _, _, _, _, _) in apps_data_list if str(a.comm_type).upper() != "RTU")
        to_import_rtu = sum(1 for (a, _, _, _, _, _) in apps_data_list if str(a.comm_type).upper() == "RTU")

        # 检查配额硬限制
        exceed_messages = []
        if current_tcp_cnt + to_import_tcp > MAX_TCP_SLAVE_INSTANCES:
            exceed_messages.append(
                f"Modbus TCP 从机将超限: 当前已有 {current_tcp_cnt} 个，拟导入 {to_import_tcp} 个，上限为 {MAX_TCP_SLAVE_INSTANCES} 个！"
            )
        if current_rtu_cnt + to_import_rtu > MAX_RTU_SLAVE_INSTANCES:
            exceed_messages.append(
                f"Modbus RTU 从机将超限: 当前已有 {current_rtu_cnt} 个，拟导入 {to_import_rtu} 个，上限为 {MAX_RTU_SLAVE_INSTANCES} 个！"
            )

        if exceed_messages:
            msg = "\n".join(exceed_messages)
            msg += "\n\n系统设置每个通信协议最多承载 100 个从机服务实例以确保工业通信稳定性。请缩减所选设备或清理现有从机后再试！"
            messagebox.showerror("实例配额限制", msg, parent=self.dlg)
            return

        # 确认批量导入
        if len(apps_data_list) > 1:
            if not messagebox.askyesno(
                "确认批量导入",
                f"确定要一次性为选中的 {len(apps_data_list)} 个设备分别创建独立的从机服务实例吗？\n"
                f"- TCP 从机设备: {to_import_tcp} 个\n"
                f"- RTU 串口设备: {to_import_rtu} 个",
                parent=self.dlg,
            ):
                return

        created_devices: List[SlaveDevice] = []
        total_points_imported = 0
        existing_devices = slave_svc.get_devices()
        next_unit = len(existing_devices) + 1
        base_port = 502 + len(existing_devices)

        for idx, (sel_app, points_data, role_tag, comm_str, pt_cnt_str, enable_tag) in enumerate(apps_data_list):
            unit_id = next_unit + idx
            disp_name = sel_app.chinese_name or sel_app.english_name
            new_name = f"从机{unit_id} [{disp_name}]"
            new_id = f"slave_{time.time_ns()}_{idx}"

            is_rtu = (str(sel_app.comm_type).upper() == "RTU")
            comm_mode = CommType.RTU if is_rtu else CommType.TCP
            s_info = getattr(sel_app, "serial_info", {}) or {}

            # 解析串口参数
            baud = 9600
            try:
                baud = int(s_info.get("baudrate", 9600) or 9600)
            except Exception:
                pass

            databit = 8
            try:
                databit = int(s_info.get("databit", 8) or 8)
            except Exception:
                pass

            parity = str(s_info.get("parity", "N") or "N").strip().upper()
            if parity not in ("N", "E", "O"):
                parity = "N"

            stopbit = 1
            try:
                stopbit = int(float(str(s_info.get("stopbit", 1) or 1)))
            except Exception:
                pass

            com_name = str(s_info.get("port_name", "COM1") or "COM1")
            ip_addr = getattr(sel_app, "ip", "127.0.0.1") or "127.0.0.1"
            try:
                port_num = int(getattr(sel_app, "port", base_port + idx) or (base_port + idx))
            except Exception:
                port_num = base_port + idx

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

            device = SlaveDevice(
                id=new_id,
                name=new_name,
                conn_config=cfg,
                db_device_id=sel_app.app_id,
                db_device_name=disp_name,
            )

            # 装配点位
            for pt in points_data:
                self._load_single_point_to_device(device, pt)

            ok, err_msg = slave_svc.register_device(device)
            if ok:
                created_devices.append(device)
                total_points_imported += len(device.points)
            else:
                logger.error(f"注册从机实例失败 [{new_name}]: {err_msg}")

        self.dlg.destroy()

        if created_devices:
            last_dev = created_devices[-1]
            self.on_imported(last_dev)

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

    def _import_merge_to_current(
        self,
        apps_data_list: List[Tuple[Any, List[Dict[str, Any]], str, str, str, str]],
        is_replace: bool,
    ) -> None:
        """覆盖替换或追加合并到当前激活的从机服务实例"""
        slave_svc = self.app.slave_service
        if not self.current_device_id:
            messagebox.showwarning("提示", "当前主界面未选定目标从机实例！", parent=self.dlg)
            return

        target_dev = slave_svc.get_device(self.current_device_id)
        if not target_dev:
            messagebox.showerror("错误", f"未找到当前从机实例: {self.current_device_id}", parent=self.dlg)
            return

        if is_replace:
            target_dev.points.clear()
            names = [sel_app.chinese_name or sel_app.english_name for (sel_app, _, _, _, _, _) in apps_data_list]
            summary_name = " + ".join(names[:3]) + (f" 等{len(names)}个设备" if len(names) > 3 else "")
            target_dev.name = f"从机{target_dev.conn_config.unit_id} [{summary_name}]"

        imported_cnt = 0
        for sel_app, points_data, role_tag, comm_str, pt_cnt_str, enable_tag in apps_data_list:
            for pt in points_data:
                if self._load_single_point_to_device(target_dev, pt):
                    imported_cnt += 1

        self.dlg.destroy()
        self.on_imported(target_dev)

        mode_text = "覆盖替换" if is_replace else "追加合并"
        msg = f"已成功完成【{mode_text}】！\n\n目标从机: [{target_dev.name}]\n当前总点位数: {len(target_dev.points)} 点"
        messagebox.showinfo("导入完成", msg, parent=self.parent.winfo_toplevel())
        self.app.logging_service.post("SYS", target_dev.conn_config.unit_id, f"成功完成点表{mode_text}，当前点位总数: {len(target_dev.points)}")

    def _load_single_point_to_device(self, target_dev: SlaveDevice, pt: Dict[str, Any]) -> bool:
        """辅助方法：校验并将单点装配写入从机设备实体及底层引擎"""
        try:
            addr = int(pt.get("address", 0))
            desc = str(pt.get("desc", f"Point_{addr}"))
            dt = pt.get("data_type", "INT16")
            bo = pt.get("byte_order", "CDAB")
            area = pt.get("area", "4x")
            val = pt.get("current_val", 0)
            sim = pt.get("sim_mode", "固定")
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
            if ok and point:
                target_dev.add_point(point)
                if target_dev.is_running:
                    self.app.slave_service.sync_point_to_runtime(target_dev.id, point)
                return True
            return False
        except Exception:
            return False
