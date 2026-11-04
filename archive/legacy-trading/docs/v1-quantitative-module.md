> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 新v1量化计算模块

2026-10-08：按用户要求，将新v1的指标计算实现放在本项目，算法沿用v1。

- 实现唯一来源：`packages/trading-quant/src/trading_quant/features.py`。
- 本项目接口：`trading_system.quantitative.compute_features`。
- 新v1接口：保留 `trading_agent.market.features.compute_features`，内部调用共享实现并保留领域模型校验与错误代码。
- 输入是具有UTC收盘/可用时点、数量单位及外部观测的规范数据对象；不直接把旧market.json或未收盘K线冒充v1输入。
- 所有Return、Wilder、回归、成交量、合约观测及质量状态公式保持原样；8组完整结果与原engine配置hash验证一致。
- 旧策略自身的EMA、入场阈值、信号生成与风控没有改写；本模块为新v1计算提供实现归属，不宣称已把旧策略全部改用新指标。

该纯计算包不依赖新Agent、PostgreSQL、Hermes或交易执行模块。修改公式时必须同步更新指标规范/版本及验证证据，不能只改名称。

本地安装：`uv sync --frozen --extra dev`。发布/部署需要独立构建并分发 `packages/trading-quant` 的wheel。
新v1源码位于本仓库 `trading-agent/`，共享依赖路径也在仓库内部。
成交与代码迭代兼容的范围、启用步骤和未完成事项见
[兼容说明](../../legacy-research/docs/legacy-compatibility.md)；同仓库安装见
[v1整合说明](v1-integration.md)。
