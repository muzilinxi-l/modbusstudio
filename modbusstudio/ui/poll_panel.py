"""
    Modbus Studio 主机轮询工作台面板模块，负责收集主机查询参数、调用 PollService 并展示寄存器多格式解算结果。
    关键导入：ThreadPoolExecutor（单次连接与读取异步线程池）、tkinter/ttk/messagebox（GUI 组件）、models/PollService（领域模型与轮询服务）。
对外暴露：PollPanel（主机轮询调试与实时监视工作台视图类）。
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import queue
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable, Dict, List

# 为什么用 try/except 导入 pyserial 的串口枚举工具：
# 允许在未安装 pyserial 或无硬件串口驱动的机器上正常打开界面并回退使用预设 COM1~COM4 列表。
try:
    import serial.tools.list_ports
except ImportError:
    serial = None

# 仅在静态类型检查阶段导入 ModbusStudioAppCore，避免视图层与根应用控制器产生运行期循环引用
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
    _parse_int,
    _parse_port,
)
from ..services import PollService


# =====================================================================
# 主机轮询调试与实时监视工作台 (PollPanel)
# 架构职责：
# 1. 纯视图层组件：收集目标 IP:Port / 串口参数、从机站号、存储区域、起始地址、数量及轮询间隔；
# 2. 异步非阻塞交互：单次连接与单次读取通过单工作线程池 `_executor` 执行，连续轮询委托 `PollService` 后台线程；
# 3. 跨线程 UI 安全：所有后台回调一律通过 `self.after(0, ...)` 切回 Tkinter 主线程更新控件与 Treeview。
# =====================================================================
class PollPanel(ttk.Frame):
    """主机轮询调试与实时监视工作台（全异步非阻塞，杜绝界面冻结）。"""

    # 下拉框可选的四大 Modbus 存储区域，统一按 0x → 1x → 3x → 4x 升序排列（默认选中 4x 保持寄存器）
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

    # 下拉框可选的 4 种 32 位寄存器字节变位模式
    BYTE_ORDER_CHOICES = ["ABCD", "CDAB", "BADC", "DCBA"]

    def __init__(self, parent: ttk.Notebook, app: ModbusStudioAppCore):
        """初始化主机轮询面板，构建单线程后台执行器、渲染 UI 控件并探测可用物理串口。"""
        super().__init__(parent)
        # 保存全局应用核心容器引用，用于访问 app.poll_service 与 app.logging_service
        self.app = app
        # 连续轮询运行状态标志
        self._is_polling_loop = False
        self._destroyed = False
        # 主线程安全 UI 回调队列与定时泵句柄（后台线程只入队，彻底实现零跨线程 Tk 调用）
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._ui_pump_after_id = None
        # 主机发送与成功接收计数统计，实时显示在右上角状态标签
        self._tx_count = 0
        self._rx_count = 0
        # 单次读取防重入忙标志，防止用户在网络超时等待期间连续狂点“单次读取”堆积请求
        self._is_poll_busy = False
        # 为什么使用 max_workers=1 的 ThreadPoolExecutor：
        # 1. 将耗时的 TCP 握手或串口打开/单次读取/断开移出 Tkinter 主线程，彻底杜绝界面卡死；
        # 2. 限制单工作线程可保证同一串口或 TCP 连接上的主机请求严格串行化，不会发生并发抢占锁冲突。
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="PollWorker")

        self._build_ui()
        self._refresh_poll_com_ports()
        self._ui_pump_after_id = self.after(30, self._drain_ui_queue)

    def _post_ui(self, fn: Callable[[], None]) -> None:
        """供后台工作线程将 UI 更新闭包安全入队，严禁后台线程直接调用任何 Tk API。"""
        if not self._destroyed:
            self._ui_queue.put(fn)

    def _drain_ui_queue(self) -> None:
        """主线程每 30ms 消费后台投递的 UI 更新回调，面板销毁后自动停止。"""
        if self._destroyed:
            return
        try:
            if not (hasattr(self, "winfo_exists") and self.winfo_exists()):
                self._destroyed = True
                return
            for _ in range(50):
                try:
                    cb = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    cb()
                except Exception:
                    pass
        finally:
            if not self._destroyed and hasattr(self, "winfo_exists") and self.winfo_exists():
                try:
                    self._ui_pump_after_id = self.after(30, self._drain_ui_queue)
                except Exception:
                    self._ui_pump_after_id = None

    def destroy(self) -> None:
        """销毁面板前取消定时泵、非阻塞停止后台连续轮询并回收执行线程池，避免残留线程访问已销毁控件。"""
        self._destroyed = True
        self._is_polling_loop = False
        if self._ui_pump_after_id is not None:
            try:
                self.after_cancel(self._ui_pump_after_id)
            except Exception:
                pass
            self._ui_pump_after_id = None
        try:
            self.app.poll_service.request_stop_polling_nonblocking()
        except Exception:
            pass
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        super().destroy()

    def _build_ui(self) -> None:
        """构建顶部两行连接与指令配置栏，以及下部 7 列寄存器多格式实时解算表格。"""
        self.pack(fill="both", expand=True, padx=4, pady=4)

        # 1. 顶部控制栏（采用两行紧凑布局：第 0 行配物理链路，第 1 行配寄存器问询指令）
        top_box = ttk.LabelFrame(self, text="主机连接与读取指令配置")
        top_box.pack(fill="x", padx=2, pady=2)

        # --- 第 0 行：目标通信参数与连接控制 ---
        row0 = ttk.Frame(top_box)
        row0.pack(fill="x", padx=4, pady=2)

        left0 = ttk.Frame(row0)
        left0.pack(side="left", fill="x", expand=True)

        # 通信方式单选切换（TCP 网口 vs RTU 串口）
        ttk.Label(left0, text="通讯方式:").pack(side="left", padx=(2, 2))
        self.poll_comm_type_var = tk.StringVar(value="TCP")
        rb_tcp = ttk.Radiobutton(left0, text="以太网 (TCP)", variable=self.poll_comm_type_var, value="TCP", command=self._on_comm_type_change)
        rb_tcp.pack(side="left", padx=2)
        rb_rtu = ttk.Radiobutton(left0, text="串口 (RTU / 485 / 232)", variable=self.poll_comm_type_var, value="RTU", command=self._on_comm_type_change)
        rb_rtu.pack(side="left", padx=2)

        # 动态连接参数容器：根据所选通信方式在 tcp_frame 与 rtu_frame 之间原地切换
        self.conn_container = ttk.Frame(left0)
        self.conn_container.pack(side="left", padx=4)

        # TCP 控件组：目标从机 IP 地址与监听端口（默认 127.0.0.1:502）
        self.tcp_frame = ttk.Frame(self.conn_container)
        ttk.Label(self.tcp_frame, text="目标IP:").pack(side="left", padx=1)
        self.entry_ip = ttk.Entry(self.tcp_frame, width=12)
        self.entry_ip.insert(0, "127.0.0.1")
        self.entry_ip.pack(side="left", padx=1)
        ttk.Label(self.tcp_frame, text="端口:").pack(side="left", padx=1)
        self.entry_port = ttk.Entry(self.tcp_frame, width=6)
        self.entry_port.insert(0, "502")
        self.entry_port.pack(side="left", padx=1)

        # RTU 控件组：串口号下拉框、热插拔刷新按钮与串口五要素（波特率/数据位/校验位/停止位）
        self.rtu_frame = ttk.Frame(self.conn_container)
        ttk.Label(self.rtu_frame, text="串口:").pack(side="left", padx=1)
        self.combo_com = ttk.Combobox(self.rtu_frame, width=8)
        self.combo_com.pack(side="left", padx=1)
        btn_refresh = ttk.Button(self.rtu_frame, text="刷新", width=4, style="Small.TButton", command=self._refresh_poll_com_ports)
        btn_refresh.pack(side="left", padx=1)
        ttk.Label(self.rtu_frame, text="波特率:").pack(side="left", padx=1)
        self.combo_baud = ttk.Combobox(self.rtu_frame, width=7, values=self.BAUDRATE_CHOICES)
        self.combo_baud.set("9600")
        self.combo_baud.pack(side="left", padx=1)
        ttk.Label(self.rtu_frame, text="数据位:").pack(side="left", padx=1)
        self.combo_databit = ttk.Combobox(self.rtu_frame, width=2, state="readonly", values=["7", "8"])
        self.combo_databit.set("8")
        self.combo_databit.pack(side="left", padx=1)
        ttk.Label(self.rtu_frame, text="校验:").pack(side="left", padx=1)
        self.combo_parity = ttk.Combobox(self.rtu_frame, width=7, state="readonly", values=["None (N)", "Even (E)", "Odd (O)"])
        self.combo_parity.set("None (N)")
        self.combo_parity.pack(side="left", padx=1)
        ttk.Label(self.rtu_frame, text="停止位:").pack(side="left", padx=1)
        self.combo_stopbit = ttk.Combobox(self.rtu_frame, width=2, state="readonly", values=["1", "2"])
        self.combo_stopbit.set("1")
        self.combo_stopbit.pack(side="left", padx=1)

        # 目标从机站地址（Unit ID，默认 1）
        ttk.Label(left0, text="从机 ID:").pack(side="left", padx=(4, 1))
        self.entry_unit_id = ttk.Entry(left0, width=4)
        self.entry_unit_id.insert(0, "1")
        self.entry_unit_id.pack(side="left", padx=1)

        right0 = ttk.Frame(row0)
        right0.pack(side="right")

        self.btn_connect = ttk.Button(right0, text="建立连接", style="primary.TButton", command=self._toggle_connect)
        self.btn_connect.pack(side="left", padx=3)

        self.lbl_conn_status = ttk.Label(right0, text="未连接", foreground="#6c757d", font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_conn_status.pack(side="left", padx=4)

        # 默认显示 TCP 参数输入框
        self._on_comm_type_change()

        # --- 第 1 行：读取指令配置与单次/连续轮询操作 ---
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

        # [POLL-CONFIG] 增加"间隔(ms)"输入框，默认 1000ms，有效范围 100~60000ms
        ttk.Label(left1, text="间隔(ms):").pack(side="left", padx=(4, 1))
        self.poll_interval_var = tk.IntVar(value=1000)
        self.entry_interval = ttk.Entry(left1, textvariable=self.poll_interval_var, width=6)
        self.entry_interval.pack(side="left", padx=1)

        btn_poll_once = ttk.Button(left1, text="单次读取", style="info.TButton", command=self._poll_once)
        btn_poll_once.pack(side="left", padx=(6, 2))

        # [POLL-CONFIG] 文案改为"连续轮询"，由用户输入的间隔(ms)动态驱动
        self.btn_poll_loop = ttk.Button(left1, text="连续轮询", style="success.TButton", command=self._toggle_poll_loop)
        self.btn_poll_loop.pack(side="left", padx=2)

        right1 = ttk.Frame(row1)
        right1.pack(side="right")

        self.lbl_stats = ttk.Label(right1, text="Tx: 0 | Rx: 0", font=("Consolas", 9))
        self.lbl_stats.pack(side="right", padx=4)

        # 2. 轮询监视数据表格：同屏并排展示 Hex、UINT16、INT16、FLOAT32(ABCD) 与 FLOAT32(CDAB) 解算值
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

        # 为什么同时展示 UINT16、INT16、FLOAT32(ABCD) 和 FLOAT32(CDAB) 四列解算结果：
        # 现场联调未知设备点表时，工程师无需反复切换类型或字节序，即可一眼看出哪种解算方式符合真实物理量（如电压 220.5V）。
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
        """响应通信方式单选按钮切换，动态显示 TCP 网口输入框或 RTU 串口输入框。"""
        mode = self.poll_comm_type_var.get()
        if mode == "RTU":
            self.tcp_frame.pack_forget()
            self.rtu_frame.pack(side="left")
        else:
            self.rtu_frame.pack_forget()
            self.tcp_frame.pack(side="left")

    def _refresh_poll_com_ports(self) -> None:
        """扫描本机当前可用的物理或虚拟串口并刷新串口下拉框列表。"""
        ports = []
        # 若安装了 pyserial 则动态枚举系统活跃串口，否则回退提供 COM1~COM4 默认选项
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
        """从顶部输入框收集并构建强类型的 ConnectionConfig 连接配置实体。"""
        # 对空白输入应用安全的工业默认值（127.0.0.1:502、COM1、9600、8-N-1、站号 1）
        parity_str = self.combo_parity.get() if hasattr(self, "combo_parity") else "None (N)"
        if "Even" in parity_str or parity_str.upper() == "E":
            parity_val = "E"
        elif "Odd" in parity_str or parity_str.upper() == "O":
            parity_val = "O"
        else:
            parity_val = "N"
        return ConnectionConfig(
            comm_type=CommType.from_str(self.poll_comm_type_var.get()),
            host=self.entry_ip.get().strip() or "127.0.0.1",
            port=_parse_port(self.entry_port.get(), default=502),
            com_port=self.combo_com.get().strip() or "COM1",
            baudrate=_parse_int(self.combo_baud.get(), default=9600, min_val=1),
            data_bits=_parse_int(self.combo_databit.get(), default=8, min_val=5, max_val=8) if hasattr(self, "combo_databit") else 8,
            parity=parity_val,
            stop_bits=_parse_int(self.combo_stopbit.get(), default=1, min_val=1, max_val=2) if hasattr(self, "combo_stopbit") else 1,
            unit_id=_parse_int(self.entry_unit_id.get(), default=1, min_val=1, max_val=247),
        )

    def _toggle_connect(self) -> None:
        """切换主机通信连接状态（连接/断开均交由单线程后台执行器串行处理，零跨线程 Tk 调用且不阻塞主线程）。"""
        if self.app.poll_service.is_connected:
            # 立即响应 UI 复位并非阻塞通知轮询线程退出，避免 stop_polling().join(2s) 阻塞主线程
            self.app.poll_service.request_stop_polling_nonblocking()
            if self._is_polling_loop:
                self._is_polling_loop = False
                self.btn_poll_loop.config(text="连续轮询", style="success.TButton", state="normal")
            self.lbl_conn_status.config(text="● 已断开", foreground="#dc3545")
            self.btn_connect.config(text="建立连接", style="primary.TButton", state="normal")

            def do_disconnect():
                try:
                    self.app.poll_service.disconnect()
                except Exception:
                    pass

            try:
                self._executor.submit(do_disconnect)
            except Exception:
                do_disconnect()
        else:
            cfg = self._get_connection_config()
            # 建连期间先将状态置为黄色“连接中...”并禁用按钮，防止重复点击触发并发连接
            self.lbl_conn_status.config(text="● 连接中...", foreground="#ffc107")
            self.btn_connect.config(state="disabled")

            def do_connect():
                """在后台工作线程执行阻塞式 TCP/RTU 连接握手，异常必兜底并入队回主线程更新状态。"""
                ok, msg = False, "未知连接异常"
                try:
                    ok, msg = self.app.poll_service.connect(cfg)
                except Exception as ex:
                    ok, msg = False, f"连接异常: {ex}"

                def on_done():
                    if self._destroyed or not (hasattr(self, "winfo_exists") and self.winfo_exists()):
                        return
                    self.btn_connect.config(state="normal")
                    if ok:
                        self.lbl_conn_status.config(text="● 已连接", foreground="#28a745")
                        self.btn_connect.config(text="断开连接", style="danger.TButton")
                    else:
                        self.lbl_conn_status.config(text="● 连接失败", foreground="#dc3545")
                        self.btn_connect.config(text="建立连接", style="primary.TButton")
                        messagebox.showerror("连接失败", msg)

                self._post_ui(on_done)

            try:
                self._executor.submit(do_connect)
            except Exception as ex:
                self.btn_connect.config(state="normal", text="建立连接", style="primary.TButton")
                self.lbl_conn_status.config(text="● 连接失败", foreground="#dc3545")
                messagebox.showerror("连接失败", str(ex))

    @staticmethod
    def _validate_poll_range(area_enum: AreaType, start_addr: int, count: int) -> Optional[str]:
        """校验主机读取起始地址与连续数量是否符合 Modbus 协议帧上限，非法返回错误原因。"""
        if not (0 <= start_addr <= 65535):
            return f"起始地址必须在 0~65535 之间: {start_addr}"
        max_count = 2000 if area_enum in (AreaType.COIL, AreaType.DISCRETE) else 125
        if not (1 <= count <= max_count):
            return f"读取数量超出协议单帧上限 (1~{max_count}): {count}"
        if start_addr + count > 65536:
            return f"读取地址范围越界 ({start_addr} + {count} > 65536)"
        return None

    def _poll_once(self) -> None:
        """异步执行单次寄存器轮询读取，彻底解放 UI 主线程，杜绝网络超时引起的界面假死。"""
        # 若上一次单次读取尚未返回，直接丢弃本次点击以防请求队列积压
        if self._is_poll_busy:
            return

        cfg = self._get_connection_config()
        try:
            start_addr = int(self.entry_start_addr.get().strip())
            count = int(self.entry_count.get().strip())
            area_enum = AreaType.normalize(self.combo_area.get())
            bo_enum = ByteOrder.normalize(self.combo_order.get())
        except Exception as e:
            messagebox.showerror("参数错误", f"输入格式有误: {e}")
            return
        range_err = self._validate_poll_range(area_enum, start_addr, count)
        if range_err:
            messagebox.showerror("参数错误", range_err)
            return

        task = PollTask(
            conn_config=cfg,
            area=area_enum,
            start_address=start_addr,
            count=count,
            byte_order=bo_enum,
        )

        self._is_poll_busy = True
        self._tx_count += 1
        self.lbl_stats.config(text=f"Tx: {self._tx_count} | Rx: {self._rx_count}")

        def do_read():
            """在后台线程调用 PollService 读取寄存器块，异常必兜底并经主线程安全队列回传结果。"""
            try:
                res: PollResult = self.app.poll_service.read_registers(task)
            except Exception as ex:
                res = PollResult(
                    success=False,
                    area=task.area,
                    start_address=task.start_address,
                    count=task.count,
                    error_msg=f"读取异常: {ex}",
                )

            def on_done():
                self._is_poll_busy = False
                if self._destroyed or not (hasattr(self, "winfo_exists") and self.winfo_exists()):
                    return
                if self.app.poll_service.is_connected:
                    self.lbl_conn_status.config(text="● 已连接", foreground="#28a745")
                    self.btn_connect.config(text="断开连接", style="danger.TButton", state="normal")
                if res.success:
                    self._rx_count += 1
                    self._update_result_tree(res)
                else:
                    self.app.logging_service.post("ERROR", cfg.unit_id, f"读取失败: {res.error_msg}")
                self.lbl_stats.config(text=f"Tx: {self._tx_count} | Rx: {self._rx_count}")

            self._post_ui(on_done)

        try:
            self._executor.submit(do_read)
        except Exception as ex:
            self._is_poll_busy = False
            self.app.logging_service.post("ERROR", cfg.unit_id, f"提交读取任务失败: {ex}")

    # [POLL-CONFIG] 连续轮询控制：调用 PollService.start_continuous_polling，支持用户自定义间隔
    def _toggle_poll_loop(self) -> None:
        """启动或停止后台连续定时轮询任务，支持 100~60000ms 自定义轮询周期。"""
        if self._is_polling_loop:
            self._is_polling_loop = False
            self.app.poll_service.request_stop_polling_nonblocking()
            self.btn_poll_loop.config(text="连续轮询", style="success.TButton", state="normal")

            def _do_stop_loop():
                try:
                    self.app.poll_service.stop_continuous_polling()
                except Exception:
                    pass

            try:
                self._executor.submit(_do_stop_loop)
            except Exception:
                pass
        else:
            cfg = self._get_connection_config()
            try:
                start_addr = int(self.entry_start_addr.get().strip())
                count = int(self.entry_count.get().strip())
                area_enum = AreaType.normalize(self.combo_area.get())
                bo_enum = ByteOrder.normalize(self.combo_order.get())
            except Exception as e:
                messagebox.showerror("参数错误", f"输入格式有误: {e}")
                return
            range_err = self._validate_poll_range(area_enum, start_addr, count)
            if range_err:
                messagebox.showerror("参数错误", range_err)
                return

            try:
                raw_interval = int(self.poll_interval_var.get())
            except Exception:
                raw_interval = 1000
            interval_ms = max(100, min(60000, raw_interval))
            self.poll_interval_var.set(interval_ms)

            task = PollTask(
                conn_config=cfg,
                area=area_enum,
                start_address=start_addr,
                count=count,
                byte_order=bo_enum,
                is_loop=True,
                interval_ms=interval_ms,
            )

            # 后台轮询回调只写入线程安全队列，由主线程定时泵消费，彻底实现零跨线程 Tk 调用
            def _on_success_from_worker(fresh_dict: Dict[int, int]):
                def _ui_update():
                    if self._destroyed or not (hasattr(self, "winfo_exists") and self.winfo_exists()):
                        return
                    self._tx_count += 1
                    self._rx_count += 1
                    self.lbl_stats.config(text=f"Tx: {self._tx_count} | Rx: {self._rx_count}")
                    self._update_from_regs(fresh_dict, start_addr, count)
                self._post_ui(_ui_update)

            def _on_error_from_worker(err_msg: str):
                def _ui_error():
                    if self._destroyed or not (hasattr(self, "winfo_exists") and self.winfo_exists()):
                        return
                    self._tx_count += 1
                    self.lbl_stats.config(text=f"Tx: {self._tx_count} | Rx: {self._rx_count}")
                    self.app.logging_service.post("ERROR", cfg.unit_id, f"轮询异常: {err_msg}")
                self._post_ui(_ui_error)

            # 将可能触发 2s TCP/RTU 建连的启动过程放入后台线程池，避免阻塞 Tkinter 主线程
            self.btn_poll_loop.config(state="disabled", text="启动中...")

            def _do_start_loop():
                ok, msg = False, "未知启动异常"
                try:
                    ok, msg = self.app.poll_service.start_continuous_polling(
                        task=task,
                        interval_ms=interval_ms,
                        on_success=_on_success_from_worker,
                        on_error=_on_error_from_worker,
                    )
                except Exception as ex:
                    ok, msg = False, f"启动轮询异常: {ex}"

                def _on_started():
                    if self._destroyed or not (hasattr(self, "winfo_exists") and self.winfo_exists()):
                        return
                    self.btn_poll_loop.config(state="normal")
                    if ok:
                        self._is_polling_loop = True
                        self.btn_poll_loop.config(text="停止轮询", style="danger.TButton")
                        self.lbl_conn_status.config(text="● 已连接", foreground="#28a745")
                        self.btn_connect.config(text="断开连接", style="danger.TButton")
                    else:
                        self._is_polling_loop = False
                        self.btn_poll_loop.config(text="连续轮询", style="success.TButton")
                        messagebox.showerror("启动失败", msg)

                self._post_ui(_on_started)

            try:
                self._executor.submit(_do_start_loop)
            except Exception as ex:
                self._is_polling_loop = False
                self.btn_poll_loop.config(state="normal", text="连续轮询", style="success.TButton")
                messagebox.showerror("启动失败", str(ex))

    # [POLL-CONFIG] 适配从局部有效寄存器字典渲染表格
    def _update_from_regs(self, regs_dict: Dict[int, int], start_addr: int, count: int) -> None:
        """从后台连续轮询返回的局部有效地址字典提取连续寄存器列表并更新表格。"""
        # 按 [start_addr, start_addr + count) 顺序提取本次轮询的新鲜值，不混入历史不同地址段的残留缓存
        regs_list = [regs_dict.get(start_addr + i, 0) for i in range(count)]
        self._render_registers_to_tree(regs_list, start_addr)

    def _update_result_tree(self, res: PollResult) -> None:
        """根据单次轮询结果对象 PollResult 刷新寄存器监视表格。"""
        self._render_registers_to_tree(res.raw_registers, res.start_address)

    def _render_registers_to_tree(self, regs: List[int], start: int) -> None:
        """将原始 16 位寄存器数组解算为 Hex、UINT16、INT16 及双字 FLOAT32（响应顶部变位下拉框）并渲染到 Treeview。"""
        from ..modbus_codec import decode_value

        # 让顶部“变位”下拉框真实生效：默认第 5 列保持 ABCD、第 6 列随用户选择的变位模式（CDAB/BADC/DCBA）切换
        selected_order = ByteOrder.normalize(self.combo_order.get()).value if hasattr(self, "combo_order") else "CDAB"
        second_order = "CDAB" if selected_order == "ABCD" else selected_order
        try:
            self.tree.heading("f32_cdab", text=f"FLOAT32 ({second_order})")
        except Exception:
            pass

        for itm in self.tree.get_children():
            self.tree.delete(itm)

        def _fmt_float32(pair_regs: List[int], order_str: str) -> str:
            try:
                val_f = decode_value(pair_regs, "FLOAT32", order_str)
                if isinstance(val_f, (int, float)):
                    return f"{val_f:.4f}"
                return str(val_f)
            except Exception:
                return "-"

        for i, val in enumerate(regs):
            addr = start + i
            hex_str = f"0x{val:04X}"
            u16 = val
            i16 = val - 65536 if val > 32767 else val

            f32_abcd = "-"
            f32_second = "-"
            if i + 1 < len(regs):
                pair = [regs[i], regs[i + 1]]
                f32_abcd = _fmt_float32(pair, "ABCD")
                f32_second = _fmt_float32(pair, second_order)

            self.tree.insert("", "end", values=(addr, hex_str, u16, i16, f32_abcd, f32_second, "OK (200)"))

