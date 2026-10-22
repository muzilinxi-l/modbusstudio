# MQTT 对账模块 — 任务 PROMPT

> 归属：Claude Code（约束/需求）。执行方：Antigravity，改完代码后在
> `python/docs/Antigravity/MQTT_RECONCILE_REPORT.md` 出 REPORT。
> 验收人：Claude Code。

## 0. 背景（为什么做）

Modbus 采集的原始寄存器经过字节序重排 + `struct.unpack` 解算后，还要乘点位
`scale` 系数才是"工程值"，再上云。当前代码：

- **全库没有 MQTT**（`paho-mqtt` 不在依赖里）；上云出口是 `hems_pairing.py`
  的 Modbus TCP 北向从机；
- `ModbusPoint.scale`（`models.py`）字段有、`to_dict`/`from_dict`/
  `session_state.json` 都带，但**运行时没有任何代码消费它**——解算完没乘系数。

要做的事：把上云数据（MQTT）和 Modbus 本地采集值按同一套系数换算后做逐点位
比对，找出链路中间哪一环算错。

## 1. 交付物（按顺序，每项都要补 pytest 测试）

### 1.1 系数换算补上（先做，无依赖）

- 在 `services.py` 的 PollService 解码环节（`PollService.read_registers`，
  行号约 838）把 `decode_value` 结果乘以 `point.scale` 再落到
  `PollResult`；新增 `PollResult.scaled_value`（float），`raw_registers`
  保持不变。
- 无点位定义的场景（裸寄存器读取）`scale` 默认 1.0，行为不变。
- 补一个单测：设一个点位 scale=2.5，解算出 4，`scaled_value` 应为 10。
- **不碰** `modbus_codec.py` 的 `decode_value` 内部（它是纯解码，系数是
  上层逻辑）。

### 1.2 `MqttService`（新建，broker = EMQX）

- 位置：`modbusstudio/mqtt_service.py`；依赖 `paho-mqtt`（加进
  `pyproject.toml`，版本锁定到当前稳定大版本）。
- **broker 明确是 EMQX**（不是裸 mosquitto），约束：
  - EMQX 标准 MQTT 监听端口 **1883**（MQTT 3.1.1/5.0 默认），`MqttService`
    默认 `port=1883`；
  - 客户端走**标准 MQTT 协议**（paho-mqtt 直连 1883），**不依赖** EMQX 的
    HTTP/REST API（dashboard 默认 18083 那一套，对账工具不碰）；
  - EMQX 默认允许匿名连接；如果用户的 EMQX 开了鉴权（用户名/密码或
    接入认证），`MqttService` 的 `username`/`password` 字段要能透传给
    paho 的 `client.username_pw_set(...)`；
  - QoS 默认 0（对账只关心值一致性，不需要 EMQX 的 QoS 1/2 持久会话）；
  - 测试环境（CI / 单测）用一个**内存 mock broker**（比如
    `emqx-mqtt-mock` 或直接 paho 的 loopback 回环测试模式），**不要求
    本地必须跑着真 EMQX**。
- 角色可配置：
  - `broker` / `port`（默认 1883）/ `client_id` / `username` / `password`；
  - `mode: publish | subscribe | both`；
  - `topic_template`（发布/订阅目标，支持 `{device_id}` 占位符）。
- 数据形态：payload 为 JSON，字段 `{device_id, point_key, value, ts}`，
  `point_key` 与 `session_state.json` 里的 `"4x_HoldingRegister|0"` 同构，
  保证两侧对得上。
- **发布侧**：在 UI 里手动触发的"上云快照"按钮（`SlavePanel` 同级，
  一行），调用 `MqttService.publish_snapshot(slave, points)`；
- **订阅侧**：`MqttService.subscribe(topic, on_message)`，回调在 worker
  线程，上层 UI 沿用现有 `after(0)` 切主线程的模式；
- 连接失败 / 断线重连：`paho` 的 `reconnect` 开，重连间隔沿用现有
  Modbus 自动重连的常量（如果 Modbus 侧有 reconnect_delay 就对齐它，
  没有就 3s）；**订阅器绝不自启发布线程**（和任务 11 的红线一致：
  `restore_devices` 不自动启动监听，这个也同理）；
- 配置持久化：`mqtt` 块加进 `session_state.json`（`MqttService.to_dict /
  from_dict`，走 `app.py` 现有的 `save_session_state` 通道，`SCHEMA_VERSION`
  保持 1，新增字段有默认值，老存档兼容）。

### 1.3 对账 CLI（stdio 工具，一期核心）

- 位置：`modbusstudio/tools/reconcile.py` + `__main__.py` 里加
  `python -m modbusstudio.tools.reconcile` 入口。
- 无 Tk：直接构造 `ModbusStudioAppCore`（无头模式，任务 11 已经铺好），
  拿 `PollService` + `MqttService(subscribe)`。
- 流程：
  1. 从 `session_state.json`（或 `--db` 指定）恢复 slave 和点位；
  2. 订阅 `--topic`（默认从 `session_state.json` 的 `mqtt.topic_template` 拼）；
  3. 轮询读一次寄存器（`PollService.read_registers`），本地值 =
     `decode × scale`；
  4. 收云端消息（默认 30s 超时，`--timeout`），拿到 `value`；
  5. 逐点位按 `--tolerance`（默认相对 1e-3 + 绝对 1e-6，可配）比对；
  6. 输出（`--format text|json`）：
     - text：表格形式，每行 `point_key  本地=… 云端=… 差=… [OK|FAIL]`；
     - json：`{point_key, local, cloud, diff, ok}` 数组 + 汇总
       `{total, ok, fail, worst_diff}`；
  7. 退出码：全 OK → 0；任一 FAIL → 1；没收到云端消息 → 2。
- **不**做 GUI 面板（二期）。

### 1.4 云端常驻监听器（`modbusstudio/tools/listen.py`）

- `python -m modbusstudio.tools.listen --broker <host> --topic <pattern>
  --out <dir>`；
- 长驻订阅（QoS 0），每条报文写一行 JSONL（`{ts, topic, payload}`），
  按天切文件；
- 复用 `MqttService.subscribe`，不重复造轮子；
- 用途：在 EMQX 侧留"眼睛"，对账 CLI 跑不通时先看日志确认
  报文到底有没有到、payload 长什么样。

## 2. 安全约束（EMQX 公网暴露）

- 用户侧 EMQX 有公网入口，**必须开 TLS + 鉴权**（EMQX 5.x 控制台：
  监听器 8883 开 TLS，认证用用户名/密码或 Basic Auth）；
- `MqttService` 增加 `tls: bool = True`、`ca_certs: str = ""` 字段，
  TLS 时走 `client.tls_set(ca_certs=...)`，端口默认 8883（TLS）/
  1883（明文，仅内网）；
- 对账 CLI 默认按 TLS 走（`--no-tls` 可关，仅内网测试用）；
- **明文 1883 暴露公网是红线**，Antigravity 在 REPORT 里必须提醒。

## 4. 红线（Antigravity 不要越界）

- 不碰 `memory/verify.py` 的 11 项检查；
- 不碰 `archive/`、`*.legacy.py`；
- 不 kill 运行中的 `ModbusStudio.exe`；
- `paho-mqtt` 只走 `pyproject.toml`，不进 `requirements.txt` 双份；
- 对账 CLI 必须能无头跑（pytest 里 import 一遍 + 断言 `--help` 不炸），
  别引入 tkinter；
- `MQTT_RECONCILE` 任务名与 REPORT 名对齐：
  `python/docs/Claude code/MQTT_RECONCILE_PROMPT.md`
  ↔ `python/docs/Antigravity/MQTT_RECONCILE_REPORT.md`。

## 5. 验收标准（Claude Code 复核点）

- [ ] `pytest` 全绿（`python/` 目录）；
- [ ] 新增单测至少 3 个：
  - 1.1 的 scale 系数单测；
  - 1.2 的 `MqttService.from_dict` 老存档兼容（给一个没有 `mqtt` 块的
    老 dict，应回落到默认值不炸）；
  - 1.3 的 CLI `--help` / 退出码 0/1/2 各跑一遍（用 mock broker，不连真机）；
- [ ] `MQTT_RECONCILE` 主题的 REPORT 落在
  `python/docs/Antigravity/`，含"改了什么 + 测试跑过哪些"；
- [ ] 本地 `session_state.json` 加 `mqtt` 块后老存档能 load 不炸；
- [ ] 对账 CLI 实际跑一次（真连一个本地 `mosquitto` 或 mock），贴 3 个
  真实输出示例（1 个全 OK、1 个 FAIL、1 个 30s 超时退出码 2）。

## 6. 待用户确认（Antigravity 不要自作主张）

- EMQX 公网入口的 TLS 证书 / 鉴权凭据（凭据不写进代码，用户手动填
  `session_state.json` 的 `mqtt` 块；PROMPT 里不出现任何密钥）；
- topic 命名规范（用户说"现在还没有报文"——一期按占位
  `ems/{device_id}/data` 设计，`topic_template` 字段可配，真实值定了
  再改配置不用改代码）；
- `paho-mqtt` 版本（默认跟最新 stable，Antigravity 加进
  `pyproject.toml` 即可，不用锁死到 patch）；
- 二期 GUI 面板挂在 `poll_panel.py` 还是独立 tab，用户定了再开。
