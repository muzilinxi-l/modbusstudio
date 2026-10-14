# 任务 10 —— 自动重连（SLAVE-RECONNECT）现场"无效"缺陷：探活漏判 + 重建静默失败
#
# 执行方：Claude Code　|　验收方：Claude Code　|　日期：2026-10-14
#
# 背景（与 09 不同，这是新发现的现场缺陷，非 09 回归）：
#   09 把"自动重连"按钮接到了 auto_reconnect_if_needed -> restart_all_broken -> restart_if_broken，
#   但现场反馈"自动重连没起作用，显示未检测到僵死，只有全部停止再全部启动才恢复"。
#
#   Claude 实测（ModbusStudio 运行中，用 pymodbus 内部 __close(reconnect=True) 模拟网络中断）坐实两处独立缺陷：
#
#   缺陷 1（探活漏判，根本原因）：
#     ModbusTransportServer.is_healthy() 旧的"三重探活"只检查 _is_running / thread.is_alive() /
#     loop.is_running()。但 _run_server_loop 里 run_until_complete(_serve()) 阻塞在
#     await self._server.serving 上，即使底层 TCP listen socket 已被 OS 关闭（网络中断），
#     线程与事件循环依然"活着"，_server 对象也仍在（退化为 Server sockets=() 空实例）。
#     于是 is_healthy() 恒返回 True，restart_if_broken() 认为"健康"直接 return False，
#     "自动重连"按钮因此永远显示"未检测到僵死"。
#     修复：is_healthy() 追加第 4 项判据 self._server.is_active()（底层监听端口是否真正存活），
#     并对 start() 返回瞬间 transport 尚未 bind 完成的 0.3s 启动竞态窗口做豁免（与 _server is None
#     分支同口径），避免正常启动被误判为僵死。
#
#   缺陷 2（重建静默失败）：
#     start() 内部 _ready.wait(timeout=1.0) 超时后**只 logger.warning 不抛错**；若端口被占用
#     （Address already in use）或 listener 再次 bind 失败，restart_if_broken() 仍返回 True，
#     UI 显示"已重建 N 个"但实际收不到报文 —— 正是"必须全部停止再启动才恢复"的另一半根因
#     （全部停止走 stop_all + 全新 start，路径干净，故能恢复）。
#     修复：restart_if_broken() 重建后追加 _log_rebuild_outcome()：TCP 端点做一次 127.0.0.1 回环
#     socket 真实可达自检，成功/失败均通过 on_packet_log 回传到 UI 日志面板（SYS/ERROR），
#     使操作者能在日志里直接看到"监听已恢复 / 端口仍不可用"，不再静默假恢复。
#
#   缺陷 3（并发竞态，加固）：
#     restart_if_broken()（UI 线程触发）与 start() 内"半死态强制重建"分支（服务线程触发）可能对同一
#     端点并发 stop+start，导致端口绑定竞态。修复：ModbusTransportServer 增加 _rebuild_lock
#     （细粒度，仅保护本实例重建过程，不持有 Hub 全局锁，避免锁嵌套），stop+start 段整体持锁串行化，
#     取锁后二次 is_healthy() 双重检查防止重复重建。
#
# 改动文件（仅 1 个 .py，均在 modbus_engine.py，未触碰 09 已验收的 H1/H2/H3/M1/M3/L1/L2/T1 逻辑）：
#   - modbusstudio/modbus_engine.py
#     - __init__：新增 self._rebuild_lock（threading.Lock）
#     - is_healthy()：追加 _server.is_active() 探活判据 + 0.3s 启动竞态豁免
#     - restart_if_broken()：_rebuild_lock 串行化 + 双重检查 + 重建后调用 _log_rebuild_outcome()
#     - 新增 _log_rebuild_outcome()：TCP 端点回环 socket 自检 + on_packet_log 结果回传（SYS/ERROR）
#     - 顶部新增 import socket（仅用于回环自检，不影响既有 import）
#   - 未改：restart_all_broken()（Hub 层逻辑不变）、auto_reconnect_if_needed()（Service 层不变）、
#     _on_auto_reconnect()（UI 层按钮回调不变，其 status 文案"已重建 N 个 / 未检测到僵死"
#     现在会因缺陷 1 修复而能真正命中"已重建"分支；缺陷 2 修复后即使重建失败也会在日志面板
#     补一条 ERROR 提示，不再静默）
#
# 验证（Claude 已实跑，Antigravity 复核时须原样复现）：
#   1. 单端点模拟僵死并修复：
#      .venv\Scripts\python.exe - 内联脚本：ModbusTransportServer(("TCP","127.0.0.1",port)) +
#      ModbusSlaveEngine 挂载 -> start() -> 正常 is_healthy()==True；
#      srv._ModbusProtocol__close(reconnect=True)（模拟 OS 层网络中断，不杀线程/loop）-> sleep 1.2s
#      -> is_healthy()==False（缺陷 1 已修复，能探测出）；
#      ts.restart_if_broken()==True -> is_healthy()==True；
#      ModbusTcpClient("127.0.0.1",port).connect()==True，读回 4x:5==4321（数据未丢，context 保留）。
#   2. 全量回归：
#      .venv\Scripts\pytest.exe -q  => 59 passed, 0 failed（含 09 全修 8 个回归 + 多站并发 7 个 +
#        test_full_ui_import_e2e + test_review_fix_09，无一失败；已知 test_ui_theme 竞态本轮未触发）
#   3. 门禁：
#      .venv\Scripts\python.exe memory/verify.py  => PASS (错误: 0, 警告: 7，全部为已知
#        code-drift 提示，非阻断)
#
# 复现步骤（给现场 / 后续验收人员）：
#   - 起一个从机服务（如 127.0.0.1:502），用 modpoll 等真实客户端持续轮询；
#   - 制造网络中断（拔线 / 切路由 / 防火墙拦 502 端口 / 网卡休眠），等待 ~2-3s；
#   - 点"自动重连"按钮：修复前应显示"未检测到僵死 listener"且客户端继续收不到响应；
#     修复后应显示"已重建 1 个僵死 listener"，且日志面板追加一行
#     "[自动重连] 端点 (('TCP','127.0.0.1',502)) 重建成功，监听已恢复（回环自检通过）"（SYS）；
#     若端口被占用等导致重建未真正成功，则追加 ERROR 行"…重建后监听仍未就绪（回环自检失败，
#     请检查端口 502 是否被占用或网络异常）"，不再静默。
#
# 红线（沿用 09）：
#   - 不动 memory/verify.py 的 11 项检查逻辑、不动 archive/、不删 *.legacy.py、不删既有 [SLAVE-*]/[POLL-CONFIG] 标记
#   - 不生成图片
#   - 验证时不杀用户正在运行的 ModbusStudio 进程（本次验证全程以独立 python -c 起临时端点，未触及运行实例）
