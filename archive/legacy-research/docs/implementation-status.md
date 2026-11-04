> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 实施状态与交付边界

本轮交付：预测、评分、复盘和运维的代码及本地证据。服务器迁移尚未执行。
依据：`specifications/交易Agent-v1-实施设计.md`、`specifications/交易Agent-v1-技术细节.md`。

2026-10-08目录整合：本实现已归入 `hermes-trade/trading-agent/`，指标包同处该仓库，
不再依赖仓库外的交易源码。安装、同步和启用边界见
[同仓库整合说明](../../legacy-trading/docs/v1-integration.md)。本次只调整源码归属和安装路径。
现有实盘 Agent 和有大量未提交改动的相邻 Hermes 仓库未修改。

2026-10-10：可选兼容链路增加历史迭代SQLite/FTS5检索、summary/content按需读取、
冻结清单和可选经验引用；未改正式预测记忆和engine。
细节见 [策略经验召回](strategy-memory.md)。40%去重及自动修订尚未实现。

2026-10-08：按用户确认新增旧项目成交/源码迭代的独立兼容入口；v1指标实现移到
hermes-trade的独立量化模块，原公式、预测/评分/复盘主流程与正式权限保留。
兼容入口默认未启用；尚未迁移服务器或执行真实策略改动。
详见`legacy-compatibility.md`及本轮独立验证记录，不能与旧收益实验/模拟合约账本混为已实现。

2026-10-07用户更新范围：CSV成交导入暂不需要；策略实验及依赖它的审批/生效/回退留到后续。
服务器现有Agent已有按仓位、最大可承受亏损和强平位置计算止盈止损的逻辑，优先复用。
现已取得本地hermes-trade代码并核对成交事件结构；具体公式、费用/保证金规则与模拟账本接口仍待核对，不能标记为已对接。

## 已实现的代码

- 独立 Python3.11 工程、锁定依赖、8 个 Alembic 迁移、单进程 FastAPI runtime。
- 四周期各80根计算数据/30根模型视图、UTC边界、不可变行情与快照、指标质量门禁。
- OKX公共K线、时钟与合约元数据校验；当前OI、已公布资金费及历史实际资金费采集。
- 合约主动买卖量、历史OI、多空账户比的HTTP适配及四周期隔离；固定篮子的成员K线、SMA20和覆盖统计。
- 公开清算频道持续采样、事件去重、心跳覆盖与断连/重启缺口；四周期金额加权汇总和不可变引用。
- 固定时间轴、三份长期趋势缓存→当前attempt的1h分析→正式发布；保存原始概率和引用链。
- 确切1m开盘价定价、±0.5%标签、Brier、追加结算修订；缺价格延迟重试及持续异常提醒。
- 每日重查既有预测的原合约端点，默认覆盖最近90天；发现订正后自动追加结算和复盘修订。
- 数据库实例锁、lease/fence、短事务、持久调度、停机缺失时段、发布后恢复和进程归属回收。
- 同engine/时点案例与经验检索、冻结上下文；日报/解释、数值复核、经验一次修正与假设隔离。
- API角色与任务权限、幂等、统一脱敏错误、请求大小/接收时间限制、暂停/恢复；
  最近24小时的预测/首次结算p50/p95、长期趋势复用和原生父子任务usage统计。
- 三个独立Hermes插件、固定Profile与部署锁生成、模型凭据预检查、Cube管理和终端适配。
- outbox及原生Hermes发送适配、独立doctor、JSON日志/降级指标、Linux服务/定时器模板。
- PostgreSQL备份保留7日/4周，校验与空库还原；wheel打包及包内迁移/提示词/插件读取。

## 阶段状态

| 阶段 | 代码状态 | 未满足的阶段验收 |
|---|---|---|
| M1 数据与契约 | 主链路及可选源适配已实现 | 真实合约/统计时间语义/市场篮子待确定，完整在线采样未验收 |
| M2 预测闭环 | runtime、Runner、缓存、发布和结算已实现 | 真实模型/Cube完整前向预测与故障演练 |
| M3 记忆与主控 | 代码已实现 | 真实主控查询、消息通道与部署隔离 |
| M4 交易与复盘 | 预测复盘已实现；模拟合约账本/风控待实现及对接；CSV暂不做 | 取得服务器现有风控代码与配置，核对9.2规则及账本接口 |
| M5 策略实验 | 用户明确留到后续；收益实验/审批/回退未实现 | 后续另行推进，当前不构成待开发阶段的阻塞项 |
| M6 在线验收 | 部署和运维工具已准备 | Linux实际部署、真实隔离与连续7天/168时段 |

**未实现不等于仅仅未启用。** 合约账本仍需实现及对接；成交导入、策略实验与版本流转留到后续。
`paper.enabled`/`experiment.enabled`不能绕过此限制；runtime拒绝这些尚未实现的模式。
operator接口和CLI返回明确的`CONTRACT_SPEC_PENDING`，不接受旧现货格式冒充合约记录。

## 明确剩余事项

1. 合约参数：instId、永续/交割、线性/反向与结算资产，资金、杠杆、保证金、仓位、费用、
   滑点、资金费、标记价格、维持保证金、强平及止盈止损。
   优先从服务器现有Agent的代码/配置获取和核对，用户已说明其风控逻辑存在；尚未取得源码。
   按实施设计7.3，在9.2确定前没有生成最终合约账本迁移。
   CSV字段/期初持仓格式随导入功能延后，不作为当前阶段的必要输入。
2. 扩展行情：清算持续采样、统计HTTP和篮子采集代码已实现。三类统计时间戳口径在部署
   验证前保持null/MISSING；不能仅凭接口可达选择时间解释。篮子采集代码已实现，成员列表仍为空。
   当前OI不能替代历史边界OI；BTC自身相关性/β/相对强弱为NOT_APPLICABLE。清算没有全量保证；
   缺口、无采样事件、未知面值或查询超限均明确记录，不能据此断言没有爆仓。
3. 历史订正：每日重查默认最近90天、每tick最多24项，可配置回溯范围；不宣称覆盖无限历史。
   只有取得确切原合约端点才追加事实。真实订正发生频率和接口历史范围仍需在线验收。
4. 可观测性：主要阶段安全事件与数据库引用、原生返回的成功/失败子任务耗时/调用量/token usage
   已采集；缺失仍为空，并报告采样覆盖率。进程崩溃或批次尚未返回时的子回合不保证完整采集；
   父模型累计usage最后采样于提交前，不能称为完整最终用量。真实运行分布和盈利指标仍未产生。
5. 部署验收及现有逻辑对接：取得服务器代码后核对风险计算接口与单位，再实现模拟账本所需适配。
   真实Cube模板/网络/凭据/guest管理接口/资源限制、模型联网、通知送达、
   Linux进程和沙箱崩溃回收、服务器备份恢复与7天连续运行均未验收。
   结构化隔离报告的校验只能证明与配置相符，报告内容必须由真实演练产生。
6. 解释/复盘联网当前通过沙箱终端；没有开放宿主机web/browser工具。主控原生查询、Gateway
   消息/Cron冒烟和完整真实AIAgent行为还需在指定部署环境检查。

## 本地验证证据

验证命令：`uv run --extra cube --group hermes-test pytest -q`。
最新回归数量与耗时记录于`verification.json`；源代码质量检查覆盖src/tests/migrations/integrations/deploy。
数据库测试使用真实PostgreSQL16、逐项创建/删除隔离库；进程测试启动真实本机子进程。
Hermes测试从固定提交归档源码加载原生PluginManager和delegate_task，外部模型执行边界为fixture。
Cube生命周期使用HTTP传输替身，取消适配另有真实loopback HTTP超时/归属测试；没有真实guest。
清算采样另有真实本机WebSocket/PG测试，验证订阅、ping/pong、断连和重启；不是交易所在线采样验收。
正式业务示例的行情/模型/通知均为fixture。另有公开接口只读探测记录，证明三个统计端点和
history-candles可返回数据；未选择生产合约，未验证完整时间口径，没有实盘下单或live验收报告。

| 验收号 | 本地证据 | 边界 |
|---|---|---|
| A01 | test_jobs、test_forecasts、test_scheduler | 真实PG并发去重；非真实模型 |
| A02 | test_storage、test_settlement、test_backups | 不可变事实、旧修订保留、还原后触发器 |
| A03 | test_market、test_observations、test_statistics、test_breadth、test_liquidations、test_liquidation_sampler、test_forecasts | 输入/时点/覆盖拒绝；完整真实采集契约待验 |
| A04 | test_forecasts、test_jobs | 截止时间与锁等待；真实模型预算待验 |
| A05 | test_memory、test_forecasts、test_market_observation_history | 时点/engine/冻结复用 |
| A06 | test_agent_runner、test_runtime | 数据库结果优先；真实Hermes中断待演练 |
| A07 | test_jobs、test_runtime、test_orphan_processes、test_cube_lifecycle | PG接管、本机PID归属；真实Cube恢复待验 |
| A08 | test_settlement、test_price_refresh | 预测评分幂等、每日订正发现；合约账本未实现 |
| A09 | test_scoring、test_probability_boundaries | 标签/Brier/精度；合约手算未实现 |
| A10/A11/A12 | 仅权限与禁用入口 | 策略审核/生效/CSV事务业务未实现 |
| A13 | test_outbox、test_doctor | 故障/UNKNOWN/重试；实际消息送达待验 |
| A14 | tests/hermes、test_api | 原生插件加载与任务权限；完整真实Profile/AIAgent待验 |
| A15 | test_sandboxes、test_cube_lifecycle、test_plugins、test_processes | 边界/限额/不回退代码证据；真实guest隔离待验 |
| A16 | test_scheduler、test_runtime | 停机覆盖与已发布结果恢复；真实长时间中断待验 |
| A17 | 无完整业务证据 | 收益实验/前向验证未实现 |
| A18 | test_deployment、test_deployment_identity、test_credentials、test_runtime、test_doctor、test_metrics | 配置/权限/循环/模型失败门禁和实测统计；真实远端依赖待验 |
| A19 | test_market、test_observations | UTC与80/30，6Hutc/12Hutc和confirm |
| A20/A21 | test_forecasts、tests/hermes/test_plugins | 原生固定两阶段、缓存和概率绑定；真实模型调用量待验 |

全部A01–A21未整体通过，M6不能由本地测试替代。实现取舍和额外补充见`decisions.md`。
