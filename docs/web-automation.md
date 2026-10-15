# 自动任务与审核交接

调度示例：每日 10:00，Asia/Taipei；每轮 AIsa 预计费用上限 $0.20。自动任务需在使用环境中单独配置。日报包括零新增结论，但已汇报日期不重复付费或重复汇报。

所有命令在仓库根目录执行，Python 使用 `.venv-win\Scripts\python.exe`。下方 YYYY-MM-DD 替换真实台北日期，输出报告路径用本轮实际文件。

## 1. 主流程与退出码

运行 `python -m radar opportunities --config config.json`（或本机已配置的 `local-run.ps1 -Mode Daily` 包装脚本）后必须读取权威 result.json，不只看退出码。`awaiting_audit` 返回 2 是待本环境审核，不是要求重跑。`incomplete` 也可能有已采集来源，可审核并修复模型分析；来源故障、空样本与历史范围缺口必须保留。没有 workflow_version=2 或没有 dispositions 时停止并说明采集失败，不编造审核结果。

## 2. 全源语义复核（不调用额外 AIsa）

```powershell
.\.venv-win\Scripts\python.exe -m radar demand-audit --result state/web-opportunities/YYYY-MM-DD/result.json --audit-action claim
```

仅首次领取生成 `semantic-audit-plan.json`，包括每条来源的稳定 id、URL、全文和任务。已经 running/tasks_pending 时读取已有计划继续本地审核，不重新发起 API。complete 时不重复导入。

由当前 Codex 代理逐条读所有来源，不能只检查模型留下的候选。推广/教程不是亲身问题；具体单用户请求要保留；泛泛情绪为 uncertain；产品安装/系统修复与可独立的用户任务分开。对照现有账本，正确且未变化的记录保留原字段，避免仅因润色制造新任务。不要读取本机独立回放标签（如忽略的 `tests/fixtures/demand-review-gold.json`）来生成日常判断。

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

原文同一任务/独立性被推翻会撤回推荐，并回写需求池待补证；搜索意图不匹配仅待改词。其余机会生成Trends清单，再执行 [Google Trends流程](google-trends.md)。running表示已领取，不自动重新访问，导入已有证据，剩余项记unavailable。所有检查只降低不确定性，`market_verified`保持false。

## 4. 最终汇报

依据最新权威JSON和报告，说明新增机会、待补证任务、未完成范围、原文/搜索/Trends各阶段状态、AIsa账户扣费及余额。有历史方向但无新增证据时不当成新发现。仅所有必要审核完成且没有新增合格方向时说“本轮样本中没有新增达到深入验证门槛的Web网站机会”。不得自动开发/部署、买域名、付费深挖或联系第三方。
