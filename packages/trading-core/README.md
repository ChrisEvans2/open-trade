# trading-core

共享风控、账户记账、持仓退出和交易所适配器，不包含CLI或调度服务。由trading-agent调用，依赖同仓库trading-quant。

默认仅查找显式参数、TRADING_CONFIG_DIR或当前工作目录的config。不会读取其他安装目录。
