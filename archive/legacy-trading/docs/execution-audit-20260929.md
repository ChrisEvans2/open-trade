> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 交易与执行链路专项审计（2026-09-29）

## 结论

> 本节包含私有账户、部署、预测/复盘或个人知识库记录，公开版隐去原始内容；技术方法保留在其他章节。

## 真实 API 验证（仅 GET）

> 本节包含私有账户、部署、预测/复盘或个人知识库记录，公开版隐去原始内容；技术方法保留在其他章节。

## 零交易漏斗

> 本节包含私有账户、部署、预测/复盘或个人知识库记录，公开版隐去原始内容；技术方法保留在其他章节。

## 可复现的执行缺陷

### E1 / P1：零成交订单被记为成交成功

- 位置：`/srv/open-trade-deployment/src/trading_system/execution/live.py:59`、`:92`、`:99`。
- 执行器查一次订单，不检查 state，随后将缺失 avgPx 用行情价补齐、零 accFillSz 用请求张数补齐，并返回 success=true。
- 离线分别返回 state=live 和 state=canceled、accFillSz=0，均得到 success=true、fill_price=100000、exchange_contracts=0.01。
- 影响：可能虚构本地持仓/平仓，再被后续对账误判。订单受理不等于成交，OKX 明确区分 live、partially_filled、filled、canceled。[OKX 订单状态说明](https://www.okx.com/docs-v5/en/)
- 修复验收：零成交不能记仓；按真实累计成交量处理部分成交；有界轮询与未知状态恢复；退出时必须确认交易所剩余仓位。

### E2 / P1：成交后保护单失败，会丢失成交状态并留下重复提交风险

- 位置：`/srv/open-trade-deployment/src/trading_system/execution/live.py:69`、`:78`、`:92`；调度器在 execution_result.success=false 时不更新本地仓位。
- 开仓、成交查询、固定保护单和移动保护单共用一个异常分支。主订单已经成交、保护单接口失败时，仍返回笼统 failure，缺少 order/成交信息，executed_ids 也没有登记。
- 离线保护单接口抛错，结果 success=false、order=null、保护单数 0；直接重试相同 client_order_id 的 execute 会再次调用 place_order，共两次。
- 这证明执行器本身没有安全的重试契约，**不代表真实守护进程已经重复成交**：下一轮对账可能因发现未知仓位而紧急停止。但紧急停止本身不会补建保护单，也不能替代成交恢复。
- OKX 的 clOrdId 仅对当前挂单要求唯一，已终结订单的同一 ID 可再次使用，不能把它当成永久去重键。[OKX clOrdId 规则](https://www.okx.com/docs-v5/en/)
- 修复验收：分离“提交/成交/保护完成”状态；在网络不确定和保护失败后先查询恢复，禁止盲目重发；持久化交易所订单 ID 与成交数量；为保护失败设计明确恢复流程。

### E3 / P1：平仓重新计算张数，部分平仓又清空整个本地仓位

- 位置：`/srv/open-trade-deployment/src/trading_system/execution/live.py:211`、`:217`；`/srv/open-trade-deployment/src/trading_system/scheduler.py:345`。
- CLOSE 沿用权益×保证金比例×杠杆÷现价的开仓公式，没有使用已经记录的实际合约张数。
- 复现一：实际持有 0.01 张，价格从 100000 上涨至 110000 后，重算并按步长取整得到 0.00 张，报低于最小张数；原来的 0.01 张本来可以平仓。
- 复现二：本地 0.1 张，交易所返回仅成交 0.01 张的 partially_filled；调度器仍删除整个本地仓位，未保留剩余 0.09 张。
- 修复验收：平仓使用交易所核对后的可平张数；按实际成交减少持仓；只有确认剩余为零才清仓、统计完整平仓并清理保护单。

### E4 / P1：持仓对账忽略同币种的方向和数量差异

- 位置：`/srv/open-trade-deployment/src/trading_system/scheduler.py:950`、`:972`。
- 对账比较的是 instId 集合；双方都有 BTC-USDT-SWAP 就可能通过，不核对符号方向和张数。
- 复现一：本地多 0.1 张、交易所空 0.1 张，返回 True。
- 复现二：本地多 0.1 张、交易所只剩多 0.01 张，仍返回 True。
- 修复验收：逐合约核对方向、实际张数和成交增量，以交易所为依据处理偏差；不能把差异直接视为成功同步。

## 策略安全检查缺口

### S1 / P1：禁止行情配置没有进入候选安全检查

- `block_regimes` 在 `/srv/open-trade-deployment/src/trading_system/config.py:63` 声明、在 app.yaml 配置，但运行代码未引用此字段。
- 离线输入 regime=range，配置明确包含 range，且给出满足其他检查的完整多周期候选；CodeSafetyGate 仍 accepted=true、输出 long。
- `cooldown_bars_after_loss`、`no_reverse_after_stop_bars` 也只见配置声明，未发现对应执行检查；这是配置覆盖缺口，不是近期空仓原因。
- `max_slippage_pct` 同样只见风控参数声明，未发现实际执行校验。其具体可接受实现需要结合市场单与成交后处理设计，不能靠设置这个数字声称滑点已受限。

### S2 / P2：失效价没有方向校验

- 位置：`/srv/open-trade-deployment/src/trading_system/ai/market_advisor.py:500`、`:504`。
- 仅用 abs(entry-invalidation) 检查距离，做多入场 101.7、失效价 102.8（在上方）也通过安全检查。
- 执行器后续会按止损百分比重新生成下方止损，因此这里的缺陷是**错误结构失效位被接受**，不能据此声称它直接发送了上方止损单。
- 修复验收：多头失效价必须低于入场价，空头必须高于；保持原有距离和风险预算检查。

## 其他运行边界

- `sync_market_state` 在实盘模式下于本地风险退出计算前直接 return（scheduler.py:197）。5 分钟刷新主要依赖交易所已有保护单，不应被描述为能够兜底所有本地止损；主策略周期仍会检查退出。若发生 E2，保护状态恢复尤其重要。
- AI 子进程错误截取 stderr 开头，归档只保留版本和启动参数，真实错误可能丢失；超时后没有显式 kill/wait 清理分支。这些属于可观测性与资源回收缺口，本次没有把它们认定为长期空仓根因。

## 验证与复现

```bash
python -m pytest -q /srv/open-trade-deployment/tests
# 201 passed in 2.02s

python /srv/open-trade-deployment/scripts/audit_execution_paths.py
# 10 个模拟场景复现违反安全契约；JSON 的 violation=true 表示缺陷存在

git diff --check
# 通过
```

离线脚本所有交易所方法均为内存假对象，并将 httpx.AsyncClient 替换为禁止联网的断言。不会创建真实执行客户端、加载密钥、写账户状态或调用真实交易所写接口。

正向对照使用当前 app.yaml/risk.yaml 配置、合法技术信号及成功的四周期/Meta 结果，通过真实 CodeSafetyGate、RiskEngine、GPT 可用性检查，到达假执行器：allow_trade=true、1 次模拟下单、1 个模拟固定保护单、success=true。它证明软件存在可通行路径，不能代替真实交易所写接口验收。

逐场景结果：`/srv/open-trade-deployment/docs/execution-audit-20260929.json`。

## 修复优先级与边界

1. 优先修复成交状态确认、保护失败恢复、订单去重与平仓数量/剩余仓位。它们共同影响真实资金状态，应该作为一组回归场景验证，不能只把 success 标志改成 false 就认为修复完成。
2. 补齐方向/张数对账、已配置的候选禁入条件与失效价方向检查；保持止损、仓位和风险预算不变。
3. 改善 AI 错误分类与进程回收，记录每一层的候选数量及具体拒绝原因。
4. 以上完成后再评估策略是否过度过滤；不为制造成交而关闭 GPT 门禁或放宽风险参数。

本次结论区分“导致这段时间没交易的证据”和“未来成交时可能触发的漏洞”。后者已经在模拟回执下复现，但没有证据表明这些场景已发生在当前真实账户。未改动或重启生产执行服务，发现项仍待修复。
