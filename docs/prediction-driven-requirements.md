> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 需求对照及额外功能

本表描述本地实现，在线效果不在“已实现”含义内。[实现与使用](prediction-driven-implementation.md)给出完整调用链，[验证与限制](prediction-driven-validation.md)区分证据。

| 用户要求 | 实现位置及行为 | 本地验证 |
|---|---|---|
| 仅hermes-trade、保留现有未提交改动 | 根目录基线清单；未commit/push/部署 | Git差异及基线核对 |
| BTC/ETH独立1h/4h/6h/12h | PredictionScheduler、按engine/symbol/tf/role作用域 | pipeline、scheduler、structural_runtime |
| 每币种预测30×4=120根、复盘80×4=320根 | 保留预测末尾30根；复盘独立冻结四组80根，后台计算仍为80根/周期 | review_market_windows、structural_runtime |
| 多根K线趋势和涨跌空间 | 情景触发/失效/目标区间/期限；不同于下一根K线分类 | contracts、structure_predictions |
| 上升三角、W底、箱体、收敛三角 | shared structures及确认关键点、阶段、斜线边界 | structure_predictions |
| 父Agent自主综合 | 独立概率/目标与交易意图；要求有效子引用，未机械平均 | pipeline、原生structural插件测试 |
| 父结果驱动下单，保留交易规则 | PredictionRiskAdapter直接进入RiskEngine；不经旧方向信号/Advisor | 旧框架回归、risk_adapter、execution |
| 原趋势/震荡风控区别 | 共用ATR/倍率与区间边界/中点公式；动态杠杆仓位和账户限额 | 参数等价及原框架207项 |
| 预测目标不覆盖订单TP | forecast_target独立保存；实际TP由共用风险参数生成 | execution及risk_adapter |
| 独立生命周期、任一更新即可综合 | 事件任务、有效旧预测复用、未冻结任务合并、发布前重验 | pipeline、scheduler、runtime |
| 每分钟补数据，缺口不交易 | 连续游标；父子验证进度检查；过期/被替代意图拒绝 | market、execution |
| 到期继续持仓 | 不产生到期CLOSE；现有退出与独立持仓结构游标继续 | market、execution |
| 失效/转向提出平仓 | 父失效绑定position_id/revision；反手只生成CLOSE | pipeline、execution |
| 子周期反向不直接平仓 | 子变化仅排父任务 | pipeline |
| 无仓不平仓，旧意图不关新仓 | 意图生成/执行二次绑定检查 | execution |
| 趋势、目标、边界、时间分开评分 | 追加EvaluationEvent与状态，Brier与分桶命中 | structure_predictions、knowledge |
| 先失效后达标仍失败、同分钟不猜先后 | INVALIDATED/AMBIGUOUS；结束后原窗口继续观察 | structure_predictions、pipeline |
| 未触发/未决/缺数据保留 | 不删除事实；统计标注不可评分，不伪造样本 | evaluation、knowledge、horizons |
| 知识点题库，而非迭代日志摘要 | knowledge_versions、case_links多对多；summary/content/procedure | knowledge |
| 父子Agent实际读知识 | v2 context/search/content及原生工具，冻结recall | API、knowledge、Hermes插件 |
| 只有复盘能修改知识 | review-only授权、证据时点及版本父链检查 | knowledge、API |
| PG正式事实，SQLite可恢复 | 异步幂等索引；同版本PG回退；冻结引用不变 | knowledge |
| 预测Agent继续学习迭代 | 每日知识新增/修订/合并/退役，程序期限学习 | knowledge、horizons、runtime |
| 40回放/20影子，达标后自动生效/回滚 | 候选固定、配对效用/失效率门槛、每20新样本复查 | horizon_learning、horizon_pipeline |
| 不修改历史预测期限和评分 | append-only事实触发器及新规则版本 | PG不可变测试、horizon_pipeline |
| 删除策略代码迭代角色及旧经验活跃入口 | CLI/插件移除iterate及memory-search/read；AGENTS更新、prompt归档 | 原生兼容插件、CLI退役测试 |
| 保留历史原始证据 | compat历史模块及记录保留，v1历史查询保留 | 旧历史与兼容回归 |
| 单runtime、租约/权限延续 | 同一Runtime，PG事务guard，外部I/O在事务外 | jobs、runtime、execution |
| 重试/重启/部分成交不重复下单 | 稳定client ID、SENDING先行、查询未知、增量入账 | execution及transport fixture |
| 本地实现、不操作线上 | 执行默认关闭；全部订单测试为fixture | 未做部署和真实订单调用 |
| 开发细节和额外功能全部说明 | 四份交付文档、Feature记录 | 文档对照与链接检查 |

## 用户未逐条指定、为完成实现增加的配套

1. 模式开关、旧风险配置目录、账户标识、180秒输入新鲜度、90秒执行价格新鲜度。
2. 结构版本与候选hash、报告schema3/API v2、保留旧历史契约。
3. 分钟完整观察窗、1～5分钟条件确认、UNKNOWN回退边界、多个结构候选。
4. 同币种事务锁、账户锁、代际版本、任务合并、过时输入新任务、模型单并发及确定性四槽。
5. 中文双字FTS5、500个PG回退候选、6000/12000正文分页、不可变recall与索引降级记录。
6. 四份未验证种子procedure；复盘90天/200样本上限；期限每组200预测扫描上限。
7. 等待成本每根0.01、父/子1h同组期限规则、不可判定配对单独统计。
8. 风控参数共用抽取；震荡单几何参数校验；保留社媒风险作用但不加入其方向裁决。
9. 每次订单准备读取现有运行/风险/策略配置，保存实际参数；发送前暂停/策略参数变化拒绝旧准备结果。
10. PREPARED/SENDING/UNKNOWN/PARTIAL等持久状态、稳定订单ID、增量费用/成交记账、未知状态占用账户。
11. 原订单未成交余量撤销、部分平仓余量保留、实际剩余合约数量平仓、持仓结构补齐游标。
12. 固定保护随开仓请求附带、实际成交价的软件退出；交易所自主退出的只读对账和唯一事实核验。
13. 首次旧账户迁移/engine版本切换遇持仓或未决订单时拒绝自动切换；不擅自认领外部仓位。
14. 新CLI及Hermes bridge只读查询工具；旧迭代工具不可再调用，历史模块可离线审计。
15. 持久账户硬风控暂停及operator恢复；整仓结束才记一次输赢，累计费用与原持仓盈亏同步。
16. 沙箱结构契约白名单、部署engine标识一致、复盘提交后的崩溃恢复、控制插件v1/v2地址兼容及异常交换数据校验。
17. injected clock修正、测试fixture包显式初始化、本地PG端口58917、wheel/依赖资源检查、基线hash记录。

## 明确的边界

没有新增一个改写交易策略源码的Agent，没有向量数据库，没有收益优化器，没有证明结构预测有效的真实样本，没有自动部署/Profile切换/线上开关修改。模拟执行没有加入自创滑点或交易费模型。在线凭证、实际成交保护和模型质量仍须验收。
