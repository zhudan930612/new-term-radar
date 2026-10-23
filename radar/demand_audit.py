"""Local semantic audit handoff. Codex reviews all sources, not only survivors.

Qwen output is provisional: a deterministic quote check cannot establish that a
marketing claim is a real user's problem. This gate uses the scheduled agent's
source reading and preserves its corrections without new paid API calls.
"""
import copy
import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import demands, google_trends, opportunity_validation, spending


def due_tasks(ledger: dict, day: str, force_ids=()) -> list[dict]:
    cutoff = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    tasks = [t for t in demands.active_tasks(ledger, day) if
             t["id"] in force_ids or t.get("fingerprint_migration_pending") or t.get("semantic_reviewed", "") <= cutoff or
             t.get("semantic_fingerprint") != demands.review_fingerprint(t, ledger) or
             t["status"] in {"待复核", "待拆分核验", "分析待修复"}]
    return sorted(tasks, key=lambda t: (t.get("semantic_reviewed", ""), t["first_seen"], t["id"]))


def merge_context(ledger: dict, day: str) -> list[dict]:
    return demands.historical_matches(ledger, due_tasks(ledger, day)[:18], day)


def confirm_followups(result: dict, ledger: dict) -> None:
    """Confirm newly observed same-task originals, never customers or promotion.

    A query's lexical match is insufficient: the source must actually belong to
    the reviewed target (or its approved merge lineage), with a current semantic
    decision. The source's stage is matched separately from the task identity.
    """
    stages = {name: result[name] for name in ("collection", "research_collection") if isinstance(result.get(name), dict)}
    for stage in stages.values():
        stage["confirmed_followups"], stage["confirmed_followup_sources"] = [], []
    if result.get("status") != "complete" or result.get("semantic_audit", {}).get("status") != "complete":
        for stage in stages.values():
            stage["confirmation_status"] = "not_evaluated"
        return
    for stage in stages.values():
        stage["confirmation_status"] = "evaluated"
    day = result["date"]
    scope = {r["id"] for r in result.get("dispositions", [])}
    normalized = lambda text: re.sub(r"\W+", "", str(text).casefold())
    historical = [e["source"] for e in ledger["sources"].values() if e.get("first_seen", day) < day]
    seen_text = {normalized(s.get("text", "")) for s in historical}
    copies = Counter(normalized(e["source"].get("text", "")) for e in ledger["sources"].values())

    def provider(source):
        host = (urlsplit(source.get("url", "")).hostname or "").removeprefix("www.")
        if host in {"x.com", "twitter.com"}: return "x"
        if host in {"reddit.com", "old.reddit.com"}: return "reddit"
        if host in {"youtube.com", "youtu.be", "m.youtube.com"}: return "youtube_comments"
        if host == "chromewebstore.google.com": return "chrome_reviews"
        if host == "github.com": return "github"
        if host == "news.ycombinator.com": return "hackernews"
        return ""

    def lineage(task):
        ids = {task["id"], *task.get("merged_ids", [])}
        for member in task.get("merge_members", []):
            ids.update(lineage(member))
        return ids

    for task in demands.active_tasks(ledger, day):
        fingerprint = demands.review_fingerprint(task, ledger)
        decision = task.get("review", {})
        approved = (decision.get("same_job") is True and decision.get("firsthand") is True and
                    task.get("semantic_fingerprint") == fingerprint and not demands.review_due(task, ledger, day) and
                    task.get("withdrawn_fingerprint") != fingerprint)
        refs = {r["source_id"]: r for r in demands.references(task, ledger)}
        valid = {sid for sid, ref in refs.items() if approved and ref.get("kind") != "official" and
                 not ref["classification_disagreement"] and not ref["analysis_error"] and
                 ledger["sources"][sid].get("assessment", {}).get("category") == "need"}
        live = list(task.get("confirmed_followup_sources", []))
        for sid in list(live):
            if sid not in valid:
                live.remove(sid)
                task.setdefault("followup_confirmation_history", []).append({"event": "revoked", "source_id": sid,
                    "date": day, "audit_revision": result["semantic_audit"].get("revision", 1)})
        identities = lineage(task)
        for sid in sorted(valid):
            entry, source = ledger["sources"][sid], ledger["sources"][sid]["source"]
            target = source.get("task_id")
            if (sid not in scope or target not in identities or entry.get("first_seen") != day or
                    entry.get("semantic_review", {}).get("date") != day):
                continue
            matching_stages = [name for name, stage in stages.items() if any(
                q.get("task_id") == target and q.get("source") == provider(source) and q.get("status") == "complete" and
                type(q.get("accepted")) is int and q["accepted"] > 0
                for q in stage.get("queries", []))]
            if not matching_stages:
                continue
            text_key = normalized(source.get("text", ""))
            # Updated posts/issues at the same URL are revisits; shared-video
            # comments may be distinct, but identical text/record IDs are not.
            previous = [e["source"] for other_sid, e in ledger["sources"].items() if other_sid != sid]
            revisit = any(old.get("url", "").rstrip("/") == source.get("url", "").rstrip("/") and
                (source.get("kind") != "comment" or (source.get("source_record_id") and
                 old.get("source_record_id") == source["source_record_id"])) for old in previous)
            if sid not in live and (not text_key or copies[text_key] > 1 or text_key in seen_text or revisit):
                continue
            seen_text.add(text_key)
            if sid not in live:
                live.append(sid)
                task.setdefault("followup_confirmation_history", []).append({"event": "confirmed", "source_id": sid,
                    "date": day, "target_task_id": target, "canonical_task_id": task["id"], "stages": matching_stages,
                    "audit_revision": result["semantic_audit"].get("revision", 1), "review_fingerprint": fingerprint,
                    "reviewer": result["semantic_audit"].get("reviewer", ""),
                    "meaning": "same-task original confirmed; does not establish an additional independent customer"})
            for name in matching_stages:
                stage = stages[name]
                if target not in stage["confirmed_followups"]:
                    stage["confirmed_followups"].append(target)
                row = {"task_id": target, "canonical_task_id": task["id"], "source_id": sid}
                if row not in stage["confirmed_followup_sources"]:
                    stage["confirmed_followup_sources"].append(row)
        if live or "confirmed_followup_sources" in task:
            task["confirmed_followup_sources"] = live
            dates = [r["date"] for r in task.get("followup_confirmation_history", [])
                     if r.get("event") == "confirmed" and r.get("source_id") in live]
            if dates:
                task["last_evidence"] = max(dates)
            else:
                task.pop("last_evidence", None)
    targets = {tid for stage in stages.values() for tid in stage.get("attempted_followups", [])}
    confirmed = {tid for stage in stages.values() for tid in stage["confirmed_followups"]}
    for task in ledger["tasks"].values():
        for event in task.get("search_history", []):
            if task["id"] in targets and event.get("date") == day:
                event["confirmation"] = {"status": "confirmed" if task["id"] in confirmed else "no_confirmed_evidence",
                    "audit_revision": result["semantic_audit"].get("revision", 1),
                    "meaning": "same-task original; independent users are checked separately"}


def query_metrics(result: dict) -> list[dict]:
    """Per-query yield; missing historical attribution stays unknown."""
    categories = {r["id"]: r["category"] for r in result.get("dispositions", [])}
    complete = result.get("status") == "complete" and result.get("semantic_audit", {}).get("status") == "complete"
    rows = []
    for name in ("collection", "research_collection"):
        stage = result.get(name, {})
        confirmed = {r["source_id"] for r in stage.get("confirmed_followup_sources", [])}
        for query in stage.get("queries", []):
            sources = [e for e in result.get("evidence", []) if e.get("query") == query.get("query") and
                       e.get("query_source") == query.get("source")]
            ids = {demands.source_id(s) for s in sources}
            # Stable IDs derive from saved raw evidence. Daily display IDs are
            # reused across days and must never be used as an attribution join.
            rows.append({"source": query.get("source"), "query": query.get("query"), "task_id": query.get("task_id", ""),
                         "status": query.get("status"), "raw_count": len(sources),
                         "attribution": "recorded" if sources else "unknown_or_empty",
                         "need_count": sum(categories.get(sid) == "need" for sid in ids),
                         "promotion_count": sum(categories.get(sid) == "promotion" for sid in ids),
                         "confirmation_status": "evaluated" if complete else "not_evaluated",
                         "confirmed_same_task_count": len(ids & confirmed) if complete else None})
    return rows


def _archive(root, revision, action, payload, before, result, ledger, plan):
    """Immutable event + snapshot; mutable index only points to preserved events."""
    directory = root / "audit-history"
    directory.mkdir(exist_ok=True)
    index_path = directory / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"events": []}
    event = {"revision": revision, "action": action, "payload": payload, "before": before,
             "after": {"result": result, "ledger": ledger, "plan": plan}}
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    name = f"{len(index['events']) + 1:05d}-r{revision}-{action}-{sha[:16]}.json"
    with (directory / name).open("xb") as handle:
        handle.write(raw)
    index["events"].append({"file": name, "sha256": sha, "revision": revision, "action": action,
                            "commit_file": name + ".committed"})
    spending.save(index_path, index)
    return directory / (name + ".committed"), name


def command(action: str, result_path: Path, ledger_path: Path, observations: Path | None = None,
            output: Path | None = None) -> int:
    if (result_path.parent / "run.lock").exists():
        raise ValueError("Daily run still active; do not audit its changing result")
    lock = ledger_path.with_suffix(".audit.lock")
    result_lock = result_path.parent / "trends-edit.lock"
    acquired_result_lock = False
    prepared, committed = False, False
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        with result_lock.open("x", encoding="utf-8"):
            pass
        acquired_result_lock = True
        result = json.loads(result_path.read_text(encoding="utf-8"))
        before = {"result": copy.deepcopy(result), "ledger": json.loads(ledger_path.read_text(encoding="utf-8"))}
        ledger = demands.load(ledger_path)
        if result.get("workflow_version") != 2:
            raise ValueError("Audit requires a task-led result")
        stage = result.setdefault("semantic_audit", {"status": "pending"})
        stage.setdefault("revision", 1)
        payload = None
        ids = [r["id"] for r in result["dispositions"]]
        if action == "claim":
            if stage["status"] != "pending":
                print(json.dumps({"claimed": False, "status": stage["status"]}))
                return 0
            stage["status"] = "running"
        elif action == "amend":
            if stage["status"] in {"pending", "running"}:
                raise ValueError("An audit is already awaiting source observations")
            payload = json.loads(observations.read_text(encoding="utf-8")) if observations else {}
            active_ids = {t["id"] for t in demands.active_tasks(ledger, result["date"])}
            forced = payload.get("task_ids", sorted(active_ids))
            if not isinstance(forced, list) or any(not isinstance(t, str) for t in forced) or not set(forced) <= active_ids:
                raise ValueError("Amendment task IDs must refer to active tasks")
            stage.update(status="running", revision=stage["revision"] + 1, forced_task_ids=forced,
                         reason=payload.get("reason", "Local semantic correction; prior evidence preserved in audit-history"))
            result["provisional_opportunities"] = result.get("opportunities", [])
            result["opportunities"] = []
            result["status"] = "awaiting_audit"
            result["zero_result_audit"]["status"] = "incomplete"
            previous_market = result.get("market_research", {})
            if previous_market.get("status") in {"running", "complete", "partial", "incomplete"}:
                result["market_research"] = {**previous_market, "status": "incomplete", "items": [],
                    "previous_stage": copy.deepcopy(previous_market),
                    "previous_opportunities": copy.deepcopy(before["result"].get("opportunities", [])),
                    "requires_local_review": True, "automatic_retry_allowed": False,
                    "reason": "语义修订后原付费研究仅保留历史，需本地复核；禁止自动重新执行或再次收费"}
            else:
                result.pop("market_research", None)
            for key in ("opportunity_validation", "google_trends"):
                result.pop(key, None)
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
                from .source_identity import apply as apply_identities
                apply_identities(ledger, payload.get("identity_observations", []), ids, result_path.parent, result["date"])
                for sid, assessment in checked:
                    entry = ledger["sources"][sid]
                    entry.setdefault("assessment_history", []).append({"date": result["date"], **entry.get("assessment", {})})
                    entry.update(assessment=assessment, audit={**assessment, "date": result["date"]}, disagreement=False)
                    entry.pop("analysis_error", None)
                    entry["semantic_review"] = {"reviewer": payload["reviewer"], "method": payload["method"], "date": result["date"]}
                demands.materialize(ledger)
                stage.update(status="tasks_pending", reviewer=payload["reviewer"], method=payload["method"], sources=len(ids),
                             task_scope=len(due_tasks(ledger, result["date"], stage.get("forced_task_ids", []))))
            else:
                if any(ledger["sources"][sid].get("semantic_review", {}).get("date") != result["date"] for sid in ids):
                    raise ValueError("All current source dispositions need semantic review before task decisions")
                groups = payload.get("groups", [])
                initial_batch = due_tasks(ledger, result["date"], stage.get("forced_task_ids", []))[:18]
                group_targets = set()
                if groups:
                    active = demands.active_tasks(ledger, result["date"])
                    active_by_id = {t["id"]: t for t in active}
                    for group in groups:
                        members = [active_by_id[tid] for tid in group.get("ids", []) if tid in active_by_id]
                        if not members or any(not demands.references(t, ledger) for t in members):
                            raise ValueError("Historical merge members require available original evidence")
                        group_targets.add(min(members, key=lambda t: (t["first_seen"], t["id"]))["id"])
                    demands.cluster(None, ledger, active, supplied=groups)
                batch_ids = ({t["id"] for t in initial_batch} | group_targets) & set(ledger["tasks"])
                tasks = [ledger["tasks"][tid] for tid in sorted(batch_ids)]
                rows = payload.get("reviews", [])
                if len(rows) != len(tasks) or {r.get("id") for r in rows} != {t["id"] for t in tasks}:
                    raise ValueError("Audit must disposition every active task")
                from .task_research import validate_research, apply_research, needs_validation
                research = {row["id"]: validate_research(row["research"], result["date"],
                    source_ids=ledger["tasks"][row["id"]]["source_ids"]) for row in rows if "research" in row}
                proposals, errors = demands.review(None, ledger, tasks, result["date"], supplied=rows)
                for task in tasks:
                    if task["id"] in ledger["tasks"] and not demands.review_due(task, ledger, result["date"]):
                        task.update(semantic_reviewed=result["date"], semantic_fingerprint=demands.review_fingerprint(task, ledger))
                        task.pop("fingerprint_migration_pending", None)
                        if task["id"] in research:
                            apply_research(task, research[task["id"]], result["date"], payload["reviewer"], "semantic_audit")
                            task["research"]["evidence_fingerprint"] = demands.review_fingerprint(task, ledger)
                        stage["forced_task_ids"] = [tid for tid in stage.get("forced_task_ids", []) if tid != task["id"]]
                stage["forced_task_ids"] = [tid for tid in stage.get("forced_task_ids", []) if tid in ledger["tasks"]]
                pending = due_tasks(ledger, result["date"], stage.get("forced_task_ids", []))
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
                         t.get("proposal") and t not in pending and not demands.review_due(t, ledger, result["date"]) and
                         t.get("semantic_fingerprint") == demands.review_fingerprint(t, ledger) and
                         (t.get("shortlisted_fingerprint") != t.get("review_fingerprint") or
                                               t.get("last_shortlisted") == result["date"] or needs_validation(t, ledger))]
                ready.sort(key=lambda t: (t.get("last_shortlisted", ""), t["first_seen"], t["id"]))
                result["status"] = "complete" if not result["errors"] and not pending else "incomplete"
                result["provisional_opportunities"] = [{**t["proposal"],
                    "is_new": t.get("shortlisted_fingerprint") != t.get("review_fingerprint") or t.get("last_shortlisted") == result["date"],
                    "validation_continuation": t.get("shortlisted_fingerprint") == t.get("review_fingerprint") and
                                               t.get("last_shortlisted") != result["date"]} for t in ready[:3]]
                result["opportunities"] = result["provisional_opportunities"] if result["status"] == "complete" else []
                if result["status"] == "complete":
                    for task in ready[:3]:
                        if task.get("shortlisted_fingerprint") != task.get("review_fingerprint"):
                            task.update(last_shortlisted=result["date"], shortlisted_fingerprint=task["review_fingerprint"])
                result["zero_result_audit"]["status"] = "complete" if result["status"] == "complete" else "incomplete"
                result["zero_result_audit"]["semantic_reviewer"] = payload["reviewer"]
        else:
            raise ValueError("Unsupported audit action")
        confirm_followups(result, ledger)
        result["query_metrics"] = query_metrics(result)
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
                 deferred_tasks=len(due_tasks(ledger, result["date"], stage.get("forced_task_ids", []))))
        if stage["status"] == "complete":
            for key in ("opportunity_validation", "google_trends"):
                if result.get(key, {}).get("status") in {None, "skipped", "waiting_validation"}:
                    result.pop(key, None)
            opportunity_validation.prepare(result, ledger=ledger, rebuild=True)
            google_trends.prepare(result, {"google_trends": {"enabled": True, "geo": "US"}})
        if stage["status"] == "complete" and result["status"] == "complete":
            from .task_research import plan as research_plan
            result["research_plan"] = research_plan(ledger, result["date"], limit=3)
        else:
            result.pop("research_plan", None)
        plan = {"date": result["date"], "status": stage["status"],
                "sources": [{"id": sid, **ledger["sources"][sid]["source"]} for sid in ids],
                "tasks": [{**t, "evidence": demands.references(t, ledger)} for t in due_tasks(ledger, result["date"], stage.get("forced_task_ids", []))[:18]],
                "merge_context": [{**t, "evidence": demands.references(t, ledger)} for t in merge_context(ledger, result["date"])],
                "merge_suggestions": ledger.get("merge_suggestions", []),
                "historical_task_index": [{k: t.get(k) for k in ("id", "job", "output", "constraint", "search_terms", "source_ids")}
                                          for t in demands.active_tasks(ledger, result["date"])]}
        # Stable source IDs must not be overwritten by original display IDs.
        for row, sid in zip(plan["sources"], ids):
            row["id"] = sid
        commit, event_name = _archive(result_path.parent, stage["revision"], action, payload, before, result, ledger, plan)
        prepared = True
        if payload is not None and action in {"sources", "tasks"}:
            saved = payload if action == "sources" else {**payload, "reviews": [t["review"] for t in task_rows
                if isinstance(t.get("review"), dict) and t.get("semantic_reviewed") == result["date"] and
                t.get("semantic_fingerprint") == demands.review_fingerprint(t, ledger)],
                "latest_batch_event": event_name}
            spending.save(result_path.parent / ("semantic-" + action + "-observations.json"), saved)
        spending.save(ledger_path, ledger)
        google_trends.write_result(result_path, result, output)
        spending.save(result_path.parent / "semantic-audit-plan.json", plan)
        research_path = result_path.parent / "research-plan.json"
        if "research_plan" in result:
            spending.save(research_path, result["research_plan"])
        elif research_path.exists():
            # Previous plan remains in the revision's immutable before snapshot;
            # do not leave an actionable alias while its audit is being amended.
            spending.save(research_path, {"date": result["date"], "status": "awaiting_audit", "items": [],
                                         "api_cost_usd": 0, "reason": "Semantic audit is not complete"})
        from .daily_recovery import record_status
        record_status(result_path.parent, result)
        with commit.open("x", encoding="utf-8") as handle:
            handle.write("committed\n")
        committed = True
        print(json.dumps({"status": stage["status"], "sources": len(ids), "tasks": len(task_rows),
                          "plan": str(result_path.parent / "semantic-audit-plan.json")}, ensure_ascii=False))
        return 0
    finally:
        # A partially written revision must be recovered from its immutable
        # event, not silently followed by another daily run against mixed state.
        if acquired_result_lock and (not prepared or committed):
            result_lock.unlink(missing_ok=True)
        if not prepared or committed:
            lock.unlink(missing_ok=True)
