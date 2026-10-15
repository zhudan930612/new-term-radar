"""Bounded, evidence-backed browser checks before spending attention on Trends.

No network here. The scheduled agent supplies saved public source observations;
the importer verifies provenance/coverage, not the truth of semantic judgments.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

from . import spending


def prepare(result: dict) -> None:
    if "opportunity_validation" in result:
        return
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


def import_observations(result: dict, payload: dict, root: Path) -> None:
    stage = result["opportunity_validation"]
    rows = payload.get("items", [])
    planned = {item["id"]: item for item in stage["items"]}
    if payload.get("date") != result["date"] or len(rows) != len(planned) or {r.get("id") for r in rows} != set(planned):
        raise ValueError("Record all planned checks and the correct date")
    checked = []
    for row in rows:
        item = planned[row["id"]]
        status = row.get("status")
        stamp = datetime.fromisoformat(row.get("observed_at", ""))
        if stamp.utcoffset() is None or stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat() != result["date"]:
            raise ValueError("Observation timestamp must match the report date")
        if status not in {"observed", "blocked", "unavailable"} or not isinstance(row.get("note"), str) or not row["note"].strip():
            raise ValueError("Need a factual status and note")
        passed = False
        if status == "observed":
            sources = row.get("sources", [])
            if len({s.get("url") for s in sources}) < 2 or any(s.get("url") not in item["source_urls"] for s in sources):
                raise ValueError("Check at least two original evidence URLs")
            for source in sources:
                check_evidence(source, root)
            check_evidence(row.get("search", {}), root, item["search_url"])
            competitors = row.get("alternatives", [])
            if len(competitors) > 2:
                raise ValueError("Only inspect two alternatives per opportunity")
            for alternative in competitors:
                check_evidence(alternative, root)
                if not all(isinstance(alternative.get(k), str) and alternative[k].strip() for k in ("price", "limitation")):
                    raise ValueError("Alternative price/limitation must be explicit, or unknown")
            # A search-intent result is not proof of market size or monetization.
            passed = row.get("same_task") is True and row.get("independent_requests") is True and row.get("intent_match") is True
        checked.append({**item, "observation": row, "status": status, "trends_ready": passed,
                        "market_verified": False})
        opportunity = result["opportunities"][item["opportunity_index"]]
        if status == "observed" and (row.get("same_task") is False or row.get("independent_requests") is False):
            opportunity.update(review_status="withdrawn", withdrawal_reason="原文上下文或独立用户证据复核未通过：" + row["note"])
        elif status == "observed" and row.get("intent_match") is False:
            opportunity["search_validation"] = {"status": "needs_rephrasing", "geo": "US", "reason": row["note"]}
    stage.update(items=checked, status="complete" if all(r["status"] == "observed" for r in checked) else "partial")


def command(action: str, path: Path, payload_path: Path | None = None, output: Path | None = None,
            ledger_path: Path | None = None) -> int:
    from . import google_trends
    if (path.parent / "run.lock").exists():
        raise ValueError("Daily run still active; do not change its result")
    if ledger_path and ledger_path.with_suffix(".audit.lock").exists():
        raise ValueError("Demand ledger audit is active")
    lock = path.parent / "trends-edit.lock"  # shared with Trends to avoid lost updates
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        prepare(result)
        stage = result["opportunity_validation"]
        claimed = False
        if action == "claim" and stage["status"] == "pending":
            stage["status"], claimed = "running", True
        elif action == "import":
            if stage["status"] != "running" or payload_path is None:
                raise ValueError("Import requires a claimed plan and saved observations")
            import_observations(result, json.loads(payload_path.read_text(encoding="utf-8")), path.parent)
            if ledger_path and ledger_path.exists():
                from . import demands
                ledger = demands.load(ledger_path)
                for opportunity in result["opportunities"]:
                    if opportunity.get("review_status") == "withdrawn" and opportunity.get("task_id") in ledger["tasks"]:
                        task = ledger["tasks"][opportunity["task_id"]]
                        task.update(status="待补证", reason=opportunity["withdrawal_reason"],
                                    withdrawn_fingerprint=demands.review_fingerprint(task, ledger))
                spending.save(ledger_path, ledger)
                result["tasks"] = [{**t, "evidence": demands.references(t, ledger)} for t in demands.active_tasks(ledger, result["date"])]
            if result.get("google_trends", {}).get("status") == "waiting_validation":
                result.pop("google_trends")
                google_trends.prepare(result, {"google_trends": {"enabled": True, "geo": "US"}})
        google_trends.write_result(path, result, output)
        print(json.dumps({"claimed": claimed, **stage}, ensure_ascii=False))
        return 0
    finally:
        lock.unlink(missing_ok=True)
