> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 开发决策、差异与补充

每项分别记录文档依据、实施选择、影响和验证。未验证的事项不得标为完成。

## D001：工程参数固化

技术细节 1.5 提供建议公式，但窗口/初始化/零分母尚未锁定。采用版本
`ohlcv_wilder_v1`：Wilder14、回归/量能20、总体标准差 ddof=0，RSI 全平取50；
核心必需项为 Return1、Return20、ATR%、RSI，其余特征可选并携带质量原因。
选择是可复现的工程起点，不宣称最优市场参数。所有参数进入 engine hash。
计算仅使用固定 Python 3.11 的 math/statistics，金额与数量始终 Decimal。

## D002：配置缺失门禁

用户尚未确定真实合约及模拟账户。代码可使用明确标记的 fixture 测试，生产配置保留 null，
不以示例 BTC-USDT-SWAP 或默认杠杆代替用户选择。收益实验依赖模拟规则完整。

## D003：本地与部署证据

宿主 macOS 本地测试用隔离 PostgreSQL；业务生产仍是一个 runtime 进程与本机标准
PostgreSQL 服务。服务器迁移、真实 Cube 隔离和7天在线验收留到部署阶段。
参考 Hermes HEAD 为文档 commit，但该工作目录存在大量已有改动；不修改它。
插件契约以锁定提交的源码为依据，避免把用户未提交改动当作标准接口。

## D004：补齐运维边界

文档2.9要求实施前确定的值：SQL语句5秒、行锁等待2秒；循环3倍周期无心跳视为不健康；
TERM宽限3秒后KILL；UNKNOWN与明确发送失败共用稳定 event_id、最多5次发送，
退避60/300/900/900秒；日志队列1000、每卷10MB、最多10卷并保留30天。
这些值将通过故障测试，必要调整保留记录。没有引入新服务或消息渠道。

## D005：锁定Hermes的实际委派行为

锁定提交的 `tools/delegate_tool.py` 明确忽略调用者的 max_iterations；
迭代上限必须写入 trading Profile 的 delegation.max_iterations，而不是只传函数参数。
该版本原生 schema 修复会另起一次子任务回合，且 schema_valid=false 仍可能返回 completed。
因此不传原生output_schema：适配层严格解析和校验子报告，只允许已保存子结果后的父级修正一次。
父级初次90秒/总计120秒，child≤4、depth≤1，固定3份长期趋势并行后运行1h。
这是对原生委派的适配取舍；原生PluginManager/delegate_task契约测试已通过，真实模型预算待验。

## D006：防止持锁连接丢失后旧连接池继续写入

专用advisory-lock连接之外，增加runtime_control中的generation与持锁backend PID。
每次业务写入在同一事务检查当前generation、实际pg_locks及任务fence。
控制操作先取排他锁，避免从共享锁升级造成并发tick死锁。
真实PostgreSQL连接终止与旧池存活测试见 test_jobs.py。

## D007：可追溯引用与精度

新增snapshot_bars、bundle_trends、forecast_trends、job_inputs等关系表，
让JSON引用同时受外键保护；attempt绑定bundle。正式预测同时保留原请求和实际1h概率，
分别存hash，numeric(12,9)只是查询投影，评分读取原始概率。
源行情超过numeric(28,12)精度时拒绝，防止数据库静默舍入后快照失真。

## D008：显式短事务

采用SQLAlchemy2 Core表和AsyncConnection，而非ORM实体/AsyncSession。
这是锁、事务归属和追加事实更清晰的实现选择，未改变数据库或对外领域接口。
并发任务各用独立连接，外部HTTP/模型/消息等待不占业务事务。

## D009：固定指标与测试时钟

Wilder14从第一组完整变化量初始化，使用80根有限窗口；初始化口径进入特征版本。
趋势报告固定为UP/RANGE/DOWN与0～1强度，不含预测概率。状态分类规则未确定，
历史检索不伪造regime匹配。small_sample参考线设为30，只提示样本量，不用于批准策略。
测试时钟只允许构造Database时显式注入；生产不接受环境变量或API修改时间。

## D010：部署起点与行情修订

增加持久forecast_cursor，从首次启动当前UTC小时计算覆盖率；之后停机遗漏的小时也记录。
历史补记按168小时一批处理，避免长事务；不会补造模型预测。
端点严格使用对应已收盘1m开盘价，缺失按60/300/900/3600秒退避，之后每小时重试。
已入库的1m价格更正由调度器触发追加结算revision，复盘修订按持久计数合并且不丢失晚到事件；
每日重拉默认最近90天预测端点以发现订正，范围和预算见D019；不宣称无限历史全量扫描。

## D011：原生插件、稳定偏好与记忆边界

不改Hermes核心：主控bridge、交易research和Cube provider三个插件分别安装。
固定Profile工具白名单和delegation上限；稳定偏好按实际原生路径放在memories/USER.md、
memories/MEMORY.md并纳入部署hash，保留state.db/FTS5。
用subagent_start/pre-tool hook绑定child角色、时点与输入；禁止子任务发布、递归或越权查询。
报告、会话ID与业务结果可审计；原生返回的成功/失败回合计量见D017，崩溃前尚未返回的回合仍有缺口。

## D012：Cube不是只填backend名称

核对SDK0.7.0源码后，采用有超时的HTTP管理面和原生数据面SDK；取消前先验证metadata归属。
数据面URL为HTTP且未覆盖管理面的TLS设置，因此限制代理为127.0.0.1，远程须受保护隧道。
显式普通用户、空环境、只读input及可写scratch；禁用IPv6，正式deny-all，复盘拒绝私网、
元数据和配置敏感公网地址。网络策略的allow优先规则意味着模板也必须检查，不能仅追加deny。
创建后核对模板cpuCount/memoryMB/diskSizeMB，不把模板容量假装成动态可修改资源。
准备限时20秒并受T+2开始门禁；真实服务超时、SDK取消、模板隔离和容量均仍须线上演练。
配置、模板、SDK与真实隔离报告hash共同决定readyz；本地替身不能充当live报告。

## D013：扩展行情保留发布时间和来源差异

资金费的fundingTime可能是未来结算时间，不能冒充当前数值的发布时间；历史结算采用实际
realizedRate。当前OI与历史边界OI分开，API不提供所需口径时保留MISSING。
源记录同时保留event time和本机获取/可用时间；快照只选decision之前可用的最新修订，
长期周期可用40天内、每来源/种类最多100条旧记录，避免用未来订正污染旧决策。
主动买卖量、历史OI、多空比、清算和篮子标准化计算器已实现；三类统计及篮子HTTP、
清算持续WebSocket采样已接入。统计时间契约保持待验证，见D018，未从不同单位拼凑值。

## D014：补齐凭据、可观测性和备份边界

新增模型凭据预检查：固定Hermes Python/仓库，用原生Profile凭据解析器，去掉ambient key，
只返回是否配置，缓存30秒；不调用模型、不回显密钥，不能据此宣称服务可达。
runtime禁止人工原始token及迁移/备份/恢复URL；API验证身份后限制1MiB请求与5秒接收预算。
脱敏JSON日志保留稳定错误码/函数栈，队列丢弃和磁盘失败影响readyz；doctor独立于业务进程/DB。
Linux systemd提供runtime、每5分钟doctor与每日UTC备份模板，未安装到任何服务器。
PostgreSQL16 custom archive备份采用独立凭据、0600 passfile、7日/4周保留及SHA256；
恢复只允许显式空目标库、无runtime锁、单事务，不创建/删除库或自动启动，角色授权另行配置。
真实PG备份还原保留原始事实及多份结算修订，触发器仍拒绝事实改写。

## D015：概率误差与数据库投影精度冲突

文档允许原始概率和误差≤1e-6。numeric(12,9)逐项投影最多增加3×0.5e-9误差，
原本合法的原始值可能被投影sum约束拒绝；查询投影边界因此为1e-6+1.5e-9。
原始合同仍为1e-6，不归一化或改动原始概率，评分始终使用原始值。
另一个规格内部边界冲突是原始(1,1e-6,0)且实际DOWN的Brier=2.000000000001：
容差允许输入，但结果略超过文档理想上限2。保留Brier公式及事实，将DB上限容差设为1e-12；
没有把分数截断为2。两项真实PG边界测试与追加0007迁移覆盖此选择。

## D016：合约模块确实未实现

实施设计7.3明确要求9.2规则确定后才生成最终合约账本迁移。
真实合约、资金/杠杆/保证金/仓位/费用/资金费/标记价/强平规则尚未获取和核验。
未生成最终合约账本，也未实现模拟成交、CSV事务导入、收益实验、审批生效和回退。
只提供通用候选/实验查询与拒绝入口，runtime也拒绝开启未实现模式；这不是“代码完成但开关关闭”。
固定规则后仍需专门开发及手算核对，不能用现货账本或假定杠杆替代。
2026-10-07用户说明服务器现有Agent已有根据仓位、最大可承受亏损和强平位置计算止盈止损的逻辑；
优先获取和复用该代码，而非另设公式。现有实盘风控不能仅凭口述视为已完成模拟账本适配。
同日用户明确CSV暂不需要、策略实验后续做；CSV与收益实验/审批/回退仍未实现，但不阻塞当前阶段。

## D017：指标由实际记录计算，缺失不能变成零

原生delegate_task返回api_calls、duration_seconds、tokens.input/output；插件只取这些字段，
不会复制summary/error、tool trace、URL或密钥。子任务计量每会话写一次，幂等且不能改写，
最多四个不同周期；父会话累计计数单调增加，最多首轮与修复两会话。写入仍检查lease/fence/DB时钟。
最后父usage采样在提交前；原生未返回、崩溃或1秒计量上报失败时有缺口，不能冒充完整费用账单。
计量失败不阻止正式发布，不用于评分、记忆或策略审批。
status提供24小时窗口：按engine分组的模型开始至发布p50/p95，首次结算相对端点分钟收盘的延迟，
正式预测所引用长期趋势的跨attempt复用数，以及各角色已上报耗时/失败率/调用量/token计数。
usage最多采样最近2000个attempt，超过时显式sample_truncated；缺失值为null，另列已知样本数。
这些指标来自fixture/实际DB时间测试；真实行情、模型性能和调用成本仍需服务器测量。

## D018：合约统计时间契约与四周期隔离

官方文档核对了合约统计端点、100条请求上限、UTC周期和单位；主动成交明确请求unit=0，
数组中的sellVol在buyVol之前。增加三类HTTP采集、严格行数/有限数/重复时间/收盘校验。
文档只称统计ts为timestamp，无法据此证明它是区间起点、终点还是瞬时观测；只读探测虽然
返回code=0，买卖量也未与抽查K线量精确对应，不能冒充完整口径验收。
因此taker_timestamp、oi_history_timestamp和long_short_timestamp默认null；完成时间口径验证后
显式选择open/close或适用的instant。未选择时不调用该统计源并记录MISSING原因，保留预测主链。
这些选择进入engine hash。观察携带timeframe，快照引用和数值计算都隔离周期，避免1h订正
触发未收盘长周期的背景重建；OI只接受相邻确切边界，不插值。

## D019：主动发现价格订正

新增PRICE_REFRESH确定性任务及0008迁移。每日为已结算预测创建幂等重查任务，默认回溯90天，
每个调度tick最多24项；范围可配置1～3650天，批次最多168，避免一次长事务灌入全部历史。
仅在每小时T+4到T+55间启动，与SETTLE共用四个确定性并发名额，SETTLE优先；不占模型名额。
从原预测读取合约和确切端点，外部I/O在事务外，共用45秒预算。缺失/失败单任务最多三次，
次日可再次重查，不将缺失当作无订正。不变内容no-op，变化追加行情版本，由既有调度链
重新评分及刷新复盘；不修改原预测、不重问LLM、不重新决定过去的交易。
扫描不是无限历史完整覆盖；部署后仍须核对API历史范围、流量和真实订正行为。

## D020：固定篮子的真实覆盖与可复现输入

篮子要求明确的OKX instId，可混合显式spot/衍生品，不自动补成员；默认仍为空。
每周期每成员需20根连续、UTC对齐、已收盘K线，SMA20含当前收盘，严格高于才计入分子。
保存原始成员K线和失败原因，分母是有效覆盖成员，同时保存配置成员总数。
达到最低覆盖门槛但未全覆盖时保留数值并标PARTIAL_COVERAGE，低于门槛则没有数值。
重取时间不进入成员内容hash，防止无行情变化的重取刷新长期报告；快照按timeframe隔离引用。
工程上限50个成员、instId最多128字符，共用5秒可选行情预算，超限显式拒绝，不悄悄截篮子。
模型usage采样也改为仅计模型任务，不把SETTLE/PRICE_REFRESH当成缺失模型用量。

## D021：清算公开采样的覆盖和金额口径

新增单runtime内的公开WebSocket采样任务，固定liquidation-orders和SWAP/FUTURES，拒绝私有路径或URL凭据。
订阅确认后每20秒发送文本ping，5秒内必须收到pong；已确认连接区间写入不可变观测，
断连/重连/停机/重启写入缺口。无订单密钥、无登录消息。可选采样断连不会伪造数值或阻塞基础预测；
状态另行报告。归属丢失或任务意外退出触发runtime故障保护，所有写入仍验证同一实例锁/代次。
原始sz保留张数、强平标记价格和方向。官方合约面值公式用于金额：线性sz×ctVal×bkPx，
反向USD合约sz×ctVal；仅支持明确币种且ctMult=1的已验证元数据，未知口径输出MISSING。
面值证据随汇总保存；它来自当前公共合约元数据，不能充当面值变更的完整历史档案。
每个周期只汇总自己的上一完整区间，保留原始事件/覆盖ID和缺口，再计算(空头金额-多头金额)/总金额。
最多读取10000事件/6000覆盖段，超限不输出指标；无采样事件时为null，不能填0或推断没有爆仓。
公开频道本来不完整，所以有数值也标PARTIAL_COVERAGE；心跳覆盖只是连接证据，不保证交易所完整推送。
频道没有事件ID，以合约和明细内容hash去重；同毫秒完全相同的真实明细可能被合并，这是公开采样限制。
乱序/迟到事件按市场时间归窗，真实available_at保留；后续汇总追加修订，已冻结预测不变。
重取时间和未来心跳不改变已闭合区间内容hash；原始高频记录不直接混入模型快照。
依赖固定websockets15.0.1。真实本机WebSocket和PG已验证，交易所连续采样/代理/负载仍待部署验收。

## D022：父进程消失后的归属回收

原PID消失或被复用时，不向该PID或旧进程组发送信号；扫描同一用户的候选进程，先核对
任务工作目录，再核对attempt ID、token摘要和创建时间。只有归属吻合的残留进程才单独TERM/KILL。
无法读取归属时保留UNVERIFIED，不能误杀同机实盘Agent。清理在工作线程检查进程，避免阻塞事件循环。
真实本机测试覆盖父进程退出且子进程另建session，同时保留同目录/不同token的无关进程。
不保证清理主动移走工作目录或清除归属环境的子进程；Linux实际崩溃演练仍需服务器验收。

## 官方核对来源

- [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)：每个并发任务独立 AsyncSession。
- [Pydantic models](https://docs.pydantic.dev/latest/concepts/models/)：拒绝未知字段及有限数校验。
- [OKX API](https://app.okx.com/docs-v5/en)：完整K线 confirm=1，6Hutc/12Hutc，成交量张数/币数/交易额区分。
- [OKX公开清算](https://www.okx.com/docs-v5/en/#public-data-websocket-liquidation-orders-channel)：非全量、乱序、sz单位及强平标记价格。
- [websockets15.0.1](https://websockets.readthedocs.io/en/15.0.1/reference/asyncio/client.html)：异步连接、超时、消息和队列边界。
- [CubeSandbox quickstart](https://docs.cubesandbox.com/guide/quickstart)：服务需要Linux虚拟化和存储部署，不能把backend配置当作验收。
- [Cube Python SDK](https://raw.githubusercontent.com/TencentCloud/CubeSandbox/master/sdk/python/README.md)：接口适配仍需真实服务测试。
- [Cube network policy](https://docs.cubesandbox.com/guide/network-policy)：allow优先、模板继承和IPv4边界。
- [Cube authentication](https://docs.cubesandbox.com/guide/authentication)与[network hardening](https://docs.cubesandbox.com/guide/network-hardening)：服务端认证、TLS和管理入口须单独配置。
- [PostgreSQL pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)、[pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html)与[libpq环境变量](https://www.postgresql.org/docs/16/libpq-envars.html)：一致custom备份、恢复事务及passfile。

## D023：策略历史经验索引与渐进读取

2026-10-10：用户授权先实现策略迭代历史经验召回及summary/content分层。
可选兼容链路用独立SQLite/FTS5派生索引，先看有界摘要，再按冻结ID分页读正文；
不共享Hermes state.db，也不改正式预测PG记忆、engine或角色权限。
旧Markdown不推断已核验测试/收益，完整结构化记录保留程序状态并与生成Markdown去重。
来源变更追加派生版本、旧召回保留正文；这是来源同步所需版本保证，不是LLM经验纠错。
可选迭代引用记录冻结正文，未引用的旧请求指纹保持兼容。
主题是固定词汇标签，不冒充行情状态分类；40%门槛、语义匹配、自动纠错和收益实验未实现。
