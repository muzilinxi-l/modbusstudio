# 任务 11 —— 会话状态持久化：记住上次打开的库文件、导入的设备点表、动态模拟状态
#
# 执行方：Antigravity　|　验收方：Claude Code　|　日期：2026-10-15
#
# 背景（用户新需求，非既有任务回归）：
#   用户反馈：每次打开 ModbusStudio 都从头来——重新猜默认数据库（`extra/hems.cdb` 等固定名）、
#   重新建一台默认"英博PCS_1"从机；上次手动切换的库文件、上次导入的设备点表、上次调好的每个点的
#   动态模拟规则（固定/正弦/方波/步进/随机）与当前数值，全部丢失，要重新配一遍。
#   本次需求：程序自动"存档"这些内容，下次启动时自动恢复；库文件失效时不崩、回落默认行为。
#
# ─────────────────────────────────────────────────────────────────
# 一、现状扫描结论（Antigravity 动手前请先核对一遍，与代码库事实对齐）
# ─────────────────────────────────────────────────────────────────
#
# 已经现成、**不用重造**的能力：
#   1. `modbusstudio/models.py` 里 `ModbusPoint`（:442）已有完整 `to_dict()`/`from_dict()`
#      （:481/:506），序列化字段包含 `value`（当前工程值）与 `sim_rule`（动态模拟规则名，
#      `SimRule` 枚举值，如"固定"/"正弦"/"方波"/"步进"/"随机"）——动态模拟状态已经在这层
#      模型里，不需要发明新的数据结构。
#   2. `ConnectionConfig`（models.py:352）已有 `to_dict()`/`from_dict()`（:390/:414），
#      覆盖 TCP host/port 与 RTU 五元组 + unit_id，可直接复用。
#   3. `SlaveDevice`（models.py:539）目前**没有** `to_dict()`/`from_dict()`，但它的核心字段
#      （`id`/`name`/`conn_config`/`db_file`/`db_device_id`/`db_device_name`）本身都是
#      基本类型或已有序列化能力的对象，`points` 的 key 是 `(area_str, address)` 元组——
#      元组键无法直接进 JSON，**这是本单需要新写的第一处东西**（见"二、需要新增/改动"第 2 项）。
#   4. `SlaveService`（services.py:281）是唯一的"从机实例集中登记处"，已有 `register_device`/
#      `remove_device`/`get_devices`/`get_occupied_tcp_ports`/`get_next_available_tcp_port`，
#      但没有"批量从 dict 重建"的入口——**需要新加一个 `restore_devices(list[dict])` 方法**
#      （见"二、需要新增/改动"第 3 项）。
#   5. `PollTask`（models.py:599）目前**没有** `to_dict`/`from_dict`，且 `poll_panel.py` 每次
#      是现算 `_get_connection_config()`、没有集中的"轮询任务列表"存储，本次**不持久化轮询任务**
#      （用户没要求，范围控制，下期再补），这条明确写进红线。
#
# 已经现成、**直接沿用不改动**的能力（本单红线不动的部分）：
#   - `modbusstudio/app.py:main()` 现有的 `settings.json` 主题读取逻辑（:222-237），
#     只在其旁边"多加读一个字段"，不改主题读取本身。
#   - `dist/settings.json` 现有内容（当前只存 `theme`）。
#   - `slave_panel.py:_select_database_file()`（:1384）的库切换流程、`_open_import_db_dialog()`
#     （:1414）的导入流程——本单只在这两个函数的"成功"分支末尾追加一次"保存"调用，不改原有
#     业务流程本身。
#
# ─────────────────────────────────────────────────────────────────
# 二、需要新增/改动的内容（按文件列出，行号为当前代码库实测位置，供定位参考）
# ─────────────────────────────────────────────────────────────────
#
# 【改动 1：`modbusstudio/models.py` — 给 `SlaveDevice` 补 `to_dict`/`from_dict`】
#   这是本次功能里唯一需要"发明"的序列化层。要求：
#   - `to_dict()` 输出字段：`id`、`name`、`conn_config`（调它的 `to_dict()`）、
#     `db_file`、`db_device_id`、`db_device_name`、`points`。
#   - `points` 字典的 key 是 `(area_str, address)` 元组，**必须约定一个稳定的字符串化规则**，
#     本单定死为 `f"{area_str}|{address}"`（竖线分隔，与现有代码里其它复合主键拼法保持一致，
#     请落码时先在仓库内 grep 确认没有已存在的 `|` 分隔的冲突用法，有的话改成别的分隔符并
#     同步改读写两侧，保持对称即可，不必拘泥于 `|`）；JSON 里以该字符串为 key，value 是
#     该点位的 `ModbusPoint.to_dict()` 结果（含 `value`/`sim_rule`）。
#   - `from_dict(d)` 对称还原：遍历 `points`，按 `"|"` 拆回 `(area_str, address)` 二元组，
#     `address` 强转 `int`，`area_str` 走 `AreaType.normalize()`（与 `ModbusPoint.from_dict`
#     里对 area 的处理保持一致，见 models.py:515），value 走 `ModbusPoint.from_dict()`。
#   - 红线：不要改动 `ModbusPoint`/`ConnectionConfig` 现有的 `to_dict`/`from_dict`，
#     只新增 `SlaveDevice` 这一层。
#
# 【改动 2：`modbusstudio/services.py` — `SlaveService` 新增 `restore_devices`】
#   - 方法签名：`restore_devices(self, devices_list: List[dict]) -> Tuple[int, List[str]]`
#     （返回成功恢复数量 + 各条恢复过程中的告警文案列表，供调用方逐条投递日志面板）。
#   - 实现：遍历 `devices_list`，每条先 `SlaveDevice.from_dict(d)`（改动 1 提供），
#     再过一次 `can_register_device(device.conn_config.comm_type)` 配额检查（复用现有
#     :319 逻辑，不新写一套），调 `register_device(device, check_quota=True)` 注册回去；
#     若该从机的 TCP 端口已被其它已恢复实例占用（可复用 `get_occupied_tcp_ports`
#     :353 的结果做预检），跳过该条并把原因写进告警列表，不要静默覆盖或抛异常卡死整个恢复流程。
#   - 红线：不要在 `restore_devices` 里调用 `start_slave`/任何会真正起监听线程或 socket 的
#     代码——恢复只是把"配置"接回内存，是否真正启动监听仍交给用户手动点"全部启动"，
#     与现有启动语义保持一致，不在启动时擅自起服务。
#
# 【改动 3：`modbusstudio/app.py` — 新增会话状态存档的"读/写"两层 + 启动时接入】
#   新增一个轻量持久化工具（建议直接放在 `app.py` 里，不新起文件，与现有
#   `main()` 里读 `settings.json` 的主题逻辑放在同一个文件里，方便测试直接调用）：
#   - `SAVE_FILENAME = "session_state.json"`（与本脚本同目录，`os.path.abspath` 定位，
#     跟现有 `settings.json` 的定位方式保持同一套 `os.path.abspath("...")` 风格，
#     app.py:224 已有先例）。
#   - `save_session_state(app: ModbusStudioAppCore, path: Optional[str] = None) -> bool`：
#     把 `app.db_path`（绝对路径，`os.path.abspath` 化，即使为相对路径也要落绝对值）+
#     `app.slave_service.get_devices()` 逐个 `to_dict()`（改动 1 提供）+
#     `saved_at`（ISO8601 时间戳）+ `schema_version`（固定 1，本单定死）写进 `session_state.json`，
#     `json.dump` 用 `ensure_ascii=False`、`indent=2`（跟现有 settings.json 的写法保持一致风格），
#     写之前先备份到 `session_state.json.bak`（`shutil.copy2`），防止写一半损坏时没有回退可用。
#   - `load_session_state(path: Optional[str] = None) -> Optional[Dict]`：读回上述 JSON，
#     任何解析失败/文件不存在直接返回 `None`，不抛错、不打印堆栈（沿用 app.py:233-236 读
#     settings.json 主题的容错风格）。
#   - **写触发点**（"自动保存"，不新增任何 UI 按钮）：
#     a) `slave_panel.py:_select_database_file()`（:1384）里，`self.app.db_path = db_path`
#        赋值成功、且 `_clear_all_slaves_and_reset_ui()` 之后，追加一行
#        `save_session_state(self.app)`（此时 `_devices` 已被清空，等价于把"新库 + 空从机列表"
#        落盘，是切换库的瞬间存档点）；
#     b) `slave_panel.py:_open_import_db_dialog()`（:1414）里 `DbImportDialog(...)` 构造成功、
#        `on_imported(target_dev)` 回调触发之后（即导入完成、从机已注册进 `slave_service`），
#        追加一行 `save_session_state(self.app)`（此时是"库 + 刚导入的完整点表"的存档点）；
#     c) `slave_panel.py:_apply_batch_point_property()`（:1095，动态模拟规则/字节序/数值批量
#        修改的统一入口，`_apply_batch_sim` :1059 是它的特例调用）成功写回点位之后，追加
#        `save_session_state(self.app)`（此时是"每个点的动态模拟规则/数值被人工调整"的存档点，
#        这是本需求里"保存被设置的动态模拟状态"真正落盘的时刻）；
#     d) `_on_click_add_point()`（:684，单个点添加成功之后）、`_add_new_slave_instance()`
#        （:939，新从机注册成功之后）、`_delete_current_slave_instance()`（:985，删除成功之后）
#        三处也各追加一行 `save_session_state(self.app)`——这些都是"用户明确改动从机/点表"
#        的时机点，粒度已经够细，不需要再监听底层波形线程每秒写一次的 `value` 变化
#        （那是引擎运行态，不是用户配置，不纳入本单范围）。
#     上述 a~d 五个触发点写死在 PROMPT 里，Antigravity 落码时照抄调用位置即可，
#     不要自己发明新的触发点或加 UI"保存"按钮。
#   - **读接入点**（启动恢复）：
#     a) `app.py:main()` 里读 `settings.json` 主题的那段（:222-237）不动，但要在 `ModbusStudioAppCore`
#        构造完成、`_probe_default_db()` 之前插入一步：先 `load_session_state()`，
#        若读到的 `last_db_path` 字段真实存在于磁盘（`os.path.exists`），把 `self.db_path`
#        直接设成这个绝对路径（跳过后续 `_probe_default_db()` 的硬编码探测，但**保留探测代码本身**
#        作为存档失效时的回落路径，不要删）；
#     b) 若存档里还带 `slaves` 列表（改动 1/2 序列化出来的内容），调
#        `self.slave_service.restore_devices(data["slaves"])`（改动 2 提供）；
#     c) 若 `last_db_path` 指向的文件已经不在（换机器/移动/删除），`restore_devices` 正常
#        执行（从机配置本身不依赖库文件还在不在，能注册回去），但要在
#        `logging_service.post("SYS", 0, ...)` 里加一行提示："存档中记录的库文件
#        [路径] 已失效，本次按默认库恢复"，同时把 `self.db_path` 落回 `_probe_default_db()`
#        的探测结果（即"库"这一块失效了，但"从机+点表"仍保留——两者是独立可恢复的
#        两块，不要绑死在一起）。
#
# 【改动 4：`modbusstudio/ui/slave_panel.py` — `initialdir` 跟随上次库所在目录】
#   `_select_database_file()`（:1384）里 `filedialog.askopenfilename(...)` 目前
#   `initialdir=os.path.abspath("extra")` 写死。改成：若 `self.app.db_path` 有值且真实存在，
#   用 `os.path.dirname(self.app.db_path)` 作为 `initialdir`，否则维持现状的
#   `os.path.abspath("extra")`。这一条是加分项（体验顺），不改也不影响核心功能，
#   但如果做，务必确认没有影响"库文件已失效"时的兜底（仍要能落到 extra/ 的默认探测位置）。
#
# ─────────────────────────────────────────────────────────────────
# 三、数据结构约定（写进 `session_state.json` 的固定格式，`schema_version=1`）
# ─────────────────────────────────────────────────────────────────
# ```json
# {
#   "schema_version": 1,
#   "last_db_path": "C:/现场数据/abc.cdb",
#   "slaves": [
#     {
#       "id": "slave_1",
#       "name": "[南向]英博PCS_1",
#       "conn_config": { ...ConnectionConfig.to_dict() 的 11 个字段，原样展开... },
#       "db_file": "C:/现场数据/abc.cdb",
#       "db_device_id": 12,
#       "db_device_name": "PCS-01",
#       "points": {
#         "4x_HoldingRegister|5": { ...ModbusPoint.to_dict() 全部 11 个字段，原样展开，
#                                    含 value/sim_rule... }
#       }
#     }
#   ],
#   "saved_at": "2026-10-15T14:30:00+08:00"
# }
# ```
# 注意：
# - 不存 `poll_tasks`（见"一"第 5 条说明，本次范围不含轮询任务）。
# - 不存 UI 布局/主题（`settings.json` 已经单独管着 `theme`，不重复存两处）。
# - 不存任何运行态 socket/线程句柄（这些本来就是每次 `start()` 全新创建的，不在本单范围）。
# - `points` 的 key 分隔符与"二、改动 1"里的字符串化规则保持一致，Antigravity 落码时
#   先 grep 一遍仓库确认 `|` 没有别的既有用法，否则换分隔符，但读写两侧必须对称。
#
# ─────────────────────────────────────────────────────────────────
# 四、执行步骤（Antigravity 请照此顺序实施）
# ─────────────────────────────────────────────────────────────────
#   0. 前置：确认 `modbusstudio/models.py`、`modbusstudio/services.py`、`modbusstudio/app.py`、
#      `modbusstudio/ui/slave_panel.py` 四个文件当前基线，与本 PROMPT 里引用的行号/方法名一致
#      （行号仅供定位，落码时以实际函数名为准，不要硬套行号）。
#   1. `models.py`：给 `SlaveDevice` 补 `to_dict`/`from_dict`（改动 1），复用 `ModbusPoint`/
#      `ConnectionConfig` 现成的序列化，不改动它们本身。
#   2. `services.py`：给 `SlaveService` 加 `restore_devices`（改动 2），复用现有
#      `can_register_device`/`get_occupied_tcp_ports`，不新写配额/端口冲突判定逻辑。
#   3. `app.py`：新增 `save_session_state`/`load_session_state` + `SAVE_FILENAME`/`schema_version=1`
#      常量（改动 3），并在 `main()` 里接入"启动恢复"（改动 3b/3c）；`_probe_default_db`
#      本身保留不删，只是被"存档命中时"短路。
#   4. `ui/slave_panel.py`：在 a~d 五个触发点（改动 3 里列的 a/b/c/d）各追加一行
#      `save_session_state(self.app)` 调用；改动 4 的 `initialdir` 跟随逻辑一并处理。
#   5. 全量自检（Antigravity 亲跑，结果如实写进 REPORT）：
#      - 手工写一个 `session_state.json`（格式见第三节），用独立脚本（不弹窗）验证
#        `SlaveDevice.from_dict` 往返一致性：`d = dev.to_dict(); d2 = SlaveDevice.from_dict(d).to_dict();
#        assert d == d2`（点表 key 字符串化/反解要能原样对上）。
#      - 起一个临时 `ModbusStudioAppCore` 实例（不实际起 GUI 主窗口，避免弹窗阻塞），
#        手工往 `slave_service` 里 `register_device` 一台带点表的从机（复用 `PointService.
#        validate_and_build_point` 造点，跟 `app.py:_init_default_slaves` 里的写法一致），
#        调 `save_session_state` 落盘，再手工 `load_session_state` 读回，逐项比对
#        `id`/`name`/`conn_config` 各字段/`points` 每个点的 `value`/`sim_rule` 是否原样还原。
#      - 模拟"库文件失效"：手工把存档里的 `last_db_path` 改成一个不存在的绝对路径，
#        重新走 `main()` 的恢复分支（不实际启动 GUI 主循环，只跑恢复逻辑部分），
#        确认 `db_path` 回落到了 `_probe_default_db()` 的探测结果、`slaves` 列表照常
#        `restore_devices` 成功、且 `logging_service` 里能看到那条"库文件已失效"的 SYS 提示。
#      - 全量回归：`.venv\Scripts\pytest.exe -q`，当前基线 58 passed, 1 skipped
#        （`test_ui_theme` 已知偶发 skip，不视为本次回归）。
#      - 门禁：`.venv\Scripts\python.exe memory/verify.py`，PASS（错误 0）。
#
# ─────────────────────────────────────────────────────────────────
# 五、红线（沿用 09/10）
# ─────────────────────────────────────────────────────────────────
#   - 不动 `memory/verify.py` 的 11 项检查逻辑、不动 `archive/`、不删 `*.legacy.py`、
#     不删既有 `[SLAVE-*]`/`[POLL-CONFIG]` 标记
#   - 不生成图片
#   - 验证时不杀用户正在运行的 ModbusStudio 进程（本次验证全程用独立脚本起临时端点/临时 AppCore，
#     不触及任何运行实例，不启动 GUI 主循环）
#   - 本次明确**不做**的事（写清楚，防止 Antigravity 顺手扩大范围）：
#     * 不持久化轮询任务（`PollTask` 目前没有 `to_dict`/`from_dict`，`poll_panel.py` 也没有集中
#       的任务列表存储，本次跳过，下期再补）
#     * 不新增任何"保存会话"之类的 UI 按钮（触发点全部是"用户已有动作成功后的自动落盘"，
#       见第四节 a~d）
#     * 不改动 `ModbusPoint`/`ConnectionConfig` 现有 `to_dict`/`from_dict` 的实现
#     * 不改动 `_probe_default_db()` 本身的探测逻辑，只是在命中存档时短路它
#
# 执行完成后：
#   - 把实施摘要 + 上面第四节第 5 项的自检结果（含库文件失效那条的实测输出）写到
#     `docs/Antigravity/11_SESSION_STATE_PERSISTENCE_REPORT.md`（与本 PROMPT 成对命名）
#   - 更新 `memory/handoffs/` 下对应记录（新起一份，编号顺延 10 之后的 handoff 文件命名惯例），
#     把该条记忆状态推进为 `proposed`，`verified_by: pending`，等 Claude 复核后推进 `verified`
#   - 在 `memory/TASKS.md` 补任务 11 条目
#   - `memory/verify.py` 门禁 PASS（错误 0）后提交，commit 信息按 09/10 惯例中文说明
#     "做了什么、为什么"，末尾加：
#     Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
