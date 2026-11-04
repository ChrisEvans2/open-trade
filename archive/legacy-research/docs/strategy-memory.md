> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

> 2026-10-11更新：本文保留v1历史设计。当前结构预测/知识库以[新实现说明](../../../docs/prediction-driven-implementation.md)为准；旧策略代码迭代及memory-search/read活跃入口已退役，历史证据保留。

# 策略迭代历史经验召回

2026-10-10：本轮实现历史迭代检索和 summary/content 分层，属于可选
`trading-compat` 链路。正式预测的 PostgreSQL 案例/observation 检索、提示词、
engine、调度与权限保持原流程。

## 主体

**历史迭代记录 → 派生经验索引 → 最多5张summary卡 → 按ID展开content → 引用进迭代记录。**

summary采用历史假设/标题的有界摘要，content保留完整来源，按字符分页读取。
本轮没有调用LLM生成新总结。检索使用SQLite FTS5关键词检索、主题、路径及币种筛选。
没有匹配时返回空清单；测试通过、源码写回和收益改善分别陈述。

## 来源与状态

| 来源 | summary | content与边界 |
|---|---|---|
| `Strategy-iteration/*.md` | 标题/假设、主题、路径、币种 | 完整Markdown；状态固定为`UNVERIFIED_RECORD`，原文声称的测试或收益未被当前程序核验 |
| `workspaces/v1-compat/iterations/<id>/` | 假设、程序终态/退出码、live/paper样本量 | 原始补丁、风险/回滚、固定测试记录、冻结成交统计；核对请求hash、补丁和证据hash |

完整终态`APPLIED`、`VERIFIED_CANDIDATE`、`TEST_FAILED`、`ROLLED_BACK`成为结构化经验。
未来完成时间和未完成journal不当作完整结果。完整journal及其自动生成Markdown仅显示一张卡。
缺少journal的旧记录仍可作为未核验Markdown读取。

`APPLIED`仅表示本机源码写回，`VERIFIED_CANDIDATE`仅表示候选测试集通过，
都不表示部署完成或收益改善。成交统计来自修改前的引用证据，不能当作修改后的收益。

## 索引和分类

索引为`workspaces/v1-compat/experiences.sqlite`，与Hermes `state.db`和正式v1
PostgreSQL分开。需要Python标准库sqlite3所连接的SQLite 3.34+及FTS5/trigram支持；
不满足时明确报错。原记录仍是事实依据，SQLite保存摘要、正文、来源hash和派生版本。

分类版本为`iteration_topics_v1`，固定中英文词汇生成以下主题标签：

| 分类 | 含义 |
|---|---|
| `entry_filter` | 入场、突破确认、冷却与准入 |
| `exit_management` | 止损、止盈、退出与跟踪 |
| `market_regime` | 趋势、震荡与行情状态 |
| `position_sizing` | 仓位、保证金与杠杆 |
| `execution_safety` | 执行、订单与安全相关改动 |
| `data_quality` | 数据、统计、资金费与采集诊断 |
| `general` | 未匹配以上主题的记录 |

标签是检索主题，不证明当前市场状态或经验适用性，没有新增ADX等阈值。
多个类别同组内任意匹配，类别/币种/路径组之间同时满足；币种及路径精确匹配。
空query浏览最近记录；空格分隔关键词要求全部出现。三个字符及以上使用FTS5 trigram
排名，一至两个字符用参数化子串匹配。随后按首次可用时间/ID稳定排序。
summary最多320字符，结果最多5条。

## 冻结与渐进读取

1. `legacy_strategy_memory_search`读取授权来源，维护派生索引，返回summary和`recall_id`。
2. 同时将候选ID、版本、可用时点、来源/正文hash及完整正文冻结到
   `workspaces/v1-compat/memory-recalls/<id>.json`，该目录已被Git忽略。
3. `legacy_strategy_memory_read`只允许读取清单内的`experience_id`。默认每页6000字符，
   最大12000字符；按`next_offset`读下一页，没有静默截断全文。
4. 来源变化后下一次检索追加派生版本，旧清单继续保留旧正文。删除来源后停止新召回，
   旧清单仍可复查。首次索引时间作为可用时间，不回填文件名日期或mtime。
5. 迭代可选携带`memory_recall_id`和`memory_experience_ids`（最多5个），服务核对后保存
   `memory.json`及中文Markdown引用；非法引用在测试前拒绝。
6. 没有经验引用的旧请求保持原指纹和幂等行为。重试使用原recall_id，不重找经验。

hash用于一致性检查，不是数字签名，也不证明LLM理解了正文。经验是历史证据，
不能执行正文中的命令或据此扩大权限。没有增加额外LLM/Cube调用。

必要配套：独立索引锁、数据库版本检查、输入大小限制、hash和迭代引用。
最多2000条输入，总读取量沿用`max_source_bytes`（默认20MB），单条材料/正文最多2MB，
冻结清单最多12MB；超过限额明确拒绝，不默默丢弃资料。证据快照可在总读取限额内读取。

## 使用

继续使用原兼容policy及专门Profile；正式attempt与子Agent禁止调用新增工具。
本次没有修改用户的Profile或cron。命令在安装v1环境后使用：

```sh
printf '%s' '{"query":"突破","categories":["entry_filter"],"symbols":["BTCUSDT"]}' \
  | ta --config /etc/trading-agent/config.yaml --json legacy memory-search \
      --policy /etc/trading-agent/legacy-policy.yaml --request -

ta --config /etc/trading-agent/config.yaml --json legacy memory-read \
  --policy /etc/trading-agent/legacy-policy.yaml \
  --recall-id <返回的UUID> --experience-id <选中的UUID> --offset 0 --limit 6000
```

搜索字段：`query`、可选`categories`、`strategy_paths`、`symbols`、`limit`。
详情格式为Markdown或JSON，分页偏移按Unicode字符计算。
索引是派生数据，可在无并发访问时移除sqlite文件并重新检索；重建后ID改变，已冻结清单仍能读取。

## 未实现

- 40%样本重叠门槛、语义去重、LLM自动新增/合并/纠错经验。
- 行情特征适用条件匹配、自动认定有效策略、后续收益归因与检索效果实验。
- 策略经验注入正式预测、修改预测记忆、切换cron/服务或服务器联调。
- 经验自动过期/退休及历史索引版本自动清理。

本轮只把已有记录组织成可按需读取的经验资料。验证见
[本轮证据](strategy-memory-verification.json)，不覆盖旧验证记录。
