"""
    HEMS 业务数据库 app 层 More 配置配对与北向-南向数据路由引擎模块。
    关键导入：json/os/sqlite3/logging（数据库解析与日志）、HemsAppModel/HemsDatabase/map_db_format_to_data_type（加载数据库与类型映射）、ByteOrderMode/ModbusDataType/AreaType（变位与区域枚举）。
对外暴露：PairingRule（单条配对绑定规则模型）、HemsPairingEngine（配对解析与路由表构建引擎）。
"""

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

# 为什么使用 try/except 双路径导入：
# 1. 当通过 `python -m modbusstudio.app` 作为包启动时，使用相对导入 `.hems_db_loader`；
# 2. 当在单文件调试或部分独立脚本测试场景下直接导入时，回退到同级模块名导入；
# 3. 这种兼容策略避免了因工作目录或启动入口不同而抛出 ImportError。
try:
    from .hems_db_loader import (
        HemsAppModel,
        HemsDatabase,
        map_db_format_to_byte_order,
        map_db_format_to_data_type,
    )
    from .modbus_codec import ByteOrderMode, ModbusDataType
    from .modbus_engine import AreaType
except ImportError:
    from hems_db_loader import (
        HemsAppModel,
        HemsDatabase,
        map_db_format_to_byte_order,
        map_db_format_to_data_type,
    )
    from modbus_codec import ByteOrderMode, ModbusDataType
    from modbus_engine import AreaType


# 初始化模块级命名日志器，归属 HemsPairing 命名空间以便按模块过滤排障日志
logger = logging.getLogger("HemsPairing")


# =====================================================================
# 1. 配对绑定规则数据模型 (PairingRule)
# 设计背景：
# 在一体机（现场 Linux ARM 设备）的 EMS 架构中：
# - 南向（South）作为 Modbus 主机轮询现场 BMS（电池）、PCS（储能变流器）、电表等物理设备；
# - 北向（North）作为 Modbus 从机向远端调度主站或云端网关暴露汇总后的寄存器点表；
# - `PairingRule` 负责描述“哪台设备的哪个采集变量”被映射到“哪个北向从机的哪个寄存器地址”。
# =====================================================================
class PairingRule:
    """代表一条“南向物理采集设备点位 -> 北向对外转发从机寄存器”的配对绑定规则。"""

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
        """初始化单条点位配对绑定规则并推导目标数据类型与默认字节序。"""
        # 为什么同时保存 src_app_id 与 src_app_name：
        # 1. src_app_id 用于程序内部精确关联 HemsAppModel 实例（若未匹配到南向设备则为 0）；
        # 2. src_app_name 用于界面直接呈现设备英文标识，即使原南向设备在库中被禁用或移除，仍能保留名称线索；
        # 3. src_address 允许为 None，因为部分北向点位引用的是 EMS 策略层计算出的虚拟量而非物理寄存器。
        self.src_app_id = src_app_id
        self.src_app_name = src_app_name
        self.src_var_name = src_var_name
        self.src_address = src_address
        self.src_format = src_format

        # 记录北向转发服务（如 [North]30）的应用标识、目标寄存器地址、功能码与从机站号：
        # - target_register：北向从机对外暴露的 Modbus 寄存器起始地址；
        # - target_fc：北向主站读取该点位时使用的功能码（如 FC03 保持寄存器、FC04 输入寄存器）；
        # - target_slave_id：北向从机的 Unit ID（站号）。
        self.target_app_id = target_app_id
        self.target_app_name = target_app_name
        self.target_register = target_register
        self.target_fc = target_fc
        self.target_slave_id = target_slave_id
        self.target_format = target_format

        # 为什么优先使用 target_format 映射数据类型与字节序：
        # 1. 北向转发点表决定了对外暴露的寄存器字长与变位规则；
        # 2. 若 target_format 为空则回退使用南向源变量的 src_format；
        # 3. 遇到 target_format 与 src_format 均未配置时记录警告，并按默认 INT16 / CDAB 兜底。
        chosen_format = target_format or src_format
        if not chosen_format:
            logger.warning(
                f"[HEMS-PAIRING] 规则 R_{target_app_id}_{target_register} (目标:{target_app_name}, 源:{src_app_name}) "
                f"未配置数据格式(target_format与src_format均为空)，按默认 INT16/1寄存器/CDAB 兜底处理，若实际为多字数据请人工核对"
            )
        self.data_type = map_db_format_to_data_type(chosen_format)
        self.byte_order = map_db_format_to_byte_order(chosen_format, default_mode=ByteOrderMode.CDAB)

    @property
    def rule_id(self) -> str:
        """生成当前配对规则的唯一可读编号（格式 R_<target_app_id>_<target_register>）。"""
        # 为什么用 target_app_id + target_register 组合作为规则 ID：
        # 同一北向转发服务下每个目标寄存器地址只能绑定唯一数据源，因此该组合天然具备唯一性且支持按关键字检索。
        return f"R_{self.target_app_id}_{self.target_register}"

    @property
    def rule_type(self) -> str:
        """返回当前配对规则的分类名称（固定为变量映射）。"""
        # 标识该规则来源于 SQLite 数据库 app 表 More 字段的 Referred Variables 映射配置
        return "变量映射 (More)"

    @property
    def source_var(self) -> str:
        """格式化南向源设备与源变量的可读描述字符串。"""
        # 为什么对 src_address 做非 None 判断：
        # 当南向变量具备物理寄存器地址时附加 `[地址: X]`，若为内部派生变量则省略地址后缀避免显示 `[地址: None]`。
        src_addr_str = f" [地址: {self.src_address}]" if self.src_address is not None else ""
        return f"[{self.src_app_name}] {self.src_var_name}{src_addr_str}"

    @property
    def target_var(self) -> str:
        """格式化北向目标转发从机及映射寄存器的可读描述字符串。"""
        # 直观呈现北向主站问询该点位时对应的北向应用名、目标寄存器号与功能码
        return f"[{self.target_app_name}] 寄存器: {self.target_register} (FC: {self.target_fc})"

    @property
    def description(self) -> str:
        """生成包含目标从机站号、数据类型与变位模式的简短摘要。"""
        # 汇总北向寄存器编码所需的三要素：
        # 1. target_slave_id：北向从机站号（Unit ID）；
        # 2. data_type.value：北向转发寄存器的数据类型（如 UINT16 / FLOAT32）；
        # 3. byte_order.value：32 位多寄存器数据的字节变位模式（默认 CDAB）。
        return f"站号: {self.target_slave_id} | 类型: {self.data_type.value} | 变位: {self.byte_order.value}"

    def to_dict(self) -> Dict[str, Any]:
        """将当前配对规则序列化为字典结构，供日志或外部导出消费。"""
        # 为什么将属性格式化为扁平的字符串字典：
        # 1. 方便直接通过 json.dumps 序列化输出到调试文件或控制台；
        # 2. source 字段聚合了南向设备名、变量名及物理地址；
        # 3. target 字段聚合了北向应用名、目标寄存器号、功能码及站号；
        # 4. 这种无嵌套的字典结构对报表渲染与单元测试断言最为友好。
        return {
            "source": f"[{self.src_app_name}] {self.src_var_name} (Addr: {self.src_address})",
            "target": f"[{self.target_app_name}] 寄存器: {self.target_register} (FC: {self.target_fc}, 站号: {self.target_slave_id})",
            "data_type": self.data_type.value,
            "byte_order": self.byte_order.value,
        }


# =====================================================================
# 2. HEMS 业务配对解析引擎 (HemsPairingEngine)
# 设计背景：
# 负责从现场 `.db`/`.cdb` 数据库中提取全部 Modbus 应用，先构建南向设备点位哈希索引，
# 再扫描北向应用的 `Referred Variables` 列表，完成 O(1) 级跨表关联与路由表生成。
#
# 核心工作流拆解：
# 步骤 1：通过 HemsDatabase(db_path).load_modbus_apps() 加载全部南北向设备模型；
# 步骤 2：遍历所有应用的 points 列表，构建 (设备名, 变量名) -> (HemsAppModel, point_dict) 索引；
# 步骤 3：遍历所有应用的 raw_more["Referred Variables"]，按 "设备名|变量名" 切分并反查南向源寄存器；
# 步骤 4：生成 PairingRule 列表并提供 get_summary_text() 报表输出。
# =====================================================================
class HemsPairingEngine:
    """HEMS 业务数据库北向与南向点位配对解析引擎。"""

    def __init__(self, db_path: str = "extra/hems.cdb"):
        """初始化配对引擎并立即扫描数据库构建全部配对规则。"""
        # 参数说明：db_path 为一体机现场导出的 SQLite 数据库文件路径（默认 extra/hems.cdb）。
        # 副作用说明：实例化期间会打开 SQLite 文件读取 app 表并完成内存路由表构建。
        # 1. 创建底层数据库访问器并读取 app 表中所有 Modbus 相关应用模型
        self.db = HemsDatabase(db_path)
        self.apps = self.db.load_modbus_apps()
        self.rules: List[PairingRule] = []
        # 2. 构造阶段即完成全量配对解析，方便调用方直接通过 load_rules() 获取结果
        self._build_pairing_rules()

    def load_rules(self) -> List[PairingRule]:
        """返回已解析出的全部北向-南向点位配对规则列表。"""
        # 返回已在 __init__ 中构建完成的内存 PairingRule 列表（只读消费，无需重复查询 SQLite）
        return self.rules

    def _build_pairing_rules(self):
        """解析所有应用 More 字段中的 Referred Variables 并与南向设备点位建立映射关联。"""
        # 先清空历史规则列表，防止重复调用时产生重复配对项
        self.rules.clear()

        # 为什么先建立 (device_name_lower, var_name_lower) 的四组合哈希索引：
        # 1. 现场工程配置中 Referred Variables 的引用字符串格式为 "设备名|变量名"；
        # 2. 不同工程人员配置时可能混用设备的英文名或中文名、变量的英文名或中文名，且大小写不统一；
        # 3. 预先将 `(英文设备名, 英文变量名)`、`(中文设备名, 英文变量名)`、`(英文设备名, 中文变量名)`、
        #    `(中文设备名, 中文变量名)` 全部转为小写写入字典，可将后续数千个点位的关联查找从 O(N*M) 降至 O(1)。
        south_index: Dict[Tuple[str, str], Tuple[HemsAppModel, Dict[str, Any]]] = {}
        for a in self.apps:
            eng_lower = a.english_name.lower().strip()
            chn_lower = a.chinese_name.lower().strip()
            for v in a.points:
                v_eng = (v.get("English Name") or "").lower().strip()
                v_chn = (v.get("Chinese Name") or "").lower().strip()
                # 注册英文变量名的中英文设备组合键
                if v_eng:
                    south_index[(eng_lower, v_eng)] = (a, v)
                    south_index[(chn_lower, v_eng)] = (a, v)
                # 注册中文变量名的中英文设备组合键
                if v_chn:
                    south_index[(eng_lower, v_chn)] = (a, v)
                    south_index[(chn_lower, v_chn)] = (a, v)

        # 为什么遍历所有应用检查 Referred Variables：
        # 一体机北向转发服务（如 [North]30）通过在 app.More.Referred Variables 中声明引用的南向点位，
        # 将不同 BMS/PCS 南向设备的实时采集数据汇聚映射到北向从机的连续寄存器空间。
        for target_app in self.apps:
            more = target_app.raw_more
            ref_vars = more.get("Referred Variables", [])
            for rv in ref_vars:
                full_eng = rv.get("English Name", "")
                # 兼容不同固件版本 JSON 字段命名（Register Address 或 Register Start Address）
                reg_addr_str = rv.get("Register Address") or rv.get("Register Start Address")
                # 为什么跳过空地址或 "none" 字符串：
                # 部分未启用的预留引用点位在数据库中填为 "None"，不占用实际北向 Modbus 寄存器。
                if not reg_addr_str or str(reg_addr_str).lower() == "none":
                    continue

                # 过滤非整型非法寄存器地址配置，防止脏数据中断整个数据库的配对加载
                try:
                    reg_addr = int(reg_addr_str)
                except ValueError:
                    continue

                # 提取北向功能码（默认 FC04 输入寄存器）、北向从机站号（默认 1）及传输数据格式
                fc = int(rv.get("Function Code", 4))
                slave_id = int(rv.get("Slave Id", 1))
                fmt = rv.get("Variable Transfer Format", "Unsigned short 1")

                # 默认回退值：当引用名未包含 "|" 或未命中南向索引时，仍保留原始标识供排障
                src_app_name = "未知设备"
                src_var_name = full_eng
                src_app_id = 0
                src_reg = None

                # 为什么按首个竖线 "|" 分割并回退保留原名：
                # 1. 标准引用名格式为 "南向设备名|南向变量名"（例如 "FDDSL600|Communication Status"）；
                # 2. 使用 split("|", 1) 只切分第一个竖线，防止变量名本身含有特殊字符时被误拆；
                # 3. 若在 south_index 中命中，则提取南向源应用的真实 ID、标准英文名与南向物理寄存器起始地址；
                # 4. 若未命中（如引用了非 Modbus 虚拟设备或已删除设备），仍保留拆分出的设备名与变量名以便界面排查。
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
                        # 提取南向设备侧的物理寄存器起始地址，若为 "None" 则保持 src_reg = None
                        addr_str = src_var.get("Register Start Address") or src_var.get("Register Address")
                        if addr_str and addr_str != "None":
                            src_reg = int(addr_str)
                    else:
                        src_app_name = parts[0].strip()
                        src_var_name = parts[1].strip()

                # 构建强类型配对规则实例并追加到全局路由表
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
        """生成人类可读的北向-南向配对绑定规则全景汇总文本报表。"""
        # 构造报表头部标题行，标明配对规则总条数
        lines = [
            f"=== HEMS 业务数据库 (app.More) 配对绑定规则全景 (共 {len(self.rules)} 条配对) ===\n"
        ]
        # 在 [get_summary_text] 中更新局部变量 [curr_target]：为后续协议编码、服务调度或界面渲染准备中间状态
        curr_target = ""
        # 为什么按 target_app_name 分段输出：
        # 1. 现场一台一体机可能配置多个北向对外服务通道（如同时对接省调与本地监控后台）；
        # 2. 当检测到 target_app_name 变化时插入分组小标题，使报表层次清晰；
        # 3. 对无物理地址的派生点位标注“内部变量”，方便联调工程师快速区分实测点与计算点。
        # 在 [get_summary_text] 中遍历集合元素：逐项执行校验、转换或状态同步，确保批量数据处理的完整性
        for r in self.rules:
            # 在 [get_summary_text] 中执行前置条件分支判断：根据当前运行状态或参数类型走对应处理路径，避免非法状态穿透
            if r.target_app_name != curr_target:
                # 在 [get_summary_text] 中更新局部变量 [curr_target]：为后续协议编码、服务调度或界面渲染准备中间状态
                curr_target = r.target_app_name
                lines.append(f"\n【目标北向转发服务: {curr_target} (App ID: {r.target_app_id})】")
            # 在 [get_summary_text] 中更新局部变量 [src_addr_info]：为后续协议编码、服务调度或界面渲染准备中间状态
            src_addr_info = f"原始地址: {r.src_address}" if r.src_address is not None else "内部变量"
            lines.append(
                f"  🔗 [源南向设备: {r.src_app_name}] 点位: {r.src_var_name:<28s} ({src_addr_info}) "
                f"===> 映射至北向寄存器: {r.target_register:4d} (FC: {r.target_fc}, 站号: {r.target_slave_id}, 类型: {r.data_type.value}, 变位: {r.byte_order.value})"
            )
        # 从 [get_summary_text] 返回执行结果给调用方，结束当前处理分支
        return "\n".join(lines)


