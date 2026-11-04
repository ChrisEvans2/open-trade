> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Strategy Optimization Todo

1. Done: Make simulated exits use each trade's own ATR-derived stop loss and take profit.
2. Done: Enable trailing take-profit only after a trade has reached at least a meaningful R multiple, instead of any small positive PnL.
3. Done: Limit highly correlated same-direction entries in the same cycle or portfolio window.
4. Pending: Add a daily strategy review report covering win rate, average win/loss, max favorable excursion, max adverse excursion, and weakest symbols.
5. Pending: Study Vibe-Trading-style skills/backtest workflows as a research layer before considering any execution-framework migration.
6. Done: Add first-stage market entry-quality filters: ADX trend strength, EMA slope, EMA separation, and ATR-based anti-chase protection.
7. Pending: Add higher-timeframe market context so 1h entries can be confirmed against 2h trend and BTC/ETH regime before opening positions.
