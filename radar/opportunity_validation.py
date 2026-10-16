"""Bounded, evidence-backed browser checks before spending attention on Trends.

No network here. The scheduled agent supplies saved public source observations;
the importer verifies provenance/coverage, not the truth of semantic judgments.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

from . import spending


def prepare(result: dict, ledger: dict | None = None, rebuild: bool = False) -> None:
    if "opportunity_validation" in result:
        old_stage = result["opportunity_validation"]
        stale = False
        if rebuild and ledger is not None:
            from . import demands
            for item in old_stage.get("items", []):
                index = item.get("opportunity_index")
                if type(index) is not int or not 0 <= index < len(result.get("opportunities", [])):
                    stale = True
                    break
                task = ledger["tasks"].get(result["opportunities"][index].get("task_id"))
                if task and item.get("evidence_fingerprint") != demands.review_fingerprint(task, ledger):
                    stale = True
                    break
        if not stale:
            return
        result.setdefault("opportunity_validation_history", []).append(copy.deepcopy(old_stage))
        result.pop("opportunity_validation")
    stage = {"status": "skipped", "geo": "US", "items": [], "api_cost_usd": 0}
    result["opportunity_validation"] = stage
    if result.get("status") != "complete":
        stage["reason"] = "主流程尚未完成"
        return
    for i, opportunity in enumerate(result.get("opportunities", [])[:3]):
        terms = opportunity.get("keywords", [])
        keyword = next((k.strip() for k in terms if isinstance(k, str) and 1 <= len(k.strip()) <= 80), "")
        if not keyword:
            continue
        stage["items"].append({"id": f"v{i+1}", "opportunity_index": i, "status": "pending",
            "keyword": keyword, "source_urls": list(dict.fromkeys(e["url"] for e in opportunity["evidence"])),
            "search_url": "https://www.google.com/search?" + urlencode({"q": keyword, "hl": "en", "gl": "us", "pws": "0"}),
            "questions": ["原文上下文是否支持同一任务及至少两个独立求助账号",
                          "美国英文搜索结果是否对应这个任务，而非同名产品或教程资讯",
                          "两种现有替代方案的价格、限制及未覆盖步骤是什么",
                          "哪一个可接触用户渠道、最小任务实验与停止条件值得下一步验证"]})
    if ledger is not None:
        from . import demands
        for item in stage["items"]:
            opportunity = result["opportunities"][item["opportunity_index"]]
            task = ledger["tasks"].get(opportunity.get("task_id"), {})
            saved = task.get("validation", {})
            if task:
                item["evidence_fingerprint"] = demands.review_fingerprint(task, ledger)
            if task and saved.get("evidence_fingerprint") == demands.review_fingerprint(task, ledger) and saved.get("item"):
                old = copy.deepcopy(saved["item"])
                item.update({k: v for k, v in old.items() if k not in {"id", "opportunity_index", "source_urls"}})
                item["evidence_fingerprint"] = saved["evidence_fingerprint"]
                item["retained_root"] = str(Path(saved["result_path"]).parent) if saved.get("result_path") else ""
                if saved.get("status") == "withdrawn":
                    opportunity["review_status"] = "withdrawn"
    for item in stage["items"]:
        item["missing_checks"] = missing_checks(item)
    stage["status"] = "pending" if stage["items"] else "skipped"
    stage.setdefault("reason", "仅核验入选机会；没有机会不查询")


def check_evidence(observation: dict, root: Path, expected_url: str | None = None) -> dict:
    url = observation.get("url", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or (expected_url and url != expected_url):
        raise ValueError("Observation URL does not match its source")
    path = (root / observation.get("evidence_file", "")).resolve()
    if not path.is_relative_to((root / "validation-evidence").resolve()) or path.suffix not in {".txt", ".md"}:
        raise ValueError("Use saved text under validation-evidence")
    if path.stat().st_size > 2_000_000:
        raise ValueError("Evidence file too large")
    text = path.read_text(encoding="utf-8-sig")
    quote = observation.get("quote", "")
    if not isinstance(quote, str) or len(quote) < 8 or quote not in text or url not in text:
        raise ValueError("Saved observation needs source URL and exact quote")
    return observation


def today_local():
    return datetime.now(timezone(timedelta(hours=8))).date()


def missing_checks(item: dict) -> dict:
    observed = item.get("observation", {})
    held = {r["url"] for r in observed.get("sources", [])}
    return {"source_urls": [url for url in item["source_urls"] if url not in held],
            "search": not observed.get("search") or observed.get("intent_match") is False,
            "alternative_slots": max(0, 2 - len(observed.get("alternatives", []))),
            "source_verdict": any(type(observed.get(k)) is not bool for k in ("same_task", "independent_requests"))}


def _item_state(item: dict) -> str:
    if item.get("validation_status"):
        return item["validation_status"]
    observed = item.get("observation", {})
    if observed.get("sources") and (observed.get("same_task") is False or observed.get("independent_requests") is False):
        return "withdrawn"
    if observed.get("intent_match") is False:
        return "needs_rephrasing"
    if item.get("trends_ready"):
        return "complete"
    return "blocked" if item.get("status") in {"blocked", "unavailable"} else "pending"


def resume(result: dict, item_id: str | None = None, keyword: str | None = None, day=None) -> bool:
    stage = result["opportunity_validation"]
    if stage["status"] == "running":
        return False  # An existing claim is resumed from saved observations, not claimed twice.
    day = day or today_local()
    if not datetime.fromisoformat(result["date"]).date() <= day <= today_local():
        raise ValueError("Resume date must be between report date and today")
    targets = [r for r in stage["items"] if item_id is None or r["id"] == item_id]
    if item_id is not None and not targets:
        raise ValueError("Unknown validation item")
    if keyword is not None and (len(targets) != 1 or not isinstance(keyword, str) or
            not 1 <= len(keyword.strip()) <= 80 or re.search(r"[\n\r,<>]", keyword)):
        raise ValueError("Rephrasing requires one item and a valid keyword")
    eligible = []
    for item in targets:
        state = _item_state(item)
        if result["opportunities"][item["opportunity_index"]].get("review_status") == "withdrawn" or state == "withdrawn":
            if item_id:
                raise ValueError("Withdrawn evidence cannot be resumed without new evidence review")
            continue
        if state not in {"blocked", "needs_rephrasing"}:
            continue
        if state == "needs_rephrasing" and (keyword is None or keyword.strip() == item["keyword"]):
            raise ValueError("Provide an explicit new keyword for the failed search intent")
        if keyword is not None and state != "needs_rephrasing":
            raise ValueError("Only a needs_rephrasing item can change its keyword")
        eligible.append(item)
    for item in eligible:
        if item.get("observation") and not item.get("attempts"):
            # Retain pre-upgrade observations before removing a failed search term.
            item["attempts"] = [{"legacy_observation": True, "keyword": item["keyword"],
                                 "observation": copy.deepcopy(item["observation"]),
                                 "validation_status": _item_state(item)}]
        if keyword is not None:
            item["keyword"] = keyword.strip()
            item["search_url"] = "https://www.google.com/search?" + urlencode({"q": keyword.strip(), "hl": "en", "gl": "us", "pws": "0"})
            item.setdefault("observation", {}).pop("search", None)
            item["observation"].pop("intent_match", None)
            opportunity = result["opportunities"][item["opportunity_index"]]
            opportunity["keywords"] = [keyword.strip()] + [k for k in opportunity.get("keywords", []) if k != keyword.strip()]
        item["missing_checks"] = missing_checks(item)
        item["validation_status"] = "pending"
    if eligible:
        stage.update(status="running", active_ids=[r["id"] for r in eligible], plan_date=day.isoformat())
    return bool(eligible)


def _observation(record: dict, root: Path, stamp: str, expected_url=None, retained=False) -> dict:
    if not isinstance(record, dict):
        raise ValueError("Malformed saved observation")
    observed_root = Path(record.get("evidence_root") or root) if retained else root
    check_evidence(record, observed_root, expected_url)
    digest = hashlib.sha256((observed_root / record["evidence_file"]).read_bytes()).hexdigest()
    if retained and record.get("evidence_sha256") and digest != record["evidence_sha256"]:
        raise ValueError("Previously observed evidence changed; do not silently reuse it")
    return {**record, "evidence_root": str(observed_root.resolve()), "evidence_sha256": digest,
            "observed_at": record.get("observed_at", stamp) if retained else stamp}


def import_observations(result: dict, payload: dict, root: Path) -> None:
    stage = result["opportunity_validation"]
    import_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if import_id in stage.get("applied_imports", []):
        return
    rows = payload.get("items", [])
    active_ids = set(stage.get("active_ids", [r["id"] for r in stage["items"]]))
    planned = {item["id"]: item for item in stage["items"] if item["id"] in active_ids}
    plan_date = stage.get("plan_date", result["date"])
    if payload.get("date") != plan_date or len(rows) != len(planned) or {r.get("id") for r in rows} != set(planned):
        raise ValueError("Record every pending check and the claimed observation date")
    checked = {}
    changes = []
    for row in rows:
        item = copy.deepcopy(planned[row["id"]])
        if result["opportunities"][item["opportunity_index"]].get("review_status") == "withdrawn":
            raise ValueError("Withdrawn evidence cannot be validated again")
        status = row.get("status")
        stamp = datetime.fromisoformat(row.get("observed_at", ""))
        if stamp.utcoffset() is None or stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat() != plan_date:
            raise ValueError("Observation timestamp must match the claimed date")
        if status not in {"observed", "blocked", "unavailable"} or not isinstance(row.get("note"), str) or not row["note"].strip():
            raise ValueError("Need a factual status and note")
        old = item.get("observation", {})
        merged = {**old, **{k: row[k] for k in ("id", "status", "observed_at", "note", "same_task", "independent_requests", "intent_match") if k in row}}
        prior_root = Path(item.get("retained_root") or root)
        for field in ("sources", "alternatives"):
            held = [_observation(r, prior_root, old.get("observed_at", row["observed_at"]), retained=True) for r in old.get(field, [])]
            by_url = {r["url"]: r for r in held}
            for record in row.get(field, []):
                if record.get("url") in by_url:
                    raise ValueError("Already observed page is retained; submit only missing observations")
                value = _observation(record, root, row["observed_at"])
                if field == "sources" and value["url"] not in item["source_urls"]:
                    raise ValueError("Source does not belong to this opportunity")
                if field == "alternatives" and not all(isinstance(value.get(k), str) and value[k].strip() for k in ("price", "limitation")):
                    raise ValueError("Alternative price/limitation required, or unknown")
                by_url[value["url"]] = value
            merged[field] = list(by_url.values())
        if len(merged["alternatives"]) > 2:
            raise ValueError("Only inspect two alternatives per opportunity")
        if old.get("search") and row.get("search"):
            raise ValueError("Already observed search is retained; do not revisit it")
        if old.get("search"):
            merged["search"] = _observation(old["search"], prior_root, old.get("observed_at", row["observed_at"]), item["search_url"], True)
        elif row.get("search"):
            merged["search"] = _observation(row["search"], root, row["observed_at"], item["search_url"])
        for key in ("same_task", "independent_requests", "intent_match"):
            if key in merged and type(merged[key]) is not bool:
                raise ValueError("Validation verdicts must be booleans")
        source_complete = len({s["url"] for s in merged["sources"]}) >= 2
        if status == "observed" and (not source_complete or not merged.get("search")):
            raise ValueError("Observed result needs two source URLs and saved search evidence")
        source_rejected = bool(merged["sources"]) and (merged.get("same_task") is False or merged.get("independent_requests") is False)
        rephrase = bool(merged.get("search")) and merged.get("intent_match") is False
        passed = source_complete and bool(merged.get("search")) and all(merged.get(k) is True for k in ("same_task", "independent_requests", "intent_match"))
        validation_status = "withdrawn" if source_rejected else "needs_rephrasing" if rephrase else "complete" if passed else "blocked"
        item.update(observation=merged, status="observed" if passed else status, trends_ready=passed and not source_rejected,
                    validation_status=validation_status, market_verified=False)
        receipt = {"import_id": import_id, "plan_date": plan_date, "keyword": item["keyword"], "observation": copy.deepcopy(merged), "validation_status": validation_status}
        item.setdefault("attempts", []).append(receipt)
        item["missing_checks"] = missing_checks(item)
        checked[item["id"]] = item
        changes.append((item["opportunity_index"], validation_status, merged["note"]))
    stage["items"] = [checked.get(item["id"], item) for item in stage["items"]]
    stage["status"] = "complete" if all(_item_state(r) in {"complete", "withdrawn"} for r in stage["items"]) else "partial"
    stage.pop("active_ids", None)
    stage.setdefault("applied_imports", []).append(import_id)
    for index, status, note in changes:
        opportunity = result["opportunities"][index]
        if status == "withdrawn":
            opportunity.update(review_status="withdrawn", withdrawal_reason="原文上下文或独立用户证据复核未通过：" + note)
        elif status == "needs_rephrasing":
            opportunity["search_validation"] = {"status": "needs_rephrasing", "geo": "US", "reason": note}
        elif status == "complete":
            opportunity["search_validation"] = {"status": "complete", "geo": "US"}


def command(action: str, path: Path, payload_path: Path | None = None, output: Path | None = None,
            ledger_path: Path | None = None, item_id: str | None = None, keyword: str | None = None) -> int:
    from . import demands, google_trends
    if (path.parent / "run.lock").exists():
        raise ValueError("Daily run still active; do not change its result")
    locks = []
    try:
        for lock in ([ledger_path.with_suffix(".audit.lock")] if ledger_path else []) + [path.parent / "trends-edit.lock"]:
            with lock.open("x", encoding="utf-8"):
                pass
            locks.append(lock)
        if (path.parent / "run.lock").exists():
            raise ValueError("Daily run still active")
        ledger = demands.load(ledger_path) if ledger_path and ledger_path.exists() else None
        result = json.loads(path.read_text(encoding="utf-8"))
        if action in {"claim", "resume", "import"} and datetime.fromisoformat(result["date"]).date() > today_local():
            raise ValueError("Cannot validate a future report")
        prepare(result, ledger)
        stage = result["opportunity_validation"]
        if ledger is not None and action in {"claim", "resume", "import"}:
            for item in stage["items"]:
                task = ledger["tasks"].get(result["opportunities"][item["opportunity_index"]].get("task_id"))
                if task is None:
                    continue
                fingerprint = demands.review_fingerprint(task, ledger)
                frozen = item.get("evidence_fingerprint") or task.get("validation", {}).get("evidence_fingerprint")
                if frozen != fingerprint:
                    raise ValueError("Validation plan evidence version changed; rebuild through formal semantic audit")
                item["evidence_fingerprint"] = frozen
        claimed = False
        if action == "claim" and stage["status"] == "pending":
            ids = [r["id"] for r in stage["items"] if _item_state(r) in {"pending", "blocked"}]
            if ids:
                for item in stage["items"]:
                    if item["id"] in ids:
                        item["validation_status"] = "pending"
                stage.update(status="running", active_ids=ids, plan_date=today_local().isoformat())
                claimed = True
        elif action == "resume":
            claimed = resume(result, item_id, keyword)
        elif action == "import":
            if payload_path is None:
                raise ValueError("Import requires a claimed plan and saved observations")
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            import_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if import_id in stage.get("applied_imports", []):
                print(json.dumps({"imported": False, "reused": True, "status": stage["status"]}))
                return 0
            if stage["status"] != "running":
                raise ValueError("Import requires a claimed plan and saved observations")
            import_observations(result, payload, path.parent)
        elif action not in {"prepare", "claim"}:
            raise ValueError("Unknown validation action")
        if ledger is not None and action in {"claim", "resume", "import"}:
            for item in stage["items"]:
                opportunity = result["opportunities"][item["opportunity_index"]]
                task = ledger["tasks"].get(opportunity.get("task_id"))
                if not task:
                    continue
                fingerprint = demands.review_fingerprint(task, ledger)
                prior = task.get("validation", {})
                if action in {"claim", "resume", "import"} and (not task.get("semantic_reviewed") or
                        task.get("semantic_fingerprint") != fingerprint or task.get("fingerprint_migration_pending")):
                    raise ValueError("Validation requires current formal semantic review")
                if prior and prior.get("evidence_fingerprint") != fingerprint:
                    task.setdefault("validation_history", []).append(copy.deepcopy(prior))
                task["validation"] = {"status": _item_state(item), "evidence_fingerprint": fingerprint,
                                      "result_path": str(path.resolve()), "item": copy.deepcopy(item)}
                if opportunity.get("review_status") == "withdrawn":
                    task.update(status="待补证", reason=opportunity["withdrawal_reason"], withdrawn_fingerprint=fingerprint)
                if keyword is not None and task.get("proposal"):
                    task["proposal"]["keywords"] = list(opportunity["keywords"])
            spending.save(ledger_path, ledger)
            result["tasks"] = [{**t, "evidence": demands.references(t, ledger)} for t in demands.active_tasks(ledger, result["date"])]
        if action == "import" and result.get("google_trends", {}).get("status") in {"waiting_validation", "partial", "skipped"}:
            # Never discard already observed Trends pages, nor reset paid research.
            old_trends = result["google_trends"]
            if not any(r.get("status") in {"observed", "no_data"} for r in old_trends.get("items", [])):
                result.pop("google_trends")
                google_trends.prepare(result, {"google_trends": {"enabled": True, "geo": "US"}})
        google_trends.write_result(path, result, output)
        from .daily_recovery import record_status
        record_status(path.parent, result)
        print(json.dumps({"claimed": claimed, **stage}, ensure_ascii=False))
        return 0
    finally:
        for lock in reversed(locks):
            lock.unlink(missing_ok=True)
