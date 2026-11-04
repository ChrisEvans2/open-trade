> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# TG Gateway 首次启动无回复修复

日期：2026-10-23（Asia/Shanghai）；仓库 `/srv/open-trade`。

用户已配置主控 `openai-codex / gpt-6.1-sol` 并启动 Gateway，但发出的“你好”没有回复。

## 原因与修复

1. systemd 显示 active 只证明进程存在。实际日志显示 Telegram 组件缺少 Python 依赖，
   触发 Hermes PM 自动准备整套隔离 runtime 并解析依赖，尚未完成 Telegram 连接。
   固定 Hermes 解释器里原来确实没有 `python-telegram-bot` 和 `cubesandbox`。
2. 首次连接日志明确记录冷启动丢弃离线待收消息：
   `drop_pending_on_cold_boot: true`。此前发出的“你好”可能已在该次清理中丢失，
   修复连接不会补回已经丢弃的 Telegram 更新，因此需要用户重新发一条消息。

本轮先正常停止唯一 Gateway，再在服务实际使用的固定 Hermes 虚拟环境中补齐依赖：

| 新增包 | 版本 | 依据 |
| --- | --- | --- |
| python-telegram-bot[webhooks] | 22.8 | 固定 Hermes 的 Telegram extra 指定版本 |
| cubesandbox | 0.7.0 | 现有 trading-cube 插件指定版本 |
| tornado | 6.6 | Telegram webhooks 所需依赖，预解析确定后固定安装 |

安装前先做 dry-run；前后包清单核对仅新增上述三项，没有升级、移除原有包。
`uv pip check` 通过，Telegram 和 Cube SDK 实际导入成功。
没有修改 Hermes 源码、绕过原生平台检测、安装另一套 Gateway 或更改用户所选模型/认证。

必要的消息保留配套是在 `controller/config.yaml` 中设置：

```yaml
platforms:
  telegram:
    enabled: true
    extra:
      drop_pending_on_cold_boot: false
```

其他配置数据保持不变。该设置让此后的冷启动保留尚未处理的 Telegram 消息；
授权仍沿用既有用户白名单，不开放陌生用户，不改变交易任务的租约、期限、风控或执行规则。

随后启动原服务 `hermes-gateway-example.service`。真实状态已达到：

- Gateway：`running`；systemd：`active/running`，无自动重启。
- `controller:telegram`：`connected`，无错误、无 needs_attention。
- Telegram SDK 已确认 `getUpdates` 正常推进，只有主控 Profile 的一个 Telegram 适配器连接。
- 新启动日志确认保留待收消息，不再为 Telegram 触发缺失依赖安装。

## 模型与验证边界

用户选择的主控模型为 **`gpt-6.1-sol / medium`**，本轮保持原样。独立认证预检成功，约 2.162 秒。
使用此 Profile 原有认证完成一次真实、无工具的模型连接检查：响应模型为 `gpt-6.1-sol`，
状态 completed，约 8.412 秒。检查仅要求回答 OK，没有调用 TG、行情、账户或订单接口。
辅助 Nous 缺少认证的启动警告不等于该主控模型不可用；本轮没有因此更换模型或凭据。

原生 Hermes 插件/认证测试 **5 passed，14.65 秒**；独立真实 PG 主控桥接测试
**7 passed，61.89 秒**，合计 12 个不同用例。临时 PG16/55432 已停止。
未为本机依赖/配置改动新增重复实现的测试，未修改业务源码或重跑无关交易测试。
固定 Hermes Git 工作树 clean；24 份受保护文件中，只有主控配置增加上述消息保留项，
模型、认证、TG Token/白名单、OKX、数据库/内部凭据、风险、服务定义和命令入口保持不变。

截至本轮状态复核，主控会话库尚无用户/助手消息；已请用户在 TG 重发验证。
**连接正常、真实模型可调用已经验证；TG 用户消息→主控模型→实际回复尚待重发确认。**
没有伪造用户消息、手工调用 `sendMessage` 发送测试消息，或另外调用 `getUpdates` 消费队列。
Gateway 按原有行为注册 Bot 命令并进行正常轮询；它处理用户真实对话属于本次修复范围。

交易 runtime 仍未启动，不把 TG 连接修复说成预测/风控/模拟成交已完成，更不启用实盘。
Gateway 开机启动仍沿用此前 disabled 状态，本轮只恢复当前运行。

## 证据与回滚

私有证据目录 `.local/private-evidence`，包含：
包清单、安装日志、配置改前副本、保护摘要、真实状态、模型布尔/耗时结果和测试日志。
私有日志不公开 Bot Token、聊天 ID 或消息正文。最初模型检查脚本误用此解释器未安装的
PyYAML 导致导入失败，已保留该失败结果，并改用现有 ruamel.yaml 后取得上述真实成功结果；
没有为检查脚本增加无关依赖。

回归命令，工作目录 `/srv/open-trade/trading-agent`：

```sh
TA_HERMES_REFERENCE=.local/private-evidence .venv/bin/pytest -q \
  tests/hermes/test_plugins.py tests/hermes/test_credentials.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q tests/integration/test_controller_bridge.py
```

回滚先停止本轮唯一 Gateway，仅移除新增的消息保留配置项和三个新增依赖，保留用户后来模型/认证修改；
移除依赖会恢复原缺失状态，重新启动前需补齐。不要覆盖整个 Profile、轮换凭据或删除历史消息/业务事实。
未 commit/push；Feature 继续忽略，其他 session 的未提交源码原样保留。
