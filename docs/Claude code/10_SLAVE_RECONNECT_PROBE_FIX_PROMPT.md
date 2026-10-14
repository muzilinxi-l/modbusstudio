# 任务 10 —— 自动重连（SLAVE-RECONNECT）现场"无效"缺陷：探活漏判 + 重建静默失败
#
# 执行方：Antigravity　|　验收方：Claude Code　|　日期：2026-10-14
#
# 背景（与 09 不同，这是 09 验收后新发现的现场缺陷，非 09 回归）：
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
# 执行步骤（Antigravity 请照此顺序实施）：
#   0. 前置：当前工作区 modbusstudio/modbus_engine.py 已还原到任务 10 修改前状态（与 019048b^ 一致，
#      未含本次任何改动）；请在此基础上实施，不要依赖 019048b 那版 diff（那版已作废重做）。
#   1. 在 modbus_engine.py 顶部 `import logging` 后新增 `import socket`。
#   2. ModbusTransportServer.__init__ 中，在 `self._is_running = False` 之后、
#      `self.on_packet_log` 之前，新增：
#          # [SLAVE-RECONNECT-10] 单端点重建互斥锁：`restart_if_broken()`（"自动重连"按钮）与
#          # `start()` 内部的"半死态强制重建"分支都可能对同一端点执行 stop+start，若两个线程
#          # （UI 线程 + 服务线程）同时进入会产生重复 stop/start 竞态，用一把细粒度锁串行化
#          # 同端点重建操作（该锁仅保护本实例的重建过程，不阻塞 Hub 全局字典，避免锁嵌套）。
#          self._rebuild_lock = threading.Lock()
#   3. ModbusTransportServer.is_healthy() 末尾（`_server is None` 分支之后、`return True` 之前）追加：
#          # [SLAVE-RECONNECT-10] 底层监听端口探活：TCP listener 的 `transport` 在正常绑定时为
#          # 非空 `asyncio.Server` 且 `is_active()==True`；listen socket 被关闭（网络中断）后
#          # `transport` 退化为 `Server sockets=()`（无实际 socket）且 `is_active()==False`，
#          # 而线程/事件循环因仍阻塞在 `await self.serving` 上保持"存活"表象，必须靠这一项才能识破。
#          # 启动竞态处理：`start()` 中 `_ready.set()` 发生在 `create_task(serve_forever)` 之后、
#          # 但 `await listen()` 真正绑定 socket 之前，故 start() 返回瞬间 `is_active()` 常为 False
#          # （实测 100% 复现），0.3s 宽限窗口内同样豁免该项判据，与 `_server is None` 的分支口径一致。
#          try:
#              if not self._server.is_active():
#                  return (time.time() - self._start_ts) < 0.3
#          except Exception:
#              # `is_active()` 是 pymodbus 稳定 API，正常不会抛错；兜底防止底层对象被破坏时探活本身崩溃
#              pass
#          return True
#      同时把该方法 docstring 与注释里"三重探活"字样更新为"三重探活 + [SLAVE-RECONNECT-10] 底层监听端口探活"，
#      并补一段说明现场实证（见本文件头部"缺陷 1"一节），说明为什么只看 _is_running/线程/loop 不够。
#   4. ModbusTransportServer.restart_if_broken() 里，把原来的裸
#          self.stop()
#          self.start()
#      改为整体持锁 + 双重检查 + 锁外自检：
#          # [SLAVE-RECONNECT-10] 细粒度互斥：串行化同一端点的 stop+start，避免与 start() 内
#          # 半死重建分支并发竞态（两个线程同时 stop/start 同一 listener 会导致端口绑定失败）。
#          with self._rebuild_lock:
#              # 双重检查：拿到锁后再探一次，可能已被另一路径恢复
#              if self.is_healthy():
#                  return False
#              # 僵死：走一次 stop + start，重建 loop / server / 线程
#              self.stop()
#              # stop 后 _is_running 已清、_loop/_server/_thread 已置 None
#              self.start()
#          # 锁外做重建后自检与日志回传，避免长时间持锁阻塞 UI
#          ...
#          # start 后 5 个已注册的从机（_server_context._slaves）仍需保持挂载，
#          # 因为 _server_context 是 self 的属性，未被 stop 清除；
#          # 新起的 TCP server 仍用同一个 context，路由表保留，从机数据块保留。
#          # [SLAVE-RECONNECT-10] 重建后主动验证一次"是否真的能收报文"（TCP 走回环真实往返；RTU 串口
#          # 无网络回环，跳过），并向 UI 日志面板回传结果，使"自动重连"按钮能明确告知用户
#          # "已重建且监听已恢复 / 已重建但端口仍不可用（请检查端口占用或网络）"，避免重建失败时静默假恢复。
#          self._log_rebuild_outcome()
#          return True
#      注意：方法开头原有的 `if self.is_healthy(): return False`（锁外第一道探活）保持不变。
#   5. 在 restart_if_broken() 下方新增方法：
#          def _log_rebuild_outcome(self) -> bool:
#              """[SLAVE-RECONNECT-10] 重建后做一次轻量"真实可达"自检并回传结果日志。
#
#              为什么必须做：`restart_if_broken()` 的 `start()` 内部在 1s 内未就绪时**只 `logger.warning`
#              不抛错**（`_ready.wait(timeout=1.0)` 超时即返回），若端口恰好被占用（`Address already in use`），
#              重建会**静默失败**——此时 UI 侧"自动重连"按钮仍显示"已重建 N 个"，但实际收不到报文，
#              正是本次现场"自动重连无效"问题的另一半根因。故重建后对 TCP 端点做一次回环 socket 往返自检，
#              把真实结果（成功/失败）显式回传到 `on_packet_log`，让操作者能在日志面板直接看到监听是否真的恢复。
#              """
#              if self.comm_type != "TCP":
#                  # RTU 串口端点无法跨进程建立 TCP 回环自检，维持原有行为，不额外自检
#                  return True
#              _, host, port = self.endpoint_key
#              # 回环自检：连接目标端点（非 127.0.0.1 时连 127.0.0.1，保证本地可测通）
#              probe_host = "127.0.0.1"
#              try:
#                  with socket.create_connection((probe_host, port), timeout=1.0):
#                      reachable = True
#              except Exception:
#                  reachable = False
#              if self.on_packet_log:
#                  if reachable:
#                      self.on_packet_log(
#                          f"[自动重连] 端点 {self.endpoint_key} 重建成功，监听已恢复（回环自检通过）", "SYS"
#                      )
#                  else:
#                      self.on_packet_log(
#                          f"[自动重连] 端点 {self.endpoint_key} 重建后监听仍未就绪（回环自检失败，"
#                          f"请检查端口 {port} 是否被占用或网络异常）", "ERROR"
#                      )
#              return reachable
#   6. 不要改动 restart_all_broken()、auto_reconnect_if_needed()、_on_auto_reconnect() 三层已有逻辑；
#      缺陷 1/2/3 的修复只落在 ModbusTransportServer 这一层。
#
# 验证（Antigravity 执行完毕后请原样实跑，结果写进 REPORT）：
#   1. 单端点模拟僵死并修复：
#      .venv\Scripts\python.exe - 内联脚本：ModbusTransportServer(("TCP","127.0.0.1",port)) +
#      ModbusSlaveEngine 挂载 -> start() -> 正常 is_healthy()==True；
#      srv._ModbusProtocol__close(reconnect=True)（模拟 OS 层网络中断，不杀线程/loop）-> sleep 1.2s
#      -> is_healthy()==False（缺陷 1 已修复，能探测出）；
#      ts.restart_if_broken()==True -> is_healthy()==True；
#      ModbusTcpClient("127.0.0.1",port).connect()==True，读回 4x:5==4321（数据未丢，context 保留）。
#   2. 全量回归：
#      .venv\Scripts\pytest.exe -q  => 全量通过（当前基线 59 passed；含 09 全修 8 个回归 + 多站并发 7 个 +
#        test_full_ui_import_e2e + test_review_fix_09，无一失败；已知 test_ui_theme 竞态若触发可单独说明，不视为本次回归）
#   3. 门禁：
#      .venv\Scripts\python.exe memory/verify.py  => PASS (错误: 0, 警告允许为已知 code-drift 提示，非阻断)
#
# 复现步骤（给现场 / 后续验收人员，供 REPORT 里说明）：
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
#
# 执行完成后，请：
#   - 把实施摘要 + 测试验收结果写到 `docs/Antigravity/10_SLAVE_RECONNECT_PROBE_FIX_REPORT.md`（与本 PROMPT 成对）
#   - 更新 memory/handoffs/ 对应记录，把 MEM-0210 状态推进为 verified（verified_by=antigravity）
#   - 在 memory/verify.py 门禁通过后提交（commit 信息按 09 惯例，chore/fix 风格，中文说明"做了什么、为什么"）
