> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 新 v1 收入 hermes-trade

2026-10-08：所有新 v1 源码和兼容改动归入 `hermes-trade` 的 `dev` 分支工作区。
一次提交和服务器拉取即可同步代码；启用服务仍需要环境配置和独立验证。

## 目录与入口

| 路径 | 用途 |
|---|---|
| `src/trading_system/` | 原交易框架，入口 `trade-agent`；入场、风控和执行逻辑保留 |
| `trading-agent/` | 新 v1 完整实现，入口 `ta`，包含迁移、插件、提示词与测试；没有独立 `.git` |
| `packages/trading-quant/` | 当前 v1 指标的纯计算实现，根项目与 v1 均使用仓库内路径安装 |
| `trading-agent/docs/specifications/` | 两份 v1 设计与技术规格，避免依赖仓库外文档 |

保留两个 Python 虚拟环境和锁文件：旧框架不需要安装全部 v1 依赖，新 v1 保留
Python 3.11、PostgreSQL 16 和可选 Cube 依赖。仓库外原 `trading-agent` 目录保留为参考，
本仓库运行不依赖它。正式 v1 预测、评分、调度与权限流程没有因目录迁移改变。

## 本地安装与验证

在仓库根目录安装旧框架及共享计算包：

```sh
uv sync --frozen --python 3.11 --extra dev
uv run --frozen --extra dev pytest -q
```

在同仓库的 v1 目录安装、检查和测试：

```sh
cd trading-agent
uv sync --frozen --extra cube --group hermes-test
uv run --frozen --extra cube --group hermes-test ta config check --json
uv run --frozen --extra cube --group hermes-test pytest -q
```

根目录 pytest 只收集根目录 `tests/`；v1 测试需在 `trading-agent/` 执行，防止两个
Python 环境的测试相互混入。v1 集成测试需要真实隔离 PostgreSQL，详情见
[v1 README](../../../trading-agent/README.md)。固定版本 Hermes 仍是外部运行依赖，
通过 v1 部署锁指定路径和提交，不是仓库外的私有交易源码依赖。

示例配置尚未绑定真实环境，`ta config check` 的 `configuration_complete=false` 表示部署资料未齐；
配置能解析不代表已经具备上线条件。已知原有用量统计测试的时间依赖失败见
[兼容验证边界](../../legacy-research/docs/legacy-compatibility.md)。

## 服务器同步与启用边界

以下命令供用户在已提交并 push 后执行；本次没有代为 commit、push 或操作服务器。
假设服务器仓库根目录为 `/srv/open-trade-deployment`，切换分支前需处理服务器本地代码改动：

```sh
cd /srv/open-trade-deployment
git fetch origin
git switch dev
git pull --ff-only origin dev
uv sync --frozen --python 3.11
cd trading-agent
uv sync --frozen --extra cube --no-dev
```

这会安装根项目与 v1 各自的共享指标依赖，不需要单独同步另一个交易项目。
若继续使用 pip，须先在对应虚拟环境安装 `packages/trading-quant`，再安装使用它的项目；
计算包未发布到公共包仓库。

启用前仍需完成真实 PostgreSQL、Hermes、模型、Cube 模板/隔离证明和角色凭据配置，
然后按 [v1 部署说明](../../../trading-agent/docs/deployment.md) 执行人工迁移与验收。
v1 自带 systemd 模板原路径为 `/opt/trading-agent`；使用本仓库布局时应将其
`WorkingDirectory` 改为 `/srv/open-trade-deployment/trading-agent`，`ExecStart` 使用该目录的 `.venv/bin/`。
本次没有替换或重启旧交易服务，也没有修改服务器 systemd 文件。

策略迭代兼容配置的 `repository` 指向 `/srv/open-trade-deployment`；可选插件的执行入口指向
`/srv/open-trade-deployment/trading-agent/.venv/bin/ta`。`enabled` 和 `apply_changes` 默认均为 false，
专门 Profile、旧 cron 切换和真实 Cube 测试尚未执行，详见
[兼容启用步骤](../../legacy-research/docs/legacy-compatibility.md)。

## 改了什么、没改什么

2026-10-10追加：策略迭代经验支持summary/content分层和按ID展开，
详见 [策略经验召回](../../legacy-research/docs/strategy-memory.md)。未启用服务器Profile或服务。

- 已收进仓库：新 v1 完整源码、八份数据库迁移、正式及可选兼容插件、提示词、
  测试、规格和部署模板；将共享量化依赖改为仓库内路径。
- 必要安装配套：更新锁文件、安装说明和测试收集目录。没有增加新的业务功能。
- 兼容成交盈亏、候选策略修改、固定沙箱测试与改动记录的范围保持上一轮实现，
  细节见 [兼容说明](../../legacy-research/docs/legacy-compatibility.md)。
- 未做：服务器部署、切换 cron/Profile、启用自动写回、真实模型/Cube 联调、
  下单联动、合约账本、收益实验或修复原有统计测试。

本轮同仓库验证结果见
[验证记录](../../legacy-research/docs/monorepo-verification.json)。
