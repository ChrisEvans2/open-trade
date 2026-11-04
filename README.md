# Open Trade

BTC、ETH 多周期结构预测交易框架。模型负责识别结构、构建条件情景和提出交易意图；确定性程序负责有效性校验、风险限制、持仓记账和执行。唯一运行入口是 `trading-agent/` 的 `ta serve`。

公共仓库提供代码、迁移、插件、提示词、合成测试夹具和通用示例。交易历史、个人账户、凭据、模型登录、运维快照和私人知识库链接由使用者独立保存。

| 目录 | 职责 |
| --- | --- |
| `trading-agent/` | 行情证据、预测轮次、父子协调、评分、知识复盘、执行与API |
| `packages/trading-core/` | 确定性风控、账户、退出与交易所适配器 |
| `packages/trading-quant/` | 指标、结构几何与数据质量计算 |
| `config/` | 新编写的暂停模拟示例，不包含个人交易参数或账户 |
| `tests/` | 共享核心测试；runtime测试在其自身目录 |
| `docs/` | 通用架构、部署、测试及限制说明 |

## 安装与离线检查

需要Python 3.11和uv。从仓库根目录安装共享测试环境：

```bash
uv sync --frozen --python 3.11 --extra dev
uv run --frozen --extra dev pytest -q tests
```

安装runtime及其测试依赖：

```bash
cd trading-agent
uv sync --frozen --extra cube --group hermes-test
uv run --frozen --extra cube --group hermes-test ta --config config.yaml config check --json
uv run --frozen --extra cube --group hermes-test pytest -q tests/unit
```

示例配置刻意不含Hermes路径、通知目标、Cube部署及认证信息，配置检查可返回未完成；这不是可直接启动的业务部署。执行默认关闭，示例策略暂停，私有交易所读取和通知关闭。检查和测试不会替你启动服务。

## 工作流

子Agent按1h/4h/6h/12h周期处理30根已收盘K线及1根未收盘快照；后台确认指标使用80根已收盘数据，临时指标与确认指标分开。父Agent综合有效子报告，必要时读取原冻结证据，不额外采集自己的1h行情。

预测发布、结构有效性、评分和交易收益分别记录。目标到期不直接关闭已成交持仓。复盘可以提出知识改进，不可改写历史预测、评分或账户风险。分钟成交聚合由确定性采集器负责，模型只读取带来源和质量标记的结果。

交互可通过 `TG ⇄ Hermes Gateway ⇄ controller Profile ⇄ runtime ⇄ prediction Profile` 接入。两个Profile承担不同角色，复用一个runtime和现有outbox通知，不额外建立网关。

## 使用说明

- [架构与数据约定](docs/architecture.md)
- [独立部署准备](docs/deployment.md)
- [测试与证据边界](docs/testing.md)
- [已知限制](docs/limitations.md)
- [公共仓库维护](docs/repository-maintenance.md)
- [维护环境的自动同步流程](docs/automatic-sync.md)

本仓库尚未指定许可证。
