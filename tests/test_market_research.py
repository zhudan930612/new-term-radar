import copy
import json

import pytest

from radar import aisa, competition, config, market_research as research, spending

MANAGED_POST = aisa._post

def sample():
    return {"date": "2026-10-15", "status": "complete", "workflow_version": 2,
            "semantic_audit": {"status": "complete"},
            "opportunities": [{"title": "Invoice task", "keywords": ["split invoices"]}],
            "opportunity_validation": {"status": "complete", "items": [
                {"status": "observed", "trends_ready": True, "opportunity_index": 0, "keyword": "split invoices"}]}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = copy.deepcopy(config.DEFAULTS)
    calls = []
    # Keep the production managed-request path, replacing only its network send.
    monkeypatch.setattr(aisa, "_post", MANAGED_POST)
    monkeypatch.setattr(research.goanyapi, "key", lambda: "")
    monkeypatch.setattr(research.revenue, "key", lambda: "")
    monkeypatch.setattr(spending, "account", lambda: {"balance_usd": 10})
    monkeypatch.setattr(spending, "model_prices", lambda model: {"input": .1, "output": .2})
    monkeypatch.setattr(spending, "quote", lambda calls: .001)
    monkeypatch.setattr(research.google_trends, "write_result", lambda path, result, output: spending.save(path, result))

    def send(url, body, timeout):
        assert url == aisa.ROUTER
        replies = []
        for call in body["calls"]:
            calls.append(call)
            query = call["arguments"]["body"][0]
            if call["tool"] == competition.SERP_TOOL:
                items = [{"type": "organic", "domain": "invoices.example", "url": "https://invoices.example/tool",
                          "title": "Split invoices online", "rank_group": 1, "description": "Original SERP evidence"}]
            else:
                items = [{"keyword": k, "keyword_properties": {"keyword_difficulty": 42}} for k in query["keywords"]]
            replies.append({"call_id": call["call_id"], "successful": True, "customer_cost_micros_usd": 1000,
                            "data": {"tasks": [{"result": [{"items": items}]}]}})
        return {"results": replies}

    monkeypatch.setattr(aisa, "_send", send)
    path = tmp_path / "web-opportunities" / "2026-10-15" / "result.json"
    spending.save(path, sample())
    return path, cfg, calls


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("gate", ["unfinished", "unvalidated", "not_ready", "withdrawn", "no_opportunity", "bad_keyword", "stale_keyword", "legacy", "unaudited"])
def test_no_queries_before_context_and_intent_gates(setup, tmp_path, monkeypatch, gate):
    path, cfg, calls = setup
    result = read(path)
    if gate == "unfinished": result["status"] = "awaiting_audit"
    if gate == "legacy": result.pop("workflow_version")
    if gate == "unaudited": result.pop("semantic_audit")
    if gate == "unvalidated": result["opportunity_validation"]["status"] = "running"
    if gate == "not_ready": result["opportunity_validation"]["items"][0]["trends_ready"] = False
    if gate == "withdrawn": result["opportunities"][0]["review_status"] = "withdrawn"
    if gate == "no_opportunity": result["opportunities"] = []
    if gate == "bad_keyword": result["opportunity_validation"]["items"][0]["keyword"] = "wrong\nterm"
    if gate == "stale_keyword": result["opportunities"][0]["keywords"] = ["replacement keyword"]
    spending.save(path, result)
    monkeypatch.setattr(spending, "account", lambda: pytest.fail("No account request allowed before validation"))
    monkeypatch.setattr(research.revenue, "products", lambda *args: pytest.fail("No revenue call before validation"))
    assert research.command(path, cfg, tmp_path) == 0
    assert not calls
    assert read(path)["market_research"]["status"] == "skipped"
    assert read(path)["status"] == result["status"]


def test_managed_paid_evidence_optional_gaps_and_no_duplicate_calls(setup, tmp_path):
    path, cfg, calls = setup
    assert research.command(path, cfg, tmp_path) == 0
    result = read(path)
    stage = result["market_research"]
    assert stage["status"] == "partial"
    assert result["opportunities"] == sample()["opportunities"]
    assert stage["aisa_journal"]["actual_usd"] == .002
    assert len(read(path.parent / "api" / "requests.json")) == 2
    item = stage["items"][0]
    assert item["market_verified"] is False
    assert item["serp_results"][0]["description"] == "Original SERP evidence"
    assert read(path.parent / item["evidence_files"][0])["data"]["tasks"][0]["result"][0]["items"]
    assert item["kd_evidence_files"]
    assert set(item["missing"]) == {"domain_rating", "advertisers", "intitle"}
    assert item["competition"]["weak"] is None
    assert stage["revenue"]["reason"] == "missing_TRUSTMRR_API_KEY"
    assert item["revenue_context"]["status"] == "unknown"
    assert stage["external_billing"]["goanyapi_reported_usd"] == 0
    assert research.command(path, cfg, tmp_path) == 0
    assert len(calls) == 2


def test_provider_error_has_no_secret_text_and_cannot_retry(setup, tmp_path, monkeypatch):
    path, cfg, calls = setup
    attempts = []
    def fail(*args):
        attempts.append(1)
        raise aisa.AisaError("Authorization: Bearer example-sensitive-provider-message")
    monkeypatch.setattr(aisa, "_send", fail)
    assert research.command(path, cfg, tmp_path) == 2
    result = read(path)
    assert result["status"] == "complete"
    stage = result["market_research"]
    assert stage["status"] == "incomplete"
    assert stage["items"][0]["competition"]["verdict"] == "unknown"
    assert "example-sensitive" not in json.dumps(result)
    assert research.command(path, cfg, tmp_path) == 2
    assert len(attempts) == 1  # The journal stops KD after the failed SERP.


def test_revenue_is_once_and_only_exact_domain_background(setup, tmp_path, monkeypatch):
    path, cfg, calls = setup
    hits = []
    monkeypatch.setattr(research.revenue, "key", lambda: "configured")
    def products(*args):
        hits.append(1)
        return {"read": 2, "products": [{"name": "Invoice app", "website": "https://invoices.example", "usd": 500},
                                        {"name": "Split invoices", "website": "https://unrelated.example", "usd": 9000}]}
    monkeypatch.setattr(research.revenue, "products", products)
    research.command(path, cfg, tmp_path)
    stage = read(path)["market_research"]
    context = stage["items"][0]["revenue_context"]
    assert context["product_indexes"] == [0]
    assert context["same_task_verified"] is False
    assert context["market_verified"] is False
    research.command(path, cfg, tmp_path)
    assert len(hits) == 1


def test_empty_serp_and_missing_kd_are_unknown_not_low_competition(setup, tmp_path, monkeypatch):
    path, cfg, calls = setup
    def empty(url, body, timeout):
        return {"results": [{"call_id": c["call_id"], "successful": True,
                             "data": {"tasks": [{"result": [{"items": []}]}]}} for c in body["calls"]]}
    monkeypatch.setattr(aisa, "_send", empty)
    assert research.command(path, cfg, tmp_path) == 2
    item = read(path)["market_research"]["items"][0]
    assert item["competition"]["verdict"] == "unknown"
    assert {"organic_results", "keyword_difficulty"} <= set(item["missing"])
    assert item["evidence_files"]  # An empty provider response remains inspectable.


@pytest.mark.parametrize("existing", ["complete", "partial", "incomplete", "running"])
def test_existing_stage_never_automatically_repeats(setup, tmp_path, monkeypatch, existing):
    path, cfg, calls = setup
    result = read(path)
    result["market_research"] = {"status": existing, "items": []}
    spending.save(path, result)
    monkeypatch.setattr(spending, "account", lambda: pytest.fail("Terminal/running stage cannot reenter"))
    research.command(path, cfg, tmp_path)
    assert not calls


@pytest.mark.parametrize("lock_name", ["run.lock", "trends-edit.lock"])
def test_shared_lock_blocks_research(setup, tmp_path, lock_name):
    path, cfg, calls = setup
    (path.parent / lock_name).touch()
    with pytest.raises((ValueError, FileExistsError)):
        research.command(path, cfg, tmp_path)
    assert not calls


def test_at_most_three_validated_opportunities_and_no_duplicate_validation_indices():
    result = sample()
    result["opportunities"] *= 5
    original = result["opportunity_validation"]["items"][0]
    result["opportunity_validation"]["items"] = [dict(original, opportunity_index=i) for i in [0, 0, 1, 2, 3, 4]]
    assert [r["opportunity_index"] for r in research.ready_items(result)] == [0, 1, 2]


def test_account_failure_is_saved_and_optional_revenue_error_is_redacted(setup, tmp_path, monkeypatch):
    path, cfg, calls = setup
    def account_failure():
        raise aisa.AisaError("private-account-error-body")
    def revenue_failure(*args):
        raise RuntimeError("private-trustmrr-error-body")
    monkeypatch.setattr(spending, "account", account_failure)
    monkeypatch.setattr(research.revenue, "key", lambda: "configured")
    monkeypatch.setattr(research.revenue, "products", revenue_failure)
    assert research.command(path, cfg, tmp_path) == 2
    stage = read(path)["market_research"]
    assert stage["errors"] == [{"error_type": "AisaError"}]
    assert stage["revenue"]["error_type"] == "RuntimeError"
    assert "private-" not in json.dumps(stage)
    assert not calls


def test_no_daily_amount_cap_still_uses_account_balance_guard(setup, tmp_path):
    path, cfg, calls = setup
    cfg["opportunities"]["budget_usd"] = None
    assert research.command(path, cfg, tmp_path) == 0
    assert len(calls) == 2


def test_optional_metrics_complete_without_promoting_market_claims(setup, tmp_path, monkeypatch):
    path, cfg, calls = setup
    monkeypatch.setattr(research.goanyapi, "key", lambda: "configured")
    monkeypatch.setattr(competition, "ratings", lambda domains, state: ({"invoices.example": 25}, 2000))
    def supply(rows):
        for row in rows:
            row["competition"].update(built=12, paid=["invoices.example"])
        return 3000, []
    monkeypatch.setattr(competition, "supply", supply)
    monkeypatch.setattr(research.revenue, "key", lambda: "configured")
    monkeypatch.setattr(research.revenue, "products", lambda *args: {"read": 1, "products": []})
    assert research.command(path, cfg, tmp_path) == 0
    stage = read(path)["market_research"]
    assert stage["status"] == "complete"
    assert stage["items"][0]["missing"] == []
    assert stage["external_billing"]["goanyapi_reported_usd"] == .005
    assert stage["aisa_journal"]["actual_usd"] == .002
    assert stage["items"][0]["revenue_context"]["status"] == "unknown"
    assert stage["market_verified"] is False
