> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 当前版本的模拟交易启动准备

核查从2026-11-03晚开始，延续至11-04凌晨（北京时间）。仓库为 `/srv/open-trade`。本文记录当前工作区，包括此前未提交的改动；没有将本轮准备包当作已安装版本。

> 私有账户状态、业务事实计数及实际决策记录已隐去。

## 实际运行状态

> 私有账户状态、业务事实计数及实际决策记录已隐去。

Cube管理端只读探针通过：带认证可读取health/sandboxes，无认证返回401，当前业务项目沙箱0；没有创建/销毁沙箱。备份timer仍启用，最近任务成功。主控Hermes、预测父Agent、单一交易runtime的角色边界保持；没有恢复归档的代码迭代或旧交易入口。

## 本轮已经完成的准备

### 配置与可审阅部署产物

产物放在忽略目录 `.local/private-evidence`：runtime、trading-core、trading-quant共3个wheel，以及Profile、插件、兼容启动入口和新部署锁。115个包内Python模块编译通过，runtime源码、六份提示词、插件、启动入口和0011迁移与工作区原文件一致；临时组装Profile和主控bridge后，`DeploymentCheck.files()`通过。`require-paper.py`确认使用PaperTransport，退役实验入口关闭。另外从wheel解包目录启动独立Python进程，确认三个包确实从wheel加载；包内提示词/插件/部署锁一致，包内Alembic head为0011，执行传输仍为paper。以上都是候选产物，尚未安装。

候选配置在同目录上一级 `config.candidate.yaml`。它基于实际业务配置，仅显式合并此前已经确认但尚未部署的配置：

- 分钟逐笔来源、11天初始化/30天修复/24小时缓存，以及既有40秒/300逻辑请求预算。
- BTC、ETH、SOL、XRP、DOGE五币市场宽度篮子与当前特征契约版本。
- `taker_timestamp`、`oi_history_timestamp`、`long_short_timestamp`采用已经交叉核对的open口径；旧实际配置仍为null，原统计采集会跳过。核查依据及限制见[指标补全交付](indicator-completion-2026-10-31.md)。主动成交当前默认已使用分钟聚合，不自动回退已确认有问题的Rubik接口。

未改模型、Profile身份、通知目标、API端口、账户、风控文件和凭据。候选配置本地校验 `configuration_complete=true`，这不证明模型服务可达或业务服务已经ready。

### 真实备份、空库恢复与迁移演练

从业务库用原备份身份生成约49MiB的新custom-format备份并校验SHA256。备份前后全部56张表的计数及逐行内容摘要一致。只在58923隔离实例中新建演练数据库，恢复完整非空业务事实，随后通过原有 `ta migrate` 迁至0011，再执行原权限配置工具。

结果为56张表增至58张，除Alembic版本外所有旧表计数及内容摘要保持一致；受限应用角色检查通过。仅 `trade_raw_cache` 可DELETE；行情、预测、评分及审计事实均无UPDATE/DELETE/TRUNCATE权限。没有迁移业务58918数据库，未改既有角色凭据。

### 测试与公开数据预检

公开GET预检在隔离恢复库、受限应用角色和实例所有权下执行两个真实采集轮次：分别238/234次逻辑GET，完整校验9/8个分钟，共BTC9、ETH8；每轮各1项在预算截止时明确缺失。两轮墙钟41.23/40.34秒，包括数据库计划与收尾，网络预算仍为每轮40秒。网络调用时没有额外业务数据库连接占用；未请求旧Rubik主动成交或订单接口。

随后BTC/ETH四周期共8组均取得80根连续已收盘K线及1根当前快照，末30根展示、四个必要指标均通过。本轮总489次逻辑GET（包括447次历史逐笔分页），仅证明抽查分钟及取数链，不证明10天连续覆盖、模型实际采用这些数据或预测效果。真实模型未调用。

本轮执行254项单元/固定Hermes契约（84.26秒）、68项共享核心（1.38秒），均通过。完整真实PG首轮为182 passed / 10 failed（1038.27秒，无skip），失败归因为：备份命令缺PG16 PATH 1项；114＋20分钟窗口旧断言1项；父任务缺子报告的通知夹具3项；缺python_executable的启动器夹具5项。运行中已逐组修正并专项通过，完整首轮仍使用收集时加载的旧夹具代码，失败记录不删除。

最终10项按最终文件重新执行：**10 passed，47.86秒**。合并覆盖为**514项不同用例全部取得通过结果：254单元/固定Hermes＋68共享核心＋192真实PG**；不是声称首轮无失败。先前专项通过的启动器6项、批量冻结4项、通知11项、备份1项均与192项集合重叠，不重复累计。

主要可复跑命令（从 `/srv/open-trade/trading-agent` 执行；测试连接必须预先设置为隔离实例，禁止使用业务58918）：

```bash
TA_HERMES_REFERENCE=.local/private-evidence .venv/bin/python -m pytest -q tests/unit tests/hermes --tb=short
PATH=.local/private-evidence TA_ACCEPTANCE_CONFIG=.local/private-evidence .venv/bin/python -m pytest -q tests/integration --tb=short
```

共享核心命令从仓库根执行：`trading-agent/.venv/bin/python -m pytest -q tests --tb=short`。69个变更Python文件的Ruff/格式、全仓 `git diff --check`、本说明本地链接、Feature忽略状态及systemd模板检查通过。

两币的受控模拟开仓→成交→绑定平仓→去重记账也已通过：测试读取既有实际risk/strategy/runtime_state，执行传输为PaperTransport，使用隔离临时库和测试预测。每币结束时持仓CLOSED、OPEN_LONG/CLOSE各一笔。原业务库没有新增意图或成交，不能把它写成真实模型自主开仓样本。

本轮测试仅修正夹具及格式，没有改动生产源码；重要行为断言没有删除或放宽。原备份用例首跑因命令环境缺PG16工具失败，修正PATH后通过；不是业务备份功能缺陷。静态检查须在trading-agent目录采用本项目配置，避免从仓库根调用时错误继承全局规则或推断导入分组。

## 尚需完成的部署与验收

> 私有账户状态、业务事实计数及实际决策记录已隐去。

可先进入受熔断保护的预测/采集前向验收，再按已确认的会话与风控规则开始模拟成交；不强迫模型入场，也不把HOLD视为交易链失败。完整手续费、滑点、资金费和成交延迟仍非基础PaperTransport的完整模拟范围，收益需单独报告。

## 自主决定、保护与回滚

本轮自主增加：完整回归而非只重跑上一批专项；新业务备份及逐表摘要对照；隔离恢复/0011迁移/最小权限演练；生成匹配当前工作区的3个wheel和临时Profile文件门禁；公开GET采集预检。隔离测试均不连接真实订单接口，不执行正式业务runtime，不调用真实模型或发送消息。

发现启动器测试夹具仍只设置旧 `hermes.executable`，新启动入口已经改用 `python_executable`；仅修正 `tests/integration/test_prediction_runner.py` 中的替身配置，恢复超时、续租、清理、去重及拒收原因检查，不削弱生产绝对路径门禁。`tests/integration/test_outbox.py`的三个父任务夹具缺有效子报告，在通知断言前即被MISSING_CONTEXT拒绝；只为父用例补充正式发布的测试子报告，保留错误留痕、刷新去重和终端错误通知断言。`tests/integration/test_market_batch_locks.py`旧断言只计114条外部观测，默认分钟来源现在另冻结20条明确缺失的窗口，实际134是正确行为；更新总数并额外核对原114条源观测全部保存，保留原续租与完成时限。`tests/hermes/round_probe.py`只修正一处长行格式。修改前副本在本轮证据目录，其他未提交改动保留。

收尾复核业务全部56张表的计数及内容摘要与最初快照相同，20个凭据/风险/Profile保护文件摘要保持一致；既有Gateway、业务PG和只读行情服务继续active，交易服务inactive。业务配置、插件、锁、数据库迁移版本及历史熔断均未切换；不涉及生产回滚。撤销本轮仅需撤上述夹具/格式变更及本文、本轮Feature条目，保留原有未提交工作。备份和隔离演练事实作为证据保存；不执行0011删表降级，不删正式事实。不commit/push，Feature.md继续忽略，changelog未改。

本轮结束时临时 `ta_test_*` / `ta_restore_test_*` 库为0，58923隔离实例已停止；非空恢复演练库、公开分钟样本和备份保留，没有删除历史证据。业务58918和既有Gateway保持运行。

详细本机证据目录为 `.local/private-evidence`，包含受限配置和数据库备份，不加入Git或对外复制。重要结论和剩余事项均已写入本文。
