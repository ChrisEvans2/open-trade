# Trading Agent

本仓库唯一的结构预测 runtime：行情 → 各周期预测 → 父 Agent 综合 → 确定性风控与执行
→ 验证评分 → 预测复盘与知识更新。使用 Python 3.11、PostgreSQL 16 和固定版本 Hermes；
CubeSandbox 承接模型终端命令。执行默认关闭，模型不接订单接口。

主控交互继续复用 Hermes：你 / TG ⇄ Hermes Gateway ⇄ 主控 Hermes ⇄ 本 runtime
⇄ 独立预测 Hermes。主控与预测父 Agent 使用不同 Profile；主控通过 trading-bridge 查询
预测、持仓、运行状态，或申请解释/指定区间复盘。runtime 调用预测 Profile 处理任务，
结果通过已有 outbox/HermesSender 和主控 Profile 发送。无需另建 TG 网关。
接口、分工与本轮验收见[主控链路恢复](../docs/controller-hermes-restore-2026-10-22.md)。

本目录属于 `hermes-trade`，不依赖仓库外同名项目。交易核心通过
`../packages/trading-core` 安装，量化计算通过 `../packages/trading-quant` 安装。
唯一服务入口为 `ta --config <配置文件> serve`；旧小时分类研究与策略代码迭代流程仅留作归档。
目录、历史保留与升级说明见 [结构收敛交付](../docs/runtime-core-consolidation-2026-10-22.md)。
以下命令在本目录执行。

```sh
uv sync --frozen --extra cube --group hermes-test
uv run --extra cube --group hermes-test pytest -q
uv run --extra cube --group hermes-test ta config check --json
```

测试数据库由`deploy/local-postgres.py`创建隔离集群，或通过`TA_TEST_DATABASE_URL`指定
专用测试实例；每项集成测试创建并删除自己的临时数据库。不要指向实盘数据库。
Hermes兼容测试使用`.local/private-evidence`或`TA_HERMES_REFERENCE`中的固定提交源码；
该目录缺失时明确跳过。模型与行情传输使用fixture，数据库与本机进程测试使用真实资源。

正式部署步骤见[部署说明](docs/deployment.md)。人工环境执行`ta migrate`，运行环境执行
`ta serve`；服务器仅保留角色token摘要。查询CLI从人工或主控环境读取对应原始token。
`ta doctor --notify`可由现有Hermes no-agent Cron每5分钟调用，独立发现进程失联。

Agent 沙箱接入、运行续期、真实组件验收及尚未完成的部署前置条件见
[Cube 接入记录](../docs/agent-cube-connection-2026-10-21.md)。

用户只读 OKX 数据接入、当前真实采集证据及后续配置/开发清单见
[只读数据与后续工作](../docs/readonly-data-and-next-steps-2026-10-22.md)。

结构预测、验证、知识复盘及复用原风控的 PaperTransport 已有本地实现，完整模型与线上验收
仍需单独完成。PaperTransport 不模拟完整手续费、滑点或资金费，不用于宣称真实净收益。
旧 `paper.enabled` 和收益实验入口继续关闭，不能作为新预测模拟执行的启动开关。
当前功能依据见 [预测实现](../docs/prediction-driven-implementation.md)，历史 v1 资料见
[归档索引](../archive/README.md)。

默认 pytest 运行当前 unit/integration/Hermes 测试。旧研究测试目录已删除，测试不再将
archive 加入导入路径；备份、通知、用量与历史读取使用当前结构预测或纯数据库事实夹具。
真实 Cube 外部验收仍位于 `tests/live/`，须显式 opt-in。
清理范围和验证见 [旧测试与日志清理](../docs/obsolete-research-cleanup-2026-10-22.md)。
