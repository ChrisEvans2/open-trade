# Changelog

## 2026-11-05 添加MIT开源协议

- 新增根目录LICENSE，使用标准MIT协议，版权署名为2026 ChrisEvans2；README与公开范围说明链接同一协议文件。
- 验证：标准协议正文、文档引用、暂存空白与密钥/身份扫描通过；仅修改许可证和文档，未运行无关交易单元或数据库测试。
- 公开同步基线保留许可证；不修改私有源基线、业务代码、部署、凭据或交易事实。回滚使用公开反向提交。


## 自动同步 2026-11-05 10:33:52

- 扩展已审查项目文件与完整技术文档；本轮46个源文件变化，保留公开文档、示例和脱敏适配。
- 验证：共享核心68、单元/固定Hermes294、隔离真实PG79项通过；密钥/身份扫描与暂存检查通过。
- 同步维护测试53项通过。
- 保留完整README、需求/设计/使用说明、测试目录和退役源码；删除个人标识、业务事实快照和私有验收原文，现用配置继续采用暂停示例。
- 不含私人提交祖先、账户记录或部署凭据。未运行真实模型、订单或业务服务；不将本轮定向回归等同收益或外部live验收。
- 如需回滚，反向提交本批公开代码；数据库事实保留并向前修复。


## 2026-11-05 框架更新与文档脱敏合并

- 合并README的功能流程和目录映射，去除个人部署状态，保留长窗口覆盖与源数据方向校验限制；更新采集、通知和通用验收说明。
- 同步分页断点与0012工作表、预算暂停和公平轮转；同步预测通知分流、父摘要和结果未知保护，以及分析轮次预算与资源恢复。正式事实仍追加保存。
- 验证：本条对应的候选树必须通过本次自动同步记录所列单元、固定Hermes、真实隔离PG及密钥/身份扫描后才生成提交；不引用私有部署结果作为公开验收。
- 不公开个人部署、账户、原始预测/通知记录或私有Git历史；示例继续暂停。回滚使用公开反向提交，已升级数据库向前修复并保留正式事实。


## 自动同步 2026-11-04 13:10:44

- 扩展已审查项目文件与完整技术文档；本轮165个源文件变化，保留公开文档、示例和脱敏适配。
- 验证：共享核心68、单元/固定Hermes254、隔离真实PG22项通过；密钥/身份扫描与暂存检查通过。
- 同步维护测试53项通过。
- 保留完整README、需求/设计/使用说明、测试目录和退役源码；删除个人标识、业务事实快照和私有验收原文，现用配置继续采用暂停示例。
- 不含私人提交祖先、账户记录或部署凭据。未运行真实模型、订单或业务服务；不将本轮定向回归等同收益或外部live验收。
- 如需回滚，反向提交本批公开代码；数据库事实保留并向前修复。


## 自动同步 2026-11-04 12:25:20

- 建立独立初始公开版本；本轮0个源文件变化，保留公开文档、示例和脱敏适配。
- 验证：共享核心68、单元/固定Hermes254、隔离真实PG22项通过；密钥/身份扫描与暂存检查通过。
- 不含私人提交祖先、账户记录或部署凭据。未运行真实模型、订单或业务服务；不将本轮定向回归等同收益或外部live验收。
- 如需回滚，反向提交本批公开代码；数据库事实保留并向前修复。


## 2026-11-04

- 建立独立公共交易框架工作区，包含runtime、共享core/quant、迁移、Hermes适配、提示词及合成测试；未携带私人Git历史或业务记录。
- 新写通用示例和架构/部署/测试说明。默认暂停模拟，关闭执行、私有读取与通知；实际配置、凭据和账户留在仓库外。
- 去除外部部署目录自动发现，使用通用客户端标识和paper-only服务模板；受控模拟测试独立生成临时策略。保留原确定性风控、授权/租约/fence、追加事实和模型订单边界。
- 验证：254单元/固定Hermes契约、68共享核心、22独立真实PostgreSQL集成，共344个不同用例通过。最后2项假密钥契约复核通过且不重复计数；runtime155文件Ruff/格式、两套锁离线检查和示例暂停校验通过。完整263文件候选树密钥扫描0命中，个人标识复核通过。测试库已回收，独立实例已停止；详细范围见docs/initial-validation.md。

限制：未重跑全部PG或外部live测试，未运行模型、业务服务或真实订单；不将测试等同收益或部署验收。同分钟目标/失效评分歧义和分钟方向/覆盖限制保留。未指定许可证，本次形成独立初始公开提交。

回滚：移除此独立准备工作区即可撤销本次导出，不影响其他部署或事实。后续若迁移业务库，保留历史并采用向前修复，禁止降级删除事实。

## 原项目开发记录（脱敏历史参考）

> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 原项目历史

## 2026-11-04

### 补齐预测证据输入与主动成交分钟采集（汇总10月28日至11月4日工作）

- 修复复盘案例查询、行情/观测逐条往返及调度排他锁造成的运行问题，采用作用域索引查询、保持身份锁的批量追加和独立调度事务锁；授权、lease、fence、期限及原数据库超时上限保持。恢复调度新增可选保留账户熔断参数，默认兼容原接口；不清除既有风险事实。
- 新增预测Hermes兼容启动入口，对插件模块清理使用字典快照，入口摘要纳入部署锁和发布包；公开强平默认443并兼容64010通知，扩展双币样本采集与冻结接入。固定Hermes源码、既有Gateway和主控bridge接入保持。
- 子预测提供30根已收盘＋1根未收盘快照，80根确认指标和80＋1计算的26项临时指标分开保存。父改为只综合四周期有效子报告，按需读取原冻结证据，不额外采父1h行情；父结构/边界来源、1h期限和执行ATR依赖同步校验，缺少必要风险来源仍拒绝准备。
- 复盘增加原预测完整输入/hash、按预测时点分钟重建与授权案例展开；原快照、事后重建和当前背景分开，缺数不补造，历史预测与评分不改写。补齐固定五币篮子、ETH/BTC弱证据、资金费率当前/历史时点和公开统计质量降级，保留源端缺失与数学退化处理。
- 默认CVD/主动买卖比改用官方公开历史逐笔的分钟聚合。确定性后台程序负责分页去重、单位/方向/总量校验、限额退避、轮转抽查与补抓；新增0011迁移、采集计划、24小时压缩逐笔缓存与分钟查询索引，分钟事实长期追加，只有缓存可DELETE。默认11天初始化、30天修复、每轮40秒/300逻辑GET，预算不足明确缺失，不自动回退已确认有问题的旧统计源。
- 同步目标、规格、配置、六角色提示词、部署说明及回归；纳入阶段真实验收与原文、指标/CVD调查、离线客服材料导出及后续客服确认、Notion知识整理交付、最新模拟启动准备和TODO。可读性记录迁至 `docs/codex不说人话记录.md` 并追加案例；旧版本通过Git历史保留。本次仅提交已有工作及必要验证记录，不重复执行历史部署、模型或外部知识写入。

验证（本次提交前）：runtime全部单元和固定Hermes契约 **254 passed**（87.22秒）；共享核心 **68 passed**（1.51秒）。独立真实PostgreSQL16全部集成 **192 passed**（1087.61秒）；本轮共 **514项通过、0失败、0跳过**，临时测试/恢复库0个，测试实例已正常停止。69个变更Python文件Ruff及格式检查、两套依赖锁离线校验通过；全部暂存文档101个本地链接有效，敏感特征/禁止路径筛查无命中，修正新报告末尾一处空行后暂存空白检查通过。`Feature.md`和`.local/`继续忽略，不提交凭据、运行状态、备份或无关脚本。完整命令、范围、结果及限制见 `docs/local-commit-verification-2026-11-04.md`，历史测试和真实模型数据不计为本轮新运行。

> 私有账户状态、业务事实计数及实际决策记录已隐去。

自主决定与回滚：使用全新回环58924隔离PG执行当前回归，保留日志和原Feature副本；仅清理一处新文档末尾空行，不改变既有证据或生产逻辑。完成后停止测试实例、确认临时库回收，提交源码/配置/测试/文档与本条记录并推送dev。源码反向提交；已迁移0011的数据库保留事实并向前修复，不能删除事实表或回灌旧备份丢弃新事实，部署须恢复配套代码/入口/Profile/锁，禁止同账户第二入口。

## 2026-10-28

### 整理预测工作流技术路线及模拟部署交付记录

- 改写 `docs/prediction-workflow-technical-route.md`，先说明业务行为，再解释父子协调、预测复用、重试、期限及名额回收；保留原数值、技术约束、复核命令与历史验收结果，明确实现记录和文字修订的时间边界。
- 新增 `docs/ai-writing-readability-issues.md`，保存跨项目文本可读性反馈、父子协调说明的原文与用户改写，以及后续案例记录格式；未将模型或运行框架归因写成已证实结论。
- 新增 `docs/paper-startup-readiness-2026-10-27.md`、`docs/paper-database-upgrade-2026-10-27.md` 和 `docs/deployment-artifact-sync-2026-10-28.md`，归档启动核对、既有模拟库 0009→0010 升级及四项参数更新、研究/Cube 插件与部署锁同步的交付证据。后续更新单独标注，保留早期阻塞和失败快照；本次提交仅包含文档，不提交本机配置、备份、插件安装副本或运行状态。

验证（本次提交前）：5 份文档标题、代码围栏及 44 个本地文件链接通过检查；技术路线原数值集合完整保留、历史 bash 命令逐字一致、11 个主章节完整。`git diff --check`、暂存差异空白及敏感特征/禁止路径筛查通过；`Feature.md` 仍忽略且未追踪。本次为纯文档提交，不重新运行单元或 PostgreSQL 测试。核对既有日志及回执：数据库升级阶段单元 **15 passed**、隔离真实 PG **9 passed**；部署同步阶段单元与原生契约 **33 passed**、隔离真实 PG **2 passed**，均为此前操作结果，不计为本次新运行。

已知限制：文档记录的数据库和文件部署检查通过不代表新流程已上线；交易 runtime 在上次交付时仍停止，真实模型、双沙箱/最多八子负载、取消恢复、模拟闭环及持续运行仍待验收。本次不启动服务、不重复执行升级或同步脚本，不调用模型、订单或 TG，不修改凭据、风险或业务事实；不将既有审计缺口写成已修复。

自主决定与回滚：按实际暂存的 5 份文档汇总记录，提交时纳入本 changelog；提交成功后清理本地 Feature 对应条目并推送当前 dev 分支。撤销本提交仅反向恢复文档，不回滚此前模拟库与部署操作；其恢复边界按各交付说明处理，保留 0010 和历史事实。

## 2026-10-27

### 持久化预测轮次、原生四子并行与有界恢复（汇总 10 月 22–26 日未提交工作）

- 新增 `0010_prediction_workflows` 迁移与 analysis scopes/rounds/runs/events、两个持久模型名额；按币种使用固定 30 秒事件归并窗，同一父会话运行四周期原生子分析，复用有效子结果并单独计算父综合时间。配置采用 120 秒实际分析、150 秒阶段、1200 秒轮次和 210 秒新输入预算；角色正常最多 3 次尝试，error 按 60/180/600 秒最多 3 次恢复，计数跨任务/重启保留，不能重置上限。
- 新增内容提交与有类型引用，由确定性程序绑定正式事实 ID；重试前、父综合前及发布事务内核验当前依赖、分钟数据和持仓。错误停用当前父预测与新开仓依据，既有持仓退出监测继续运行。取消不确认时保留资源名额并回收，公共原生生命周期、兄弟命令取消隔离和子目录边界保持可核验；历史预测/评分不重写。
- 补齐可信提交拒绝审计、安全字段反馈和冻结精确引用；冻结输入过期时改用新任务/新输入，减少分钟缺口和监测游标造成的误刷新。发布事实优先于进程退出及失败收尾；修复几何校验的领域错误反馈、短进程已退出竞态，并保留有界恢复和全部失败事实。
- 停止自动上下文刷新逐任务通知，历史同类队列终止投递但不删除；过期发送租约转 UNKNOWN 后不盲目重发，明确失败仍有限重试。UNKNOWN 可能未送达，不承诺必达或恰好一次，交易/真实故障通知仍复用 outbox/HermesSender。
- 支持模型端点与推理强度、独立预测 TG 开关和聊天/CLI 工具隔离，继续复用唯一 Gateway；认证检查使用独立预算、缓存和后台刷新。纳入 Cube 本机边界、guest 令牌隔离、paper-only 服务守卫、内存参数及显式选择运行的隔离/容量探针，不增加模型订单权限或改变账户风险上限。
- 归档本批 Profile、CLI、TG/代理登录恢复、既有模拟部署闭环、重复通知修复、重试和 Cube 内存/合成容量的交付说明，以及讨论定稿与最新技术路线。历史线上操作按原日期保留，不将旧工作流真实模拟闭环或合成容量等同最新轮次工作流已上线。本次按实际暂存差异整理本地提交与验证记录，Feature 继续忽略，不 push。

验证（本次提交前复核）：runtime 全部单元和固定 Hermes 契约 **165 passed**（67.56 秒）；隔离真实 PostgreSQL16 八组定向回归 **58 passed**（363.44 秒），本轮共 **223 项测试通过**；临时测试/恢复库全部回收，测试集群正常停止。runtime `ruff check/format src tests integrations deploy` 通过（130 个文件），两套锁离线校验、边界脚本语法、内存参数 JSON 和暂存空白检查通过，敏感特征与禁止路径筛查无命中。实际命令及本轮结果见 `docs/local-commit-verification-2026-10-27.md`，历史失败与复测证据保留在各交付文件，不把重复执行或 collect-only 当作本次通过。

已知限制：本次没有重跑全部集成、真实 Cube 隔离/容量或真实模型/TG/收益验收；原生契约加载真实固定 Hermes，但模型/API/沙箱与消息传输为测试替身。最新代码要求 0010；业务库迁移、新表权限、匹配安装/Profile/锁、真实双模型八子负载及持续运行均需后续验收。本轮没有迁移业务库、部署/重启业务服务、调用真实模型/订单、发送 TG、修改凭据/风险或删除历史事实。

回滚：源码反向恢复本批差异；若业务库已迁移或新流程已运行，先暂停新分析并保留全部事实，采用匹配 0010 的修复版本向前恢复，不执行删除历史的 downgrade、不回灌旧备份丢弃新事实、不启用同账户第二入口。此前本机操作分别按各交付说明回退。

## 2026-10-22

### 收敛唯一预测交易运行时、共享交易核心并恢复 Hermes 主控桥接

- 根目录改为开发工作区，抽取 `packages/trading-core/` 共享风控、账户记账、退出和执行代码，`trading-agent/` 直接依赖该核心与既有 `trading-quant`；旧 CLI、研究/策略迭代流程、提示词和服务模板移至不安装部署的 `archive/`。清理旧研究测试及过时策略/交易日志文档，当前测试使用共享核心或独立事实夹具，保留九份数据库迁移、正式历史事实与只读查询。
- 接入 Cube 沙箱运行期间的有界续期，核验资源归属、任务授权、fence 与期限，失败终止并回收；补齐现有 API/模板、文件桥和资源检查兼容，新增明确选择运行的真实组件探针。预测期限和风险上限不变。
- 增加显式宿主凭据文件支持的 GET 白名单只读行情，以及模拟准备阶段快照采集脚本；备份恢复在外部 pg_restore 前结束数据库事务，同时保留 session advisory lock。模拟数据库、身份、备份和部署清理的操作仅纳入交付说明，不提交凭据、数据库或运行状态。
- 保留主控 Hermes 与既有 TG/Gateway 接入边界，恢复状态/持仓查询、受限解释/复盘任务及 v1 兼容申请入口，内部使用当前结构 runtime；通知复用 outbox/HermesSender。主控与独立预测父 Agent 分离，模型不获得订单接口，实验仍受现有门禁限制。
- 同步根目录和 runtime 的配置、锁文件、README、AGENTS、规格、回归测试及本批交付说明，纳入模块协作 HTML。补齐被忽略的本地 Feature 记录，并仅清理归档源码末尾空行；本次只生成本地 commit，不 push、部署或重启业务服务。

验证（本次提交前复核）：根目录测试 **68 passed**（2.06 秒）；runtime 全部单元及固定 Hermes 插件契约 **132 passed**（47.76 秒）；runtime Ruff、两份锁文件离线校验和暂存差异空白检查通过，暂存新增/修改内容敏感特征与禁止路径检查无命中。隔离真实 PostgreSQL16 六组定向回归 **28 passed**（309.65 秒），共 **228 项测试通过**，临时业务测试库均回收且测试集群正常停止。完整命令、首轮测试环境连接失败及证据见 `docs/local-commit-verification-2026-10-22.md`。

已知限制：扩展到交易核心、根测试及行情脚本的 Ruff 检查有 74 条格式/导入/异常链提示未处理，不宣称全仓库静态检查通过。本次没有重跑全部集成或真实 Cube/模型/TG 联调。历史凭据/runtime 时间预算失败、高频调度锁竞争、Cube 完整隔离验收、主控资料迁移与最终部署锁仍按交付记录保留，不宣称修复或上线；模型、行情、订单和消息替身不代表真实收益或成交。未调用真实订单、启用实盘、修改现有凭据、提高全局风险上限或删除历史事实。

回滚：反向提交本批源码和依赖接入，保留数据库历史事实与既有本地操作证据；此前模拟准备操作按各交付说明回滚。交易服务切换另行验收，禁止同账户双入口或重发未知订单。

## 2026-10-15

### 集成预测驱动交易、共享量化模块与知识复盘（汇总 10 月 8–11 日未提交改动）

- 将完整 `trading-agent/` 源码、配置、Hermes 插件、数据库迁移、测试和规格纳入同一仓库；新增独立 `packages/trading-quant/` 作为 v1 量化指标与结构计算的共享来源，根框架和新框架通过仓库内路径依赖使用，各自保留环境与锁文件。
- 新增 BTC/ETH 的 1h/4h/6h/12h 独立结构预测、父 Agent 综合、分钟事件验证和单 runtime 调度；预测输入每周期 30 根、合计 120 根，复盘每周期 80 根、合计 320 根，冻结行情来源并拒绝不完整窗口。预测目标与订单止盈保持各自用途。
- 抽取共用风险参数并新增 `PredictionRiskAdapter`，复用原风险限制、账户与退出计算；持久化交易意图、稳定订单 ID、未知结果查询、账户串行、持仓版本绑定、部分成交及费用增量去重。开仓到期仅撤未成交余量，已成交持仓继续退出与结构监测；执行默认关闭。
- 增加 PostgreSQL 追加事实、知识版本与案例关系，SQLite/FTS5 摘要检索、正文分页和冻结引用；知识复盘支持新增、修订、合并与退役，期限学习按 40 个回放、20 个后续影子样本和 20 个生效后复查样本验证与回滚。任务写入检查授权、租约、fence 和期限，外部 I/O 位于事务外。
- 保留迁入的成交复盘、历史策略迭代实现与经验索引证据；预测方案退役旧策略代码迭代及旧经验检索的活跃 CLI/插件入口，复盘职责转为预测与知识改进。同步 `AGENTS.md`、部署说明、需求对照、技术决策、实现和验证文档，并按原字节归档同步前的两份规格。
- 本次依据 220 个实际暂存源码、配置、测试与文档文件汇总上述改动，并将本条记录一并提交；`Feature.md`、本地环境与数据库、运行数据、`.DS_Store` 及无关排查材料不提交。没有部署、启用实盘、修改凭证或风险上限，也没有调用真实订单接口。

验证（2026-10-15）：根目录 `.venv/bin/python -m pytest -q` 为 **207 passed**；`trading-agent/` 中使用隔离的本地 PostgreSQL 55432 执行 `TA_TEST_DATABASE_URL=postgresql+psycopg://example-operator@127.0.0.1:55432/postgres .venv/bin/python -m pytest -q`，结果为 **244 passed**（94.34 秒，无跳过）。在 `trading-agent/` 执行的 `.venv/bin/ruff check src tests integrations`、两套 `uv lock --check --offline` 和暂存差异空白检查通过。新增文件的密钥特征筛查仅命中 `.env.example` 的示例密码占位符。

已知限制：模型和订单响应均为 fixture；真实模型预测价值、真实 Cube 隔离、持续在线行情、真实交易所成交/保护单和切换流程仍待验收，不据此宣称收益或线上可用。早期记录中的阶段性失败以本次完整回归结果为准；既有执行审计发现不得因新增框架测试通过而视为全部修复。完整边界见 `docs/prediction-driven-validation.md`。

回滚：反向提交本批代码与依赖接入，保持执行关闭；已迁移数据库保留历史事实并采用向前迁移。运行后若存在持仓或未决订单，须由原入口处理完成后再切换，禁止双入口或盲目重发未知订单。

## 2026-10-07

### AI 开仓门禁、输出诊断与离线审计（汇总 9 月未提交改动）

- 9 月 7 日：将 `market_advisor.failure_policy` 设置为 `block_new_entries`；Meta 或任一配置周期失败、缺失时阻止新开仓，潜在开仓绕过 Advisor 限频以获取新结果。平仓绕过 AI 门禁，已触发的退出不等待模型调用。
- 9 月 24 日：约束 Meta 的 `max_leverage_override` 只能为 null 或 >=1，保留非法输出校验；风控报告区分“代码无信号”和“技术信号被 AI 否决”。补充相关回归测试。
- 新增只读 AI 否决回放脚本、测试及报告：从决策完成后的下一根完整小时 K 线开始，排除缺失窗口，标注同根 K 线止盈止损先后不明；17 个事件不足以支持放宽入场门槛。
- 9 月 29 日：新增执行契约离线审计脚本及报告，使用内存模拟交易所并禁止 HTTP 调用；复现成交确认、保护失败重试、平仓数量/记账、持仓对账及候选安全检查缺陷。**这些执行缺陷仍待修复，本次提交不包含对应生产修复。**
- 纳入 9 月 7 日、24 日策略迭代记录；这些生产改动已有历史部署记录。本次提交整理未重启服务、未直接下单、未提高风险限制。

### 开发记录与提交说明规则

- 更新 `AGENTS.md`：`Feature.md` 仅记录尚未提交的改动并保持 Git 忽略；提交前按实际暂存差异汇总到 `changelog.md`，再总结为 commit message 的标题和正文。
- 提交成功后清理 Feature 中已提交条目，保留剩余工作；提交失败保留条目，推送失败单独报告待推送状态。
- 排除本地 `check_okx_pos*.py` 排查文件、运行数据和凭证。

验证（2026-10-07）：`python -m pytest -q tests`，`201 passed`；`git diff --check` 通过。回滚使用反向提交，部署回滚单独核对；审计通过不代表实盘执行缺陷已解决。

## 2026-08-24

### 开发记录流程与策略审计补充

- 新增本地开发记录约定：提交前先在 `/srv/open-trade-deployment/Feature.md` 记录当前批次改动，再将摘要整理进 `changelog.md`；`Feature.md` 已加入 `.gitignore`，不参与 git 追踪。
- 更新 `/srv/open-trade-deployment/AGENTS.md`，明确 `Feature.md` 的用途、禁止提交规则、敏感信息边界，以及 commit 前同步 `changelog.md` 的流程。
- 调整 market advisor pre-entry 限频优先级：开仓前 `pre_entry_signal` 使用独立同币种额度，避免被普通 hold 结构审查消耗；同时补充低质量 `BREAKOUT_DOWN/UP -> TREND` 首根贴边追单拦截。
- 平仓事件新增不含杠杆的 MFE/MAE 审计字段，基于持仓期间 high/low watermark 记录最大有利/不利波动，便于后续评估移动止盈、止损和出场质量。
- 保留社媒 AI 停用配置：`runtime.social_ai_enabled=false` 时跳过社媒抓取、过滤和 AI 分析，避免继续消耗 Vertex/Gemini 额度。

验证：`pytest -q` 通过，结果为 `185 passed`。

## 2026-07-31

### 停用社媒 AI 与 Vertex 额度止血

- 停止并禁用 `litellm-vertex.service`，避免本机继续通过 LiteLLM 调用 Google Vertex/Gemini。
- 新增 `runtime.social_ai_enabled` 配置开关，并在 `/srv/open-trade-deployment/config/app.yaml` 中设为 `false`。
- 交易周期在 `social_ai_enabled=false` 时跳过社媒抓取、过滤和 AI 分析，直接产出中性的 `no_recent_data` 社媒分析结果；消息面不会再创建、拦截或降仓开仓信号。
- 保留原有审计结构，`social_raw` 与 `social_filtered` 写空列表，方便后续恢复或复盘时对比。

验证：`pytest -q /srv/open-trade-deployment/tests/test_live_trading.py /srv/open-trade-deployment/tests/test_event_alpha_combiner.py /srv/open-trade-deployment/tests/test_ai_schema.py` 通过，结果为 `49 passed`；`pytest -q /srv/open-trade-deployment/tests` 通过，结果为 `184 passed`。

## 2026-07-06

### 趋势单右侧确认门槛

- 修复 `min_bars_before_entry` 只作用于区间均值回归、普通趋势单未等待结构稳定的问题；ADAUSDT 这类 `TRANSITION -> TREND_UP` 第一根直接开多的场景现在会被 `regime_not_stable` 拦截。
- 新增 `trend_min_bars_before_entry: 2`，趋势单等待门槛短于区间均值回归的 `min_bars_before_entry: 3`，避免趋势行情走得快时过度滞后。
- 该门槛仅作用于从 `TRANSITION`、`EXPANDING_RANGE`、`SHOCK` 等不稳定结构刚切换到趋势的普通技术单；已经处于趋势中的延续突破不因这条规则直接被否决。

验证：`pytest /srv/open-trade-deployment/tests -q` 通过，结果为 `170 passed`。

## 2026-07-03

### 社媒事件单止盈收敛与平仓委托清理

- 将纯社媒/事件创建的小仓位信号固定止盈从偏趋势的 `2R` 收敛为 `1.2R`；普通技术趋势单的 `2R` 止盈逻辑不变，避免把趋势策略整体短线化。
- 实盘平仓后新增 OKX 策略委托清理：当本地 live close 成功，或 reconcile 发现仓位已被交易所原生止盈/止损/移动止盈关闭时，会按合约撤销残留的 `oco`、`conditional` 和 `move_order_stop` 策略委托。
- OKX 客户端新增 `/api/v5/trade/cancel-algos` 封装，用于清理 `orders-algo-pending` 中的残留保护委托，减少 OKX 委托列表混乱和后续复盘噪音。

验证：`pytest /srv/open-trade-deployment/tests -q` 通过，结果为 `162 passed`。

## 2026-06-30

### 宽幅震荡与过渡行情开仓拦截

- 根据 2026-06-29 亏损复盘方案，强化普通技术开仓权限门：`transition`、`shock`、`choppy_high_vol` 以及 `EXPANDING_RANGE`、`RANGE_HIGH_VOL`、`WATERFALL` 等危险细分状态下不再允许普通技术开仓或区间均值回归。
- 区间识别改为同时输出 raw 区间边界和 0.90/0.10 分位数有效边界，区间策略使用有效边界，风险识别保留 raw 高低点，降低单根插针对 range_position、止损和中线止盈的污染。
- 新增 `range_quality_score`、`range_width_expansion`、连续刷新高/低点计数、`risk_flags`、`block_reasons` 和状态持续 bar 数；`RANGE_WIDE` 必须满足更高置信度、区间质量和冷却 bar 才允许均值回归。
- 上沿连续刷新高点时禁止直接开空，下沿连续刷新低点时禁止直接开多；拒绝开仓会在 `invalid_reason` 中记录明确原因，方便复盘。
- 区间均值回归单新增轻量保护：记录策略类型和区间中线；本地风险管理在到达中线时触发区间止盈，浮盈超过 0.5R 后收紧本地止损到接近保本。
- 实盘/模拟交易事件新增策略类型、行情状态、风险标记、区间质量和区间边界字段，后续无需翻完整周期目录也能判断开仓依据。
- 本次未修改杠杆选择、杠杆上下限、单笔仓位上限、API 密钥或实盘开关。

验证：`pytest /srv/open-trade-deployment/tests -q` 通过，结果为 `158 passed`。

### 防御性社媒空单确认门槛

- 修复社媒防御性 short 过早触发的问题：`BREAKOUT_DOWN` 不再天然允许社媒 short 将技术 `hold` 改写为开空，必须同时出现 `fresh_lower_break` 或 `recent_low_break_count >= 2`；`WATERFALL` 仍作为急跌保护场景允许。
- 单根冲击跌破近端有效下沿但仍未确认跌破更大箱体时，会保持 `hold` 并记录 `social_alpha_short_blocked_unconfirmed_breakdown`，方便复盘区分“近端支撑下探”和“确认破位”。

验证：`pytest /srv/open-trade-deployment/tests -q` 通过，结果为 `159 passed`。

## 2026-06-29

### 社媒定投/长期配置消息过滤

- 将定投、DCA、长期持有、长期配置、长期收益、退休账户/储蓄型买入等慢变量叙事加入社媒 alpha 硬过滤：没有明确短线催化时，只能作为低相关背景或 ignored_events，不得触发 long/short 入场。
- 在社媒 AI prompt 之外新增 analyzer 保险丝：即使模型把定投/长期配置消息误判为 `long`，进入策略合成器前也会强制降为 `alpha_signal=hold`、`alpha_confidence=0`、`event_decay_minutes=0`。
- 无 LLM 启发式路径同步过滤这类消息，避免 fallback 路径和模型路径结论不一致。
- 补充回归测试覆盖“定投 BTC/长期收益”启发式过滤，以及模型误判 `long` 后被压回 `hold`。

## 2026-06-20

### 实盘执行、交易所保护委托与出场规则

- 实盘交易事件和 Telegram 报告新增开仓来源上下文：持久化并展示 `technical/social/mixed` 来源、技术信号、社媒信号、社媒影响和入场依据。当前持仓的来源摘要也写入 live account state，确保后续由 OKX 原生止盈/止损触发的 exchange reconcile 平仓消息仍能说明当初为何开仓。
- 实盘平仓收益改为优先读取 OKX `/api/v5/account/positions-history` 的历史仓位口径：使用 `realizedPnl` 作为本仓真实净收益，并在 Telegram 中拆分展示 `pnl`、`fee`、`fundingFee`。累计已实现回填也优先匹配 OKX 历史仓位，避免本地按成交价/fills 重算时漏掉开仓手续费或资金费。
- 修复 OKX REST 读接口瞬时 403 造成误硬停的抗抖能力：OKX 客户端默认不继承环境代理变量，GET 类行情/账户读取遇到 `403/408/425/429/5xx` 或网络读错误时短重试；POST 下单不重试，避免重复委托。错误诊断新增 `content-type`、`cf-ray` 和响应体预览，便于区分 OKX API 错误与边缘/WAF 拒绝。
- 修复 `/resume ALL` 仅更新运行时状态、未同步更新内存风控限额的问题；daemon 现在每次热加载 `runtime_state.yaml` 时都会重建当前 mode/execution mode 对应的风险限制，避免界面显示 normal 但实际仍沿用 emergency 的 `allow_new_entries: false`。
- 修复 OKX 下单失败诊断缺失：保留交易所原始错误响应及单笔 `sCode/sMsg`，并在 Telegram 周期报告的 `Execution=False` 时展示实际执行错误，避免只显示策略理由而掩盖交易所拒单详情。
- 新增防御性社媒短空通道：仅当社媒分析正常、短空置信度/来源权重/时效性均达阈值，且细粒度价格结构确认 `BREAKOUT_DOWN` 或 `WATERFALL` 时，允许技术 `hold` 下以 2% 保证金上限建立防御性空单；仍受止损、杠杆、最大持仓和同向集中度限制。未确认下破的社媒短空继续拒绝。
- 修复 `ai_failure_policy` 只写在配置和文案、未实际生效的问题。默认改为 `technical_reduced_size`：AI 解析/模型错误保留 `analysis_status` 与错误明细、禁用消息面 alpha，但技术单只降至 50% 仓位而不再硬阻断；保守的 `block_new_entries` 策略仍可显式配置。
- 修复区间均值回归误入场：当细粒度市场结构已识别为 `BREAKOUT_UP`、`BREAKOUT_DOWN`、`WATERFALL` 或 `EXPANDING_RANGE` 时，即使粗粒度分类仍暂时显示 `range`，也禁止在区间边缘逆势均值回归开仓。
- 实盘交易池从仅 BTC/ETH 扩展为主流币动态池：BTC、ETH、SOL、DOGE、OKB 为固定核心；每小时从严格白名单候选中依据 24h 流动性、盘口深度、点差、20 根 1 小时成交量稳定性、ATR 波动和数据完整性综合评分补足至最多 8 个标的。24h 成交额只占 40%，meme 币及未列入白名单的合约不会进入实盘池。
- 实盘交易池增加四层币种分类：BTC、ETH 为核心市场锚；SOL、BNB、XRP、ADA、AVAX、LINK、LTC、BCH 为一级主流；OKB、TON、DOT、SUI、NEAR、ATOM、UNI、AAVE、ETC 为二级主流；DOGE 和典型 meme 币进入排除池。`live_universe.json` 现在记录入选币的语义化 tier，并且排除池不会因成交额高而入选。
- 新增 `state/live_universe.json`，记录当轮入选标的和各候选指标/得分，便于复盘为何某币被纳入或排除；当前持仓始终继续盯市与风控，不受后续排名变化影响。
- 实盘累计已实现收益改为从 OKX `fills-history` 回填：仅匹配本框架 `lv` client order id 发起且已完全平仓的仓位，按实际 `fillPnl`、开仓手续费和平仓手续费计算；交易所原生 OCO/追踪止盈生成的订单会在其平掉框架仓位时一并纳入，不混入手工或其他策略仓位，也不把当前未平仓的开仓费用计为已实现收益。
- 实盘账本改为以 OKX 成交回报为准：新开仓写入实际 `avgPx`、`accFillSz`、合约面值和开仓手续费；原生止盈止损或手动平仓导致仓位消失时，reconcile 从 OKX fills 读取实际平仓价、`fillPnl` 与开平仓费用，避免用本地 K 线收盘价或最后 mark price 估算已实现收益。
- `live_trade_events.jsonl` 的实盘平仓事件新增交易所毛收益、合计手续费和净收益字段；`live_account.json` 持久化合约张数、合约面值和开仓手续费，未实现收益也按实际合约数量计算。
- 增加实盘成交账务回归测试，并完成全量验证：`pytest -q /srv/open-trade-deployment/tests`，结果为 `130 passed`。
- 将 systemd 实盘服务统一为 `trade-live-okx.service`；旧 `trade-paper-okx.service` 已停用，避免运行模式与服务名称不一致。
- 调度器每个策略/盯市周期都会重新加载 `config/runtime_state.yaml`，使 `!resume ALL`、暂停和 emergency 状态即时生效，不再依赖重启 daemon。
- 实盘开仓订单现在附带 OKX mark-price OCO 固定止损/止盈，并新增 OKX 原生 `move_order_stop` 追踪止盈；交易所触发后，本地 reconcile 会同步已关闭仓位，不再误判为状态不一致。
- 修复 OKB 急波动复盘暴露的实盘退出单基准价问题：开仓后固定 TP/SL 和原生移动止盈改为先读取 OKX 实际成交均价 `avgPx`，再按成交价提交独立 OCO 与 `move_order_stop`，避免用信号 K 线 close 计算退出参数导致移动止盈提前激活或保护价格错位。
- 增加社媒 long 的下跌结构保护：技术 `hold` 时，若细分行情仍为 `WATERFALL`、`BREAKOUT_DOWN` 或低效率下跌冲击，direct-asset 利好不再单独创建开多信号，避免 OKB 这类消息利好在瀑布结构中逆势开多。
- 修复社媒多空覆盖技术 `hold` 的不对称问题：技术 `hold` 且行情处于 `range`、`transition`、`squeeze`、低效率 `shock` 或明确下跌结构时，社媒 long 现在会像社媒 short 一样被阻断并记录 `social_alpha_long_blocked_low_efficiency_non_uptrend`，避免 BTC 宽幅震荡中社媒利多单独开多。
- 当前已有 ETH 实盘仓已补齐交易所 `reduceOnly` 保护委托：固定止盈、固定止损和 1R 激活的追踪止盈。
- 5 分钟任务保留盯市、日志和账户对账；live 模式不再依赖本地轮询主动执行固定止损、固定止盈或追踪止盈。
- 趋势单移动止盈改为至少达到 `1R` 才启动；回撤幅度继续按 ATR 价格单位计算，杠杆不再影响移动止盈的价格阈值。
- 区间均值回归单改用区间边界加 `0.5 ATR` buffer 计算止损、以区间中线计算止盈，并关闭追踪止盈。
- AI 事件单止损改为固定阈值与 ATR 倍数取较大值，并设置止损上限；新增 `max_notional_pct: 0.60` 名义仓位硬上限。

验证：`pytest /srv/open-trade-deployment/tests` 通过，结果为 `128 passed`。

### Hermes main agent Vertex routing

- Corrected the default Hermes Telegram main agent from `openai-codex/gpt-5.5` to the local LiteLLM OpenAI-compatible endpoint at `http://127.0.0.1:4000/v1`.
- Set the main agent model to `vertex-gemini-3.1-pro-preview` and added a matching Hermes custom-provider entry with a 256k context length.
- Backed up the prior default Hermes configuration at `/home/operator/.hermes/config.yaml.bak.20260620_vertex_main`.
- Verified both direct LiteLLM and `hermes -z` requests return `VERTEX_OK` through Vertex Gemini 3.1 Pro Preview.
- Restarted the default Hermes gateway; both default and trade-review gateways remain running.

## 2026-06-19

### 小账户风险预算与保证金下限调整

- 将单笔最低保证金从 `1.0 USDT` 提高到 `2.0 USDT`，用于减少 `1.x USDT` 级别的碎单和低影响交易。
- 将单笔最大风险预算从账户权益的 `0.6%` 提高到 `1.0%`，让有效信号在 100U 级别账户中有更可观察的收益/亏损表现。
- 保持单笔保证金上限 `10%` 不变。
- 保持动态杠杆范围 `3x - 20x` 不变，没有继续提高杠杆上限。
- 补充风控测试，验证 `2 USDT` 最低保证金与 `1%` 风险预算下的动态仓位计算仍可正常通过。
- 已重启 `trade-paper-okx.service` 让新风控参数生效。

验证：`pytest -q /srv/open-trade-deployment/tests/test_risk.py /srv/open-trade-deployment/tests/test_paper_risk.py /srv/open-trade-deployment/tests/test_scheduler_equity.py` 通过，结果为 `28 passed`。

## 2026-06-05

### Trade review agent Telegram profile

- Added `/srv/open-trade-deployment/docs/trade_review_strategy_iteration_agent_prompt.md` as the daily paper-trading review and strategy-iteration prompt.
- Created a dedicated Hermes `trade-review` profile for the strategy review Telegram bot, with `terminal.cwd` fixed to `/srv/open-trade-deployment`.
- Moved the daily `trade-review-strategy-iteration` cron job to the `trade-review` profile and paused the duplicate default-profile job.
- Updated the new bot channel prompt so strategy/code/config changes are proposed for human execution instead of direct agent execution.
- Added `docs/server_migration.md` so the framework can be moved to a new server without pushing secrets or runtime state.

## 2026-06-08

### Trade review Codex auth recovery

- Repaired the dedicated Hermes `trade-review` profile by syncing valid `openai-codex` runtime provider credentials into `/home/operator/.hermes/profiles/trade-review/auth.json`.
- Backed up the previous profile auth file at `/home/operator/.hermes/profiles/trade-review/auth.json.bak.20260608_105037`.
- Verified `hermes --profile trade-review auth status openai-codex` reports logged in.
- Verified `hermes --profile trade-review -z '只回复 OK'` successfully returns `OK`.
- Installed and started `hermes-gateway-trade-review.service` as a user systemd service with linger enabled so the review bot and cron scheduler survive logout.

Validation: `hermes gateway list` shows both `default` and `trade-review` running.

## 2026-06-09

### Regime-aware strategy routing and social audit

- Added a lightweight `MarketRegimeClassifier` for finer crypto market states: trend, range, squeeze, transition, shock, choppy high-volatility, and local patterns such as relief rally, pullback, V reversal, inverted V, false breakout, and range edge.
- Updated `FreqtradeStyleStrategy` to attach market regime diagnostics to every `StrategySignal`.
- Added regime-aware routing so trend entries are blocked during range, squeeze, shock, choppy, transition, and rebound-conflict states.
- Added conservative range-edge mean-reversion entries with smaller position sizing for confirmed range regimes.
- Added `SocialContributionAudit` to record whether social/event alpha changed, created, aligned with, or degraded a strategy decision.
- Allowed high-confidence geopolitical or macro social alpha to affect strategy only when the analyzer provides a market transmission path.
- Added regime and social-contribution details to cycle reports and strategy signal JSON output.

Validation: `pytest -q` passed with `58 passed`.

### Automatic strategy iteration mode

- Updated `/srv/open-trade-deployment/docs/trade_review_strategy_iteration_agent_prompt.md` so the `trade-review` agent can automatically apply low-risk paper-strategy iterations without waiting for human review.
- Updated `/srv/open-trade-deployment/AGENTS.md` with automatic iteration authority, safety boundaries, validation requirements, and record-writing rules.
- Updated `/home/operator/.hermes/profiles/trade-review/SOUL.md` and `profile.yaml` so the dedicated Hermes profile identity matches automatic strategy iteration mode.
- Updated the `trade-review-strategy-iteration` cron prompt in `/home/operator/.hermes/profiles/trade-review/cron/jobs.json`; backed up the old job file at `/home/operator/.hermes/profiles/trade-review/cron/jobs.json.bak.20260609_auto_iteration`.
- Added `/srv/open-trade-deployment/Strategy-iteration/README.md` for iteration record naming and content requirements.

Validation: scanned the active prompt files and cron job prompt for stale human-review-only instructions.

### X API social source scaffold

- Added `XApiSource` for official X API v2 Bearer Token access.
- Supports configured watchlist usernames via user timeline reads.
- Supports recent-search query templates with `{asset}` and `{symbol}` interpolation.
- Parses X posts into `SocialItem` with source account, timestamp, text, post URL, engagement, and source trust.
- Added disabled `X Crypto Watchlist` config block in `config/social_sources.yaml`.
- Added `X_BEARER_TOKEN=` to `.env.example`.
- Kept X disabled by default to avoid accidental paid/rate-limited reads.
- Added regression coverage for X post parsing and query rendering.

Validation: `pytest -q` passed with `55 passed`.

## 2026-06-11

### Social AI failure semantics and macro short alpha

- Added `analysis_status` and `analysis_error` to social AI analysis output so schema/model failures are auditable instead of being confused with real social risk.
- Changed social AI fallback from fake `event_risk=high` to `event_risk=medium` with `should_block_trade=true`, preserving the current conservative `block_new_entries` behavior while making clear that the block is caused by analysis failure.
- Strengthened the social analyzer prompt so regional hot conflicts, war escalation, energy/CPI shocks, BTC/ETH ETF outflows, and macro-driven whale de-risking are treated as strong market-relevant inputs instead of unrelated political news.
- Updated the no-LLM heuristic path to classify geopolitical/energy/CPI/ETF-outflow combinations as medium macro risk with potential `short` event alpha rather than low-relevance political noise.
- Added regression coverage for system-failure fallback semantics and geopolitical macro short-alpha classification.

Validation: `pytest -q` passed with `62 passed`.

## 2026-05-27

### Social feed relevance and diagnostics

- Tightened `SocialFilter` so assetless news must mention the traded asset/symbol or clear crypto/market-risk keywords before reaching social AI analysis.
- Prevented unrelated BlockBeats Pro items such as AI tooling, ordinary tech equities, or broad non-crypto news from being applied to every symbol.
- Made social source fetch failures and missing BlockBeats API keys visible in service stderr/journald.
- Made social AI output normalization more tolerant of common model shape issues, including string confidence, string position multiplier, malformed key events, and overlong reasons.
- Added explicit social AI failure details to fallback reasons instead of only reporting `ValueError`.
- Restarted `trade-paper-okx.service` so the daemon uses the updated social filtering and diagnostics.

Validation: `pytest` passed with `34 passed`.

### Telegram position-change notifications

- Fixed the trading daemon Telegram reporter to use Hermes' `TELEGRAM_HOME_CHANNEL` when `TELEGRAM_CHAT_ID` is not set.
- Added explicit stderr logging for Telegram HTTP/network send failures so missed push notifications are visible in `journalctl`.
- Restarted `trade-paper-okx.service` so paper open/close position events use the fixed reporter.

Validation: `pytest tests/test_telegram_commands.py tests/test_scheduler_equity.py` passed with `10 passed`.

## 2026-05-25

### StrategySignal risk protocol

- Added `MAX_SINGLE_TRADE_MARGIN_PCT = 0.10` to make the per-trade capital allocation cap explicit.
- Constrained `StrategySignal.suggested_position_pct` to at most 10% of account capital/margin.
- Added optional technical risk fields to `StrategySignal`:
  - `stop_loss_pct`
  - `take_profit_pct`
  - `risk_reward_ratio`
- Updated `FreqtradeStyleStrategy` to:
  - clamp suggested capital allocation to 10%;
  - generate ATR-based technical stop loss;
  - set take profit to `2R` by default.
- Updated `TrendStrategy` to clamp suggested capital allocation to 10%.
- Added regression assertions covering the 10% cap and new risk fields.

Validation: `pytest -q` passed with `12 passed`.

## 2026-05-26

### Paper position visibility

- Enhanced `/positions` output to show simulated position principal amount alongside the margin percentage.
- Added notional position amount alongside notional percentage.
- Added unrealized PnL amount alongside unrealized PnL percentage for each open paper position.
- Added total unrealized PnL amount and realized PnL amount to the position summary.
- Persisted paper position amount fields in `state/paper_account.json`:
  - `open_position_principal`
  - `open_position_notional`
  - `open_position_unrealized_pnl_amount`
  - `total_unrealized_pnl_amount`
  - `realized_pnl_amount`
- Added regression coverage for the enriched `/positions` display.

Validation: `pytest -q` passed with `13 passed`.

### Social source quality

- Disabled BlockBeats free RSS entries because the configured endpoints currently return empty `data` arrays.
- Documented that BlockBeats should be re-enabled through the Pro API path with `BLOCKBEATS_API_KEY`.

Validation: `pytest -q` passed with `13 passed`.

## 2026-05-27

### BlockBeats Pro API source

- Added `BlockBeatsProSource` for the official BlockBeats Pro API.
- Enabled `BlockBeats Pro Newsflash` in `config/social_sources.yaml` using `/v1/newsflash`.
- Supports both `BLOCKBEATS_API_KEY` and the currently configured fallback `BLOCKBEAT_API_KEY`.
- Parses BlockBeats Pro API rows into `SocialItem` objects with stripped HTML content, source trust, timestamp, and link.
- Verified live Pro API access with the configured key without printing the key.
- Added regression coverage for BlockBeats Pro response parsing.
- Added `/social` and `/news` commands to read recent deduplicated social/news captures from `logs/*/*/social_raw.jsonl`.
- Restarted `trade-paper-okx.service` so the daemon loads the BlockBeats Pro source and the API key from `.env`.

Validation: `pytest -q` passed with `30 passed`.

## 2026-05-29

### Social AI review sample

- Created `docs/social_ai_review_2026-05-29.md` with 100 latest social/news AI analysis samples.
- Each sample pairs filtered message inputs with the AI agent's risk, sentiment, block decision, confidence, key events, and reason.
- Added an initial manual review section covering granularity, confidence, key event extraction, and source quality.
- Added Jin10 non-official access research covering flash API, RSSHub, Apify, and recommended integration shape.

## 2026-05-30

### Social AI output granularity

- Extended `AIAnalysisResult` with relevance fields:
  - `market_relevance`
  - `relevance_score`
  - `macro_assessment`
  - `ignored_events`
- Updated the social analyzer prompt to distinguish direct asset news, crypto sector news, macro market news, political/geopolitical news, and unrelated items.
- Added explicit political/geopolitical handling: political news should not become high risk unless it has a clear market transmission path.
- Normalized malformed model output for relevance fields and ignored events.
- Updated heuristic fallback so unrelated political news is marked as low relevance, not as a blocking event.
- Added regression tests for relevance normalization and political-news handling.

Validation: `pytest -q` passed with `44 passed`.

### Event alpha path

- Added `docs/agent_architecture_options.md` documenting TradingAgents as a backup architecture and the current PolyClaw-inspired path.
- Extended social AI output with event alpha fields:
  - `alpha_signal`
  - `alpha_confidence`
  - `alpha_horizon`
  - `alpha_reason`
  - `event_type`
  - `event_decay_minutes`
  - `source_weight`
  - `freshness_score`
  - `evidence`
- Updated the social analyzer prompt to separate risk filtering from event alpha generation.
- Added normalization for event alpha fields in `SocialAIAnalyzer`.
- Added heuristic source-weight and freshness scoring plus event type classification.
- Added a scheduler combiner that can turn high-quality event alpha into a small `StrategySignal` only when the technical strategy is neutral.
- Preserved risk engine veto as the final gate.
- Added regression tests for alpha normalization and hard-filtered event signal composition.

Validation: `pytest -q` passed with `48 passed`.

### Dynamic leverage sizing

- Updated `RiskEngine` to size new paper entries from technical stop distance and single-trade risk budget instead of always using fixed `max_leverage`.
- Enforced the 10% per-trade margin cap inside the risk engine with `MAX_SINGLE_TRADE_MARGIN_PCT`.
- Dynamic sizing now calculates:
  - risk budget from `max_single_trade_loss_pct`;
  - technical stop from `StrategySignal.stop_loss_pct` with config fallback;
  - target notional from `risk_budget_pct / stop_loss_pct`;
  - margin and leverage under max margin and max leverage constraints.
- Updated paper-mode config to allow up to `20x` so simulated trades can show dynamic leverage; default/non-paper max leverage remains `2x`.
- Aligned configured `max_position_pct` values with the 10% single-trade margin cap.
- Added regression coverage for narrow-stop higher leverage, wide-stop lower leverage, and the 10% margin cap.

Validation: `pytest -q` passed with `16 passed`.

### Account equity synchronization

- Changed the default paper account equity from `10000` to `100` USDT.
- Migrated the current `state/paper_account.json` to `equity: 100.0` and recalculated principal, notional, unrealized PnL amount, and realized PnL amount.
- Enabled OKX private read in `config/exchange.yaml`.
- Added OKX `totalEq` extraction and scheduler equity synchronization before each trading cycle when private read is enabled.
- Added regression coverage for OKX equity parsing and position amount recalculation after equity sync.

Validation: `pytest -q` passed with `18 passed`.

### OKX funding rate sync

- Added OKX public funding-rate fetch via `/api/v5/public/funding-rate`.
- Added perpetual swap symbol mapping, e.g. `BTCUSDT` to `BTC-USDT-SWAP`.
- Extended `MarketSnapshot` with:
  - `funding_rate`
  - `next_funding_rate`
  - `funding_time`
  - `funding_inst_id`
- OKX market snapshots now include funding data when available, and these fields are written into each cycle's `market.json`.
- Added funding rate display to Markdown/Telegram cycle reports.
- Added regression coverage for swap instrument mapping and funding-rate parsing.

Validation: `pytest -q` passed with `19 passed`.

### Minimum paper margin

- Added `min_position_margin: 5.0` USDT to risk config.
- Enforced the minimum margin in `RiskEngine`; new trades below the 5 USDT minimum are skipped instead of opening dust-sized positions.
- Kept the existing 10% maximum margin cap, so with a roughly 100 USDT account new paper entries must fit between 5 and about 10 USDT margin.
- Re-synced `state/paper_account.json` to OKX reported `totalEq=99.899` and recalculated paper position amount fields.
- Added regression coverage for skipping below-minimum entries and ensuring opened entries meet the 5 USDT minimum.

Validation: `pytest -q` passed with `21 passed`.

### Paper account funding metadata

- Added per-position funding metadata to `state/paper_account.json`:
  - `open_position_funding_rate`
  - `open_position_next_funding_rate`
  - `open_position_funding_time`
  - `open_position_funding_inst_id`
- Scheduler now refreshes funding metadata for open paper positions whenever that symbol is marked to market.
- Stopped and restarted `trade-paper-okx.service` so the daemon runs the updated code.
- Migrated the current paper account state to include OKX-reported equity and funding metadata for open positions.

Validation: `pytest -q` passed with `22 passed`.

### Paper account local timestamps

- Added `last_mark_update_local` and `last_mark_update_timezone` to `state/paper_account.json`.
- `/positions` now displays the local Asia/Shanghai mark-to-market update time instead of raw UTC.
- Migrated the current paper account state so agents can read both UTC and local update timestamps.

Validation: `pytest -q` passed with `23 passed`.

## 2026-05-27

### Five-minute market sync

- Added OKX ticker fetch via `/api/v5/market/ticker` for current mark price updates.
- Added lightweight `TradingScheduler.sync_market_state()` to refresh open paper positions without running social analysis, AI analysis, or strategy entry logic.
- The lightweight sync updates mark price, unrealized PnL, funding metadata, account equity, and paper close decisions.
- Updated daemon scheduling:
  - `--mark-interval-seconds 300` for five-minute market sync;
  - `--interval-seconds 3600` for full 1h strategy cycles.
- Updated both the deploy template and active systemd service file, then restarted `trade-paper-okx.service`.
- Added regression coverage for mark-price sync.

Validation: `pytest -q` passed with `25 passed`.

### Paper trade audit log

- Added append-only simulated trading audit events at `logs/paper_trade_events.jsonl`.
- The audit log records paper open, close, and five-minute mark update events with:
  - event source (`strategy_cycle` vs `mark_sync`);
  - symbol, timeframe, side, reason, entry price, fill/mark price;
  - margin percentage and USDT amount;
  - leverage and notional amount;
  - position PnL percentage, account PnL percentage, and USDT PnL amount;
  - realized and unrealized account totals after the event;
  - funding-rate metadata when available.
- Kept 5-minute sync as market-state and risk refresh only; it does not generate new 1h/2h strategy entry signals.
- Added regression coverage for strategy-cycle open audit events and mark-sync close audit events.

Validation: `pytest -q` passed with `27 passed`.

### Paper trade Telegram details

- Added direct Telegram notifications for simulated open and close events from the trading daemon.
- Event messages now include symbol, direction, margin amount and percentage, leverage, notional amount, entry price, close/mark price, PnL amount, PnL percentage, funding rate, event source, and reason.
- Kept five-minute `mark_update` events in the audit log but suppressed direct Telegram pushes for routine mark updates to avoid noise.
- Updated and paused the old Hermes `monitor-paper-positions` cron job so it no longer sends low-detail `open_positions` diff summaries now that the trading daemon sends direct event notifications.
- Added regression coverage for paper trade event Telegram rendering.

Validation: `pytest -q` passed with `28 passed`.

## 2026-05-29

### Strategy exit and concentration controls

- Added `todo.md` with the five-step strategy optimization roadmap.
- Added per-position paper fields:
  - `open_position_stop_loss_pct`
  - `open_position_take_profit_pct`
- New simulated entries now persist the strategy signal's ATR-derived stop loss and take profit on the position.
- Paper close logic now prefers each position's own strategy stop/take thresholds and converts price-distance thresholds into leveraged position-PnL thresholds.
- Fixed trailing take-profit so it only activates after a meaningful profit threshold instead of closing on any tiny positive PnL after a pullback.
- Added same-direction concentration limits:
  - `max_same_direction_positions: 2`
  - `max_same_direction_margin_pct: 0.20`
- Paper trade Telegram/audit events now include strategy stop-loss and take-profit percentages when available.
- Added regression coverage for per-position exits, delayed trailing take-profit, persisted strategy stop/take fields, and same-direction concentration blocks.

Validation: `pytest -q` passed with `42 passed`.

## 2026-05-30

### Market entry-quality filters

- Tightened the Freqtrade-style 1h market strategy with configurable entry-quality filters:
  - ADX minimum trend strength;
  - EMA fast-line slope;
  - minimum EMA fast/slow separation;
  - maximum price extension from the fast EMA measured in ATR.
- The strategy now avoids opening positions during weak drift and avoids chasing a sudden move after price has already stretched too far from trend.
- Added focused regression coverage for valid long and short trends, weak-trend rejection, and overextended-move rejection.
- Recorded higher-timeframe 2h and BTC/ETH regime confirmation as the next strategy-layer optimization.

Validation: `pytest -q` passed with `51 passed`.

### Social AI single-object array compatibility

- Fixed repeated conservative trade blocks caused by some model responses returning a valid social-analysis object wrapped in a single-element JSON array.
- The analyzer now unwraps only the safe `[{"...": "..."}]` shape and continues to reject ambiguous arrays.
- Tightened the social analyzer prompt to require a JSON object at the root.

Validation: `pytest -q` passed with `53 passed`.
