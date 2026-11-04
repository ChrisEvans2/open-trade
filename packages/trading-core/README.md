> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# trading-core

供唯一预测 runtime 复用的交易核心，不提供 CLI 或调度器，不依赖归档框架。

- `risk/`、`parameters.py`、`policy.py`：原风险裁决、风险参数及配置读取。
- `accounting.py`、`prediction_adapter.py`：确定性仓位记账、资金费、标记与退出；由调用方持久化。
- `execution/`、`market_data/okx_client.py`：既有执行/签名客户端；只由确定性执行服务使用。
- `social/`、`ai/`：沿用的社媒风险辅助；不提供独立方向裁决或模型订单工具。
- `schemas.py`：沿用的账户、风险与执行数据结构。

无生产入口。数据库任务事务、订单幂等与持久状态由 `trading-agent` runtime 负责；
量化与结构计算统一依赖 `trading-quant`。风险数值和已有计算不因提取改变。
