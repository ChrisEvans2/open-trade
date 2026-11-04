# Trading Agent

唯一结构预测runtime，使用Python 3.11、PostgreSQL16、固定Hermes版本和Cube沙箱。与共享core/quant一起从本仓库安装。

```bash
uv sync --frozen --extra cube --group hermes-test
uv run --frozen --extra cube --group hermes-test pytest -q tests/unit
uv run --frozen --extra cube --group hermes-test ta --config config.yaml config check --json
```

模型生成预测与交易意图；风控、幂等执行和账务由程序完成。默认关闭执行，示例不是现成部署。

见[部署指南](../docs/deployment.md)、[测试说明](../docs/testing.md)和[已知限制](../docs/limitations.md)。迁移保留历史表及只读兼容，不安装旧研究入口或代码迭代Agent。
