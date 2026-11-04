> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

> 2026-10-11更新：本文保留v1历史设计。当前结构预测/知识库以[新实现说明](../../../docs/prediction-driven-implementation.md)为准；旧策略代码迭代及memory-search/read活跃入口已退役，历史证据保留。

# 新v1与旧交易项目兼容

2026-10-08确认：保留新v1处理逻辑；兼容成交盈亏、策略源码修改、测试与改动记录。
指标使用当前v1算法，计算实现归属hermes-trade。

新v1现位于同一Git仓库 `hermes-trade/trading-agent/`；指标包位于
`hermes-trade/packages/trading-quant/`。本次目录整合没有增加业务功能，
安装与同步见 [整合说明](../../legacy-trading/docs/v1-integration.md)。

2026-10-10新增策略迭代历史经验召回：SQLite/FTS5派生索引提供summary，
按冻结ID分页展开content，并可在迭代记录中引用。规则与未实现项见
[策略经验召回](strategy-memory.md)。正式预测记忆不变。

## 已实现

| 请求 | 实现与边界 |
|---|---|
| 沿用v1指标 | 公式与指标名称移到 `hermes-trade/packages/trading-quant`，标准库独立包；旧项目通过 `trading_system.quantitative` 提供接口，新v1保留原 `compute_features` 领域适配入口 |
| 保留v1逻辑 | 四周期80/30输入、趋势缓存、1h主预测、评分、案例筛选、正式提示词、任务调度、数据库事务和原研究插件权限未修改；迁移前后8组完整指标输出与engine配置hash相同 |
| 成交盈亏 | 单独读取旧项目live/paper事件及账户文件，分别统计闭环成交、已记录收益、胜率、profit factor、币种和方向；mark_update不算平仓，资金费不重复加减 |
| 代码迭代 | 兼容工具读取策略源码/hash，接受一个有成交证据的候选补丁；仅允许现有策略Python源码与新增测试 |
| 运行测试 | 程序冻结候选源码和原有测试，在独立Cube沙箱运行固定pytest命令；没有宿主机执行候选测试的生产回退 |
| 记录改动 | 追加证据快照、请求、原文件/候选、测试状态与中文Markdown改动记录；测试失败不写回，写回途中失败恢复原文件 |

兼容入口没有替换默认日报，也没有把旧成交塞进正式预测案例或已验证经验。
旧策略的入场规则、现有EMA等辅助计算及风控仍保留；它们不参与新v1指标计算。
本轮没有改变旧策略正在使用的算法，也没有把新v1预测接到下单接口。

## 调用流程

1. `legacy_trade_review`：冻结指定已发生区间的成交与账户证据。完整快照保存在旧项目 `workspaces/v1-compat/evidence/`；模型看到统计与每种模式最多30个平仓示例。
2. Agent解释证据和样本量，提出一个可验证假设。账户是读取时的状态，不能当作复盘区间的历史账户快照；旧事件首次可用时间未知，不用于正式预测记忆。
3. `legacy_strategy_source`：读取允许的策略文件和原始SHA256。
4. `legacy_strategy_iterate`：提交冻结证据ID、幂等迭代ID、假设、风险、回滚说明与完整文件替换内容。
5. 程序在禁出站、无宿主挂载/业务凭据的Cube中运行原有测试及新增测试。测试解释器、命令和原有测试不由模型更改。
6. 根据管理员固定的 `apply_changes`：保存已验证候选，或在所有测试通过且旧项目源码未变化时写回指定本机源码；追加改动记录。不会commit、push、重启、部署或发送额外通知。

测试通过表示测试集通过，不能证明策略收益改善。账户与事件金额沿用原日志口径，尚未核对完整合约费用与保证金账本。

## 必要配套与权限

以下是本次接入所需配套，单独列出，避免被当作用户另行要求的新业务功能：

- 同仓库的独立纯计算包与两个Python入口的本地依赖锁，避免新v1导入旧交易执行框架。
- 独立CLI/Hermes兼容插件与默认禁用的配置；兼容工具不给正式attempt或子Agent开放写入权限。
- 冻结成交证据及限制模型输入量，避免重试读取到另一份事实或输出全部交易日志。
- 文件锁、幂等ID、原文件hash/源码集合检查及失败回滚，防止重复执行、覆盖并发修改与部分写回。
- 独立Cube项目归属、普通用户、冻结测试文件和测试退出码验证，避免混入正式任务的沙箱回收。
- 追加文件记录和中文Markdown记录。兼容操作使用旧项目文件日志，不纳入v1的PG任务队列、lease/fence或outbox；不能宣称拥有同样的数据库事务保证。

写入范围固定为旧项目 `src/trading_system/strategy/*.py` 及必要的新增 `tests/*.py`。
新增测试只能是`test_*.py`，不能新增conftest或替换测试入口。
不能改原有测试、共享量化包、执行模块、风险模块、凭据、部署文件或正式v1源码。
文件替换前再次检查整个冻结源码集合；中断后的未完成写回在下一次兼容迭代前恢复。
Cube异常或测试结果不明时拒绝写回。远程资源设置到期销毁；跨进程中断、管理响应迟到等完整真实演练仍待进行。

## 如何启用（尚未执行）

1. 在仓库根目录与 `trading-agent/` 分别安装依赖，命令见 [同仓库整合说明](../../legacy-trading/docs/v1-integration.md)。不再需要仓库外的v1源码。
2. 复制 `legacy-policy.example.yaml`，填写源码目录（服务器示例 `/srv/open-trade-deployment`）；默认 `enabled=false`、`apply_changes=false`。
3. 准备真实Cube模板，预装旧项目测试依赖及固定测试解释器；沿用v1部署锁和真实隔离报告校验。真实依赖未就绪时拒绝测试。
4. 仅在主控/专门兼容Profile安装可选 `trading-compat`；原正式研究Profile不变。管理员配置三个绝对路径：

```yaml
plugins:
  entries:
    trading-compat:
      settings:
        executable: /srv/open-trade-deployment/trading-agent/.venv/bin/ta
        config_path: /etc/trading-agent/config.yaml
        policy_path: /etc/trading-agent/legacy-policy.yaml
```

专门兼容Profile的工具白名单使用 `trading_compat`（需要正式查询时另加 `trading_bridge`），
不要通过宿主机terminal/file工具绕过兼容入口；使用 `docs/archive/legacy-iteration.md` 的迭代说明。
兼容测试的Cube凭据由受信任工具提供，不传入guest。没有修改用户现有Profile、cron或真实配置。

CLI对应命令：`ta legacy review --policy ... --from ... --to ...`、`ta legacy source --policy ... --path ...`、`ta legacy iterate --policy ... --request -`。

部署时需要同时构建/分发 `trading-quant` 与 `trading-agent` 两个wheel；只复制原来的新v1 wheel不足以安装共享计算依赖。

## 未做与验证边界

- 未连接服务器、切换旧cron、启用兼容Profile或执行真实策略迭代。
- 未完成真实模型/Cube联调、真实guest执行原交易项目测试或连续在线验收。
- 未实现收益实验、审批/生效/回退体系、CSV、模拟合约账本；代码文件恢复不等于收益实验的策略版本回退。
- 测试快照复制Git可见的授权Python源码/测试、已被原测试引用的`replay_advisor_veto.py`、选定非凭据配置及pyproject。任意新增辅助文件尚不自动收集；缺少依赖或辅助文件会测试失败并拒绝写回。
- 新兼容链路以固定成交日志及一次真实本机测试子进程证明实现；Cube测试为传输/SDK边界替身，不能称为已在线接通。
- 新v1原有用量统计测试 `test_native_usage_scope_idempotence_conflicts_and_measurements` 在未修改HEAD也失败：fixture业务时间固定为2026-10-07，而attempt.started_at使用数据库实际时间，导致24小时查询排除样本。本轮未修改原任务时间逻辑或该测试。

本轮测试与构建结果记录于 `legacy-compat-verification.json`，不覆盖原 `verification.json` 的历史证据。
