"""Independent review regressions; all model/network outputs are local fixtures."""
import io
import json
from datetime import date
import pytest

from radar import demand_audit, demand_sources, demands, opportunity_validation, opportunities, spending


DAY = date(2026, 10, 15)


def ledger_with_tasks(count=2):
    ledger = {"version": demands.VERSION, "sources": {}, "tasks": {}}
    for i in range(count):
        source = {"id": f"e{i}", "url": f"https://example.com/post/{i}",
                  "text": f"I need a distinct concrete workflow number {i} every week.",
                  "kind": "post", "author": f"x:person{i}"}
        sid = demands.source_id(source)
        atom = {k: "Unknown" for k in demands.TASK_FIELDS}
        atom.update(job=f"Distinct task {i}", anchor=source["text"], search_terms=[f"task {i}"])
        assessment = {"category": "need", "reason": "Concrete request", "tasks": [atom]}
        ledger["sources"][sid] = {"source": source, "assessment": assessment,
                                   "audit": assessment.copy(), "first_seen": DAY.isoformat(),
                                   "last_seen": DAY.isoformat()}
    demands.materialize(ledger)
    return ledger


def test_hn_item_query_urls_survive_collection(monkeypatch):
    """HN's canonical URL contains ?id=; a successful fetch must not lose all hits."""
    monkeypatch.setattr(demand_sources.opportunities, "tweets", lambda *_: [])
    monkeypatch.setattr(demand_sources.opportunities, "collect", lambda *_: ([], []))
    monkeypatch.setattr(demand_sources.breakout, "_get", lambda *_: {"items": []})
    response = {"hits": [{"objectID": "123456", "author": "human",
                          "comment_text": "I manually reconcile invoices every week.",
                          "created_at": "2026-10-14T10:00:00Z"}]}
    monkeypatch.setattr(demand_sources.urllib.request, "urlopen",
                        lambda *a, **kw: io.StringIO(json.dumps(response)))
    evidence, errors, _ = demand_sources.collect([], DAY, 0, [], True)
    assert not errors
    assert any(e["url"] == "https://news.ycombinator.com/item?id=123456" for e in evidence)


def test_rejected_merge_restores_original_task_seeds(monkeypatch):
    """A skeptical same_job=false review must not strand distinct seeds in one cluster."""
    ledger = ledger_with_tasks()
    original = set(ledger["tasks"])
    outputs = iter([{"groups": [{"ids": sorted(original), "same_job": True,
                                  "same_constraints": True, "reason": "Model made a bad merge"}]}])
    monkeypatch.setattr(demands.aisa, "chat", lambda *a, **kw: json.dumps(next(outputs)))
    demands.cluster("test", ledger, list(ledger["tasks"].values()))
    reviews = [{"id": t["id"], "same_job": False, "firsthand": True,
                "independent": True, "web_fit": "browser", "bounded": True,
                "reason": "These are different jobs", "proposal": None}
               for t in ledger["tasks"].values()]
    monkeypatch.setattr(demands.aisa, "chat", lambda *a, **kw: json.dumps({"reviews": reviews}))
    proposals, _ = demands.review("test", ledger, list(ledger["tasks"].values()), DAY.isoformat())
    assert not proposals
    assert original <= set(ledger["tasks"])


def test_empty_handle_cannot_supply_second_user(monkeypatch):
    ledger = ledger_with_tasks()
    entries = list(ledger["sources"].values())
    entries[0]["source"]["author"] = "x:"
    tasks = list(ledger["tasks"].values())
    target = tasks[0]
    target["source_ids"] += tasks[1]["source_ids"]
    target["atoms"] += tasks[1]["atoms"]
    proposal = {k: "Concrete field" for k in
                ("title", "user", "job", "pain", "web_advantage", "input", "output", "mvp", "gap",
                 "constraints", "validation", "success_condition", "stop_condition")}
    proposal["keywords"] = ["batch invoice reconciliation"]
    row = {"id": target["id"], "same_job": True, "firsthand": True, "independent": True,
           "web_fit": "browser", "bounded": True, "reason": "Model claims two users", "proposal": proposal}
    monkeypatch.setattr(demands.aisa, "chat", lambda *a, **kw: json.dumps({"reviews": [row]}))
    proposals, _ = demands.review("test", ledger, [target], DAY.isoformat())
    assert not proposals
    assert target["status"] != "值得深入验证"
    entries[0]["source"]["author"] = "x:real_person"
    accepted, _ = demands.review("test", ledger, [target], DAY.isoformat())
    assert len(accepted) == 1, "Positive control: all other required fields were valid"


def test_unreviewed_backlog_does_not_claim_complete_zero_audit(monkeypatch, tmp_path):
    ledger = ledger_with_tasks(19)
    path = tmp_path / "ledger.json"
    spending.save(path, ledger)
    monkeypatch.setattr(demands, "cluster", lambda *a: None)

    def fake_review(model, ledger, tasks, day):
        for task in tasks:
            task["reviewed"] = day
            task["status"] = "待补证"
            task["review_fingerprint"] = demands.review_fingerprint(task, ledger)
        return [], []

    monkeypatch.setattr(demands, "review", fake_review)
    current_source = next(iter(ledger["sources"].values()))["source"]
    result = demands.process("test", [current_source], path, DAY)
    assert result["coverage"]["deferred_tasks"] == 1
    assert result["zero_result_audit"]["status"] != "complete"


def test_nested_rejected_merges_preserve_all_leaf_seeds(monkeypatch):
    ledger = ledger_with_tasks(3)
    leaves = set(ledger["tasks"])
    first_pair = sorted(leaves)[:2]

    def merge(ids):
        payload = {"groups": [{"ids": ids, "same_job": True, "same_constraints": True,
                                "reason": "Synthetic proposed merge"}]}
        monkeypatch.setattr(demands.aisa, "chat", lambda *a, **kw: json.dumps(payload))
        demands.cluster("test", ledger, list(ledger["tasks"].values()))

    def reject(task):
        payload = {"reviews": [{"id": task["id"], "same_job": False,
                                 "firsthand": True, "independent": True, "web_fit": "browser",
                                 "bounded": True, "reason": "Distinct jobs", "proposal": None}]}
        monkeypatch.setattr(demands.aisa, "chat", lambda *a, **kw: json.dumps(payload))
        demands.review("test", ledger, [task], DAY.isoformat())

    merge(first_pair)
    inner = next(t for t in ledger["tasks"].values() if t.get("merge_members"))
    inner_id, inner_atoms = inner["id"], set(inner["atoms"])
    merge(list(ledger["tasks"]))
    reject(next(iter(ledger["tasks"].values())))
    assert set(ledger["tasks"][inner_id]["atoms"]) == inner_atoms
    assert len(ledger["tasks"][inner_id]["merge_members"]) == 2
    reject(ledger["tasks"][inner_id])
    assert set(ledger["tasks"]) == leaves
    assert all(len(t["source_ids"]) == 1 for t in ledger["tasks"].values())
    merge(first_pair)
    assert set(ledger["tasks"]) == leaves, "Rejected pair must not be silently re-merged"


def test_unchanged_large_pool_is_not_re_reviewed_every_day(monkeypatch, tmp_path):
    ledger = ledger_with_tasks(30)
    for task in ledger["tasks"].values():
        task["reviewed"] = "2026-10-14"
        task["review_fingerprint"] = demands.review_fingerprint(task, ledger)
    path = tmp_path / "ledger.json"
    spending.save(path, ledger)
    monkeypatch.setattr(demands, "cluster", lambda *a: (_ for _ in ()).throw(AssertionError("Unchanged pool must not re-cluster")))
    reviewed = []

    def fake_review(model, ledger, tasks, day):
        reviewed.extend(tasks)
        return [], []

    monkeypatch.setattr(demands, "review", fake_review)
    source = next(iter(ledger["sources"].values()))["source"]
    result = demands.process("test", [source], path, DAY)
    assert not reviewed
    assert result["coverage"]["deferred_tasks"] == 0
    assert result["zero_result_audit"]["status"] == "complete"


def test_old_failed_source_is_repaired_without_search_rediscovery(monkeypatch, tmp_path):
    ledger = ledger_with_tasks()
    old_sid, fresh_sid = list(ledger["sources"])
    old = ledger["sources"][old_sid]
    assessment = old.pop("assessment")
    old.pop("audit")
    old["analysis_error"] = "Earlier model transport error"
    old["last_seen"] = "2026-10-14"
    path = tmp_path / "ledger.json"
    spending.save(path, ledger)
    called = []

    def fake_classify(model, ledger, ids, *, audit, day, persist):
        called.extend(ids)
        for sid in ids:
            assert sid == old_sid
            entry = ledger["sources"][sid]
            entry["assessment"] = assessment
            if audit:
                entry["audit"] = {**assessment, "date": day}
            entry.pop("analysis_error", None)
        persist()
        return []

    def fake_review(model, ledger, tasks, day):
        for task in tasks:
            task["reviewed"] = day
            task["review_fingerprint"] = demands.review_fingerprint(task, ledger)
        return [], []

    monkeypatch.setattr(demands, "_classify", fake_classify)
    monkeypatch.setattr(demands, "cluster", lambda *a: None)
    monkeypatch.setattr(demands, "review", fake_review)
    result = demands.process("test", [ledger["sources"][fresh_sid]["source"]], path, DAY)
    assert old_sid in called
    assert result["coverage"]["historical_repair_sources"] == 1
    assert demands.load(path)["sources"][old_sid]["last_seen"] == "2026-10-14"


def test_two_tasks_sharing_one_literal_span_are_both_retained():
    source = {"id": "multi", "url": "https://example.com/two-tasks", "kind": "post", "author": "x:alice",
              "text": "Every week I export invoices to CSV and separately redact client emails before sharing them."}
    sid = demands.source_id(source)
    tasks = []
    for job, output in [("Export invoices to CSV", "CSV of invoice rows"),
                        ("Redact client email addresses", "Document without email addresses")]:
        atom = {k: "Unknown" for k in demands.TASK_FIELDS}
        atom.update(job=job, output=output, anchor=source["text"], search_terms=[job])
        tasks.append(atom)
    ledger = {"version": demands.VERSION, "sources": {sid: {"source": source,
              "assessment": {"category": "need", "reason": "Two distinct tasks", "tasks": tasks},
              "first_seen": DAY.isoformat(), "last_seen": DAY.isoformat()}}, "tasks": {}}
    demands.materialize(ledger)
    assert {t["job"] for t in ledger["tasks"].values()} == {t["job"] for t in tasks}
    assert len(ledger["tasks"]) == 2
    assert all(demands.references(t, ledger) for t in ledger["tasks"].values())


def test_same_quote_semantic_revision_invalidates_cached_review():
    ledger = ledger_with_tasks(1)
    task = next(iter(ledger["tasks"].values()))
    task["reviewed"] = DAY.isoformat()
    before = demands.review_fingerprint(task, ledger)
    task["review_fingerprint"] = before
    assert not demands.review_due(task, ledger, DAY.isoformat())
    source = next(iter(ledger["sources"].values()))
    # The source already says "every week"; a later audit corrects an Unknown field.
    source["assessment"]["tasks"][0]["frequency"] = "Every week"
    demands.materialize(ledger)
    current = list(ledger["tasks"].values())
    assert len(current) == 1, "A revision must not create a second independent task vote"
    assert current[0]["frequency"] == "Every week"
    assert demands.review_fingerprint(current[0], ledger) != before
    assert demands.review_due(current[0], ledger, DAY.isoformat())


def test_semantic_task_import_cannot_skip_whole_source_review(tmp_path):
    ledger = ledger_with_tasks(1)
    sid, entry = next(iter(ledger["sources"].items()))
    entry["assessment"] = {"category": "other", "reason": "Preprocessor says irrelevant", "tasks": []}
    entry["audit"] = entry["assessment"].copy()
    ledger["tasks"] = {}
    result = {"date": DAY.isoformat(), "workflow_version": 2, "status": "awaiting_audit",
              "semantic_audit": {"status": "running"},
              "dispositions": [{"id": sid, "category": "other", "reason": "provisional", "url": entry["source"]["url"]}],
              "coverage": {}, "zero_result_audit": {"status": "incomplete"},
              "errors": [], "analysis_errors": [], "opportunities": []}
    ledger_path, result_path, observations = tmp_path / "ledger.json", tmp_path / "result.json", tmp_path / "tasks.json"
    spending.save(ledger_path, ledger)
    spending.save(result_path, result)
    spending.save(observations, {"date": DAY.isoformat(), "reviewer": "independent-agent",
                                "method": "task review only", "reviews": []})
    with pytest.raises(ValueError):
        demand_audit.command("tasks", result_path, ledger_path, observations)
    assert json.loads(result_path.read_text(encoding="utf-8"))["semantic_audit"]["status"] == "running"


def test_original_source_contradiction_withdraws_positive_report(tmp_path):
    sources = [{"url": f"https://example.com/source/{i}", "id": f"s{i}", "author": f"x:person{i}",
                "quote": "A concrete source quotation."} for i in range(2)]
    result = {"date": DAY.isoformat(), "workflow_version": 2, "status": "complete", "errors": [],
              "opportunities": [{"title": "Original proposal", "task_id": "d1", "status": "值得深入验证",
                                  "keywords": ["batch invoice export"], "evidence": sources}],
              "tasks": [], "dispositions": [], "coverage": {}}
    opportunity_validation.prepare(result)
    plan = result["opportunity_validation"]["items"][0]
    directory = tmp_path / "validation-evidence"
    directory.mkdir()

    def saved(url, name):
        quote = "Context shows this source does not support the same task."
        path = directory / (name + ".txt")
        path.write_text(url + "\n" + quote, encoding="utf-8")
        return {"url": url, "evidence_file": "validation-evidence/" + path.name, "quote": quote}

    observation = {"id": plan["id"], "status": "observed", "observed_at": "2026-10-15T12:00:00+08:00",
                   "note": "Original context contradicts the proposed common task.",
                   "same_task": False, "independent_requests": True, "intent_match": True,
                   "sources": [saved(s["url"], f"source{i}") for i, s in enumerate(sources)],
                   "search": saved(plan["search_url"], "search"), "alternatives": []}
    opportunity_validation.import_observations(result, {"date": DAY.isoformat(), "items": [observation]}, tmp_path)
    assert not result["opportunity_validation"]["items"][0]["trends_ready"]
    assert "今天发现 1 个值得深入验证" not in opportunities.render(result)


def test_semantic_import_keeps_unreviewed_historical_source_gap(tmp_path):
    ledger = ledger_with_tasks(2)
    reviewed_sid, outside_sid = list(ledger["sources"])
    reviewed = ledger["sources"][reviewed_sid]
    reviewed["assessment"] = {"category": "other", "reason": "Read source has no usable task", "tasks": []}
    reviewed["audit"] = reviewed["assessment"].copy()
    reviewed["semantic_review"] = {"date": DAY.isoformat(), "reviewer": "independent-agent"}
    outside = ledger["sources"][outside_sid]
    outside.pop("assessment")
    outside.pop("audit")
    outside["analysis_error"] = "Historical source not yet processed"
    ledger["tasks"] = {}
    result = {"date": DAY.isoformat(), "workflow_version": 2, "status": "incomplete",
              "semantic_audit": {"status": "tasks_pending"},
              "dispositions": [{"id": reviewed_sid, "category": "other", "reason": "Reviewed",
                                "url": reviewed["source"]["url"]}],
              "coverage": {"current_sources": 1}, "zero_result_audit": {"status": "incomplete"},
              "errors": ["Historical queue was capped"], "analysis_errors": ["Historical queue was capped"],
              "opportunities": [], "evidence": [reviewed["source"]]}
    ledger_path, result_path, observations = tmp_path / "ledger.json", tmp_path / "result.json", tmp_path / "tasks.json"
    spending.save(ledger_path, ledger)
    spending.save(result_path, result)
    spending.save(observations, {"date": DAY.isoformat(), "reviewer": "independent-agent",
                                "method": "All in-scope source and task review", "reviews": []})
    demand_audit.command("tasks", result_path, ledger_path, observations)
    saved = json.loads(result_path.read_text(encoding="utf-8"))
    assert saved["status"] == "incomplete"
    assert saved["zero_result_audit"]["status"] == "incomplete"
    assert any("历史来源" in error for error in saved["errors"])
