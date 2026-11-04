> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 开发仓到公开仓自动同步交付（2026-11-04）

已安装并启用独立维护定时任务：从 `/srv/open-trade` 的 `dev` 工作区向 `/srv/open-trade-public` 的 `main` 导出审核范围内的文件，自动验证、生成公开提交并推送。它是文件发布任务，不是交易 runtime。

首次公开提交：`<private-artifact-hash>`，已与 [GitHub main](https://github.com/ChrisEvans2/open-trade/tree/main) 核对一致。公开历史为独立根提交，264 个文件，没有携带私有仓祖先。公开 Git 使用用户指定的 `chrisevans2` 及已确认的历史 QQ 邮箱；邮箱仅保留在 Git 身份/提交元数据，不抄入本说明。

## 日常行为

- systemd 每分钟检查一次；白名单文件内容稳定 120 秒后才生成候选版本，验证耗时另计。包含尚未提交的本地改动，不要求先在私有仓 commit。
- 原审核清单中 237 个源文件受控同步。代码目录新增的非忽略 Python 文件和结构提示词目录新增 Markdown 可自动纳入；其他新格式不自动公开。
- 私人文档、账户记录、Profile、凭据、状态和本地工具不在导出范围内。公开自有 README、文档、示例及通用 paper 服务模板保留。
- 三方合并保留公开脱敏适配；重叠改动、公开工作树/暂存区/分支/远端独立变化均阻断。不会合并私有分支、force push 或覆盖维护者工作。
- 隐私、合并和测试失败阻断，同一内容快照退避 900 秒再尝试；源更新后重新等待稳定。推送失败保留已成功 commit 的版本，下轮重试该提交，不制造重复 commit。
- 验证期间源再次变化会放弃该候选，下次重新检查。无文件变化只检查状态，不重复测试或提交。

## 发布门禁及本次证据

每次候选先执行路径/类型/大小、已知身份与凭据字面量检查和固定版本 Gitleaks 全树扫描；再使用独立依赖环境执行核心、runtime 单元、固定 Hermes 契约、Ruff/格式及隔离真实 PostgreSQL 定向集成。改动的集成测试额外追加。成功后生成公开 changelog，最终树再扫描，复核源及公开仓状态再 commit/push。

本次自动化单元 35 项通过，覆盖未提交源改动、脱敏合并/冲突、新增/删除、隐私阻断、工作区保护、源变化、独立历史、推送重试、安装恢复、Feature 保留、稳定等待与失败退避。日志：`.local/private-evidence`。

首次真实发布门禁通过：共享核心 68 项、runtime 单元/固定 Hermes 契约 254 项、隔离真实 PG 22 项，共 344 个不同用例；完整候选树密钥扫描零命中。临时 PG 中测试库清理至零，实例停止并移除。详细结果保存在 `.local/private-evidence`。

早期自动化测试曾因空 changelog 格式、预期原因顺序和已删文件处理失败，已修正并通过最终 35 项。首次初始化被公开测试的短假凭据字面量阻断，后仅将公开测试改成明确的 `fixture-*` 假值，原框架源码不变；没有绕过扫描。此前导出验证和本轮门禁不累计为额外独立用例。

受限 systemd 环境独立验证了 uv、缓存写入、既有 SSH 远端读取和临时 PG 启停/查询；正式服务手动启动及定时器 04:28:02 UTC 自动触发均无改动检查成功，退出码 0，状态 `UP_TO_DATE`，公开提交数量保持 1。环境验证结果：`.local/private-evidence`。首次完整发布在启用定时器前由同一安装脚本完成；受限服务未重复执行完整 344 项。

## 安装与查看

源码：`/srv/open-trade/tools/public_sync.py`、`/srv/open-trade/tools/tests/test_public_sync.py`；unit 模板位于 `/srv/open-trade/tools/systemd/`。实际运行安装快照 `.local/private-evidence`，并校验脚本和扫描器摘要；配置、基线、状态、日志均留在忽略目录 `.local/private-evidence`，不导出。

```bash
systemctl is-enabled open-trade-public-sync.timer
systemctl status open-trade-public-sync.timer --no-pager
journalctl -u open-trade-public-sync.service -n 20 --no-pager
/usr/bin/python3 .local/private-evidence --config .local/private-evidence --status
```

定时器和服务安装到 `/etc/systemd/system/`，定时器为 enabled/active，重启后继续。只启用本维护任务；未修改或重启交易服务。任务使用文件锁防止并行，限制 CPU 为一个核心额度、内存 1 GiB，私有临时目录、只读系统/家目录；仅公开工作区、自动化状态及依赖缓存允许写入。

公开仓 origin fetch 保留用户给定 HTTPS 地址，pushurl 指向同一 GitHub 仓库的 SSH 地址，复用已有认证。没有创建或更换凭据、修改私有仓/全局 Git 身份。

修改工具源码不会静默改变已安装任务；升级需重新验证并更新安装快照及摘要。修复失败原因后，定时器自动重试；不要通过手改公开工作树或放宽扫描来处理正常同步。

## 边界与回滚

扫描只能覆盖固定规则和已知标识，不能判断所有未知商业机密；白名单代码不应直接包含私人业务材料。自动导出只处理文件内容；公开专用文档、示例和新文件类型需要另外维护。未选择许可证，保留现状。

22 项 PG 为针对性集成，不代表全部 PG、外部 live、收益或线上交易验收。验证隔离清除业务凭据，不运行真实模型/通知/订单，不连接业务数据库、启用第二交易入口或提高风险。正式交易与预测事实未修改。

停用后续同步：

```bash
systemctl disable --now open-trade-public-sync.timer
```

此命令仅停止后续调度；如已有发布正在执行，先查看服务状态并等待完成，停用定时器本身不会取消在途任务。需要取消在途维护时可停止 `open-trade-public-sync.service`，安装恢复日志会保护已完成的本地公开提交；不要删除状态、基线或已提交历史。公开代码回滚采用反向提交，不重写私人或公开历史；若外部反向提交改变公开基线，自动任务会阻断，须审核后重新建立基线。

私有仓本次工具、模板和交付说明仍为未提交改动，保留原 README/文档及 Feature 既有工作；未自动 commit/push 私有仓。
