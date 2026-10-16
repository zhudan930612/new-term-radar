"""Offline publishing, audit revision, recall and fingerprint regressions."""
import copy
import json
from datetime import date

import pytest

from radar import aisa, demand_audit, demands, spending

DAY = "2026-10-16"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def ledger(count=2):
    value = {"version": 2, "sources": {}, "tasks": {}}
    for n in range(count):
        source = {"id": f"e{n}", "kind": "post", "author": f"reddit:user{n}",
                  "url": f"https://example.com/source/{n}", "text": f"I manually split invoices for client {n} every week."}
        sid = demands.source_id(source)
        atom = {k: "未知" for k in demands.TASK_FIELDS}
        atom.update(job=f"Task {n}", output="Split invoice files", anchor=source["text"], search_terms=[f"invoice task {n}"])
        assessment = {"category": "need", "reason": "Concrete firsthand task", "tasks": [atom]}
        value["sources"][sid] = {"source": source, "assessment": assessment, "audit": {**assessment, "date": DAY},
            "first_seen": DAY, "last_seen": DAY, "semantic_review": {"date": DAY, "reviewer": "fixture"}}
    demands.materialize(value)
    return value


def case(tmp_path, value):
    tmp_path.mkdir(parents=True, exist_ok=True)
    lp, rp = tmp_path / "ledger.json", tmp_path / "result.json"
    spending.save(lp, value)
    spending.save(rp, {"date": DAY, "workflow_version": 2, "status": "incomplete", "errors": [], "analysis_errors": [],
        "semantic_audit": {"status": "tasks_pending"}, "zero_result_audit": {"status": "incomplete"},
        "evidence": [e["source"] for e in value["sources"].values()], "tasks": [], "opportunities": [],
        "coverage": {"current_sources": len(value["sources"])}, "dispositions": [
            {"id": sid, "url": e["source"]["url"], "category": "need", "reason": "fixture"} for sid, e in value["sources"].items()]})
    return lp, rp


def decision(task, positive=False):
    proposal = {k: "Specific experiment" for k in ("title", "user", "job", "pain", "web_advantage", "input", "output", "mvp",
        "gap", "constraints", "validation", "success_condition", "stop_condition")}
    proposal["keywords"] = ["split invoices by client"]
    return {"id": task["id"], "same_job": True, "firsthand": True, "independent": positive, "bounded": True,
            "web_fit": "browser", "reason": "Original evidence reviewed", "proposal": proposal if positive else None}


def audit(lp, rp, rows, **extra):
    op = rp.parent / "input.json"
    spending.save(op, {"date": read(rp)["date"], "reviewer": "Codex fixture", "method": "Read originals", "reviews": rows, **extra})
    demand_audit.command("tasks", rp, lp, op)


def approve(task, value, day=DAY):
    fp = demands.review_fingerprint(task, value)
    task.update(reviewed=day, semantic_reviewed=day, review_fingerprint=fp, semantic_fingerprint=fp, review=decision(task))


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    monkeypatch.setattr(aisa, "chat", lambda *a, **k: pytest.fail("Local audit must not buy model work"))


def test_incomplete_batch_does_not_consume_shortlist_and_next_day_can_publish(tmp_path):
    value = ledger(20)
    ids = sorted(value["tasks"])[:2]
    demands.cluster(None, value, list(value["tasks"].values()), supplied=[
        {"ids": ids, "same_job": True, "same_constraints": True}])
    target = ids[0]
    lp, rp = case(tmp_path, value)
    audit(lp, rp, [decision(t, t["id"] == target) for t in demand_audit.due_tasks(value, DAY)[:18]])
    partial, current = read(rp), demands.load(lp)
    assert partial["status"] == "incomplete" and partial["opportunities"] == []
    assert "research_plan" not in partial
    assert len(partial["provisional_opportunities"]) == 1
    assert "last_shortlisted" not in current["tasks"][target]
    # Next day's completed source audit retains current task decisions.
    tomorrow = "2026-10-17"
    partial["date"] = tomorrow
    for entry in current["sources"].values():
        entry["semantic_review"]["date"] = tomorrow
    spending.save(rp, partial)
    spending.save(lp, current)
    audit(lp, rp, [decision(t) for t in demand_audit.due_tasks(current, tomorrow)])
    final = read(rp)
    assert final["status"] == "complete" and len(final["opportunities"]) == 1
    assert final["research_plan"] == read(tmp_path / "research-plan.json")
    assert demands.load(lp)["tasks"][target]["last_shortlisted"] == tomorrow


def test_collection_error_cannot_publish_or_mark_shortlisted(tmp_path):
    value = ledger()
    ids = sorted(value["tasks"])
    demands.cluster(None, value, list(value["tasks"].values()), supplied=[{"ids": ids, "same_job": True, "same_constraints": True}])
    lp, rp = case(tmp_path, value)
    result = read(rp)
    result["errors"] = ["Source page failed"]
    spending.save(rp, result)
    audit(lp, rp, [decision(next(iter(value["tasks"].values())), True)])
    assert read(rp)["status"] == "incomplete" and not read(rp)["opportunities"]
    assert "last_shortlisted" not in next(iter(demands.load(lp)["tasks"].values()))


def test_model_groups_are_suggestions_and_do_not_modify_verified_tasks(monkeypatch):
    value = ledger()
    for task in value["tasks"].values():
        approve(task, value)
    before = copy.deepcopy(value["tasks"])
    groups = [{"ids": sorted(before), "same_job": True, "same_constraints": True}]
    monkeypatch.setattr(aisa, "chat", lambda *a, **k: json.dumps({"groups": groups}))
    demands.cluster("preprocessor", value, list(value["tasks"].values()))
    assert value["tasks"] == before and len(value["merge_suggestions"]) == 1
    demands.cluster(None, value, list(value["tasks"].values()), supplied=groups)
    assert len(value["tasks"]) == 1


def test_historical_recall_finds_late_task_by_terms_and_constraints():
    value = ledger(45)
    tasks = list(value["tasks"].values())
    for task in tasks:
        task.update(job="unrelated cooking", search_terms=["recipe cooking"], constraint="kitchen", output="meal", context="")
    historical = tasks[-1]
    historical.update(job="PDF book outlines", search_terms=["offline PDF heading bookmarks"], constraint="local Mac PDF")
    seed = tasks[0]
    seed.update(job="generate document bookmarks", search_terms=["offline PDF heading bookmarks"], constraint="local Mac PDF")
    matches = demands.historical_matches(value, [seed], DAY, limit=3)
    assert matches[0]["id"] == historical["id"] and len(matches) <= 3


def test_supplied_merge_accepts_valid_history_outside_retrieval_window(tmp_path):
    value = ledger(25)
    tasks = sorted(value["tasks"].values(), key=lambda t: t["id"])
    for task in tasks[1:]:
        approve(task, value)
    first, far = tasks[0], tasks[-1]
    lp, rp = case(tmp_path, value)
    target = min((first, far), key=lambda t: (t["first_seen"], t["id"]))
    audit(lp, rp, [decision(target, True)], groups=[{"ids": [first["id"], far["id"]], "same_job": True, "same_constraints": True}])
    saved = demands.load(lp)
    assert len(saved["tasks"][target["id"]]["source_ids"]) == 2
    assert read(rp)["status"] == "complete"


@pytest.mark.parametrize("field,value", [("author", "reddit:other"), ("url", "https://example.com/changed"),
    ("text", "Changed original content"), ("source_record_id", "new-comment"), ("author_url", "https://example.com/account")])
def test_identity_or_original_change_invalidates_review(field, value):
    data = ledger(1)
    task = next(iter(data["tasks"].values()))
    approve(task, data)
    data["sources"][task["source_ids"][0]]["source"][field] = value
    assert demands.review_due(task, data, DAY) and demand_audit.due_tasks(data, DAY)


def test_reason_polish_does_not_invalidate_but_task_constraint_does():
    value = ledger(1)
    task = next(iter(value["tasks"].values()))
    approve(task, value)
    task["reason"] = "Edited explanation"
    value["sources"][task["source_ids"][0]]["assessment"]["reason"] = "Polished source explanation"
    assert not demands.review_due(task, value, DAY) and not demand_audit.due_tasks(value, DAY)
    task["constraint"] = "Offline only"
    assert demands.review_due(task, value, DAY)


def test_legacy_migration_retains_publication_and_withdrawal_without_model_work(tmp_path):
    value = ledger(1)
    task = next(iter(value["tasks"].values()))
    task.update(reviewed=DAY, semantic_reviewed=DAY, review_fingerprint="old-review", semantic_fingerprint="old-semantic",
                shortlisted_fingerprint="old-published", withdrawn_fingerprint="old-withdrawn", last_shortlisted=DAY)
    path = tmp_path / "ledger.json"
    spending.save(path, value)
    original = path.read_bytes()
    migrated = demands.load(path)
    current = migrated["tasks"][task["id"]]
    fp = demands.review_fingerprint(current, migrated)
    assert path.read_bytes() == original, "Loading alone must not mutate saved state"
    assert current["review_fingerprint"] == current["shortlisted_fingerprint"] == current["withdrawn_fingerprint"] == fp
    assert current["fingerprint_compat"][0]["legacy"]["semantic_fingerprint"] == "old-semantic"
    assert demand_audit.due_tasks(migrated, DAY) and not demands.review_due(current, migrated, DAY)
    result = demands.process("never-call", [next(iter(migrated["sources"].values()))["source"]], path, date.fromisoformat(DAY))
    assert not result["opportunities"]
    assert demands.load(path)["tasks"][task["id"]]["fingerprint_migration_pending"]


def test_amend_preserves_snapshots_and_each_task_batch(tmp_path):
    value = ledger(19)
    lp, rp = case(tmp_path, value)
    audit(lp, rp, [decision(t) for t in demand_audit.due_tasks(value, DAY)[:18]])
    first_events = {p.name: p.read_bytes() for p in (tmp_path / "audit-history").glob("*-tasks-*.json")}
    audit(lp, rp, [decision(t) for t in demand_audit.due_tasks(demands.load(lp), DAY)])
    assert read(rp)["status"] == "complete"
    assert all((tmp_path / "audit-history" / name).read_bytes() == raw for name, raw in first_events.items())
    assert len(read(tmp_path / "semantic-tasks-observations.json")["reviews"]) == 19
    assert len(read(tmp_path / "audit-history/index.json")["events"]) == 2
    before = rp.read_bytes()
    demand_audit.command("amend", rp, lp)
    assert read(rp)["semantic_audit"]["status"] == "running" and read(rp)["status"] == "awaiting_audit"
    assert "research_plan" not in read(rp) and read(tmp_path / "research-plan.json")["items"] == []
    events = read(tmp_path / "audit-history/index.json")["events"]
    event = read(tmp_path / "audit-history" / events[-1]["file"])
    assert event["action"] == "amend" and event["before"]["result"] == json.loads(before)
    assert event["before"]["ledger"]["tasks"]


def test_interrupted_commit_keeps_recovery_locks_and_snapshot(tmp_path, monkeypatch):
    value = ledger(1)
    lp, rp = case(tmp_path, value)
    monkeypatch.setattr(demand_audit.google_trends, "write_result", lambda *a, **k: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError):
        audit(lp, rp, [decision(next(iter(value["tasks"].values())))])
    assert lp.with_suffix(".audit.lock").exists() and (tmp_path / "trends-edit.lock").exists()
    events = read(tmp_path / "audit-history/index.json")["events"]
    assert (tmp_path / "audit-history" / events[-1]["file"]).exists()
    assert not (tmp_path / "audit-history" / events[-1]["commit_file"]).exists()


def test_amend_can_revise_source_without_changing_request_ledger(tmp_path):
    value = ledger(1)
    lp, rp = case(tmp_path, value)
    task = next(iter(value["tasks"].values()))
    audit(lp, rp, [decision(task)])
    api = tmp_path / "api"
    api.mkdir()
    paid = api / "requests.json"
    paid.write_text('[{"id":"paid-original","status":"ok","actual_usd":0.01}]', encoding="utf-8")
    paid_before = paid.read_bytes()
    amendment = tmp_path / "amend.json"
    spending.save(amendment, {"task_ids": [task["id"]], "reason": "Correct a mistaken need classification"})
    demand_audit.command("amend", rp, lp, amendment)
    sid = task["source_ids"][0]
    source_input = tmp_path / "corrected-source.json"
    spending.save(source_input, {"date": DAY, "reviewer": "Codex correction", "method": "Full original reread",
        "rows": [{"id": sid, "category": "promotion", "reason": "Original is a pitch, not a firsthand request", "tasks": []}]})
    demand_audit.command("sources", rp, lp, source_input)
    audit(lp, rp, [])
    assert read(rp)["status"] == "complete" and read(rp)["tasks"] == []
    assert demands.load(lp)["tasks"][task["id"]]["status"] == "提取已修订"
    assert paid.read_bytes() == paid_before
    assert len(read(tmp_path / "audit-history/index.json")["events"]) == 4


def test_previously_displayed_unvalidated_candidate_returns_without_becoming_new(tmp_path):
    value = ledger()
    ids = sorted(value["tasks"])
    demands.cluster(None, value, list(value["tasks"].values()), supplied=[{"ids": ids, "same_job": True, "same_constraints": True}])
    target = value["tasks"][ids[0]]
    demands.review(None, value, [target], DAY, supplied=[decision(target, True)])
    approve(target, value)
    target.update(last_shortlisted="2026-10-15", shortlisted_fingerprint=target["review_fingerprint"])
    lp, rp = case(tmp_path, value)
    audit(lp, rp, [])
    assert len(read(rp)["opportunities"]) == 1
    item = read(rp)["opportunities"][0]
    assert item["validation_continuation"] is True and item["is_new"] is False
    assert demands.load(lp)["tasks"][target["id"]]["last_shortlisted"] == "2026-10-15"


def test_research_decision_is_recorded_only_after_current_semantic_review(tmp_path):
    value = ledger(1)
    lp, rp = case(tmp_path, value)
    task = next(iter(value["tasks"].values()))
    row = decision(task)
    row["research"] = {"status": "active", "priority": 2, "gap": "Second independent user missing",
        "next_action": "Review another firsthand workflow", "reason": "Concrete source-backed task",
        "next_review": "2026-10-17", "evidence_source_ids": task["source_ids"]}
    audit(lp, rp, [row])
    saved = demands.load(lp)
    research = saved["tasks"][task["id"]]["research"]
    assert research["priority"] == 2 and len(research["history"]) == 1
    assert research["evidence_fingerprint"] == demands.review_fingerprint(saved["tasks"][task["id"]], saved)


def test_collecting_changed_identity_preserves_old_source_and_invalidates_semantics(tmp_path, monkeypatch):
    value = ledger(1)
    task = next(iter(value["tasks"].values()))
    approve(task, value)
    sid = task["source_ids"][0]
    incoming = {**value["sources"][sid]["source"], "author": "reddit:corrected_identity"}
    path = tmp_path / "ledger.json"
    spending.save(path, value)
    monkeypatch.setattr(aisa, "chat", lambda *a, **kw: json.dumps({"reviews": [decision(task)]}))
    result = demands.process("mock", [incoming], path, date.fromisoformat(DAY))
    saved = demands.load(path)
    assert saved["sources"][sid]["source"]["author"] == "reddit:corrected_identity"
    assert saved["sources"][sid]["source_history"][0]["author"] == "reddit:user0"
    assert demand_audit.due_tasks(saved, DAY) and not result["opportunities"]


def test_source_checkpoint_preserves_full_scope_without_preprocessing(tmp_path):
    value = ledger(1)
    task = next(iter(value["tasks"].values()))
    approve(task, value)
    sid = task["source_ids"][0]
    changed = {**value["sources"][sid]["source"], "author": "reddit:corrected"}
    fresh = {"id": "e2", "url": "https://example.com/new-need", "text": "I manually export a hundred invoices every week.",
             "kind": "post", "author": "reddit:new_user"}
    path = tmp_path / "ledger.json"
    spending.save(path, value)
    ids = demands.checkpoint_sources([changed, fresh, fresh], path, date.fromisoformat(DAY))
    saved = demands.load(path)
    assert ids == [sid, demands.source_id(fresh)]
    assert saved["sources"][sid]["source"]["author"] == "reddit:corrected"
    assert saved["sources"][sid]["source_history"][0]["author"] == "reddit:user0"
    assert saved["sources"][ids[1]]["source"]["text"] == fresh["text"]
    assert "assessment" not in saved["sources"][ids[1]], "Unknown new source stays pending local/model assessment"
    assert demand_audit.due_tasks(saved, DAY), "Identity correction must not inherit prior semantic approval"


@pytest.mark.parametrize("status", ["running", "complete", "partial", "incomplete"])
def test_amend_retains_paid_research_barrier_after_audit_finishes(tmp_path, monkeypatch, status):
    from radar import market_research
    value = ledger(1)
    task = next(iter(value["tasks"].values()))
    lp, rp = case(tmp_path, value)
    audit(lp, rp, [decision(task)])
    result = read(rp)
    paid_stage = {"status": status, "items": [{"task_id": task["id"], "keyword": "invoice task"}],
                  "aisa_account_balance_change_usd": .02, "goanyapi_cost_usd": .03}
    result["market_research"] = paid_stage
    spending.save(rp, result)
    demand_audit.command("amend", rp, lp)
    current = demands.load(lp)
    source_input = tmp_path / "sources.json"
    spending.save(source_input, {"date": DAY, "reviewer": "Codex", "method": "Reread unchanged source",
        "rows": [{"id": sid, **e["assessment"]} for sid, e in current["sources"].items()]})
    demand_audit.command("sources", rp, lp, source_input)
    audit(lp, rp, [decision(t) for t in demands.active_tasks(demands.load(lp), DAY)])
    saved = read(rp)
    assert saved["status"] == "complete"
    retained = saved["market_research"]
    assert retained["status"] == "incomplete" and retained["previous_stage"] == paid_stage
    assert retained["goanyapi_cost_usd"] == .03 and retained["automatic_retry_allowed"] is False
    monkeypatch.setattr(market_research, "ready_items", lambda *a: pytest.fail("Paid terminal gate must run first"))
    monkeypatch.setattr(spending, "account", lambda: pytest.fail("No account call"))
    monkeypatch.setattr(spending, "quote", lambda *a: pytest.fail("No quote"))
    monkeypatch.setattr(market_research.goanyapi, "key", lambda: pytest.fail("No GoAnyAPI stage restart"))
    assert market_research.command(rp, {}, tmp_path) == 2


def followup_case(tmp_path):
    value = ledger(2)
    old, new = list(value["tasks"].values())
    old_sid, new_sid = old["source_ids"][0], new["source_ids"][0]
    for sid in (old_sid, new_sid):
        value["sources"][sid]["source"].update(url=f"https://www.reddit.com/r/work/comments/{sid}/task/", author="")
    old["first_seen"] = value["sources"][old_sid]["first_seen"] = "2026-10-15"
    approve(old, value)
    value["sources"][new_sid]["source"]["task_id"] = old["id"]
    lp, rp = case(tmp_path, value)
    result = read(rp)
    result["collection"] = {"queries": [{"source": "x", "task_id": old["id"], "status": "complete", "accepted": 0}],
                            "confirmed_followups": []}
    result["research_collection"] = {"queries": [{"source": "reddit", "task_id": old["id"], "status": "complete", "accepted": 1}],
                                     "confirmed_followups": []}
    spending.save(rp, result)
    return value, old, new, lp, rp


def test_approved_same_task_new_source_confirms_only_its_stage_not_an_independent_customer(tmp_path):
    value, old, new, lp, rp = followup_case(tmp_path)
    audit(lp, rp, [decision(old)], groups=[{"ids": [old["id"], new["id"]], "same_job": True, "same_constraints": True}])
    result, saved = read(rp), demands.load(lp)
    target = saved["tasks"][old["id"]]
    assert result["status"] == "complete" and not result["opportunities"]
    assert result["collection"]["confirmed_followups"] == []
    assert result["research_collection"]["confirmed_followups"] == [old["id"]]
    assert target["confirmed_followup_sources"] == new["source_ids"] and target["last_evidence"] == DAY
    assert target["review"]["independent"] is False
    assert len(target["followup_confirmation_history"]) == 1
    result["market_research"] = {"status": "incomplete", "automatic_retry_allowed": False, "previous_stage": {"paid_usd": .03}}
    paid_barrier = copy.deepcopy(result["market_research"])
    demand_audit.confirm_followups(result, saved)
    assert len(target["followup_confirmation_history"]) == 1, "Repeated finalization must not fabricate another confirmation"
    assert result["market_research"] == paid_barrier
    # A later local semantic correction revokes the live confirmation while
    # retaining the original confirmation event and the external paid barrier.
    saved["sources"][new["source_ids"][0]]["assessment"] = {"category": "promotion", "reason": "Corrected", "tasks": []}
    approve(target, saved)
    demand_audit.confirm_followups(result, saved)
    assert target["confirmed_followup_sources"] == [] and "last_evidence" not in target
    assert target["followup_confirmation_history"][-1]["event"] == "revoked"
    assert result["research_collection"]["confirmed_followups"] == [] and result["market_research"] == paid_barrier


def test_query_link_without_approved_task_membership_does_not_confirm_original_task(tmp_path):
    value, old, new, lp, rp = followup_case(tmp_path)
    audit(lp, rp, [decision(new)])
    result, saved = read(rp), demands.load(lp)
    assert result["status"] == "complete"
    assert result["research_collection"]["confirmed_followups"] == []
    assert "last_evidence" not in saved["tasks"][old["id"]]
    assert "last_evidence" not in saved["tasks"][new["id"]]


@pytest.mark.parametrize("gap", ["repeated_text", "old_source", "same_url_revisit", "not_firsthand", "incomplete", "wrong_stage", "empty_query"])
def test_followup_confirmation_rejects_revisits_and_unverified_or_unfinished_evidence(tmp_path, gap):
    value, old, new, lp, rp = followup_case(tmp_path)
    old_sid, new_sid = old["source_ids"][0], new["source_ids"][0]
    if gap == "repeated_text":
        value["sources"][new_sid]["source"]["text"] = value["sources"][old_sid]["source"]["text"]
        atom = value["sources"][new_sid]["assessment"]["tasks"][0]
        atom["anchor"] = value["sources"][new_sid]["source"]["text"]
        new["atoms"] = [demands.atom_id(new_sid, atom)]
    if gap == "old_source": value["sources"][new_sid]["first_seen"] = "2026-10-15"
    if gap == "same_url_revisit": value["sources"][new_sid]["source"]["url"] = value["sources"][old_sid]["source"]["url"]
    result = read(rp)
    if gap == "incomplete": result["errors"] = ["Another source failed"]
    if gap == "wrong_stage": result["research_collection"]["queries"][0]["source"] = "youtube_comments"
    if gap == "empty_query": result["research_collection"]["queries"][0]["accepted"] = 0
    spending.save(lp, value)
    spending.save(rp, result)
    row = decision(old)
    if gap == "not_firsthand": row["firsthand"] = False
    audit(lp, rp, [row], groups=[{"ids": [old["id"], new["id"]], "same_job": True, "same_constraints": True}])
    final, saved = read(rp), demands.load(lp)
    assert not final["research_collection"]["confirmed_followups"]
    assert "last_evidence" not in saved["tasks"][old["id"]]
