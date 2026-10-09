"""
Modbus Studio - 核心应用装配器 (Application Coordinator)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
创建并连接领域模型、业务服务与界面视图，协调生命周期与系统级退出清理。
"""

from __future__ import annotations
import os
import sys
import tkinter as tk
from typing import Optional

from .models import (
    AreaType,
    ByteOrder,
    CommType,
    ConnectionConfig,
    DataType,
    ModbusPoint,
    SlaveDevice,
)
from .services import (
    LoggingService,
    PointService,
    PollService,
    SlaveService,
)
from .ui import MainWindow


class ModbusStudioAppCore:
    """应用程序核心协调器"""

    def __init__(self):
        # 1. 业务与通信基础设施初始化
        self.logging_service = LoggingService(log_dir="logs")
        self.slave_service = SlaveService(self.logging_service)
        self.poll_service = PollService(self.logging_service)

        # 2. 默认数据库路径探测
        self.db_path = self._probe_default_db()

        # 3. 装配默认从机服务实例
        self._init_default_slaves()

    def _probe_default_db(self) -> Optional[str]:
        candidates = [
            os.path.abspath("extra/hems.cdb"),
            os.path.abspath("extra/hems.sqlite"),
            os.path.abspath("extra/hems.db"),
        ]
        for path in candidates:
            if os.path.exists(path):
                self.logging_service.post("SYS", 0, f"发现并预载业务数据库: {path}")
                return path
        return None

    def _init_default_slaves(self) -> None:
        """初始化首个默认从机 (南向 PCS 模拟器)"""
        cfg = ConnectionConfig(
            comm_type=CommType.TCP,
            host="127.0.0.1",
            port=502,
            com_port="COM1",
            baudrate=9600,
            unit_id=1,
        )
        slave_1 = SlaveDevice(
            id="slave_1",
            name="[南向]英博PCS_1",
            conn_config=cfg,
        )

        # 预设几个常规点位
        default_points = [
            (1, "运行模式 (Operation Mode)", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 1),
            (2, "恒压充电电压给定", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 750),
            (3, "恒流充电电流给定", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 100),
            (4, "有功功率设定 (Active Power)", AreaType.HOLDING, DataType.INT16, ByteOrder.ABCD, 50),
            (5, "无功功率设定 (Reactive Power)", AreaType.HOLDING, DataType.INT16, ByteOrder.ABCD, 0),
            (6, "并网/离网模式设置", AreaType.HOLDING, DataType.UINT16, ByteOrder.ABCD, 1),
            (10, "直流侧总电压 (FLOAT32)", AreaType.HOLDING, DataType.FLOAT32, ByteOrder.CDAB, 752.4),
            (12, "直流侧总电流 (FLOAT32)", AreaType.HOLDING, DataType.FLOAT32, ByteOrder.CDAB, 98.6),
            (20, "电网A相电压 (FLOAT32)", AreaType.INPUT, DataType.FLOAT32, ByteOrder.ABCD, 220.5),
            (22, "电网B相电压 (FLOAT32)", AreaType.INPUT, DataType.FLOAT32, ByteOrder.ABCD, 221.1),
            (24, "电网C相电压 (FLOAT32)", AreaType.INPUT, DataType.FLOAT32, ByteOrder.ABCD, 219.8),
        ]

        for addr, desc, area, dt, bo, val in default_points:
            ok, pt, _ = PointService.validate_and_build_point(
                address=addr,
                description=desc,
                area=area,
                data_type=dt,
                byte_order=bo,
                val_input=val,
                sim_rule="固定",
            )
            if ok and pt:
                slave_1.add_point(pt)

        self.slave_service.register_device(slave_1)

    def shutdown(self) -> None:
        """优雅关闭：释放所有从机监听与客户端连接"""
        self.logging_service.post("SYS", 0, "正在关闭系统服务并释放网络/串口端口...")
        for dev in self.slave_service.get_devices():
            if dev.is_running:
                self.slave_service.stop_slave(dev.id)
        if self.poll_service.is_connected:
            self.poll_service.disconnect()


def main():
    root = tk.Tk()
    app = ModbusStudioAppCore()
    main_window = MainWindow(root, app)

    def on_closing():
        try:
            app.shutdown()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
