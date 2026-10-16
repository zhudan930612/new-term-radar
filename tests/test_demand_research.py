"""Synthetic source-contract tests; no live data or credentials."""
from datetime import date

import pytest

from radar import demand_research as research

DAY = date(2026, 10, 15)
VIDEO = "abcdefghijk"


@pytest.fixture
def settings(cfg, monkeypatch):
    cfg["research"] = {"enabled": True, "reddit_queries_per_day": 1, "pain_tasks_per_day": 0,
                       "videos_per_task": 2, "chrome_extensions_per_task": 2, "chrome_review_pages": 1}
    cfg["asks"]["patterns"] = ['"is there a tool"']
    monkeypatch.setattr(research.aisa, "call", lambda calls: ({"research": {"posts": []}}, 5))
    monkeypatch.setattr(research.competition, "page_one", lambda *a: ({"results": []}, 7))
    return cfg


def test_reddit_low_score_full_text_and_real_author(settings, tmp_path, monkeypatch):
    text = "I need every invoice exported without losing any rows.\n" * 70
    received = []

    def call(calls):
        received.extend(calls)
        return {"research": {"posts": [
            {"title": "Help with my invoices", "selftext": text, "score": -2, "author": "Actual_User",
             "permalink": "/r/tasks/comments/abc/help/", "id": "abc"},
            {"title": "The second question", "selftext": "I need to export other workbooks too.",
             "author": "[deleted]", "permalink": "/r/tasks/comments/def/help/"}]}}, 11

    monkeypatch.setattr(research.aisa, "call", call)
    rows, errors, coverage = research.collect(settings, DAY, [], [], tmp_path)
    assert errors == [] and len(rows) == 2
    assert rows[0]["text"] == "Help with my invoices\n" + text
    assert len(rows[0]["text"]) > 3000 and not rows[0]["truncated"]
    assert rows[0]["author"] == "reddit:actual_user"
    assert rows[1]["author"] == ""
    assert coverage["queries"][0]["unknown_authors"] == 1
    assert received[0]["arguments"] == {"query": '"is there a tool"', "sort": "new", "timeframe": "month", "trim": "false"}


def test_followup_search_terms_drive_pain_queries_and_missing_optional_key(settings, tmp_path, monkeypatch):
    settings["research"].update(pain_tasks_per_day=1)
    queries = []
    monkeypatch.setattr(research.aisa, "call", lambda calls: (queries.append(calls[0]["arguments"]["query"]) or
                                                            {"research": {"posts": []}}, 0))
    rows, errors, coverage = research.collect(settings, DAY,
        [{"id": "task1", "search_terms": ["merge invoices -site:evil.example"]}], [{"term": "ViralProduct"}], tmp_path)
    assert errors == [] and rows == []
    assert any("merge invoices" in q for q in queries)
    assert not any("ViralProduct" in q or "-site:" in q for q in queries)
    chrome = next(q for q in coverage["queries"] if q["source"] == "chrome_search")
    assert chrome["status"] == "unavailable" and "KEY missing" in chrome["reason"]
    assert coverage["successful_followups"] == ["task1"]


def test_first_run_uses_configurable_task_seeds_not_product_names(settings, tmp_path, monkeypatch):
    settings["research"].update(pain_tasks_per_day=1, seed_task_phrases=["split scanned receipts"])
    queries = []
    monkeypatch.setattr(research.competition, "page_one", lambda query, location: (queries.append(query) or {"results": []}, 0))
    research.collect(settings, DAY, [], [{"term": "ViralProduct"}], tmp_path)
    assert queries == ["site:youtube.com split scanned receipts tutorial"]


def test_known_reddit_failure_does_not_skip_youtube(settings, tmp_path, monkeypatch):
    settings["research"].update(pain_tasks_per_day=1)
    def failed(calls):
        exc = research.aisa.AisaError("private provider details")
        exc.request_sent = True
        exc.failure_kind = "retry_exhausted"
        raise exc
    searched = []
    monkeypatch.setattr(research.aisa, "call", failed)
    monkeypatch.setattr(research.competition, "page_one", lambda *args:
                        (searched.append(args) or {"results": []}, 0))
    _, errors, coverage = research.collect(settings, DAY, [], [], tmp_path)
    assert searched
    assert all(q["status"] == "failed" and q["request_sent"] for q in coverage["queries"] if q["source"] == "reddit")
    assert any(q["source"] == "youtube_search" and q["status"] == "complete" for q in coverage["queries"])
    assert "private provider details" not in str(errors) + str(coverage)


def test_journal_block_is_reported_as_not_executed(settings, tmp_path, monkeypatch):
    def blocked(calls):
        exc = research.aisa.AisaError("private provider details")
        exc.request_sent = False
        exc.failure_kind = "global_block"
        raise exc
    monkeypatch.setattr(research.aisa, "call", blocked)
    _, errors, coverage = research.collect(settings, DAY, [], [], tmp_path)
    check = coverage["queries"][0]
    assert check["status"] == "not_executed" and check["request_sent"] is False
    assert check["failure_kind"] == "global_block"
    assert errors == ["reddit: not_executed (global_block)"]


def test_youtube_keeps_full_comment_and_never_uses_display_name(settings, tmp_path, monkeypatch):
    settings["research"].update(pain_tasks_per_day=1)
    monkeypatch.setattr(research.competition, "page_one", lambda *a: ({"results": [
        {"url": f"https://www.youtube.com/watch?v={VIDEO}"},
        {"url": f"https://youtube.com.evil.example/watch?v={VIDEO}"}]}, 7))
    text = "I am doing this step manually for every project.\n" * 80
    monkeypatch.setattr(research.social, "youtube_comments", lambda video: ([
        {"text": text, "author_name": "Display Only", "likes": 0,
         "author_url": "https://www.youtube.com/@observed_handle", "author_display_name": "Observed display",
         "identity_status": "unknown", "publication_date": "4 years ago", "provider_timestamp": "2022-10-15 04:46:16 +00:00",
         "created_precision": "relative_estimate", "reply_count": 2, "video_metadata": {"title": "Task tutorial", "comments_count": 22}},
        {"text": "Another detailed comment about manually processing files.", "author_id": "UCactualChannel", "likes": 1}], 13))
    rows, errors, coverage = research.collect(settings, DAY, [], [], tmp_path)
    assert errors == [] and len(rows) == 2
    assert rows[0]["text"] == text and rows[0]["author"] == ""
    assert rows[0]["author_url"].endswith("/@observed_handle") and rows[0]["identity_status"] == "unknown"
    assert rows[0]["publication_date"] == "4 years ago" and rows[0]["created_precision"] == "relative_estimate"
    assert rows[0]["reply_count"] == 2 and rows[0]["video_metadata"]["comments_count"] == 22
    assert "历史评论不代表今天" in rows[0]["source_time_note"]
    assert rows[1]["author"] == "youtube:UCactualChannel"
    assert rows[0]["url"] == rows[1]["url"] == f"https://youtu.be/{VIDEO}"
    assert all(row["kind"] == "comment" for row in rows)
    query = next(q for q in coverage["queries"] if q["source"] == "youtube_comments")
    assert query["returned"] == 2 and query["unknown_authors"] == 1


def test_chrome_preserves_raw_text_and_account_ids_with_shared_source_url(settings, tmp_path, monkeypatch):
    settings["research"].update(pain_tasks_per_day=1)
    monkeypatch.setenv("GOANYAPI_API_KEY", "synthetic-test-only")
    extension = "a" * 32
    monkeypatch.setattr(research.goanyapi, "serp", lambda q: ([
        {"url": f"https://chromewebstore.google.com/detail/synthetic/{extension}"}], 17))
    text = "I like it, but batch exports still take too much manual work.\n" * 65
    monkeypatch.setattr(research.goanyapi, "call", lambda *a, **k: ({"comments": [
        {"content": text, "rating": 5, "author": {"name": "Someone"}},
        {"content": "Please let me export all selected records in one file.", "author": {"id": "account123"}}],
        "hasMore": True, "nextCursor": "next-page"}, 19))
    rows, errors, coverage = research.collect(settings, DAY, [], [], tmp_path)
    assert errors == [] and len(rows) == 2
    assert rows[0]["text"] == text and rows[0]["rating"] == 5
    assert rows[0]["author"] == "" and rows[1]["author"] == "chrome:account123"
    assert rows[0]["url"] == rows[1]["url"]
    check = next(q for q in coverage["queries"] if q["source"] == "chrome_reviews")
    assert check["more_pages"] and check["unknown_authors"] == 1


@pytest.mark.parametrize("payload", [{}, {"research": {}}, {"research": {"posts": "bad"}},
                                     {"research": {"success": False, "posts": []}}])
def test_missing_or_malformed_payload_is_failure_not_empty_demand(settings, tmp_path, monkeypatch, payload):
    monkeypatch.setattr(research.aisa, "call", lambda calls: (payload, 0))
    rows, errors, coverage = research.collect(settings, DAY, [], [], tmp_path)
    assert rows == [] and errors
    assert coverage["queries"][0]["status"] == "failed"


def test_source_failure_keeps_other_sources_and_does_not_echo_exception_body(settings, tmp_path, monkeypatch):
    settings["research"].update(pain_tasks_per_day=1)

    def fail(*a):
        raise RuntimeError("sensitive provider response must stay out of errors")

    monkeypatch.setattr(research.competition, "page_one", fail)
    rows, errors, coverage = research.collect(settings, DAY, [], [], tmp_path)
    assert errors == ["youtube_search: RuntimeError"]
    assert not any("sensitive" in str(row) for row in coverage["queries"])
    assert any(q["source"] == "reddit" and q["status"] == "complete" for q in coverage["queries"])
    assert any(q["source"] == "youtube_search" and q["status"] == "failed" for q in coverage["queries"])


def test_real_comment_id_keeps_exact_comment_locator(settings, tmp_path, monkeypatch):
    settings["research"].update(pain_tasks_per_day=1)
    monkeypatch.setattr(research.competition, "page_one", lambda *a: ({"results": [
        {"url": f"https://www.youtube.com/watch?v={VIDEO}"}]}, 0))
    monkeypatch.setattr(research.social, "youtube_comments", lambda video: ([
        {"text": "Please add a batch operation for my worksheets.", "id": "Ugx.real-comment", "author_id": "UCactual"},
        {"text": "I also need repeatable import without manually copying each row.", "id": "", "author_name": "UCfake"}], 0))
    rows, errors, _ = research.collect(settings, DAY, [], [], tmp_path)
    assert errors == [] and len(rows) == 2
    assert rows[0]["url"] == f"https://www.youtube.com/watch?v={VIDEO}&lc=Ugx.real-comment"
    assert rows[1]["url"] == f"https://youtu.be/{VIDEO}" and rows[1]["author"] == ""
