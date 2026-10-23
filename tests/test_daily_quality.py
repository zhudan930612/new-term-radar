"""Offline regressions for daily readouts, billing semantics and research gates."""
import copy
import json
from datetime import date

import pytest

from radar import aisa, daily_summary, demand_audit, demands, research_queries, source_identity, spending, task_research
from test_task_research import reviewed, decision, records, DAY


def daily(ledger):
    return {"date": DAY.isoformat(), "workflow_version": 2, "status": "complete", "errors": [],
            "semantic_audit": {"status": "complete"}, "opportunities": [], "coverage": {},
            "tasks": [{**t, "reason": "Original review", "evidence": demands.references(t, ledger)} for t in ledger["tasks"].values()],
            "dispositions": [], "research_plan": task_research.plan(ledger, DAY)}


def test_model_cost_is_estimate_with_snapshot_but_retains_budget_amount():
    prices = {"input": .2, "output": .7, "cache_write": .3}
    response = {"usage": {"prompt_tokens": 1000, "completion_tokens": 100}, "choices": [{"finish_reason": "stop"}]}
    row = spending._classify(aisa.CHAT, {}, response, prices)
    assert row["actual_usd"] == row["estimated_usd"] == pytest.approx(.00037)
    assert row["cost_basis"] == "token_estimate" and row["settled_usd"] is None
    prices["input"] = 999
    assert row["pricing_snapshot"]["input"] == .2
    assert row["charge_known"] is True and not row["global_stop"]


def test_mixed_legacy_journal_unknown_zero_is_not_free_and_not_mutated():
    entries = [{"label": "qwen3.7-flash", "status": "ok", "actual_usd": .2},
               {"label": "get_reddit_search", "attempts": [
                   {"status": "failed", "charge_known": True, "actual_usd": .01},
                   {"status": "pending", "charge_known": False, "actual_usd": 0, "reserved_usd": .3}]}]
    before = copy.deepcopy(entries)
    bill = spending.billing_summary(entries)
    assert bill["model_estimated_usd"] == .2 and bill["provider_reported_usd"] == .01
    assert bill["unknown_attempts"] == 1 and bill["unknown_reserved_usd"] == .3
    assert entries == before


def test_summary_exposes_latest_decision_without_rewriting_plan_or_opportunity(tmp_path):
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    result = daily(ledger)
    task_research.apply_research(task, decision(task, status="paused", next_review="2026-10-23",
        waiting_on="public_sample", resume_condition="A public anonymized example becomes available"), DAY, "Codex", "audit")
    task["research"]["evidence_fingerprint"] = demands.review_fingerprint(task, ledger)
    before = copy.deepcopy(result)
    summary = daily_summary.build(result, ledger, [], "2026-10-16T12:00:00+08:00")
    assert summary["research_updates"][0]["research"]["status"] == "paused"
    assert summary["research_plan_snapshot"] == result["research_plan"] and "research_plan" not in summary
    assert result == before and summary["opportunities"] == result["opportunities"]
    assert "A public anonymized example" in daily_summary.render(summary)
    root = tmp_path / "web-opportunities" / DAY.isoformat()
    spending.save(root / "result.json", result)
    spending.save(tmp_path / "demand-ledger.json", ledger)
    originals = {p: p.read_bytes() for p in [root / "result.json", tmp_path / "demand-ledger.json"]}
    daily_summary.command(tmp_path, DAY.isoformat())
    assert all(p.read_bytes() == data for p, data in originals.items())
    with pytest.raises(ValueError, match="separate"):
        daily_summary.command(tmp_path, DAY.isoformat(), root / "result.md")


def test_summary_refuses_active_audit(tmp_path):
    path = tmp_path / "demand-ledger.audit.lock"
    path.touch()
    with pytest.raises(ValueError, match="active"):
        daily_summary.command(tmp_path, DAY.isoformat())
    assert path.exists()


def test_import_refreshes_summary_and_duplicate_import_remains_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(task_research, "today_local", lambda: DAY)
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    original = daily(ledger)
    planned = task_research.plan(ledger, DAY)
    payload = records(tmp_path, task, planned, ledger, decision_name="pause", status="paused", next_review="2026-10-23")
    lp, pp, op = tmp_path / "demand-ledger.json", tmp_path / "plan.json", tmp_path / "observations.json"
    root = tmp_path / "web-opportunities" / DAY.isoformat()
    for p, value in [(lp, ledger), (pp, planned), (op, payload), (root / "result.json", original)]:
        spending.save(p, value)
    before = (root / "result.json").read_bytes()
    task_research.command("import", lp, DAY, observations=op, plan_path=pp)
    assert json.loads((root / "summary.json").read_text(encoding="utf-8"))["research_updates"][0]["research"]["status"] == "paused"
    imported = lp.read_bytes()
    task_research.command("import", lp, DAY, observations=op, plan_path=pp)
    assert lp.read_bytes() == imported and (root / "result.json").read_bytes() == before


def test_incomplete_followups_do_not_count_as_failed_evidence_rounds():
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    task["search_history"] = [{"date": DAY.isoformat(), "queries": []}]
    result = daily(ledger)
    result.update(status="incomplete", collection={"attempted_followups": [task["id"]], "queries": []})
    demand_audit.confirm_followups(result, ledger)
    assert result["collection"]["confirmation_status"] == "not_evaluated"
    assert task_research.unproductive_rounds(task) == 0
    result["status"] = "complete"
    demand_audit.confirm_followups(result, ledger)
    assert task_research.unproductive_rounds(task) == 1
    demand_audit.confirm_followups(result, ledger)
    assert task_research.unproductive_rounds(task) == 1


def test_repeated_unproductive_queries_require_local_change_not_more_paid_calls():
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    task["search_history"] = [{"confirmation": {"status": "no_confirmed_evidence"}} for _ in range(2)]
    assert task_research.rank_followups(ledger, DAY, 3) == []
    assert task_research.plan(ledger, DAY)["items"][0]["local_review_required"] == "query_revision_required"
    task_research.apply_research(task, decision(task, query_variants=["reconcile invoice line items"]), DAY, "Codex", "audit")
    assert task_research.unproductive_rounds(task) == 0


@pytest.mark.parametrize("kwargs", [{"waiting_on": "public_sample"},
    {"waiting_on": "user_material", "resume_condition": "x"},
    {"waiting_on": "invented", "resume_condition": "long enough condition"},
    {"query_context": {"role": "site:reddit.com"}}, {"query_context": {"untrusted": "anything"}}])
def test_invalid_pause_or_query_context_is_rejected(kwargs):
    task = next(iter(reviewed()["tasks"].values()))
    with pytest.raises(ValueError):
        task_research.validate_research(decision(task, status="paused", next_review="2026-10-23", **kwargs), DAY, task["source_ids"])


def test_query_context_is_bounded_and_keeps_specific_task():
    task = {"search_terms": ["extract invoice line items"], "research": {"query_context": {"role": "bookkeeper", "alternative": "Excel"}}}
    values = research_queries.query_variants(task)
    assert values[:2] == ["extract invoice line items bookkeeper", "extract invoice line items excel"]
    assert all(len(q.split()) <= 12 for q in values)


def test_success_breaks_unproductive_streak():
    task = {"search_history": [{"confirmation": {"status": s}} for s in
        ("no_confirmed_evidence", "no_confirmed_evidence", "confirmed", "no_confirmed_evidence")]}
    assert task_research.unproductive_rounds(task) == 1


def identity_fixture(tmp_path):
    ledger = reviewed()
    task = next(iter(ledger["tasks"].values()))
    sid = task["source_ids"][0]
    source = ledger["sources"][sid]["source"]
    source.update(kind="comment", url="https://youtu.be/abcdefghijk", author="", author_url="https://www.youtube.com/@example")
    channel = "UC" + "a" * 22
    root = tmp_path / "validation-evidence"
    root.mkdir()
    comment_url = "https://www.youtube.com/watch?v=abcdefghijk&lc=COMMENT_123"
    cq = source["text"] + "\n" + source["author_url"]
    pq = "https://www.youtube.com/channel/" + channel
    (root / "comment.txt").write_text(comment_url + "\n" + cq, encoding="utf-8")
    (root / "profile.txt").write_text(source["author_url"] + "\n" + pq, encoding="utf-8")
    stamp = "2026-10-16T10:00:00+08:00"
    row = {"source_id": sid, "channel_id": channel,
           "comment": {"url": comment_url, "evidence_file": "validation-evidence/comment.txt", "quote": cq, "observed_at": stamp},
           "profile": {"url": source["author_url"], "evidence_file": "validation-evidence/profile.txt", "quote": pq, "observed_at": stamp}}
    return ledger, task, sid, row


def test_identity_import_preserves_original_and_invalidates_semantic_review(tmp_path):
    ledger, task, sid, row = identity_fixture(tmp_path)
    before = demands.review_fingerprint(task, ledger)
    assert source_identity.checks(demands.references(task, ledger))[0]["missing"] == ["channel_identity", "comment_locator"]
    source_identity.apply(ledger, [row], [sid], tmp_path, DAY.isoformat())
    source = ledger["sources"][sid]["source"]
    assert source["author"] == "youtube:" + row["channel_id"] and "lc=" in source["url"]
    assert ledger["sources"][sid]["source_history"][0]["author"] == ""
    assert demands.review_fingerprint(task, ledger) != before
    assert not task_research._reviewed(task, ledger)
    assert source_identity.checks(demands.references(task, ledger)) == []
    old_raw = ledger["sources"][sid]["source_history"][0]
    # This fixture's source id predates its URL change; use the original hash
    # to reproduce a provider re-observation of a real saved comment.
    old_sid = demands.source_id(old_raw)
    ledger["sources"][old_sid] = ledger["sources"][sid]
    demands._register_sources(ledger, [old_raw], "2026-10-17")
    assert ledger["sources"][old_sid]["source"]["author"] == "youtube:" + row["channel_id"]


@pytest.mark.parametrize("bad", ["other_video", "fabricated_quote", "other_profile", "no_time", "unknown_channel", "out_of_scope"])
def test_identity_cannot_be_invented_or_taken_from_other_comment(tmp_path, bad):
    ledger, task, sid, row = identity_fixture(tmp_path)
    if bad == "other_video": row["comment"]["url"] = "https://youtu.be/ZZZZZZZZZZZ"
    if bad == "fabricated_quote": row["comment"]["quote"] = "A different comment"
    if bad == "other_profile": row["profile"]["url"] = "https://www.youtube.com/@other"
    if bad == "no_time": row["comment"]["observed_at"] = "2026-10-16T10:00:00"
    if bad == "unknown_channel": row["channel_id"] = "@display_name"
    if bad == "out_of_scope": row["source_id"] = "foreign"
    before = copy.deepcopy(ledger)
    with pytest.raises(ValueError):
        source_identity.apply(ledger, [row], [sid], tmp_path, DAY.isoformat())
    assert ledger == before


def test_extract_only_preserves_all_sources_and_cannot_publish(tmp_path, monkeypatch):
    evidence = [{"id": "e1", "url": "https://example.com/1", "text": "A long original " + "x" * 50000,
                 "author": "x:person", "kind": "post"}]
    calls = []
    def chat(model, system, text, **kwargs):
        pack = json.loads(text)
        calls.append((system, pack))
        assert len(pack[0]["segments"]["0"]) > 0
        return json.dumps({"rows": [{"id": p["id"], "category": "other", "reason": "No concrete task", "tasks": []} for p in pack]})
    monkeypatch.setattr(aisa, "chat", chat)
    ledger_path = tmp_path / "demand-ledger.json"
    result = demands.process("qwen3.7-flash", evidence, ledger_path, DAY, mode="extract_only")
    assert len(calls) == 1 and "初次需求提取" in calls[0][0]
    saved = demands.load(ledger_path)
    assert next(iter(saved["sources"].values()))["source"]["text"] == evidence[0]["text"]
    assert result["opportunities"] == [] and result["analysis_errors"]
    assert result["coverage"]["audit_complete"] == 0


def test_long_sources_have_own_batch_without_truncating_neighbors():
    ledger = {"sources": {str(i): {"source": {"text": "x" * n}} for i, n in enumerate([10, 100000, 10])}}
    assert list(demands._source_batches(ledger, ["0", "1", "2"])) == [["0"], ["1"], ["2"]]
