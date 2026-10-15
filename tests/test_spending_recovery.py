import copy
import hashlib
import json
import threading
import urllib.error

import pytest

from radar import aisa, spending


def body(query="one", count=1):
    return {"calls": [{"call_id": f"c{i}", "tool": "get_reddit_search", "arguments": {"query": query}}
                      for i in range(count)]}


def failure(request=None, *, retryable=True, cost=0, status=500, kind="upstream"):
    return {"results": [{"call_id": c["call_id"], "successful": False, "customer_cost_micros_usd": cost,
                          "error": {"type": kind, "status": status, "retryable": retryable}}
                         for c in (request or body())["calls"]]}


def success(request=None, cost=1000):
    return {"results": [{"call_id": c["call_id"], "successful": True, "customer_cost_micros_usd": cost, "data": {"items": []}}
                         for c in (request or body())["calls"]]}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    quoted, sleeps = [], []
    monkeypatch.setattr(spending, "quote", lambda calls: quoted.append(copy.deepcopy(calls)) or .002)
    monkeypatch.setattr(spending.time, "sleep", sleeps.append)
    return spending.Journal(tmp_path, .02, {"input": .2, "output": .7}, 1), quoted, sleeps


def test_transient_zero_charge_is_requoted_three_times_and_cached(setup, tmp_path):
    journal, quotes, sleeps = setup
    replies = iter([failure(), failure(status=429), success()])
    def send(*args):
        stored = read(journal.path)[0]
        assert stored["status"] == "pending"
        assert (tmp_path / stored["attempts"][-1]["request_file"]).exists()
        return next(replies)
    answer = journal.request(aisa.ROUTER, body(), 1, send)
    entry = journal.entries[0]
    assert answer == success()
    assert sleeps == [1, 2] and len(quotes) == 3
    assert [a["outcome"] for a in entry["attempts"]] == ["retryable_zero", "retryable_zero", "ok"]
    assert journal.reserved == .001 and entry["actual_usd"] == .001
    assert len(list(tmp_path.glob("*.attempt-*.request.json"))) == 3
    assert len(list(tmp_path.glob("*.attempt-*.response.json"))) == 3
    assert read(tmp_path / (entry["id"] + ".pre-recovery.json")) == failure()
    assert journal.request(aisa.ROUTER, body(), 1, lambda *a: pytest.fail("cached")) == answer
    assert len(quotes) == 3


def test_exhausted_source_does_not_stop_unrelated_work(setup):
    journal, quotes, sleeps = setup
    with pytest.raises(spending.JournalError) as error:
        journal.request(aisa.ROUTER, body(), 1, lambda *a: failure())
    assert error.value.failure_kind == "retry_exhausted" and error.value.request_sent is True
    assert len(quotes) == 3 and journal.reserved == 0
    with pytest.raises(spending.JournalError) as error:
        journal.request(aisa.ROUTER, body(), 1, lambda *a: pytest.fail("No fourth attempt"))
    assert error.value.failure_kind == "retry_disabled" and error.value.request_sent is False
    assert journal.request(aisa.ROUTER, body("other"), 1, lambda *a: success()) == success()
    assert len(quotes) == 4


@pytest.mark.parametrize("cost", [0, 1200])
def test_known_nonretryable_failure_is_local_and_charged_once(setup, cost):
    journal, quotes, _ = setup
    with pytest.raises(aisa.AisaError):
        journal.request(aisa.ROUTER, body(), 1, lambda *a: failure(retryable=False, cost=cost, status=400))
    assert len(quotes) == 1 and journal.reserved == cost / 1e6
    journal.request(aisa.ROUTER, body("another"), 1, lambda *a: success())
    assert journal.entries[0]["actual_usd"] == cost / 1e6


def test_known_mixed_batch_is_not_retried_but_independent_request_can_run(setup):
    journal, quotes, _ = setup
    request = body(count=2)
    response = success(request)
    response["results"][1] = failure(request)["results"][1]
    with pytest.raises(aisa.AisaError):
        journal.request(aisa.ROUTER, request, 1, lambda *a: response)
    assert len(quotes) == 1 and journal.reserved == .001
    assert journal.entries[0]["outcome"] == "source_failure"
    journal.request(aisa.ROUTER, body("another"), 1, lambda *a: success())


@pytest.mark.parametrize("mutation", ["missing_cost", "string_cost", "bool_cost", "nan_cost", "negative_cost",
                                      "missing_id", "wrong_id", "duplicate_ids", "missing_reply", "nonbool_success"])
def test_unknown_receipts_stop_every_source_without_retry(setup, mutation):
    journal, quotes, _ = setup
    request = body(count=2) if mutation == "duplicate_ids" else body()
    reply = failure(request)
    row = reply["results"][0]
    if mutation == "missing_cost": row.pop("customer_cost_micros_usd")
    if mutation == "string_cost": row["customer_cost_micros_usd"] = "0"
    if mutation == "bool_cost": row["customer_cost_micros_usd"] = False
    if mutation == "nan_cost": row["customer_cost_micros_usd"] = float("nan")
    if mutation == "negative_cost": row["customer_cost_micros_usd"] = -1
    if mutation == "missing_id": row.pop("call_id")
    if mutation == "wrong_id": row["call_id"] = "not_requested"
    if mutation == "duplicate_ids": reply["results"][1]["call_id"] = row["call_id"]
    if mutation == "missing_reply": reply["results"] = []
    if mutation == "nonbool_success": row["successful"] = 0
    with pytest.raises(aisa.AisaError) as error:
        journal.request(aisa.ROUTER, request, 1, lambda *a: reply)
    assert error.value.failure_kind == "unknown_outcome"
    assert len(quotes) == 1 and journal.reserved == .0025
    assert journal.entries[0]["outcome"] == "unknown"
    with pytest.raises(spending.JournalError) as error:
        journal.request(aisa.ROUTER, body("other"), 1, lambda *a: pytest.fail("Global block"))
    assert error.value.request_sent is False and error.value.failure_kind == "global_block"


@pytest.mark.parametrize("retryable,status,kind", [(1, 500, "upstream"), ("true", 500, "upstream"),
                                                   (True, "500", "upstream"), (True, 400, "upstream"),
                                                   (True, 500, "provider_unknown")])
def test_only_strict_transient_contract_licenses_retry(setup, retryable, status, kind):
    journal, quotes, _ = setup
    with pytest.raises(aisa.AisaError):
        journal.request(aisa.ROUTER, body(), 1, lambda *a: failure(retryable=retryable, status=status, kind=kind))
    assert len(quotes) == 1
    assert journal.entries[0]["outcome"] == "source_failure"


@pytest.mark.parametrize("status", [401, 402, 403])
def test_account_receipt_stops_all_sources(setup, status):
    journal, quotes, _ = setup
    with pytest.raises(aisa.AisaError):
        journal.request(aisa.ROUTER, body(), 1, lambda *a: failure(status=status))
    assert journal.entries[0]["outcome"] == "account_failure"
    with pytest.raises(spending.JournalError, match="further paid"):
        journal.request(aisa.ROUTER, body("other"), 1, lambda *a: pytest.fail("account block"))
    assert len(quotes) == 1


def test_timeout_and_wrapped_http_auth_are_never_retried(setup):
    journal, quotes, _ = setup
    def send(*args):
        try:
            raise urllib.error.HTTPError("https://example.invalid", 401, "private error", {}, None)
        except urllib.error.HTTPError as cause:
            raise aisa.AisaError("private error") from cause
    with pytest.raises(aisa.AisaError) as error:
        journal.request(aisa.ROUTER, body(), 1, send)
    assert error.value.request_sent is True and error.value.failure_kind == "account_failure"
    assert journal.entries[0]["outcome"] == "account_failure"
    assert "private error" not in json.dumps(journal.entries)
    assert len(quotes) == 1


def write_legacy(tmp_path, request, response):
    digest = spending._digest(aisa.ROUTER, request)
    entry = {"id": digest, "label": "get_reddit_search", "status": "failed", "reserved_usd": .01, "error_type": "AisaError"}
    spending.save(tmp_path / "requests.json", [entry])
    spending.save(tmp_path / (digest + ".request.json"), {"url": aisa.ROUTER, "body": request})
    spending.save(tmp_path / (digest + ".json"), response)
    return digest


def test_legacy_first_attempt_counts_and_original_failure_is_preserved(tmp_path, monkeypatch):
    digest = write_legacy(tmp_path, body(), failure())
    original = (tmp_path / (digest + ".json")).read_bytes()
    quotes = []
    monkeypatch.setattr(spending, "quote", lambda c: quotes.append(1) or .001)
    monkeypatch.setattr(spending.time, "sleep", lambda _: None)
    journal = spending.Journal(tmp_path, None, {}, .02)
    assert journal.reserved == 0 and journal.limit == .01
    assert journal.entries[0]["attempts"][0]["number"] == 1
    replies = iter([failure(), success(cost=100)])
    journal.request(aisa.ROUTER, body(), 1, lambda *a: next(replies))
    assert len(quotes) == 2 and len(journal.entries[0]["attempts"]) == 3
    assert (tmp_path / (digest + ".attempt-1.response.json")).read_bytes() == original
    assert (tmp_path / (digest + ".pre-recovery.json")).read_bytes() == original
    assert read(tmp_path / (digest + ".legacy-entry.json"))["status"] == "failed"
    resumed = spending.Journal(tmp_path, None, {}, .02)
    resumed.request(aisa.ROUTER, body(), 1, lambda *a: pytest.fail("must reuse success"))
    assert len(resumed.entries[0]["attempts"]) == 3


def test_legacy_wrong_request_hash_cannot_authorize_recovery(tmp_path, monkeypatch):
    digest = write_legacy(tmp_path, body(), failure())
    spending.save(tmp_path / (digest + ".request.json"), {"url": aisa.ROUTER, "body": body("altered")})
    journal = spending.Journal(tmp_path, None, {}, 1)
    assert journal.entries[0]["outcome"] == "unknown" and journal.reserved == .01
    with pytest.raises(aisa.AisaError):
        journal.request(aisa.ROUTER, body(), 1, lambda *a: pytest.fail("No recovery"))


@pytest.mark.parametrize("model", [False, True])
def test_pre_snapshot_success_keeps_exact_settlement_and_can_only_reuse_cache(tmp_path, model):
    request = {"model": "qwen3.7-flash", "enable_thinking": False, "messages": [], "max_tokens": 1} if model else body()
    url = aisa.CHAT if model else aisa.ROUTER
    digest = spending._digest(url, request)
    cached = {"choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}} if model else success()
    amount = .123456789 if model else .001
    entry = {"id": digest, "label": "qwen3.7-flash" if model else "get_reddit_search", "status": "ok",
             "reserved_usd": .2, "actual_usd": amount}
    spending.save(tmp_path / "requests.json", [entry])
    spending.save(tmp_path / (digest + ".json"), cached)
    journal = spending.Journal(tmp_path, None, {"input": 99, "output": 99}, 1)
    assert journal.entries[0]["status"] == "ok"
    assert journal.entries[0]["actual_usd"] == amount
    assert journal.entries[0]["attempts"][0]["legacy_request_missing"] is True
    assert "request_file" not in journal.entries[0]["attempts"][0]
    assert journal.request(url, request, 1, lambda *a: pytest.fail("Never resend")) == cached
    (tmp_path / (digest + ".json")).unlink()
    with pytest.raises(aisa.AisaError, match="retry disabled"):
        journal.request(url, request, 1, lambda *a: pytest.fail("Missing cache cannot authorize send"))


def test_missing_legacy_request_never_allows_failed_attempt_recovery(tmp_path):
    digest = write_legacy(tmp_path, body(), failure())
    (tmp_path / (digest + ".request.json")).unlink()
    journal = spending.Journal(tmp_path, None, {}, 1)
    assert journal.entries[0]["outcome"] == "unknown"
    with pytest.raises(aisa.AisaError):
        journal.request(aisa.ROUTER, body(), 1, lambda *a: pytest.fail("No trustworthy original request"))


def test_new_quote_can_stop_recovery_without_spending_again(setup, monkeypatch):
    journal, _, sleeps = setup
    amounts = iter([.001, 1])
    monkeypatch.setattr(spending, "quote", lambda calls: next(amounts))
    attempts = []
    with pytest.raises(spending.JournalError, match="limit"):
        journal.request(aisa.ROUTER, body(), 1, lambda *a: attempts.append(1) or failure())
    assert len(attempts) == 1 and len(journal.entries[0]["attempts"]) == 1
    assert journal.reserved == 0


def test_internal_exclusive_lock_and_reload_prevent_duplicate_send(setup, tmp_path):
    first, _, _ = setup
    second = spending.Journal(tmp_path, .02, {}, 1)
    started, finish = threading.Event(), threading.Event()
    failures = []
    def send(*args):
        started.set()
        assert finish.wait(5)
        return success()
    def run():
        try:
            first.request(aisa.ROUTER, body(), 1, send)
        except Exception as exc:
            failures.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(5)
    try:
        with pytest.raises(spending.JournalError) as error:
            second.request(aisa.ROUTER, body(), 1, lambda *a: pytest.fail("Duplicate send"))
        assert error.value.failure_kind == "journal_locked" and error.value.request_sent is False
    finally:
        finish.set()
        thread.join(5)
    assert not failures
    assert second.request(aisa.ROUTER, body(), 1, lambda *a: pytest.fail("Reload cached response")) == success()
    assert len(second.entries) == 1


@pytest.mark.parametrize("bad", [{"calls": []}, {"calls": [{"tool": "get_reddit_search"}]},
                                  {"calls": [{"call_id": "c", "tool": "write_data", "arguments": {}}]},
                                  {"calls": [{"call_id": "c", "tool": "get_reddit_search", "arguments": {}}] * 2}])
def test_bad_request_never_sent_or_reserved(setup, bad):
    journal, quotes, _ = setup
    with pytest.raises(aisa.AisaError) as error:
        journal.request(aisa.ROUTER, bad, 1, lambda *a: pytest.fail("Malformed request"))
    assert error.value.request_sent is False
    assert journal.entries == [] and not quotes


@pytest.mark.parametrize("field,value", [("total_count", 2), ("success_count", 1), ("error_count", 0),
                                        ("error_count", "1"), ("total_count", True)])
def test_inconsistent_batch_summary_cannot_authorize_retry(setup, field, value):
    journal, quotes, _ = setup
    response = {**failure(), field: value}
    with pytest.raises(aisa.AisaError):
        journal.request(aisa.ROUTER, body(), 1, lambda *a: response)
    assert len(quotes) == 1
    assert journal.entries[0]["outcome"] == "unknown"
    assert journal.reserved == .0025


def test_partial_known_billing_is_retained_without_releasing_unknown_reservation(setup):
    journal, quotes, _ = setup
    request = body(count=2)
    response = failure(request)
    response["results"][0]["customer_cost_micros_usd"] = 1200
    response["results"][1].pop("customer_cost_micros_usd")
    with pytest.raises(aisa.AisaError):
        journal.request(aisa.ROUTER, request, 1, lambda *a: response)
    assert journal.entries[0]["actual_usd"] == .0012
    assert journal.entries[0]["global_stop"] is True
    assert journal.reserved == .0025


def youtube_empty_payload(video="abcdefghijk"):
    return {"status_code": 20000, "tasks_count": 1, "tasks_error": 0, "tasks": [{"status_code": 40102,
             "result_count": 1, "path": ["v3", "serp", "youtube", "video_comments", "live", "advanced"],
             "data": {"api": "serp", "function": "live", "se": "youtube", "se_type": "video_comments", "video_id": video},
             "result": [{"video_id": video, "se_domain": "youtube.com", "comments_count": 0, "items_count": 0, "items": None}]}]}


def youtube_empty_request():
    return {"calls": [{"call_id": "y", "tool": "post_dataforseo_serp_youtube_video_comments_live", "arguments": {
        "body": [{"video_id": "abcdefghijk", "language_code": "en", "location_code": 2840}]}}]}


def youtube_empty_reply():
    return {"results": [{"call_id": "y", "successful": True, "customer_cost_micros_usd": 2000,
                          "data": youtube_empty_payload()}]}


def test_youtube_40102_complete_empty_response_is_success_with_charge(setup):
    journal, quotes, _ = setup
    result = journal.request(aisa.ROUTER, youtube_empty_request(), 1, lambda *a: youtube_empty_reply())
    assert result == youtube_empty_reply()
    assert journal.entries[0]["outcome"] == "ok" and journal.entries[0]["actual_usd"] == .002
    assert len(quotes) == 1


@pytest.mark.parametrize("bad", ["root", "task_error", "task_count", "task_status", "result_count", "path", "query_video",
                                "result_video", "comment_count", "item_count", "missing_items", "non_null_items", "boolean_zero"])
def test_youtube_empty_exception_requires_full_specific_contract(bad):
    payload = youtube_empty_payload()
    task, result = payload["tasks"][0], payload["tasks"][0]["result"][0]
    if bad == "root": payload.pop("status_code")
    if bad == "task_error": payload["tasks_error"] = 1
    if bad == "task_count": payload["tasks_count"] = "1"
    if bad == "task_status": task["status_code"] = 40101
    if bad == "result_count": task["result_count"] = 0
    if bad == "path": task["path"] = ["another", "endpoint"]
    if bad == "query_video": task["data"]["video_id"] = "other_video"
    if bad == "result_video": result["video_id"] = "other_video"
    if bad == "comment_count": result["comments_count"] = 1
    if bad == "item_count": result["items_count"] = None
    if bad == "missing_items": result.pop("items")
    if bad == "non_null_items": result["items"] = []
    if bad == "boolean_zero": result["comments_count"] = False
    assert not spending.youtube_no_results(payload, "abcdefghijk")


def old_youtube_failure(journal, monkeypatch):
    with monkeypatch.context() as old:
        old.setattr(spending, "youtube_no_results", lambda *a: False)
        with pytest.raises(aisa.AisaError):
            journal.request(aisa.ROUTER, youtube_empty_request(), 1, lambda *a: youtube_empty_reply())


def test_old_charged_empty_sample_is_readonly_reinterpreted_never_resent(setup, tmp_path, monkeypatch):
    journal, quotes, _ = setup
    old_youtube_failure(journal, monkeypatch)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    result = journal.request(aisa.ROUTER, youtube_empty_request(), 1, lambda *a: pytest.fail("Must only reuse raw cache"))
    assert result == youtube_empty_reply()
    assert journal.entries[0]["outcome"] == "source_failure"
    assert journal.entries[0]["actual_usd"] == .002 and journal.reserved == .002
    assert before == {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    assert journal.cache_interpretations[0]["request_sent"] is False
    assert len(quotes) == 1


def test_cached_empty_reinterpretation_cannot_bypass_another_unknown_failure(setup, monkeypatch):
    journal, _, _ = setup
    old_youtube_failure(journal, monkeypatch)
    def timeout(*args):
        raise TimeoutError("unknown provider outcome")
    with pytest.raises(TimeoutError):
        journal.request(aisa.ROUTER, body("independent"), 1, timeout)
    with pytest.raises(spending.JournalError) as exc:
        journal.request(aisa.ROUTER, youtube_empty_request(), 1, lambda *a: pytest.fail("No send"))
    assert exc.value.failure_kind == "global_block"
    assert not journal.cache_interpretations


def test_old_empty_reinterpretation_rejects_cost_or_original_request_mismatch(setup, tmp_path, monkeypatch):
    journal, _, _ = setup
    old_youtube_failure(journal, monkeypatch)
    entry = journal.entries[0]
    latest = entry["attempts"][-1]
    request = read(tmp_path / latest["request_file"])
    request["body"]["calls"][0]["arguments"]["body"][0]["video_id"] = "other_video"
    spending.save(tmp_path / latest["request_file"], request)
    with pytest.raises(spending.JournalError, match="retry disabled"):
        journal.request(aisa.ROUTER, youtube_empty_request(), 1, lambda *a: pytest.fail("Cannot reinterpret mismatched request"))
