> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Hermes 使用现有 Codex 登录恢复

日期：2026-10-25（Asia/Shanghai）；仓库 `/srv/open-trade`。

用户要求先解决 `hermes --profile prediction model` 重新登录时出现 `Device code request returned status 403`，本轮只修复 Hermes 的登录和模型访问连接，不改预测任务重试逻辑。

## 原因与修复

本机的 Codex 交互入口已配置使用现有本地 Mihomo 代理 `http://127.0.0.1:17890`，Hermes 的交互入口和后台进程没有相同的连接配置。后台预测进程还会清理继承环境，所以只在某个终端设置代理并不能修好后台任务。

对相同官方端点的实际对比：

| 访问方式 | 设备码登录接口 | 未认证模型接口 |
| --- | --- | --- |
| 直连 | HTTP403 | HTTP403，网页错误页 |
| 本机既有代理 | HTTP200，包含合法设备码字段 | HTTP401 JSON，进入正常鉴权环节 |

检查时，Codex、prediction、controller 的现有访问令牌尚有约39小时有效期；后续带原令牌的真实模型调用成功。因此本次故障的已证实原因是访问连接差异，不能将403简单解释为“登录已过期”。没有重新登录、交换新令牌、导入或复制Codex凭据，也未改用户的模型选择或推理强度。

必要的本机配置改动：

- 在 `hermes-home/profiles/prediction/.env` 与 `hermes-home/profiles/controller/.env` 各增加 HTTPS_PROXY、HTTP_PROXY 及小写形式，均指向既有127.0.0.1:17890。Hermes原生加载Profile环境，交互登录、新启动的研究进程都能使用；没有放宽子进程的全局环境白名单。
- 同时设置 NO_PROXY/no_proxy，保留 localhost、127.0.0.1、::1、api.telegram.org 直连，避免本机任务API、Cube管理连接和原Telegram链路被代理转发。
- 唯一Gateway是多Profile常驻进程，其模型连接读取进程环境，因此新增 `/etc/systemd/system/hermes-gateway-example.service.d/20-local-proxy.conf`，配置相同的网络变量。确认当时 active_agents=0 后，仅重启此Gateway一次使其生效；没有新增网关或重复Bot轮询。
- 唯一paper runtime无需重启，新启动的Hermes研究进程自行读取更新后的Profile网络配置。没有修改runtime、部署锁、提示词、固定Hermes源码、代理服务或其他仓库。

## 实际验证

- 从 `env -i` 干净环境运行，使用Hermes原生 `.env` 加载、认证解析和设备码申请函数：prediction、controller均验证设备码端点可用。一次性设备码只在内存中验证字段后丢弃，没有打印、保存、提供给用户或完成设备授权。
- 两个Profile分别用原有认证发送一次无工具、无行情/账户数据的真实 `gpt-6-luna / high` 请求，均收到 `response.completed`。prediction约9.269秒，controller约6.721秒；这两次是连接验证，不作为交易预测事实。
- 干净shell中的 `hermes --profile prediction config path` 仍解析原prediction目录；没有更换CLI入口或Profile。
- Gateway从PID760897切换为936344，实际进程已带代理变量，最终状态running，controller:telegram和prediction:telegram均connected且无错误。没有手工发送TG测试消息；通道连接不等于已验收每个聊天回复。
- 北京时间01:44，唯一模拟runtime的 `/readyz` 返回200，全部健康检查通过。原调度的BTC父综合任务已正常SUCCEEDED，正式预测发布时间为01:44:14（UTC前一日17:44:14）。不是手工补预测、模拟模型返回或触发订单来验收；后续预测是否通过结构/上下文校验仍遵守原规则。
- 保护核对覆盖20份文件，只有两个Profile的`.env`发生预期变化，且原有所有键值保持不变，只增加6个网络变量；原有认证文件、Codex登录文件、凭据、风险、模型配置的内容和权限均保持不变。`.env`及其私有备份均为0600。

部署配置单元、真实PostgreSQL部署身份及主控桥接回归 **20 passed / 52.13秒**，`systemd-analyze verify`及`git diff --check`通过；独立测试PG58917已停止，业务PG58918保持运行。命令：

```bash
TA_TEST_DATABASE_URL=postgresql+psycopg://ta_test_admin@127.0.0.1:55432/postgres \
  trading-agent/.venv/bin/pytest -q \
  trading-agent/tests/unit/test_deployment.py \
  trading-agent/tests/integration/test_deployment_identity.py \
  trading-agent/tests/integration/test_controller_bridge.py --tb=short
```

真实模型连接成功、Gateway连通、正式预测发布和交易收益分别报告，不能据此声称预测任务的重试时间冲突已修复。本轮没有修改重试/180秒新鲜度/风控/通知策略。

最终01:46:41复查，Gateway仍running、两个TG均connected、代理变量已应用，原认证与配置保护核对通过。业务runtime此时又因agent检查不健康返回503，其他检查均通过，不能将01:44短时200描述为整个预测系统持续健康；既有任务未产出结果及重试问题仍需另行处理。本轮最后可见的PermissionDeniedError请求记录在01:43:11，之后已出现上述真实模型调用和BTC正式发布成功。这是登录/连接恢复证据，不是对后续所有业务失败的定性结论。

## 操作、边界与回滚

可继续执行原命令 `hermes --profile prediction` 使用现有登录；这次无需重新登录。若用户主动选择全新登录，设备码申请接口已通过真实检查，后续浏览器确认属于用户自己的登录步骤，本轮没有代替用户完成。

官方认证文档说明设备码登录的操作条件和无浏览器环境的认证方式，参见[OpenAI 官方认证说明](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)。本轮使用 OpenAI Docs 技能核对官方说明，实际结论以本机网络对比和真实模型请求为依据，不将一般文档当成本机故障证明。

本轮自主决定是将已验证的既有代理用于两个Hermes Profile及唯一Gateway，并让本机API与Telegram保持直连，属于恢复连接的必要配套；没有增加业务Agent、订单入口或风险权限。保留已有未提交工作，不commit/push。`Feature.md`继续被Git忽略。

证据目录 `.local/private-evidence` 私有保存两个`.env`改前备份、保护摘要、受限验证脚本和汇总；备份含既有私有配置，不加入Git、不输出内容。

回滚只移除两个Profile本轮追加的网络配置，保留后续新增配置和原凭据；移除本轮Gateway drop-in后 `systemctl daemon-reload`，在无活跃对话时重启同一个Gateway。不要恢复旧令牌或覆盖整个`.env`，不要回滚数据库/交易事实/部署锁。回滚后直连403可能重新出现。代理本身的长期可用性仍是网络依赖，本轮未增加自动切换线路或改变代理服务。
