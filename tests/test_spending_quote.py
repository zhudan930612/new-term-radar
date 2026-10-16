"""Current AIsa batch_quote contract; no provider calls in these tests."""
import copy

import pytest

from radar import aisa, spending


def calls():
    return [{"call_id": "r", "tool": "get_reddit_search", "arguments": {"query": "invoice export", "trim": "false"}},
            {"call_id": "x", "tool": "get_twitter_tweet_advanced_search", "arguments": {"query": "invoice export"}}]


def quoted(kind="estimate", amount=1880, maximum=None):
    price = {"object": "cost_estimate", "currency": "USD", "estimate_kind": kind,
             "estimated_at": "2026-10-16T01:07:47Z", "estimated_cost_micros_usd": amount,
             "may_exceed_estimate": kind == "estimate"}
    if kind != "estimate":
        price["max_cost_micros_usd"] = amount if maximum is None else maximum
    return price


def response():
    return {"total_count": 2, "success_count": 2, "error_count": 0,
            "results": [{"call_id": c["call_id"], "tool": c["tool"], "successful": True,
                         "data": quoted()} for c in calls()]}


def test_batch_quote_sends_exact_calls_and_sums_reordered_mixed_bounds(monkeypatch):
    payload = response()
    payload["results"][0]["data"] = quoted("upper_bound", 1880, 4000)
    payload["results"][1]["data"] = quoted("exact", 1000)
    payload["results"].reverse()
    observed = []
    monkeypatch.setattr(spending, "control", lambda requests: observed.append(copy.deepcopy(requests)) or [payload])
    assert spending.quote(calls()) == .005
    assert observed == [[("batch_quote", {"calls": calls()})]]


def test_estimate_quote_uses_estimate_without_execution(monkeypatch):
    monkeypatch.setattr(spending, "control", lambda requests: [response()])
    monkeypatch.setattr(aisa, "_send", lambda *a: pytest.fail("Quote must not execute"))
    assert spending.quote(calls()) == .00376


def test_explicit_zero_quote_is_allowed(monkeypatch):
    payload = response()
    for row in payload["results"]:
        row["data"] = quoted("exact", 0)
    monkeypatch.setattr(spending, "control", lambda requests: [payload])
    assert spending.quote(calls()) == 0


@pytest.mark.parametrize("bad", ["missing_result", "extra_result", "duplicate_id", "foreign_id", "wrong_tool",
                                "missing_tool", "failed", "truthy_success", "error", "missing_data", "not_object",
                                "missing_count", "partial_count", "bool_count", "top_error"])
def test_incomplete_or_unattributable_batch_is_rejected(monkeypatch, bad):
    payload = response()
    row = payload["results"][0]
    if bad == "missing_result": payload["results"].pop()
    if bad == "extra_result": payload["results"].append(copy.deepcopy(row))
    if bad == "duplicate_id": row["call_id"] = "x"
    if bad == "foreign_id": row["call_id"] = "not_requested"
    if bad == "wrong_tool": row["tool"] = "get_twitter_user_last_tweets"
    if bad == "missing_tool": row.pop("tool")
    if bad == "failed": row["successful"] = False
    if bad == "truthy_success": row["successful"] = 1
    if bad == "error": row["error"] = {"type": "upstream"}
    if bad == "missing_data": row.pop("data")
    if bad == "not_object": payload["results"][0] = None
    if bad == "missing_count": payload.pop("total_count")
    if bad == "partial_count": payload["success_count"] = 1
    if bad == "bool_count": payload["error_count"] = False
    if bad == "top_error": payload["error"] = {"type": "authentication"}
    monkeypatch.setattr(spending, "control", lambda requests: [payload])
    with pytest.raises(aisa.AisaError):
        spending.quote(calls())


@pytest.mark.parametrize("field,value", [
    ("object", "price"), ("currency", "EUR"), ("currency", None),
    ("estimated_cost_micros_usd", None), ("estimated_cost_micros_usd", True),
    ("estimated_cost_micros_usd", "1880"), ("estimated_cost_micros_usd", -1),
    ("estimated_cost_micros_usd", float("nan")), ("estimated_cost_micros_usd", float("inf")),
    ("estimate_kind", "unknown"), ("estimate_kind", []), ("may_exceed_estimate", 1), ("may_exceed_estimate", False),
    ("max_cost_micros_usd", None), ("max_cost_micros_usd", 4000),
])
def test_malformed_price_is_not_free(monkeypatch, field, value):
    payload = response()
    payload["results"][0]["data"][field] = value
    monkeypatch.setattr(spending, "control", lambda requests: [payload])
    with pytest.raises(aisa.AisaError):
        spending.quote(calls())


@pytest.mark.parametrize("kind,maximum", [("exact", 2000), ("upper_bound", 1000),
                                         ("exact", True), ("upper_bound", float("inf")),
                                         ("exact", "1880"), ("upper_bound", -1)])
def test_bound_contract_is_strict(monkeypatch, kind, maximum):
    payload = response()
    payload["results"][0]["data"] = quoted(kind, 1880, maximum)
    monkeypatch.setattr(spending, "control", lambda requests: [payload])
    with pytest.raises(aisa.AisaError):
        spending.quote(calls())


@pytest.mark.parametrize("kind", ["exact", "upper_bound"])
def test_missing_bound_or_exceed_flag_rejected(monkeypatch, kind):
    payload = response()
    payload["results"][0]["data"] = quoted(kind)
    payload["results"][0]["data"].pop("max_cost_micros_usd")
    monkeypatch.setattr(spending, "control", lambda requests: [payload])
    with pytest.raises(aisa.AisaError):
        spending.quote(calls())
    payload["results"][0]["data"] = quoted(kind)
    payload["results"][0]["data"]["may_exceed_estimate"] = True
    with pytest.raises(aisa.AisaError):
        spending.quote(calls())


@pytest.mark.parametrize("payload", [[], [None], [response(), response()]])
def test_control_response_count_is_checked(monkeypatch, payload):
    monkeypatch.setattr(spending, "control", lambda requests: payload)
    with pytest.raises(aisa.AisaError):
        spending.quote(calls())


def test_quote_failure_reserves_nothing_and_never_sends(tmp_path, monkeypatch):
    payload = response()
    payload["results"][0]["data"].pop("estimated_cost_micros_usd")
    monkeypatch.setattr(spending, "control", lambda requests: [payload])
    journal = spending.Journal(tmp_path, .2, {}, 1)
    with pytest.raises(aisa.AisaError) as exc:
        journal.request(aisa.ROUTER, {"calls": calls()}, 1, lambda *a: pytest.fail("No paid send"))
    assert exc.value.request_sent is False
    assert journal.entries == [] and journal.reserved == 0


@pytest.mark.parametrize("bad_calls", [[], [calls()[0], calls()[0]],
                                     [{"call_id": "bad", "tool": "write_anything", "arguments": {}}]])
def test_invalid_or_unauthorized_requests_rejected_before_quote(monkeypatch, bad_calls):
    monkeypatch.setattr(spending, "control", lambda *a: pytest.fail("No quote"))
    with pytest.raises(aisa.AisaError):
        spending.quote(bad_calls)
