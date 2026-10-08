"""HEMS 业务数据库 (extra/hems.cdb) 适配器.

专门对接 app 表中配置的 Modbus 业务模块：
- 提取南向设备 (South / Poll) 的轮询规则与变量点表
- 提取北向服务 (North / Slave) 的寄存器映射点表
- 转换为 Modbus Studio 可直接加载的点位模型
- 支持将点表与变位配置同步回 app 数据库
"""

import json
import logging
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

from modbus_codec import ByteOrderMode, ModbusDataType, parse_hems_type
from modbus_engine import AreaType

logger = logging.getLogger("HemsDbLoader")


def map_db_format_to_data_type(fmt_str: Optional[Any]) -> ModbusDataType:
    """将 app 数据库里的 Variable Transfer Format 映射为 ModbusDataType (完美支持图二/图三 44 种类型)."""
    if not fmt_str:
        return ModbusDataType.INT16
    parsed = parse_hems_type(fmt_str)
    if parsed:
        return parsed[0]
    s = str(fmt_str).lower().strip()
    if "unsigned short" in s or "uint16" in s:
        return ModbusDataType.UINT16
    elif "signed short" in s or "int16" in s:
        return ModbusDataType.INT16
    elif "unsigned int" in s or "uint32" in s or "dword" in s:
        return ModbusDataType.UINT32
    elif "signed int" in s or "int32" in s:
        return ModbusDataType.INT32
    elif "float" in s:
        return ModbusDataType.FLOAT32
    elif "double" in s:
        return ModbusDataType.DOUBLE64
    elif "bool" in s or "bit" in s:
        return ModbusDataType.BOOL
    elif "string" in s:
        return ModbusDataType.STRING
    return ModbusDataType.INT16


def map_db_format_to_byte_order(fmt_str: Optional[Any], default_mode: ByteOrderMode = ByteOrderMode.CDAB) -> ByteOrderMode:
    """从字段格式中提取变位模式 (如 Signed int 3 -> CDAB, Float 1 -> ABCD)."""
    if not fmt_str:
        return default_mode
    parsed = parse_hems_type(fmt_str)
    if parsed:
        return parsed[1]
    return default_mode


SERIAL_PORT_NAMES = {
    0: "Invalid (无效)",
    1: "RS-485(A1/B1)",
    2: "RS-485(A2/B2)",
    3: "RS-485(A3/B3)",
    4: "RS-485(A4/B4)",
    5: "RS-485(A5/B5)",
    6: "RS-485(A6/B6)",
    7: "RS-485(A7/B7)",
}

SERIAL_PARITY_NAMES = {
    0: "N",
    1: "N",  # None
    2: "O",  # Odd
    3: "E",  # Even
}


class HemsAppModel:
    """代表 app 表中的一个 Modbus 设备模型 (支持以太网 TCP 与 串行端口 RTU 485/232)."""

    def __init__(
        self,
        app_id: int,
        app_type: int,  # 1: South, 2: North, 3: Strategy
        english_name: str,
        chinese_name: str,
        model_name: str,
        controller_lib: str,
        enable: int,
        raw_more: Dict[str, Any],
        local_params: Optional[Dict[str, Any]] = None,
    ):
        self.app_id = app_id
        self.app_type = app_type
        self.english_name = english_name
        self.chinese_name = chinese_name
        self.model_name = model_name
        self.controller_lib = controller_lib
        self.enable = enable
        self.raw_more = raw_more
        self.local_params: Dict[str, Any] = local_params or {}

        # 解析轮询规则与点位
        self.pollings: List[Dict[str, Any]] = raw_more.get("Pollings", [])
        self.points: List[Dict[str, Any]] = (
            raw_more.get("Realtime Variables", [])
            or raw_more.get("Referred Variables", [])
        )
        self.orders: List[Dict[str, Any]] = raw_more.get("Local Orders", [])

        # 解析通信方式与网络/串口参数
        self.comm_type, self.serial_info = self._parse_comm_mode()
        self.ip, self.port = self._parse_network_address()

    def _parse_comm_mode(self) -> Tuple[str, Dict[str, Any]]:
        """从 Local Parameters 中解析通信类型与串口参数."""
        port_type = str(self.local_params.get("Port Type", "1")).strip()
        serial_port_idx = self.local_params.get("Serial Port")

        is_serial = (port_type == "2") or (serial_port_idx is not None and str(serial_port_idx) not in ("0", ""))

        if is_serial:
            try:
                idx = int(str(serial_port_idx))
            except Exception:
                idx = 1
            port_name = SERIAL_PORT_NAMES.get(idx, f"RS-485_{idx}")
            try:
                baud = int(str(self.local_params.get("Serial Baudrate", 9600) or 9600))
            except Exception:
                baud = 9600
            try:
                databit = int(str(self.local_params.get("Serial Databit", 8) or 8))
            except Exception:
                databit = 8
            parity_val = self.local_params.get("Serial Parity", 1)
            try:
                parity_code = SERIAL_PARITY_NAMES.get(int(str(parity_val)), "N")
            except Exception:
                parity_code = "N"
            try:
                stopbit = int(float(str(self.local_params.get("Serial Stopbit", 1) or 1)))
            except Exception:
                stopbit = 1

            return "RTU", {
                "port_name": port_name,
                "port_idx": idx,
                "baudrate": baud,
                "databit": databit,
                "parity": parity_code,
                "stopbit": stopbit,
            }

        return "TCP", {}

    def _parse_network_address(self) -> Tuple[str, int]:
        """解析当前应用配置的 IP 与端口号，保证不同设备可分配独立端口."""
        ip = "127.0.0.1"
        port = 502

        # 优先从 Local Parameters 中提取
        remote_ip = str(self.local_params.get("Ethernet Remote IPv4", "")).strip()
        server_ip = str(self.local_params.get("Server Address", "")).strip()
        if remote_ip:
            ip = remote_ip
        elif server_ip:
            ip = server_ip

        remote_port = str(self.local_params.get("Ethernet Remote Port", "")).strip()
        local_port = str(self.local_params.get("Ethernet Local Port", "")).strip()
        server_port = str(self.local_params.get("Server Port", "")).strip()

        target_port_str = remote_port or local_port or server_port
        if target_port_str and target_port_str.isdigit():
            port = int(target_port_str)
        else:
            # 如果端口未配置，为不同设备自动生成互不冲突的独立端口
            if self.app_type == 1:
                port = 10000 + (self.app_id % 1000)
            elif self.app_type == 2:
                port = 9000 + (self.app_id % 1000)

        return ip, port

    @property
    def is_south_master(self) -> bool:
        return self.app_type == 1

    @property
    def is_north_slave(self) -> bool:
        return self.app_type == 2

    @property
    def can_import_to_slave(self) -> bool:
        """业务规则：只有数据库 app 表中的 type=1 才能导入 slave 模块."""
        return self.app_type == 1

    @property
    def can_import_to_poll(self) -> bool:
        """业务规则：只有数据库 app 表中的 type=2 才能导入 poll 模块."""
        return self.app_type == 2

    @property
    def permission_tag(self) -> str:
        """导入权限描述标签."""
        if self.app_type == 1:
            return "✅ 仅允许导入 Slave 从机"
        elif self.app_type == 2:
            return "✅ 仅允许导入 Poll 主机"
        else:
            return f"❌ 不可导入 (Type={self.app_type})"

    @property
    def display_name(self) -> str:
        tag = "南向采集" if self.is_south_master else "北向从机" if self.is_north_slave else "策略模块"
        if self.comm_type == "RTU":
            comm_desc = f"🔌串口 [{self.serial_info['port_name']}] {self.serial_info['baudrate']},{self.serial_info['databit']}-{self.serial_info['parity']}-{self.serial_info['stopbit']}"
        else:
            comm_desc = f"🌐网口 [{self.ip}:{self.port}]"
        return f"[{tag}] ID {self.app_id:3d} : {self.english_name} ({self.chinese_name}) [{comm_desc}]"

    def extract_studio_points(self) -> List[Dict[str, Any]]:
        """将当前设备的业务点位转换为 Modbus Studio 点位列表."""
        result = []
        for p in self.points:
            addr_str = p.get("Register Start Address") or p.get("Register Address")
            if addr_str is None or str(addr_str).lower() == "none" or str(addr_str) == "":
                continue

            try:
                addr = int(addr_str)
            except ValueError:
                continue

            eng_name = p.get("English Name", f"Point_{addr}")
            chn_name = p.get("Chinese Name", eng_name)
            desc = chn_name if chn_name and chn_name != eng_name else eng_name

            fc_str = str(p.get("Function Code", "3"))
            area = AreaType.HOLDING_REGISTER
            if fc_str in ("1", "01"):
                area = AreaType.COIL
            elif fc_str in ("2", "02"):
                area = AreaType.DISCRETE_INPUT
            elif fc_str in ("4", "04"):
                area = AreaType.INPUT_REGISTER
            else:
                area = AreaType.HOLDING_REGISTER

            fmt_str = p.get("Variable Transfer Format")
            dtype = map_db_format_to_data_type(fmt_str)
            order = map_db_format_to_byte_order(fmt_str, default_mode=ByteOrderMode.CDAB)

            scale = float(p.get("Scale Factor", 1.0) or 1.0)

            result.append({
                "address": addr,
                "desc": desc,
                "area": area,
                "data_type": dtype,
                "byte_order": order,
                "scale": scale,
                "current_val": 0.0 if dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64) else 0,
                "sim_mode": "随机波动" if dtype in (ModbusDataType.FLOAT32, ModbusDataType.INT16, ModbusDataType.UINT16) else "固定",
            })
        return result


class HemsDatabase:
    """SQLite 数据库操作管理器."""

    def __init__(self, db_path: str = "extra/hems.cdb"):
        resolved = db_path
        if not os.path.exists(resolved):
            # 1. 尝试相对于 exe 所在目录
            candidate1 = os.path.join(os.path.dirname(sys.executable), db_path)
            # 2. 尝试相对于当前脚本目录
            candidate2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), db_path)
            if os.path.exists(candidate1):
                resolved = candidate1
            elif os.path.exists(candidate2):
                resolved = candidate2
        self.db_path = resolved

    def load_modbus_apps(self) -> List[HemsAppModel]:
        """从 app 表中读取所有 Modbus 业务模块."""
        if not os.path.exists(self.db_path):
            logger.warning(f"数据库文件不存在: {self.db_path}")
            return []

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT Id, Type, [English Name], [Chinese Name], [Controller Library], [Referred Model], [Local Parameters], More, enable FROM app;")
        rows = cursor.fetchall()
        apps = []

        for r in rows:
            more_raw = r["More"]
            more = {}
            if isinstance(more_raw, bytes):
                try:
                    more = json.loads(more_raw.decode("utf-8"))
                except Exception:
                    pass
            elif isinstance(more_raw, str) and more_raw.strip().startswith("{"):
                try:
                    more = json.loads(more_raw)
                except Exception:
                    pass

            # 解析 Local Parameters (包含网络配置、IP、端口、串口参数等)
            lp_raw = r["Local Parameters"]
            local_params = {}
            if isinstance(lp_raw, bytes):
                try:
                    lp_list = json.loads(lp_raw.decode("utf-8"))
                    if isinstance(lp_list, list):
                        local_params = {item.get("English Name"): item.get("Value") for item in lp_list if isinstance(item, dict)}
                except Exception:
                    pass
            elif isinstance(lp_raw, str) and lp_raw.strip().startswith("["):
                try:
                    lp_list = json.loads(lp_raw)
                    if isinstance(lp_list, list):
                        local_params = {item.get("English Name"): item.get("Value") for item in lp_list if isinstance(item, dict)}
                except Exception:
                    pass

            ctrl = str(r["Controller Library"])
            has_polls = len(more.get("Pollings", [])) > 0
            has_vars = len(more.get("Realtime Variables", [])) > 0 or len(more.get("Referred Variables", [])) > 0
            is_modbus = "Mb" in ctrl or has_polls or has_vars or r["Type"] in (1, 2)

            if is_modbus:
                app_obj = HemsAppModel(
                    app_id=r["Id"],
                    app_type=r["Type"],
                    english_name=r["English Name"] or "",
                    chinese_name=r["Chinese Name"] or "",
                    model_name=r["Referred Model"] or "",
                    controller_lib=ctrl,
                    enable=r["enable"],
                    raw_more=more,
                    local_params=local_params,
                )
                apps.append(app_obj)

        conn.close()
        return apps

    def save_app_points(self, app_id: int, points: List[Dict[str, Any]]) -> bool:
        """支持将软件中调整好的点表与变位信息更新写回 app 表的 More 字段中."""
        if not os.path.exists(self.db_path):
            return False

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT More FROM app WHERE Id = ?;", (app_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False

        more_raw = row[0]
        more = {}
        if isinstance(more_raw, bytes):
            try:
                more = json.loads(more_raw.decode("utf-8"))
            except Exception:
                pass
        elif isinstance(more_raw, str):
            try:
                more = json.loads(more_raw)
            except Exception:
                pass

        # 更新实时变量表
        var_key = "Realtime Variables" if "Realtime Variables" in more else "Referred Variables"
        db_vars = []
        for p in points:
            db_vars.append({
                "English Name": p.get("desc"),
                "Chinese Name": p.get("desc"),
                "Function Code": "3" if AreaType.HOLDING_REGISTER in p.get("area", "") else "4",
                "Register Start Address": str(p.get("address")),
                "Variable Transfer Format": p.get("data_type").value if hasattr(p.get("data_type"), "value") else str(p.get("data_type")),
                "Scale Factor": str(p.get("scale", 1)),
                "Byte Order": p.get("byte_order").value if hasattr(p.get("byte_order"), "value") else str(p.get("byte_order")),
            })
        more[var_key] = db_vars

        new_more_json = json.dumps(more, ensure_ascii=False)
        cursor.execute("UPDATE app SET More = ? WHERE Id = ?;", (new_more_json, app_id))
        conn.commit()
        conn.close()
        return True
