# new-term-radar

## 项目目标与入口

本机目标是持续发现真实用户需求，筛选适合做 Web 网站、值得进一步验证的机会；允许零新增。旧版新产品名、热度和关键词工具仍保留，不能代替需求判断。

源码目录是 `radar/`，仓库内使用 `python -m radar`；安装后的包名是 `new_term_radar`，映射见 `pyproject.toml`。实际运行配置为本地 `config.json`，默认值在 `radar/config.py`；不要用 `config.example.json` 覆盖已有配置。

按任务读取文档：

- 修改需求入口、筛选规则、跨日账本或报告口径：读 [Web 机会工作流](docs/web-opportunities.md)。
- 运行日报、处理 `awaiting_audit`、导入语义复核或调整自动任务：读 [自动任务与审核交接](docs/web-automation.md)，使用其中的 CLI 和观察文件格式。
- 修改或执行免费趋势核验：读 [Google Trends 验证](docs/google-trends.md)。
- 排查本机环境、加密凭据或启动脚本：读 `LOCAL-SETUP.md`（本机文件，不随仓库分发）。旧版功能查 `README.zh-CN.md`，注意其中原版流程与本地新增流程的区别。

## 代码导航

| 要改的行为 | 入口 |
|---|---|
| CLI 分流、配置 | `radar/__main__.py`、`radar/config.py` |
| 日报编排、同日复用、状态保存 | `radar/opportunity_runner.py` |
| 任务来源、定向补证 | `radar/demand_sources.py`；新增 Reddit/教程与扩展评论原文见 `radar/demand_research.py` |
| 分类、原子任务、合并恢复、跨日复查 | `radar/demands.py` |
| Codex 全源语义审核与导入 | `radar/demand_audit.py` |
| 原文上下文、搜索意图、推荐撤回 | `radar/opportunity_validation.py` |
| 验证后的搜索竞争、营收背景与证据 | `radar/market_research.py` |
| 免费趋势计划与导入 | `radar/google_trends.py` |
| 候选背景采集、旧版证据分析、日报渲染 | `radar/opportunities.py` |
| AIsa 请求、费用预留与缓存 | `radar/aisa.py`、`radar/spending.py` |
| 上游能力与独立研究入口 | `radar/asks.py`、`radar/pains.py`、`radar/competition.py`、`radar/revenue.py`；Web 流程通过上述两个研究模块分阶段接入，`radar/opportunity.py` 仍仅为原版格式/教程标签 |

## 必须保持的业务约束

- 具体单用户问题先保留为需求种子，不要求新产品名、点赞、星数或第二个用户；晋级为机会才检查独立用户、同一窄任务及 Web 可行性。
- 推广、教程和官方功能介绍不能当亲身痛点。同一作者、同 URL、重复或相似传播不能凑独立证据；共享话题不等于同一任务。错误合并须可恢复，语义修订须使旧复核失效。
- 引用必须能定位原文。频率、成本、目标国家、上传意愿和付费意愿没有来源就保留未知；不为适配网页而改写原任务约束。
- Qwen 仅作预处理。正式发布前必须经过 Codex 全源语义复核，包含新增 Reddit、教程评论、扩展评论及所有负例和模糊项；不能只审核入选项，也不能跳过来源审核直接导入任务结论。新增入口保留原文，不沿用旧模型摘要或点赞/搜索量过滤；账号未知或共用页面不能凑独立证据。
- 空样本、来源失败、分析失败和未处理积压不等于没有需求。零新增结论只针对已完成的有限采样范围，报告必须展示覆盖与缺口。
- 保留跨日种子及历史失败来源；未变化的任务复用有效审核，不每天全量重审。上榜记录只能在正式审核通过后写入，新证据才形成新的推荐版本。
- 入选后先查原文上下文与搜索意图，再运行 `research-opportunities`、免费 Google Trends。竞争/营收仅对 workflow_version=2、语义审核完成且原文/意图通过的未撤回机会运行；原始 SERP/KD 证据必须保留。缺指标不等于低竞争，相关产品营收仅作背景，不证明同一任务或盈利；`market_verified` 保持 false。原文推翻证据要撤回推荐；搜索词不匹配应待改词，低热度不等于无需求，趋势指数不是搜索次数。

## 运行状态、费用与本机数据

- 权威日报为 `state/web-opportunities/YYYY-MM-DD/result.json` 及同目录 `result.md`；跨日需求池为 `state/demand-ledger.json`。`reports/` 是报告输出，`state-free/` 与正式状态分开。
- 日报退出码 2 可能表示 `awaiting_audit`。先读 JSON，再按交接文档完成本地审核；不要因非零退出码重新发起采集。
- 同日结果和请求账本用于防止重复计费。失败、超时或中断先检查结果、锁、缓存和计费记录；不得删除它们绕过保护。状态升级应保留旧副本及费用归属。
- AIsa 单来源的已知失败只隔离该请求。仅限允许列表中的只读数据请求，且完整回包逐项明确未扣费、可重试的上游临时错误，最多尝试 3 次（包含历史尝试），每次重新询价并保存独立尝试账本；成功缓存继续复用。超时、费用未知、回包无法归属及账户/鉴权错误仍停止后续 AIsa 付费请求，不能为补齐日报重置重试次数。LLM 和 GoAnyAPI 不自动重试。
- 质量优先配置允许 `opportunities.budget_usd=null` 取消旧金额上限；仍使用 `spending.Journal` 的询价、预留、缓存、余额及失败保护。配置为正数时尊重金额上限，不擅自换付费模型或把免费 Trends 失败转成付费接口。GoAnyAPI/TrustMRR 不包含在 AIsa 账单内，费用与缺口单列；Web 流程内 GoAnyAPI 不自动重试。
- `research.enabled` 控制新增原文入口，代码缺省 false 保持兼容，启用示例见工作流文档。上游独立 `pains`、`asks`、`tutorials`、`revenue`、`competition` 能力保留；独立命令不受 Web 日报请求账本保护。`sources`、`keywords` 和真实模型回放也可能收费。完整免费跳过集合必须覆盖新增付费来源，且优先于强制运行标志。
- 日报授权不包含自动充值、开发发布网站、购买域名或联系第三方；这些动作按用户另行授权执行。
- 本机凭据通过现有加密入口或进程环境加载，不打印密钥、认证头或凭据助手的原始输出。`config.json`、`.local/`、`state/`、`reports/` 和本机启动脚本可能受 `.gitignore` 或 `.git/info/exclude` 排除；不能因未跟踪就删除、强制提交或判断为未配置。

## 验证

从仓库根目录使用本机虚拟环境：

```powershell
.\.venv-win\Scripts\python.exe -m radar --help
.\.venv-win\Scripts\python.exe -m pytest -q
git diff --check
```

按修改范围先运行相关测试：需求与审核见 `tests/test_demands_review.py`、`tests/test_demand_audit_flow.py`；编排与计费见 `tests/test_opportunities.py`；新增来源与后续研究见 `tests/test_demand_research.py`、`tests/test_market_research.py`；网页核验见 `tests/test_opportunity_validation.py`、`tests/test_google_trends.py`。改动共用状态或门禁后运行完整测试。

本机 `tests/fixtures/demand-review-gold.json` 是独立审查子代理标注的真实样本回放基准，属于忽略的本地数据，不随仓库分发，也不是市场真值。不能据此生成日常审核答案或为迎合模型修改标签。`scripts/replay_demands.py` 会调用真实模型并计费；授权回放时使用隔离状态与保留的请求账本，不能覆盖正式账本。报告验证结论时区分离线合成测试、真实来源回放与真实市场验证。
