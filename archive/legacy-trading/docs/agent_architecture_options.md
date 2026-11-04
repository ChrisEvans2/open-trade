> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Agent Architecture Options

## Current Direction

The current framework keeps execution deterministic and auditable:

```text
market data -> technical strategy -> StrategySignal
social/news -> SocialAIAnalyzer -> risk view + event alpha view
StrategySignal + event alpha -> combined StrategySignal
combined StrategySignal + risk view + account state -> RiskDecision
```

The social/news agent is not allowed to execute trades directly. It can:

- classify risk;
- explain relevance and ignored events;
- produce event alpha candidates;
- provide evidence, source weight, freshness, and decay.

The risk engine remains the final gate.

## TradingAgents As Backup Architecture

TradingAgents is a viable future replacement or higher-level orchestration model.

The useful idea is its separation of roles:

- technical analyst;
- sentiment/news analyst;
- fundamental analyst;
- trader;
- risk manager;
- bull/bear debate or reviewer roles.

If the current single-process framework becomes too rigid, we can migrate toward a TradingAgents-like architecture where each analyst produces a typed view and a final decision agent composes them. For now, we keep the lighter local architecture because it is easier to test, log, and operate in paper trading.

## PolyClaw-Inspired Mechanisms Adopted

We are adopting these mechanisms from event-driven trading design:

- source credibility weighting via `source_weight`;
- freshness decay via `freshness_score` and `event_decay_minutes`;
- event typing via `event_type`;
- relevance classification via `market_relevance` and `relevance_score`;
- hard filters for ads, unrelated politics, and weak transmission paths;
- small event-alpha entries only when technical strategy is neutral;
- risk veto remains separate from alpha generation.

The goal is not to let the LLM trade. The goal is to let the LLM structure messy social/news data into auditable event candidates.
