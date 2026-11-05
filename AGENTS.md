# AGENTS.md

## 项目与角色边界

本地仓库为 `hermes-trade`；部署目录为 `/srv/open-trade-deployment`。以当前实际仓库绝对路径引用文件，
不得把相邻的 Hermes 或 trading-agent 仓库误认为本项目。维护已有未提交改动。

当前目标以 `docs/prediction-driven-trading-goal.md` 为准：BTC、ETH 的结构与趋势预测驱动交易。

- 子 Agent 分周期识别结构、条件情景与目标区间；父 Agent 综合仍有效的预测并发布交易意图。
- 主控 Hermes 通过既有 Hermes Gateway 与用户 TG 交互，使用 trading-bridge 查询状态、持仓和预测并申请解释/复盘；它与独立预测 Hermes 的父 Agent 是不同角色，不因旧流程归档删除主控接入。
- 模型不得调用订单接口、自行改变仓位或账户风险上限；确定性程序复用既有风控和执行。
- 预测复盘 Agent 只提出知识版本和预测分析改进。知识组织为知识点 summary、案例 content、操作 procedure。
- 期限学习由程序按回放、后续影子和回滚规则验证。不得更改已发布预测或历史评分。
- 旧策略代码迭代 Agent、写代码工具和对应经验索引的活跃入口已退役；保留历史证据。
- 结构失效或父 Agent 转向才提出绑定持仓的平仓；单纯到期继续现有退出与结构风险监测。
- 预测有效性、交易收益、模拟结果、真实模型与线上验收分别报告，不混同。

不得在开发或复盘中启用实盘、调用真实订单、修改凭证、提高全局风险限制或删除历史事实。
唯一业务 runtime 位于 `trading-agent/`；共享交易核心为 `packages/trading-core/`，共享量化库为 `packages/trading-quant/`。
保留 TG ⇄ Hermes Gateway ⇄ 主控 Hermes ⇄ 交易 runtime ⇄ 独立预测 Hermes；唯一 runtime 不排斥两个隔离 Hermes Profile，通知复用现有 outbox/HermesSender，不另造网关。
旧 CLI、旧预测研究和策略代码迭代流程位于 `archive/`，不安装、不部署；保留数据库迁移和历史读取。
旧研究测试与旧策略/交易日志文档已按用户 2026-10-22 的后续要求清理；当前测试不得重新依赖归档代码。
正式预测、评分、交易与审计事实仍须保留；旧文件原版本通过 Git 历史追溯。
同一账户只能使用一个交易入口，旧安装与预测 runtime 禁止并行执行。

## Development Change Recording

使用以下流程维护未提交改动、版本记录和提交说明：

- `/srv/open-trade/Feature.md` 只记录当前尚未 commit 的改动，使用中文描述涉及文件、行为变化、验证命令及结果、安全边界和回滚方式；开发过程中及时更新。
- `Feature.md` 是本地工作记录，必须保留在 `.gitignore` 中，不得加入 Git 追踪。
- 每次 commit 前，以实际暂存差异为准，将本次提交对应的 `Feature.md` 内容汇总到 `/srv/open-trade/changelog.md`，记录日期、主要改动、验证结果及已知限制，避免重复归档已提交内容。
- Commit message 必须总结本次 changelog 条目：标题概括主要变化，正文说明关键行为变化、验证结果及重要限制；不得把审计发现写成已经完成的修复。
- 将本批代码、配置、测试、文档、迭代记录与 `changelog.md` 一起提交；不要提交 `Feature.md`、密钥、运行状态或无关排查文件。
- Commit 成功后，从 `Feature.md` 移除本次已提交条目，保留尚未提交的工作；如果没有待提交改动，写明“当前无待提交改动”。Commit 失败时保留原条目。
- Push 失败不撤销成功的 commit，也不将已提交内容重新标为未提交；另行报告待推送的提交。
- `Feature.md`、`changelog.md` 和 commit message 均不得包含密钥、API token、私钥、账户凭证或原始私有交易所响应。
- 开发过程中使用模块化开发思想，如果用户对开发有要求，但是在开发过程中需要开发要求之外的功能，必须在完成之后告知用户。
## 验证与输出

变更运行相关单元测试、真实 PostgreSQL 集成测试和必要回归。外部 I/O 不放进数据库事务。
事实追加保存；任务写入在同一事务检查授权、租约、fence 和期限。
所有自主决定和额外功能写入交付文档，缺失数据不得编造。对用户简短报告并链接完整说明。
