> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 提交前验证方法与历史结果

> 最新历史验证摘要见文末2026-11-05条目；下文11月4日结果保留原时点，不代表本次公开候选树已通过。

仓库：`/srv/open-trade`；分支：`dev`。用户要求提交当前代码并推送。本次将2026-10-28上次提交之后的已有工作纳入同一提交，保留各阶段交付和失败证据。本文件中的本轮检查结果与历史真实模型验收分别报告。

## 本批实际范围

- 运行修复：复盘案例索引查询、行情与观测批量保存、共享所有权锁及独立调度串行锁；保留原事务授权、租约、fence与期限。恢复调度可选择保留账户熔断，默认旧接口语义保持；本轮没有实际恢复调度或清除熔断。
- Hermes兼容启动入口修复插件加载时遍历共享模块字典的竞态，入口摘要纳入部署锁；公开强平连接采用443端口并兼容64010通知，BTC/ETH采样和冻结接入。
- 子预测使用30根已收盘＋1根当前未收盘快照；80根已收盘计算确认指标，80＋1计算26项临时指标。父只综合四周期有效子报告，按需回读本次冻结子证据，不额外取父自身1h行情。执行所需ATR由程序取有效1h子证据，缺少时拒绝准备。
- 复盘保存原预测输入及hash，按当时边界重建分钟参考；缺数和事后采集时间明确标记，不改写历史快照、预测或评分。父纯综合输入不补造独立1h快照。
- 外部指标补齐五币篮子、ETH/BTC基准弱证据、当前与历史资金费率时点、公开统计与双币样本强平；质量不足如实降级。
- 默认主动成交来源改为公开历史逐笔的分钟聚合，确定性程序分页、去重、方向/总量校验、预算与退避、历史抽查和补抓；模型只读结果。新增0011迁移、可变采集计划与短期压缩逐笔缓存；永久分钟事实追加保存，仅缓存可DELETE。
- 纳入各阶段技术交付、真实验收原文及失败记录、CVD差异、11月2日离线客服材料导出及11月3日客服确认、启动准备、Notion知识整理交付和TODO。可读性问题记录迁至`docs/codex不说人话记录.md`并追加案例，原版本仍可从Git历史读取。

完整行为、默认选择和回滚限制见[分钟成交聚合](trade-minute-aggregation-2026-11-03.md)、[父综合输入](parent-synthesis-inputs-2026-10-31.md)、[复盘原始证据](review-candle-history-2026-10-30.md)、[外部指标](indicator-completion-2026-10-31.md)及[最新启动准备](paper-startup-readiness-2026-11-03.md)。这些历史文件保留各自时点的状态；其中“未提交”描述当时操作，不代表本次提交后仍未入库。

## 本轮验证方法

新建独立PostgreSQL16集群于`.local/private-evidence`，仅监听回环58924，测试角色为`commit_test`，每个PG用例创建并迁移自己的临时数据库。未复用业务58918、旧恢复库或旧测试集群。隔离实例启动参数设置`jit=off`，避免本机已知LLVM缺失；不改业务或全局PG配置。原Feature副本和全部本轮日志在`.local/private-evidence`，保持Git忽略。

从`/srv/open-trade/trading-agent`执行：

```bash
TA_HERMES_REFERENCE=.local/private-evidence \
  .venv/bin/python -m pytest -q tests/unit tests/hermes --tb=short
PATH=.local/private-evidence \
TA_TEST_DATABASE_URL=postgresql+psycopg://commit_test@127.0.0.1:55432/postgres \
TA_ACCEPTANCE_CONFIG=.local/private-evidence \
  .venv/bin/python -m pytest -q tests/integration --tb=short
```

从仓库根执行共享核心回归：

```bash
trading-agent/.venv/bin/python -m pytest -q tests --tb=short
uv lock --check --offline
uv lock --project trading-agent --check --offline
git diff --cached --check
git check-ignore Feature.md .local
git ls-files Feature.md
```

Ruff在trading-agent目录显式采用`pyproject.toml`，对69个变更Python文件分别执行`ruff check --config pyproject.toml`和`ruff format --check --config pyproject.toml`。两套锁校验均离线执行，不修改依赖。PG受控模拟测试只读取现有配置和风险文件，在隔离库使用PaperTransport与测试预测，不请求真实订单或模型，不写业务账户。

## 本轮结果

| 检查 | 本轮结果 | 本机证据 |
| --- | --- | --- |
| runtime全部单元/固定Hermes契约 | 254 passed，87.22秒，无skip | unit-hermes.log |
| 共享核心 | 68 passed，1.51秒，无skip | shared.log |
| 全部真实PostgreSQL集成 | 192 passed，1087.61秒，无skip | postgres.log |
| 69个变更Python文件Ruff/格式 | 通过 | static.log |
| 两套依赖锁离线检查 | 通过，未修改锁文件 | 本轮命令回执 |
| 文档与暂存检查 | 最终101个本地链接、代码围栏、敏感特征及禁止路径通过 | staged-checks.json |

本轮共**514项通过、0失败、0跳过**，全部来自本次运行；没有用历史测试或复测拼接为首次全通过。PG用例结束后确认`ta_test_*`和`ta_restore_test_*`临时库均为0，已正常停止本次58924实例；数据目录和日志保留，未操作业务实例。汇总回执为`results.json`。

首轮暂存空白检查发现新验收报告末尾多余空行，移除该空行后通过；原报告正文和历史测试结果未改。链接检查识别本地路径后的`:行号`，未将这类有效链接误判为文件缺失。额外长凭据赋值检查仅命中测试占位值`fixture-controller`，没有真实凭据。

实际导入路径确认runtime/core/quant均来自本仓库；固定Hermes仍为`<private-artifact-hash>`，受追踪源码无改动。只读确认`hermes-paper-runtime.service`为inactive/dead；本次未启动它。

## 已知限制与安全边界

本次只做提交准备、隔离测试和Git推送，不部署候选wheel、不迁移业务库、不重装Profile/插件、不启动业务runtime或Gateway，不调用真实模型、订单、TG或Notion写入，不修改凭据和风险，不删除或重写历史事实。先前业务JIT修复、真实模型验收与Notion操作只归档已有交付，不重复执行。

> 私有账户状态、业务事实计数及实际决策记录已隐去。

自主决定：使用全新隔离PG，运行全部当前回归并检查完整暂存清单；只清理新验收报告末尾一处多余空行，未改变其证据正文，不修改生产逻辑以凑测试。敏感特征筛查只记录文件位置和类别，不打印匹配内容；不提交本机脚本、备份、数据库、认证或运行状态。

源码撤回使用反向提交；实际部署回滚须成套核验代码、入口、Profile和锁。已迁移0011的数据库保留事实并向前修复，不删除事实表、不回灌旧备份丢弃后续事实，不启用同账户第二入口。提交成功后清理Feature已归档条目；推送失败保留commit并单独报告。

## 2026-11-05 历史验证摘要（公开技术范围）

框架新增逐笔采集断点恢复、通知分流与摘要、整轮分析预算和资源恢复；0012迁移只扩展可重建的采集工作表，正式事实仍追加保存。

| 历史验证范围 | 结果 |
| --- | --- |
| 共享核心 | 68项通过 |
| 自动同步维护 | 53项通过 |
| runtime单元与固定Hermes契约 | 294项通过 |
| 隔离真实PostgreSQL集成 | 首轮216项通过、2项显式跳过；两项受控paper测试随后用合成策略补跑通过 |

合计633项不同用例通过。受控paper验证不使用业务策略，不代表实际部署策略验收；此历史结果不替代当前公开候选树的重新验证。实际发布验证记录见公开仓changelog。不公开个人安装路径、账户、进程、部署状态或原始业务响应。
