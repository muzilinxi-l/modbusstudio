"""HEMS 业务数据库 app 层 More 配置配对与数据路由引擎.

功能：
1. 解析 app 表中 More 字段内的所有配对绑定关系：
   - 北向引用变量 (Referred Variables) -> 南向物理变量 (Realtime Variables) 的地址映射配对
   - 策略引用指令 (Referred Orders) -> 南向控制指令 (Local Orders) 的控制配对
2. 自动构建全局配对关系表 (Pairing Routing Table)
3. 提供端到端数据自动路由：
   - 当南向 Poll 采集到源变量值时，根据配对表自动刷新北向 Slave 对应寄存器
   - 支持反向控制路由
"""

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

try:
    from .hems_db_loader import HemsAppModel, HemsDatabase, map_db_format_to_data_type
    from .modbus_codec import ByteOrderMode, ModbusDataType
    from .modbus_engine import AreaType
except ImportError:
    from hems_db_loader import HemsAppModel, HemsDatabase, map_db_format_to_data_type
    from modbus_codec import ByteOrderMode, ModbusDataType
    from modbus_engine import AreaType


logger = logging.getLogger("HemsPairing")


class PairingRule:
    """代表一条具体的点位配对绑定规则."""

    def __init__(
        self,
        src_app_id: int,
        src_app_name: str,
        src_var_name: str,
        src_address: Optional[int],
        src_format: str,
        target_app_id: int,
        target_app_name: str,
        target_register: int,
        target_fc: int,
        target_slave_id: int,
        target_format: str,
    ):
        self.src_app_id = src_app_id
        self.src_app_name = src_app_name
        self.src_var_name = src_var_name
        self.src_address = src_address
        self.src_format = src_format

        self.target_app_id = target_app_id
        self.target_app_name = target_app_name
        self.target_register = target_register
        self.target_fc = target_fc
        self.target_slave_id = target_slave_id
        self.target_format = target_format

        self.data_type = map_db_format_to_data_type(target_format or src_format)
        self.byte_order = ByteOrderMode.CDAB

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": f"[{self.src_app_name}] {self.src_var_name} (Addr: {self.src_address})",
            "target": f"[{self.target_app_name}] 寄存器: {self.target_register} (FC: {self.target_fc}, 站号: {self.target_slave_id})",
            "data_type": self.data_type.value,
            "byte_order": self.byte_order.value,
        }


class HemsPairingEngine:
    """业务配对引擎."""

    def __init__(self, db_path: str = "extra/hems.cdb"):
        self.db = HemsDatabase(db_path)
        self.apps = self.db.load_modbus_apps()
        self.rules: List[PairingRule] = []
        self._build_pairing_rules()

    def _build_pairing_rules(self):
        """解析所有基于 More 配置的配对规则."""
        self.rules.clear()

        # 1. 建立南向设备及其变量的索引字典: (device_name_lower, var_name_lower) -> (app, var_dict)
        south_index: Dict[Tuple[str, str], Tuple[HemsAppModel, Dict[str, Any]]] = {}
        for a in self.apps:
            eng_lower = a.english_name.lower().strip()
            chn_lower = a.chinese_name.lower().strip()
            for v in a.points:
                v_eng = (v.get("English Name") or "").lower().strip()
                v_chn = (v.get("Chinese Name") or "").lower().strip()
                if v_eng:
                    south_index[(eng_lower, v_eng)] = (a, v)
                    south_index[(chn_lower, v_eng)] = (a, v)
                if v_chn:
                    south_index[(eng_lower, v_chn)] = (a, v)
                    south_index[(chn_lower, v_chn)] = (a, v)

        # 2. 扫描所有包含 Referred Variables 的应用（如北向服务 [North]30）
        for target_app in self.apps:
            more = target_app.raw_more
            ref_vars = more.get("Referred Variables", [])
            for rv in ref_vars:
                full_eng = rv.get("English Name", "")
                reg_addr_str = rv.get("Register Address") or rv.get("Register Start Address")
                if not reg_addr_str or str(reg_addr_str).lower() == "none":
                    continue

                try:
                    reg_addr = int(reg_addr_str)
                except ValueError:
                    continue

                fc = int(rv.get("Function Code", 4))
                slave_id = int(rv.get("Slave Id", 1))
                fmt = rv.get("Variable Transfer Format", "Unsigned short 1")

                # 解析格式: "设备名|变量名" (例如: "FDDSL600|Communication Status")
                src_app_name = "未知设备"
                src_var_name = full_eng
                src_app_id = 0
                src_reg = None

                if "|" in full_eng:
                    parts = full_eng.split("|", 1)
                    dev_key = parts[0].strip().lower()
                    var_key = parts[1].strip().lower()

                    match = south_index.get((dev_key, var_key))
                    if match:
                        src_app, src_var = match
                        src_app_id = src_app.app_id
                        src_app_name = src_app.english_name
                        src_var_name = src_var.get("English Name", var_key)
                        addr_str = src_var.get("Register Start Address") or src_var.get("Register Address")
                        if addr_str and addr_str != "None":
                            src_reg = int(addr_str)
                    else:
                        src_app_name = parts[0].strip()
                        src_var_name = parts[1].strip()

                rule = PairingRule(
                    src_app_id=src_app_id,
                    src_app_name=src_app_name,
                    src_var_name=src_var_name,
                    src_address=src_reg,
                    src_format=fmt,
                    target_app_id=target_app.app_id,
                    target_app_name=target_app.english_name,
                    target_register=reg_addr,
                    target_fc=fc,
                    target_slave_id=slave_id,
                    target_format=fmt,
                )
                self.rules.append(rule)

        logger.info(f"已根据 app.More 配置成功解析出 {len(self.rules)} 条配对绑定规则！")

    def get_summary_text(self) -> str:
        """生成人类可读的配对全景汇总报表."""
        lines = [
            f"=== HEMS 业务数据库 (app.More) 配对绑定规则全景 (共 {len(self.rules)} 条配对) ===\n"
        ]
        curr_target = ""
        for r in self.rules:
            if r.target_app_name != curr_target:
                curr_target = r.target_app_name
                lines.append(f"\n【目标北向转发服务: {curr_target} (App ID: {r.target_app_id})】")
            src_addr_info = f"原始地址: {r.src_address}" if r.src_address is not None else "内部变量"
            lines.append(
                f"  🔗 [源南向设备: {r.src_app_name}] 点位: {r.src_var_name:<28s} ({src_addr_info}) "
                f"===> 映射至北向寄存器: {r.target_register:4d} (FC: {r.target_fc}, 站号: {r.target_slave_id}, 类型: {r.data_type.value}, 变位: {r.byte_order.value})"
            )
        return "\n".join(lines)
