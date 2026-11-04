> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 旧 Telegram Bot 迁入本机主控 Hermes

日期：2026-10-23（Asia/Shanghai）；仓库 `/srv/open-trade`。

用户要求复用旧服务器配置过的 Telegram Bot，迁入当前服务器；随后明确选择自行配置主控模型。

同日后续：用户已配置主控模型并启动 Gateway；首次 Telegram 依赖与待收消息问题的修复、真实连接及模型检查，
见 [启动无回复修复](tg-gateway-startup-fix-2026-10-23.md)。下文未启动状态保留为迁移当时的记录。

随后按用户要求新增了独立预测 Bot，两个 Bot 复用同一 Gateway，见
[独立预测 Telegram 接入](prediction-telegram-2026-10-23.md)；原主控 Bot 保留。

## 截图原因与最终分工

截图是固定 Hermes 版本的安装保护：命名 Profile 默认不能单独安装自己的 Gateway，主机默认
Gateway 复用多个 Profile。报错代码并没有在此分支检测端口占用。本机检查也没有运行中的
Hermes Gateway；既有 `job-match-gateway.service` 属于其他业务，本轮未操作。

本项目继续保留：

`TG ⇄ 唯一 Hermes Gateway ⇄ controller 主控 ⇄ 唯一交易 runtime ⇄ prediction 预测 Hermes`

不为 `prediction` 使用 `--force` 或 `gateway.standalone` 另建入口，也不新增自制网关。

## 已完成的配置

- 通过 Hermes 原生 `create_profile` 创建独立 `controller`，不克隆预测模型、认证、会话或历史。
  用户选择稍后手动配置模型，因此主控 `model.default/provider` 留空，没有复制预测 OAuth 文件。
- 将用户已经保存到 `prediction/.env` 的 `TELEGRAM_*` 设置迁入 `controller/.env`：
  Bot Token、用户白名单、Home Channel 等原值不变。预测 Profile 移除这些项，其他环境项保持不变，
  避免同一 Bot Token 被两个 Profile 的 poller 重复使用。
- 仅在 `controller/.env` 接入之前已生成的内部主控令牌，令牌值未轮换；没有把 operator、
  数据库、迁移、备份或 OKX 凭据放入主控 Profile。
- 安装当前 `trading-bridge`，API 绑定本机唯一 runtime；主控 CLI/TG 工具集合限定为
  `trading_bridge`，不开放预测提交、订单、主机 terminal 或写代码工具。新增主控 SOUL 明确查询、
  解释/复盘请求、事实边界和简洁中文回复。
- 在 Hermes 根目录新增 host Gateway 配置，启用原生多 Profile 复用；根 Profile 不启用 TG，
  Bot 由拥有该配置的 `controller` 服务，不把 TG 消息交给预测 Profile。
- 将本机 runtime 草稿的 `notification.target` 绑定已有 TG Home Channel；除该字段外，
  runtime 草稿其他数据一致。未生成最终部署锁、启动交易 runtime 或写入正式业务事实。

根目录：`.local/private-evidence`。
主控目录：`<上述根目录>/profiles/controller`。Profile 名称不包含 paper，当前模拟环境仍由路径和执行配置标识。
主控 `.env`、配置和 SOUL 均为 root 所有、0600，目录仅本机管理。

已安装原生 systemd 服务 **`hermes-gateway-example.service`**，固定到上述 Hermes 根目录。
按当前 root 管理、root 私有 Profile/解释器的部署方式，使用 `--run-as-user root` 显式指定已有身份，
没有放宽 `/root` 或凭据目录权限。服务目前 **inactive / disabled**；主控模型未配好前不接收消息，
也不在重启后意外启动。

## 用户接下来操作

先配置主控模型和其独立认证：

```sh
hermes --profile controller model
```

完成后，从默认根目录启动唯一主机 Gateway（命令不加 `--profile prediction`）：

```sh
hermes gateway start --system
hermes gateway status --system
```

确认正常后，可开启开机启动：

```sh
systemctl enable hermes-gateway-example.service
```

然后由用户向原 Bot 发一条消息验证对话。本轮没有主动发 TG 测试消息、启动 Gateway 或调用主控模型。
本机交易 runtime 尚未启动，主控此时不能声称存在新预测、成交或收益；交易事实查询可用性要在
后续 runtime 部署验收后另行验证。

## 验证与边界

- 按 [Telegram getMe](https://core.telegram.org/bots/api#getme) 检查现有 Token：HTTP 200、ok=true、
  is_bot=true；说明这份 Token 当前有效。按
  [getWebhookInfo](https://core.telegram.org/bots/api#getwebhookinfo) 检查：HTTP 200、未配置 webhook。
  没有删除 webhook、重置 Token、读取/消费 `getUpdates` 或发送任何消息。
- 原生配置加载并验证三个独立 scope：只有 `controller` 启用 TG 并加载 Bot Token、白名单与内部主控令牌；
  default 和 prediction 均不加载上述凭据。原生插件发现成功，状态/持仓/任务申请工具存在，预测提交工具不存在。
- `hermes --profile controller config path` 可找到新主控配置。systemd 定义检查和 `git diff --check` 通过。
- 部署单元、原生 Hermes 插件、真实 PG 主控桥接回归 **21 passed，56.06 秒**；其中真实 PostgreSQL
  7 项，使用独立 55432 测试集群，结束后已停止。模型和消息传输是测试替身，不作为实际 TG 对话证据。
- 实际应用库只读复核：50 张业务表，迁移 `0009_prediction_knowledge`，仍只有 runtime_control 初始化 1 行。
  行情、应用数据库和备份 timer 保持 active，无服务重启。既有密钥、风险、预测模型配置/认证的保护摘要不变。
- 新主控创建时尚无模型，因此原生工具没有生成 `config.yaml`；首次准备脚本在备份该缺失文件时停止，
  未开始迁移，修正为允许空白 Profile 后继续。首次原生 systemd 安装也按默认策略拒绝 root，未写服务；
  显式指定现有 root 身份后安装成功。未更改固定 Hermes 源码、保护策略或其他 session 的改动。

旧服务器是否仍有另一个 poller、当前 TG 实际收发和主控真实模型调用尚未验收；本轮没有把 Token 有效
写成已完成对话迁移。用户称旧服务器已过期，若后续启动实际出现 409/重复 poller，再据真实日志定位。

回归命令，工作目录 `/srv/open-trade/trading-agent`：

```sh
TA_HERMES_REFERENCE=.local/private-evidence \
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q \
  tests/unit/test_deployment.py tests/hermes/test_plugins.py tests/integration/test_controller_bridge.py
```

证据与改前私有备份位于 `.local/private-evidence`，0700/0600；
私有 Token/会话 ID 不进入源码、交付文档或日志输出。本轮只追加忽略的 Feature 记录和交付文档，
不 commit/push，不修改其他 session 的业务源码。

## 回滚

先停止并卸载本轮唯一 host Gateway，再按私有备份逐项恢复 prediction 中原有 TG 设置及通知目标；
仅在确认无用户后续变更后撤回本轮新建主控和 host 配置。不要同时启动恢复后的 prediction Gateway
与主机 Gateway，也不要整份覆盖后来模型认证、轮换 Token、删除历史事实或改动无关网关。
