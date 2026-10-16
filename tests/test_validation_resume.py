import copy
import json
from datetime import date

import pytest

from radar import demands, opportunity_validation as validation, spending
from test_opportunity_validation import sample, observations
from test_task_research import reviewed


def failure(result, day="2026-10-15"):
    return {"date": day, "items": [{"id": "v1", "status": "blocked", "observed_at": day + "T10:00:00+08:00", "note": "Browser blocked"}]}


def test_partial_source_checks_resume_only_missing_pages_and_keep_immutable_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    result = sample()
    validation.prepare(result)
    complete = observations(tmp_path, result)
    first = failure(result)
    first["items"][0].update(sources=complete["items"][0]["sources"], same_task=True, independent_requests=True)
    validation.import_observations(result, first, tmp_path)
    original = copy.deepcopy(result["opportunity_validation"]["items"][0]["attempts"])
    assert validation.resume(result)
    item = result["opportunity_validation"]["items"][0]
    assert item["missing_checks"]["source_urls"] == []
    assert item["missing_checks"]["search"] is True
    next_result = {"date": "2026-10-16", "items": [{"id": "v1", "status": "observed", "observed_at": "2026-10-16T10:00:00+08:00",
                   "note": "Search now readable", "search": complete["items"][0]["search"], "intent_match": True}]}
    validation.import_observations(result, next_result, tmp_path)
    final = result["opportunity_validation"]["items"][0]
    assert final["trends_ready"] is True and final["validation_status"] == "complete"
    assert final["attempts"][:1] == original
    assert final["observation"]["sources"][0]["observed_at"].startswith("2026-10-15")
    assert len(final["attempts"]) == 2


def test_rephrase_requires_new_keyword_and_keeps_original_sources_and_paid_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    result = sample()
    validation.prepare(result)
    payload = observations(tmp_path, result)
    payload["items"][0]["intent_match"] = False
    validation.import_observations(result, payload, tmp_path)
    result["market_research"] = {"status": "partial", "items": [{"keyword": "split invoices"}]}
    paid = copy.deepcopy(result["market_research"])
    with pytest.raises(ValueError, match="new keyword"):
        validation.resume(result, "v1")
    with pytest.raises(ValueError, match="new keyword"):
        validation.resume(result, "v1", "split invoices")
    assert validation.resume(result, "v1", "split scanned invoice pages")
    item = result["opportunity_validation"]["items"][0]
    assert item["missing_checks"]["source_urls"] == [] and not item["observation"].get("search")
    assert item["attempts"][0]["observation"]["search"]["url"] != item["search_url"]
    path = tmp_path / "validation-evidence" / "new-search.txt"
    quote = "A new search result about separating invoice pages"
    path.write_text(item["search_url"] + "\n" + quote, encoding="utf-8")
    validation.import_observations(result, {"date": "2026-10-16", "items": [{"id": "v1", "status": "observed",
        "observed_at": "2026-10-16T11:00:00+08:00", "note": "Intent now matches", "intent_match": True,
        "search": {"url": item["search_url"], "evidence_file": "validation-evidence/new-search.txt", "quote": quote}}]}, tmp_path)
    assert result["opportunity_validation"]["items"][0]["trends_ready"]
    assert result["market_research"] == paid
    assert not validation.resume(result, "v1")


def test_withdrawn_evidence_cannot_be_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    result = sample()
    validation.prepare(result)
    payload = observations(tmp_path, result)
    payload["items"][0]["independent_requests"] = False
    validation.import_observations(result, payload, tmp_path)
    with pytest.raises(ValueError, match="Withdrawn"):
        validation.resume(result, "v1")


def test_resuming_one_item_does_not_reopen_successful_neighbor(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    result = sample()
    result["opportunities"].append(copy.deepcopy(result["opportunities"][0]))
    validation.prepare(result)
    payload = observations(tmp_path, result)
    payload["items"].append({"id": "v2", "status": "blocked", "observed_at": "2026-10-15T12:00:00+08:00", "note": "blocked"})
    validation.import_observations(result, payload, tmp_path)
    neighbor = copy.deepcopy(result["opportunity_validation"]["items"][0])
    assert validation.resume(result)
    assert result["opportunity_validation"]["active_ids"] == ["v2"]
    assert result["opportunity_validation"]["items"][0] == neighbor


def test_modified_successful_evidence_is_not_silently_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    result = sample()
    validation.prepare(result)
    original = observations(tmp_path, result)
    partial = failure(result)
    partial["items"][0].update(sources=original["items"][0]["sources"], same_task=True, independent_requests=True)
    validation.import_observations(result, partial, tmp_path)
    validation.resume(result)
    path = tmp_path / "validation-evidence/source-0.txt"
    path.write_text(path.read_text(encoding="utf-8") + "\nUnreviewed changed text", encoding="utf-8")
    payload = {"date": "2026-10-16", "items": [{"id": "v1", "status": "observed", "observed_at": "2026-10-16T10:00:00+08:00",
               "note": "Search only", "search": original["items"][0]["search"], "intent_match": True}]}
    with pytest.raises(ValueError, match="evidence changed"):
        validation.import_observations(result, payload, tmp_path)


def test_cross_report_plan_reuses_verified_sources_without_copying_or_visiting_them(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    old = sample()
    old["opportunities"][0]["task_id"] = task["id"]
    validation.prepare(old)
    originals = observations(tmp_path, old)
    partial = failure(old)
    partial["items"][0].update(sources=originals["items"][0]["sources"], same_task=True, independent_requests=True)
    validation.import_observations(old, partial, tmp_path)
    task["validation"] = {"status": "blocked", "evidence_fingerprint": demands.review_fingerprint(task, ledger),
                          "result_path": str(tmp_path / "old.json"), "item": old["opportunity_validation"]["items"][0]}
    new = {"date": "2026-10-16", "status": "complete", "opportunities": copy.deepcopy(old["opportunities"])}
    validation.prepare(new, ledger)
    item = new["opportunity_validation"]["items"][0]
    assert item["missing_checks"]["source_urls"] == []
    assert len(item["attempts"]) == 1
    assert item["observation"]["sources"][0]["evidence_root"] == str(tmp_path.resolve())


def test_running_resume_is_idempotent_and_cannot_reclaim_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    result = sample()
    validation.prepare(result)
    validation.import_observations(result, failure(result), tmp_path)
    assert validation.resume(result)
    snapshot = copy.deepcopy(result)
    assert not validation.resume(result)
    assert result == snapshot


def test_reimport_is_idempotent_without_duplicate_attempts(tmp_path):
    result = sample()
    validation.prepare(result)
    payload = failure(result)
    validation.import_observations(result, payload, tmp_path)
    snapshot = copy.deepcopy(result)
    validation.import_observations(result, payload, tmp_path)
    assert result == snapshot


def test_legacy_rephrase_archives_old_observation_before_changing_search(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    result = sample()
    validation.prepare(result)
    payload = observations(tmp_path, result)
    payload["items"][0]["intent_match"] = False
    validation.import_observations(result, payload, tmp_path)
    item = result["opportunity_validation"]["items"][0]
    item.pop("attempts")
    old = copy.deepcopy(item["observation"])
    assert validation.resume(result, "v1", "new natural invoice phrase")
    assert item["attempts"][0]["legacy_observation"]
    assert item["attempts"][0]["observation"] == old


def test_commands_update_ledger_validation_and_resume_cross_day_without_paid_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    from radar import google_trends
    monkeypatch.setattr(google_trends, "write_result", lambda path, result, output: spending.save(path, result))
    ledger = reviewed(2)
    tasks = list(ledger["tasks"].values())
    demands.cluster(None, ledger, tasks, supplied=[{"ids": [t["id"] for t in tasks], "same_job": True, "same_constraints": True}])
    task = next(iter(ledger["tasks"].values()))
    task.update(semantic_reviewed="2026-10-16", semantic_fingerprint=demands.review_fingerprint(task, ledger), status="值得深入验证")
    result = sample()
    result["opportunities"][0].update(task_id=task["id"], evidence=demands.references(task, ledger))
    result["market_research"] = {"status": "partial", "items": [{"keyword": "split invoices"}]}
    ledger_path, path, payload_path = tmp_path / "ledger.json", tmp_path / "result.json", tmp_path / "observations.json"
    spending.save(ledger_path, ledger)
    spending.save(path, result)
    spending.save(tmp_path / "run-receipt.json", {"origin": "scheduled", "last_execution_origin": "scheduled", "validation": "pending"})
    validation.command("claim", path, ledger_path=ledger_path)
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["tasks"][task["id"]]["validation"]["status"] == "pending"
    spending.save(payload_path, failure(result, "2026-10-16"))
    validation.command("import", path, payload_path, ledger_path=ledger_path)
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["tasks"][task["id"]]["validation"]["status"] == "blocked"
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 17))
    validation.command("resume", path, ledger_path=ledger_path, item_id="v1")
    current = json.loads(path.read_text(encoding="utf-8"))
    payload = observations(tmp_path, current)
    payload["date"] = "2026-10-17"
    payload["items"][0]["observed_at"] = "2026-10-17T10:00:00+08:00"
    spending.save(payload_path, payload)
    validation.command("import", path, payload_path, ledger_path=ledger_path)
    final = json.loads(path.read_text(encoding="utf-8"))
    saved = json.loads(ledger_path.read_text(encoding="utf-8"))["tasks"][task["id"]]["validation"]
    assert saved["status"] == "complete" and len(saved["item"]["attempts"]) == 2
    receipt = json.loads((tmp_path / "run-receipt.json").read_text(encoding="utf-8"))
    assert receipt["validation"] == "complete" and receipt["origin"] == receipt["last_execution_origin"] == "scheduled"
    assert final["market_research"] == result["market_research"]
    before = (path.read_bytes(), ledger_path.read_bytes())
    validation.command("import", path, payload_path, ledger_path=ledger_path)
    assert before == (path.read_bytes(), ledger_path.read_bytes())


@pytest.mark.parametrize("false_field", ["same_task", "independent_requests"])
def test_one_saved_contradicting_source_with_other_page_blocked_withdraws(tmp_path, false_field):
    result = sample()
    validation.prepare(result)
    full = observations(tmp_path, result)
    payload = failure(result)
    payload["items"][0].update(sources=full["items"][0]["sources"][:1], **{false_field: False})
    validation.import_observations(result, payload, tmp_path)
    assert result["opportunities"][0]["review_status"] == "withdrawn"
    assert result["opportunity_validation"]["items"][0]["validation_status"] == "withdrawn"


def test_negative_boolean_without_saved_source_cannot_withdraw(tmp_path):
    result = sample()
    validation.prepare(result)
    payload = failure(result)
    payload["items"][0].update(same_task=False, independent_requests=False)
    validation.import_observations(result, payload, tmp_path)
    assert result["opportunities"][0].get("review_status") != "withdrawn"
    assert result["opportunity_validation"]["items"][0]["validation_status"] == "blocked"


def test_prepare_cannot_wash_old_plan_to_new_evidence_version(tmp_path, monkeypatch):
    from radar import google_trends
    monkeypatch.setattr(validation, "today_local", lambda: date(2026, 10, 16))
    monkeypatch.setattr(google_trends, "write_result", lambda path, result, output: spending.save(path, result))
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    result = sample()
    result["opportunities"][0]["task_id"] = task["id"]
    validation.prepare(result, ledger)
    validation.import_observations(result, failure(result), tmp_path)
    frozen = result["opportunity_validation"]["items"][0]["evidence_fingerprint"]
    task["validation"] = {"status": "blocked", "evidence_fingerprint": frozen, "result_path": str(tmp_path / "result.json"),
                          "item": copy.deepcopy(result["opportunity_validation"]["items"][0])}
    ledger["sources"][task["source_ids"][0]]["source"]["author"] = "reddit:new_verified_identity"
    task["semantic_fingerprint"] = demands.review_fingerprint(task, ledger)
    lp, rp = tmp_path / "ledger.json", tmp_path / "result.json"
    spending.save(lp, ledger)
    spending.save(rp, result)
    original_ledger = lp.read_bytes()
    validation.command("prepare", rp, ledger_path=lp)
    assert lp.read_bytes() == original_ledger
    assert json.loads(rp.read_text(encoding="utf-8"))["opportunity_validation"]["items"][0]["evidence_fingerprint"] == frozen
    for action in ["claim", "resume"]:
        with pytest.raises(ValueError, match="plan evidence version changed"):
            validation.command(action, rp, ledger_path=lp, item_id="v1")
    # Only the formal audit's explicit rebuild path archives and makes a new plan.
    validation.prepare(result, ledger, rebuild=True)
    assert result["opportunity_validation_history"][0]["items"][0]["evidence_fingerprint"] == frozen
    assert result["opportunity_validation"]["items"][0]["evidence_fingerprint"] != frozen
