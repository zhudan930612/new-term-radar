"""Synthetic source-contract tests; no live data or credentials."""
from datetime import date

import pytest

from radar import demand_research as research

DAY = date(2026, 10, 15)
VIDEO = "abcdefghijk"


@pytest.fixture
def settings(cfg, monkeypatch):
    cfg["research"] = {"enabled": True, "reddit_queries_per_day": 1, "pain_tasks_per_day": 0,
                       "videos_per_task": 2, "chrome_extensions_per_task": 2, "chrome_review_pages": 1,
                       "discovery_queries": ['"is there a tool"']}
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
        {"url": f"https://www.youtube.com/watch?v={VIDEO}", "title": "Batch document conversion tutorial"},
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
        {"url": f"https://www.youtube.com/watch?v={VIDEO}", "title": "Batch document conversion tutorial"}]}, 0))
    monkeypatch.setattr(research.social, "youtube_comments", lambda video: ([
        {"text": "Please add a batch operation for my worksheets.", "id": "Ugx.real-comment", "author_id": "UCactual"},
        {"text": "I also need repeatable import without manually copying each row.", "id": "", "author_name": "UCfake"}], 0))
    rows, errors, _ = research.collect(settings, DAY, [], [], tmp_path)
    assert errors == [] and len(rows) == 2
    assert rows[0]["url"] == f"https://www.youtube.com/watch?v={VIDEO}&lc=Ugx.real-comment"
    assert rows[1]["url"] == f"https://youtu.be/{VIDEO}" and rows[1]["author"] == ""


def test_web_discovery_ignores_legacy_ai_patterns_and_rotates_daily_blocks(settings, tmp_path, monkeypatch):
    from radar.research_queries import DEFAULT_DISCOVERY_QUERIES
    settings["research"].pop("discovery_queries")
    settings["research"]["reddit_queries_per_day"] = 4
    settings["asks"]["patterns"] = ["LEGACY_AI_ONLY"]
    calls = []
    monkeypatch.setattr(research.aisa, "call", lambda payload: (
        calls.append(payload[0]["arguments"]["query"]) or {"research": {"posts": []}}, 0))
    research.collect(settings, DAY, [], [], tmp_path)
    first = set(calls)
    calls.clear()
    research.collect(settings, date(2026, 10, 16), [], [], tmp_path)
    assert len(first) == len(set(calls)) == 4
    assert not first.intersection(calls)
    assert first | set(calls) == set(DEFAULT_DISCOVERY_QUERIES)
    assert settings["asks"]["patterns"] == ["LEGACY_AI_ONLY"]


def test_empty_followup_uses_at_most_two_unquoted_variants_and_is_not_evidence(settings, tmp_path, monkeypatch):
    settings["research"].update(reddit_queries_per_day=0, pain_tasks_per_day=1, query_variants_per_task=2)
    calls = []
    monkeypatch.setattr(research.aisa, "call", lambda payload: (
        calls.append(payload[0]["arguments"]["query"]) or {"research": {"posts": []}}, 0))
    task = {"id": "task", "search_terms": ["extend protected spreadsheet formulas when adding rows"]}
    _, errors, coverage = research.collect(settings, DAY, [task], [], tmp_path)
    assert not errors
    assert len(calls) == 2 and calls[0] != calls[1]
    assert all('"' not in query for query in calls)
    assert coverage["attempted_followups"] == ["task"]
    assert coverage["evidence_followups"] == coverage["confirmed_followups"] == []
    assert all(q["outcome"] == "empty" for q in coverage["followup_outcomes"] if q["source"] == "reddit")


def test_relevant_raw_post_stops_variant_but_does_not_confirm_demand(settings, tmp_path, monkeypatch):
    settings["research"].update(reddit_queries_per_day=0, pain_tasks_per_day=1)
    calls = []
    def respond(payload):
        calls.append(payload)
        return {"research": {"posts": [{"title": "I search inside Excel files", "selftext": "One person needs help.",
            "author": "person", "permalink": "/r/work/comments/one/help/"}]}}, 0
    monkeypatch.setattr(research.aisa, "call", respond)
    rows, _, coverage = research.collect(settings, DAY, [{"id": "task", "search_terms": ["search spreadsheet content"]}], [], tmp_path)
    assert len(calls) == len(rows) == 1
    assert coverage["evidence_followups"] == ["task"] and coverage["confirmed_followups"] == []


def test_low_relevance_post_is_still_kept_and_allows_bounded_second_query(settings, tmp_path, monkeypatch):
    settings["research"].update(reddit_queries_per_day=0, pain_tasks_per_day=1)
    calls = []
    def respond(payload):
        calls.append(payload)
        return {"research": {"posts": [{"title": "I need help with a different task", "selftext": "A real unrelated request.",
            "author": "person", "permalink": "/r/work/comments/other/help/"}]}}, 0
    monkeypatch.setattr(research.aisa, "call", respond)
    rows, _, coverage = research.collect(settings, DAY, [{"id": "task", "search_terms": ["search spreadsheet content"]}], [], tmp_path)
    assert len(calls) == 2 and len(rows) == 1
    assert rows[0]["text"].endswith("A real unrelated request.")
    assert coverage["confirmed_followups"] == []


def test_tutorial_object_action_check_precedes_paid_comments(settings, tmp_path, monkeypatch):
    settings["research"].update(reddit_queries_per_day=0, pain_tasks_per_day=1)
    relevant = "123456789ab"
    missing = "missinginfo"
    monkeypatch.setattr(research.competition, "page_one", lambda *args: ({"results": [
        {"url": f"https://youtu.be/{VIDEO}", "title": "Excel tips: formatting and conditional formulas"},
        {"url": f"https://youtu.be/{relevant}", "title": "Find text inside Excel workbooks"},
        {"url": f"https://youtu.be/{missing}"}]}, 0))
    called = []
    monkeypatch.setattr(research.social, "youtube_comments", lambda video: (called.append(video) or [], 0))
    _, errors, coverage = research.collect(settings, DAY, [{"id": "task", "search_terms": ["search spreadsheet content"]}], [], tmp_path)
    assert not errors and called == [relevant]
    selection = next(q for q in coverage["queries"] if q["source"] == "youtube_search")
    assert selection["matched"] == 3 and selection["relevant_candidates"] == 1
    assert {c["video_id"] for c in selection["candidates"] if c["status"] == "skipped"} == {VIDEO, missing}
    assert all(c["reason"] for c in selection["candidates"])
    assert coverage["confirmed_followups"] == []


@pytest.mark.parametrize("sent", [False, True])
def test_followup_sent_failure_is_distinct_from_quote_block(settings, tmp_path, monkeypatch, sent):
    settings["research"].update(reddit_queries_per_day=0, pain_tasks_per_day=1)
    def fail(*args, **kwargs):
        error = research.aisa.AisaError("hidden details")
        error.request_sent = sent
        raise error
    monkeypatch.setattr(research.aisa, "call", fail)
    monkeypatch.setattr(research.competition, "page_one", fail)
    _, _, coverage = research.collect(settings, DAY, [{"id": "task", "search_terms": ["search spreadsheet"]}], [], tmp_path)
    assert coverage["attempted_followups"] == (["task"] if sent else [])
    assert coverage["evidence_followups"] == coverage["confirmed_followups"] == []


def test_query_variants_rotate_without_operators_or_new_domains():
    from radar.research_queries import query_variants
    first = query_variants({"search_terms": ["search spreadsheet -site:evil.example after:2030"]})
    next_round = query_variants({"search_terms": ["search spreadsheet -site:evil.example after:2030"], "search_attempts": 1})
    assert first[0] != next_round[0]
    assert all("evil" not in phrase and ":" not in phrase for phrase in first)
    reviewed = query_variants({"research": {"query_variants": ["search workbook contents", "find Excel text"],
        "query_history": [{"query": "search workbook contents"}]}})
    assert reviewed[0] == "find excel text"


def test_drama_recommendation_is_not_video_generation_tutorial():
    from radar.research_queries import relevance
    result = relevance("AI vertical drama recommendations original story",
                       "Generate an original AI vertical drama story in minutes")
    assert result["candidate"] is False


def test_duplicate_task_ids_cannot_expand_per_task_query_limit(settings, tmp_path, monkeypatch):
    settings["research"].update(reddit_queries_per_day=0, pain_tasks_per_day=3)
    calls = []
    monkeypatch.setattr(research.aisa, "call", lambda payload: (
        calls.append(payload[0]) or {"research": {"posts": []}}, 0))
    research.collect(settings, DAY, [{"id": "same", "search_terms": ["search spreadsheet"]},
        {"id": "same", "search_terms": ["find workbook contents"]}], [], tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize("phrase,title,constraint", [
    ("extend protected spreadsheet formulas", "Automatically add formulas to new rows in Excel", "protected"),
    ("offline PDF heading detection", "PDF heading detection in the browser", "offline"),
    ("batch document conversion", "Convert a document to PDF", "batch"),
])
def test_missing_critical_constraint_is_partial_not_paid_candidate(phrase, title, constraint):
    from radar.research_queries import relevance
    result = relevance(phrase, title)
    assert not result["candidate"] and result["match_status"] == "partial"
    assert result["constraint_status"] == "constraint_unknown"
    assert constraint in result["missing_constraints"]


def test_constraint_match_and_conflict_are_distinct():
    from radar.research_queries import relevance
    accepted = relevance("extend protected spreadsheet formulas", "Extend formulas in a locked Excel sheet")
    assert accepted["candidate"] and accepted["constraint_status"] == "present_in_metadata"
    rejected = relevance("extend protected spreadsheet formulas", "Extend protected Excel formulas by unprotecting the sheet")
    assert not rejected["candidate"] and rejected["contradicted_constraints"] == ["protected"]


def test_summary_side_topic_cannot_override_different_title_action():
    from radar.research_queries import relevance
    title = "How to Hide and Unhide a Specific Row in Excel"
    summary = "Adding and Deleting Columns and Rows in Excel. Lock Cells to Protect your Formulas."
    result = relevance("extend protected spreadsheet formulas when adding rows", title + " " + summary, title=title)
    assert result["candidate"] is False and result["title_action_conflict"] is True


def test_approved_short_query_cannot_drop_original_task_constraint(settings, tmp_path, monkeypatch):
    settings["research"].update(reddit_queries_per_day=0, pain_tasks_per_day=1)
    monkeypatch.setattr(research.competition, "page_one", lambda *args: ({"results": [
        {"url": f"https://youtu.be/{VIDEO}", "title": "Automatically add formulas to new rows in Excel"}]}, 0))
    def forbidden(video):
        raise AssertionError("Missing protected-sheet constraint must be checked before a paid comment request")
    monkeypatch.setattr(research.social, "youtube_comments", forbidden)
    task = {"id": "task", "search_terms": ["extend protected spreadsheet formulas when adding rows"],
            "research": {"query_variants": ["extend Excel formulas"]}}
    _, errors, coverage = research.collect(settings, DAY, [task], [], tmp_path)
    assert not errors
    candidate = next(q for q in coverage["queries"] if q["source"] == "youtube_search")["candidates"][0]
    assert candidate["status"] == "skipped" and candidate["missing_constraints"] == ["protected"]


def test_query_rotation_reads_record_attempts_search_history_and_ignores_quote_blocks():
    from radar.research_queries import query_variants
    task = {"research": {"query_variants": ["search workbook contents", "find Excel text"]},
            "search_attempts": 0, "search_history": [{"date": "2026-10-16", "queries": [
                {"source": "reddit", "query": "search workbook contents", "status": "complete", "returned": 0},
                {"source": "reddit", "query": "find Excel text", "status": "not_executed", "request_sent": False}]}]}
    assert query_variants(task)[0] == "find excel text"


@pytest.mark.parametrize("constraint,context", [
    ("Must work offline without uploading files", "Local Mac PDF processing"),
    ("必须离线处理，不上传文件", "Mac本地PDF处理"),
])
def test_real_task_fields_block_cloud_video_even_when_keywords_omit_constraints(settings, tmp_path, monkeypatch, constraint, context):
    settings["research"].update(reddit_queries_per_day=0, pain_tasks_per_day=1)
    monkeypatch.setattr(research.competition, "page_one", lambda *args: ({"results": [
        {"url": f"https://youtu.be/{VIDEO}", "title": "Detect PDF headings with an online-only cloud service",
         "description": "Upload required"}]}, 0))
    calls = []
    monkeypatch.setattr(research.social, "youtube_comments", lambda video: (calls.append(video) or [], 0))
    task = {"id": "task", "search_terms": ["detect PDF headings"], "constraint": constraint, "context": context}
    _, errors, coverage = research.collect(settings, DAY, [task], [], tmp_path)
    assert not errors and not calls
    candidate = next(q for q in coverage["queries"] if q["source"] == "youtube_search")["candidates"][0]
    assert candidate["status"] == "skipped"
    assert {"offline", "no_upload"} <= set(candidate["contradicted_constraints"])
    assert {"local", "mac"} <= set(candidate["missing_constraints"])


def test_unknown_constraint_statements_do_not_fabricate_requirements():
    from radar.research_queries import task_constraint_context, relevance
    task = {"search_terms": ["detect PDF headings"],
            "constraint": "上传意愿未知；是否需离线未知；是否免费待确认；Mac支持未知；是否批量未说明",
            "context": "Whether local processing is needed is unknown"}
    check = relevance("detect PDF headings", "Detect PDF headings online", constraint_context=task_constraint_context(task))
    assert check["required_constraints"] == [] and check["candidate"]


def test_bilingual_explicit_constraints_are_independent_of_unknown_neighbors():
    from radar.research_queries import task_constraint_context, relevance
    task = {"search_terms": ["extend formulas"], "constraint": "批量；受保护公式列；免费；上传意愿未知", "context": "本地处理"}
    check = relevance("extend formulas", "Extend formulas in bulk for free in locked local workbooks",
                      constraint_context=task_constraint_context(task))
    assert check["candidate"]
    assert set(check["required_constraints"]) == {"batch", "protected", "free", "local"}


def test_no_paid_api_is_not_a_blanket_free_software_requirement():
    from radar.research_queries import task_constraint_context, relevance
    task = {"search_terms": ["detect PDF headings"], "constraint": "Mac本地、初始安装后离线、无付费AI API或token成本"}
    check = relevance("detect PDF headings", "Detect PDF headings offline locally on Mac without paid AI APIs",
                      constraint_context=task_constraint_context(task))
    assert check["candidate"] and "no_paid_api" in check["required_constraints"]
    assert "free" not in check["required_constraints"]
