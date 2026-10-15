"""Local semantic audit handoff. Codex reviews all sources, not only survivors.

Qwen output is provisional: a deterministic quote check cannot establish that a
marketing claim is a real user's problem. This gate uses the scheduled agent's
source reading and preserves its corrections without new paid API calls.
"""
import json
from datetime import date, timedelta
from pathlib import Path

from . import demands, google_trends, opportunity_validation, spending


def due_tasks(ledger: dict, day: str) -> list[dict]:
    cutoff = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    tasks = [t for t in demands.active_tasks(ledger, day) if
             t.get("semantic_reviewed", "") <= cutoff or
             t.get("semantic_fingerprint") != demands.review_fingerprint(t, ledger) or
             t["status"] in {"待复核", "待拆分核验", "分析待修复"}]
    return sorted(tasks, key=lambda t: (t.get("semantic_reviewed", ""), t["first_seen"], t["id"]))


def merge_context(ledger: dict, day: str) -> list[dict]:
    due = {t["id"] for t in due_tasks(ledger, day)}
    return [t for t in demands.active_tasks(ledger, day) if t["id"] not in due][:18]


def command(action: str, result_path: Path, ledger_path: Path, observations: Path | None = None,
            output: Path | None = None) -> int:
    if (result_path.parent / "run.lock").exists():
        raise ValueError("Daily run still active; do not audit its changing result")
    lock = ledger_path.with_suffix(".audit.lock")
    result_lock = result_path.parent / "trends-edit.lock"
    acquired_result_lock = False
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        with result_lock.open("x", encoding="utf-8"):
            pass
        acquired_result_lock = True
        result = json.loads(result_path.read_text(encoding="utf-8"))
        ledger = demands.load(ledger_path)
        if result.get("workflow_version") != 2:
            raise ValueError("Audit requires a task-led result")
        stage = result.setdefault("semantic_audit", {"status": "pending"})
        ids = [r["id"] for r in result["dispositions"]]
        if action == "claim":
            if stage["status"] != "pending":
                print(json.dumps({"claimed": False, "status": stage["status"]}))
                return 0
            stage["status"] = "running"
        elif action in {"sources", "tasks"}:
            expected_stage = "running" if action == "sources" else "tasks_pending"
            if stage["status"] != expected_stage or observations is None:
                raise ValueError("Claim the audit first and provide saved observations")
            payload = json.loads(observations.read_text(encoding="utf-8"))
            if payload.get("date") != result["date"] or not payload.get("reviewer") or not payload.get("method"):
                raise ValueError("Audit date, reviewer and method required")
            if action == "sources":
                rows = payload.get("rows", [])
                if len(rows) != len(ids) or {r.get("id") for r in rows} != set(ids):
                    raise ValueError("Semantic audit must cover every source, including all negatives")
                checked = [(r["id"], demands.validate_row(r, ledger["sources"][r["id"]]["source"])) for r in rows]
                for sid, assessment in checked:
                    entry = ledger["sources"][sid]
                    entry.setdefault("assessment_history", []).append({"date": result["date"], **entry.get("assessment", {})})
                    entry.update(assessment=assessment, audit={**assessment, "date": result["date"]}, disagreement=False)
                    entry.pop("analysis_error", None)
                    entry["semantic_review"] = {"reviewer": payload["reviewer"], "method": payload["method"], "date": result["date"]}
                demands.materialize(ledger)
                stage.update(status="tasks_pending", reviewer=payload["reviewer"], method=payload["method"], sources=len(ids),
                             task_scope=len(due_tasks(ledger, result["date"])))
            else:
                if any(ledger["sources"][sid].get("semantic_review", {}).get("date") != result["date"] for sid in ids):
                    raise ValueError("All current source dispositions need semantic review before task decisions")
                groups = payload.get("groups", [])
                if groups:
                    demands.cluster(None, ledger, due_tasks(ledger, result["date"])[:18] + merge_context(ledger, result["date"]), supplied=groups)
                tasks = due_tasks(ledger, result["date"])[:18]
                rows = payload.get("reviews", [])
                if len(rows) != len(tasks) or {r.get("id") for r in rows} != {t["id"] for t in tasks}:
                    raise ValueError("Audit must disposition every active task")
                proposals, errors = demands.review(None, ledger, tasks, result["date"], supplied=rows)
                for task in tasks:
                    if task["id"] in ledger["tasks"] and not demands.review_due(task, ledger, result["date"]):
                        task.update(semantic_reviewed=result["date"], semantic_fingerprint=demands.review_fingerprint(task, ledger))
                pending = due_tasks(ledger, result["date"])
                if pending:
                    errors.append(f"{len(pending)} 个任务尚需复核")
                stage.update(status="complete" if not errors else "tasks_pending", task_errors=errors,
                             reviewer=payload["reviewer"], method=payload["method"])
                old_analysis = set(result.get("analysis_errors", []))
                cutoff = (date.fromisoformat(result["date"]) - timedelta(days=60)).isoformat()
                audit_cutoff = (date.fromisoformat(result["date"]) - timedelta(days=7)).isoformat()
                outside = [sid for sid, entry in ledger["sources"].items() if sid not in ids and
                           entry["last_seen"] >= cutoff and (not entry.get("assessment") or not entry.get("audit") or entry.get("analysis_error") or
                           (entry.get("disagreement") and entry.get("audit", {}).get("date", "") <= audit_cutoff))]
                if outside:
                    errors.append(f"{len(outside)} 条历史来源未在本轮语义复核范围内，仍待修复")
                if result.get("coverage", {}).get("current_sources", len(result.get("evidence", []))) == 0:
                    errors.append("本轮没有有效来源样本，不能据此判断没有需求")
                result["errors"] = [e for e in result.get("errors", []) if e not in old_analysis] + errors
                result["analysis_errors"] = errors
                ready = [t for t in demands.active_tasks(ledger, result["date"]) if t["status"] == "值得深入验证" and
                         t.get("proposal") and t not in pending and (t.get("shortlisted_fingerprint") != t.get("review_fingerprint") or
                                               t.get("last_shortlisted") == result["date"])]
                ready.sort(key=lambda t: (t.get("last_shortlisted", ""), t["first_seen"], t["id"]))
                result["opportunities"] = [t["proposal"] for t in ready[:3]]
                for task in ready[:3]:
                    task.update(last_shortlisted=result["date"], shortlisted_fingerprint=task["review_fingerprint"])
                result["status"] = "complete" if not result["errors"] and not pending else "incomplete"
                result["zero_result_audit"]["status"] = "complete" if result["status"] == "complete" else "incomplete"
                result["zero_result_audit"]["semantic_reviewer"] = payload["reviewer"]
            spending.save(result_path.parent / ("semantic-" + action + "-observations.json"), payload)
        task_rows = [{**t, "evidence": demands.references(t, ledger)} for t in demands.active_tasks(ledger, result["date"])]
        result["tasks"] = task_rows
        result["zero_result_audit"]["task_ids"] = [t["id"] for t in task_rows]
        for row in result["dispositions"]:
            entry = ledger["sources"][row["id"]]
            row.update(category=entry.get("assessment", {}).get("category", "analysis_pending"),
                       reason=entry.get("assessment", {}).get("reason", "未完成"),
                       disagreement=entry.get("disagreement", False), analysis_error=entry.get("analysis_error", ""))
        c = result["coverage"]
        c.setdefault("model_review_due", c.get("review_due", 0))
        if stage.get("task_scope") is not None:
            c["review_due"] = stage["task_scope"]
        c.update(classified=sum(r["category"] != "analysis_pending" for r in result["dispositions"]),
                 audit_complete=sum(bool(ledger["sources"][sid].get("audit")) for sid in ids),
                 unresolved_sources=sum(bool(ledger["sources"][sid].get("analysis_error")) for sid in ids),
                 active_tasks=len(task_rows), reviewed_today=sum(t.get("reviewed") == result["date"] for t in task_rows),
                 deferred_tasks=len(due_tasks(ledger, result["date"])))
        if stage["status"] == "complete":
            for key in ("opportunity_validation", "google_trends"):
                if result.get(key, {}).get("status") in {None, "skipped", "waiting_validation"}:
                    result.pop(key, None)
            opportunity_validation.prepare(result)
            google_trends.prepare(result, {"google_trends": {"enabled": True, "geo": "US"}})
        spending.save(ledger_path, ledger)
        google_trends.write_result(result_path, result, output)
        plan = {"date": result["date"], "status": stage["status"],
                "sources": [{"id": sid, **ledger["sources"][sid]["source"]} for sid in ids],
                "tasks": [{**t, "evidence": demands.references(t, ledger)} for t in due_tasks(ledger, result["date"])[:18]],
                "merge_context": [{**t, "evidence": demands.references(t, ledger)} for t in merge_context(ledger, result["date"])]}
        # Stable source IDs must not be overwritten by original display IDs.
        for row, sid in zip(plan["sources"], ids):
            row["id"] = sid
        spending.save(result_path.parent / "semantic-audit-plan.json", plan)
        print(json.dumps({"status": stage["status"], "sources": len(ids), "tasks": len(task_rows),
                          "plan": str(result_path.parent / "semantic-audit-plan.json")}, ensure_ascii=False))
        return 0
    finally:
        if acquired_result_lock:
            result_lock.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
