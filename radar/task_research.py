"""Evidence-backed seed research and scheduling, without promotion or network calls."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import spending


STATES = {"active", "paused", "resolved"}
KINDS = {"source_context", "alternative", "task_sample", "experiment", "channel", "payment"}


def today_local() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def _day(value) -> date:
    return value if type(value) is date else date.fromisoformat(value)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_research(value: dict, day: date | str, source_ids=None) -> dict:
    """Validate a reviewer-authored decision; no state is inferred from prose."""
    day = _day(day)
    allowed = {"status", "priority", "gap", "next_action", "reason", "next_review", "evidence_source_ids", "query_variants"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unknown research decision fields")
    if value.get("status") not in STATES or type(value.get("priority")) is not int or not 0 <= value["priority"] <= 3:
        raise ValueError("Research status and priority 0..3 required")
    if any(not isinstance(value.get(k), str) or not 1 <= len(value[k].strip()) <= 1500 for k in ("gap", "next_action", "reason")):
        raise ValueError("Research gap, next action and reason required")
    next_review = value.get("next_review")
    if next_review is not None:
        parsed = _day(next_review)
        if not day <= parsed <= day + timedelta(days=90):
            raise ValueError("Next review must be within 90 days and not in the past")
    if value["status"] == "paused" and next_review is None:
        raise ValueError("Paused research needs an explicit review date")
    if value["status"] == "resolved" and next_review is not None:
        raise ValueError("Resolved research cannot schedule automatic followup")
    sources = value.get("evidence_source_ids", [])
    if (not isinstance(sources, list) or any(not isinstance(s, str) or not s for s in sources) or
            len(set(sources)) != len(sources) or (source_ids is not None and not set(sources) <= set(source_ids)) or
            (value["priority"] > 0 and not sources)):
        raise ValueError("Priority evidence must reference this task's sources")
    variants = value.get("query_variants", [])
    if (not isinstance(variants, list) or len(variants) > 2 or any(not isinstance(q, str) or not 3 <= len(q.strip()) <= 100 or
            re.search(r'[\n\r"\'“”‘’():<>]|\b(?:AND|OR|NOT)\b', q) for q in variants)):
        raise ValueError("Use at most two natural task phrases without query operators")
    return {**value, "next_review": next_review, "evidence_source_ids": list(sources),
            **({"query_variants": list(variants)} if "query_variants" in value else {})}


def apply_research(task: dict, value: dict, day: date | str, reviewer: str, origin: str) -> None:
    value = validate_research(value, day, task["source_ids"])
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(origin, str) or not origin.strip():
        raise ValueError("Research reviewer and origin required")
    current = task.setdefault("research", {})
    event = {"date": _day(day).isoformat(), "reviewer": reviewer, "origin": origin, "decision": copy.deepcopy(value)}
    event["id"] = _hash(event)
    history = current.setdefault("history", [])
    if not any(r.get("id") == event["id"] for r in history):
        history.append(event)
    current.update(value)


def needs_validation(task: dict, ledger: dict) -> bool:
    from . import demands
    fingerprint = demands.review_fingerprint(task, ledger)
    if task.get("withdrawn_fingerprint") == fingerprint:
        return False
    stage = task.get("validation", {})
    prior_version = stage.get("evidence_fingerprint")
    if prior_version and not str(prior_version).startswith("v2:") and fingerprint.startswith("v2:"):
        return False  # Schema migration alone must not trigger paid/browser research.
    if stage.get("evidence_fingerprint") != fingerprint:
        return bool(task.get("last_shortlisted") or stage)
    return stage.get("status") in {"pending", "blocked", "needs_rephrasing"}


def _reviewed(task: dict, ledger: dict) -> bool:
    from . import demands
    return (bool(task.get("semantic_reviewed")) and not task.get("fingerprint_migration_pending") and
            task.get("semantic_fingerprint") == demands.review_fingerprint(task, ledger))


def local_review_reason(task: dict, ledger: dict, day: date | str) -> str | None:
    from . import demands
    day = _day(day)
    fingerprint = demands.review_fingerprint(task, ledger)
    validation = task.get("validation", {})
    old = validation.get("evidence_fingerprint")
    if old and not str(old).startswith("v2:") and fingerprint.startswith("v2:"):
        return "legacy_validation_requires_local_review"
    research = task.get("research", {})
    if research.get("status") in {"paused", "resolved"} and research.get("evidence_fingerprint") != fingerprint:
        return "evidence_changed_research_decision_stale"
    if research.get("status") == "paused" and research.get("next_review") and _day(research["next_review"]) <= day:
        return "paused_review_due"
    if not _reviewed(task, ledger):
        return "semantic_review_required"
    if (task.get("status") == "值得深入验证" and validation.get("status") == "complete" and
            validation.get("evidence_fingerprint") == fingerprint and task.get("withdrawn_fingerprint") != fingerprint and
            research.get("status", "active") == "active" and
            (not research.get("next_review") or _day(research["next_review"]) <= day)):
        return "qualified_research_due"
    return None


def rank_followups(ledger: dict, day: date | str, limit: int) -> list[dict]:
    from . import demands
    day = _day(day)
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError("Invalid research limit")
    candidates = []
    for task in demands.active_tasks(ledger, day.isoformat()):
        research = task.get("research", {})
        if (task["status"] not in {"待补证", "待复核", "网页可行性待验证"} or
                not _reviewed(task, ledger) or
                research.get("status", "active") != "active" or
                research.get("next_review") and _day(research["next_review"]) > day or
                task.get("last_search", "") >= (day - timedelta(days=3)).isoformat()):
            continue
        candidates.append(task)
    def quality(task):
        review = task.get("review", {}) if _reviewed(task, ledger) else {}
        score = (40 * (review.get("firsthand") is True) + 30 * (review.get("bounded") is True) +
                 15 * (review.get("web_fit") in {"browser", "server"}) +
                 10 * any(r.get("author") for r in demands.references(task, ledger)))
        research = task.get("research", {})
        priority = research.get("priority", 0) if _reviewed(task, ledger) and research.get("evidence_fingerprint") == demands.review_fingerprint(task, ledger) else 0
        return (-priority, -score, task.get("last_search", ""), task["first_seen"], task["job"], task["id"])
    ordered = sorted(candidates, key=quality)
    if limit >= 3 and len(ordered) > limit:
        chosen = ordered[:limit - 1]
        # One bounded exploration slot protects older, uncertain seeds from starvation.
        remaining = [t for t in candidates if t not in chosen]
        oldest = min(remaining, key=lambda t: (t.get("last_search", ""), t["first_seen"], t["job"], t["id"]))
        return chosen + [oldest]
    return ordered[:limit]


def plan(ledger: dict, day: date | str, limit: int = 3, task_ids: list[str] | None = None) -> dict:
    from . import demands
    day = _day(day)
    if task_ids is not None:
        if len(set(task_ids)) != len(task_ids) or len(task_ids) > 25 or any(i not in ledger["tasks"] for i in task_ids):
            raise ValueError("Unknown or duplicate research task")
        tasks = [ledger["tasks"][i] for i in task_ids]
    else:
        # Local due decisions remain visible beyond the 60-day paid-search window.
        # Reconsidering old evidence never rewrites last_seen or authorizes a new scrape.
        local = [t for t in ledger["tasks"].values() if t.get("status") != "提取已修订" and local_review_reason(t, ledger, day)]
        local.sort(key=lambda t: (t.get("research", {}).get("next_review") or t["first_seen"], t["first_seen"], t["job"]))
        tasks = (local + [t for t in rank_followups(ledger, day, limit) if t not in local])[:limit]
    items = [{"task_id": t["id"], "job": t["job"], "evidence_fingerprint": demands.review_fingerprint(t, ledger),
              "research_version": _hash(t.get("research", {})), "semantic_review_valid": _reviewed(t, ledger),
              "local_review_required": local_review_reason(t, ledger, day),
              "priority_effective": t.get("research", {}).get("priority", 0) if _reviewed(t, ledger) and
                 t.get("research", {}).get("evidence_fingerprint") == demands.review_fingerprint(t, ledger) else 0,
              "research": copy.deepcopy(t.get("research", {})), "evidence": demands.references(t, ledger),
              "questions": ["原文与当前未解决步骤是什么", "现有替代方案是否已经满足", "哪份授权任务样例能复现问题",
                            "可接触渠道、最小实验及继续或停止条件是什么"]} for t in tasks]
    result = {"version": 1, "date": day.isoformat(), "items": items, "api_cost_usd": 0, "market_verified": False}
    result["plan_id"] = _hash(result)
    return result


def import_observations(ledger: dict, planned: dict, payload: dict, root: Path, day: date | str) -> bool:
    from . import demands
    from .opportunity_validation import check_evidence
    day = _day(day)
    if (planned.get("version") != 1 or planned.get("plan_id") != _hash({k: v for k, v in planned.items() if k != "plan_id"}) or
            payload.get("plan_id") != planned["plan_id"] or payload.get("date") != day.isoformat() or
            _day(planned["date"]) > day or not isinstance(payload.get("reviewer"), str) or not payload["reviewer"].strip()):
        raise ValueError("Research plan, date and reviewer must match")
    rows = payload.get("items", [])
    expected = {i["task_id"]: i for i in planned["items"]}
    if not isinstance(rows, list) or len(rows) != len(expected) or {r.get("task_id") for r in rows} != set(expected):
        raise ValueError("Disposition every planned task once")
    import_id = _hash(payload)
    if rows and all(import_id in ledger["tasks"][r["task_id"]].get("research", {}).get("imports", []) for r in rows):
        return False
    checked = []
    for row in rows:
        task = ledger["tasks"][row["task_id"]]
        item = expected[task["id"]]
        if (item["evidence_fingerprint"] != demands.review_fingerprint(task, ledger) or
                item["research_version"] != _hash(task.get("research", {})) or not _reviewed(task, ledger)):
            raise ValueError("Task evidence/research changed or lacks current semantic review")
        decision = row.get("decision")
        states = {"continue": "active", "pause": "paused", "stop": "resolved"}
        if decision not in states:
            raise ValueError("Use continue, pause or stop")
        value = validate_research(row.get("research"), day, task["source_ids"])
        if value["status"] != states[decision]:
            raise ValueError("Decision and research state disagree")
        records = row.get("records")
        if not isinstance(records, list) or not 1 <= len(records) <= 8:
            raise ValueError("Research decision requires 1..8 saved observations")
        checked_records = []
        for record in records:
            if record.get("kind") not in KINDS or not isinstance(record.get("note"), str) or not record["note"].strip():
                raise ValueError("Observation kind and factual note required")
            stamp = datetime.fromisoformat(record.get("observed_at", ""))
            if stamp.utcoffset() is None or not _day(planned["date"]) <= stamp.astimezone(timezone(timedelta(hours=8))).date() <= day:
                raise ValueError("Observation timestamp outside this research plan")
            origin = record.get("origin", "web")
            if origin == "local":
                if record["kind"] not in {"task_sample", "experiment"} or record.get("url") not in {None, ""}:
                    raise ValueError("Local evidence is only a task sample or experiment and cannot claim a URL")
                evidence_path = (root / record.get("evidence_file", "")).resolve()
                if (not evidence_path.is_relative_to((root / "validation-evidence").resolve()) or
                        evidence_path.suffix.lower() not in {".txt", ".md", ".csv", ".json", ".log"} or
                        not evidence_path.is_file() or evidence_path.stat().st_size > 2_000_000):
                    raise ValueError("Local evidence must be bounded text inside validation-evidence")
                text = evidence_path.read_text(encoding="utf-8-sig")
                quote = record.get("quote")
                if not isinstance(quote, str) or len(quote) < 8 or quote not in text:
                    raise ValueError("Local evidence requires an exact saved quote")
            elif origin == "web":
                check_evidence(record, root)
            else:
                raise ValueError("Evidence origin must be web or local")
            if record["kind"] == "source_context" and record["url"] not in {r["url"] for r in item["evidence"]}:
                raise ValueError("Source observation must belong to the task")
            saved = copy.deepcopy(record)
            if origin == "local":
                saved.update(url="", independent_user_evidence=False,
                             provenance_note="本地任务样例或实验记录；不是网页原文，也不增加独立客户证据。")
            saved.update(evidence_fingerprint=item["evidence_fingerprint"], import_id=import_id,
                         evidence_sha256=hashlib.sha256((root / record["evidence_file"]).read_bytes()).hexdigest(),
                         market_verified=False)
            checked_records.append(saved)
        checked.append((task, value, checked_records))
    for task, value, records in checked:
        apply_research(task, value, day, payload["reviewer"], "task-research:" + import_id)
        research = task["research"]
        research.setdefault("records", []).extend(records)
        research.setdefault("imports", []).append(import_id)
        research["evidence_fingerprint"] = demands.review_fingerprint(task, ledger)
    return True


def command(action: str, ledger_path: Path, day: date | str | None = None, observations: Path | None = None,
            plan_path: Path | None = None, limit: int = 3, task_ids: list[str] | None = None) -> int:
    from . import demands
    day = _day(day) if day else today_local()
    if day > today_local():
        raise ValueError("Research date cannot be in the future")
    if action == "plan":
        result = plan(demands.load(ledger_path), day, limit, task_ids)
        if plan_path:
            spending.save(plan_path, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if action != "import" or observations is None or plan_path is None:
        raise ValueError("Research import requires a saved plan and observations")
    lock = ledger_path.with_suffix(".audit.lock")
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        if any(ledger_path.parent.glob("web-opportunities/*/run.lock")):
            raise ValueError("Daily collection is active")
        ledger = demands.load(ledger_path)
        changed = import_observations(ledger, json.loads(plan_path.read_text(encoding="utf-8")),
                                      json.loads(observations.read_text(encoding="utf-8")), plan_path.parent, day)
        if changed:
            spending.save(ledger_path, ledger)
        print(json.dumps({"imported": changed, "market_verified": False}))
        return 0
    finally:
        lock.unlink(missing_ok=True)
