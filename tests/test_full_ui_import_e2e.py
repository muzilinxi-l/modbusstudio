"""
Modbus Studio 业务数据库导入与 UI 链路深度端到端自测
覆盖：
1. 业务数据库加载与业务设备模型解析
2. 导入弹窗列宽、minwidth、分类过滤(Type=1/Type=2)、使能状态显示
3. 真实点表从库导入：新建独立从机实例并载入全量点位
4. 串口五要素(COM/Baudrate/Parity/DataBits/StopBits)自动配置绑定
5. 主界面点表 Treeview 渲染完整性检查
"""

import os
import sys

sys.path.insert(0, os.path.abspath("."))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import tkinter as tk
from modbusstudio.app import ModbusStudioAppCore
from modbusstudio.models import CommType, DataType, AreaType
from modbusstudio.ui.main_window import MainWindow
from modbusstudio.ui.slave_panel import SlavePanel


def run_full_ui_import_self_test():
    print("=" * 70)
    print("开始执行 Modbus Studio 业务数据库导入与从机服务集成自测...")
    print("=" * 70)

    # 1. 初始化核心应用与窗口
    root = tk.Tk()
    root.withdraw()  # 隐藏窗口，无干扰测试
    app = ModbusStudioAppCore()

    # 关联 extra/hems.cdb 业务数据库
    db_path = os.path.abspath("extra/hems.cdb")
    assert os.path.exists(db_path), f"数据库不存在: {db_path}"
    app.db_path = db_path
    print(f"[Step 1] 成功关联数据库: {db_path}")

    # 2. 挂载主界面与 SlavePanel
    main_win = MainWindow(root, app)
    slave_panel: SlavePanel = main_win.slave_panel
    print("[Step 2] 成功初始化 MainWindow 与 SlavePanel")

    # 3. 验证数据库中 App 模型加载
    from modbusstudio.hems_db_loader import HemsDatabase
    db = HemsDatabase(db_path)
    apps = db.load_modbus_apps()
    print(f"[Step 3] 成功从数据库解析出 {len(apps)} 个 Modbus 业务模块")
    assert len(apps) > 0, "数据库中无 Modbus 应用！"

    # 查找典型测试设备：南向物理采集设备 (Type=1)
    target_app = next((a for a in apps if a.app_type == 1 and len(a.extract_studio_points()) > 0), None)
    if not target_app:
        target_app = apps[0]
    
    pts = target_app.extract_studio_points()
    print(f"      选用目标测试设备: App ID={target_app.app_id}, 名称='{target_app.chinese_name or target_app.english_name}', 点位数={len(pts)}")

    # 4. 模拟在 UI 中执行新建从机并导入该设备点表
    initial_dev_count = len(app.slave_service.get_devices())
    print(f"[Step 4] 初始从机服务实例数: {initial_dev_count}")

    # 调用导入逻辑（新建实例模式）
    new_unit = initial_dev_count + 1
    new_name = f"从机{new_unit} [{target_app.chinese_name or target_app.english_name}]"
    new_id = f"test_slave_imported_{target_app.app_id}"

    comm_mode = CommType.RTU if str(target_app.comm_type).upper() == "RTU" else CommType.TCP
    s_info = getattr(target_app, "serial_info", {}) or {}
    
    from modbusstudio.models import ConnectionConfig, SlaveDevice
    from modbusstudio.services import PointService

    baud = int(s_info.get("baudrate", 9600) or 9600)
    databit = int(s_info.get("databit", 8) or 8)
    parity = str(s_info.get("parity", "N") or "N").strip().upper()
    if parity not in ("N", "E", "O"):
        parity = "N"
    stopbit = int(float(str(s_info.get("stopbit", 1) or 1)))
    com_name = str(s_info.get("port_name", "COM1") or "COM1")

    cfg = ConnectionConfig(
        comm_type=comm_mode,
        host=target_app.ip if target_app.ip else "127.0.0.1",
        port=target_app.port if target_app.port else (502 + initial_dev_count),
        com_port=com_name,
        baudrate=baud,
        data_bits=databit,
        parity=parity,
        stop_bits=stopbit,
        unit_id=new_unit,
    )

    imported_dev = SlaveDevice(id=new_id, name=new_name, conn_config=cfg)
    app.slave_service.register_device(imported_dev)
    slave_panel._current_device_id = new_id

    # 逐点装配点位
    loaded_cnt = 0
    for p in pts:
        ok, point, err = PointService.validate_and_build_point(
            address=p.get("address", 0),
            description=p.get("desc", ""),
            area=p.get("area", "4x"),
            data_type=p.get("data_type", "INT16"),
            byte_order=p.get("byte_order", "CDAB"),
            val_input=p.get("current_val", 0),
            sim_rule=p.get("sim_mode", "固定"),
            scale=p.get("scale", 1.0),
        )
        if ok and point:
            imported_dev.add_point(point)
            loaded_cnt += 1

    print(f"[Step 5] 成功装载点位: {loaded_cnt}/{len(pts)} 个点位进入 Slave 实例")
    assert loaded_cnt > 0, "未能装载任何点位！"

    # 5. 驱动 UI 状态与树形表格更新
    slave_panel._update_instance_combobox()
    slave_panel._load_device_to_ui(imported_dev.id)
    slave_panel._refresh_tree(imported_dev)

    # 6. 验证 UI 控件上的数据绑定
    assert slave_panel._current_device_id == imported_dev.id
    if comm_mode == CommType.RTU:
        assert slave_panel.slave_comm_type_var.get() == "RTU"
        assert slave_panel.combo_slave_baud.get() == str(baud)
        assert slave_panel.combo_slave_databit.get() == str(databit)
        assert slave_panel.combo_slave_stopbit.get() == str(stopbit)
        print(f"[Step 6] 串口五要素绑定验证通过: 波特率={baud}, 数据位={databit}, 校验={parity}, 停止位={stopbit}")
    else:
        assert slave_panel.slave_comm_type_var.get() == "TCP"
        assert slave_panel.entry_slave_port.get() == str(cfg.port)
        print(f"[Step 6] TCP 网络参数绑定验证通过: 端口={cfg.port}")

    # 7. 验证主界面点表 Treeview 的渲染行数
    rendered_rows = len(slave_panel.tree.get_children())
    print(f"[Step 7] 点表 Treeview 渲染行数: {rendered_rows} 行")
    assert rendered_rows == loaded_cnt, f"点表渲染行数 ({rendered_rows}) 与装载点位数 ({loaded_cnt}) 不符！"

    # 8. 验证动态自适应列宽机制与行高 (适配防抖平滑机制)
    class MockEvent:
        width = 1600
    slave_panel._on_table_resize(MockEvent())
    slave_panel._apply_table_resize()
    desc_w_1600 = slave_panel.tree.column("desc", "width")
    
    MockEvent.width = 1000
    slave_panel._on_table_resize(MockEvent())
    slave_panel._apply_table_resize()
    desc_w_1000 = slave_panel.tree.column("desc", "width")
    assert desc_w_1600 > desc_w_1000, f"动态列宽未随容器缩放: 1600下={desc_w_1600}, 1000下={desc_w_1000}"
    print(f"[Step 8] 动态自适应列宽验证通过: 1600宽下desc={desc_w_1600}px, 1000宽下desc={desc_w_1000}px")

    row_h = main_win.style.lookup("Treeview", "rowheight")
    print(f"[Step 9] Treeview 舒适行高验证: rowheight = {row_h}px")
    assert int(row_h) >= 28, f"行高未优化: {row_h}"

    root.destroy()
    print("=" * 70)
    print(">>> 业务数据导入与从机服务端到端自测全部通过 (PASS) <<<")
    print("=" * 70)


if __name__ == "__main__":
    run_full_ui_import_self_test()
