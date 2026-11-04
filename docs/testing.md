# 测试与证据边界

共享核心、runtime单元、Hermes契约和PostgreSQL集成分别运行；同名测试模块不应混在一个pytest收集进程。

```bash
# 仓库根目录
uv run --frozen --extra dev pytest -q tests
# trading-agent目录
uv run --frozen --extra cube --group hermes-test pytest -q tests/unit
TA_HERMES_REFERENCE=/path/to/pinned-hermes uv run --frozen --extra cube --group hermes-test pytest -q tests/hermes
TA_TEST_DATABASE_URL=postgresql+psycopg://test_admin@127.0.0.1:5432/postgres uv run --frozen --extra cube --group hermes-test pytest -q tests/integration
```

PG连接必须指向可丢弃的独立实例。每个用例创建独立数据库、迁移并清理；备份测试需要PostgreSQL16工具在PATH中。未指定连接时local-postgres.py会尝试启动本项目的开发实例；root用户需由管理员预先准备非root测试实例。

Hermes契约缺少固定checkout时显式skip，不能记成已通过；模型/API/沙箱传输使用fixture，固定原生代码和本机进程实际加载。tests/live中的3项外部Cube验收需显式opt-in，普通回归不运行。

受控模拟开平仓使用临时生成的合成paper策略，不读取其他业务配置。可用TA_ACCEPTANCE_CONFIG显式指定自己的模拟策略；不得指向实盘。即使账务测试通过，也不等于真实模型自主成交或净收益验收。
