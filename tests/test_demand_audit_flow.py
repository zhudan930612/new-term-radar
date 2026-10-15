"""Local handoff integration: recover errors, merge independent evidence, gate publishing."""
import json

from radar import demand_audit, demands, spending


def test_semantic_import_can_recover_model_failure_and_merge_true_same_job(tmp_path):
    day = "2026-10-15"
    ledger = {"version": 2, "sources": {}, "tasks": {}}
    rows = []
    dispositions = []
    for n in (1, 2):
        source = {"id": f"e{n}", "kind": "post", "author": f"x:person{n}",
                  "url": f"https://example.com/{n}", "text": f"I split client {n} invoices by customer manually every week."}
        sid = demands.source_id(source)
        atom = {key: "未知" for key in demands.TASK_FIELDS}
        atom.update(job="按客户拆分发票PDF", pain="每周手动拆分", anchor=source["text"], search_terms=["split invoices by customer"])
        rows.append({"id": sid, "category": "need", "reason": "Concrete personal task", "tasks": [atom]})
        ledger["sources"][sid] = {"source": source, "first_seen": day, "last_seen": day, "analysis_error": "model failure"}
        dispositions.append({"id": sid, "original_id": f"e{n}", "url": source["url"], "category": "analysis_pending", "reason": "pending"})
    path, ledger_path, payload_path = tmp_path / "result.json", tmp_path / "ledger.json", tmp_path / "payload.json"
    spending.save(ledger_path, ledger)
    spending.save(path, {"date": day, "workflow_version": 2, "status": "incomplete", "semantic_audit": {"status": "pending"},
                         "dispositions": dispositions, "evidence": [e["source"] for e in ledger["sources"].values()],
                         "opportunities": [], "tasks": [], "coverage": {"current_sources": 2, "review_due": 0},
                         "zero_result_audit": {"status": "incomplete"}, "analysis_errors": ["model failure"], "errors": ["model failure"]})
    demand_audit.command("claim", path, ledger_path)
    spending.save(payload_path, {"date": day, "reviewer": "Codex", "method": "all originals read", "rows": rows})
    demand_audit.command("sources", path, ledger_path, payload_path)
    revised = demands.load(ledger_path)
    task_ids = sorted(revised["tasks"])
    proposal = {key: "Specific proposed experiment" for key in
                ("title", "user", "job", "pain", "web_advantage", "input", "output", "mvp", "gap",
                 "constraints", "validation", "success_condition", "stop_condition")}
    proposal["keywords"] = ["split invoices by customer"]
    spending.save(payload_path, {"date": day, "reviewer": "Codex", "method": "check narrow job and distinct accounts",
        "groups": [{"ids": task_ids, "same_job": True, "same_constraints": True, "reason": "Same job"}],
        "reviews": [{"id": task_ids[0], "same_job": True, "firsthand": True, "independent": True,
                     "web_fit": "browser", "bounded": True, "reason": "Two independent concrete requests", "proposal": proposal}]})
    demand_audit.command("tasks", path, ledger_path, payload_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["status"] == "complete" and saved["errors"] == []
    assert len(saved["opportunities"]) == 1
    assert saved["opportunity_validation"]["status"] == "pending"
    assert saved["google_trends"]["status"] == "waiting_validation"
    task = next(iter(demands.load(ledger_path)["tasks"].values()))
    assert len(task["source_ids"]) == 2 and task["last_shortlisted"] == day
    assert not demand_audit.due_tasks(demands.load(ledger_path), "2026-10-16")
