"""
    HEMS 业务数据库 (extra/hems.cdb) 适配器模块，负责解析一体机 app 表中的南北向 Modbus 设备与点表。
    关键导入：json/os/sqlite3/sys/logging（SQLite 读写与 JSON 字段解析）、ByteOrderMode/ModbusDataType/parse_hems_type/AreaType（数据类型映射与存储区枚举）。
对外暴露：map_db_format_to_data_type、map_db_format_to_byte_order、HemsAppModel、HemsDatabase。
"""

import json
import logging
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

# 为什么使用 try/except 双路径导入：
# 1. 支持以标准包结构 `python -m modbusstudio.app` 运行时通过相对路径导入编解码与引擎枚举；
# 2. 支持在独立脚本或诊断工具直接加载 `hems_db_loader.py` 时退回同级模块导入；
# 3. 避免不同启动入口下因包上下文缺失而触发 ImportError。
try:
    from .modbus_codec import ByteOrderMode, ModbusDataType, parse_hems_type
    from .modbus_engine import AreaType
except ImportError:
    from modbus_codec import ByteOrderMode, ModbusDataType, parse_hems_type
    from modbus_engine import AreaType


# 模块级日志器，统一归集数据库加载与点表回写过程中的告警信息
logger = logging.getLogger("HemsDbLoader")


# =====================================================================
# 1. 数据库字段格式映射辅助函数
# 设计背景：
# 一体机现场 SQLite 数据库 `app.More` JSON 中使用字符串描述寄存器传输格式
# （如 "Unsigned short 1"、"Signed int 3"、"Float 1" 等 44 种组合），
# 需要将其无损转换为本工程的强类型 `ModbusDataType` 与 `ByteOrderMode`。
# =====================================================================
def map_db_format_to_data_type(fmt_str: Optional[Any]) -> ModbusDataType:
    """将 app 数据库里的 Variable Transfer Format 映射为 ModbusDataType（支持全部 44 种格式）。"""
    # 参数：fmt_str（数据库中的格式描述字符串或空值）。返回：匹配到的 ModbusDataType 枚举（默认 INT16）。
    # 步骤：1. 空值默认返回 INT16；2. 优先调用 parse_hems_type 查精确映射表；3. 未命中则按关键字模糊匹配兜底。
    # 为什么空值回退为 INT16：Modbus 工业现场最通用的单寄存器默认类型为有符号 16 位整型。
    if not fmt_str:
        return ModbusDataType.INT16
    # 优先委托 modbus_codec.parse_hems_type 精确匹配 44 种标准 HEMS 复合格式字符串
    parsed = parse_hems_type(fmt_str)
    if parsed:
        return parsed[0]
    # 若遇到手工编辑或旧版固件的非标格式字符串，通过小写子串启发式兜底识别
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
    """从数据库字段格式字符串中提取 32/64 位变位模式（如 Signed int 3 -> CDAB, Float 1 -> ABCD）。"""
    # 参数：fmt_str（格式字符串）、default_mode（未指明变位时的默认模式，默认为一体机常用的 CDAB）。
    # 返回：ByteOrderMode 枚举值。步骤：1. 空值返回默认变位；2. 调用 parse_hems_type 提取尾号对应的字节序。
    if not fmt_str:
        return default_mode
    parsed = parse_hems_type(fmt_str)
    if parsed:
        return parsed[1]
    return default_mode


# 一体机硬件串口编号 (0~7) 到物理丝印端子名称 (RS-485 A1/B1 ~ A7/B7) 的映射字典：
# 为什么保留该硬件丝印映射：方便现场工程师在导入点表时直接看到设备接在一体机的哪一组 485 端子上。
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

# 数据库串口校验位枚举值 (0/1/2/3) 到 pyserial 标准单字符校验码 (N/O/E) 的映射表
SERIAL_PARITY_NAMES = {
    0: "N",
    1: "N",  # None（无校验）
    2: "O",  # Odd（奇校验）
    3: "E",  # Even（偶校验）
}


# =====================================================================
# 2. HEMS 业务设备模型 (HemsAppModel)
# 设计背景：
# 封装 SQLite `app` 表中的单行记录，负责从 `Local Parameters` 与 `More` JSON 中
# 自动提炼通信模式（TCP/RTU）、IP 端口、串口五要素、从机站号及标准化点表。
# =====================================================================
class HemsAppModel:
    """代表 app 表中的一个 Modbus 业务设备模型（支持以太网 TCP 与串行端口 RTU 485/232）。"""

    def __init__(
        self,
        app_id: int,
        app_type: int,  # 1: 南向采集设备, 2: 北向转发从机, 3: 策略模块
        english_name: str,
        chinese_name: str,
        model_name: str,
        controller_lib: str,
        enable: int,
        raw_more: Dict[str, Any],
        local_params: Optional[Dict[str, Any]] = None,
    ):
        """初始化 HEMS 设备模型并自动解析轮询规则、变量点表与物理通信连接参数。"""
        # 保存数据库基础元数据列（Id、Type、中英文名、模板名、驱动动态库名、使能标志）
        self.app_id = app_id
        self.app_type = app_type
        self.english_name = english_name
        self.chinese_name = chinese_name
        self.model_name = model_name
        self.controller_lib = controller_lib
        self.enable = enable
        self.raw_more = raw_more
        self.local_params: Dict[str, Any] = local_params or {}

        # 为什么 points 同时兼容 Realtime Variables 与 Referred Variables：
        # 1. 南向物理采集设备 (Type=1) 的点表存放在 `More["Realtime Variables"]` 中；
        # 2. 北向对外转发从机 (Type=2) 的点表存放在 `More["Referred Variables"]` 中；
        # 3. 统一收敛到 `self.points` 属性后，上层提取点表时无需区分南北向 JSON 结构差异。
        self.pollings: List[Dict[str, Any]] = raw_more.get("Pollings", [])
        self.points: List[Dict[str, Any]] = (
            raw_more.get("Realtime Variables", [])
            or raw_more.get("Referred Variables", [])
        )
        self.orders: List[Dict[str, Any]] = raw_more.get("Local Orders", [])

        # 自动解析通信方式（TCP/RTU）、网络地址（IP/Port）与从机站号（Unit ID）
        self.comm_type, self.serial_info = self._parse_comm_mode()
        self.ip, self.port = self._parse_network_address()
        self.slave_id = self._parse_slave_id()

    def _parse_slave_id(self) -> Optional[int]:
        """从 Pollings 或 Local Parameters 中提取设备配置的真实从机站地址（Unit ID / 从机站号）。"""
        # 为什么按“先 Pollings 后 Local Parameters”的优先级提取从机站号：
        # 1. 南向设备在 `More.Pollings` 数组中直接记录了实际发送 Modbus 报文时的从机站号；
        # 2. 部分北向从机或仅配了全局参数的设备则将站号放在 `Local Parameters` 中；
        # 3. 严格校验 `1 <= val <= 247` 符合 Modbus 工业规范单播站地址范围，过滤 0 或非法值。
        for p in self.pollings:
            s_id = p.get("Slave Id") or p.get("Slave ID") or p.get("Device Address") or p.get("Unit ID")
            if s_id is not None and str(s_id).strip().isdigit():
                val = int(str(s_id).strip())
                if 1 <= val <= 247:
                    return val

        # 2. 若 Pollings 未命中，则遍历 Local Parameters 中所有可能的站地址键名别名
        for k in ("Slave Id", "Slave ID", "Device Address", "Station ID", "Unit ID", "从机地址", "站地址"):
            val_s = str(self.local_params.get(k, "")).strip()
            if val_s.isdigit():
                val = int(val_s)
                if 1 <= val <= 247:
                    return val

        return None

    def _parse_comm_mode(self) -> Tuple[str, Dict[str, Any]]:
        """从 Local Parameters 中解析通信类型（TCP 或 RTU）及完整串口五要素参数。"""
        # 为什么同时检查 Port Type == "2" 与 Serial Port 非零：
        # 不同版本的一体机控制器库有的通过 `Port Type=2` 声明串口，有的直接配置 `Serial Port=1~7`。
        port_type = str(self.local_params.get("Port Type", "1")).strip()
        serial_port_idx = self.local_params.get("Serial Port")

        is_serial = (port_type == "2") or (serial_port_idx is not None and str(serial_port_idx) not in ("0", ""))

        # 当判定为串口通信时，逐项提取端子号、波特率、数据位、校验位与停止位，任何单项异常均回退到工业默认值 (9600, 8-N-1)
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
        """解析当前应用配置的 IP 与端口号，若未配置则按应用 ID 派发互不冲突的独立端口。"""
        ip = "127.0.0.1"
        port = 502

        # 优先从 Local Parameters 中提取远端 IP 或服务端监听 IP
        remote_ip = str(self.local_params.get("Ethernet Remote IPv4", "")).strip()
        server_ip = str(self.local_params.get("Server Address", "")).strip()
        if remote_ip:
            ip = remote_ip
        elif server_ip:
            ip = server_ip

        # 依次尝试读取远端端口、本地端口与服务端口字段
        remote_port = str(self.local_params.get("Ethernet Remote Port", "")).strip()
        local_port = str(self.local_params.get("Ethernet Local Port", "")).strip()
        server_port = str(self.local_params.get("Server Port", "")).strip()

        target_port_str = remote_port or local_port or server_port
        if target_port_str and target_port_str.isdigit():
            port = int(target_port_str)
        else:
            # 为什么在未配置端口时按 `10000 + app_id` / `9000 + app_id` 自动生成端口：
            # 串口设备或未填端口的模板在本地转为 TCP 模拟调试时，自动分配独立高位端口可避免全部挤在 502 端口引发冲突。
            if self.app_type == 1:
                port = 10000 + (self.app_id % 1000)
            elif self.app_type == 2:
                port = 9000 + (self.app_id % 1000)

        return ip, port

    @property
    def is_south_master(self) -> bool:
        """判断当前应用是否为南向采集设备（数据库 app.Type == 1）。"""
        # Type=1 代表一体机向南轮询的物理设备（如 BMS、PCS、电表），在本软件中可作为从机被模拟
        return self.app_type == 1

    @property
    def is_north_slave(self) -> bool:
        """判断当前应用是否为北向对外转发从机服务（数据库 app.Type == 2）。"""
        # Type=2 代表一体机向北暴露的汇总转发从机服务（如 [North]30）
        return self.app_type == 2

    @property
    def can_import_to_slave(self) -> bool:
        """业务规则：判断该应用是否允许作为南向物理从机导入到从机模拟模块（app.Type == 1）。"""
        return self.app_type == 1

    @property
    def can_import_to_poll(self) -> bool:
        """业务规则：判断该应用是否允许导入到主机轮询模块（app.Type == 2）。"""
        return self.app_type == 2

    @property
    def permission_tag(self) -> str:
        """生成界面展示用的导入权限说明标签。"""
        # 根据 app_type 给出直观的勾选提示文本
        if self.app_type == 1:
            return "✅ 仅允许导入 Slave 从机"
        elif self.app_type == 2:
            return "✅ 仅允许导入 Poll 主机"
        else:
            return f"❌ 不可导入 (Type={self.app_type})"

    @property
    def display_name(self) -> str:
        """生成包含业务方向、应用 ID、中英文名称及通信连接摘要的单行展示名称。"""
        # 组装角色标签与串口/网口参数描述，方便用户在下拉框或列表中一眼识别设备物理链路
        tag = "南向采集" if self.is_south_master else "北向从机" if self.is_north_slave else "策略模块"
        if self.comm_type == "RTU":
            comm_desc = f"🔌串口 [{self.serial_info['port_name']}] {self.serial_info['baudrate']},{self.serial_info['databit']}-{self.serial_info['parity']}-{self.serial_info['stopbit']}"
        else:
            comm_desc = f"🌐网口 [{self.ip}:{self.port}]"
        return f"[{tag}] ID {self.app_id:3d} : {self.english_name} ({self.chinese_name}) [{comm_desc}]"

    @staticmethod
    def _normalize_function_code(raw_fc: Any) -> Optional[int]:
        """将数据库中各种形态的 Function Code（如 "03"/"3"/"0x03"/"FC03"/3）统一归一化为整数功能码。"""
        if raw_fc is None:
            return None
        if isinstance(raw_fc, int) and not isinstance(raw_fc, bool):
            return raw_fc
        s = str(raw_fc).strip().upper()
        if not s or s in ("NONE", "NULL"):
            return None
        if s.startswith("FC"):
            s = s[2:].strip(" _:-")
        try:
            if s.startswith("0X"):
                return int(s, 16)
            return int(s, 10)
        except ValueError:
            import re
            m = re.search(r"0X[0-9A-F]+|\d+", s)
            if m:
                tok = m.group(0)
                return int(tok, 16) if tok.startswith("0X") else int(tok, 10)
            return None

    def extract_studio_points(self) -> List[Dict[str, Any]]:
        """将当前设备的原始 JSON 业务点位列表清洗转换为 Modbus Studio 标准点位字典列表。"""
        # 步骤拆解：
        # 1. 遍历 self.points，过滤无有效寄存器地址的虚点；
        # 2. 提取中英文描述、数据类型、变位模式与缩放系数；
        # 3. 结合功能码 (Function Code) 与数据类型智能推导四大存储区 (0x/1x/3x/4x)；
        # 4. 提取初始值并设置默认动态模拟规则。
        result = []
        for p in self.points:
            addr_str = p.get("Register Start Address") or p.get("Register Address")
            # 跳过未绑定 Modbus 物理地址的内部点位（地址为空或 "None"）
            if addr_str is None or str(addr_str).lower() == "none" or str(addr_str) == "":
                continue

            try:
                addr = int(addr_str)
            except ValueError:
                continue

            eng_name = p.get("English Name", f"Point_{addr}")
            chn_name = p.get("Chinese Name", eng_name)
            desc = chn_name if chn_name and chn_name != eng_name else eng_name

            fmt_str = p.get("Variable Transfer Format")
            dtype = map_db_format_to_data_type(fmt_str)
            order = map_db_format_to_byte_order(fmt_str, default_mode=ByteOrderMode.CDAB)
            scale = float(p.get("Scale Factor", 1.0) or 1.0)

            # 为什么先把 Function Code 统一归一化为整数且在存在时严格尊重库值：
            # 1. 现场数据库中 Function Code 可能表现为 "03"、"3"、3、"0x03"、"FC03" 等多种形态；
            # 2. 当点位显式配置了 Function Code（或可从所属 Pollings 规则继承功能码）时，必须严格尊重库值：
            #    - FC03（及 FC06/16 写保持寄存器）：数值点一律落 4x 保持寄存器（仅显式 BOOL 格式落 0x 线圈），绝不因描述含 "status"/"状态" 等关键词被误改到 1x 离散输入或 3x 输入寄存器；
            #    - FC04（输入寄存器）：数值点一律落 3x 输入寄存器（仅显式 BOOL 格式落 1x 离散输入）；
            # 3. 仅当点位与 Pollings 均未提供有效 Function Code 时，才启用启发式兜底逻辑。
            fc_num = self._normalize_function_code(p.get("Function Code"))
            if fc_num is None and self.pollings:
                for poll in self.pollings:
                    if not isinstance(poll, dict):
                        continue
                    p_fc = self._normalize_function_code(poll.get("Function Code"))
                    if p_fc is None:
                        continue
                    try:
                        p_start = int(poll.get("Register Start Address", 0) or 0)
                        p_cnt = int(poll.get("Register Number", 0) or 0)
                        if p_cnt > 0 and p_start <= addr < p_start + p_cnt:
                            fc_num = p_fc
                            break
                    except Exception:
                        pass
                if fc_num is None and len(self.pollings) == 1 and isinstance(self.pollings[0], dict):
                    fc_num = self._normalize_function_code(self.pollings[0].get("Function Code"))

            is_explicit_numeric = bool(fmt_str and dtype != ModbusDataType.BOOL)
            # 预判点位名称是否含状态/告警关键词，仅在点位与 Pollings 均未提供有效 Function Code 时作为兜底启发式依据
            is_alarm_or_status = any(k in desc.lower() or k in eng_name.lower() for k in ("alarm", "status", "fault", "protect", "warn", "告警", "保护", "故障", "状态"))

            # FC01/FC05/FC15 对应 0x 线圈区（可读可写单比特布尔量）
            if fc_num in (1, 5, 15):
                area = AreaType.COIL
                dtype = ModbusDataType.BOOL
            # FC02 对应 1x 离散量输入区（只读单比特布尔量）
            elif fc_num == 2:
                area = AreaType.DISCRETE_INPUT
                dtype = ModbusDataType.BOOL
            elif fc_num == 4:
                # FC04 是只读输入寄存器区（3x），显式配置时数值点必须落 3x，不因描述含 status/告警 关键词被改到 1x 离散输入
                if dtype == ModbusDataType.BOOL:
                    area = AreaType.DISCRETE_INPUT
                else:
                    area = AreaType.INPUT_REGISTER
            elif fc_num in (3, 6, 16):
                # FC03/FC06/FC16 对应 4x 保持寄存器区，显式配置时数值点保持 4x、显式 BOOL 落 0x 线圈，绝不因描述关键词改到 1x/3x
                if dtype == ModbusDataType.BOOL:
                    area = AreaType.COIL
                else:
                    area = AreaType.HOLDING_REGISTER
            else:
                # 仅当点位与 Pollings 均未提供有效 Function Code 时才走名称启发式兜底；明确数值型落 4x，布尔/告警型落 1x 离散输入
                if is_explicit_numeric:
                    area = AreaType.HOLDING_REGISTER
                elif dtype == ModbusDataType.BOOL or is_alarm_or_status:
                    area = AreaType.DISCRETE_INPUT
                    dtype = ModbusDataType.BOOL
                else:
                    area = AreaType.HOLDING_REGISTER

            # 按优先级依次探测数据库中可能携带的初始值/默认值/实时值字段
            db_val = None
            for key in ("Value", "Default Value", "Initial Value", "Realtime Value", "Current Value", "Raw Value"):
                if p.get(key) is not None and str(p.get(key)).strip() != "":
                    db_val = p.get(key)
                    break

            # 将字符串初始值安全转换为与 dtype 匹配的 Python 标量（int 或 float）
            if db_val is not None:
                try:
                    if dtype == ModbusDataType.BOOL:
                        current_val = 1 if str(db_val).strip().lower() in ("1", "true", "yes", "on") else 0
                    elif dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64):
                        current_val = float(db_val)
                    else:
                        current_val = int(float(str(db_val).strip()))
                except Exception:
                    current_val = 0.0 if dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64) else 0
            else:
                current_val = 0.0 if dtype in (ModbusDataType.FLOAT32, ModbusDataType.DOUBLE64) else 0

            # 组装标准点位字典：数值型默认开启“随机波动”以便导入后启动从机即可观察实时动态波形
            result.append({
                "address": addr,
                "desc": desc,
                "area": area,
                "data_type": dtype,
                "byte_order": order,
                "scale": scale,
                "current_val": current_val,
                "sim_mode": "随机波动" if dtype in (ModbusDataType.FLOAT32, ModbusDataType.INT16, ModbusDataType.UINT16) else "固定",
            })
        return result


# =====================================================================
# 3. SQLite 数据库操作管理器 (HemsDatabase)
# 设计背景：
# 负责定位磁盘上的 `.cdb` / `.db` 文件，读取 `app` 表中的全部 Modbus 设备记录，
# 并支持将软件内修改后的点表与变位配置反向写回 `app.More` JSON 字段。
# =====================================================================
class HemsDatabase:
    """一体机 HEMS SQLite 数据库读写管理器。"""

    def __init__(self, db_path: str = "extra/hems.cdb"):
        """初始化数据库管理器，按工作目录、可执行文件目录与脚本目录三级回退解析真实文件路径。"""
        # 为什么设计三级路径回退探测：
        # 兼顾源码直跑（相对当前工作目录）、PyInstaller 打包 exe 运行（相对 sys.executable）以及单元测试调用。
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
        """从 SQLite app 表中读取并解析所有 Modbus 业务设备模块。"""
        # 步骤拆解：
        # 1. 检查数据库文件是否存在；
        # 2. 连接 SQLite 并查询 app 表全部核心列；
        # 3. 兼容解析 bytes 或 str 格式的 More 与 Local Parameters JSON 字段；
        # 4. 筛选与 Modbus 相关的记录并封装为 HemsAppModel 列表返回。
        if not os.path.exists(self.db_path):
            logger.warning(f"数据库文件不存在: {self.db_path}")
            return []

        # 设置 5.0 秒锁等待超时，防止现场数据库被其他工具短暂读取时立即抛出 OperationalError
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM app;")
        rows = cursor.fetchall()
        apps = []

        for r in rows:
            row_keys = set(r.keys())
            # 为什么对 More 字段同时处理 bytes 与 str 两种类型：
            # 不同版本的 SQLite 驱动或现场 `.cdb` 导出工具可能将 BLOB/TEXT 列存储为 UTF-8 字节流或字符串。
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

            # 为什么从顶层 Realtime Variables / Referred Variables 列回填点位 Chinese Name：
            # 现场 hems.cdb 的 app.More 内部仅保存寄存器地址、功能码与 English Name，
            # 而点位的中文业务名称存放在同行的 Realtime Variables / Referred Variables 顶层列中。
            for var_col in ("Realtime Variables", "Referred Variables"):
                if var_col in row_keys and r[var_col] and isinstance(more.get(var_col), list):
                    try:
                        col_raw = r[var_col]
                        col_list = json.loads(col_raw.decode("utf-8") if isinstance(col_raw, bytes) else col_raw)
                        if isinstance(col_list, list):
                            chn_lookup = {
                                str(item.get("English Name", "")).strip(): str(item.get("Chinese Name", "")).strip()
                                for item in col_list
                                if isinstance(item, dict) and item.get("English Name") and item.get("Chinese Name")
                            }
                            for pt in more[var_col]:
                                if isinstance(pt, dict) and not pt.get("Chinese Name"):
                                    eng_k = str(pt.get("English Name", "")).strip()
                                    if eng_k in chn_lookup and chn_lookup[eng_k]:
                                        pt["Chinese Name"] = chn_lookup[eng_k]
                    except Exception:
                        pass

            # 解析 Local Parameters 数组（将 [{"English Name": k, "Value": v}, ...] 扁平化为 {k: v} 字典）
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

            # 判定该行是否为 Modbus 相关业务模块（驱动名含 Mb、或含轮询/点表配置、或 Type 为 1/2）
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
        """将软件中调整好的点表与变位信息反向序列化更新回 SQLite app 表的 More 字段。"""
        # 参数：app_id（目标应用 ID）、points（点位字典列表）。返回：是否成功写回数据库。
        # 副作用：修改 SQLite 文件中对应 Id 行的 More 字段并提交事务。
        if not os.path.exists(self.db_path):
            return False

        conn = sqlite3.connect(self.db_path, timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("SELECT More FROM app WHERE Id = ?;", (app_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False

        # 先读取并反序列化原有的 More JSON，确保只更新变量数组而不破坏 Pollings 等其他兄弟字段
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

        # 更新实时变量表：若原 JSON 使用 Realtime Variables 则保持该键名，否则写入 Referred Variables
        var_key = "Realtime Variables" if "Realtime Variables" in more else "Referred Variables"
        db_vars = []
        for p in points:
            area_str = str(p.get("area", "")).upper()
            if "COIL" in area_str or "线圈" in str(p.get("area", "")) or "0X" in area_str:
                fc = "1"
            elif "DISCRETE" in area_str or "离散" in str(p.get("area", "")) or "1X" in area_str:
                fc = "2"
            elif "INPUT" in area_str or "输入" in str(p.get("area", "")) or "3X" in area_str:
                fc = "4"
            else:
                fc = "3"

            db_vars.append({
                "English Name": p.get("desc"),
                "Chinese Name": p.get("desc"),
                "Function Code": fc,
                "Register Start Address": str(p.get("address")),
                "Variable Transfer Format": p.get("data_type").value if hasattr(p.get("data_type"), "value") else str(p.get("data_type")),
                "Scale Factor": str(p.get("scale", 1)),
                "Byte Order": p.get("byte_order").value if hasattr(p.get("byte_order"), "value") else str(p.get("byte_order")),
            })
        more[var_key] = db_vars

        # 序列化回 UTF-8 JSON 字符串并执行参数化 UPDATE，防止 SQL 注入且保留中文字符可读性
        new_more_json = json.dumps(more, ensure_ascii=False)
        cursor.execute("UPDATE app SET More = ? WHERE Id = ?;", (new_more_json, app_id))
        conn.commit()
        conn.close()
        return True


