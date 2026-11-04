> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 行情判断 LLM 网关配置计划：Early Structure Candidate

## Summary

- 新增独立本地 HTTP 网关 `market-advisor-gateway`，用于让中转站 LLM 判断“提前结构候选”，不接触本地文件、账户、风控配置或交易所密钥。
- 核心逻辑改为：当代码策略 `HOLD` 时，LLM 可输出 `early_long_candidate` 或 `early_short_candidate`；系统不直接采纳，必须通过本地代码硬规则二次确认后，才允许实盘小仓位开仓。
- 第一版实盘启用，多空都开；LLM 只能提出候选，不能绕过结构突破、成交量、大盘同向、震荡过滤、止损距离、仓位和风控限制。
- LLM 失败、超时、schema 错误时，不影响原策略，按 `neutral` 处理。

## Key Changes

- 新增配置 `market_advisor`：
  - `enabled: true`
  - `endpoint: "http://127.0.0.1:8765/v1/judge"`
  - `timeout_seconds: 3`
  - `failure_policy: "strategy_fallback"`
  - `mode: "early_candidate_confirmed_by_code"`
  - `early_candidate_live_enabled: true`
  - `early_candidate_position_pct: 0.02`
  - `early_candidate_max_leverage: 3`
  - `min_model_confidence: 0.62`
  - `breakout_lookback_bars: 20`
  - `breakout_buffer_atr: 0.15`
  - `min_volume_ratio: 1.25`
  - `max_stop_loss_pct: 0.012`
  - `btc_eth_confirmation_required: true`
  - `block_regimes: ["range", "choppy_high_vol", "transition", "shock"]`

- 新增网关服务：
  - CLI：`trade-agent advisor-gateway --host 127.0.0.1 --port 8765`
  - 环境变量独立：
    - `MARKET_ADVISOR_LLM_BASE_URL`
    - `MARKET_ADVISOR_LLM_API_KEY`
    - `MARKET_ADVISOR_MODEL`
    - `MARKET_ADVISOR_TIMEOUT_SECONDS=3`
  - 网关只负责调用中转站 API、固定 prompt、schema 校验、超时回退和审计摘要。
  - 网关不读取 `/srv/open-trade-deployment` 文件，不拥有 shell，不接 OKX key。

## Trading Flow

- 原策略有明确 `long/short` 时：
  - LLM 只做辅助判断和日志记录。
  - 不允许提高仓位、提高杠杆或改变原策略方向。
  - 可输出 `confirm/warn/block`，第一版仅记录，不改变原策略执行。

- 原策略为 `HOLD` 时：
  - 调用 LLM 判断是否存在：
    - `early_long_candidate`
    - `early_short_candidate`
    - `neutral`
  - 只有 `confidence >= 0.62` 才进入代码二次确认。
  - 二次确认全部通过后，把 `StrategySignal` 从 `HOLD` 转成小仓位早期结构信号。
  - 早期信号 `entry_reason` 必须包含 `llm_early_structure_candidate` 和通过的硬规则标签。

- 本地二次确认规则：
  - long：当前价突破最近 `20` 根 K 线结构高点，并超过 `0.15 * ATR` 缓冲。
  - short：当前价跌破最近 `20` 根 K 线结构低点，并超过 `0.15 * ATR` 缓冲。
  - 当前成交量 / 20 根均量 `>= 1.25`。
  - BTC 与 ETH 的 1h 方向必须同向确认；long 要 BTC/ETH 非下行，short 要 BTC/ETH 非上行。
  - 当前市场 regime 不得是 `range/choppy_high_vol/transition/shock`。
  - 止损距离必须 `> 0` 且 `<= 1.2%`。
  - 仓位固定不超过 `2%` 保证金。
  - 杠杆不超过当前风控上限，且额外封顶 `3x`。
  - 最终仍必须通过 `RiskEngine.evaluate`，不绕过风控。

## Interfaces

- 网关 request 只包含脱敏行情摘要：
  - `symbol`, `timeframe`, `strategy_signal`
  - 最近最多 80 根 closed candles
  - 当前 `market_snapshot`
  - 本地 regime 摘要
  - BTC/ETH 方向摘要
  - `permission_boundaries`

- 网关 response 固定 JSON：
  - `status`: `ok | error`
  - `judgement`: `early_long_candidate | early_short_candidate | confirm | warn | block | neutral`
  - `confidence`: `0..1`
  - `regime`: `clean_trend | choppy_trend | range | reversal | divergence | unknown`
  - `structure_level`: number or null
  - `invalidation_price`: number or null
  - `risk_flags`: list
  - `reason`: 中文短说明
  - `fallback`: 失败时为 `strategy_direction_used`

- 审计输出：
  - 每个 cycle 保存 `/srv/open-trade-deployment/logs/YYYY-MM-DD/HH-MM-SYMBOL/market_advisor.json`
  - 早期结构开仓事件写入：
    - `signal_source: "mixed"`
    - `strategy_type: "llm_early_structure"`
    - `entry_reasons`
    - `advisor_judgement`
    - `advisor_confidence`
    - `advisor_confirmed_by_code: true`

## Test Plan

- 单元测试：
  - LLM 输出 `early_long_candidate` 但未突破结构高点，不开仓。
  - LLM 输出 `early_short_candidate` 但 BTC/ETH 不同向确认，不开仓。
  - 成交量不足、震荡 regime、止损过宽时均不开仓。
  - 满足全部规则时，`HOLD` 转成小仓位 `long/short`。
  - 早期信号仓位不超过 `0.02`，杠杆不超过 `3x`。
  - 网关超时、500、非法 JSON、资产不匹配时返回 `neutral`，原策略不受影响。

- 集成测试：
  - mock 网关返回 early long，mock candles 构造结构突破，验证进入 `RiskEngine` 后仍受最大持仓、重复持仓、暂停模式、日亏损限制约束。
  - mock live 模式下 open position 已存在，确认 early candidate 不会重复开仓。
  - 检查发送给网关的 payload 不包含账户状态、API key、`.env`、风控配置全文和交易日志。

- 手工验证：
  - 启动：`trade-agent advisor-gateway --host 127.0.0.1 --port 8765`
  - 健康检查：`curl http://127.0.0.1:8765/health`
  - 单轮验证：`trade-agent cycle --symbol BTCUSDT --timeframe 1h --market-data okx --use-litellm`
  - 检查 `market_advisor.json` 和 trade event 中的 early structure 字段。

## Assumptions

- 中转站 API 兼容 OpenAI `/chat/completions`。
- 第一版允许实盘小仓位 early candidate，多空都启用。
- LLM 只提出候选，不直接开仓；最终开仓必须由本地代码硬规则和 `RiskEngine` 共同确认。
- Early candidate 不提高全局风险上限、不提高杠杆、不绕过暂停、日亏损、重复持仓、最大持仓等风控。
- 现有社媒 AI 逻辑保持不变，行情 advisor 是新增独立辅助通道。
