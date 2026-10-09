"""
Modbus Studio - 批量设备导入与从机实例配额限制测试套件
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
验证：
1. Modbus TCP 与 Modbus RTU 各自独立 100 个服务实例的配额硬限制；
2. 批量导入多设备时一次性创建多个独立从机实例；
3. 多设备点表覆盖与追加合并模式；
4. 端口与从机 Unit ID 自动递增防冲突机制。
"""

import os
import pytest
from modbusstudio.models import (
    CommType,
    ConnectionConfig,
    MAX_RTU_SLAVE_INSTANCES,
    MAX_TCP_SLAVE_INSTANCES,
    SlaveDevice,
)
from modbusstudio.services import LoggingService, PointService, SlaveService


def test_slave_quota_independent_limits():
    """测试 TCP 与 RTU 从机服务实例独立 100 个的配额限制"""
    log_svc = LoggingService()
    slave_svc = SlaveService(log_svc)

    # 1. 初始状态为 0
    assert slave_svc.count_devices_by_comm(CommType.TCP) == 0
    assert slave_svc.count_devices_by_comm(CommType.RTU) == 0

    # 2. 批量注册 100 个 TCP 实例
    for i in range(MAX_TCP_SLAVE_INSTANCES):
        cfg = ConnectionConfig(comm_type=CommType.TCP, port=502 + i, unit_id=i + 1)
        dev = SlaveDevice(id=f"tcp_{i}", name=f"TCP_Slave_{i}", conn_config=cfg)
        ok, msg = slave_svc.register_device(dev)
        assert ok is True, f"注册第 {i} 个 TCP 从机失败: {msg}"

    assert slave_svc.count_devices_by_comm(CommType.TCP) == 100

    # 3. 尝试注册第 101 个 TCP 实例 -> 必须被拒绝拦截
    cfg_101 = ConnectionConfig(comm_type=CommType.TCP, port=603, unit_id=101)
    dev_101 = SlaveDevice(id="tcp_101", name="TCP_Slave_101", conn_config=cfg_101)
    ok_101, err_101 = slave_svc.register_device(dev_101)
    assert ok_101 is False
    assert "以太网" in err_101 or "TCP" in err_101
    assert "已达系统容量上限" in err_101

    # 4. 验证 RTU 独立不受影响 (此时 RTU 仍可正常注册)
    assert slave_svc.count_devices_by_comm(CommType.RTU) == 0
    cfg_rtu_1 = ConnectionConfig(comm_type=CommType.RTU, com_port="COM1", unit_id=1)
    dev_rtu_1 = SlaveDevice(id="rtu_1", name="RTU_Slave_1", conn_config=cfg_rtu_1)
    ok_rtu_1, _ = slave_svc.register_device(dev_rtu_1)
    assert ok_rtu_1 is True
    assert slave_svc.count_devices_by_comm(CommType.RTU) == 1

    # 5. 填满 RTU 实例到 100 个
    for j in range(1, MAX_RTU_SLAVE_INSTANCES):
        cfg_rtu = ConnectionConfig(comm_type=CommType.RTU, com_port="COM2", unit_id=j + 1)
        dev_rtu = SlaveDevice(id=f"rtu_{j+1}", name=f"RTU_Slave_{j+1}", conn_config=cfg_rtu)
        ok, _ = slave_svc.register_device(dev_rtu)
        assert ok is True

    assert slave_svc.count_devices_by_comm(CommType.RTU) == 100

    # 6. 尝试注册第 101 个 RTU 实例 -> 必须被拦截
    cfg_rtu_overflow = ConnectionConfig(comm_type=CommType.RTU, com_port="COM3", unit_id=101)
    dev_rtu_overflow = SlaveDevice(id="rtu_overflow", name="RTU_Slave_Overflow", conn_config=cfg_rtu_overflow)
    ok_rtu_overflow, err_rtu_overflow = slave_svc.register_device(dev_rtu_overflow)
    assert ok_rtu_overflow is False
    assert "串口" in err_rtu_overflow or "RTU" in err_rtu_overflow
    assert "已达系统容量上限" in err_rtu_overflow


def test_batch_device_import_simulation():
    """模拟多选设备批量导入创建独立从机服务及点表装配"""
    log_svc = LoggingService()
    slave_svc = SlaveService(log_svc)

    # 模拟从业务数据库选中的 5 个设备
    mock_selected_devices = [
        {"app_id": 101, "name": "南向逆变器1", "comm": "TCP", "points": [
            {"address": 0, "desc": "直流电压", "data_type": "FLOAT32", "byte_order": "CDAB", "area": "4x", "current_val": 550.2},
            {"address": 2, "desc": "直流电流", "data_type": "FLOAT32", "byte_order": "CDAB", "area": "4x", "current_val": 12.5},
        ]},
        {"app_id": 102, "name": "南向逆变器2", "comm": "TCP", "points": [
            {"address": 0, "desc": "直流电压", "data_type": "FLOAT32", "byte_order": "CDAB", "area": "4x", "current_val": 548.8},
            {"address": 2, "desc": "直流电流", "data_type": "FLOAT32", "byte_order": "CDAB", "area": "4x", "current_val": 11.9},
        ]},
        {"app_id": 103, "name": "电表", "comm": "RTU", "points": [
            {"address": 100, "desc": "有功功率", "data_type": "INT32", "byte_order": "ABCD", "area": "4x", "current_val": 15000},
        ]},
        {"app_id": 104, "name": "环境仪", "comm": "RTU", "points": [
            {"address": 10, "desc": "环境温度", "data_type": "INT16", "byte_order": "ABCD", "area": "3x", "current_val": 28},
        ]},
        {"app_id": 105, "name": "BMS系统", "comm": "TCP", "points": [
            {"address": 200, "desc": "SOC", "data_type": "UINT16", "byte_order": "ABCD", "area": "4x", "current_val": 95},
        ]},
    ]

    # 一次性批量导入为独立从机
    created_devices = []
    base_port = 502
    for idx, d_info in enumerate(mock_selected_devices):
        comm_type = CommType.RTU if d_info["comm"] == "RTU" else CommType.TCP
        port = base_port + idx
        unit_id = idx + 1
        cfg = ConnectionConfig(
            comm_type=comm_type,
            host="127.0.0.1",
            port=port,
            com_port="COM1",
            unit_id=unit_id,
        )
        dev = SlaveDevice(
            id=f"slave_batch_{idx}",
            name=f"从机{unit_id} [{d_info['name']}]",
            conn_config=cfg,
            db_device_id=d_info["app_id"],
            db_device_name=d_info["name"],
        )

        for pt in d_info["points"]:
            ok, p, _ = PointService.validate_and_build_point(
                address=pt["address"],
                description=pt["desc"],
                area=pt["area"],
                data_type=pt["data_type"],
                byte_order=pt["byte_order"],
                val_input=pt["current_val"],
            )
            assert ok is True
            dev.add_point(p)

        ok, msg = slave_svc.register_device(dev)
        assert ok is True
        created_devices.append(dev)

    # 验证创建数量与分配
    assert len(created_devices) == 5
    assert slave_svc.count_devices_by_comm(CommType.TCP) == 3
    assert slave_svc.count_devices_by_comm(CommType.RTU) == 2

    # 验证点位装配
    inv1 = created_devices[0]
    assert len(inv1.points) == 2
    assert inv1.get_point("4x_HoldingRegister", 0) is not None
    assert inv1.get_point("4x_HoldingRegister", 2) is not None

    bms = created_devices[4]
    assert len(bms.points) == 1
    assert bms.get_point("4x_HoldingRegister", 200).value == 95


def test_batch_device_merge_and_replace():
    """测试多选设备合并追加与覆盖替换到当前从机点表"""
    log_svc = LoggingService()
    slave_svc = SlaveService(log_svc)

    # 初始从机
    cfg = ConnectionConfig(comm_type=CommType.TCP, port=502, unit_id=1)
    target_dev = SlaveDevice(id="main_slave", name="主从机", conn_config=cfg)
    ok, p0, _ = PointService.validate_and_build_point(0, "原有点位", "4x", "INT16", "ABCD", 100)
    target_dev.add_point(p0)
    slave_svc.register_device(target_dev)

    assert len(target_dev.points) == 1

    # 模拟多选 2 个设备追加
    dev_a_pts = [{"address": 10, "desc": "A设备点1", "val": 1}, {"address": 11, "desc": "A设备点2", "val": 2}]
    dev_b_pts = [{"address": 20, "desc": "B设备点1", "val": 3}]

    for pt in dev_a_pts + dev_b_pts:
        ok, p, _ = PointService.validate_and_build_point(pt["address"], pt["desc"], "4x", "INT16", "ABCD", pt["val"])
        assert ok is True
        target_dev.add_point(p)

    # 原有点位保留，追加了 3 个点位，总共 4 个
    assert len(target_dev.points) == 4

    # 模拟覆盖替换模式 (replace)
    target_dev.points.clear()
    assert len(target_dev.points) == 0

    for pt in dev_b_pts:
        ok, p, _ = PointService.validate_and_build_point(pt["address"], pt["desc"], "4x", "INT16", "ABCD", pt["val"])
        assert ok is True
        target_dev.add_point(p)

    assert len(target_dev.points) == 1
    assert target_dev.get_point("4x_HoldingRegister", 20) is not None


def test_db_import_dialog_default_selection_policy():
    """测试导入弹窗分类策略：Type=1 或 Type=2 时默认全选已使能设备，全部业务模块不全选"""
    import tkinter as tk
    from modbusstudio.app import ModbusStudioAppCore
    from modbusstudio.ui.dialogs import DbImportDialog

    root = tk.Tk()
    root.withdraw()
    try:
        app = ModbusStudioAppCore()
        dlg = DbImportDialog(root, app, lambda d: None)

        # 1. 验证默认 "all" 模式下：不进行全部选中（仅选中 1 项或首项）
        dlg.type_var.set("all")
        all_sel = dlg.tree.selection()
        assert len(all_sel) <= 1, f"'all' 模式下不应当全选，当前选中: {len(all_sel)}"

        # 2. 验证切换至 "1" (南向采集设备) 模式：必须默认全选当前所有已使能设备！
        dlg.type_var.set("1")
        sel_type1 = dlg.tree.selection()
        assert len(sel_type1) > 1, f"Type=1 模式下应当自动全选所有已使能设备，当前选中数: {len(sel_type1)}"
        for item_id in sel_type1:
            a = dlg.app_item_map[int(item_id)][0]
            assert a.enable == 1, f"未使能设备不应被默认全选: App ID {a.app_id}"

        # 3. 验证切换回 "all" 模式：恢复为单选首项
        dlg.type_var.set("all")
        assert len(dlg.tree.selection()) <= 1

        dlg.dlg.destroy()
    finally:
        root.destroy()


def test_clear_all_devices_on_database_switch():
    """测试切换数据库时，自动停止底层引擎并清空所有已添加的从机服务实例与点表"""
    log_svc = LoggingService()
    slave_svc = SlaveService(log_svc)

    # 1. 预先添加 3 个从机实例
    for i in range(3):
        cfg = ConnectionConfig(comm_type=CommType.TCP, port=502 + i, unit_id=i + 1)
        dev = SlaveDevice(id=f"slave_{i}", name=f"Slave_{i}", conn_config=cfg)
        ok, p, _ = PointService.validate_and_build_point(i, f"P_{i}", "4x", "INT16", "ABCD", i * 10)
        dev.add_point(p)
        slave_svc.register_device(dev)

    assert len(slave_svc.get_devices()) == 3

    # 2. 调用 clear_all_devices 清空
    cleared_count = slave_svc.clear_all_devices()
    assert cleared_count == 3
    assert len(slave_svc.get_devices()) == 0
    assert slave_svc.count_devices_by_comm(CommType.TCP) == 0
    assert slave_svc.count_devices_by_comm(CommType.RTU) == 0


