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
| 任务来源、定向补证 | `radar/demand_sources.py` |
| 分类、原子任务、合并恢复、跨日复查 | `radar/demands.py` |
| Codex 全源语义审核与导入 | `radar/demand_audit.py` |
| 原文上下文、搜索意图、推荐撤回 | `radar/opportunity_validation.py` |
| 免费趋势计划与导入 | `radar/google_trends.py` |
| 候选背景采集、旧版证据分析、日报渲染 | `radar/opportunities.py` |
| AIsa 请求、费用预留与缓存 | `radar/aisa.py`、`radar/spending.py` |

## 必须保持的业务约束

- 具体单用户问题先保留为需求种子，不要求新产品名、点赞、星数或第二个用户；晋级为机会才检查独立用户、同一窄任务及 Web 可行性。
- 推广、教程和官方功能介绍不能当亲身痛点。同一作者、同 URL、重复或相似传播不能凑独立证据；共享话题不等于同一任务。错误合并须可恢复，语义修订须使旧复核失效。
- 引用必须能定位原文。频率、成本、目标国家、上传意愿和付费意愿没有来源就保留未知；不为适配网页而改写原任务约束。
- Qwen 仅作预处理。正式发布前必须经过 Codex 全源语义复核，包含负例和模糊项；不能只审核入选项，也不能跳过来源审核直接导入任务结论。
- 空样本、来源失败、分析失败和未处理积压不等于没有需求。零新增结论只针对已完成的有限采样范围，报告必须展示覆盖与缺口。
- 保留跨日种子及历史失败来源；未变化的任务复用有效审核，不每天全量重审。上榜记录只能在正式审核通过后写入，新证据才形成新的推荐版本。
- 入选后先查原文上下文与搜索意图，再查免费 Google Trends。原文推翻证据要撤回推荐；搜索词不匹配应待改词，低热度不等于无需求。趋势指数不是搜索次数，入选不代表已验证市场或盈利。

## 运行状态、费用与本机数据

- 权威日报为 `state/web-opportunities/YYYY-MM-DD/result.json` 及同目录 `result.md`；跨日需求池为 `state/demand-ledger.json`。`reports/` 是报告输出，`state-free/` 与正式状态分开。
- 日报退出码 2 可能表示 `awaiting_audit`。先读 JSON，再按交接文档完成本地审核；不要因非零退出码重新发起采集。
- 同日结果和请求账本用于防止重复计费。失败、超时或中断先检查结果、锁、缓存和计费记录；不得删除它们绕过保护。状态升级应保留旧副本及费用归属。
- 付费请求沿用 `spending.Journal` 的询价、预留、缓存及失败保护。不得绕开预算、擅自提高费用上限、换付费模型或把免费网页失败转成付费接口。`sources`、`keywords` 和真实模型回放也可能收费，不是离线测试命令。
- 日报授权不包含自动充值、开发发布网站、购买域名或联系第三方；这些动作按用户另行授权执行。
- 本机凭据通过现有加密入口或进程环境加载，不打印密钥、认证头或凭据助手的原始输出。`config.json`、`.local/`、`state/`、`reports/` 和本机启动脚本可能受 `.gitignore` 或 `.git/info/exclude` 排除；不能因未跟踪就删除、强制提交或判断为未配置。

## 验证

从仓库根目录使用本机虚拟环境：

```powershell
.\.venv-win\Scripts\python.exe -m radar --help
.\.venv-win\Scripts\python.exe -m pytest -q
git diff --check
```

按修改范围先运行相关测试：需求与审核见 `tests/test_demands_review.py`、`tests/test_demand_audit_flow.py`；编排与计费见 `tests/test_opportunities.py`；网页核验见 `tests/test_opportunity_validation.py`、`tests/test_google_trends.py`。改动共用状态或门禁后运行完整测试。

本机 `tests/fixtures/demand-review-gold.json` 是独立审查子代理标注的真实样本回放基准，属于忽略的本地数据，不随仓库分发，也不是市场真值。不能据此生成日常审核答案或为迎合模型修改标签。`scripts/replay_demands.py` 会调用真实模型并计费；授权回放时使用隔离状态与保留的请求账本，不能覆盖正式账本。报告验证结论时区分离线合成测试、真实来源回放与真实市场验证。
