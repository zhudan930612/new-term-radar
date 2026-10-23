# 日报质量与研究后的汇总

## 费用口径与兼容

模型请求保存 `cost_basis=token_estimate`、`pricing_snapshot`、`estimated_usd`，`settled_usd=null`；数据接口请求标记 `provider_response`。旧 `actual_usd` 数值保留供原有预算、缓存与恢复逻辑使用，不代表模型结算回执。旧账本不重算、不清空失败标记，也不为补元数据访问供应商。

`request_billing` 分列数据接口回包费用、模型用量估算、历史未归属金额、未知费用预留及未知次数。账户余额变化独立展示。未知、超时及账户错误仍停止付费请求；重试允许列表和最多三次门禁不变。

## 研究后汇总

`task-research import` 成功后自动生成当天 `summary.md` / `summary.json`。若派生报告失败，导入仍已保存，输出 `summary_error`，只重建汇总，不重新研究。也可离线执行：

```powershell
.\.venv-win\Scripts\python.exe -m radar daily-summary --state state --date YYYY-MM-DD
```

`result.json` / `result.md` 继续作为采集、审核及上榜快照；新汇总包含更新时间、输入文件哈希、最新研究决定及费用口径。旧领取计划保留为 `research_plan_snapshot`，不作为再次执行依据。身份或证据已变化的决定标明需本地复核；不改变机会、审核状态、付费阶段或已展示版本。默认查看 `summary.md` 和原始 `result.json`。

## 补证查询与暂停条件

研究决定允许可选 `query_context`，只填写原文支持的自然短语：

```json
{"query_context":{"role":"bookkeeper","step":"reconcile invoice line items","alternative":"Excel"}}
```

仍受原有有效审核、来源引用和最多两条查询限制；上下文只调整检索，不过滤返回原文。查询记录带归属信息；正式审核后分别统计需求原文、推广和确认同任务原文。历史缺归属显示未知，未完成轮次为 `not_evaluated`，不能算补证失败或进入成功率分母。连续两轮完成审核但无确认补证、且词变体和上下文未变时，任务进入 `query_revision_required` 本地队列，停止相同计划的常规付费补证。改变词/上下文必须经过受控研究或语义审核导入。

暂停决定可加入 `waiting_on` 和 `resume_condition`，两者同时填写；旧记录允许缺省但计划会标明需要补齐：

```json
{"status":"paused","waiting_on":"public_sample","resume_condition":"出现公开脱敏的原始样例并能够复现该步骤","next_review":"YYYY-MM-DD"}
```

`waiting_on` 可为 `public_feedback`（公开新反馈）、`product_delivery`（现有产品交付）、`public_sample`（公开样例）、`user_material`（需要授权的用户材料）。仍须填写原有 priority、gap、next_action、reason、evidence_source_ids。到期仅本地复核；没有恢复证据时不能自动改成 active。旧日期和原文保持不变。

## 评论身份与定位补证

`task-research plan` 的 `identity_checks` 展示缺少的账号身份、评论定位及已知作者页。纳入原有每任务一条完整原文和一种替代方案、整轮最多六页的免费访问额度；页面不足时保留未知，不扩大付费采集。查询更多评论不能替代身份核验。

身份更正先走 `demand-audit --audit-action amend`，再在完整的 `sources` 观察顶层添加 `identity_observations`；不能跳过来源全覆盖或任务重审：

```json
{"identity_observations":[{"source_id":"s-原ID","channel_id":"UC加22位真实频道ID",
 "comment":{"url":"https://www.youtube.com/watch?v=真实视频ID&lc=真实评论ID","evidence_file":"validation-evidence/comment.txt","quote":"包含该条完整原文和原作者页面URL的逐字材料","observed_at":"实际带时区时间"},
 "profile":{"url":"计划中的原作者页面URL","evidence_file":"validation-evidence/profile.txt","quote":"包含/channel/真实频道ID的逐字材料","observed_at":"实际带时区时间"}}]}
```

原始内容及身份历史保留；校验同一视频、完整评论与作者链接绑定、频道ID、文件引用及时间。身份更正使旧任务指纹失效，随后正式逐任务审核和原文/独立性验证仍必须完成。仅有 handle、显示名或共用页面时不补造身份。

## 预处理与离线验证

`demands.preprocessing_mode` 支持 `full`（兼容缺省）和 `extract_only`。后者只做初次提取，省去第二轮模型盲审、模型聚类和任务评审；缺失审核明确待处理，最终仍由 Codex 逐条覆盖正例、负例、模糊项、历史积压及到期任务。模型输出无法发布机会。分类按完整来源大小分批，长文单独一批，不截断原文；七天审核有效期不变。

```powershell
.\.venv-win\Scripts\python.exe scripts/study_preprocessing.py --state state --since YYYY-MM-DD --until YYYY-MM-DD
```

此命令只统计已保存请求，比较历史全流程与仅提取部分的模型调用和估算费用，不产生新模型输出、不读取 gold 标签，也不证明未来模型质量或实际节省。质量回归使用合成样例和保存的真实来源隔离回放；真实供应商质量仍由后续正式日报检验。

查询策略已升级为 2；冻结计划的版本/配置不匹配时明确拒绝恢复，不能拿新词或新模式重跑旧日期。历史结果、原账本和收费阶段保留。
