> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 独立预测 Hermes Telegram 接入

日期：2026-10-23（Asia/Shanghai）；仓库 `/srv/open-trade`。

用户要求 `prediction` 也接入 Telegram，并选择与主控分开的独立 Bot。
已接入 [@example_bot](https://example.invalid/private-messaging)，用户已确认收到新 Bot 回复。
当前 Gateway 状态中 `controller:telegram`、`prediction:telegram` 均 connected，无错误；
仍使用唯一主机 Gateway，PID（私有进程标识已隐去），本轮没有重启主控或另起网关。

## 接入方式与当前权限

- 主控继续使用原 Bot、独立 `controller` Profile 和原 `trading-bridge`。
- 预测端新 Bot 的 Token 只接入 `prediction/.env`，用户白名单和通知 Home Channel 沿用同一所有者。
  Bot Token 与主控不同；没有复制 controller/operator/API 数据库凭据到预测 Profile。
- 新 Token 通过 `getMe` 验证为有效机器人，未配置 webhook；Home Channel 的 `getChat` 返回成功。
  未手工发送测试消息或调用 `getUpdates` 消费队列；实际会话由用户发起，既有 Gateway 收发。
- 新 Bot 仍由现有 host Gateway 原生多 Profile 托管，通过原生 rescan 热加载，没有新服务或第二套轮询进程。
- TG 模型对话先不开放研究任务提交、terminal 或其他模型工具，避免把聊天入口当作正式任务授权。
  CLI 仍保留 `trading_research`、Cube terminal 及既有父子任务、lease/fence/期限检查。
  白名单、Gateway 管理命令仍沿用 Hermes 行为；本轮不把模型工具收紧称为完整的只读管理权限体系。
- 用户当前预测模型 `gpt-6-luna / high`、认证、稳定偏好文件保持不变；原主控模型与凭据也保持不变。

新增本机凭据源文件：

```text
.local/private-evidence
```

此文件与生效的 `hermes-home/profiles/prediction/.env` 均为 root 所有、0600，所在目录私有。
后续如果更换 Token，需要重新验证并同步到生效 Profile；不能只改源文件后假定 Gateway 自动读取它。
不在 `.env`、文档或代码中写入交易写权限或全局风险调整。

## 必要源码配套

直接往预测 Profile 加 Telegram 字段会偏离现有规范 Profile hash，因此新增：

```yaml
hermes:
  trading_telegram_enabled: true
```

该字段默认 false，通用配置示例仍关闭；本机模拟草稿已开启。
启用时规范生成器加入 TG 平台配置、离线消息保留和显式空 TG 工具集合，并将
`trading_research` 列入该平台的 `known_plugin_toolsets`。原因是固定 Hermes 会默认启用尚未知的插件工具集，
单独写 `telegram: []` 不足以排除研究插件。

新设置纳入 Profile hash/部署锁；不改变 CLI 研究工具或预测 engine identity。
没有绕过锁校验，也没有提前生成最终部署锁。TG 注册命令及实际对话记录不作为正式预测事实。

## 已验证与尚待开发

实际验证结果：

- 用户确认新 Bot 能回复；预测 Profile 会话库只读统计有 1 条 user、1 条 assistant，另有 1 条 session_meta，
  没有把原始消息正文、聊天 ID 或 Token 复制到交付文档。
- 当前主机 Gateway 的两个 Bot 均 connected；原主控进程没有重启。
- 实际预测 Profile 的 Hermes 原生工具解析：TG 工具集合及模型工具定义为空；
  CLI 仍包含研究和 terminal，Profile 原始配置与规范生成结果一致。
- 部署单元与原生 Hermes 插件 **16 passed，16.39 秒**；独立真实 PG 部署身份/主控桥接
  **8 passed，59.26 秒**，合计 24 个不同用例。临时 PG16/55432 已停止。
- 新增测试验证 TG 权限变化使旧部署锁失效、预测 engine identity 不变，及固定 Hermes 不会为 TG
  自动加回研究插件工具；不是仅检查 YAML 字段或用模拟响应冒充真实 TG 验收。
- Ruff、格式和 `git diff --check` 通过；既有密钥/风险/模型保护摘要不变。runtime 草稿仅新增 TG 开关，
  预测 `.env` 仅增加三项 TG 配置，稳定偏好内容仍与锁定规范一致。固定 Hermes 源码未改。

本轮完成 **独立 Bot、原生 Gateway 收发和对话工具边界**。用户提到的后续功能分别保留为待办：

| 功能 | 当前状态与接入方向 |
| --- | --- |
| 只读事实查询 | TG 对话目前未配置任务领域 API 或数据库读取凭据；需另加明确的只读权限和查询工具。普通聊天回答不等于已读取实际行情/持仓。 |
| 自动行情分析报告 | 尚未增加定时触发或正式预测事实到新 Bot 的派送。应复用 outbox/HermesSender，并按报告类型选择预测发送 Profile；当前全局通知仍由 controller 发送。 |
| 迭代同意请求 | 尚未实现请求 ID、授权校验及审批回写；“请求同意”和“执行获批动作”分开，聊天中的“同意”不直接产生执行权限。审批范围后续明确，本轮不恢复已退役的策略代码自动迭代。 |

唯一交易 runtime 仍未启动，端口 18080 无监听；没有以本轮聊天声称完成正式行情预测、模拟成交、
收益验收或实盘上线，没有提高风险或调用真实订单。

## 实施边界、证据与回滚

第一次填写的 Token 与主控相同，检查后拒绝启用第二个 adapter。用户通过 BotFather 创建另一 Bot、
重新保存后，才通过唯一性/真实接口检查并启用；没有覆盖或撤销主控 Token。

必要配套是 Profile 开关、规范 hash 与工具边界测试；自主选择保留冷启动待收消息并关闭新 Bot 的
额外启动通知。没有增加新的业务角色、网关、交易入口或审批 API。

证据 `.local/private-evidence` 私有保存改前配置、
本轮前源码副本、Token 唯一性布尔结果、原生权限解析、Gateway rescan、测试和最终状态。
回归命令，工作目录 `/srv/open-trade/trading-agent`：

```sh
TA_HERMES_REFERENCE=.local/private-evidence .venv/bin/pytest -q \
  tests/unit/test_deployment.py tests/hermes/test_plugins.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q \
  tests/integration/test_deployment_identity.py tests/integration/test_controller_bridge.py
```

回滚应先关闭预测 TG 开关并通知现有 Gateway 重新扫描，确认新 adapter 停止，再从 prediction `.env`
移除本轮增加的 TG 项；保留原主控、后来模型认证和真实会话记录。源码仅回退本轮增量，不覆盖其他
session 的预检/Cube/进程修复，也不通过删除历史事实回滚。
Feature 保持 Git 忽略，未 commit/push，未提前归档 changelog。
