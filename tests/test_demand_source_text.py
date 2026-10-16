"""Full-source and collection-boundary regressions; synthetic, offline inputs only."""
import base64
import io
import json
from datetime import date

from radar import demand_sources, opportunities


DAY = date(2026, 10, 16)


def test_x_preserves_tail_task_and_uses_eligible_posts_not_filtered_prefix(monkeypatch):
    text = "Background.\n" * 250 + "I need a specific task at the end."
    rows = [{"text": "automated", "url": f"https://x.com/bot/status/{i}", "author": {"isAutomated": True}} for i in range(12)]
    rows += [{"text": text, "url": "https://x.com/person/status/99", "author": {"userName": "person"}}]
    monkeypatch.setattr(opportunities, "tweets", lambda query: rows)
    monkeypatch.setattr(opportunities, "collect", lambda *args: ([], []))
    evidence, errors, coverage = demand_sources.collect([], DAY, 1, [], False)
    assert not errors and len(evidence) == 1 and evidence[0]["text"] == text
    assert evidence[0]["provider_text_completeness"] == "unknown" and not evidence[0]["truncated"]
    assert coverage["queries"][0]["considered"] == 13 and coverage["queries"][0]["accepted"] == 1


def test_github_and_hn_keep_full_returned_text(monkeypatch):
    text = "long source\n" * 300 + "Actual task at the tail"
    monkeypatch.setattr(opportunities, "collect", lambda *args: ([], []))
    monkeypatch.setattr(demand_sources.breakout, "_get", lambda *args: {"items": [{"title": "Title", "body": text,
        "html_url": "https://github.com/team/repo/issues/1", "user": {"login": "person"}}]})
    monkeypatch.setattr(demand_sources.urllib.request, "urlopen", lambda *a, **k: io.StringIO(json.dumps({"hits": [
        {"objectID": "123", "comment_text": text, "author": "reader"}]})))
    evidence, errors, _ = demand_sources.collect([], DAY, 0, [], True)
    assert not errors and [s["text"] for s in evidence] == ["Title\n" + text, text]
    assert all(not s["truncated"] and s["provider_text_completeness"] == "unknown" for s in evidence)


def test_legacy_background_readme_issue_and_x_do_not_silently_truncate(monkeypatch):
    text = "First line\n" * 300 + "Important ending."
    def get(path):
        if path.endswith("/readme"):
            return {"content": base64.b64encode(text.encode()).decode()}
        return [{"title": "Issue", "body": text, "html_url": "https://github.com/team/repo/issues/1",
                 "user": {"login": "person"}, "author_association": "NONE"}]
    monkeypatch.setattr(opportunities.breakout, "_get", get)
    monkeypatch.setattr(opportunities, "tweets", lambda query: [{"text": text + " Tweet",
        "url": "https://x.com/person/status/1", "author": {"userName": "person"}}])
    evidence, errors = opportunities.collect([{"term": "Example", "repo": "team/repo"}], DAY, 0)
    assert not errors and len(evidence) == 3
    assert [s["text"] for s in evidence] == [text, "Issue\n" + text, text + " Tweet"]
    assert all(not s["truncated"] and s["provider_text_completeness"] == "unknown" for s in evidence)


def test_no_paid_collector_is_added_to_free_source_only_path(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("No paid call expected")
    monkeypatch.setattr(opportunities, "tweets", unexpected)
    monkeypatch.setattr(opportunities, "collect", lambda *args: ([], []))
    monkeypatch.setattr(demand_sources.breakout, "_get", lambda *a: {"items": []})
    monkeypatch.setattr(demand_sources.urllib.request, "urlopen", lambda *a, **k: io.StringIO('{"hits": []}'))
    evidence, errors, coverage = demand_sources.collect([], DAY, 0, [], True)
    assert not evidence and not errors and not coverage["attempted_followups"]
