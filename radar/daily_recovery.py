"""Explicit, plan-bound collection recovery; successful requests keep their journal."""
from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import aisa, demands, demand_sources, google_trends, spending


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sampling_config(cfg):
    # Credentials are never part of a saved plan.
    return {name: copy.deepcopy(cfg.get(name, {})) for name in
            ("opportunities", "demands", "research", "competition", "google_trends")}


def save_plan(root, cfg, day, selected, followups, source):
    from .research_queries import QUERY_STRATEGY_VERSION
    from . import goanyapi
    plan = {"version": 1, "strategy_version": QUERY_STRATEGY_VERSION, "date": day.isoformat(),
            "config": sampling_config(cfg), "selected": copy.deepcopy(selected),
            "followups": copy.deepcopy(followups), "discovery": copy.deepcopy(source),
            "external_provider": {"goany_enabled": bool(goanyapi.key()),
                                  "endpoint": spending._digest("goany-endpoint", {"base": goanyapi.BASE})}}
    plan["plan_id"] = spending._digest("collection-plan", plan)
    path = root / "collection-plan.json"
    if path.exists() and read(path) != plan:
        raise ValueError("Collection plan is immutable; do not silently change a paid scope")
    spending.save(path, plan)
    return plan


def record_status(root, result, origin=None):
    path = root / "run-receipt.json"
    old = read(path) if path.exists() else {}
    value = {"date": result["date"], "origin": old.get("origin") or origin or "unknown",
             "last_execution_origin": origin or old.get("last_execution_origin", "unknown"),
             "updated_at": datetime.now(timezone.utc).isoformat(), "status": result["status"],
             "semantic_audit": result.get("semantic_audit", {}).get("status", "not_started"),
             "validation": result.get("opportunity_validation", {}).get("status", "not_started"),
             "sources": len(result.get("evidence", [])), "opportunities": len(result.get("opportunities", [])),
             "errors": result.get("errors", []), "recovery_count": len(result.get("recovery_history", []))}
    spending.save(path, value)


def record_attempts(ledger_path, collection, day, round_id=None):
    """A search attempt is not a confirmed independent source or a closed gap."""
    ids = collection.get("attempted_followups", collection.get("successful_followups", []))
    if not ids:
        return
    ledger = demands.load(ledger_path)
    for tid in dict.fromkeys(ids):
        task = ledger["tasks"].get(tid)
        if not task:
            continue
        rows = [r for r in collection.get("queries", []) if r.get("task_id") == tid]
        history = task.setdefault("search_history", [])
        # One frozen plan/task is one research round across providers and resumes.
        key = spending._digest("search-attempt", {"date": day.isoformat(), "plan": round_id or day.isoformat(), "task": tid})
        event = next((r for r in history if r.get("id") == key), None)
        if event is None:
            event = {"id": key, "date": day.isoformat(), "queries": [], "query_events": [], "outcome": "no_confirmed_evidence"}
            history.append(event)
            task["search_attempts"] = task.get("search_attempts", 0) + 1
        for row in rows:
            snapshot = copy.deepcopy(row)
            if snapshot not in event["query_events"]:
                event["query_events"].append(snapshot)
            identity = lambda r: (r.get("source"), r.get("query"), r.get("task_id"))
            prior = next((i for i, r in enumerate(event["queries"]) if identity(r) == identity(row)), None)
            if prior is None:
                event["queries"].append(snapshot)
            else:
                event["queries"][prior] = snapshot
        if tid in collection.get("evidence_followups", []):
            event["outcome"] = "candidates_unreviewed"
        task["last_search"] = day.isoformat()
    spending.save(ledger_path, ledger)


def merge_evidence(previous, incoming):
    rows = copy.deepcopy(previous)
    indices = {(r["url"], r["text"]): i for i, r in enumerate(rows)}
    for row in incoming:
        key = (row["url"], row["text"])
        if key in indices:
            i = indices[key]
            rows[i] = {**rows[i], **row, "id": rows[i]["id"]}
        else:
            indices[key] = len(rows)
            rows.append({**row, "id": f"e{len(rows) + 1}"})
    return rows


def checkpoint_audit(result, evidence, ledger_path, day):
    """Persist an auditable source scope before optional model preprocessing."""
    from .demand_audit import due_tasks
    current = demands.checkpoint_sources(evidence, ledger_path, day)
    ledger = demands.load(ledger_path)
    ids = list(dict.fromkeys(current + [r['id'] for r in result.get('dispositions', [])
                                      if r['id'] in ledger['sources']]))
    dispositions = []
    for sid in ids:
        entry = ledger['sources'][sid]
        source = entry['source']
        dispositions.append({'id': sid, 'original_id': source.get('original_id'), 'url': source['url'],
            'category': entry.get('assessment', {}).get('category', 'analysis_pending'),
            'reason': entry.get('assessment', {}).get('reason', '等待正式来源审核'),
            'disagreement': entry.get('disagreement', False), 'analysis_error': entry.get('analysis_error', '')})
    tasks = [{**t, 'evidence': demands.references(t, ledger)} for t in demands.active_tasks(ledger, day.isoformat())]
    error = '模型预处理未完成；已保存全部来源，可继续本地语义审核'
    result.update(workflow_version=2, evidence=evidence, dispositions=dispositions, tasks=tasks,
        opportunities=[], status='incomplete', analysis_errors=[error],
        semantic_audit={'status': 'pending', 'reason': 'Source checkpoint retained before model preprocessing'},
        zero_result_audit={'status': 'incomplete', 'task_ids': [t['id'] for t in tasks]},
        coverage={'sources': len(ids), 'current_sources': len(current), 'historical_repair_sources': len(set(ids)-set(current)),
                  'classified': sum(d['category'] != 'analysis_pending' for d in dispositions),
                  'audit_complete': sum(bool(ledger['sources'][sid].get('audit')) for sid in ids),
                  'unresolved_sources': sum(not ledger['sources'][sid].get('assessment') or bool(ledger['sources'][sid].get('analysis_error')) for sid in ids),
                  'active_tasks': len(tasks), 'deferred_tasks': len(due_tasks(ledger, day.isoformat()))})
    return error


def resume_daily(cfg, state: Path, output: Path, day, origin="manual"):
    from . import demand_research, goanyapi
    from .research_queries import QUERY_STRATEGY_VERSION
    root = state / "web-opportunities" / day.isoformat()
    final, plan_path = root / "result.json", root / "collection-plan.json"
    ledger_path = state / "demand-ledger.json"
    if any(p.exists() for p in (root / "run.lock", root / "trends-edit.lock", root / "api/requests.lock",
                               root / "external-api/requests.lock", ledger_path.with_suffix(".audit.lock"))):
        raise aisa.AisaError("Recovery is active or interrupted; inspect locks without deleting them")
    if not final.exists():
        raise ValueError("Recovery requires the original result; do not reset old journals")
    result = read(final)
    if result.get("status") == "complete" or (not result.get("collection_errors") and result.get("workflow_version") == 2):
        # Analysis errors are handled by local semantic audit, never another paid run.
        from .opportunities import render
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render(result), encoding="utf-8")
        spending.save(output.with_suffix('.json'), result)
        return 0 if result.get("status") == "complete" else 2
    if not plan_path.exists():
        raise ValueError("Recovery requires the saved collection plan; do not reconstruct or reset old journals")
    plan = read(plan_path)
    if (plan.get("version") != 1 or plan.get("strategy_version") != QUERY_STRATEGY_VERSION or
            plan.get("plan_id") != spending._digest("collection-plan", {k: v for k, v in plan.items() if k != "plan_id"}) or
            plan.get("date") != day.isoformat() or result.get("date") != day.isoformat() or
            plan.get("config") != sampling_config(cfg)):
        raise ValueError("Recovery scope or strategy changed; saved plan cannot be replaced by new queries")
    if not cfg.get("demands", {}).get("enabled", True):
        raise ValueError("Plan recovery requires the task-led workflow")
    external = {"goany_enabled": bool(goanyapi.key()), "endpoint": spending._digest("goany-endpoint", {"base": goanyapi.BASE})}
    if plan.get("external_provider") != external:
        raise ValueError("Optional provider availability or endpoint changed; recovery cannot expand the original paid scope")
    entries = read(root / "api/requests.json") if (root / "api/requests.json").exists() else []
    if any(e.get("global_stop", e.get("status") not in {"ok"}) for e in entries):
        raise aisa.AisaError("Unknown or account-level billing outcome blocks paid recovery; reconcile it first")
    number = len(result.get("recovery_history", [])) + 1
    backup = root / "recoveries" / f"{number:04d}"
    backup.mkdir(parents=True, exist_ok=False)
    for path in (final, root / "result.md", root / "evidence.json", ledger_path, root / "api/requests.json", plan_path):
        if path.exists():
            shutil.copy2(path, backup / path.name)
    lock = root / "run.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(datetime.now(timezone.utc).isoformat())
    before, journal, external_cache, analysis_pending = None, None, None, False
    event = {"number": number, "origin": origin, "started_at": datetime.now(timezone.utc).isoformat(),
             "status": "running", "plan": "collection-plan.json", "previous_errors": result.get("errors", [])}
    result.setdefault("recovery_history", []).append(event)
    spending.save(final, result)
    try:
        before = spending.account()
        if before["balance_usd"] <= .01:
            raise aisa.AisaError("Insufficient balance for recovery")
        prices = spending.model_prices(cfg["opportunities"]["model"])
        journal = spending.Journal(root / "api", cfg["opportunities"]["budget_usd"], prices, before["balance_usd"])
        from .external_cache import RequestCache
        external_cache = RequestCache(root / "external-api", allow_new=False)
        with aisa.managed_requests(journal), goanyapi.managed_requests(external_cache):
            base, errors, collection = demand_sources.collect(plan["selected"], day,
                cfg["opportunities"]["problem_queries_per_day"], plan["followups"],
                cfg["demands"].get("free_sources", True), cfg.get("research", {}))
            result["collection"] = collection
            extra = []
            if cfg.get("research", {}).get("enabled"):
                extra, failed, coverage = demand_research.collect(cfg, day, plan["followups"], plan["selected"], state)
                result["research_collection"] = coverage
                errors += failed
                record_attempts(ledger_path, coverage, day, plan["plan_id"])
            record_attempts(ledger_path, collection, day, plan["plan_id"])
            evidence = merge_evidence(result.get("evidence", []), base + extra)
            result["evidence"] = evidence
            result["collection_errors"] = errors
            spending.save(root / "evidence.json", evidence)
            checkpoint_audit(result, evidence, ledger_path, day)
            result['errors'] = result.get('discovery_errors', []) + errors + result['analysis_errors']
            spending.save(final, result)
            analysis_pending = True
            assessment = demands.process(cfg["opportunities"]["model"], evidence, ledger_path, day)
            analysis_pending = False
            result.update(assessment)
        result["errors"] = result.get("discovery_errors", []) + errors + assessment.get("analysis_errors", [])
        result["semantic_audit"] = {"status": "pending", "reason": "恢复采集后需核对全部来源及有效任务"}
        result["status"] = "incomplete" if result["errors"] else "awaiting_audit"
        previous_market = result.get("market_research", {})
        if previous_market.get("status") in {"running", "complete", "partial", "incomplete"}:
            result["market_research"] = {**previous_market, "status": "incomplete", "items": [],
                "previous_stage": copy.deepcopy(previous_market),
                "previous_opportunities": read(backup / 'result.json').get('opportunities', []),
                "requires_local_review": True, "automatic_retry_allowed": False,
                "reason": "恢复后原付费研究需本地复核；保留历史与费用，禁止自动重新执行"}
        else:
            result.pop("market_research", None)
        for key in ("opportunity_validation", "google_trends"):
            if key in result:
                event.setdefault("previous_stages", {})[key] = result.pop(key)
        event["status"] = "collected"
    except Exception as exc:
        # No raw transport message/headers in resumable metadata.
        error = "Recovery failed: " + type(exc).__name__
        result.setdefault("errors", []).append(error)
        if analysis_pending:
            result.setdefault('analysis_errors', []).append(error)
        result["status"] = "incomplete"
        event.update(status="incomplete", error_type=type(exc).__name__)
    finally:
        if journal:
            result["reserved_usd"] = journal.reserved
        if external_cache:
            result["external_collection_billing"] = external_cache.billing_summary()
        if before:
            event["billing"] = {"before": before}
            try:
                after = spending.account()
                event["billing"].update(after=after, charged_usd=round(before["balance_usd"] - after["balance_usd"], 6))
            except Exception:
                result.setdefault("errors", []).append("Recovery balance verification unavailable")
                result["status"] = "incomplete"
        from . import opportunity_validation
        opportunity_validation.prepare(result)
        google_trends.prepare(result, cfg)
        spending.save(backup / "recovery.json", event)
        google_trends.write_result(final, result, output)
        record_status(root, result, origin)
        lock.unlink(missing_ok=True)
    return 0 if result["status"] == "complete" else 2
