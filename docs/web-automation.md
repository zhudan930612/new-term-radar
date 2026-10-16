# 自动任务与审核交接

调度示例：每日 09:00，Asia/Taipei。质量优先配置启用 `research.enabled`、设置 `opportunities.budget_usd=null`，取消旧的 $0.20 金额上限；实时询价、账本、缓存及余额保护继续生效。自动任务需在使用环境中单独配置，调用时传 `--run-origin scheduled`（本机包装脚本 `-RunOrigin scheduled`）。日报包括零新增结论，但已汇报日期不重复付费或重复汇报。

所有命令在仓库根目录执行，Python 使用 `.venv-win\Scripts\python.exe`。下方 YYYY-MM-DD 替换真实台北日期，输出报告路径用本轮实际文件。

## 1. 主流程与退出码

运行 `python -m radar opportunities --config config.json`（或本机已配置的 `local-run.ps1 -Mode Daily` 包装脚本）后必须读取权威 result.json，不只看退出码。`awaiting_audit` 返回 2 是待本环境审核，不是要求重跑。`incomplete` 也可能有已采集来源，可审核并修复模型分析；来源故障、空样本与历史范围缺口必须保留。没有 workflow_version=2 或没有 dispositions 时停止并说明采集失败，不编造审核结果。

新增的 Reddit 求助、定向痛点检索、YouTube 教程评论和可选 Chrome 评论，在 `research_collection` 中记录查询、成功/失败、返回数和缺口。Chrome 缺少 GoAnyAPI Key 时标 unavailable，不解释为没有抱怨；不会调用旧 `asks`/`pains` 的模型摘要替代原文。主流程 API 调用会收费，不能为“检查是否修好”重复采集已有日期。

先用 `python -m radar workflow-status --state state --date YYYY-MM-DD` 读取结果及运行收据。有新格式 `collection-plan.json` 且采集未完成时，查清账本、锁和失败性质后才可显式 `python -m radar opportunities --config config.json --state state --resume`，或本机 `local-run.ps1 -Mode Resume -RunOrigin scheduled`。配置、策略版本、提供商可用性须匹配冻结计划；未开启Web/需求工作流或携带skip时拒绝恢复，不落回旧付费daily。无计划的旧失败结果不能用此命令猜测恢复。

恢复保留原结果快照与原账本，只补原计划；AIsa成功复用，GoAny仅复用已有成功缓存，未知费用不绕过。若只剩模型分析问题，不再付费调用模型，直接做下述本地语义审核。采集后的原文和待审范围先于模型保存，模型异常也可继续审核全部来源。

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

本轮审核还应为优先研究、暂停或已解决的任务填写可选 `research` 字段，让建议进入真实队列，而非仅写在报告正文。示例（ID须来自本任务证据）：

```json
"research": {"status":"active","priority":3,"evidence_source_ids":["s-真实来源ID"],
 "gap":"还不知道现有方案是否已经满足","next_action":"读完整原文并检查一种现有方案",
 "reason":"原文描述了具体重复步骤和现有绕法","next_review":null,
 "query_variants":["short task phrase","alternate task wording"]}
```

priority取0–3，是研究次序；大于0必须引用本任务来源。已解决使用resolved且next_review=null；暂停使用paused并填未来90天内复查日期。不得从文案关键词自动推断已解决。未正式审核或证据版本变化时优先级失效；到期/变化先本地复核，不自动恢复付费查询。query_variants最多两条自然短语，不含搜索操作符。

计划的 `merge_context` 另列最多18个已审核历史任务，供跨日同任务核对。若本批 tasks 与另一条 tasks/merge_context 确实是相同用户工作、相同输出、兼容约束，可在任务观察JSON顶层加入可选 `groups`：

```json
"groups": [{"ids": ["任务A的id", "任务B的id"], "same_job": true, "same_constraints": true, "reason": "两条原文为什么是同一具体任务"}]
```

每批最多6组、每组2–5个ID、ID不能重复参与。相同话题、产品名或泛泛的省时不是合并依据；不为凑两用户而合并。每组保留 `(first_seen, id)` 排序最早的任务ID；reviews对该合并后ID提供一条核对所有原文的评审，不再为被合并ID重复给review。其他本批任务仍逐条评审；merge_context中没有参加合并的历史任务不重审。合并不调用AIsa，原记录保留可恢复快照；复核发现任务不同会恢复并继续待审。若不确定就不分组，保留单用户线索。

merge_context从全活跃池按任务相关性选取，模型输出的merge_suggestions仅供参考。发现计划外的确切历史匹配时，groups可引用该活跃任务ID，但须先读取其全部原文，reviews覆盖原批次仍存在的任务及合并后目标；不会因合并空位自动塞入下一批。共享页面不同评论均展示，独立性另行判断。

```powershell
.\.venv-win\Scripts\python.exe -m radar demand-audit --result state/web-opportunities/YYYY-MM-DD/result.json --audit-action tasks --observations state/web-opportunities/YYYY-MM-DD/semantic-task-observations.json --out reports/实际日报.md
```

若还有待审批次，更新计划会列出，继续完成本地审核；不可把积压视为无机会。未变更且7天内已审的历史任务复用审核结果。完成后回读 status/errors/coverage/semantic_audit/zero_result_audit 和需求池。来源全覆盖不代表市场全覆盖。

只有整轮complete且无错误才发布机会/写已展示版本；未完成的建议放provisional，不消耗次日上榜资格。validation_continuation/is_new=false表示旧机会继续验证，报告不得计作新机会。正式结束生成research-plan.json，与研究队列使用同一规则。

已完成后发现错误，执行 `python -m radar demand-audit --result ... --audit-action amend` 后重新导入完整来源观察及待审任务。amend可用 `--observations` 指定 `{ "task_ids":["d-需更正ID"], "reason":"有依据的修订原因" }`；省略则重新本地核对所有活跃任务。每次修订及每批原始观察均进入audit-history与索引，不覆盖前批；任何写入中断保留锁供诊断，不能删除锁重跑。旧指纹首次迁移需一次本地审核，不重买模型分析。

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

验证受阻可用 `python -m radar validate-opportunities --result ... --trends-action resume` 领取未完成检查；改词须明确 `--item v1 --keyword "新的任务短语"`。读取返回的active_ids/missing_checks，只访问缺失部分，原成功观察自动保留，不重复提交或访问已成功来源。新观察的observed_at采用实际读取日期，允许跨日恢复；旧证据仍保留原时刻。旧计划绑定证据版本，普通prepare不能重绑；版本变化须通过正式审核归档并重建。已核验的一条核心反证也会撤回，不等另一页成功；没有保存原文的否定结论不能冒充反证。

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

该阶段先保存 running，再发起请求；complete/partial/incomplete/running 均不自动重启整阶段。partial 通常返回 0，incomplete/running 返回 2，必须读 JSON 的具体缺口，不能因此重跑日报或删除账本。原日报的完成状态与本阶段状态独立；研究数据不修改需求晋级或“无新增”的原结论。AIsa 与当天 `api/requests.json` 共用账本：单来源的已知失败不阻断其他请求，只有完整回包明确未扣费且可重试的允许列表内只读数据请求才可有限重试（含历史尝试共 3 次）；费用未知或账户问题仍全局保护。逐次请求/回包及计费留档，已成功请求复用缓存，Web 流程内 GoAnyAPI 不自动重试。

然后继续已有 [Google Trends流程](google-trends.md)，只查原文和意图已确认的短语，市场美国、免费网页、失败不改用付费 API。竞争/营收缺 Key 不要求补齐后才能读免费趋势；各阶段的未知分别保留。

语义amend或采集恢复后，已运行的市场阶段仍保留终态门禁和原数据/费用，并标requires_local_review；不能删除阶段或把旧结果直接挂到新关键词来触发重买或宣称新验证。

## 5. 种子前置研究和验证记录

强单用户任务仍可研究原文、替代方案或任务样例，不改变两独立证据的晋级要求。领取本地计划：

```powershell
.\.venv-win\Scripts\python.exe -m radar task-research --research-action plan --state state --date YYYY-MM-DD --limit 3 --plan state/research/YYYY-MM-DD/plan.json
```

需要明确研究某方向时可重复传 `--task-id d-真实ID`，最多25项。计划保存plan_id、证据和研究版本；默认优先处理本地复核项，再按已审核优先级及探索名额安排。plan不修改账本，也不访问网络。

将实际读取的材料保存到计划旁的validation-evidence/，随后导入：

```powershell
.\.venv-win\Scripts\python.exe -m radar task-research --research-action import --state state --date YYYY-MM-DD --plan state/research/YYYY-MM-DD/plan.json --observations state/research/YYYY-MM-DD/observations.json
```

观察格式如下；每个计划任务恰好一项，记录必须来自真实材料。source_context URL须归属本任务；alternative/task_sample/experiment/channel/payment分别记录已观察到的事实，未知不补造。

```json
{"plan_id":"计划中的哈希","date":"YYYY-MM-DD","reviewer":"Codex",
 "items":[{"task_id":"d-计划任务ID","decision":"continue",
   "research":{"status":"active","priority":3,"gap":"待验证的问题","next_action":"具体下一步",
     "reason":"有依据的研究次序","next_review":null,"evidence_source_ids":["s-本任务来源ID"]},
   "records":[{"kind":"source_context","url":"计划原文URL","evidence_file":"validation-evidence/source.txt",
     "quote":"文件中的逐字引文","observed_at":"YYYY-MM-DDTHH:MM:SS+08:00","note":"实际观察及限制"}]}]}
```

decision为continue/pause/stop，分别对应active/paused/resolved。导入校验计划哈希、当前正式审核、证据版本、来源归属、逐字引用及文件哈希，重复导入不重复追加。每任务1–8条记录，历史保留；报告与下一轮调度消费这些决定。联系第三方、购买、开发发布仍需用户另行授权，记录实验计划不能冒充已做实验。

授权的本地样例或离线实验没有公开URL时，只有task_sample/experiment可显式使用 `"origin":"local"` 并省略URL；仍须提供validation-evidence内的文件、逐字引文、实际时间与事实说明。路径越界、空引文或伪造网页来源会拒绝；本地记录不增加独立用户数。网页原文、替代产品、渠道与付费信息仍须可追溯网页证据。

已入选且原文/意图通过的方向会以qualified_research_due进入本地研究计划，继续检查替代方案、任务样例和实验条件，不重新计作新增，也不自动调用付费接口。明确的next_review、暂停或停止决定控制后续节奏。实验结果必须记录实际观察，不把计划或推断写成已验证需求。

## 6. 最终汇报

依据最新权威 JSON 和报告，说明新增机会、待补证任务、来源与审核缺口、原文/搜索/竞争营收/Trends 各阶段状态。区分本轮 AIsa 请求账本费用、账户余额变化和外部服务费用；并发账户余额变化不能全部归因本轮。不要在聊天或提交中暴露密钥，日报、账本、余额及真实来源样本留在忽略的本地目录。

有历史方向但无新增证据时不当成新发现。仅所有必要审核完成且没有新增合格方向时说“本轮样本中没有新增达到深入验证门槛的 Web 网站机会”。本流程已配置的需求发现与研究可按授权运行；不得自动充值、开发/部署、买域名、扩展到额外付费研究或联系第三方。离线模拟验证、真实来源回放与真实市场验证分别报告，不能把通过测试写成已经验证供应商数据或商业机会。
