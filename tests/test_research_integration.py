import json
from datetime import date

import pytest

from radar import aisa, competition, config, demand_research, demands, goanyapi, opportunity_runner as runner, social, spending

GOANY_CALL = goanyapi.call


def test_quality_sources_reach_full_source_audit(tmp_path, cfg, monkeypatch):
    cfg["research"]["enabled"] = True
    cfg["opportunities"]["budget_usd"] = None
    monkeypatch.setattr(runner, "today_local", lambda: date(2026, 10, 20))
    monkeypatch.setattr(spending, "account", lambda: {"balance_usd": 1})
    monkeypatch.setattr(spending, "model_prices", lambda *a: {"input": .2, "output": .7})
    base = {"id": "e1", "url": "https://example.com/a", "text": "Original task", "author": "x:one"}
    extra = {"id": "research1", "url": "https://example.com/b", "text": "long original " * 300, "author": ""}
    monkeypatch.setattr(runner.demand_sources, "collect", lambda *a: ([base], [], {"successful_followups": []}))
    monkeypatch.setattr(demand_research, "collect", lambda *a: ([extra, extra], [], {"queries": [], "successful_followups": []}))
    captured = []

    def process(model, evidence, path, day):
        assert [r["id"] for r in evidence] == ["e1", "e2"]
        assert evidence[1]["text"] == extra["text"] and evidence[1]["author"] == ""
        captured.append(True)
        return {"workflow_version": 2, "opportunities": [], "analysis_errors": []}

    monkeypatch.setattr(demands, "process", process)
    output = tmp_path / "report.md"
    assert runner.run_daily(cfg, tmp_path / "state", output) == 2
    result = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert result["status"] == "awaiting_audit"
    assert result["semantic_audit"]["status"] == "pending" and captured == [True]
    assert "未设置每日金额上限" in output.read_text(encoding="utf-8")


def test_reddit_contract_and_long_text(monkeypatch):
    body = "A complete source paragraph " * 100
    def call(calls):
        assert calls[0]["arguments"] == {"query": "task", "sort": "new", "timeframe": "month", "trim": False}
        return {"r": {"success": True, "posts": [{"id": "p", "title": "task", "selftext": body,
            "author": "alice", "author_fullname": "t2_abc", "permalink": "/r/tools/comments/p/title/"}]}}, 1
    monkeypatch.setattr(aisa, "call", call)
    rows, _ = social.reddit("task", time="month", sort="new")
    assert rows[0]["text"] == body and rows[0]["author_id"] == "t2_abc"


def test_youtube_contract_preserves_identity_without_fabricating_comment_id(monkeypatch):
    def call(calls):
        assert calls[0]["tool"] == "post_dataforseo_serp_youtube_video_comments_live"
        assert calls[0]["arguments"]["body"] == [{"video_id": "abcdefghijk", "location_code": 2840, "language_code": "en"}]
        return {"y": {"tasks": [{"status_code": 20000, "result": [{"items": [
            {"type": "youtube_comment", "text": "Need this for all my files", "author_name": "Display",
             "author_url": "https://www.youtube.com/channel/UC_test", "likes_count": 0}]}]}]}}, 1
    monkeypatch.setattr(aisa, "call", call)
    rows, _ = social.youtube_comments("abcdefghijk")
    assert rows[0]["id"] == "" and rows[0]["author_id"] == "UC_test"
    assert rows[0]["url"] == "https://youtu.be/abcdefghijk"


@pytest.mark.parametrize("payload", [{}, {"tasks": [{"status_code": 50000, "result": None}]}])
def test_search_failure_is_not_a_successful_empty_page(monkeypatch, payload):
    monkeypatch.setattr(aisa, "call", lambda *a: ({"serp": payload}, 0))
    with pytest.raises(aisa.AisaError):
        competition.page_one("task", 2840)


def test_quality_budget_still_requires_balance_and_blocks_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(spending, "quote", lambda calls: .01)
    journal = spending.Journal(tmp_path / "api", None, {}, .01)
    with pytest.raises(aisa.AisaError, match="limit"):
        journal.request(aisa.ROUTER, {"calls": [{"tool": "get_reddit_search"}]}, 1,
                        lambda *a: pytest.fail("No available balance"))
    assert journal.entries == []


@pytest.mark.parametrize("budget", [-1, True, "unlimited", float("nan")])
def test_invalid_quality_budget_is_rejected(tmp_path, budget):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"opportunities": {"budget_usd": budget}}))
    with pytest.raises(ValueError, match="budget"):
        config.load(path)


def test_provider_task_error_is_journaled_and_blocks_later_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(spending, "quote", lambda calls: .001)
    journal = spending.Journal(tmp_path, None, {}, 1)
    body = {"calls": [{"tool": competition.SERP_TOOL}]}
    reply = {"results": [{"successful": True, "data": {"tasks": [{"status_code": 50000}]}}]}
    with pytest.raises(aisa.AisaError, match="unsuccessful"):
        journal.request(aisa.ROUTER, body, 1, lambda *a: reply)
    assert journal.entries[0]["status"] == "failed"
    with pytest.raises(aisa.AisaError, match="retry disabled"):
        journal.request(aisa.ROUTER, body, 1, lambda *a: pytest.fail("must not retry"))


@pytest.mark.parametrize("result", [None, [], [{}], [{"items": None}]])
def test_missing_youtube_results_are_failures(monkeypatch, result):
    monkeypatch.setattr(aisa, "call", lambda *a: ({"y": {"tasks": [{"result": result}]}}, 0))
    with pytest.raises(aisa.AisaError, match="unavailable"):
        social.youtube_comments("abcdefghijk")


def test_explicit_empty_youtube_page_is_valid(monkeypatch):
    monkeypatch.setattr(aisa, "call", lambda *a: ({"y": {"tasks": [{"result": [{"items": []}]}]}}, 0))
    assert social.youtube_comments("abcdefghijk") == ([], 0)


def test_goany_timeout_is_not_retried(monkeypatch):
    monkeypatch.setattr(goanyapi, "call", GOANY_CALL)
    attempts = []
    def send(*args, **kwargs):
        attempts.append(1)
        raise TimeoutError("mock timeout")
    monkeypatch.setattr(goanyapi.urllib.request, "urlopen", send)
    with pytest.raises(goanyapi.GoAnyError):
        goanyapi.call("chrome-web-store-reviews", extensionId="example")
    assert attempts == [1]


def test_unrelated_rating_cache_does_not_measure_current_competition(tmp_path, cfg, monkeypatch):
    cfg["competition"]["max_rows"] = 1
    monkeypatch.setattr(competition, "page_one", lambda *a: ({"results": [
        {"domain": "current.example", "url": "https://current.example", "position": 1, "kind": "tool"}],
        "ads": 0, "features": []}, 0))
    monkeypatch.setattr(competition, "difficulty", lambda *a: ({}, 0))
    monkeypatch.setattr(competition, "ratings", lambda *a: ({"unrelated.example": 90}, 0))
    monkeypatch.setattr(competition, "supply", lambda *a: (0, []))
    rows = [{"phrase": "task", "decision": "watch"}]
    competition.assess(rows, cfg, tmp_path)
    assert rows[0]["competition"]["weak"] is None
    assert rows[0]["competition"]["weakest"] is None
