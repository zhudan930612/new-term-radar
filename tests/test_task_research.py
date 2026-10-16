import copy
import json
from datetime import date

import pytest

from radar import demands, task_research as research
from test_demands_review import ledger_with_tasks

DAY = date(2026, 10, 16)


def reviewed(count=1):
    ledger = ledger_with_tasks(count)
    for task in ledger["tasks"].values():
        task.update(semantic_reviewed=DAY.isoformat(), semantic_fingerprint=demands.review_fingerprint(task, ledger),
                    review={"firsthand": True, "bounded": True, "web_fit": "browser"})
    return ledger


def decision(task, **kwargs):
    return {"status": "active", "priority": 2, "gap": "Need original task sample", "next_action": "Read source context",
            "reason": "A concrete source describes recurring manual work", "next_review": None,
            "evidence_source_ids": task["source_ids"], **kwargs}


def records(tmp_path, task, planned, ledger, decision_name="continue", **kwargs):
    directory = tmp_path / "validation-evidence"
    directory.mkdir(exist_ok=True)
    src = ledger["sources"][task["source_ids"][0]]["source"]
    (directory / "source.txt").write_text(src["url"] + "\n" + src["text"], encoding="utf-8")
    return {"plan_id": planned["plan_id"], "date": DAY.isoformat(), "reviewer": "Codex", "items": [{
        "task_id": task["id"], "decision": decision_name, "research": decision(task, **kwargs),
        "records": [{"kind": "source_context", "url": src["url"], "evidence_file": "validation-evidence/source.txt",
                     "quote": src["text"], "observed_at": "2026-10-16T10:00:00+08:00", "note": "Need sample before changing hypothesis"}]}]}


def test_rank_respects_reviewed_priority_and_retirement_without_guessing_reason():
    ledger = reviewed(5)
    tasks = list(ledger["tasks"].values())
    research.apply_research(tasks[3], decision(tasks[3], priority=3), DAY, "Codex", "audit")
    tasks[3]["research"]["evidence_fingerprint"] = demands.review_fingerprint(tasks[3], ledger)
    research.apply_research(tasks[0], decision(tasks[0], status="resolved"), DAY, "Codex", "audit")
    research.apply_research(tasks[1], decision(tasks[1], status="paused", next_review="2026-10-20"), DAY, "Codex", "audit")
    tasks[4]["reason"] = "已经解决，不需要新工具"  # Unstructured legacy prose cannot silently retire evidence.
    ranked = research.rank_followups(ledger, DAY, 3)
    assert ranked[0]["id"] == tasks[3]["id"]
    assert {t["id"] for t in ranked} == {t["id"] for t in tasks[2:]}


def test_stale_priority_cannot_override_fresh_semantic_evidence():
    ledger = reviewed(2)
    a, b = list(ledger["tasks"].values())
    research.apply_research(b, decision(b, priority=3), DAY, "Codex", "audit")
    b["research"]["evidence_fingerprint"] = "obsolete"
    b["review"]["bounded"] = False
    assert research.rank_followups(ledger, DAY, 1)[0]["id"] == a["id"]


def test_exploration_slot_preserves_old_uncertain_seed():
    ledger = reviewed(5)
    tasks = list(ledger["tasks"].values())
    tasks[-1]["first_seen"] = "2026-09-01"
    tasks[-1]["review"] = {"firsthand": False, "bounded": False, "web_fit": "unknown"}
    ranked = research.rank_followups(ledger, DAY, 3)
    assert ranked[-1]["id"] == tasks[-1]["id"]


def test_research_import_is_evidence_backed_append_only_and_idempotent(tmp_path):
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    planned = research.plan(ledger, DAY)
    payload = records(tmp_path, task, planned, ledger)
    old_status = task["status"]
    assert research.import_observations(ledger, planned, payload, tmp_path, DAY)
    assert task["status"] == old_status
    assert task["research"]["records"][0]["evidence_sha256"]
    assert task["research"]["records"][0]["market_verified"] is False
    original_history = copy.deepcopy(task["research"]["history"])
    assert not research.import_observations(ledger, planned, payload, tmp_path, DAY)
    assert task["research"]["history"] == original_history
    second_plan = research.plan(ledger, DAY)
    second = records(tmp_path, task, second_plan, ledger, decision_name="stop", status="resolved")
    research.import_observations(ledger, second_plan, second, tmp_path, DAY)
    assert task["research"]["history"][:1] == original_history and len(task["research"]["records"]) == 2
    assert research.rank_followups(ledger, DAY, 3) == []


@pytest.mark.parametrize("bad", ["foreign_source", "stale_evidence", "stale_plan", "no_semantic_review", "fake_quote", "future_date"])
def test_untrusted_or_stale_research_is_rejected_without_partial_mutation(tmp_path, bad):
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    planned = research.plan(ledger, DAY)
    payload = records(tmp_path, task, planned, ledger)
    if bad == "foreign_source": payload["items"][0]["research"]["evidence_source_ids"] = ["foreign"]
    if bad == "stale_evidence": ledger["sources"][task["source_ids"][0]]["source"]["author"] = "x:changed"
    if bad == "stale_plan": planned["items"][0]["job"] = "tampered"
    if bad == "no_semantic_review": task.pop("semantic_reviewed")
    if bad == "fake_quote": payload["items"][0]["records"][0]["quote"] = "fabricated source quote"
    if bad == "future_date": payload["items"][0]["records"][0]["observed_at"] = "2026-10-17T10:00:00+08:00"
    snapshot = copy.deepcopy(ledger)
    with pytest.raises(ValueError):
        research.import_observations(ledger, planned, payload, tmp_path, DAY)
    assert ledger == snapshot


@pytest.mark.parametrize("change", [{"priority": True}, {"priority": 4}, {"next_review": "2027-10-01"},
                                    {"status": "paused"}, {"query_variants": ['"whole phrase"']}, {"query_variants": ["site:reddit.com tool"]}])
def test_strict_research_fields(change):
    task = next(iter(reviewed()["tasks"].values()))
    with pytest.raises(ValueError):
        research.validate_research(decision(task, **change), DAY, task["source_ids"])


def test_pending_validation_is_carried_but_same_version_withdrawal_is_not():
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    fingerprint = demands.review_fingerprint(task, ledger)
    task["last_shortlisted"] = DAY.isoformat()
    assert research.needs_validation(task, ledger)
    for state in ["pending", "blocked", "needs_rephrasing"]:
        task["validation"] = {"status": state, "evidence_fingerprint": fingerprint}
        assert research.needs_validation(task, ledger)
    for state in ["complete", "withdrawn"]:
        task["validation"] = {"status": state, "evidence_fingerprint": fingerprint}
        assert not research.needs_validation(task, ledger)


def test_plan_command_does_not_write_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "today_local", lambda: DAY)
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(reviewed()), encoding="utf-8")
    original = path.read_bytes()
    planned = tmp_path / "research-plan.json"
    research.command("plan", path, DAY, plan_path=planned)
    assert path.read_bytes() == original
    assert json.loads(planned.read_text(encoding="utf-8"))["plan_id"]


def test_paused_due_and_changed_resolved_decision_enter_local_plan_not_paid_queue():
    ledger = reviewed(2)
    first, second = list(ledger["tasks"].values())
    research.apply_research(first, decision(first, status="paused", next_review=DAY.isoformat()), DAY, "Codex", "audit")
    first["research"]["evidence_fingerprint"] = demands.review_fingerprint(first, ledger)
    research.apply_research(second, decision(second, status="resolved"), DAY, "Codex", "audit")
    second["research"]["evidence_fingerprint"] = "old"
    assert research.rank_followups(ledger, DAY, 3) == []
    items = research.plan(ledger, DAY)["items"]
    assert {r["local_review_required"] for r in items} == {"paused_review_due", "evidence_changed_research_decision_stale"}


def test_legacy_validation_migration_requires_local_review_not_research_replay():
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    task.update(last_shortlisted=DAY.isoformat(), validation={"status": "complete", "evidence_fingerprint": "legacy-unversioned"})
    assert not research.needs_validation(task, ledger)
    assert research.plan(ledger, DAY)["items"][0]["local_review_required"] == "legacy_validation_requires_local_review"


def test_priority_change_preserves_optional_queries_unless_explicitly_replaced():
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    research.apply_research(task, decision(task, query_variants=["split scanned invoices"]), DAY, "Codex", "audit")
    research.apply_research(task, decision(task, priority=3), DAY, "Codex", "manual followup")
    assert task["research"]["query_variants"] == ["split scanned invoices"]


def test_model_review_scores_cannot_enter_paid_queue_before_formal_audit():
    ledger = reviewed(2)
    audited, provisional = list(ledger["tasks"].values())
    audited["review"] = {"firsthand": True, "bounded": False, "web_fit": "unknown"}
    provisional.pop("semantic_reviewed")
    provisional.pop("semantic_fingerprint")
    assert research.rank_followups(ledger, DAY, 3) == [audited]
    planned = research.plan(ledger, DAY)
    assert next(i for i in planned["items"] if i["task_id"] == provisional["id"])["local_review_required"] == "semantic_review_required"


def test_due_pause_beyond_active_window_surfaces_locally_without_paid_reactivation():
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    research.apply_research(task, decision(task, status="paused", next_review="2027-01-04"), DAY, "Codex", "audit")
    task["research"]["evidence_fingerprint"] = demands.review_fingerprint(task, ledger)
    original_seen = task["last_seen"]
    assert research.rank_followups(ledger, "2027-01-04", 3) == []
    planned = research.plan(ledger, "2027-01-04")
    assert planned["items"][0]["local_review_required"] == "paused_review_due"
    assert task["last_seen"] == original_seen


@pytest.mark.parametrize("kind", ["task_sample", "experiment"])
def test_local_task_material_is_recorded_without_fabricating_web_or_customer_evidence(tmp_path, kind):
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    planned = research.plan(ledger, DAY)
    payload = records(tmp_path, task, planned, ledger)
    record = payload["items"][0]["records"][0]
    record.update(origin="local", kind=kind, url="", note="Authorized local sample and reproducible offline observation")
    record["quote"] = "Local processing produced the expected output on this fixture."
    (tmp_path / record["evidence_file"]).write_text(record["quote"], encoding="utf-8")
    sources_before = copy.deepcopy(ledger["sources"])
    assert research.import_observations(ledger, planned, payload, tmp_path, DAY)
    saved = task["research"]["records"][0]
    assert saved["origin"] == "local" and saved["url"] == ""
    assert saved["independent_user_evidence"] is False and saved["market_verified"] is False
    assert saved["evidence_sha256"] and ledger["sources"] == sources_before
    assert task["status"] == "待补证"


@pytest.mark.parametrize("bad", ["escape", "fake_quote", "fake_url", "source_context", "payment", "alternative", "channel", "invalid_utf8"])
def test_local_evidence_cannot_escape_or_impersonate_public_proof(tmp_path, bad):
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    planned = research.plan(ledger, DAY)
    payload = records(tmp_path, task, planned, ledger)
    record = payload["items"][0]["records"][0]
    record.update(origin="local", kind="experiment", url="")
    if bad == "escape": record["evidence_file"] = "../outside.txt"
    if bad == "fake_quote": record["quote"] = "A fabricated result not present in the file"
    if bad == "fake_url": record["url"] = "https://example.com/not-an-actual-web-observation"
    if bad in {"source_context", "payment", "alternative", "channel"}: record["kind"] = bad
    if bad == "invalid_utf8": (tmp_path / record["evidence_file"]).write_bytes(b"\xff\xfe\xff")
    before = copy.deepcopy(ledger)
    with pytest.raises((ValueError, OSError)):
        research.import_observations(ledger, planned, payload, tmp_path, DAY)
    assert ledger == before


def test_completed_context_validation_continues_only_as_local_experiment_plan():
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    fp = demands.review_fingerprint(task, ledger)
    task.update(status="值得深入验证", last_shortlisted=DAY.isoformat(), shortlisted_fingerprint=fp,
                validation={"status": "complete", "evidence_fingerprint": fp})
    assert not research.needs_validation(task, ledger)
    assert research.rank_followups(ledger, DAY, 3) == []
    assert research.plan(ledger, DAY)["items"][0]["local_review_required"] == "qualified_research_due"
    original_shortlist = task["shortlisted_fingerprint"]
    research.apply_research(task, decision(task, next_review="2026-10-20"), DAY, "Codex", "experiment planned")
    task["research"]["evidence_fingerprint"] = fp
    assert research.plan(ledger, DAY)["items"] == []
    assert research.plan(ledger, "2026-10-20")["items"][0]["local_review_required"] == "qualified_research_due"
    research.apply_research(task, decision(task, status="paused", next_review="2026-10-21"), DAY, "Codex", "await authorized sample")
    assert research.plan(ledger, DAY)["items"] == []
    research.apply_research(task, decision(task, status="resolved"), DAY, "Codex", "existing solution meets task")
    assert research.plan(ledger, "2026-10-21")["items"] == []
    assert task["shortlisted_fingerprint"] == original_shortlist and not research.needs_validation(task, ledger)
