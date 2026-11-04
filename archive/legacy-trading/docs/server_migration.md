> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Server Migration Guide

This project is designed so the repository contains the full runnable framework,
while credentials and runtime state stay outside Git.

## What Git Includes

- Python package source under `src/`
- Shared v1 indicator package under `packages/trading-quant/`
- Complete v1 research agent under `trading-agent/` (same Git repository)
- Tests under `tests/`
- Non-secret YAML config under `config/`
- Deployment templates under `deploy/`
- Agent prompts and architecture notes under `docs/`
- `.env.example` with required environment variable names

## What Git Must Not Include

These paths are intentionally ignored and must be recreated or restored on the
new server:

- `.env`
- `logs/`
- `state/`
- `workspaces/`
- `.pytest_cache/`
- `__pycache__/`

If historical paper-trading results matter, back up `logs/` and `state/`
separately before shutting down the old server. Do not push them to GitHub.

## Fresh Server Setup

```bash
git clone https://github.com/ChrisEvans2/open-trade.git /srv/open-trade-deployment
cd /srv/open-trade-deployment

python3 -m venv .venv
. .venv/bin/activate
pip install -e packages/trading-quant
pip install -e ".[dev]"

cp .env.example .env
mkdir -p logs state workspaces/trading_workspace
```

Edit `.env` on the new server and fill in the real values:

- `LITELLM_BASE_URL`
- `LITELLM_API_KEY`
- `OKX_API_KEY`
- `OKX_API_SECRET`
- `OKX_API_PASSPHRASE`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SOCIAL_TELEGRAM_BOT_TOKEN`
- `BLOCKBEATS_API_KEY`

Keep `.env` private. The repository only stores `.env.example`.

For the new v1 agent on the `dev` branch, follow
[the same-repository integration guide](v1-integration.md). Pulling the repository
does not activate its Profile, cron, Cube configuration or runtime service.

## Deployment Notes

The current systemd template is `deploy/trade-live-okx.service`. It assumes:

- project directory: `/srv/open-trade-deployment`
- environment file: `/home/operator/.hermes/.env`
- `trade-agent` executable path: `/home/operator/miniconda3/bin/trade-agent`

If the new server uses a different Python environment, update `ExecStart` before
installing the service. If the project is not restored to `/srv/open-trade-deployment`, also
update the service file and any Hermes agent profiles or prompts that reference
`/srv/open-trade-deployment`.

The strategy review agent prompt lives at:

```text
/srv/open-trade-deployment/docs/trade_review_strategy_iteration_agent_prompt.md
```

It intentionally uses absolute `/srv/open-trade-deployment/...` paths so the deployed review
agent stays inside the trading project. When migrating to a different project
path, update those paths before redeploying the agent profile or cron job.

## Validation Checklist

Run these checks after migration:

```bash
cd /srv/open-trade-deployment
. .venv/bin/activate
pytest -q
trade-agent command "/status"
trade-agent cycle --symbol BTCUSDT --timeframe 1h
```

Before enabling the daemon, confirm that `.env` contains the intended paper/live
settings and that `config/runtime_state.yaml` is in the expected mode.

## 预测驱动入口的后续切换

当前只完成本地开发，未迁移线上。新部署流程见[实现说明](../../../docs/prediction-driven-implementation.md#9-迁移与恢复边界)。同一交易账户不得同时运行旧CLI和新预测runtime；首次切换及engine版本切换要求原入口已处理完持仓和未决订单。新执行默认关闭，禁止因为代码已安装就直接开启。需先完成[在线验收项](../../../docs/prediction-driven-validation.md#仍需真实模型或在线验收)，包括固定保护单、实际成交/费用、净持仓模式和Cube隔离。
