"""Derived daily readout: immutable audit snapshot plus current research decisions.

No collection, audit, plan, shortlist, or billing state is rewritten here.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from . import demands, opportunities, spending, task_research


def build(result: dict, ledger: dict, entries: list[dict], as_of: str) -> dict:
    from .demand_audit import query_metrics
    summary = copy.deepcopy(result)
    summary["summary_as_of"] = as_of
    summary["summary_kind"] = "derived_research_readout"
    summary["request_billing"] = spending.billing_summary(entries)
    summary["query_metrics"] = query_metrics(result)
    updates = []
    ids = {t["id"] for t in result.get("tasks", [])}
    ids.update(i["task_id"] for i in result.get("research_plan", {}).get("items", []))
    for tid in sorted(ids):
        task = ledger["tasks"].get(tid)
        if not task or not task.get("research"):
            continue
        research = task["research"]
        valid = (task_research._reviewed(task, ledger) and
                 research.get("evidence_fingerprint") == demands.review_fingerprint(task, ledger))
        updates.append({"task_id": tid, "job": task["job"], "research": copy.deepcopy(research),
                        "evidence_current": valid})
    summary["research_updates"] = updates
    # The original plan is a receipt, never an instruction to repeat finished work.
    summary["research_plan_snapshot"] = summary.pop("research_plan", {})
    return summary


def render(summary: dict) -> str:
    lines = ["# 日报研究后汇总", "", f"更新于 {summary['summary_as_of']}。采集与审核结论取自原始日报，研究状态取自当前需求池。",
             "原始领取计划保留在 research_plan_snapshot；本汇总不授权重新采集或发布机会。", "",
             "## 最新研究决定", ""]
    for item in summary["research_updates"]:
        r = item["research"]
        stale = "；证据已变化，先本地复核" if not item["evidence_current"] else ""
        lines += [f"- **{opportunities.clean(item['job'])}**：{r['status']}{stale}；复查 {r.get('next_review') or '按证据变化'}。",
                  f"  下一步：{opportunities.clean(r['next_action'])}"]
        if r.get("resume_condition"):
            lines.append(f"  恢复条件（{r.get('waiting_on', 'unspecified')}）：{opportunities.clean(r['resume_condition'])}")
    # Avoid stale research instructions in the historical task appendix.
    view = copy.deepcopy(summary)
    for task in view.get("tasks", []):
        task.pop("research", None)
    return "\n".join(lines) + "\n\n" + opportunities.render(view)


def command(state: Path, day: str, output: Path | None = None, *, ledger_path: Path | None = None, emit=True) -> int:
    day = date.fromisoformat(day).isoformat()
    root = state / "web-opportunities" / day
    ledger_path = ledger_path or state / "demand-ledger.json"
    protected = [ledger_path, root / "result.json", root / "api/requests.json"]
    if ledger_path.with_suffix(".audit.lock").exists() or any(root.glob("*.lock")) or (root / "api/requests.lock").exists():
        raise ValueError("Daily/audit active; generate summary after the writer finishes")
    raw = {p: p.read_bytes() for p in protected if p.exists()}
    if root / "result.json" not in raw or ledger_path not in raw:
        raise ValueError("Summary requires a saved result and demand ledger")
    result = json.loads(raw[root / "result.json"])
    if result.get("date") != day:
        raise ValueError("Summary date does not match result")
    summary = build(result, json.loads(raw[ledger_path]), json.loads(raw.get(root / "api/requests.json", b"[]")),
                    datetime.now(timezone.utc).isoformat())
    summary["input_sha256"] = {str(p.resolve()): hashlib.sha256(data).hexdigest() for p, data in raw.items()}
    target = output or root / "summary.md"
    # Refuse to replace originals/plans/evidence via --out, including JSON aliases.
    targets = {target.resolve(), target.with_suffix(".json").resolve()}
    reserved = {p.resolve() for p in protected} | {root.joinpath(n).resolve() for n in
        ("result.md", "research-plan.json", "collection-plan.json", "semantic-audit-plan.json")}
    if (targets & reserved or target.suffix.lower() != ".md" or
            target.resolve().is_relative_to(state.resolve()) and target.resolve() != (root / "summary.md").resolve()):
        raise ValueError("Summary output must be a separate Markdown path")
    if any(p.read_bytes() != data for p, data in raw.items()) or ledger_path.with_suffix(".audit.lock").exists():
        raise ValueError("Inputs changed during summary; retry after writers finish")
    target.parent.mkdir(parents=True, exist_ok=True)
    spending.save(target.with_suffix(".json"), summary)
    target.write_text(render(summary), encoding="utf-8")
    if emit:
        print(json.dumps({"summary": str(target), "date": day, "status": result["status"], "paid_calls": 0}, ensure_ascii=False))
    return 0
