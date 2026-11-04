> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 当前项目测试样例全量清单

整理日期：2026-11-04。覆盖当前仓库可收集的351个测试函数，参数化逐项展开为517个用例；包括68个共享核心、241个运行时单元、192个PostgreSQL集成、13个Hermes契约及3个真实Cube验收用例。

代码文件链接定位到测试函数；样例中的价格、金额、日期及身份均为测试设定。下表登记现有测试方法与断言预期，本轮仅收集用例，不执行测试正文、不新增通过结论。预设数据、假客户端及需显式启用的真实环境在“测试范围”中注明；归档代码和一次性历史验收脚本不计入本清单。

## 共享交易核心（68例）

| 测试范围 | 代码文件 | 怎么测，预期是什么 |
| --- | --- | --- |
| 共享核心／资金费结算幂等 | [tests/test_accounting_core.py:23](../tests/test_accounting_core.py#L23) | 构造多仓、空仓及正负资金费，重复调用到期结算；预期只结算一次，已实现收益、累计资金费和审计金额一致。 参数：direction=1；fee=-0.01。 |
| 共享核心／资金费结算幂等 | [tests/test_accounting_core.py:23](../tests/test_accounting_core.py#L23) | 构造多仓、空仓及正负资金费，重复调用到期结算；预期只结算一次，已实现收益、累计资金费和审计金额一致。 参数：direction=-1；fee=0.01。 |
| 共享核心／账户估值 | [tests/test_accounting_core.py:48](../tests/test_accounting_core.py#L48) | 向持仓注入行情和资金费元数据；预期更新同一个共享账户的资金费率、未实现盈亏和总未实现盈亏。 |
| 共享核心／成交记账 | [tests/test_accounting_core.py:76](../tests/test_accounting_core.py#L76) | 用确认成交依次开仓和平仓；预期保证金、名义金额、止损参数及实现收益正确，只追加对应成交事件，不创建并行文件账本。 |
| 共享核心／持仓价格波动记录 | [tests/test_accounting_core.py:102](../tests/test_accounting_core.py#L102) | 构造多空方向的有利和不利价格波动后退出；预期退出事件保存未乘杠杆的最大有利、最大不利波动比例。 参数：direction=1；adverse=0.05。 |
| 共享核心／持仓价格波动记录 | [tests/test_accounting_core.py:102](../tests/test_accounting_core.py#L102) | 构造多空方向的有利和不利价格波动后退出；预期退出事件保存未乘杠杆的最大有利、最大不利波动比例。 参数：direction=-1；adverse=0.06。 |
| 模型返回校验／仓位倍率 | [tests/test_ai_schema.py:7](../tests/test_ai_schema.py#L7) | 传入position_multiplier大于1的分析对象；预期校验失败，不能用模型输出放大仓位。 |
| 模型返回校验／提示注入 | [tests/test_ai_schema.py:23](../tests/test_ai_schema.py#L23) | 向分析字段加入越权或提示注入语言；预期结构校验拒绝该输出。 |
| 模型返回校验／常见格式偏差 | [tests/test_ai_schema.py:39](../tests/test_ai_schema.py#L39) | 传入常见字段格式错误的模型JSON；预期按既定规则规范化置信度和事件列表。 |
| 模型返回校验／单元素数组 | [tests/test_ai_schema.py:56](../tests/test_ai_schema.py#L56) | 返回只有一个对象的JSON数组；预期解包为单份分析，并保留标的及禁止交易标记。 |
| 模型返回校验／多元素数组 | [tests/test_ai_schema.py:77](../tests/test_ai_schema.py#L77) | 返回含多个分析对象的数组；预期拒绝，不能任意挑选一个结果。 |
| 模型返回校验／事件字段 | [tests/test_ai_schema.py:82](../tests/test_ai_schema.py#L82) | 传入事件字段类型及形状不规范的JSON；预期规范化倍率、置信度、事件标题及风险级别。 |
| 模型返回校验／新闻相关性 | [tests/test_ai_schema.py:101](../tests/test_ai_schema.py#L101) | 传入相关性字段不规范的JSON；预期规范化相关性分类、分数和忽略事件列表。 |
| 模型返回校验／事件方向字段 | [tests/test_ai_schema.py:123](../tests/test_ai_schema.py#L123) | 传入方向、期限和证据字段不规范的JSON；预期得到规定的方向、置信度、日内期限、衰减分钟和证据列表。 |
| 社媒分析／无近期数据 | [tests/test_ai_schema.py:157](../tests/test_ai_schema.py#L157) | 调用无近期新闻的降级结果；预期标记no_recent_data，保留既定缩仓倍率，不伪造高风险事件或阻止交易。 |
| 社媒分析／系统异常降级 | [tests/test_ai_schema.py:166](../tests/test_ai_schema.py#L166) | 模拟模型或schema失败；预期记录真实错误类别、零置信度和既定降级倍率，不把系统故障冒充真实高风险。 |
| 社媒分析／阻止新仓的降级策略 | [tests/test_ai_schema.py:182](../tests/test_ai_schema.py#L182) | 启用明确阻止新仓的故障策略并触发失败；预期禁止新仓且仓位倍率为0。 |
| 社媒分析／低相关政治新闻 | [tests/test_ai_schema.py:189](../tests/test_ai_schema.py#L189) | 输入政治或外交新闻样本；预期低市场相关性、忽略事件及HOLD，不无条件阻止交易。 |
| 社媒分析／地缘市场冲击 | [tests/test_ai_schema.py:210](../tests/test_ai_schema.py#L210) | 输入会影响市场的地缘冲击新闻样本；预期识别宏观相关性及短期偏空信息，记录启发式分析来源。 |
| 社媒分析／定投与长期配置 | [tests/test_ai_schema.py:246](../tests/test_ai_schema.py#L246) | 输入定投、长期持有或资产配置新闻；预期不产生短线方向信号，方向置信度和衰减期限归零。 |
| 社媒分析／抑制无效看多信号 | [tests/test_ai_schema.py:269](../tests/test_ai_schema.py#L269) | 给被动配置新闻附带模型看多结论；预期抑制该方向，清空不适用证据和事件。 |
| 新闻解析／BlockBeats | [tests/test_blockbeats_pro_source.py:4](../tests/test_blockbeats_pro_source.py#L4) | 解析预设快讯响应；预期去除HTML，保留来源类型、信任度、正文和原文链接。 |
| 风控／紧急状态新仓 | [tests/test_execution_core.py:104](../tests/test_execution_core.py#L104) | 将运行状态设为紧急模式并申请开仓；预期风控拒绝，最终动作HOLD。 |
| 风控／紧急状态减仓 | [tests/test_execution_core.py:117](../tests/test_execution_core.py#L117) | 紧急模式下申请减少已有持仓；预期允许绑定的平仓动作，并沿用原持仓参数。 |
| 执行适配（假交易所客户端）／合约与只减仓 | [tests/test_execution_core.py:135](../tests/test_execution_core.py#L135) | 通过假的下单客户端执行平仓；预期使用BTC-USDT-SWAP合约及正确买卖方向，订单设置reduce_only。 |
| 执行适配（假交易所客户端）／成交明细 | [tests/test_execution_core.py:164](../tests/test_execution_core.py#L164) | 给出预设交易所成交返回；预期保留实际成交价、合约张数、面值和费用金额。 |
| 执行适配（假交易所客户端）／拒单原因 | [tests/test_execution_core.py:181](../tests/test_execution_core.py#L181) | 让假客户端返回OKX拒单；预期执行失败并保留原错误码明细，不伪造成交。 |
| 执行适配（假交易所客户端）／原生止盈止损 | [tests/test_execution_core.py:195](../tests/test_execution_core.py#L195) | 模拟开仓成功；预期生成标记价格触发的OCO止盈止损，执行价采用规定的市价标记。 |
| 执行适配（假交易所客户端）／退出价格基准 | [tests/test_execution_core.py:222](../tests/test_execution_core.py#L222) | 令成交价与信号收盘价不同；预期原生止盈止损及移动止盈激活价使用成交价计算。 |
| 执行适配（假交易所客户端）／原生移动止盈 | [tests/test_execution_core.py:249](../tests/test_execution_core.py#L249) | 模拟开仓后的移动止盈委托；预期使用move_order_stop、相反买卖方向及reduceOnly。 |
| 新闻解析／OKX公告 | [tests/test_okx_announcements_source.py:8](../tests/test_okx_announcements_source.py#L8) | 解析预设官方公告响应；预期保留标题、官方来源类型、信任度和公告原文链接。 |
| 新闻采集（模拟HTTP）／公告缓存 | [tests/test_okx_announcements_source.py:44](../tests/test_okx_announcements_source.py#L44) | 连续请求同一批公告；预期缓存复用，HTTP只请求一次，分页参数及User-Agent正确。 |
| 交易所响应解析／账户权益 | [tests/test_okx_client.py:8](../tests/test_okx_client.py#L8) | 传入预设账户返回；预期提取正确总权益数值。 |
| 行情客户端（模拟HTTP）／GET重试 | [tests/test_okx_client.py:14](../tests/test_okx_client.py#L14) | 首个GET返回临时403、之后成功；预期有界重试成功，忽略环境代理并使用规定的请求头。 |
| 订单客户端（模拟HTTP）／POST不重试 | [tests/test_okx_client.py:52](../tests/test_okx_client.py#L52) | 模拟订单POST失败；预期只请求一次并抛出错误，避免重复订单。 |
| 模拟执行／订单去重 | [tests/test_order_idempotency.py:8](../tests/test_order_idempotency.py#L8) | 使用同一订单ID重复提交模拟订单；预期第一次成功、第二次拒绝，不重复成交。 |
| 模拟风控／杠杆亏损止损 | [tests/test_paper_risk.py:31](../tests/test_paper_risk.py#L31) | 构造达到杠杆损失阈值的持仓行情；预期触发平仓，原因包含止损。 |
| 模拟风控／杠杆盈利止盈 | [tests/test_paper_risk.py:37](../tests/test_paper_risk.py#L37) | 构造达到杠杆利润阈值的持仓行情；预期触发平仓，原因包含止盈。 |
| 模拟风控／持仓专属止损 | [tests/test_paper_risk.py:43](../tests/test_paper_risk.py#L43) | 设置持仓专属止损比例并提供未达到该阈值的行情；预期不被通用固定止损提前平仓。 |
| 模拟风控／持仓专属止盈 | [tests/test_paper_risk.py:52](../tests/test_paper_risk.py#L52) | 设置持仓专属止盈比例并提供未达到该阈值的行情；预期不被通用固定止盈提前平仓。 |
| 模拟风控／移动止盈启动 | [tests/test_paper_risk.py:61](../tests/test_paper_risk.py#L61) | 构造尚未积累足够利润的回撤；预期移动止盈尚未激活，不平仓。 |
| 模拟风控／趋势持仓移动止盈 | [tests/test_paper_risk.py:72](../tests/test_paper_risk.py#L72) | 设置持仓专属移动止盈参数并模拟趋势中的正常回撤；预期按专属规则继续持仓。 |
| 模拟风控／价格回撤退出 | [tests/test_paper_risk.py:84](../tests/test_paper_risk.py#L84) | 先达到价格单位移动止盈激活线，再发生足够回撤；预期触发价格回撤平仓。 |
| 模拟风控／激活线不乘杠杆 | [tests/test_paper_risk.py:98](../tests/test_paper_risk.py#L98) | 构造杠杆收益达标但真实价格涨幅尚未达标的行情；预期不提前激活价格单位移动止盈。 |
| 模拟风控／区间中线退出 | [tests/test_paper_risk.py:111](../tests/test_paper_risk.py#L111) | 区间持仓价格回到冻结区间中线；预期触发区间均值回归平仓。 |
| 模拟风控／半R后收紧止损 | [tests/test_paper_risk.py:123](../tests/test_paper_risk.py#L123) | 区间单运行到半个风险单位的利润后再检查；预期收紧持仓止损，尚未满足退出时不平仓。 |
| 预测风控接入／交易风格参数 | [tests/test_prediction_risk_adapter.py:53](../tests/test_prediction_risk_adapter.py#L53) | 分别用趋势、突破、区间样例计算参数；预期沿用不同止损、止盈和仓位算法，无正向收益空间的区间单被拒绝。 |
| 预测风控接入／低概率与原风险预算 | [tests/test_prediction_risk_adapter.py:82](../tests/test_prediction_risk_adapter.py#L82) | 给父决策很低的置信度并走完整风险准备及成交记账；预期不增加固定概率门槛，仍按原风险预算计算仓位杠杆，重复入场被拒绝。 |
| 预测风控接入／账户与社媒限制 | [tests/test_prediction_risk_adapter.py:129](../tests/test_prediction_risk_adapter.py#L129) | 分别触发高社媒风险和日亏损限制，再申请绑定平仓；预期限制新仓，但不阻止必要平仓。 |
| 预测风控接入／区间退出规则 | [tests/test_prediction_risk_adapter.py:167](../tests/test_prediction_risk_adapter.py#L167) | 生成区间单并成交后检查中线行情；预期保留区间中线退出，不给区间单附加趋势移动止盈。 |
| 风控／中等社媒风险 | [tests/test_risk.py:31](../tests/test_risk.py#L31) | 输入中等风险社媒分析；预期允许交易但按原规则减少仓位。 |
| 风控／高社媒风险 | [tests/test_risk.py:38](../tests/test_risk.py#L38) | 输入高风险社媒分析；预期拒绝交易并标记社媒过滤阻断。 |
| 风控／窄止损动态杠杆 | [tests/test_risk.py:44](../tests/test_risk.py#L44) | 给出较窄止损距离；预期在原风险预算内提高动态杠杆，仓位和名义敞口计算一致。 |
| 风控／宽止损动态杠杆 | [tests/test_risk.py:55](../tests/test_risk.py#L55) | 给出较宽止损距离；预期降低动态杠杆，保留风险预算约束。 |
| 风控／最低杠杆约束 | [tests/test_risk.py:66](../tests/test_risk.py#L66) | 将计算杠杆压到最低杠杆以下；预期采用最低杠杆，同时调整仓位比例满足风险预算。 |
| 风控／最低保证金与风险预算冲突 | [tests/test_risk.py:77](../tests/test_risk.py#L77) | 构造最低杠杆下无法兼顾最低保证金及风险预算的账户；预期拒绝开仓，不通过扩大风险凑足保证金。 |
| 风控／保证金比例硬上限 | [tests/test_risk.py:94](../tests/test_risk.py#L94) | 将配置允许值调到超过测试规定上限；预期最终保证金比例仍不超过10%的既有硬限制。 |
| 风控／保证金不足1 USDT | [tests/test_risk.py:104](../tests/test_risk.py#L104) | 构造按预算计算保证金不足最低金额的账户；预期拒绝并记录below_min_margin。 |
| 风控／满足最低保证金 | [tests/test_risk.py:121](../tests/test_risk.py#L121) | 构造可满足1 USDT最低保证金的账户；预期开仓获准，保证金达到下限。 |
| 风控／自定义最低保证金 | [tests/test_risk.py:137](../tests/test_risk.py#L137) | 设置2 USDT最低保证金及1%风险预算；预期合法仓位达到最低金额，未突破预算。 |
| 风控／同方向持仓数限制 | [tests/test_risk.py:154](../tests/test_risk.py#L154) | 预先放置足量同方向持仓后申请新仓；预期被同方向持仓数检查拒绝。 |
| 风控／同方向保证金限制 | [tests/test_risk.py:164](../tests/test_risk.py#L164) | 构造同方向保证金集中敞口；预期超过限制时拒绝新仓。 |
| 共享量化／已知公式样例 | [tests/test_shared_quantitative.py:31](../tests/test_shared_quantitative.py#L31) | 构造80根从100涨到179的K线；预期20根收益、ATR、RSI、ADX及相对量能与手算值一致，缺主动成交数据时CVD明确缺失。 |
| 共享量化／历史与时点边界 | [tests/test_shared_quantitative.py:40](../tests/test_shared_quantitative.py#L40) | 分别只提供79根历史、将决策时点提前到最后数据可用之前；预期报历史不足和未来数据错误。 |
| 社媒过滤／无关新闻 | [tests/test_social_filter.py:18](../tests/test_social_filter.py#L18) | 混入模型产品、股票及加密市场新闻；预期排除无关无标的内容，保留明确加密标的或市场相关新闻。 |
| 社媒过滤／明确标的 | [tests/test_social_filter.py:39](../tests/test_social_filter.py#L39) | 提供显式匹配标的与无关标的新闻；预期只保留匹配目标币种的内容。 |
| 社媒解析／X消息 | [tests/test_x_api_source.py:4](../tests/test_x_api_source.py#L4) | 解析预设X消息；预期保存作者、正文、互动总量、消息链接及来源信任度。 |
| 社媒查询／X查询模板 | [tests/test_x_api_source.py:33](../tests/test_x_api_source.py#L33) | 渲染BTC查询及排除条件；预期正确展开币名和交易对，保留排除转推、回复的过滤条件。 |

## 运行时单元测试（241例）

| 测试范围 | 代码文件 | 怎么测，预期是什么 |
| --- | --- | --- |
| 只读行情认证／显式凭据文件 | [trading-agent/tests/unit/test_authenticated_market.py:30](../trading-agent/tests/unit/test_authenticated_market.py#L30) | 读取受限测试凭据文件，其中含类似变量的字面文本；预期不进行环境展开，不把凭据导出到进程环境。 |
| 只读行情认证／不安全文件拒绝 | [trading-agent/tests/unit/test_authenticated_market.py:41](../trading-agent/tests/unit/test_authenticated_market.py#L41) | 分别提供过宽权限、符号链接、缺失文件或非法文本；预期OKX_CREDENTIAL_FILE_INVALID，错误不包含凭据内容。 参数：invalid=public_permissions。 |
| 只读行情认证／不安全文件拒绝 | [trading-agent/tests/unit/test_authenticated_market.py:41](../trading-agent/tests/unit/test_authenticated_market.py#L41) | 分别提供过宽权限、符号链接、缺失文件或非法文本；预期OKX_CREDENTIAL_FILE_INVALID，错误不包含凭据内容。 参数：invalid=symlink。 |
| 只读行情认证／不安全文件拒绝 | [trading-agent/tests/unit/test_authenticated_market.py:41](../trading-agent/tests/unit/test_authenticated_market.py#L41) | 分别提供过宽权限、符号链接、缺失文件或非法文本；预期OKX_CREDENTIAL_FILE_INVALID，错误不包含凭据内容。 参数：invalid=missing。 |
| 只读行情认证／不安全文件拒绝 | [trading-agent/tests/unit/test_authenticated_market.py:41](../trading-agent/tests/unit/test_authenticated_market.py#L41) | 分别提供过宽权限、符号链接、缺失文件或非法文本；预期OKX_CREDENTIAL_FILE_INVALID，错误不包含凭据内容。 参数：invalid=unicode。 |
| 只读行情认证（模拟HTTP）／签名与摘要 | [trading-agent/tests/unit/test_authenticated_market.py:58](../trading-agent/tests/unit/test_authenticated_market.py#L58) | 向假HTTP端发送只读签名查询，独立计算签名并解析权限结果；预期GET及签名正确，公开摘要仅给GET_ONLY权限结论，不泄露私有账户返回。 |
| 只读行情认证（模拟HTTP）／传输白名单 | [trading-agent/tests/unit/test_authenticated_market.py:104](../trading-agent/tests/unit/test_authenticated_market.py#L104) | 尝试订单端点、POST、非信任域名、HTTP、异常端口或带请求体GET；预期在I/O前报OKX_READ_ONLY_FORBIDDEN。 参数：method=POST；url=https://www.okx.com/api/v5/trade/order；body=无。 |
| 只读行情认证（模拟HTTP）／传输白名单 | [trading-agent/tests/unit/test_authenticated_market.py:104](../trading-agent/tests/unit/test_authenticated_market.py#L104) | 尝试订单端点、POST、非信任域名、HTTP、异常端口或带请求体GET；预期在I/O前报OKX_READ_ONLY_FORBIDDEN。 参数：method=POST；url=https://www.okx.com/api/v5/account/config；body=无。 |
| 只读行情认证（模拟HTTP）／传输白名单 | [trading-agent/tests/unit/test_authenticated_market.py:104](../trading-agent/tests/unit/test_authenticated_market.py#L104) | 尝试订单端点、POST、非信任域名、HTTP、异常端口或带请求体GET；预期在I/O前报OKX_READ_ONLY_FORBIDDEN。 参数：method=GET；url=https://www.okx.com/api/v5/trade/order；body=无。 |
| 只读行情认证（模拟HTTP）／传输白名单 | [trading-agent/tests/unit/test_authenticated_market.py:104](../trading-agent/tests/unit/test_authenticated_market.py#L104) | 尝试订单端点、POST、非信任域名、HTTP、异常端口或带请求体GET；预期在I/O前报OKX_READ_ONLY_FORBIDDEN。 参数：method=GET；url=https://untrusted.invalid/api/v5/account/config；body=无。 |
| 只读行情认证（模拟HTTP）／传输白名单 | [trading-agent/tests/unit/test_authenticated_market.py:104](../trading-agent/tests/unit/test_authenticated_market.py#L104) | 尝试订单端点、POST、非信任域名、HTTP、异常端口或带请求体GET；预期在I/O前报OKX_READ_ONLY_FORBIDDEN。 参数：method=GET；url=http://www.okx.com/api/v5/account/config；body=无。 |
| 只读行情认证（模拟HTTP）／传输白名单 | [trading-agent/tests/unit/test_authenticated_market.py:104](../trading-agent/tests/unit/test_authenticated_market.py#L104) | 尝试订单端点、POST、非信任域名、HTTP、异常端口或带请求体GET；预期在I/O前报OKX_READ_ONLY_FORBIDDEN。 参数：method=GET；url=https://www.okx.com:8443/api/v5/account/config；body=无。 |
| 只读行情认证（模拟HTTP）／传输白名单 | [trading-agent/tests/unit/test_authenticated_market.py:104](../trading-agent/tests/unit/test_authenticated_market.py#L104) | 尝试订单端点、POST、非信任域名、HTTP、异常端口或带请求体GET；预期在I/O前报OKX_READ_ONLY_FORBIDDEN。 参数：method=GET；url=https://www.okx.com/api/v5/account/config；body=非空字节请求体（测试数据）。 |
| 只读行情认证（模拟HTTP）／不重定向不降级 | [trading-agent/tests/unit/test_authenticated_market.py:121](../trading-agent/tests/unit/test_authenticated_market.py#L121) | 让端点重定向或移除指定凭据文件；预期不跟随跳转，也不静默退回未验证认证方式，报明确错误。 |
| 行情客户端／未配置文件保留公开模式 | [trading-agent/tests/unit/test_authenticated_market.py:143](../trading-agent/tests/unit/test_authenticated_market.py#L143) | 不指定凭据文件创建客户端；预期得到普通公开OKXClient，不自动读取其他凭据。 |
| 只读采集预检（模拟HTTP）／权限验证 | [trading-agent/tests/unit/test_authenticated_market.py:151](../trading-agent/tests/unit/test_authenticated_market.py#L151) | 运行标准采集预检；预期先查公开时钟再执行只读账户权限确认，返回credential_read_only，不调用写端点。 |
| 备份保留策略／不删无关文件 | [trading-agent/tests/unit/test_backup_retention.py:6](../trading-agent/tests/unit/test_backup_retention.py#L6) | 准备多天日备份、多周周备份、无关文件和符号链接再清理；预期保留7日4周，不删除无关文件或跟随符号链接。 |
| 市场宽度（模拟HTTP）／篮子覆盖与UTC边界 | [trading-agent/tests/unit/test_breadth.py:37](../trading-agent/tests/unit/test_breadth.py#L37) | 给五币篮子中四币有效历史、两币高于SMA20，遍历四周期；预期宽度0.5及4/5部分覆盖，时点对齐，覆盖不足时无有效数值。 |
| 市场宽度／成员K线质量 | [trading-agent/tests/unit/test_breadth.py:82](../trading-agent/tests/unit/test_breadth.py#L82) | 先给连续已收盘有效历史，再注入缺口、未结束桶或坏价格；预期有效成员结果hash稳定，无效历史拒绝。 |
| 市场宽度／空篮子 | [trading-agent/tests/unit/test_breadth.py:99](../trading-agent/tests/unit/test_breadth.py#L99) | 用空篮子调用采集；预期直接返回空，不请求网络也不擅自添加资产。 |
| 分钟重建／只取当时完整分钟 | [trading-agent/tests/unit/test_candle_reconstruction.py:47](../trading-agent/tests/unit/test_candle_reconstruction.py#L47) | 用目标周期内203根完整分钟重建快照；预期OHLC、成交量及截止时点精确一致，标RECONSTRUCTED，不允许混入目标截止之后的分钟。 |
| 分钟重建／两币四周期公式与时间语义 | [trading-agent/tests/unit/test_candle_reconstruction.py:75](../trading-agent/tests/unit/test_candle_reconstruction.py#L75) | 遍历两币四周期，以原80根窗口加重建快照计算；预期使用原公式，保留真实采集时间，对秒级未覆盖明确45秒缺口，不冒充当时已收到的快照。 参数：timeframe=1h；inst_id=BTC-USDT-SWAP。 |
| 分钟重建／两币四周期公式与时间语义 | [trading-agent/tests/unit/test_candle_reconstruction.py:75](../trading-agent/tests/unit/test_candle_reconstruction.py#L75) | 遍历两币四周期，以原80根窗口加重建快照计算；预期使用原公式，保留真实采集时间，对秒级未覆盖明确45秒缺口，不冒充当时已收到的快照。 参数：timeframe=1h；inst_id=ETH-USDT-SWAP。 |
| 分钟重建／两币四周期公式与时间语义 | [trading-agent/tests/unit/test_candle_reconstruction.py:75](../trading-agent/tests/unit/test_candle_reconstruction.py#L75) | 遍历两币四周期，以原80根窗口加重建快照计算；预期使用原公式，保留真实采集时间，对秒级未覆盖明确45秒缺口，不冒充当时已收到的快照。 参数：timeframe=4h；inst_id=BTC-USDT-SWAP。 |
| 分钟重建／两币四周期公式与时间语义 | [trading-agent/tests/unit/test_candle_reconstruction.py:75](../trading-agent/tests/unit/test_candle_reconstruction.py#L75) | 遍历两币四周期，以原80根窗口加重建快照计算；预期使用原公式，保留真实采集时间，对秒级未覆盖明确45秒缺口，不冒充当时已收到的快照。 参数：timeframe=4h；inst_id=ETH-USDT-SWAP。 |
| 分钟重建／两币四周期公式与时间语义 | [trading-agent/tests/unit/test_candle_reconstruction.py:75](../trading-agent/tests/unit/test_candle_reconstruction.py#L75) | 遍历两币四周期，以原80根窗口加重建快照计算；预期使用原公式，保留真实采集时间，对秒级未覆盖明确45秒缺口，不冒充当时已收到的快照。 参数：timeframe=6h；inst_id=BTC-USDT-SWAP。 |
| 分钟重建／两币四周期公式与时间语义 | [trading-agent/tests/unit/test_candle_reconstruction.py:75](../trading-agent/tests/unit/test_candle_reconstruction.py#L75) | 遍历两币四周期，以原80根窗口加重建快照计算；预期使用原公式，保留真实采集时间，对秒级未覆盖明确45秒缺口，不冒充当时已收到的快照。 参数：timeframe=6h；inst_id=ETH-USDT-SWAP。 |
| 分钟重建／两币四周期公式与时间语义 | [trading-agent/tests/unit/test_candle_reconstruction.py:75](../trading-agent/tests/unit/test_candle_reconstruction.py#L75) | 遍历两币四周期，以原80根窗口加重建快照计算；预期使用原公式，保留真实采集时间，对秒级未覆盖明确45秒缺口，不冒充当时已收到的快照。 参数：timeframe=12h；inst_id=BTC-USDT-SWAP。 |
| 分钟重建／两币四周期公式与时间语义 | [trading-agent/tests/unit/test_candle_reconstruction.py:75](../trading-agent/tests/unit/test_candle_reconstruction.py#L75) | 遍历两币四周期，以原80根窗口加重建快照计算；预期使用原公式，保留真实采集时间，对秒级未覆盖明确45秒缺口，不冒充当时已收到的快照。 参数：timeframe=12h；inst_id=ETH-USDT-SWAP。 |
| 分钟重建／坏原料不补造 | [trading-agent/tests/unit/test_candle_reconstruction.py:116](../trading-agent/tests/unit/test_candle_reconstruction.py#L116) | 分别注入缺分钟、重复、空历史、错标的、未来收到或过旧分钟；预期报对应覆盖/范围错误，不生成伪完整快照。 参数：fault=gap；code=MINUTE_COVERAGE_GAP。 |
| 分钟重建／坏原料不补造 | [trading-agent/tests/unit/test_candle_reconstruction.py:116](../trading-agent/tests/unit/test_candle_reconstruction.py#L116) | 分别注入缺分钟、重复、空历史、错标的、未来收到或过旧分钟；预期报对应覆盖/范围错误，不生成伪完整快照。 参数：fault=duplicate；code=MINUTE_COVERAGE_GAP。 |
| 分钟重建／坏原料不补造 | [trading-agent/tests/unit/test_candle_reconstruction.py:116](../trading-agent/tests/unit/test_candle_reconstruction.py#L116) | 分别注入缺分钟、重复、空历史、错标的、未来收到或过旧分钟；预期报对应覆盖/范围错误，不生成伪完整快照。 参数：fault=empty；code=MINUTE_COVERAGE_GAP。 |
| 分钟重建／坏原料不补造 | [trading-agent/tests/unit/test_candle_reconstruction.py:116](../trading-agent/tests/unit/test_candle_reconstruction.py#L116) | 分别注入缺分钟、重复、空历史、错标的、未来收到或过旧分钟；预期报对应覆盖/范围错误，不生成伪完整快照。 参数：fault=symbol；code=RECONSTRUCTION_SCOPE。 |
| 分钟重建／坏原料不补造 | [trading-agent/tests/unit/test_candle_reconstruction.py:116](../trading-agent/tests/unit/test_candle_reconstruction.py#L116) | 分别注入缺分钟、重复、空历史、错标的、未来收到或过旧分钟；预期报对应覆盖/范围错误，不生成伪完整快照。 参数：fault=future_receipt；code=RECONSTRUCTION_SCOPE。 |
| 分钟重建／坏原料不补造 | [trading-agent/tests/unit/test_candle_reconstruction.py:116](../trading-agent/tests/unit/test_candle_reconstruction.py#L116) | 分别注入缺分钟、重复、空历史、错标的、未来收到或过旧分钟；预期报对应覆盖/范围错误，不生成伪完整快照。 参数：fault=old；code=RECONSTRUCTION_SCOPE。 |
| 分钟重建／周期刚开始 | [trading-agent/tests/unit/test_candle_reconstruction.py:147](../trading-agent/tests/unit/test_candle_reconstruction.py#L147) | 目标时点位于新周期开始0秒或45秒；预期NO_COMPLETED_MINUTE，不用尚未结束的一分钟构造历史快照。 参数：target=2026-10-30 08:00:00+00:00。 |
| 分钟重建／周期刚开始 | [trading-agent/tests/unit/test_candle_reconstruction.py:147](../trading-agent/tests/unit/test_candle_reconstruction.py#L147) | 目标时点位于新周期开始0秒或45秒；预期NO_COMPLETED_MINUTE，不用尚未结束的一分钟构造历史快照。 参数：target=2026-10-30 08:00:45+00:00。 |
| 分钟重建（模拟HTTP）／12h分页边界 | [trading-agent/tests/unit/test_candle_reconstruction.py:153](../trading-agent/tests/unit/test_candle_reconstruction.py#L153) | 让真实解析客户端经假HTTP分页取得长周期分钟；预期三页取到719个完整分钟，只聚合目标分钟之前的数据并保留截止时点。 |
| 部署锁／隔离证据真实性 | [trading-agent/tests/unit/test_deployment.py:92](../trading-agent/tests/unit/test_deployment.py#L92) | 先给合法锁，再分别将报告改为fixture、错配置hash、缺检查或文件hash不符；预期拒绝并报CUBE_ISOLATION_NOT_VERIFIED。 参数：change=fixture。 |
| 部署锁／隔离证据真实性 | [trading-agent/tests/unit/test_deployment.py:92](../trading-agent/tests/unit/test_deployment.py#L92) | 先给合法锁，再分别将报告改为fixture、错配置hash、缺检查或文件hash不符；预期拒绝并报CUBE_ISOLATION_NOT_VERIFIED。 参数：change=config_hash。 |
| 部署锁／隔离证据真实性 | [trading-agent/tests/unit/test_deployment.py:92](../trading-agent/tests/unit/test_deployment.py#L92) | 先给合法锁，再分别将报告改为fixture、错配置hash、缺检查或文件hash不符；预期拒绝并报CUBE_ISOLATION_NOT_VERIFIED。 参数：change=incomplete。 |
| 部署锁／隔离证据真实性 | [trading-agent/tests/unit/test_deployment.py:92](../trading-agent/tests/unit/test_deployment.py#L92) | 先给合法锁，再分别将报告改为fixture、错配置hash、缺检查或文件hash不符；预期拒绝并报CUBE_ISOLATION_NOT_VERIFIED。 参数：change=file_hash。 |
| 部署锁／模型与稳定记忆位置 | [trading-agent/tests/unit/test_deployment.py:111](../trading-agent/tests/unit/test_deployment.py#L111) | 篡改模型锁或原生memories目录下的稳定文件；预期部署清单或Profile锁不匹配被拒绝。 |
| 部署锁／模型端点及推理设置漂移 | [trading-agent/tests/unit/test_deployment.py:137](../trading-agent/tests/unit/test_deployment.py#L137) | 配置模型base_url或reasoning_effort，再单独改Profile；预期父子模型设置同步且改变引擎身份，锁定后漂移被拒绝。 参数：settings={model_base_url=https://model.example/v1}；section=model；key=base_url；changed_value=https://other.example/v1。 |
| 部署锁／模型端点及推理设置漂移 | [trading-agent/tests/unit/test_deployment.py:137](../trading-agent/tests/unit/test_deployment.py#L137) | 配置模型base_url或reasoning_effort，再单独改Profile；预期父子模型设置同步且改变引擎身份，锁定后漂移被拒绝。 参数：settings={reasoning_effort=high}；section=agent；key=reasoning_effort；changed_value=low。 |
| 部署配置／禁止端点嵌入凭据 | [trading-agent/tests/unit/test_deployment.py:169](../trading-agent/tests/unit/test_deployment.py#L169) | 分别将端点设为含用户认证、查询秘密、片段或本地文件URL；预期配置校验失败，不把凭据写入部署产物。 参数：endpoint=https URL（含用户认证，认证内容省略）。 |
| 部署配置／禁止端点嵌入凭据 | [trading-agent/tests/unit/test_deployment.py:169](../trading-agent/tests/unit/test_deployment.py#L169) | 分别将端点设为含用户认证、查询秘密、片段或本地文件URL；预期配置校验失败，不把凭据写入部署产物。 参数：endpoint=https URL（含查询参数，参数内容省略）。 |
| 部署配置／禁止端点嵌入凭据 | [trading-agent/tests/unit/test_deployment.py:169](../trading-agent/tests/unit/test_deployment.py#L169) | 分别将端点设为含用户认证、查询秘密、片段或本地文件URL；预期配置校验失败，不把凭据写入部署产物。 参数：endpoint=https URL（含片段，片段内容省略）。 |
| 部署配置／禁止端点嵌入凭据 | [trading-agent/tests/unit/test_deployment.py:169](../trading-agent/tests/unit/test_deployment.py#L169) | 分别将端点设为含用户认证、查询秘密、片段或本地文件URL；预期配置校验失败，不把凭据写入部署产物。 参数：endpoint=file:///local/file。 |
| 部署Profile／预测TG工具隔离 | [trading-agent/tests/unit/test_deployment.py:174](../trading-agent/tests/unit/test_deployment.py#L174) | 启用预测TG后检查CLI及TG工具列表，并故意改锁定配置；预期CLI研究工具保留、TG无研究工具，引擎预测身份不变，配置漂移被拒绝。 |
| 部署预检（假子进程）／预算、缓存及失败关闭 | [trading-agent/tests/unit/test_deployment_preflight.py:18](../trading-agent/tests/unit/test_deployment_preflight.py#L18) | 分别模拟认证就绪、无效、超时和启动失败，并重复预检；预期仅就绪返回true、单次探测缓存、使用固定超时并清理进程，不把业务数据库环境交给探针。 本变体预期预检为true。 参数：outcome=ready。 |
| 部署预检（假子进程）／预算、缓存及失败关闭 | [trading-agent/tests/unit/test_deployment_preflight.py:18](../trading-agent/tests/unit/test_deployment_preflight.py#L18) | 分别模拟认证就绪、无效、超时和启动失败，并重复预检；预期仅就绪返回true、单次探测缓存、使用固定超时并清理进程，不把业务数据库环境交给探针。 本变体预期预检为false；失败不视为已认证。 参数：outcome=invalid。 |
| 部署预检（假子进程）／预算、缓存及失败关闭 | [trading-agent/tests/unit/test_deployment_preflight.py:18](../trading-agent/tests/unit/test_deployment_preflight.py#L18) | 分别模拟认证就绪、无效、超时和启动失败，并重复预检；预期仅就绪返回true、单次探测缓存、使用固定超时并清理进程，不把业务数据库环境交给探针。 本变体预期预检为false；失败不视为已认证。 参数：outcome=timeout。 |
| 部署预检（假子进程）／预算、缓存及失败关闭 | [trading-agent/tests/unit/test_deployment_preflight.py:18](../trading-agent/tests/unit/test_deployment_preflight.py#L18) | 分别模拟认证就绪、无效、超时和启动失败，并重复预检；预期仅就绪返回true、单次探测缓存、使用固定超时并清理进程，不把业务数据库环境交给探针。 本变体预期预检为false；失败不视为已认证。 参数：outcome=start_failure。 |
| 部署预检（假探针）／后台刷新与过期 | [trading-agent/tests/unit/test_deployment_preflight.py:52](../trading-agent/tests/unit/test_deployment_preflight.py#L52) | 推进缓存时钟并重复readiness，在后台刷新未完成时继续检查；预期ready调用有界，不重复创建刷新任务，不延长旧缓存有效期，关闭时取消刷新。 |
| 部署预检（假探针）／无请求也刷新 | [trading-agent/tests/unit/test_deployment_preflight.py:101](../trading-agent/tests/unit/test_deployment_preflight.py#L101) | 成功启动后不再调用readiness并推进时间；预期后台认证刷新仍继续，关闭时后台任务结束。 |
| 当前K线（模拟HTTP）／两币四周期精确范围 | [trading-agent/tests/unit/test_forming_candle.py:60](../trading-agent/tests/unit/test_forming_candle.py#L60) | 分别请求BTC、ETH四周期当前K线；预期只调用公开current端点并传正确合约/周期/数量，不带账户认证，当前周期和进度正确。 参数：timeframe=1h；inst_id=BTC-USDT-SWAP。 |
| 当前K线（模拟HTTP）／两币四周期精确范围 | [trading-agent/tests/unit/test_forming_candle.py:60](../trading-agent/tests/unit/test_forming_candle.py#L60) | 分别请求BTC、ETH四周期当前K线；预期只调用公开current端点并传正确合约/周期/数量，不带账户认证，当前周期和进度正确。 参数：timeframe=1h；inst_id=ETH-USDT-SWAP。 |
| 当前K线（模拟HTTP）／两币四周期精确范围 | [trading-agent/tests/unit/test_forming_candle.py:60](../trading-agent/tests/unit/test_forming_candle.py#L60) | 分别请求BTC、ETH四周期当前K线；预期只调用公开current端点并传正确合约/周期/数量，不带账户认证，当前周期和进度正确。 参数：timeframe=4h；inst_id=BTC-USDT-SWAP。 |
| 当前K线（模拟HTTP）／两币四周期精确范围 | [trading-agent/tests/unit/test_forming_candle.py:60](../trading-agent/tests/unit/test_forming_candle.py#L60) | 分别请求BTC、ETH四周期当前K线；预期只调用公开current端点并传正确合约/周期/数量，不带账户认证，当前周期和进度正确。 参数：timeframe=4h；inst_id=ETH-USDT-SWAP。 |
| 当前K线（模拟HTTP）／两币四周期精确范围 | [trading-agent/tests/unit/test_forming_candle.py:60](../trading-agent/tests/unit/test_forming_candle.py#L60) | 分别请求BTC、ETH四周期当前K线；预期只调用公开current端点并传正确合约/周期/数量，不带账户认证，当前周期和进度正确。 参数：timeframe=6h；inst_id=BTC-USDT-SWAP。 |
| 当前K线（模拟HTTP）／两币四周期精确范围 | [trading-agent/tests/unit/test_forming_candle.py:60](../trading-agent/tests/unit/test_forming_candle.py#L60) | 分别请求BTC、ETH四周期当前K线；预期只调用公开current端点并传正确合约/周期/数量，不带账户认证，当前周期和进度正确。 参数：timeframe=6h；inst_id=ETH-USDT-SWAP。 |
| 当前K线（模拟HTTP）／两币四周期精确范围 | [trading-agent/tests/unit/test_forming_candle.py:60](../trading-agent/tests/unit/test_forming_candle.py#L60) | 分别请求BTC、ETH四周期当前K线；预期只调用公开current端点并传正确合约/周期/数量，不带账户认证，当前周期和进度正确。 参数：timeframe=12h；inst_id=BTC-USDT-SWAP。 |
| 当前K线（模拟HTTP）／两币四周期精确范围 | [trading-agent/tests/unit/test_forming_candle.py:60](../trading-agent/tests/unit/test_forming_candle.py#L60) | 分别请求BTC、ETH四周期当前K线；预期只调用公开current端点并传正确合约/周期/数量，不带账户认证，当前周期和进度正确。 参数：timeframe=12h；inst_id=ETH-USDT-SWAP。 |
| 当前K线（模拟HTTP）／坏响应处理 | [trading-agent/tests/unit/test_forming_candle.py:94](../trading-agent/tests/unit/test_forming_candle.py#L94) | 分别返回空、已收盘、上一周期、未来、重复、非法OHLC或NaN；预期没有当前快照时返回缺失，非法或重复字段明确拒绝，不错选旧/未来K线。 本变体预期返回无当前快照，不把该行作为有效盘中输入。 参数：case=empty。 |
| 当前K线（模拟HTTP）／坏响应处理 | [trading-agent/tests/unit/test_forming_candle.py:94](../trading-agent/tests/unit/test_forming_candle.py#L94) | 分别返回空、已收盘、上一周期、未来、重复、非法OHLC或NaN；预期没有当前快照时返回缺失，非法或重复字段明确拒绝，不错选旧/未来K线。 本变体预期返回无当前快照，不把该行作为有效盘中输入。 参数：case=closed。 |
| 当前K线（模拟HTTP）／坏响应处理 | [trading-agent/tests/unit/test_forming_candle.py:94](../trading-agent/tests/unit/test_forming_candle.py#L94) | 分别返回空、已收盘、上一周期、未来、重复、非法OHLC或NaN；预期没有当前快照时返回缺失，非法或重复字段明确拒绝，不错选旧/未来K线。 本变体预期返回无当前快照，不把该行作为有效盘中输入。 参数：case=previous。 |
| 当前K线（模拟HTTP）／坏响应处理 | [trading-agent/tests/unit/test_forming_candle.py:94](../trading-agent/tests/unit/test_forming_candle.py#L94) | 分别返回空、已收盘、上一周期、未来、重复、非法OHLC或NaN；预期没有当前快照时返回缺失，非法或重复字段明确拒绝，不错选旧/未来K线。 本变体预期返回无当前快照，不把该行作为有效盘中输入。 参数：case=future。 |
| 当前K线（模拟HTTP）／坏响应处理 | [trading-agent/tests/unit/test_forming_candle.py:94](../trading-agent/tests/unit/test_forming_candle.py#L94) | 分别返回空、已收盘、上一周期、未来、重复、非法OHLC或NaN；预期没有当前快照时返回缺失，非法或重复字段明确拒绝，不错选旧/未来K线。 本变体预期报INVALID_FORMING_CANDLE。 参数：case=duplicate。 |
| 当前K线（模拟HTTP）／坏响应处理 | [trading-agent/tests/unit/test_forming_candle.py:94](../trading-agent/tests/unit/test_forming_candle.py#L94) | 分别返回空、已收盘、上一周期、未来、重复、非法OHLC或NaN；预期没有当前快照时返回缺失，非法或重复字段明确拒绝，不错选旧/未来K线。 本变体预期报INVALID_FORMING_CANDLE。 参数：case=invalid。 |
| 当前K线（模拟HTTP）／坏响应处理 | [trading-agent/tests/unit/test_forming_candle.py:94](../trading-agent/tests/unit/test_forming_candle.py#L94) | 分别返回空、已收盘、上一周期、未来、重复、非法OHLC或NaN；预期没有当前快照时返回缺失，非法或重复字段明确拒绝，不错选旧/未来K线。 本变体预期报INVALID_FORMING_CANDLE。 参数：case=nan。 |
| 当前K线（模拟HTTP）／请求跨收盘边界 | [trading-agent/tests/unit/test_forming_candle.py:134](../trading-agent/tests/unit/test_forming_candle.py#L134) | 让请求开始和收到响应落在不同周期；预期FORMING_CANDLE_BOUNDARY，不冻结混合期间快照。 |
| 临时指标／复用公式及质量标记 | [trading-agent/tests/unit/test_forming_candle.py:152](../trading-agent/tests/unit/test_forming_candle.py#L152) | 四周期分别以80根确认K线加1快照计算；预期26项PROVISIONAL、确认指标不变，盘中突破未获收盘确认，部分成交量比较明确未按进度归一化。 参数：timeframe=1h。 |
| 临时指标／复用公式及质量标记 | [trading-agent/tests/unit/test_forming_candle.py:152](../trading-agent/tests/unit/test_forming_candle.py#L152) | 四周期分别以80根确认K线加1快照计算；预期26项PROVISIONAL、确认指标不变，盘中突破未获收盘确认，部分成交量比较明确未按进度归一化。 参数：timeframe=4h。 |
| 临时指标／复用公式及质量标记 | [trading-agent/tests/unit/test_forming_candle.py:152](../trading-agent/tests/unit/test_forming_candle.py#L152) | 四周期分别以80根确认K线加1快照计算；预期26项PROVISIONAL、确认指标不变，盘中突破未获收盘确认，部分成交量比较明确未按进度归一化。 参数：timeframe=6h。 |
| 临时指标／复用公式及质量标记 | [trading-agent/tests/unit/test_forming_candle.py:152](../trading-agent/tests/unit/test_forming_candle.py#L152) | 四周期分别以80根确认K线加1快照计算；预期26项PROVISIONAL、确认指标不变，盘中突破未获收盘确认，部分成交量比较明确未按进度归一化。 参数：timeframe=12h。 |
| 当前K线／不能升级确认或倒用时间 | [trading-agent/tests/unit/test_forming_candle.py:191](../trading-agent/tests/unit/test_forming_candle.py#L191) | 尝试把快照改成已确认、制造无效状态或在过去决策时点计算；预期模型/schema或FORMING_CANDLE_SCOPE拒绝。 |
| Hermes加载器／并发模块清理 | [trading-agent/tests/unit/test_hermes_loader.py:18](../trading-agent/tests/unit/test_hermes_loader.py#L18) | 构造会在遍历时增加模块的字典，并运行旧遍历及修复函数；预期旧法复现字典变化异常，新法仅删目标命名空间且保留兄弟模块。 |
| Hermes加载器／两处原生绑定一致 | [trading-agent/tests/unit/test_hermes_loader.py:32](../trading-agent/tests/unit/test_hermes_loader.py#L32) | 替换固定Hermes的两个清理函数绑定后清理插件模块；预期两个绑定指向同一修复函数，目标及子模块清除。 |
| 期限学习／只改变候选期限 | [trading-agent/tests/unit/test_horizon_learning.py:21](../trading-agent/tests/unit/test_horizon_learning.py#L21) | 用同一冻结预测回放现用期限和候选期限，比较40与39样本；预期只改变候选观察期限，满足样本门槛才晋升，原预测hash和历史期限不变。 |
| 期限学习／等待成本和回滚 | [trading-agent/tests/unit/test_horizon_learning.py:40](../trading-agent/tests/unit/test_horizon_learning.py#L40) | 构造延长期限却未解决结果或使失败增加的样本；预期扣除等待成本、不晋升，达到最小回滚样本才回滚。 |
| 期限学习／缺口、歧义和未触发 | [trading-agent/tests/unit/test_horizon_learning.py:56](../trading-agent/tests/unit/test_horizon_learning.py#L56) | 分别构造分钟缺口、目标/失效歧义及未触发情景；预期缺口和歧义不冒充可评分成功，统计保留unscored及未触发比例；歧义沿用当前规则，分钟内先后问题见README待审核项。 |
| 期限学习／失败率保护 | [trading-agent/tests/unit/test_horizon_learning.py:71](../trading-agent/tests/unit/test_horizon_learning.py#L71) | 构造表面效用提升但失败率超限的候选，再将失败率调到可接受范围；预期前者禁止晋升，后者才可通过门槛。 |
| 相对指标／已知beta与相关性 | [trading-agent/tests/unit/test_indicator_review.py:43](../trading-agent/tests/unit/test_indicator_review.py#L43) | 构造ETH收益为BTC收益的线性变换，分别beta=2和-0.5；预期beta、正负相关性及20收益复合相对强弱与独立手算一致。 参数：beta=2。 |
| 相对指标／已知beta与相关性 | [trading-agent/tests/unit/test_indicator_review.py:43](../trading-agent/tests/unit/test_indicator_review.py#L43) | 构造ETH收益为BTC收益的线性变换，分别beta=2和-0.5；预期beta、正负相关性及20收益复合相对强弱与独立手算一致。 参数：beta=-0.5。 |
| 相对指标／非法BTC基准 | [trading-agent/tests/unit/test_indicator_review.py:61](../trading-agent/tests/unit/test_indicator_review.py#L61) | 分别提供未来、时间错位、外币、过短或缺失基准；预期相关性、beta及相对强弱明确MISSING而不输出伪数值。 参数：defect=future。 |
| 相对指标／非法BTC基准 | [trading-agent/tests/unit/test_indicator_review.py:61](../trading-agent/tests/unit/test_indicator_review.py#L61) | 分别提供未来、时间错位、外币、过短或缺失基准；预期相关性、beta及相对强弱明确MISSING而不输出伪数值。 参数：defect=shifted。 |
| 相对指标／非法BTC基准 | [trading-agent/tests/unit/test_indicator_review.py:61](../trading-agent/tests/unit/test_indicator_review.py#L61) | 分别提供未来、时间错位、外币、过短或缺失基准；预期相关性、beta及相对强弱明确MISSING而不输出伪数值。 参数：defect=foreign。 |
| 相对指标／非法BTC基准 | [trading-agent/tests/unit/test_indicator_review.py:61](../trading-agent/tests/unit/test_indicator_review.py#L61) | 分别提供未来、时间错位、外币、过短或缺失基准；预期相关性、beta及相对强弱明确MISSING而不输出伪数值。 参数：defect=short。 |
| 相对指标／非法BTC基准 | [trading-agent/tests/unit/test_indicator_review.py:61](../trading-agent/tests/unit/test_indicator_review.py#L61) | 分别提供未来、时间错位、外币、过短或缺失基准；预期相关性、beta及相对强弱明确MISSING而不输出伪数值。 参数：defect=absent。 |
| 相对指标／恒定基准与自比较 | [trading-agent/tests/unit/test_indicator_review.py:83](../trading-agent/tests/unit/test_indicator_review.py#L83) | 用BTC零收益基准计算ETH相对指标，再计算BTC对自身；预期相关性/beta为DEGENERATE，相对收益可独立有效，自比较正常NOT_APPLICABLE。 |
| 相对指标／浮点噪声不放大beta | [trading-agent/tests/unit/test_indicator_review.py:94](../trading-agent/tests/unit/test_indicator_review.py#L94) | 构造恒定复利BTC收益及波动ETH收益；预期微小舍入方差不生成巨大beta，相关性和beta无值并DEGENERATE，相对强弱仍可计算。 参数：rate=0.002。 |
| 相对指标／浮点噪声不放大beta | [trading-agent/tests/unit/test_indicator_review.py:94](../trading-agent/tests/unit/test_indicator_review.py#L94) | 构造恒定复利BTC收益及波动ETH收益；预期微小舍入方差不生成巨大beta，相关性和beta无值并DEGENERATE，相对强弱仍可计算。 参数：rate=0.003。 |
| 相对指标／浮点噪声不放大beta | [trading-agent/tests/unit/test_indicator_review.py:94](../trading-agent/tests/unit/test_indicator_review.py#L94) | 构造恒定复利BTC收益及波动ETH收益；预期微小舍入方差不生成巨大beta，相关性和beta无值并DEGENERATE，相对强弱仍可计算。 参数：rate=0.005。 |
| 资金费率／当前快照与结算历史分时点 | [trading-agent/tests/unit/test_indicator_review.py:120](../trading-agent/tests/unit/test_indicator_review.py#L120) | 在4h收盘后加入当前预计费率、新结算及未来才可见快照；预期当前费率用决策时可见发布值，历史zscore仍按收盘截止，不混用未来数据。 |
| 资金费率／修订去重与未来隔离 | [trading-agent/tests/unit/test_indicator_review.py:148](../trading-agent/tests/unit/test_indicator_review.py#L148) | 混入同次结算不同修订及未来结算；预期选冻结时点最新可见修订、只展示规定历史数量，不把观测间隔当已验证结算周期。 |
| 资金费率／缺期不擅自归一化 | [trading-agent/tests/unit/test_indicator_review.py:162](../trading-agent/tests/unit/test_indicator_review.py#L162) | 在8小时样例序列中去掉一次结算；预期间隔变长时降部分覆盖，原始费率保留，暂停可比均值/变化，不擅自除二或换算每8小时费率。 |
| 旧统计兼容解析（预设数据）／长周期起点口径 | [trading-agent/tests/unit/test_indicator_review.py:179](../trading-agent/tests/unit/test_indicator_review.py#L179) | 对4h、6h、12h注入标记区间起点的完整主动成交、OI和多空比；预期只含结束桶，买卖方向、币数量单位、CVD、比例及OI变化符合已知值。 参数：timeframe=4h。 |
| 旧统计兼容解析（预设数据）／长周期起点口径 | [trading-agent/tests/unit/test_indicator_review.py:179](../trading-agent/tests/unit/test_indicator_review.py#L179) | 对4h、6h、12h注入标记区间起点的完整主动成交、OI和多空比；预期只含结束桶，买卖方向、币数量单位、CVD、比例及OI变化符合已知值。 参数：timeframe=6h。 |
| 旧统计兼容解析（预设数据）／长周期起点口径 | [trading-agent/tests/unit/test_indicator_review.py:179](../trading-agent/tests/unit/test_indicator_review.py#L179) | 对4h、6h、12h注入标记区间起点的完整主动成交、OI和多空比；预期只含结束桶，买卖方向、币数量单位、CVD、比例及OI变化符合已知值。 参数：timeframe=12h。 |
| 统计采集（模拟HTTP）／单周期请求范围 | [trading-agent/tests/unit/test_indicator_review.py:218](../trading-agent/tests/unit/test_indicator_review.py#L218) | 仅请求ETH的4h背景；预期保持指定anchor、合约及4H参数，有界请求次数，不无故扇出其他周期。 |
| 统计采集（模拟HTTP）／缺失原因区分 | [trading-agent/tests/unit/test_indicator_review.py:270](../trading-agent/tests/unit/test_indicator_review.py#L270) | 分别返回空、未完成桶、非法数字或到期；预期MISSING并记录SOURCE_EMPTY、NO_COMPLETED_STATISTICS_BUCKET、SOURCE_INVALID或MARKET_DEADLINE。 参数：failure=empty；expected=SOURCE_EMPTY。 |
| 统计采集（模拟HTTP）／缺失原因区分 | [trading-agent/tests/unit/test_indicator_review.py:270](../trading-agent/tests/unit/test_indicator_review.py#L270) | 分别返回空、未完成桶、非法数字或到期；预期MISSING并记录SOURCE_EMPTY、NO_COMPLETED_STATISTICS_BUCKET、SOURCE_INVALID或MARKET_DEADLINE。 参数：failure=open_bucket；expected=NO_COMPLETED_STATISTICS_BUCKET。 |
| 统计采集（模拟HTTP）／缺失原因区分 | [trading-agent/tests/unit/test_indicator_review.py:270](../trading-agent/tests/unit/test_indicator_review.py#L270) | 分别返回空、未完成桶、非法数字或到期；预期MISSING并记录SOURCE_EMPTY、NO_COMPLETED_STATISTICS_BUCKET、SOURCE_INVALID或MARKET_DEADLINE。 参数：failure=invalid_number；expected=SOURCE_INVALID。 |
| 统计采集（模拟HTTP）／缺失原因区分 | [trading-agent/tests/unit/test_indicator_review.py:270](../trading-agent/tests/unit/test_indicator_review.py#L270) | 分别返回空、未完成桶、非法数字或到期；预期MISSING并记录SOURCE_EMPTY、NO_COMPLETED_STATISTICS_BUCKET、SOURCE_INVALID或MARKET_DEADLINE。 参数：failure=deadline；expected=MARKET_DEADLINE。 |
| 模型上下文／BTC相对信息弱证据 | [trading-agent/tests/unit/test_indicator_review.py:306](../trading-agent/tests/unit/test_indicator_review.py#L306) | 生成子上下文与父综合视图；预期三项BTC相对指标显式WEAK并禁止单独确认方向，父没有新增自身行情输入。 |
| 旧统计兼容校验／逐桶成交量一致 | [trading-agent/tests/unit/test_indicator_review.py:362](../trading-agent/tests/unit/test_indicator_review.py#L362) | 提供每桶主动买卖量与K线一致的完整窗口；预期CVD和买卖比为OK且值与已知汇总相同。 |
| 旧统计兼容校验／逐桶不一致不能相互抵消 | [trading-agent/tests/unit/test_indicator_review.py:372](../trading-agent/tests/unit/test_indicator_review.py#L372) | 分别让某桶少一半或让两桶偏差抵消总量；预期逐桶发现不一致、降PARTIAL_COVERAGE，保留原始买卖分配，不猜测缺额方向。 参数：defect=missing_half_bucket。 |
| 旧统计兼容校验／逐桶不一致不能相互抵消 | [trading-agent/tests/unit/test_indicator_review.py:372](../trading-agent/tests/unit/test_indicator_review.py#L372) | 分别让某桶少一半或让两桶偏差抵消总量；预期逐桶发现不一致、降PARTIAL_COVERAGE，保留原始买卖分配，不猜测缺额方向。 参数：defect=cancelling_bucket_errors。 |
| 强平解析／单位、去重和乱序 | [trading-agent/tests/unit/test_liquidations.py:39](../trading-agent/tests/unit/test_liquidations.py#L39) | 输入重复及乱序公开强平明细；预期按内容去重，合约张数和被平多空方向正确，不用到达顺序冒充事件时间。 |
| 强平解析／坏事件拒绝 | [trading-agent/tests/unit/test_liquidations.py:58](../trading-agent/tests/unit/test_liquidations.py#L58) | 分别注入未来时间、NaN、矛盾方向、私有消息格式、负数量或空价格；预期拒绝为不可用样本。 参数：change=future。 |
| 强平解析／坏事件拒绝 | [trading-agent/tests/unit/test_liquidations.py:58](../trading-agent/tests/unit/test_liquidations.py#L58) | 分别注入未来时间、NaN、矛盾方向、私有消息格式、负数量或空价格；预期拒绝为不可用样本。 参数：change=nan。 |
| 强平解析／坏事件拒绝 | [trading-agent/tests/unit/test_liquidations.py:58](../trading-agent/tests/unit/test_liquidations.py#L58) | 分别注入未来时间、NaN、矛盾方向、私有消息格式、负数量或空价格；预期拒绝为不可用样本。 参数：change=direction。 |
| 强平解析／坏事件拒绝 | [trading-agent/tests/unit/test_liquidations.py:58](../trading-agent/tests/unit/test_liquidations.py#L58) | 分别注入未来时间、NaN、矛盾方向、私有消息格式、负数量或空价格；预期拒绝为不可用样本。 参数：change=private。 |
| 强平解析／坏事件拒绝 | [trading-agent/tests/unit/test_liquidations.py:58](../trading-agent/tests/unit/test_liquidations.py#L58) | 分别注入未来时间、NaN、矛盾方向、私有消息格式、负数量或空价格；预期拒绝为不可用样本。 参数：change=negative。 |
| 强平解析／坏事件拒绝 | [trading-agent/tests/unit/test_liquidations.py:58](../trading-agent/tests/unit/test_liquidations.py#L58) | 分别注入未来时间、NaN、矛盾方向、私有消息格式、负数量或空价格；预期拒绝为不可用样本。 参数：change=empty-price。 |
| 强平覆盖／断线不能被连接段填平 | [trading-agent/tests/unit/test_liquidations.py:77](../trading-agent/tests/unit/test_liquidations.py#L77) | 给出有间隙、无覆盖及超出窗口的连接段；预期只合并真实覆盖，断开区间和完全无覆盖如实返回。 |
| 强平采样配置／公开安全URL | [trading-agent/tests/unit/test_liquidations.py:96](../trading-agent/tests/unit/test_liquidations.py#L96) | 尝试私有频道、明文ws、含认证信息或查询参数的URL；预期配置校验拒绝，仅允许规定的无凭据公开wss频道。 参数：url=wss://ws.okx.com:8443/ws/v5/private。 |
| 强平采样配置／公开安全URL | [trading-agent/tests/unit/test_liquidations.py:96](../trading-agent/tests/unit/test_liquidations.py#L96) | 尝试私有频道、明文ws、含认证信息或查询参数的URL；预期配置校验拒绝，仅允许规定的无凭据公开wss频道。 参数：url=ws://ws.okx.com/ws/v5/public。 |
| 强平采样配置／公开安全URL | [trading-agent/tests/unit/test_liquidations.py:96](../trading-agent/tests/unit/test_liquidations.py#L96) | 尝试私有频道、明文ws、含认证信息或查询参数的URL；预期配置校验拒绝，仅允许规定的无凭据公开wss频道。 参数：url=wss URL（含用户认证，认证内容省略）。 |
| 强平采样配置／公开安全URL | [trading-agent/tests/unit/test_liquidations.py:96](../trading-agent/tests/unit/test_liquidations.py#L96) | 尝试私有频道、明文ws、含认证信息或查询参数的URL；预期配置校验拒绝，仅允许规定的无凭据公开wss频道。 参数：url=wss URL（含查询参数，参数内容省略）。 |
| 强平金额／合约面值依据 | [trading-agent/tests/unit/test_liquidations.py:101](../trading-agent/tests/unit/test_liquidations.py#L101) | 提供合法线性和反向合约元数据，再篡改面值币种、乘数等字段；预期仅明确支持的合约可计算金额，缺失或不支持规格拒绝。 |
| 行情时间／UTC边界和窗口 | [trading-agent/tests/unit/test_market.py:18](../trading-agent/tests/unit/test_market.py#L18) | 用固定时点计算6h、12h边界及观察窗口；预期返回规定UTC边界及明确起止时间。 |
| 行情快照／数量与内容hash | [trading-agent/tests/unit/test_market.py:24](../trading-agent/tests/unit/test_market.py#L24) | 冻结相同K线窗口两次，再改变源内容；预期每周期80根、展示30根，相同输入内容hash复用，采集包hash可不同，源改变则输入hash改变。 |
| 行情快照／缺口、重复及未来数据 | [trading-agent/tests/unit/test_market.py:40](../trading-agent/tests/unit/test_market.py#L40) | 分别构造时间缺口、重复K线及冻结时点之后数据；预期schema或领域校验拒绝。 |
| 技术指标／手算基线 | [trading-agent/tests/unit/test_market.py:52](../trading-agent/tests/unit/test_market.py#L52) | 用可手算的80根递增K线计算收益、RSI、DI、价格位置等；预期与已知数值一致，BTC自相关不适用，缺主动成交时不编造CVD。 |
| 技术指标／平价退化数据 | [trading-agent/tests/unit/test_market.py:70](../trading-agent/tests/unit/test_market.py#L70) | 构造完全平价、无波动K线；预期RSI=50、ATR比例=0，无定义指标标DEGENERATE且为空，不伪造信号。 |
| 配置与序列化／失败关闭及数值hash | [trading-agent/tests/unit/test_market.py:79](../trading-agent/tests/unit/test_market.py#L79) | 输入非法配置、非有限数值或不支持对象，并比较不同Decimal尾零；预期非法输入拒绝，相同数值的规范hash一致。 |
| 行情客户端（模拟HTTP）／周期映射与确认 | [trading-agent/tests/unit/test_market.py:93](../trading-agent/tests/unit/test_market.py#L93) | 提供包含未确认K线的预设响应并获取精确报价；预期6h映射6Hutc、只接受确认数据、缺报价不猜值，非公开路径被拒。 |
| 合约元数据（模拟HTTP）／配置一致性 | [trading-agent/tests/unit/test_market.py:121](../trading-agent/tests/unit/test_market.py#L121) | 分别将合约类型、结算币或状态改错；预期INSTRUMENT_MISMATCH或INSTRUMENT_UNAVAILABLE，不把其他规格当本合约。 参数：field=instType；value=FUTURES。 |
| 合约元数据（模拟HTTP）／配置一致性 | [trading-agent/tests/unit/test_market.py:121](../trading-agent/tests/unit/test_market.py#L121) | 分别将合约类型、结算币或状态改错；预期INSTRUMENT_MISMATCH或INSTRUMENT_UNAVAILABLE，不把其他规格当本合约。 参数：field=settleCcy；value=BTC。 |
| 合约元数据（模拟HTTP）／配置一致性 | [trading-agent/tests/unit/test_market.py:121](../trading-agent/tests/unit/test_market.py#L121) | 分别将合约类型、结算币或状态改错；预期INSTRUMENT_MISMATCH或INSTRUMENT_UNAVAILABLE，不把其他规格当本合约。 参数：field=state；value=suspend。 |
| 资金费率入库解析／预计快照时点 | [trading-agent/tests/unit/test_observation_ingestion.py:10](../trading-agent/tests/unit/test_observation_ingestion.py#L10) | 解析当前发布的预计费率并混入错误合约；预期event_at和available_at为真实发布/采集时点，未来结算时间不当当前事件，错标的拒绝。 |
| 资金费率入库解析／历史结算可用时间 | [trading-agent/tests/unit/test_observation_ingestion.py:28](../trading-agent/tests/unit/test_observation_ingestion.py#L28) | 在较晚采集时读取旧结算；预期event_at为结算时间、available_at保留实际取得时间，标realized而非published，不回溯伪造当时可用。 |
| 资金费率上下文／四周期当前快照 | [trading-agent/tests/unit/test_observations.py:17](../trading-agent/tests/unit/test_observations.py#L17) | 给各周期相同决策时可见的当前预计费率并序列化往返；预期均可作为当前背景，保留事件ID、时点及快照hash。 |
| 主动成交窗口／完整与缺口 | [trading-agent/tests/unit/test_observations.py:46](../trading-agent/tests/unit/test_observations.py#L46) | 提供20个完整桶后移除一个；预期完整CVD及买卖比等于手算，缺桶降部分覆盖且不输出不完整CVD。 |
| 外部观测／未来隔离与必要指标门禁 | [trading-agent/tests/unit/test_observations.py:71](../trading-agent/tests/unit/test_observations.py#L71) | 注入未来才可用的费率并令必要指标缺失；预期未来观测不使用，缺必需特征时拒绝冻结。 |
| 引擎身份／路径变化不改分析身份 | [trading-agent/tests/unit/test_observations.py:95](../trading-agent/tests/unit/test_observations.py#L95) | 只变更服务器安装路径，再改变真正分析配置；预期迁移路径不改engine hash，分析规则改变会改hash。 |
| 数据精度／拒绝静默数据库舍入 | [trading-agent/tests/unit/test_observations.py:121](../trading-agent/tests/unit/test_observations.py#L121) | 构造超过允许精度的源数值；预期模型校验拒绝，不能进入PG后静默截断。 |
| 日志／安全异常栈 | [trading-agent/tests/unit/test_operations.py:11](../trading-agent/tests/unit/test_operations.py#L11) | 抛出包含测试秘密和路径的异常并格式化日志；预期只保存异常类型、安全栈帧及计时，不输出异常原文或敏感路径。 |
| 日志／磁盘故障可见 | [trading-agent/tests/unit/test_operations.py:37](../trading-agent/tests/unit/test_operations.py#L37) | 让日志写盘失败；预期健康状态明确write_failures，标准输出不泄露业务payload或秘密。 |
| 日志／队列脱敏及稳定标识 | [trading-agent/tests/unit/test_operations.py:64](../trading-agent/tests/unit/test_operations.py#L64) | 向有限日志队列写入含秘密、原始令牌、URL或路径的记录并触发丢弃；预期脱敏、计数正确，保留request_id及安全错误码。 |
| CLI／子命令后JSON选项 | [trading-agent/tests/unit/test_operations.py:105](../trading-agent/tests/unit/test_operations.py#L105) | 分别在状态、预测、暂停、配置、doctor或复盘子命令之后追加--json；预期参数解析均设置json=true。 参数：words=[status]。 |
| CLI／子命令后JSON选项 | [trading-agent/tests/unit/test_operations.py:105](../trading-agent/tests/unit/test_operations.py#L105) | 分别在状态、预测、暂停、配置、doctor或复盘子命令之后追加--json；预期参数解析均设置json=true。 参数：words=[forecast，latest]。 |
| CLI／子命令后JSON选项 | [trading-agent/tests/unit/test_operations.py:105](../trading-agent/tests/unit/test_operations.py#L105) | 分别在状态、预测、暂停、配置、doctor或复盘子命令之后追加--json；预期参数解析均设置json=true。 参数：words=[schedule，pause]。 |
| CLI／子命令后JSON选项 | [trading-agent/tests/unit/test_operations.py:105](../trading-agent/tests/unit/test_operations.py#L105) | 分别在状态、预测、暂停、配置、doctor或复盘子命令之后追加--json；预期参数解析均设置json=true。 参数：words=[config，check]。 |
| CLI／子命令后JSON选项 | [trading-agent/tests/unit/test_operations.py:105](../trading-agent/tests/unit/test_operations.py#L105) | 分别在状态、预测、暂停、配置、doctor或复盘子命令之后追加--json；预期参数解析均设置json=true。 参数：words=[doctor，--notify]。 |
| CLI／子命令后JSON选项 | [trading-agent/tests/unit/test_operations.py:105](../trading-agent/tests/unit/test_operations.py#L105) | 分别在状态、预测、暂停、配置、doctor或复盘子命令之后追加--json；预期参数解析均设置json=true。 参数：words=[review，show，<private-id>]。 |
| CLI／参数错误不回显私密值 | [trading-agent/tests/unit/test_operations.py:109](../trading-agent/tests/unit/test_operations.py#L109) | 传入含敏感测试字串的非法参数并要求JSON错误；预期退出码2及CLI_ARGUMENT_INVALID，不回显非法原值。 |
| runtime凭据边界／拒绝人工与管理凭据 | [trading-agent/tests/unit/test_operations.py:132](../trading-agent/tests/unit/test_operations.py#L132) | 逐一向runtime环境注入操作员、主控原始令牌或迁移/备份/恢复连接变量；预期构造依赖前报RUNTIME_SECRET_SCOPE，错误不泄露变量值。 参数：name=TA_OPERATOR_TOKEN。 |
| runtime凭据边界／拒绝人工与管理凭据 | [trading-agent/tests/unit/test_operations.py:132](../trading-agent/tests/unit/test_operations.py#L132) | 逐一向runtime环境注入操作员、主控原始令牌或迁移/备份/恢复连接变量；预期构造依赖前报RUNTIME_SECRET_SCOPE，错误不泄露变量值。 参数：name=TA_CONTROLLER_TOKEN。 |
| runtime凭据边界／拒绝人工与管理凭据 | [trading-agent/tests/unit/test_operations.py:132](../trading-agent/tests/unit/test_operations.py#L132) | 逐一向runtime环境注入操作员、主控原始令牌或迁移/备份/恢复连接变量；预期构造依赖前报RUNTIME_SECRET_SCOPE，错误不泄露变量值。 参数：name=TA_MIGRATION_DATABASE_URL。 |
| runtime凭据边界／拒绝人工与管理凭据 | [trading-agent/tests/unit/test_operations.py:132](../trading-agent/tests/unit/test_operations.py#L132) | 逐一向runtime环境注入操作员、主控原始令牌或迁移/备份/恢复连接变量；预期构造依赖前报RUNTIME_SECRET_SCOPE，错误不泄露变量值。 参数：name=TA_BACKUP_DATABASE_URL。 |
| runtime凭据边界／拒绝人工与管理凭据 | [trading-agent/tests/unit/test_operations.py:132](../trading-agent/tests/unit/test_operations.py#L132) | 逐一向runtime环境注入操作员、主控原始令牌或迁移/备份/恢复连接变量；预期构造依赖前报RUNTIME_SECRET_SCOPE，错误不泄露变量值。 参数：name=TA_RESTORE_DATABASE_URL。 |
| 本机进程恢复／归属验证后终止 | [trading-agent/tests/unit/test_orphan_processes.py:11](../trading-agent/tests/unit/test_orphan_processes.py#L11) | 创建测试进程，依次用错误出生时间、令牌归属或工作目录申请回收；预期不误杀，只有全部归属条件匹配才终止。 |
| 本机进程恢复／脱离父进程的子孙 | [trading-agent/tests/unit/test_orphan_processes.py:46](../trading-agent/tests/unit/test_orphan_processes.py#L46) | 让测试进程派生子进程后父退出，分别有无已知出生时间回收；预期找到本任务脱离进程并终止，无关进程继续运行。 参数：unknown_birth=否。 |
| 本机进程恢复／脱离父进程的子孙 | [trading-agent/tests/unit/test_orphan_processes.py:46](../trading-agent/tests/unit/test_orphan_processes.py#L46) | 让测试进程派生子进程后父退出，分别有无已知出生时间回收；预期找到本任务脱离进程并终止，无关进程继续运行。 参数：unknown_birth=是。 |
| paper服务守卫／构造传输前拒绝live | [trading-agent/tests/unit/test_paper_service_guard.py:15](../trading-agent/tests/unit/test_paper_service_guard.py#L15) | 组合执行模式与传输声明；预期live状态在构造传输前拒绝，paper但传输live也拒绝，仅paper＋paper通过。 本变体预期守卫拒绝。 参数：mode=live；transport_mode=paper。 |
| paper服务守卫／构造传输前拒绝live | [trading-agent/tests/unit/test_paper_service_guard.py:15](../trading-agent/tests/unit/test_paper_service_guard.py#L15) | 组合执行模式与传输声明；预期live状态在构造传输前拒绝，paper但传输live也拒绝，仅paper＋paper通过。 本变体预期守卫拒绝。 参数：mode=paper；transport_mode=live。 |
| paper服务守卫／构造传输前拒绝live | [trading-agent/tests/unit/test_paper_service_guard.py:15](../trading-agent/tests/unit/test_paper_service_guard.py#L15) | 组合执行模式与传输声明；预期live状态在构造传输前拒绝，paper但传输live也拒绝，仅paper＋paper通过。 本变体预期守卫通过。 参数：mode=paper；transport_mode=paper。 |
| 分钟行情（模拟HTTP）／旧分钟优先分页 | [trading-agent/tests/unit/test_prediction_market_client.py:9](../trading-agent/tests/unit/test_prediction_market_client.py#L9) | 按多页获取ETH分钟并制造窗口缺口；预期从最旧待补处读取、边界过滤和标的正确，缺口保留供后续重试。 |
| 交易传输（假交易所）／稳定订单ID及恢复查询 | [trading-agent/tests/unit/test_prediction_transport.py:73](../trading-agent/tests/unit/test_prediction_transport.py#L73) | 准备开仓保护单后模拟成交、查询、取消和查询未找到；预期稳定客户端ID、止盈止损正确，费用保存，只发送一次，不因查询未知重下单。 |
| 交易传输（假交易所）／按剩余张数平仓 | [trading-agent/tests/unit/test_prediction_transport.py:94](../trading-agent/tests/unit/test_prediction_transport.py#L94) | 让权益比例计算无法用于新开仓，再关闭已有0.37张仓位；预期仍按剩余张数只减仓，不设置新开仓杠杆或附加保护单。 |
| 交易传输（假交易所）／错误订单和费用币种 | [trading-agent/tests/unit/test_prediction_transport.py:112](../trading-agent/tests/unit/test_prediction_transport.py#L112) | 给恢复查询返回不属于本订单的成交，或返回不支持的费用币种；预期拒绝解析和记账，不混入其他订单/无法换算费用。 |
| 本机子进程／输出有界读取 | [trading-agent/tests/unit/test_processes.py:10](../trading-agent/tests/unit/test_processes.py#L10) | 测试进程同时大量输出stdout和stderr；预期两路持续排空、不死锁，只保留各4096字节并明确truncated。 |
| 本机子进程／超时杀进程组 | [trading-agent/tests/unit/test_processes.py:18](../trading-agent/tests/unit/test_processes.py#L18) | 测试进程派生子进程后超时，同时运行事件循环心跳；预期超时异常、父子均终止，事件循环不被阻塞。 |
| 研究子进程／不继承宿主凭据 | [trading-agent/tests/unit/test_processes.py:42](../trading-agent/tests/unit/test_processes.py#L42) | 在宿主放入人工令牌、PG密码、模型API键等测试环境变量，再构建研究环境；预期只保留明确授予的本次研究令牌，宿主凭据被剔除。 |
| 本机子进程／已回收短进程结果 | [trading-agent/tests/unit/test_processes.py:57](../trading-agent/tests/unit/test_processes.py#L57) | 启动后立即退出并被回收的测试进程，再读取结果；预期保留真实退出码和输出，不向未知复用PID发信号。 |
| 共享量化／迁移前全量数值基线 | [trading-agent/tests/unit/test_quant_owner.py:20](../trading-agent/tests/unit/test_quant_owner.py#L20) | 遍历四周期及平价/趋势输入，与移动到共享库之前的冻结输出摘要比较；预期整个v1数值与质量基线一致。 参数：flat=否；timeframe=1h。 |
| 共享量化／迁移前全量数值基线 | [trading-agent/tests/unit/test_quant_owner.py:20](../trading-agent/tests/unit/test_quant_owner.py#L20) | 遍历四周期及平价/趋势输入，与移动到共享库之前的冻结输出摘要比较；预期整个v1数值与质量基线一致。 参数：flat=否；timeframe=4h。 |
| 共享量化／迁移前全量数值基线 | [trading-agent/tests/unit/test_quant_owner.py:20](../trading-agent/tests/unit/test_quant_owner.py#L20) | 遍历四周期及平价/趋势输入，与移动到共享库之前的冻结输出摘要比较；预期整个v1数值与质量基线一致。 参数：flat=否；timeframe=6h。 |
| 共享量化／迁移前全量数值基线 | [trading-agent/tests/unit/test_quant_owner.py:20](../trading-agent/tests/unit/test_quant_owner.py#L20) | 遍历四周期及平价/趋势输入，与移动到共享库之前的冻结输出摘要比较；预期整个v1数值与质量基线一致。 参数：flat=否；timeframe=12h。 |
| 共享量化／迁移前全量数值基线 | [trading-agent/tests/unit/test_quant_owner.py:20](../trading-agent/tests/unit/test_quant_owner.py#L20) | 遍历四周期及平价/趋势输入，与移动到共享库之前的冻结输出摘要比较；预期整个v1数值与质量基线一致。 参数：flat=是；timeframe=1h。 |
| 共享量化／迁移前全量数值基线 | [trading-agent/tests/unit/test_quant_owner.py:20](../trading-agent/tests/unit/test_quant_owner.py#L20) | 遍历四周期及平价/趋势输入，与移动到共享库之前的冻结输出摘要比较；预期整个v1数值与质量基线一致。 参数：flat=是；timeframe=4h。 |
| 共享量化／迁移前全量数值基线 | [trading-agent/tests/unit/test_quant_owner.py:20](../trading-agent/tests/unit/test_quant_owner.py#L20) | 遍历四周期及平价/趋势输入，与移动到共享库之前的冻结输出摘要比较；预期整个v1数值与质量基线一致。 参数：flat=是；timeframe=6h。 |
| 共享量化／迁移前全量数值基线 | [trading-agent/tests/unit/test_quant_owner.py:20](../trading-agent/tests/unit/test_quant_owner.py#L20) | 遍历四周期及平价/趋势输入，与移动到共享库之前的冻结输出摘要比较；预期整个v1数值与质量基线一致。 参数：flat=是；timeframe=12h。 |
| 共享量化／扩展契约与共用入口 | [trading-agent/tests/unit/test_quant_owner.py:27](../trading-agent/tests/unit/test_quant_owner.py#L27) | 比较旧引擎身份，再从共享计算入口和runtime适配入口计算同批数据；预期新观测契约改变身份，两入口指标名称、数值、质量一致。 |
| 安装入口／唯一结构runtime | [trading-agent/tests/unit/test_runtime_packaging.py:16](../trading-agent/tests/unit/test_runtime_packaging.py#L16) | 尝试旧模式、旧命令并解析当前serve；预期只接受结构模式和现有CLI，退役入口不可用。 |
| 风控配置兼容／拒绝禁用既有参数体系 | [trading-agent/tests/unit/test_runtime_packaging.py:27](../trading-agent/tests/unit/test_runtime_packaging.py#L27) | 给禁用现有参数计算方式的配置，再检查合法默认参数；预期RISK_ADAPTER_UNSUPPORTED，不能悄悄改用简化风控。 |
| 安装包／不依赖归档框架 | [trading-agent/tests/unit/test_runtime_packaging.py:39](../trading-agent/tests/unit/test_runtime_packaging.py#L39) | 在受控安装路径中加载runtime并检查包依赖、已导入模块和CLI；预期只依赖trading-core等当前包，不导入旧交易框架或研究模块，不恢复旧CLI。 |
| 沙箱策略／正式与复盘网络边界 | [trading-agent/tests/unit/test_sandboxes.py:14](../trading-agent/tests/unit/test_sandboxes.py#L14) | 分别生成正式预测及解释/复盘网络规则，加入敏感公网地址；预期正式deny-all且无allow漏洞，研究仅允许受限公网、拒绝私网/元数据及指定敏感地址。 |
| 沙箱输入桥／文件白名单 | [trading-agent/tests/unit/test_sandboxes.py:27](../trading-agent/tests/unit/test_sandboxes.py#L27) | 输入目录放符号链接或未知宿主文件后枚举；预期拒绝，不将越界文件复制到沙箱。 |
| 沙箱网络／IPv4 SDK字段拒绝IPv6 | [trading-agent/tests/unit/test_sandboxes.py:42](../trading-agent/tests/unit/test_sandboxes.py#L42) | 将IPv6 CIDR放入只支持IPv4的deny字段；预期SANDBOX_NETWORK_INVALID，不默默忽略规则。 |
| 沙箱配置／续租与清理时间余量 | [trading-agent/tests/unit/test_sandboxes.py:48](../trading-agent/tests/unit/test_sandboxes.py#L48) | 配置不足以覆盖控制调用、续租及退出清理的超时；预期配置校验拒绝。 |
| 沙箱资源（假SDK）／磁盘测量门禁 | [trading-agent/tests/unit/test_sandboxes.py:59](../trading-agent/tests/unit/test_sandboxes.py#L59) | 返回超限容量、0或未知磁盘测量值；预期CUBE_RESOURCE_LIMITS，不将未知容量视为可用。 参数：capacity=8192。 |
| 沙箱资源（假SDK）／磁盘测量门禁 | [trading-agent/tests/unit/test_sandboxes.py:59](../trading-agent/tests/unit/test_sandboxes.py#L59) | 返回超限容量、0或未知磁盘测量值；预期CUBE_RESOURCE_LIMITS，不将未知容量视为可用。 参数：capacity=0。 |
| 沙箱资源（假SDK）／磁盘测量门禁 | [trading-agent/tests/unit/test_sandboxes.py:59](../trading-agent/tests/unit/test_sandboxes.py#L59) | 返回超限容量、0或未知磁盘测量值；预期CUBE_RESOURCE_LIMITS，不将未知容量视为可用。 参数：capacity=unknown。 |
| 沙箱认证（模拟HTTP）／输入前强制鉴权 | [trading-agent/tests/unit/test_sandboxes.py:83](../trading-agent/tests/unit/test_sandboxes.py#L83) | 组合认证初始化成功/失败与未认证探测200/401；预期只有初始化成功且无认证被拒绝时才暴露任务数据，否则清理令牌并报CUBE_GUEST_AUTH_FAILED。 本变体预期准备拒绝，不能暴露任务输入。 参数：init_status=500；probe_status=401。 |
| 沙箱认证（模拟HTTP）／输入前强制鉴权 | [trading-agent/tests/unit/test_sandboxes.py:83](../trading-agent/tests/unit/test_sandboxes.py#L83) | 组合认证初始化成功/失败与未认证探测200/401；预期只有初始化成功且无认证被拒绝时才暴露任务数据，否则清理令牌并报CUBE_GUEST_AUTH_FAILED。 本变体预期准备拒绝，不能暴露任务输入。 参数：init_status=204；probe_status=200。 |
| 沙箱认证（模拟HTTP）／输入前强制鉴权 | [trading-agent/tests/unit/test_sandboxes.py:83](../trading-agent/tests/unit/test_sandboxes.py#L83) | 组合认证初始化成功/失败与未认证探测200/401；预期只有初始化成功且无认证被拒绝时才暴露任务数据，否则清理令牌并报CUBE_GUEST_AUTH_FAILED。 本变体预期认证初始化成功且无认证访问401，准备通过。 参数：init_status=204；probe_status=401。 |
| 沙箱管理（假HTTP）／总墙钟上限 | [trading-agent/tests/unit/test_sandboxes.py:130](../trading-agent/tests/unit/test_sandboxes.py#L130) | 令控制请求无法在总预算内完成；预期有界超时并报CUBE_UNAVAILABLE，不无限挂起。 |
| 沙箱readiness（假HTTP）／客户端与服务端认证 | [trading-agent/tests/unit/test_sandboxes.py:159](../trading-agent/tests/unit/test_sandboxes.py#L159) | 组合有效/无效key及服务端是否真正强制认证；预期只有key可用且服务端拒绝无认证访问时ready=true。 本变体预期ready=true。 参数：enforced=是；valid_key=是。 |
| 沙箱readiness（假HTTP）／客户端与服务端认证 | [trading-agent/tests/unit/test_sandboxes.py:159](../trading-agent/tests/unit/test_sandboxes.py#L159) | 组合有效/无效key及服务端是否真正强制认证；预期只有key可用且服务端拒绝无认证访问时ready=true。 本变体预期ready=false。 参数：enforced=否；valid_key=是。 |
| 沙箱readiness（假HTTP）／客户端与服务端认证 | [trading-agent/tests/unit/test_sandboxes.py:159](../trading-agent/tests/unit/test_sandboxes.py#L159) | 组合有效/无效key及服务端是否真正强制认证；预期只有key可用且服务端拒绝无认证访问时ready=true。 本变体预期ready=false。 参数：enforced=是；valid_key=否。 |
| 旧统计解析／方向、单位与时间契约 | [trading-agent/tests/unit/test_statistics.py:21](../trading-agent/tests/unit/test_statistics.py#L21) | 解析20个主动成交桶并比较open/close时间口径；预期卖买字段不颠倒、单位为币数量，只用已结束窗口，CVD和买卖比与手算一致。 |
| 外部统计／周期和OI时点不能混用 | [trading-agent/tests/unit/test_statistics.py:47](../trading-agent/tests/unit/test_statistics.py#L47) | 混入不同周期OI与多空比或未验证的边界估计；预期仅正确对齐的周期可计算，不合格输入MISSING。 |
| 外部统计／1h更新不污染长周期 | [trading-agent/tests/unit/test_statistics.py:83](../trading-agent/tests/unit/test_statistics.py#L83) | 更新小时统计但高周期桶尚未结束；预期1h输入hash变化，未结束的长周期不引入该观测且原hash保持。 |
| 旧统计解析／非法源行拒绝 | [trading-agent/tests/unit/test_statistics.py:125](../trading-agent/tests/unit/test_statistics.py#L125) | 逐一输入非法时间、布尔/小数时间、超大时间、坏数值、NaN、负量、缺列或重复桶；预期领域或数值校验拒绝。 参数：rows=[[fixture-invalid-time，1，2]]。 |
| 旧统计解析／非法源行拒绝 | [trading-agent/tests/unit/test_statistics.py:125](../trading-agent/tests/unit/test_statistics.py#L125) | 逐一输入非法时间、布尔/小数时间、超大时间、坏数值、NaN、负量、缺列或重复桶；预期领域或数值校验拒绝。 参数：rows=[[是，1，2]]。 |
| 旧统计解析／非法源行拒绝 | [trading-agent/tests/unit/test_statistics.py:125](../trading-agent/tests/unit/test_statistics.py#L125) | 逐一输入非法时间、布尔/小数时间、超大时间、坏数值、NaN、负量、缺列或重复桶；预期领域或数值校验拒绝。 参数：rows=[[1.25，1，2]]。 |
| 旧统计解析／非法源行拒绝 | [trading-agent/tests/unit/test_statistics.py:125](../trading-agent/tests/unit/test_statistics.py#L125) | 逐一输入非法时间、布尔/小数时间、超大时间、坏数值、NaN、负量、缺列或重复桶；预期领域或数值校验拒绝。 参数：rows=[[999999999999999999999999，1，2]]。 |
| 旧统计解析／非法源行拒绝 | [trading-agent/tests/unit/test_statistics.py:125](../trading-agent/tests/unit/test_statistics.py#L125) | 逐一输入非法时间、布尔/小数时间、超大时间、坏数值、NaN、负量、缺列或重复桶；预期领域或数值校验拒绝。 参数：rows=[[0，broken，2]]。 |
| 旧统计解析／非法源行拒绝 | [trading-agent/tests/unit/test_statistics.py:125](../trading-agent/tests/unit/test_statistics.py#L125) | 逐一输入非法时间、布尔/小数时间、超大时间、坏数值、NaN、负量、缺列或重复桶；预期领域或数值校验拒绝。 参数：rows=[[0，1，NaN]]。 |
| 旧统计解析／非法源行拒绝 | [trading-agent/tests/unit/test_statistics.py:125](../trading-agent/tests/unit/test_statistics.py#L125) | 逐一输入非法时间、布尔/小数时间、超大时间、坏数值、NaN、负量、缺列或重复桶；预期领域或数值校验拒绝。 参数：rows=[[0，-1，2]]。 |
| 旧统计解析／非法源行拒绝 | [trading-agent/tests/unit/test_statistics.py:125](../trading-agent/tests/unit/test_statistics.py#L125) | 逐一输入非法时间、布尔/小数时间、超大时间、坏数值、NaN、负量、缺列或重复桶；预期领域或数值校验拒绝。 参数：rows=[[0，1]]。 |
| 旧统计解析／非法源行拒绝 | [trading-agent/tests/unit/test_statistics.py:125](../trading-agent/tests/unit/test_statistics.py#L125) | 逐一输入非法时间、布尔/小数时间、超大时间、坏数值、NaN、负量、缺列或重复桶；预期领域或数值校验拒绝。 参数：rows=[[0，1，2]，[0，1，2]]。 |
| 旧统计采集（模拟HTTP）／四周期请求契约 | [trading-agent/tests/unit/test_statistics.py:130](../trading-agent/tests/unit/test_statistics.py#L130) | 显式选旧统计来源并以假HTTP获取三类四周期统计；预期12个统计请求的period/instId/unit=0正确，CVD、OI、多空比符合预设值；不代表正式默认回退该源。 |
| 统计时点未确认／跳过并说明缺失 | [trading-agent/tests/unit/test_statistics.py:200](../trading-agent/tests/unit/test_statistics.py#L200) | 不声明统计时间口径后采集，再声明正确口径；预期未验证时不发相应统计请求，保留UNVERIFIED_TIMESTAMP_CONTRACT，规则变化改变引擎身份。 |
| 结构识别／上升三角与因果拐点 | [trading-agent/tests/unit/test_structure_predictions.py:107](../trading-agent/tests/unit/test_structure_predictions.py#L107) | 构造水平110上沿和抬升低点，再把最新未确认低点设为极低值；预期斜支撑随时间上升、水平阻力不变，末尾未确认拐点不参与识别。 |
| 结构识别／W底阶段 | [trading-agent/tests/unit/test_structure_predictions.py:119](../trading-agent/tests/unit/test_structure_predictions.py#L119) | 构造双底95、颈线110并分别设置形成、突破、跌破及回踩行情；预期对应FORMING、BREAKOUT_UP、INVALIDATED、RETEST_UP，边界与算法版本保留。 |
| 结构识别／箱体、收敛三角和未知 | [trading-agent/tests/unit/test_structure_predictions.py:147](../trading-agent/tests/unit/test_structure_predictions.py#L147) | 分别构造箱体、收敛三角、边界已交叉及无有效形态的序列；预期识别有效候选、拒绝交叉假三角，未知返回空候选。 |
| 结构识别／历史范围校验 | [trading-agent/tests/unit/test_structure_predictions.py:157](../trading-agent/tests/unit/test_structure_predictions.py#L157) | 分别将决策时点提前、删掉一根历史或混入其他合约；预期拒绝未来未可用数据、少于80根及跨标的输入。 |
| 预测评分／触发前高点不能算目标达成 | [trading-agent/tests/unit/test_structure_predictions.py:168](../trading-agent/tests/unit/test_structure_predictions.py#L168) | 首分钟高点越过目标但尚未完成触发，之后再给触发后达标分钟；预期先保持ACTIVE，后变ACHIEVED，目标Brier按0.6概率得到0.16。 |
| 预测评分／先失效后达标不能翻案 | [trading-agent/tests/unit/test_structure_predictions.py:180](../trading-agent/tests/unit/test_structure_predictions.py#L180) | 依次给出跌破失效线、触发方向和后来目标价；预期结构生命周期仍INVALIDATED、目标评分为空，方向评分独立保留。 |
| 预测评分／同分钟目标与失效（现状争议） | [trading-agent/tests/unit/test_structure_predictions.py:191](../trading-agent/tests/unit/test_structure_predictions.py#L191) | 在已触发情景下一分钟同时提供目标价及收盘失效条件满足的K线；当前断言期望AMBIGUOUS、目标Brier为空。README已记录触价与收盘可判先后的问题，该测试通过不代表业务规则已修复。 [README中的待审核说明](../README.md#L44)。 |
| 持仓动作／到期、失效与父转向 | [trading-agent/tests/unit/test_structure_predictions.py:200](../trading-agent/tests/unit/test_structure_predictions.py#L200) | 分别让预测到期、结构失效、父转向并组合有无持仓；预期单纯到期且未转向继续HOLD，结构失效或转向只对已有持仓提出CLOSE。 |
| 预测评分／分钟缺口不伪结算 | [trading-agent/tests/unit/test_structure_predictions.py:220](../trading-agent/tests/unit/test_structure_predictions.py#L220) | 省略窗口最早分钟后推进到预测到期；预期AWAITING_DATA、观察未完成，不将缺证据预测直接结算到期。 |
| 预测评分／增量与整段回放一致 | [trading-agent/tests/unit/test_structure_predictions.py:227](../trading-agent/tests/unit/test_structure_predictions.py#L227) | 对同一120分钟序列分别整段回放、分段增量和重复验证；预期最终状态完全一致，重复验证无新事件。 |
| 预测评分／禁止跨合约或未来K线 | [trading-agent/tests/unit/test_structure_predictions.py:238](../trading-agent/tests/unit/test_structure_predictions.py#L238) | 将其他合约分钟或决策时点后才可用数据传给评分器；预期明确拒绝。 |
| 父决策契约／自主综合与动作绑定 | [trading-agent/tests/unit/test_structure_predictions.py:246](../trading-agent/tests/unit/test_structure_predictions.py#L246) | 构造独立概率的父预测，再提交方向不一致、缺具体持仓的平仓或跨合约动作；预期允许独立概率，非法动作/方向/持仓绑定拒绝。 |
| 逐笔完整性／总量相同仍需翻页边界 | [trading-agent/tests/unit/test_trade_minute_review.py:81](../trading-agent/tests/unit/test_trade_minute_review.py#L81) | 给买卖总量恰好匹配K线但分页尚未跨过分钟起点的数据，并耗尽请求预算；预期MISSING/TRADE_CYCLE_BUDGET，保留原料而不冒充完整。 |
| 逐笔完整性／旧边界坏分类不能跳过 | [trading-agent/tests/unit/test_trade_minute_review.py:98](../trading-agent/tests/unit/test_trade_minute_review.py#L98) | 在分钟起点之外的分页边界注入未知source分类；预期TRADE_CLASSIFICATION且分页未获完整认证，不能跳过坏行后判完整。 |
| 逐笔单位／不支持合约在I/O前拒绝 | [trading-agent/tests/unit/test_trade_minute_review.py:116](../trading-agent/tests/unit/test_trade_minute_review.py#L116) | 分别改变合约乘数、面值币种或线性类型；预期TRADE_CONTRACT_UNSUPPORTED，无公开请求、无K线或逐笔结果。 参数：field=ctMult；value=2。 |
| 逐笔单位／不支持合约在I/O前拒绝 | [trading-agent/tests/unit/test_trade_minute_review.py:116](../trading-agent/tests/unit/test_trade_minute_review.py#L116) | 分别改变合约乘数、面值币种或线性类型；预期TRADE_CONTRACT_UNSUPPORTED，无公开请求、无K线或逐笔结果。 参数：field=ctValCcy；value=USDT。 |
| 逐笔单位／不支持合约在I/O前拒绝 | [trading-agent/tests/unit/test_trade_minute_review.py:116](../trading-agent/tests/unit/test_trade_minute_review.py#L116) | 分别改变合约乘数、面值币种或线性类型；预期TRADE_CONTRACT_UNSUPPORTED，无公开请求、无K线或逐笔结果。 参数：field=ctType；value=inverse。 |
| 逐笔聚合／边界、来源与重复去重 | [trading-agent/tests/unit/test_trade_minutes.py:91](../trading-agent/tests/unit/test_trade_minutes.py#L91) | 让相邻分页重复同一成交，同时放入分钟起点、前1毫秒及下一分钟成交；预期只计当前分钟唯一两笔、买2卖3，并保存source=1分组量和正确游标。 |
| 逐笔聚合／坏字段不静默丢弃 | [trading-agent/tests/unit/test_trade_minutes.py:122](../trading-agent/tests/unit/test_trade_minutes.py#L122) | 分别输入未知买卖方向、未知source、错标的、负数量、NaN或非法tradeId；预期整个分钟MISSING并保存对应错误原因。 参数：field=side；value=unknown；reason=TRADE_CLASSIFICATION。 |
| 逐笔聚合／坏字段不静默丢弃 | [trading-agent/tests/unit/test_trade_minutes.py:122](../trading-agent/tests/unit/test_trade_minutes.py#L122) | 分别输入未知买卖方向、未知source、错标的、负数量、NaN或非法tradeId；预期整个分钟MISSING并保存对应错误原因。 参数：field=source；value=9；reason=TRADE_CLASSIFICATION。 |
| 逐笔聚合／坏字段不静默丢弃 | [trading-agent/tests/unit/test_trade_minutes.py:122](../trading-agent/tests/unit/test_trade_minutes.py#L122) | 分别输入未知买卖方向、未知source、错标的、负数量、NaN或非法tradeId；预期整个分钟MISSING并保存对应错误原因。 参数：field=instId；value=ETH-USDT-SWAP；reason=TRADE_SCOPE。 |
| 逐笔聚合／坏字段不静默丢弃 | [trading-agent/tests/unit/test_trade_minutes.py:122](../trading-agent/tests/unit/test_trade_minutes.py#L122) | 分别输入未知买卖方向、未知source、错标的、负数量、NaN或非法tradeId；预期整个分钟MISSING并保存对应错误原因。 参数：field=sz；value=-1；reason=TRADE_FIELDS_INVALID。 |
| 逐笔聚合／坏字段不静默丢弃 | [trading-agent/tests/unit/test_trade_minutes.py:122](../trading-agent/tests/unit/test_trade_minutes.py#L122) | 分别输入未知买卖方向、未知source、错标的、负数量、NaN或非法tradeId；预期整个分钟MISSING并保存对应错误原因。 参数：field=sz；value=NaN；reason=TRADE_SOURCE_INVALID。 |
| 逐笔聚合／坏字段不静默丢弃 | [trading-agent/tests/unit/test_trade_minutes.py:122](../trading-agent/tests/unit/test_trade_minutes.py#L122) | 分别输入未知买卖方向、未知source、错标的、负数量、NaN或非法tradeId；预期整个分钟MISSING并保存对应错误原因。 参数：field=tradeId；value=abc；reason=TRADE_ID_INVALID。 |
| 逐笔聚合／分页不足或冲突 | [trading-agent/tests/unit/test_trade_minutes.py:139](../trading-agent/tests/unit/test_trade_minutes.py#L139) | 分别模拟空页、页数上限、游标不推进和同ID不同内容；预期MISSING并区分SOURCE_EMPTY、PAGE_LIMIT、PAGINATION_STALLED及DUPLICATE_CONFLICT。 参数：mode=empty；reason=TRADE_SOURCE_EMPTY。 |
| 逐笔聚合／分页不足或冲突 | [trading-agent/tests/unit/test_trade_minutes.py:139](../trading-agent/tests/unit/test_trade_minutes.py#L139) | 分别模拟空页、页数上限、游标不推进和同ID不同内容；预期MISSING并区分SOURCE_EMPTY、PAGE_LIMIT、PAGINATION_STALLED及DUPLICATE_CONFLICT。 参数：mode=limit；reason=TRADE_PAGE_LIMIT。 |
| 逐笔聚合／分页不足或冲突 | [trading-agent/tests/unit/test_trade_minutes.py:139](../trading-agent/tests/unit/test_trade_minutes.py#L139) | 分别模拟空页、页数上限、游标不推进和同ID不同内容；预期MISSING并区分SOURCE_EMPTY、PAGE_LIMIT、PAGINATION_STALLED及DUPLICATE_CONFLICT。 参数：mode=stalled；reason=TRADE_PAGINATION_STALLED。 |
| 逐笔聚合／分页不足或冲突 | [trading-agent/tests/unit/test_trade_minutes.py:139](../trading-agent/tests/unit/test_trade_minutes.py#L139) | 分别模拟空页、页数上限、游标不推进和同ID不同内容；预期MISSING并区分SOURCE_EMPTY、PAGE_LIMIT、PAGINATION_STALLED及DUPLICATE_CONFLICT。 参数：mode=conflict；reason=TRADE_DUPLICATE_CONFLICT。 |
| 逐笔聚合／总量不符不猜分配 | [trading-agent/tests/unit/test_trade_minutes.py:155](../trading-agent/tests/unit/test_trade_minutes.py#L155) | 给买2卖3、K线总量6的分钟；预期TRADE_CANDLE_VOLUME_MISMATCH，原买卖值和两笔原料保留，不猜剩余1的方向。 |
| 逐笔聚合／总量正确仍检查方向 | [trading-agent/tests/unit/test_trade_minutes.py:163](../trading-agent/tests/unit/test_trade_minutes.py#L163) | 故意把聚合函数的买卖总量交换；预期独立原始side复核发现TRADE_SIDE_CHECK_FAILED并标MISSING。 |
| 逐笔聚合／零成交也要完整证据 | [trading-agent/tests/unit/test_trade_minutes.py:177](../trading-agent/tests/unit/test_trade_minutes.py#L177) | 给零成交K线及已跨分钟起点的分页边界；预期完整零量分钟为OK、买卖均0，不能只凭空响应认定无成交。 |
| 采集配置／数据源改变引擎身份 | [trading-agent/tests/unit/test_trade_minutes.py:184](../trading-agent/tests/unit/test_trade_minutes.py#L184) | 比较分钟聚合来源与旧统计来源配置；预期engine hash不同，默认原始逐笔缓存保留24小时。 |
| 主动成交默认来源／不请求缺陷旧接口 | [trading-agent/tests/unit/test_trade_minutes.py:193](../trading-agent/tests/unit/test_trade_minutes.py#L193) | 启用分钟来源并调用外部观测采集；预期不发送旧Rubik主动成交请求，也不生成来自该接口的taker_volume观测。 |

## 真实PostgreSQL集成测试（192例）

| 测试范围 | 代码文件 | 怎么测，预期是什么 |
| --- | --- | --- |
| 真实PG＋预设预测／父错误阻断旧开仓 | [trading-agent/tests/integration/test_analysis_execution.py:27](../trading-agent/tests/integration/test_analysis_execution.py#L27) | 先生成开仓意图，再令父分析进入错误状态；预期旧开仓标STALE及PARENT_UNAVAILABLE，未生成持仓，不删除原预测。 |
| 真实PG＋预设预测／父错误不阻止绑定平仓 | [trading-agent/tests/integration/test_analysis_execution.py:46](../trading-agent/tests/integration/test_analysis_execution.py#L46) | 已有持仓及绑定平仓意图时触发父错误；预期平仓继续完成，不伪造原结构已经失效的事实。 |
| 真实PG／固定事件合并窗口 | [trading-agent/tests/integration/test_analysis_rounds.py:151](../trading-agent/tests/integration/test_analysis_rounds.py#L151) | 在固定窗口内写入多个周期事件，并在到期前后尝试领取；预期窗口不滑动，同一轮合并所需角色并保留真实事件，不重复发布。 |
| 真实PG＋预设角色输出／四周期一轮发布 | [trading-agent/tests/integration/test_analysis_rounds.py:181](../trading-agent/tests/integration/test_analysis_rounds.py#L181) | 依次完成四个子角色和父综合；预期同属一轮、父看到四份子报告，数据库身份由服务绑定，各正式事实只发布一次。 |
| 真实PG＋预设行情／局部重试 | [trading-agent/tests/integration/test_analysis_rounds.py:209](../trading-agent/tests/integration/test_analysis_rounds.py#L209) | 先完成有效角色，再令单个周期失败后重试；预期只重采和重跑失败角色，冻结新input_id及时点，其他有效子预测不改变。 |
| 真实PG／有界重试与恢复 | [trading-agent/tests/integration/test_analysis_rounds.py:278](../trading-agent/tests/integration/test_analysis_rounds.py#L278) | 耗尽正常重试，再按规则走三次恢复，最后重启检查；预期计数持久化，退避有效，耗尽后不再领取新父任务。 |
| 真实PG／两个模型槽原子占用 | [trading-agent/tests/integration/test_analysis_rounds.py:320](../trading-agent/tests/integration/test_analysis_rounds.py#L320) | 并发领取任务并模拟资源清理状态未知；预期最多占用两个槽，未知清理继续占容量，确认清理后才释放名额。 |
| 真实PG／角色授权与引用绑定 | [trading-agent/tests/integration/test_analysis_rounds.py:341](../trading-agent/tests/integration/test_analysis_rounds.py#L341) | 尝试固定数据库ID、越角色提交、非法结构或错误引用；预期授权或schema拒绝，正式预测数量保持0。 |
| 真实PG／子角色执行期限 | [trading-agent/tests/integration/test_analysis_rounds.py:361](../trading-agent/tests/integration/test_analysis_rounds.py#L361) | 延迟子角色提交到执行期限之后；预期拒绝保存预测，不因迟到提交延长期限。 |
| 真实PG／父等待与局部重试预算 | [trading-agent/tests/integration/test_analysis_rounds.py:372](../trading-agent/tests/integration/test_analysis_rounds.py#L372) | 先让父等待四子，再让父单独失败重试；预期等子时间不计入父实际分析预算，父获得规定预算，四子不重跑。 |
| 真实PG＋模拟HTTP／父错误后的查询 | [trading-agent/tests/integration/test_analysis_rounds.py:393](../trading-agent/tests/integration/test_analysis_rounds.py#L393) | 父进入错误状态后查询latest及已发布历史；预期latest返回不可用，历史结果仍能正常读取。 |
| 真实PG／发布事务回滚 | [trading-agent/tests/integration/test_analysis_rounds.py:430](../trading-agent/tests/integration/test_analysis_rounds.py#L430) | 在发布过程中注入异常；预期正式事实不落库，角色状态也不产生半次成功提交。 |
| 真实PG／已关闭窗口不被新事件推迟 | [trading-agent/tests/integration/test_analysis_rounds.py:450](../trading-agent/tests/integration/test_analysis_rounds.py#L450) | 窗口到期后再追加事件并调度；预期已就绪轮次仍能按原时点开始。 |
| 真实PG＋预设行情／分析期间结构检查 | [trading-agent/tests/integration/test_analysis_rounds.py:462](../trading-agent/tests/integration/test_analysis_rounds.py#L462) | 在分析期间注入普通价格噪声或真实结构失效；预期噪声不重启任务，真实失效拒绝发布。 参数：invalid=否。 |
| 真实PG＋预设行情／分析期间结构检查 | [trading-agent/tests/integration/test_analysis_rounds.py:462](../trading-agent/tests/integration/test_analysis_rounds.py#L462) | 在分析期间注入普通价格噪声或真实结构失效；预期噪声不重启任务，真实失效拒绝发布。 参数：invalid=是。 |
| 真实PG／提交期间重启接管 | [trading-agent/tests/integration/test_analysis_rounds.py:483](../trading-agent/tests/integration/test_analysis_rounds.py#L483) | 让角色停在提交阶段后重启所有权；预期旧任务失权，轮次计数保留，新尝试使用下一次角色序号。 |
| 真实PG／错误退避不被事件清零 | [trading-agent/tests/integration/test_analysis_rounds.py:514](../trading-agent/tests/integration/test_analysis_rounds.py#L514) | 在错误退避期追加周期事件；预期原恢复时间、错误组和恢复次数保留，不通过新事件绕过退避。 |
| 真实PG／父重试前复查复用子报告 | [trading-agent/tests/integration/test_analysis_rounds.py:552](../trading-agent/tests/integration/test_analysis_rounds.py#L552) | 在父重试前令原子报告需要更新；预期立即要求补齐相应角色，暂停父可用性，不依赖下轮调度才发现。 |
| 真实PG／崩溃跨越总期限 | [trading-agent/tests/integration/test_analysis_rounds.py:572](../trading-agent/tests/integration/test_analysis_rounds.py#L572) | 进程崩溃后推进到轮次期限之外再调度；预期关闭旧范围、记录ANALYSIS_DEADLINE及恢复退避，不能通过领取绕过。 |
| 真实PG＋假研究进程／父协调重启 | [trading-agent/tests/integration/test_analysis_rounds.py:611](../trading-agent/tests/integration/test_analysis_rounds.py#L611) | 模拟研究宿主重启、旧会话迟到事件和新父执行；预期仅重启父协调，旧启动不能终止新会话，输入与预算分别冻结，资源被清理。 |
| 真实PG＋假研究进程／零退出但无角色结果 | [trading-agent/tests/integration/test_analysis_rounds.py:660](../trading-agent/tests/integration/test_analysis_rounds.py#L660) | 研究进程多次正常退出但不提交正式角色结果；预期最多三次持久化启动，最终COORDINATOR_RETRIES_EXHAUSTED，未写正式预测。 |
| 真实PG＋假研究进程／取消未确认 | [trading-agent/tests/integration/test_analysis_rounds.py:682](../trading-agent/tests/integration/test_analysis_rounds.py#L682) | 令子任务取消结果不确定，再尝试新的协调进程；预期先停止当前宿主，旧取消事件不误伤后续合法启动。 |
| 真实PG＋真实备份工具／非空事实恢复 | [trading-agent/tests/integration/test_backups.py:23](../trading-agent/tests/integration/test_backups.py#L23) | 生成结构预测和评分后备份，恢复到新空库；预期内容完全一致、恢复全程持实例锁且外部工具运行时无打开事务，历史不可修改，非空目标拒绝覆盖。 |
| 真实PG＋模拟HTTP／主控任务接口 | [trading-agent/tests/integration/test_controller_bridge.py:48](../trading-agent/tests/integration/test_controller_bridge.py#L48) | 分别用未认证、研究角色和主控身份创建解释或复盘任务并重复相同请求；预期401/403/202及幂等行为正确，门禁生效，查询需要认证。 |
| 真实PG＋假Gateway发送／解释冻结与通知 | [trading-agent/tests/integration/test_controller_bridge.py:128](../trading-agent/tests/integration/test_controller_bridge.py#L128) | 分别解释当前或历史预测，记录拟发送命令而不真实发TG；预期使用主控Profile及既有通知通道，不泄露研究令牌，原预测事实不变。 参数：historical=否。 |
| 真实PG＋假Gateway发送／解释冻结与通知 | [trading-agent/tests/integration/test_controller_bridge.py:128](../trading-agent/tests/integration/test_controller_bridge.py#L128) | 分别解释当前或历史预测，记录拟发送命令而不真实发TG；预期使用主控Profile及既有通知通道，不泄露研究令牌，原预测事实不变。 参数：historical=是。 |
| 真实PG＋模拟HTTP／指定复盘范围 | [trading-agent/tests/integration/test_controller_bridge.py:223](../trading-agent/tests/integration/test_controller_bridge.py#L223) | 提交半开时间范围的复盘申请，再申请两币扇出；预期仅选择范围内对应证据，各币生成有界任务并冻结范围。 |
| 真实PG＋假执行／成交或拒绝通知去重 | [trading-agent/tests/integration/test_controller_bridge.py:272](../trading-agent/tests/integration/test_controller_bridge.py#L272) | 分别模拟执行成功或被拒绝；预期意图进入对应终态，仅入队一条发往主控既有目标的通知。 参数：rejected=否。 |
| 真实PG＋假执行／成交或拒绝通知去重 | [trading-agent/tests/integration/test_controller_bridge.py:272](../trading-agent/tests/integration/test_controller_bridge.py#L272) | 分别模拟执行成功或被拒绝；预期意图进入对应终态，仅入队一条发往主控既有目标的通知。 参数：rejected=是。 |
| 真实PG／终端失败通知 | [trading-agent/tests/integration/test_controller_bridge.py:292](../trading-agent/tests/integration/test_controller_bridge.py#L292) | 让任务发生不可恢复的终端错误；预期任务FAILED，并通过现有outbox保存对应失败通知。 |
| 真实PG＋假Cube API／有界续租 | [trading-agent/tests/integration/test_cube_lifecycle.py:86](../trading-agent/tests/integration/test_cube_lifecycle.py#L86) | 以不同剩余授权时间续租，再模拟外部I/O期间授权过期；预期续租不超过剩余期限，I/O后重新校验，过期失败且资源可清理。 |
| 真实PG＋假Cube API／过期或越权资源 | [trading-agent/tests/integration/test_cube_lifecycle.py:128](../trading-agent/tests/integration/test_cube_lifecycle.py#L128) | 分别改变fence、撤销授权、推进期限、移除沙箱或替换成他人资源；预期全部拒绝续租，不发续租控制调用。 参数：change=fence。 |
| 真实PG＋假Cube API／过期或越权资源 | [trading-agent/tests/integration/test_cube_lifecycle.py:128](../trading-agent/tests/integration/test_cube_lifecycle.py#L128) | 分别改变fence、撤销授权、推进期限、移除沙箱或替换成他人资源；预期全部拒绝续租，不发续租控制调用。 参数：change=revoked。 |
| 真实PG＋假Cube API／过期或越权资源 | [trading-agent/tests/integration/test_cube_lifecycle.py:128](../trading-agent/tests/integration/test_cube_lifecycle.py#L128) | 分别改变fence、撤销授权、推进期限、移除沙箱或替换成他人资源；预期全部拒绝续租，不发续租控制调用。 参数：change=deadline。 |
| 真实PG＋假Cube API／过期或越权资源 | [trading-agent/tests/integration/test_cube_lifecycle.py:128](../trading-agent/tests/integration/test_cube_lifecycle.py#L128) | 分别改变fence、撤销授权、推进期限、移除沙箱或替换成他人资源；预期全部拒绝续租，不发续租控制调用。 参数：change=missing。 |
| 真实PG＋假Cube API／过期或越权资源 | [trading-agent/tests/integration/test_cube_lifecycle.py:128](../trading-agent/tests/integration/test_cube_lifecycle.py#L128) | 分别改变fence、撤销授权、推进期限、移除沙箱或替换成他人资源；预期全部拒绝续租，不发续租控制调用。 参数：change=foreign。 |
| 真实PG＋假Cube API／先记创建意图再创建 | [trading-agent/tests/integration/test_cube_lifecycle.py:162](../trading-agent/tests/integration/test_cube_lifecycle.py#L162) | 记录沙箱准备、输入及子命令资源后重复清理；预期先有归属记录，环境与宿主挂载为空，秘密文件受限，只清理所属沙箱且清理幂等。 |
| 真实PG＋假Cube API／恢复清理范围 | [trading-agent/tests/integration/test_cube_lifecycle.py:192](../trading-agent/tests/integration/test_cube_lifecycle.py#L192) | 混入他人、未知及本项目已过期沙箱；预期只删除能由数据库验证归属的已过期资源。 |
| 真实PG＋假Cube API／模板资源超限 | [trading-agent/tests/integration/test_cube_lifecycle.py:227](../trading-agent/tests/integration/test_cube_lifecycle.py#L227) | 返回超过配置预算的沙箱模板规格；预期报CUBE_RESOURCE_LIMITS，并能清理本任务已创建资源。 |
| 真实PG＋临时Git仓库／部署身份 | [trading-agent/tests/integration/test_deployment_identity.py:17](../trading-agent/tests/integration/test_deployment_identity.py#L17) | 使用正确和错误Git提交、受限角色及管理员角色做预检；预期只允许固定提交及最小权限应用角色。 |
| 运维（假API/发送端）／故障静默与恢复通知 | [trading-agent/tests/integration/test_doctor.py:17](../trading-agent/tests/integration/test_doctor.py#L17) | 连续注入同一故障、重启doctor并恢复；预期首次故障和恢复各通知一次，重复事件静默，状态文件受限且不保存原始令牌。 |
| 运维（假API）／doctor身份错误 | [trading-agent/tests/integration/test_doctor.py:40](../trading-agent/tests/integration/test_doctor.py#L40) | 用错误身份调用健康检查；预期ready=false并返回DOCTOR_CREDENTIAL_REJECTED。 |
| 真实PG＋预设行情／80＋1输入冻结 | [trading-agent/tests/integration/test_forming_candle_inputs.py:73](../trading-agent/tests/integration/test_forming_candle_inputs.py#L73) | 遍历BTC、ETH各周期，冻结80根已收盘和独立当前快照；预期模型只展示30根，26临时指标有标记，原内容hash稳定且不把快照当未来已收盘事实。 |
| 真实PG＋预设行情／可选快照缺失 | [trading-agent/tests/integration/test_forming_candle_inputs.py:155](../trading-agent/tests/integration/test_forming_candle_inputs.py#L155) | 模拟当前快照接口为空或拒绝；预期快照为空、临时指标为空并明确MISSING原因，确认指标及旧输入兼容读取保留。 参数：failure=empty；reason=CURRENT_CANDLE_NOT_RETURNED。 |
| 真实PG＋预设行情／可选快照缺失 | [trading-agent/tests/integration/test_forming_candle_inputs.py:155](../trading-agent/tests/integration/test_forming_candle_inputs.py#L155) | 模拟当前快照接口为空或拒绝；预期快照为空、临时指标为空并明确MISSING原因，确认指标及旧输入兼容读取保留。 参数：failure=rejected；reason=MARKET_REJECTED。 |
| 真实PG／非法盘中快照回滚 | [trading-agent/tests/integration/test_forming_candle_inputs.py:183](../trading-agent/tests/integration/test_forming_candle_inputs.py#L183) | 分别注入错币种、错周期、未来、过旧或错误期间的快照；预期FORMING_CANDLE_SCOPE，相关事实表不留下部分输入。 参数：fault=symbol。 |
| 真实PG／非法盘中快照回滚 | [trading-agent/tests/integration/test_forming_candle_inputs.py:183](../trading-agent/tests/integration/test_forming_candle_inputs.py#L183) | 分别注入错币种、错周期、未来、过旧或错误期间的快照；预期FORMING_CANDLE_SCOPE，相关事实表不留下部分输入。 参数：fault=timeframe。 |
| 真实PG／非法盘中快照回滚 | [trading-agent/tests/integration/test_forming_candle_inputs.py:183](../trading-agent/tests/integration/test_forming_candle_inputs.py#L183) | 分别注入错币种、错周期、未来、过旧或错误期间的快照；预期FORMING_CANDLE_SCOPE，相关事实表不留下部分输入。 参数：fault=future。 |
| 真实PG／非法盘中快照回滚 | [trading-agent/tests/integration/test_forming_candle_inputs.py:183](../trading-agent/tests/integration/test_forming_candle_inputs.py#L183) | 分别注入错币种、错周期、未来、过旧或错误期间的快照；预期FORMING_CANDLE_SCOPE，相关事实表不留下部分输入。 参数：fault=stale。 |
| 真实PG／非法盘中快照回滚 | [trading-agent/tests/integration/test_forming_candle_inputs.py:183](../trading-agent/tests/integration/test_forming_candle_inputs.py#L183) | 分别注入错币种、错周期、未来、过旧或错误期间的快照；预期FORMING_CANDLE_SCOPE，相关事实表不留下部分输入。 参数：fault=period。 |
| 真实PG＋预设行情／原快照不可替换 | [trading-agent/tests/integration/test_forming_candle_inputs.py:235](../trading-agent/tests/integration/test_forming_candle_inputs.py#L235) | 先冻结一份盘中数据，再提供较新快照或最终K线；预期新输入可变化，原快照、临时指标和内容hash保持原值。 |
| 真实PG＋预设分钟历史／期限学习全流程 | [trading-agent/tests/integration/test_horizon_pipeline.py:111](../trading-agent/tests/integration/test_horizon_pipeline.py#L111) | 注入历史回放和严格后续影子样本，模拟晋升、重启监控及恶化；预期候选先SHADOW，达标才晋升，恶化按规则回滚，原预测和评分不改写。 |
| 真实PG／期限学习样本与权限 | [trading-agent/tests/integration/test_horizon_pipeline.py:223](../trading-agent/tests/integration/test_horizon_pipeline.py#L223) | 只提供不足数量的历史预测，再用非确定性学习任务调用；预期返回INSUFFICIENT_REPLAY或拒绝错误任务权限。 |
| 真实PG＋预设强平／双币及冻结范围 | [trading-agent/tests/integration/test_indicator_review.py:65](../trading-agent/tests/integration/test_indicator_review.py#L65) | 同一采样器记录两币事件，冻结ETH输入及独立BTC基准，再写迟到事件；预期只用ETH强平窗口、保留部分覆盖，冻结80根基准及原hash不变。 |
| 真实PG／BTC基准范围回滚 | [trading-agent/tests/integration/test_indicator_review.py:170](../trading-agent/tests/integration/test_indicator_review.py#L170) | 向ETH冻结输入传入其他币种或未来可用的BTC基准；预期整个输入事务回滚，不落部分基准或输入。 参数：defect=foreign。 |
| 真实PG／BTC基准范围回滚 | [trading-agent/tests/integration/test_indicator_review.py:170](../trading-agent/tests/integration/test_indicator_review.py#L170) | 向ETH冻结输入传入其他币种或未来可用的BTC基准；预期整个输入事务回滚，不落部分基准或输入。 参数：defect=future。 |
| 真实PG／失效任务禁止写外部证据 | [trading-agent/tests/integration/test_indicator_review.py:197](../trading-agent/tests/integration/test_indicator_review.py#L197) | 使attempt过期后再冻结基准与强平窗口；预期StaleAttempt，相关事实不落库。 |
| 真实PG／并发创建与领取幂等 | [trading-agent/tests/integration/test_jobs.py:42](../trading-agent/tests/integration/test_jobs.py#L42) | 并发20次创建同一业务键，再并发20次领取；预期只创建一个任务，只有一个成功领取和一个attempt。 |
| 真实PG／租约过期与fence | [trading-agent/tests/integration/test_jobs.py:67](../trading-agent/tests/integration/test_jobs.py#L67) | 将已领取任务租约设为过期，再续租、认证、写入并重新领取；预期旧尝试均失效，新尝试fence递增且仅保存令牌hash。 |
| 真实PG／锁等待后重查期限 | [trading-agent/tests/integration/test_jobs.py:99](../trading-agent/tests/integration/test_jobs.py#L99) | 故意持任务行锁超过其截止时间；预期等待结束后的写入仍按当前时钟拒绝，不能沿用加锁前的授权时间。 |
| 真实PG／唯一runtime及接管 | [trading-agent/tests/integration/test_jobs.py:124](../trading-agent/tests/integration/test_jobs.py#L124) | 尝试第二个runtime，再终止原所有权连接并接管；预期并行入口被拒，接管代次和fence递增，旧池连接也不能写。 |
| 真实PG／任务完成撤权 | [trading-agent/tests/integration/test_jobs.py:153](../trading-agent/tests/integration/test_jobs.py#L153) | 用任务令牌调用错误种类工具，再完成任务后复用令牌；预期越权403，完成后原令牌立即失效且结果ID保存。 |
| 真实PG／任务事务原子性 | [trading-agent/tests/integration/test_jobs.py:169](../trading-agent/tests/integration/test_jobs.py#L169) | 创建任务后在提交前注入异常；预期事务回滚，不留下半个任务。 |
| 真实PG／终止任务清除租约 | [trading-agent/tests/integration/test_jobs.py:181](../trading-agent/tests/integration/test_jobs.py#L181) | 分别耗尽尝试或超出截止时间后再次领取；预期不再领取，任务FAILED或SKIPPED，旧attempt撤权并清空租约所有者。 参数：expired_deadline=否。 |
| 真实PG／终止任务清除租约 | [trading-agent/tests/integration/test_jobs.py:181](../trading-agent/tests/integration/test_jobs.py#L181) | 分别耗尽尝试或超出截止时间后再次领取；预期不再领取，任务FAILED或SKIPPED，旧attempt撤权并清空租约所有者。 参数：expired_deadline=是。 |
| 真实PG＋预设强平／去重、缺口与接管 | [trading-agent/tests/integration/test_liquidation_sampler.py:63](../trading-agent/tests/integration/test_liquidation_sampler.py#L63) | 注入重复、乱序、迟到强平事件和连接缺口，设置查询上限后再接管；预期金额去重、缺口保留且部分覆盖，迟到不改原窗口，旧owner不能写。 |
| 真实PG＋本机WebSocket服务／心跳与重启 | [trading-agent/tests/integration/test_liquidation_sampler.py:156](../trading-agent/tests/integration/test_liquidation_sampler.py#L156) | 用回环WebSocket模拟订阅、可选退役通知、ping/pong、断开和重启；预期仅确认订阅后记录覆盖，事件去重，真实断线/重启留痕，网络I/O不占额外数据库事务。 参数：port_notice=否。 |
| 真实PG＋本机WebSocket服务／心跳与重启 | [trading-agent/tests/integration/test_liquidation_sampler.py:156](../trading-agent/tests/integration/test_liquidation_sampler.py#L156) | 用回环WebSocket模拟订阅、可选退役通知、ping/pong、断开和重启；预期仅确认订阅后记录覆盖，事件去重，真实断线/重启留痕，网络I/O不占额外数据库事务。 参数：port_notice=是。 |
| 真实PG／K线批量与单条混合并发 | [trading-agent/tests/integration/test_market_batch_locks.py:27](../trading-agent/tests/integration/test_market_batch_locks.py#L27) | 8路同时持久化同一窗口，再混写单条修订并注入回滚；预期相同内容复用ID，改变只追加一个版本，回滚不残留修订。 |
| 真实PG／114条外部观测并发 | [trading-agent/tests/integration/test_market_batch_locks.py:55](../trading-agent/tests/integration/test_market_batch_locks.py#L55) | 8路写同批114观测，再混入单条更新、批内重复及回滚；预期版本链和去重正确，重复不追加，回滚不留下版本。 |
| 真实PG／调度与续租不互阻 | [trading-agent/tests/integration/test_market_batch_locks.py:98](../trading-agent/tests/integration/test_market_batch_locks.py#L98) | 业务写入持有共享所有权锁时并行调度和续租；预期无关任务续租及调度在测试限定等待内完成。 |
| 真实PG／大批输入冻结期间续租 | [trading-agent/tests/integration/test_market_batch_locks.py:118](../trading-agent/tests/integration/test_market_batch_locks.py#L118) | 冻结114条外部观测及20个分钟窗口的同时给协调任务续租；预期续租及时、冻结完成，共134条引用，原114条源观测完整保留。 |
| 真实PG＋模拟HTTP／模型用量记录 | [trading-agent/tests/integration/test_metrics.py:21](../trading-agent/tests/integration/test_metrics.py#L21) | 分别提交无认证、越权、重复、冲突及非法用量；预期按身份拒绝，重复幂等、冲突拒绝，汇总保留未知计数而不补零。 |
| 真实PG／用量单调及子预算 | [trading-agent/tests/integration/test_metrics.py:85](../trading-agent/tests/integration/test_metrics.py#L85) | 先记录父累计调用数，再尝试降低累计值或多记超过范围的子会话；预期拒绝倒退和超预算，合法递增保存。 |
| 真实PG＋假行情/API／模型实际视图 | [trading-agent/tests/integration/test_model_candle_payload.py:15](../trading-agent/tests/integration/test_model_candle_payload.py#L15) | 对BTC和ETH逐角色执行四子及父综合；预期子各30已收盘＋1当前快照、26临时指标，父只接收四份子报告，回看冻结子证据不重新取行情。 参数：symbol=BTCUSDT。 |
| 真实PG＋假行情/API／模型实际视图 | [trading-agent/tests/integration/test_model_candle_payload.py:15](../trading-agent/tests/integration/test_model_candle_payload.py#L15) | 对BTC和ETH逐角色执行四子及父综合；预期子各30已收盘＋1当前快照、26临时指标，父只接收四份子报告，回看冻结子证据不重新取行情。 参数：symbol=ETHUSDT。 |
| 真实PG／通知未知结果不重发 | [trading-agent/tests/integration/test_outbox.py:18](../trading-agent/tests/integration/test_outbox.py#L18) | 重复入队同一事件，再让投递租约超时且回执迟到；预期一条事件、状态UNKNOWN，拒绝旧回执，后续轮次不重新领取。 |
| 真实PG／明确发送失败才重试 | [trading-agent/tests/integration/test_outbox.py:53](../trading-agent/tests/integration/test_outbox.py#L53) | 按退避序列多次模拟明确失败，再模拟一次结果未知并重启通知器；预期明确失败按上限重试，UNKNOWN不重发。 |
| 真实PG／上下文刷新与通知边界 | [trading-agent/tests/integration/test_outbox.py:90](../trading-agent/tests/integration/test_outbox.py#L90) | 对父或子任务注入上下文过期、上下文改变或持仓改变；预期保留失败并只排一个替代任务，不发噪声通知；真正终端错误仍入队通知。 参数：code=CONTEXT_EXPIRED；kind=PREDICT_PARENT。 |
| 真实PG／上下文刷新与通知边界 | [trading-agent/tests/integration/test_outbox.py:90](../trading-agent/tests/integration/test_outbox.py#L90) | 对父或子任务注入上下文过期、上下文改变或持仓改变；预期保留失败并只排一个替代任务，不发噪声通知；真正终端错误仍入队通知。 参数：code=CONTEXT_EXPIRED；kind=PREDICT_TIMEFRAME。 |
| 真实PG／上下文刷新与通知边界 | [trading-agent/tests/integration/test_outbox.py:90](../trading-agent/tests/integration/test_outbox.py#L90) | 对父或子任务注入上下文过期、上下文改变或持仓改变；预期保留失败并只排一个替代任务，不发噪声通知；真正终端错误仍入队通知。 参数：code=CONTEXT_CHANGED；kind=PREDICT_PARENT。 |
| 真实PG／上下文刷新与通知边界 | [trading-agent/tests/integration/test_outbox.py:90](../trading-agent/tests/integration/test_outbox.py#L90) | 对父或子任务注入上下文过期、上下文改变或持仓改变；预期保留失败并只排一个替代任务，不发噪声通知；真正终端错误仍入队通知。 参数：code=CONTEXT_CHANGED；kind=PREDICT_TIMEFRAME。 |
| 真实PG／上下文刷新与通知边界 | [trading-agent/tests/integration/test_outbox.py:90](../trading-agent/tests/integration/test_outbox.py#L90) | 对父或子任务注入上下文过期、上下文改变或持仓改变；预期保留失败并只排一个替代任务，不发噪声通知；真正终端错误仍入队通知。 参数：code=POSITION_CHANGED；kind=PREDICT_PARENT。 |
| 真实PG／上下文刷新与通知边界 | [trading-agent/tests/integration/test_outbox.py:90](../trading-agent/tests/integration/test_outbox.py#L90) | 对父或子任务注入上下文过期、上下文改变或持仓改变；预期保留失败并只排一个替代任务，不发噪声通知；真正终端错误仍入队通知。 参数：code=POSITION_CHANGED；kind=PREDICT_TIMEFRAME。 |
| 真实PG／旧上下文通知退役 | [trading-agent/tests/integration/test_outbox.py:135](../trading-agent/tests/integration/test_outbox.py#L135) | 准备不同投递状态的旧上下文错误通知；预期标为已被替代而不发送，不删除原正文及尝试次数，普通执行通知照常处理。 |
| 真实PG＋假发送端／通知失败不回滚业务 | [trading-agent/tests/integration/test_outbox.py:191](../trading-agent/tests/integration/test_outbox.py#L191) | 先提交业务任务和通知，再模拟网络发送失败；预期业务任务仍保存，通知转RETRY_WAIT。 |
| 真实PG＋假发送端／消息已送回执丢失 | [trading-agent/tests/integration/test_outbox.py:210](../trading-agent/tests/integration/test_outbox.py#L210) | 模拟远端已接收、发送调用却超时，再重启通知器；预期只发送一次，保留UNKNOWN且不伪造消息回执ID。 |
| 真实PG＋现有风控＋PaperTransport／受控开平仓 | [trading-agent/tests/integration/test_paper_acceptance_evidence.py:23](../trading-agent/tests/integration/test_paper_acceptance_evidence.py#L23) | 读取指定模拟配置，对BTC或ETH用预设父决策开仓及绑定平仓，各重复执行；预期仅paper、两笔意图、两次成交记账，持仓最终为空；未提供显式验收配置时跳过。 参数：symbol=BTCUSDT。 |
| 真实PG＋现有风控＋PaperTransport／受控开平仓 | [trading-agent/tests/integration/test_paper_acceptance_evidence.py:23](../trading-agent/tests/integration/test_paper_acceptance_evidence.py#L23) | 读取指定模拟配置，对BTC或ETH用预设父决策开仓及绑定平仓，各重复执行；预期仅paper、两笔意图、两次成交记账，持仓最终为空；未提供显式验收配置时跳过。 参数：symbol=ETHUSDT。 |
| 真实PG／父仅有4h子报告 | [trading-agent/tests/integration/test_parent_synthesis.py:31](../trading-agent/tests/integration/test_parent_synthesis.py#L31) | 只发布有效4h子预测后做父综合；预期使用该子冻结边界及父1h期限，不编造父自己的行情或复盘快照，明确其他子周期缺失。 |
| 真实PG＋模拟HTTP／父回看子证据权限 | [trading-agent/tests/integration/test_parent_synthesis.py:120](../trading-agent/tests/integration/test_parent_synthesis.py#L120) | 父读取本轮子报告后更换当前行情，另用子令牌或越范围参数查询；预期父读原冻结内容不变，错误身份、引用及失效租约被拒绝。 |
| 真实PG／无子报告禁止伪造父输入 | [trading-agent/tests/integration/test_parent_synthesis.py:168](../trading-agent/tests/integration/test_parent_synthesis.py#L168) | 没有有效子预测时请求父冻结；预期MISSING_CONTEXT，不创建父输入。 |
| 真实PG／父采用4h箱体 | [trading-agent/tests/integration/test_parent_synthesis.py:177](../trading-agent/tests/integration/test_parent_synthesis.py#L177) | 给父提供子4h箱体及相应冻结边界后提交；预期结构ID和证据来自子，父输出仍为1h尺度并使用自身期限规则。 |
| 真实PG／缺1h风险依据不准备订单 | [trading-agent/tests/integration/test_parent_synthesis.py:267](../trading-agent/tests/integration/test_parent_synthesis.py#L267) | 只有高周期子报告、缺有效1h确认ATR时申请开仓准备；预期RISK_DATA_MISSING，未创建持仓或越过订单准备边界。 |
| 真实PG＋模拟HTTP／V2研究API边界 | [trading-agent/tests/integration/test_prediction_api.py:17](../trading-agent/tests/integration/test_prediction_api.py#L17) | 测试未认证、跨币种、主控冒充研究、合法研究发布及复盘提交；预期身份/范围正确校验，冻结知识版本可读，合法发布和复盘返回正式ID。 |
| 真实PG＋模拟HTTP／恢复调度的操作员权限 | [trading-agent/tests/integration/test_prediction_api.py:92](../trading-agent/tests/integration/test_prediction_api.py#L92) | 用主控和操作员身份恢复调度，分别选择保留或清除测试账户熔断并重放幂等键；预期仅操作员可操作，冲突参数409，账户修订号与所选行为一致。 本变体由已授权操作员清除测试账户risk_halt，账户版本加1；不操作实际业务账户。 参数：preserve=否。 |
| 真实PG＋模拟HTTP／恢复调度的操作员权限 | [trading-agent/tests/integration/test_prediction_api.py:92](../trading-agent/tests/integration/test_prediction_api.py#L92) | 用主控和操作员身份恢复调度，分别选择保留或清除测试账户熔断并重放幂等键；预期仅操作员可操作，冲突参数409，账户修订号与所选行为一致。 本变体保留测试账户risk_halt且账户版本不变。 参数：preserve=是。 |
| 真实PG＋假行情＋PaperTransport／父信号到绑定平仓 | [trading-agent/tests/integration/test_prediction_execution.py:126](../trading-agent/tests/integration/test_prediction_execution.py#L126) | 发布预设父开仓、重复执行并反向判断后申请绑定平仓；预期仅OPEN_LONG和CLOSE，不直接反手，成交记账幂等且持仓最终清空。 |
| 真实PG＋假交易传输／未知订单恢复 | [trading-agent/tests/integration/test_prediction_execution.py:188](../trading-agent/tests/integration/test_prediction_execution.py#L188) | 模拟下单结果未知、进程恢复查询及部分成交；预期不重复发送，确认部分逐次只记一次，未决订单继续占用账户。 |
| 真实PG＋假行情／过期数据阻止执行 | [trading-agent/tests/integration/test_prediction_execution.py:225](../trading-agent/tests/integration/test_prediction_execution.py#L225) | 将行情或预测置为过期后执行意图；预期标STALE，不创建持仓、不提交新订单。 |
| 真实PG／关闭执行保留意图 | [trading-agent/tests/integration/test_prediction_execution.py:237](../trading-agent/tests/integration/test_prediction_execution.py#L237) | 关闭预测执行再处理已发布意图；预期意图DISABLED，无账户初始化或订单/持仓副作用。 |
| 真实PG＋假交易传输／部分平仓和持仓绑定 | [trading-agent/tests/integration/test_prediction_execution.py:276](../trading-agent/tests/integration/test_prediction_execution.py#L276) | 模拟只成交40%的平仓，再持新仓重放旧平仓；预期保留60%原持仓，旧意图不能关闭新持仓，禁止重复发送。 |
| 真实PG＋假退出证据／原生退出确认 | [trading-agent/tests/integration/test_prediction_execution.py:334](../trading-agent/tests/integration/test_prediction_execution.py#L334) | 先给不唯一或缺失的交易所退出证据，再给唯一成交证据并重复处理；预期仅充分证据可关闭持仓，实现收益和退出事件只记一次。 |
| 真实PG＋假交易传输／attempt过期禁发单 | [trading-agent/tests/integration/test_prediction_execution.py:383](../trading-agent/tests/integration/test_prediction_execution.py#L383) | 使执行attempt失效后再发送或应用已知成交；预期授权拒绝，发送计数保持0。 |
| 真实PG＋假交易传输／过期部分订单撤余量 | [trading-agent/tests/integration/test_prediction_execution.py:395](../trading-agent/tests/integration/test_prediction_execution.py#L395) | 订单部分成交后让attempt过期，再通过新fence恢复；预期只撤销未成交剩余，保留已确认仓位，原订单只发送一次。 |
| 真实PG＋假交易传输／账户熔断跨币种 | [trading-agent/tests/integration/test_prediction_execution.py:429](../trading-agent/tests/integration/test_prediction_execution.py#L429) | 触发账户日亏损硬停止后，再申请另一币种新仓；预期熔断持久化，所有币种新仓均被阻止，不创建持仓。 |
| 真实PG＋假交易传输／部分平仓费用修订 | [trading-agent/tests/integration/test_prediction_execution.py:450](../trading-agent/tests/integration/test_prediction_execution.py#L450) | 部分平仓后取消余量并追加费用修订，最后完全平仓；预期费用累计正确，连续亏损次数只在整笔仓位关闭后计一次。 |
| 真实PG＋本地索引／知识初始化与检索 | [trading-agent/tests/integration/test_prediction_knowledge.py:60](../trading-agent/tests/integration/test_prediction_knowledge.py#L60) | 重复初始化种子知识、使索引不可用、分页检索后恢复；预期种子只建一次且UNVERIFIED，降级仍读正式PG版本，恢复索引不改结果，非复盘角色不能写。 |
| 真实PG＋故障注入／索引失败与知识版本时点 | [trading-agent/tests/integration/test_prediction_knowledge.py:90](../trading-agent/tests/integration/test_prediction_knowledge.py#L90) | 知识提交成功后让索引写入失败，再以历史和当前时点查询；预期正式PG提交保留，历史看不到未来版本，重试幂等、过旧版本修改拒绝。 |
| 真实PG／知识合并与退役追加保存 | [trading-agent/tests/integration/test_prediction_knowledge.py:146](../trading-agent/tests/integration/test_prediction_knowledge.py#L146) | 合并知识概念并退役旧版本，再查询有效卡片及版本表；预期有效集合变化但旧版本不删除，所有版本继续追加保存。 |
| 真实PG＋预设分钟行情／复盘评分与伪造成功 | [trading-agent/tests/integration/test_prediction_knowledge.py:171](../trading-agent/tests/integration/test_prediction_knowledge.py#L171) | 先生成已完成观察且达标的案例，再准备复盘统计并提交伪造成功叙述；预期案例、方向Brier和案例内容来自已验证事实，伪造结论被拒绝。 |
| 真实PG／复盘案例可见修订 | [trading-agent/tests/integration/test_prediction_knowledge.py:219](../trading-agent/tests/integration/test_prediction_knowledge.py#L219) | 准备不同币种、边界外及冻结时点之后的评分修订；预期仅选对应币种半开范围内的案例和当时最新可见评分。 |
| 真实PG／结构阶段知识分开检索 | [trading-agent/tests/integration/test_prediction_knowledge.py:276](../trading-agent/tests/integration/test_prediction_knowledge.py#L276) | 分别检索同一结构在形成、突破等不同阶段的指引；预期只返回目标阶段的匹配知识卡片。 |
| 真实PG＋假行情客户端／过期冻结输入拒绝重试 | [trading-agent/tests/integration/test_prediction_market.py:71](../trading-agent/tests/integration/test_prediction_market.py#L71) | 让重试拿到已超新鲜度的冻结输入；预期在行情及模型I/O之前报CONTEXT_EXPIRED，不重新采数或追加输入。 参数：age_seconds=41。 |
| 真实PG＋假行情客户端／过期冻结输入拒绝重试 | [trading-agent/tests/integration/test_prediction_market.py:71](../trading-agent/tests/integration/test_prediction_market.py#L71) | 让重试拿到已超新鲜度的冻结输入；预期在行情及模型I/O之前报CONTEXT_EXPIRED，不重新采数或追加输入。 参数：age_seconds=181。 |
| 真实PG＋假行情客户端／历史复盘延迟重试 | [trading-agent/tests/integration/test_prediction_market.py:95](../trading-agent/tests/integration/test_prediction_market.py#L95) | 延迟已冻结历史复盘后再重试；预期复用原input_id和原复盘上下文，不重采今天行情，仍可提交合法复盘。 |
| 真实PG＋预设分钟行情／缺口游标恢复 | [trading-agent/tests/integration/test_prediction_market.py:118](../trading-agent/tests/integration/test_prediction_market.py#L118) | 先给缺分钟的验证窗口，再补齐缺口；预期先AWAITING_DATA、游标不越过缺口，补齐后恢复OK并保存真实分钟。 |
| 真实PG＋假行情／分钟发布延迟重试 | [trading-agent/tests/integration/test_prediction_market.py:150](../trading-agent/tests/integration/test_prediction_market.py#L150) | 组合有无持仓、缺口期间有无父申请，模拟刚收盘分钟暂不可得后补齐；预期有界重试及持仓监测继续，父等待完整验证后再恢复。 参数：with_position=否；request_during_gap=否。 |
| 真实PG＋假行情／分钟发布延迟重试 | [trading-agent/tests/integration/test_prediction_market.py:150](../trading-agent/tests/integration/test_prediction_market.py#L150) | 组合有无持仓、缺口期间有无父申请，模拟刚收盘分钟暂不可得后补齐；预期有界重试及持仓监测继续，父等待完整验证后再恢复。 参数：with_position=否；request_during_gap=是。 |
| 真实PG＋假行情／分钟发布延迟重试 | [trading-agent/tests/integration/test_prediction_market.py:150](../trading-agent/tests/integration/test_prediction_market.py#L150) | 组合有无持仓、缺口期间有无父申请，模拟刚收盘分钟暂不可得后补齐；预期有界重试及持仓监测继续，父等待完整验证后再恢复。 参数：with_position=是；request_during_gap=否。 |
| 真实PG＋假行情／分钟发布延迟重试 | [trading-agent/tests/integration/test_prediction_market.py:150](../trading-agent/tests/integration/test_prediction_market.py#L150) | 组合有无持仓、缺口期间有无父申请，模拟刚收盘分钟暂不可得后补齐；预期有界重试及持仓监测继续，父等待完整验证后再恢复。 参数：with_position=是；request_during_gap=是。 |
| 真实PG＋预设持仓／预测到期后的结构保护 | [trading-agent/tests/integration/test_prediction_market.py:272](../trading-agent/tests/integration/test_prediction_market.py#L272) | 先让绑定预测到期，再使原结构失效；预期单纯到期不平仓，结构失效仍只生成一次绑定原持仓的平仓意图。 |
| 真实PG／目标方向几何校验 | [trading-agent/tests/integration/test_prediction_pipeline.py:128](../trading-agent/tests/integration/test_prediction_pipeline.py#L128) | 分别提交上涨目标位于触发价下方、下跌目标位于触发价上方的报告，再纠正；预期先PREDICTION_GEOMETRY/422且无事实，纠正后可发布。 参数：direction=UP。 |
| 真实PG／目标方向几何校验 | [trading-agent/tests/integration/test_prediction_pipeline.py:128](../trading-agent/tests/integration/test_prediction_pipeline.py#L128) | 分别提交上涨目标位于触发价下方、下跌目标位于触发价上方的报告，再纠正；预期先PREDICTION_GEOMETRY/422且无事实，纠正后可发布。 参数：direction=DOWN。 |
| 真实PG／多周期独立与父自主综合 | [trading-agent/tests/integration/test_prediction_pipeline.py:175](../trading-agent/tests/integration/test_prediction_pipeline.py#L175) | 混合两币不同周期子报告，发布独立父预测；预期只复用有效同币子报告，缺周期明确，父期限和概率可独立判断，不照抄1h。 |
| 真实PG／旧父上下文和非法引用 | [trading-agent/tests/integration/test_prediction_pipeline.py:206](../trading-agent/tests/integration/test_prediction_pipeline.py#L206) | 在上下文变化后提交父报告，再用跨币种、未知知识或篡改边界的报告提交；预期逐项拒绝过期上下文和越范围引用。 |
| 真实PG／预测不可改与提交幂等 | [trading-agent/tests/integration/test_prediction_pipeline.py:241](../trading-agent/tests/integration/test_prediction_pipeline.py#L241) | 重复提交完全相同报告，随后改已发布内容、直接改数据库事实或用过期attempt写入；预期同内容复用ID，其余操作被拒绝。 |
| 真实PG／缺口、失效与子不得直接平仓 | [trading-agent/tests/integration/test_prediction_pipeline.py:257](../trading-agent/tests/integration/test_prediction_pipeline.py#L257) | 先保留分钟缺口，再补齐使子预测失效；预期追加评分修订、保留方向与结构结果区别，子仅触发后续分析而不直接生成交易平仓。 |
| 真实PG＋假行情／失败后冻结新证据 | [trading-agent/tests/integration/test_prediction_recovery.py:86](../trading-agent/tests/integration/test_prediction_recovery.py#L86) | 分别使父或子分析失败，等待退避后创建新任务并准备输入；预期失败留痕、审计关联，新任务使用新时点/input_id并能正式发布。 参数：kind=PREDICT_PARENT。 |
| 真实PG＋假行情／失败后冻结新证据 | [trading-agent/tests/integration/test_prediction_recovery.py:86](../trading-agent/tests/integration/test_prediction_recovery.py#L86) | 分别使父或子分析失败，等待退避后创建新任务并准备输入；预期失败留痕、审计关联，新任务使用新时点/input_id并能正式发布。 参数：kind=PREDICT_TIMEFRAME。 |
| 真实PG／新鲜重试有上限 | [trading-agent/tests/integration/test_prediction_recovery.py:158](../trading-agent/tests/integration/test_prediction_recovery.py#L158) | 连续触发超时及边界引用失败；预期有限重试后停止，保存每次真实错误，终端失败通知只入队一次。 |
| 真实PG／失败与替代任务同事务 | [trading-agent/tests/integration/test_prediction_recovery.py:182](../trading-agent/tests/integration/test_prediction_recovery.py#L182) | 在失败处理和排替代任务之间注入故障，再尝试旧fence处理；预期两步原子回滚，合法终止后旧授权不能越权改状态。 |
| 真实PG／复用已有新鲜任务 | [trading-agent/tests/integration/test_prediction_recovery.py:199](../trading-agent/tests/integration/test_prediction_recovery.py#L199) | 已有同币周期的新鲜排队任务时再次要求分析；预期返回同一个待处理任务，不重复创建分析。 |
| 真实PG＋假API/进程／拒收原因安全与发布优先 | [trading-agent/tests/integration/test_prediction_recovery.py:216](../trading-agent/tests/integration/test_prediction_recovery.py#L216) | 提交畸形参数并竞争合法发布，再启动研究恢复；预期返回安全字段级错误不泄露原敏感文本，已有正式发布获优先且不重复启动模型。 |
| 真实PG＋假研究进程／保留正式拒收原因 | [trading-agent/tests/integration/test_prediction_recovery.py:257](../trading-agent/tests/integration/test_prediction_recovery.py#L257) | 预先记录CONTEXT_CHANGED再令进程无正式结果退出；预期最终暴露原拒收原因，不笼统替换为无结果。 |
| 真实PG／监测游标与持仓修订区别 | [trading-agent/tests/integration/test_prediction_recovery.py:267](../trading-agent/tests/integration/test_prediction_recovery.py#L267) | 只更新监测游标，再修改真实交易持仓版本；预期前者不改变父决策上下文，后者必须使上下文发生变化。 |
| 真实PG／父在分钟缺口前等待 | [trading-agent/tests/integration/test_prediction_recovery.py:303](../trading-agent/tests/integration/test_prediction_recovery.py#L303) | 存在已知验证缺口时请求父输入和模型启动；预期MINUTE_DATA_PENDING，任务AWAITING_DATA，不采父行情、不冻结半成品，重复申请复用等待任务。 |
| 真实PG／恢复不得延长显式期限 | [trading-agent/tests/integration/test_prediction_recovery.py:342](../trading-agent/tests/integration/test_prediction_recovery.py#L342) | 让带显式截止时间的分析过期后进入恢复；预期任务SKIPPED，不创建延长原期限的新任务。 |
| 真实PG／已平持仓不进入当前上下文 | [trading-agent/tests/integration/test_prediction_recovery.py:361](../trading-agent/tests/integration/test_prediction_recovery.py#L361) | 关闭原持仓后请求父上下文；预期当前position及绑定引用为空，原CLOSED历史保留，不产生额外意图。 |
| 真实PG＋假行情／旧重复任务被新预测覆盖 | [trading-agent/tests/integration/test_prediction_recovery.py:384](../trading-agent/tests/integration/test_prediction_recovery.py#L384) | 分别对父或子准备较新的有效预测，再领取旧重复任务；预期ANALYSIS_SUPERSEDED并SKIPPED，不调用行情/模型，不追加输入或通知。 参数：kind=PREDICT_PARENT。 |
| 真实PG＋假行情／旧重复任务被新预测覆盖 | [trading-agent/tests/integration/test_prediction_recovery.py:384](../trading-agent/tests/integration/test_prediction_recovery.py#L384) | 分别对父或子准备较新的有效预测，再领取旧重复任务；预期ANALYSIS_SUPERSEDED并SKIPPED，不调用行情/模型，不追加输入或通知。 参数：kind=PREDICT_TIMEFRAME。 |
| 真实PG＋假行情／子报告变化仍需父重综合 | [trading-agent/tests/integration/test_prediction_recovery.py:420](../trading-agent/tests/integration/test_prediction_recovery.py#L420) | 已存在父任务时改变所引用的子报告集合；预期不能当作无效重复跳过，应重新综合，父不额外采自身行情。 |
| 真实PG／父等待不跨事务 | [trading-agent/tests/integration/test_prediction_recovery.py:446](../trading-agent/tests/integration/test_prediction_recovery.py#L446) | 让父等待评分提交并同时发生真实结构或持仓上下文变化；预期等待在事务外，等待后仍重新校验并报CONTEXT_CHANGED。 参数：lifecycle=ACTIVE。 |
| 真实PG／父等待不跨事务 | [trading-agent/tests/integration/test_prediction_recovery.py:446](../trading-agent/tests/integration/test_prediction_recovery.py#L446) | 让父等待评分提交并同时发生真实结构或持仓上下文变化；预期等待在事务外，等待后仍重新校验并报CONTEXT_CHANGED。 参数：lifecycle=INVALIDATED。 |
| 真实PG／分钟未决原因准确 | [trading-agent/tests/integration/test_prediction_recovery.py:505](../trading-agent/tests/integration/test_prediction_recovery.py#L505) | 让分钟验证持续未完成；预期按有界等待返回MINUTE_DATA_PENDING/503，不能误报上下文改变或覆盖原预测事实。 |
| 真实PG／失败检查与发布并发 | [trading-agent/tests/integration/test_prediction_recovery.py:529](../trading-agent/tests/integration/test_prediction_recovery.py#L529) | 在失败检查的两次读取之间让合法发布提交；预期最终任务SUCCEEDED绑定正式结果，不覆盖成失败、不重复事实或发失败通知。 |
| 真实PG＋假研究进程／stdout不能发布 | [trading-agent/tests/integration/test_prediction_runner.py:52](../trading-agent/tests/integration/test_prediction_runner.py#L52) | 并发两次执行同attempt，让进程打印伪预测及大量stdout后退出0；预期实际进程只运行一次，无领域API正式提交即报AGENT_NO_FORMAL_RESULT，资源清理。 |
| 真实PG＋假研究进程／已有发布不重启 | [trading-agent/tests/integration/test_prediction_runner.py:71](../trading-agent/tests/integration/test_prediction_runner.py#L71) | 先保存正式预测，再模拟CLI崩溃；预期直接复用正式结果，不重新准备沙箱或启动研究进程。 |
| 真实PG＋假研究进程／墙钟超时清理 | [trading-agent/tests/integration/test_prediction_runner.py:83](../trading-agent/tests/integration/test_prediction_runner.py#L83) | 启动长时间休眠进程并给很短预算；预期AGENT_TIMEOUT，进程和已准备资源清理，活动任务集合为空。 |
| 真实PG＋假Cube续租／续租失败终止研究 | [trading-agent/tests/integration/test_prediction_runner.py:99](../trading-agent/tests/integration/test_prediction_runner.py#L99) | 研究运行期间让续租控制调用失败；预期传播CUBE_UNAVAILABLE并终止进程、回收资源，不继续运行失去保护的任务。 |
| 真实PG＋假研究进程／运行期间持续续租 | [trading-agent/tests/integration/test_prediction_runner.py:126](../trading-agent/tests/integration/test_prediction_runner.py#L126) | 以短测试续租间隔运行研究并结束；预期多次续租的剩余预算递减，清理后停止续租且无正式提交则报无结果。 |
| 真实PG／逾期子任务调度优先级 | [trading-agent/tests/integration/test_prediction_scheduler.py:16](../trading-agent/tests/integration/test_prediction_scheduler.py#L16) | 同时排入到期子分析、新父任务、风险及主控任务；预期子优先于新父，但不能越过更高优先级的风险和主控工作。 |
| 真实PG／人工复盘取得下个模型槽 | [trading-agent/tests/integration/test_prediction_scheduler.py:41](../trading-agent/tests/integration/test_prediction_scheduler.py#L41) | 混合确定性监测、主控请求复盘及普通分析；预期监测仍先执行，主控复盘优先取得随后空闲模型槽，不无限饥饿。 |
| 真实PG／启动、暂停及独立周期任务 | [trading-agent/tests/integration/test_prediction_scheduler.py:84](../trading-agent/tests/integration/test_prediction_scheduler.py#L84) | 启动调度并重复tick，再暂停和推进时钟；预期两币四周期任务独立去重、验证继续，无退役FORECAST任务，暂停遵守既定调度边界。 |
| 真实PG／连续子更新合并父任务 | [trading-agent/tests/integration/test_prediction_scheduler.py:121](../trading-agent/tests/integration/test_prediction_scheduler.py#L121) | 连续发布多个子更新，等合并窗口关闭后调度；预期父输入冻结前合并更新，只排一个父综合任务。 |
| 真实PG＋预设分钟历史／复盘原证据与重建 | [trading-agent/tests/integration/test_review_candle_history.py:103](../trading-agent/tests/integration/test_review_candle_history.py#L103) | 分别为两币准备有原快照及无快照案例，并通过API按案例展开；预期保留原证据，有缺失才按当时完整分钟重建，查询只允许本复盘案例范围。 参数：symbol=BTCUSDT。 |
| 真实PG＋预设分钟历史／复盘原证据与重建 | [trading-agent/tests/integration/test_review_candle_history.py:103](../trading-agent/tests/integration/test_review_candle_history.py#L103) | 分别为两币准备有原快照及无快照案例，并通过API按案例展开；预期保留原证据，有缺失才按当时完整分钟重建，查询只允许本复盘案例范围。 参数：symbol=ETHUSDT。 |
| 真实PG＋假分钟客户端／重建缺失不伪造 | [trading-agent/tests/integration/test_review_candle_history.py:228](../trading-agent/tests/integration/test_review_candle_history.py#L228) | 分别制造分钟缺口、接口拒绝或未来数据；预期重建MISSING并保留具体原因，不生成假快照，原输入和hash不变。 参数：fault=gap；code=MINUTE_COVERAGE_GAP。 |
| 真实PG＋假分钟客户端／重建缺失不伪造 | [trading-agent/tests/integration/test_review_candle_history.py:228](../trading-agent/tests/integration/test_review_candle_history.py#L228) | 分别制造分钟缺口、接口拒绝或未来数据；预期重建MISSING并保留具体原因，不生成假快照，原输入和hash不变。 参数：fault=error；code=MARKET_REJECTED。 |
| 真实PG＋假分钟客户端／重建缺失不伪造 | [trading-agent/tests/integration/test_review_candle_history.py:228](../trading-agent/tests/integration/test_review_candle_history.py#L228) | 分别制造分钟缺口、接口拒绝或未来数据；预期重建MISSING并保留具体原因，不生成假快照，原输入和hash不变。 参数：fault=future；code=RECONSTRUCTION_SCOPE。 |
| 真实PG／过期复盘不写证据 | [trading-agent/tests/integration/test_review_candle_history.py:246](../trading-agent/tests/integration/test_review_candle_history.py#L246) | 使复盘attempt过期后准备新证据；预期授权失败，该复盘任务不新增冻结输入。 |
| 真实PG＋预设K线／复盘320根窗口 | [trading-agent/tests/integration/test_review_market_windows.py:40](../trading-agent/tests/integration/test_review_market_windows.py#L40) | 分别对两币准备四周期各80根复盘窗口并冻结预测输入，再重试；预期复盘320根、预测每周期展示30根，标的正确，重试原输入不变。 参数：symbol=BTCUSDT。 |
| 真实PG＋预设K线／复盘320根窗口 | [trading-agent/tests/integration/test_review_market_windows.py:40](../trading-agent/tests/integration/test_review_market_windows.py#L40) | 分别对两币准备四周期各80根复盘窗口并冻结预测输入，再重试；预期复盘320根、预测每周期展示30根，标的正确，重试原输入不变。 参数：symbol=ETHUSDT。 |
| 真实PG／非法复盘窗口整批回滚 | [trading-agent/tests/integration/test_review_market_windows.py:97](../trading-agent/tests/integration/test_review_market_windows.py#L97) | 注入缺周期、少K线、混币、时间缺口、错边界或未来可用时点；预期返回对应数据错误，相关表不留下部分窗口。 参数：fault=missing_timeframe；code=REVIEW_TIMEFRAMES。 |
| 真实PG／非法复盘窗口整批回滚 | [trading-agent/tests/integration/test_review_market_windows.py:97](../trading-agent/tests/integration/test_review_market_windows.py#L97) | 注入缺周期、少K线、混币、时间缺口、错边界或未来可用时点；预期返回对应数据错误，相关表不留下部分窗口。 参数：fault=short_window；code=BAR_COUNT。 |
| 真实PG／非法复盘窗口整批回滚 | [trading-agent/tests/integration/test_review_market_windows.py:97](../trading-agent/tests/integration/test_review_market_windows.py#L97) | 注入缺周期、少K线、混币、时间缺口、错边界或未来可用时点；预期返回对应数据错误，相关表不留下部分窗口。 参数：fault=mixed_symbol；code=REVIEW_MARKET_SCOPE。 |
| 真实PG／非法复盘窗口整批回滚 | [trading-agent/tests/integration/test_review_market_windows.py:97](../trading-agent/tests/integration/test_review_market_windows.py#L97) | 注入缺周期、少K线、混币、时间缺口、错边界或未来可用时点；预期返回对应数据错误，相关表不留下部分窗口。 参数：fault=gap；code=BAR_GAP。 |
| 真实PG／非法复盘窗口整批回滚 | [trading-agent/tests/integration/test_review_market_windows.py:97](../trading-agent/tests/integration/test_review_market_windows.py#L97) | 注入缺周期、少K线、混币、时间缺口、错边界或未来可用时点；预期返回对应数据错误，相关表不留下部分窗口。 参数：fault=wrong_boundary；code=BAR_BOUNDARY。 |
| 真实PG／非法复盘窗口整批回滚 | [trading-agent/tests/integration/test_review_market_windows.py:97](../trading-agent/tests/integration/test_review_market_windows.py#L97) | 注入缺周期、少K线、混币、时间缺口、错边界或未来可用时点；预期返回对应数据错误，相关表不留下部分窗口。 参数：fault=future_available；code=FUTURE_DATA。 |
| 真实PG／非法复盘窗口整批回滚 | [trading-agent/tests/integration/test_review_market_windows.py:97](../trading-agent/tests/integration/test_review_market_windows.py#L97) | 注入缺周期、少K线、混币、时间缺口、错边界或未来可用时点；预期返回对应数据错误，相关表不留下部分窗口。 参数：fault=future_anchor；code=FUTURE_DATA。 |
| 真实PG＋模拟HTTP／退役旧研究写入口 | [trading-agent/tests/integration/test_runtime_consolidation.py:36](../trading-agent/tests/integration/test_runtime_consolidation.py#L36) | 向旧写入口发送未认证或畸形请求，同时读取旧历史；预期先鉴权，授权后返回410退役，历史仍可读且事实摘要不变。 |
| 真实PG／恢复时跳过旧队列 | [trading-agent/tests/integration/test_runtime_consolidation.py:72](../trading-agent/tests/integration/test_runtime_consolidation.py#L72) | 恢复包含旧流程排队任务的数据库；预期旧任务统一SKIPPED并记录LEGACY_FLOW_RETIRED，已有结果与事实不改写。 |
| 真实PG＋模拟HTTP／旧结算评分审计可读 | [trading-agent/tests/integration/test_runtime_consolidation.py:120](../trading-agent/tests/integration/test_runtime_consolidation.py#L120) | 保存旧结算、评分和审计后经历史API读取；预期返回原结算ID和Brier，读取前后事实摘要相同。 |
| 真实PG／行情修订追加 | [trading-agent/tests/integration/test_storage.py:19](../trading-agent/tests/integration/test_storage.py#L19) | 重复保存相同K线，再改变价格内容保存；预期相同内容复用原ID和原可用时间，变化追加revision=2，旧内容仍存在。 |
| 真实PG／首根K线并发去重 | [trading-agent/tests/integration/test_storage.py:50](../trading-agent/tests/integration/test_storage.py#L50) | 多个事务并发保存同一初始K线；预期返回同一ID，表中仅一条修订。 |
| 真实PG／快照往返与外键 | [trading-agent/tests/integration/test_storage.py:63](../trading-agent/tests/integration/test_storage.py#L63) | 保存带320根K线引用的快照并重复保存，再构造非法引用；预期快照hash和关联完整，重复复用ID，错误外键拒绝。 |
| 真实PG／事实表不可变 | [trading-agent/tests/integration/test_storage.py:90](../trading-agent/tests/integration/test_storage.py#L90) | 直接尝试UPDATE、DELETE、TRUNCATE正式事实；预期数据库不可变保护拒绝，原K线值仍保留。 |
| 真实PG／最小应用角色权限 | [trading-agent/tests/integration/test_storage.py:105](../trading-agent/tests/integration/test_storage.py#L105) | 以新建受限角色执行事实修改及权限检查；预期无事实修改权限，合法业务所需权限齐全，不意外改变所有权代次。 |
| 真实PG＋假模型/行情/Cube／唯一runtime启动 | [trading-agent/tests/integration/test_structural_runtime.py:149](../trading-agent/tests/integration/test_structural_runtime.py#L149) | 启动唯一结构runtime，等待两币四子与父、复盘和索引流程；预期形成10个分析范围，复用同一服务，没有旧预测任务或交易账户副作用，退出无致命错误。 |
| 真实PG＋假公开API／双币预算公平 | [trading-agent/tests/integration/test_trade_minute_review.py:81](../trading-agent/tests/integration/test_trade_minute_review.py#L81) | 将每轮请求预算压得很小并执行两轮；预期每轮不超预算，资产轮转后BTC、ETH均能取得已验证分钟。 |
| 真实PG／历史抽查轮转 | [trading-agent/tests/integration/test_trade_minute_review.py:102](../trading-agent/tests/integration/test_trade_minute_review.py#L102) | 准备已验证旧分钟及冷启动回补积压；预期到期抽查优先进入计划，按最久未校验选择旧历史，不只盯最近分钟。 |
| 真实PG／分钟修订按可用时点可见 | [trading-agent/tests/integration/test_trade_minute_review.py:127](../trading-agent/tests/integration/test_trade_minute_review.py#L127) | 先保存完整小时的有效分钟，再追加一个较晚才可见的无效修订；预期旧时点仍看原OK，新时点降MISSING、59分钟且不输出伪买卖总量。 |
| 真实PG／仅短期缓存可删除 | [trading-agent/tests/integration/test_trade_minute_review.py:174](../trading-agent/tests/integration/test_trade_minute_review.py#L174) | 使用最小runtime角色清理过期原始缓存并检查事实表DELETE权限；预期缓存清理成功，正式行情/预测/评分/审计均不可删除。 |
| 真实PG＋假公开API／采集预算与事务边界 | [trading-agent/tests/integration/test_trade_minutes.py:110](../trading-agent/tests/integration/test_trade_minutes.py#L110) | 在所有权保护下运行有界采集，再推进两分钟并重启采集器；预期请求计数受限、两币分钟验证入库、不请求Rubik，网络I/O在业务事务外，任务不重复。 |
| 真实PG＋假公开API／异常补抓追加修订 | [trading-agent/tests/integration/test_trade_minutes.py:139](../trading-agent/tests/integration/test_trade_minutes.py#L139) | 先让逐笔总量与K线不一致，退避后返回正确数据补抓；预期旧MISSING保留，新OK作为第二修订追加，不改写旧记录。 |
| 真实PG＋假公开API／缓存过期与方向抽查 | [trading-agent/tests/integration/test_trade_minutes.py:187](../trading-agent/tests/integration/test_trade_minutes.py#L187) | 保存分钟后交换原始买卖方向再抽查，并清理过期缓存；预期记录分配变化审计，缓存可清理而长期分钟事实仍保留，非法事实修改拒绝。 |
| 真实PG／旧所有权不能落采集结果 | [trading-agent/tests/integration/test_trade_minutes.py:249](../trading-agent/tests/integration/test_trade_minutes.py#L249) | 先完成公开数据获取，再更换runtime所有权后落库；预期StaleAttempt，缓存及正式观测均不写入。 |
| 真实PG／预测读取分钟修订并冻结 | [trading-agent/tests/integration/test_trade_minutes.py:301](../trading-agent/tests/integration/test_trade_minutes.py#L301) | 准备完整20小时分钟事实、冻结预测，再追加较晚修订；预期CVD来源为分钟聚合，旧/新时点窗口不同，原预测仍复用原输入及指标。 |
| 真实PG／分钟缺失不回退缺陷统计源 | [trading-agent/tests/integration/test_trade_minutes.py:343](../trading-agent/tests/integration/test_trade_minutes.py#L343) | 分钟库无完整覆盖而传入旧Rubik主动成交样本；预期CVD为MISSING，旧统计不会作为替代输入落库。 |
| 真实PG＋假公开API／程序退避修复 | [trading-agent/tests/integration/test_trade_minutes.py:376](../trading-agent/tests/integration/test_trade_minutes.py#L376) | 先制造分钟异常，推进到退避之后让源恢复并再次调度；预期程序自动补抓验证，任务状态VERIFIED，不依赖模型采集工具。 |
| 真实PG＋假采集器／采集器启停顺序 | [trading-agent/tests/integration/test_trade_minutes.py:411](../trading-agent/tests/integration/test_trade_minutes.py#L411) | 分别配置分钟来源或旧统计来源并启动/关闭runtime；预期仅分钟来源构造采集器，先取得所有权再启动，先停止采集器再关闭HTTP客户端，覆盖两币。 参数：source=trade_minutes。 |
| 真实PG＋假采集器／采集器启停顺序 | [trading-agent/tests/integration/test_trade_minutes.py:411](../trading-agent/tests/integration/test_trade_minutes.py#L411) | 分别配置分钟来源或旧统计来源并启动/关闭runtime；预期仅分钟来源构造采集器，先取得所有权再启动，先停止采集器再关闭HTTP客户端，覆盖两币。 参数：source=rubik。 |

## 固定Hermes契约测试（13例）

| 测试范围 | 代码文件 | 怎么测，预期是什么 |
| --- | --- | --- |
| 固定Hermes契约（假模型/API）／四子生命周期 | [trading-agent/tests/hermes/test_analysis_plugin.py:14](../trading-agent/tests/hermes/test_analysis_plugin.py#L14)<br>[调用探针：round_probe.py](../trading-agent/tests/hermes/round_probe.py#L1) | 加载固定Hermes原生生命周期及实际插件，以假模型和假领域API执行四子并行、局部重试和父综合；预期角色、输入和预算边界有效。 本变体让4h首次失败，只重试4h；四子并发峰值4，父只启动一次。 参数：mode=retry。 |
| 固定Hermes契约（假模型/API）／四子生命周期 | [trading-agent/tests/hermes/test_analysis_plugin.py:14](../trading-agent/tests/hermes/test_analysis_plugin.py#L14)<br>[调用探针：round_probe.py](../trading-agent/tests/hermes/round_probe.py#L1) | 加载固定Hermes原生生命周期及实际插件，以假模型和假领域API执行四子并行、局部重试和父综合；预期角色、输入和预算边界有效。 本变体让4h首次超时，确认取消后只重试4h；其他有效角色保留。 参数：mode=timeout。 |
| 固定Hermes契约（假模型/API）／四子生命周期 | [trading-agent/tests/hermes/test_analysis_plugin.py:14](../trading-agent/tests/hermes/test_analysis_plugin.py#L14)<br>[调用探针：round_probe.py](../trading-agent/tests/hermes/round_probe.py#L1) | 加载固定Hermes原生生命周期及实际插件，以假模型和假领域API执行四子并行、局部重试和父综合；预期角色、输入和预算边界有效。 本变体在父启动前撤回6h依赖，要求补齐6h，再只运行一次父综合。 参数：mode=expanded。 |
| 固定Hermes契约（假模型/API）／四子生命周期 | [trading-agent/tests/hermes/test_analysis_plugin.py:14](../trading-agent/tests/hermes/test_analysis_plugin.py#L14)<br>[调用探针：round_probe.py](../trading-agent/tests/hermes/round_probe.py#L1) | 加载固定Hermes原生生命周期及实际插件，以假模型和假领域API执行四子并行、局部重试和父综合；预期角色、输入和预算边界有效。 本变体令4h取消无法确认；预期CHILD_CANCELLATION_PENDING，父不启动、不创建替代子进程。 参数：mode=pending。 |
| 固定Hermes契约（假模型/API）／四子生命周期 | [trading-agent/tests/hermes/test_analysis_plugin.py:14](../trading-agent/tests/hermes/test_analysis_plugin.py#L14)<br>[调用探针：round_probe.py](../trading-agent/tests/hermes/round_probe.py#L1) | 加载固定Hermes原生生命周期及实际插件，以假模型和假领域API执行四子并行、局部重试和父综合；预期角色、输入和预算边界有效。 本变体给出超长子上下文；预期模型侧收到有界摘要并通过只读工具回读完整30＋1证据，四子各只运行一次。 参数：mode=large。 |
| 固定Hermes契约／认证预检 | [trading-agent/tests/hermes/test_credentials.py:16](../trading-agent/tests/hermes/test_credentials.py#L16) | 在有、无测试认证的隔离Profile运行预检；预期只输出configured布尔值及对应退出码，不输出秘密、不调用模型。 参数：configured=否。 |
| 固定Hermes契约／认证预检 | [trading-agent/tests/hermes/test_credentials.py:16](../trading-agent/tests/hermes/test_credentials.py#L16) | 在有、无测试认证的隔离Profile运行预检；预期只输出configured布尔值及对应退出码，不输出秘密、不调用模型。 参数：configured=是。 |
| 固定Hermes契约（假Cube）／取消隔离 | [trading-agent/tests/hermes/test_cube_commands.py:13](../trading-agent/tests/hermes/test_cube_commands.py#L13)<br>[调用探针：cube_command_probe.py](../trading-agent/tests/hermes/cube_command_probe.py#L1) | 在兄弟命令会话并存时取消其中一个；预期只取消目标命令，兄弟继续运行且使用不同会话目录。 |
| 固定Hermes契约／并发插件加载 | [trading-agent/tests/hermes/test_loader_repair.py:12](../trading-agent/tests/hermes/test_loader_repair.py#L12) | 在后台并发导入模块时反复发现并加载插件；预期兼容入口不因活动模块字典变化而失败。 |
| 固定Hermes契约（假模型/API）／工具发现与角色边界 | [trading-agent/tests/hermes/test_plugins.py:22](../trading-agent/tests/hermes/test_plugins.py#L22)<br>[调用探针：probe.py](../trading-agent/tests/hermes/probe.py#L1) | 分别用主控桥接、结构研究、Cube取消、普通对话Profile加载原生插件和委派包装；预期只暴露对应角色获准的工具，间接调用仍受权限检查。 本变体只启主控桥接，检查研究工具不可用及有界任务申请路由。 参数：mode=bridge。 |
| 固定Hermes契约（假模型/API）／工具发现与角色边界 | [trading-agent/tests/hermes/test_plugins.py:22](../trading-agent/tests/hermes/test_plugins.py#L22)<br>[调用探针：probe.py](../trading-agent/tests/hermes/probe.py#L1) | 分别用主控桥接、结构研究、Cube取消、普通对话Profile加载原生插件和委派包装；预期只暴露对应角色获准的工具，间接调用仍受权限检查。 本变体检查预测角色工具发现、委派和输入/引用绑定，模型与API均为替身。 参数：mode=structural。 |
| 固定Hermes契约（假模型/API）／工具发现与角色边界 | [trading-agent/tests/hermes/test_plugins.py:22](../trading-agent/tests/hermes/test_plugins.py#L22)<br>[调用探针：probe.py](../trading-agent/tests/hermes/probe.py#L1) | 分别用主控桥接、结构研究、Cube取消、普通对话Profile加载原生插件和委派包装；预期只暴露对应角色获准的工具，间接调用仍受权限检查。 本变体检查原生Cube取消包装与角色边界，使用测试替身。 参数：mode=cube_cancel。 |
| 固定Hermes契约（假模型/API）／工具发现与角色边界 | [trading-agent/tests/hermes/test_plugins.py:22](../trading-agent/tests/hermes/test_plugins.py#L22)<br>[调用探针：probe.py](../trading-agent/tests/hermes/probe.py#L1) | 分别用主控桥接、结构研究、Cube取消、普通对话Profile加载原生插件和委派包装；预期只暴露对应角色获准的工具，间接调用仍受权限检查。 本变体为预测TG普通对话；预期工具白名单为空，不能借对话调用研究写入。 参数：mode=dialogue。 |

## 需要显式启用的真实Cube验收（3例）

| 测试范围 | 代码文件 | 怎么测，预期是什么 |
| --- | --- | --- |
| 真实Cube显式验收（合成负载）／双沙箱容量 | [trading-agent/tests/live/test_cube_capacity.py:64](../trading-agent/tests/live/test_cube_capacity.py#L64) | 仅在显式配置和独占空闲窗口下创建两个真实沙箱，三轮注入受限内存负载并采样宿主；预期两任务完成、资源上限及内存余量满足条件、无新增OOM且清理确认，不调用模型或写交易事实。 |
| 真实Cube显式验收／原生命令与续期 | [trading-agent/tests/live/test_cube_connection.py:25](../trading-agent/tests/live/test_cube_connection.py#L25)<br>[调用探针：cube_live_probe.py](../trading-agent/tests/hermes/cube_live_probe.py#L1) | 在真实Cube创建受限沙箱，通过固定Hermes原生terminal执行命令并多次延长超时；预期SDK/命令链路可用、同一沙箱期限正确延长、归属匹配且最终销毁。 |
| 真实Cube显式验收／完整隔离 | [trading-agent/tests/live/test_cube_isolation.py:37](../trading-agent/tests/live/test_cube_isolation.py#L37)<br>[调用探针：cube_isolation_probe.py](../trading-agent/tests/hermes/cube_isolation_probe.py#L1) | 显式配置后检查普通用户、只读输入、可写scratch、禁sudo/guest管理、无宿主挂载和凭据、网络限制、fence、崩溃归属回收、资源上限及真实原生命令；预期全部必需项通过才发布新的live报告并确认清理。 |
