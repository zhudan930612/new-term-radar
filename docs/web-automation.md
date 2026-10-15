# 自动任务与审核交接

调度示例：每日 10:00，Asia/Taipei。质量优先配置启用 `research.enabled`、设置 `opportunities.budget_usd=null`，取消旧的 $0.20 金额上限；实时询价、账本、缓存及余额保护继续生效。自动任务需在使用环境中单独配置。日报包括零新增结论，但已汇报日期不重复付费或重复汇报。

所有命令在仓库根目录执行，Python 使用 `.venv-win\Scripts\python.exe`。下方 YYYY-MM-DD 替换真实台北日期，输出报告路径用本轮实际文件。

## 1. 主流程与退出码

运行 `python -m radar opportunities --config config.json`（或本机已配置的 `local-run.ps1 -Mode Daily` 包装脚本）后必须读取权威 result.json，不只看退出码。`awaiting_audit` 返回 2 是待本环境审核，不是要求重跑。`incomplete` 也可能有已采集来源，可审核并修复模型分析；来源故障、空样本与历史范围缺口必须保留。没有 workflow_version=2 或没有 dispositions 时停止并说明采集失败，不编造审核结果。

新增的 Reddit 求助、定向痛点检索、YouTube 教程评论和可选 Chrome 评论，在 `research_collection` 中记录查询、成功/失败、返回数和缺口。Chrome 缺少 GoAnyAPI Key 时标 unavailable，不解释为没有抱怨；不会调用旧 `asks`/`pains` 的模型摘要替代原文。主流程 API 调用会收费，不能为“检查是否修好”重复采集已有日期。

## 2. 全源语义复核（不调用额外 AIsa）

```powershell
.\.venv-win\Scripts\python.exe -m radar demand-audit --result state/web-opportunities/YYYY-MM-DD/result.json --audit-action claim
```

仅首次领取生成 `semantic-audit-plan.json`，包括每条来源的稳定 id、URL、全文和任务。已经 running/tasks_pending 时读取已有计划继续本地审核，不重新发起 API。complete 时不重复导入。

由当前 Codex 代理逐条读所有来源，包括新增 Reddit、YouTube、Chrome 评论，不能只检查模型留下的候选。推广/教程不是亲身问题，教程下的具体用户评论须按原文独立判断；具体单用户请求要保留；泛泛情绪为 uncertain；产品安装/系统修复与可独立的用户任务分开。无法识别真实账号时保留未知，不能把显示名或同一扩展页面上的多条评论凑成独立证据。对照现有账本，正确且未变化的记录保留原字段，避免仅因润色制造新任务。不要读取本机独立回放标签（如忽略的 `tests/fixtures/demand-review-gold.json`）来生成日常判断。

生成 `semantic-source-observations.json`：

```json
{"date":"YYYY-MM-DD","reviewer":"Codex","method":"逐条原文语义复读与遗漏检查",
 "rows":[{"id":"计划中的稳定source id","category":"need","reason":"原文依据",
 "tasks":[{"user":"...","job":"...","context":"...","pain":"...","output":"...",
 "constraint":"未知","workaround":"未知","frequency":"未知","cost":"未知",
 "anchor":"原文中连续8至180字符","search_terms":["natural English task phrase"]}]}]}
```

每个计划来源恰好一条；类别为 need/maintenance/promotion/other/uncertain。need 为1–2个原子任务，其余 tasks=[]。未知不能推断填充。

```powershell
.\.venv-win\Scripts\python.exe -m radar demand-audit --result state/web-opportunities/YYYY-MM-DD/result.json --audit-action sources --observations state/web-opportunities/YYYY-MM-DD/semantic-source-observations.json
```

再读取更新的计划，逐个核对本批最多18个待审任务的证据、独立性、Web限制。不得把不同任务/同作者/类似推广凑成两用户。生成 `semantic-task-observations.json`：

```json
{"date":"YYYY-MM-DD","reviewer":"Codex","method":"逐任务核对原文、独立性与网页约束",
 "reviews":[{"id":"计划task id","same_job":true,"firsthand":true,"independent":false,
 "web_fit":"unknown","bounded":false,"reason":"单用户保留，缺口说明","proposal":null}]}
```

web_fit为browser/server/native/unknown。只有满足两独立用户、同一窄任务与网页可行性时 proposal 才填写 title,user,job,pain,workaround,web_advantage,input,output,mvp,gap,constraints,validation,success_condition,stop_condition（字符串）及keywords（英文短语数组）；全部为待验证假设，不可写“已证明市场/低竞争”。

计划的 `merge_context` 另列最多18个已审核历史任务，供跨日同任务核对。若本批 tasks 与另一条 tasks/merge_context 确实是相同用户工作、相同输出、兼容约束，可在任务观察JSON顶层加入可选 `groups`：

```json
"groups": [{"ids": ["任务A的id", "任务B的id"], "same_job": true, "same_constraints": true, "reason": "两条原文为什么是同一具体任务"}]
```

每批最多6组、每组2–5个ID、ID不能重复参与。相同话题、产品名或泛泛的省时不是合并依据；不为凑两用户而合并。每组保留 `(first_seen, id)` 排序最早的任务ID；reviews对该合并后ID提供一条核对所有原文的评审，不再为被合并ID重复给review。其他本批任务仍逐条评审；merge_context中没有参加合并的历史任务不重审。合并不调用AIsa，原记录保留可恢复快照；复核发现任务不同会恢复并继续待审。若不确定就不分组，保留单用户线索。

```powershell
.\.venv-win\Scripts\python.exe -m radar demand-audit --result state/web-opportunities/YYYY-MM-DD/result.json --audit-action tasks --observations state/web-opportunities/YYYY-MM-DD/semantic-task-observations.json --out reports/实际日报.md
```

若还有待审批次，更新计划会列出，继续完成本地审核；不可把积压视为无机会。未变更且7天内已审的历史任务复用审核结果。完成后回读 status/errors/coverage/semantic_audit/zero_result_audit 和需求池。来源全覆盖不代表市场全覆盖。

## 3. 入选后的免费网页验证

无机会或主流程未完成就跳过。先领原文/搜索意图计划：

```powershell
.\.venv-win\Scripts\python.exe -m radar validate-opportunities --result state/web-opportunities/YYYY-MM-DD/result.json --trends-action claim
```

仅 claimed=true 时开启新的访问。只读每个方向两条原始证据上下文、计划内美国英文搜索URL，以及搜索发现的最多两种替代方案（最多5页/方向，3方向共15页，约8分钟）。不购买服务、不联系作者、不绕过登录/验证码。保存实际可见原文与URL到当天 validation-evidence/*.txt；每个观察须有逐字 quote，不能把搜索摘要当完整原文。核对原文是否同一任务、账号是否独立及是否推广、搜索词是否匹配用户任务；价格/限制查不到写未知。若浏览器不可用或受阻，记录失败且不猜测结果。

```json
{"date":"YYYY-MM-DD","items":[{"id":"v1","status":"observed","observed_at":"当天带时区时间",
 "note":"具体观察及未核实项","same_task":true,"independent_requests":true,"intent_match":true,
 "sources":[{"url":"计划内原文URL","evidence_file":"validation-evidence/source1.txt","quote":"至少8字符原文"},
            {"url":"计划内第二原文URL","evidence_file":"validation-evidence/source2.txt","quote":"至少8字符原文"}],
 "search":{"url":"计划内完整搜索URL","evidence_file":"validation-evidence/search.txt","quote":"至少8字符原文"},
 "alternatives":[{"url":"实际替代产品URL","evidence_file":"validation-evidence/alternative.txt","quote":"至少8字符原文","price":"查到的价格或未知","limitation":"有来源的限制或未知"}]}]}
```

每个计划项目必须有一条；status可用observed/blocked/unavailable。失败项写真实原因，不填成功证据；空白页不是无需求。只检查到一条原文或搜索受阻时用unavailable并在note说明局部证据。未读到替代产品时alternatives=[]并写缺口；没有替代产品观察不代表无竞争。

```powershell
.\.venv-win\Scripts\python.exe -m radar validate-opportunities --result state/web-opportunities/YYYY-MM-DD/result.json --trends-action import --observations state/web-opportunities/YYYY-MM-DD/validation-observations.json --out reports/实际日报.md
```

原文同一任务/独立性被推翻会撤回推荐，并回写需求池待补证；搜索意图不匹配仅待改词。running表示已领取，不自动重新访问，导入已有证据，剩余项记unavailable。所有检查只降低不确定性，`market_verified`保持false。

## 4. 竞争与营收研究，再查免费趋势

完成上述 `validate-opportunities` 导入后，执行独立研究阶段；它只研究 workflow_version=2、主流程和 semantic_audit 均 complete、对应验证项 trends_ready=true 且未撤回的最多 3 个机会。缺条件时不调用账户、竞争或营收接口。

```powershell
.\.venv-win\Scripts\python.exe -m radar research-opportunities --config config.json --state state --result state/web-opportunities/YYYY-MM-DD/result.json --out reports/实际日报.md
```

本机已配置加密凭据包装脚本时，也可用：

```powershell
.\local-run.ps1 -Mode Research -Result state/web-opportunities/YYYY-MM-DD/result.json
```

回读 `market_research`：每项保存竞争信息、缺失字段、SERP 原始结果及 `market-research-evidence/` 证据路径。缺 GoAnyAPI Key 时 DR/广告主/标题竞争保持缺失；KD 或结果缺失不能解释成低竞争。TrustMRR 每轮最多读取一次，缺 Key 标 unavailable；产品营收仅为背景，精确域名重合也不证明同一用户任务或本机会可盈利。GoAnyAPI/TrustMRR 费用与 AIsa 账本分开说明。

该阶段先保存 running，再发起请求；complete/partial/incomplete/running 均不自动重新执行付费查询。partial 通常返回 0，incomplete/running 返回 2，必须读 JSON 的具体缺口，不能因此重跑日报或删除账本。原日报的完成状态与本阶段状态独立；研究数据不修改需求晋级或“无新增”的原结论。AIsa 与当天 `api/requests.json` 共用账本，失败记录仍阻止后续重复付费；Web 流程内 GoAnyAPI 不自动重试。

然后继续已有 [Google Trends流程](google-trends.md)，只查原文和意图已确认的短语，市场美国、免费网页、失败不改用付费 API。竞争/营收缺 Key 不要求补齐后才能读免费趋势；各阶段的未知分别保留。

## 5. 最终汇报

依据最新权威 JSON 和报告，说明新增机会、待补证任务、来源与审核缺口、原文/搜索/竞争营收/Trends 各阶段状态。区分本轮 AIsa 请求账本费用、账户余额变化和外部服务费用；并发账户余额变化不能全部归因本轮。不要在聊天或提交中暴露密钥，日报、账本、余额及真实来源样本留在忽略的本地目录。

有历史方向但无新增证据时不当成新发现。仅所有必要审核完成且没有新增合格方向时说“本轮样本中没有新增达到深入验证门槛的 Web 网站机会”。本流程已配置的需求发现与研究可按授权运行；不得自动充值、开发/部署、买域名、扩展到额外付费研究或联系第三方。离线模拟验证、真实来源回放与真实市场验证分别报告，不能把通过测试写成已经验证供应商数据或商业机会。
