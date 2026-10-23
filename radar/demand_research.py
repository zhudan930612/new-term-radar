"""Raw, task-led research inputs; classification and independence remain downstream gates.

No score, search-volume or model-summary filters are used here. API text is retained
without a local character cap. A successful empty page describes only that query.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from . import aisa, competition, goanyapi, opportunities, social
from .research_queries import DEFAULT_DISCOVERY_QUERIES, followup_summary, query_variants, relevance, task_constraint_context

SEED_TASKS = ["batch document conversion", "invoice data extraction", "subtitle editing"]
ID = re.compile(r"[A-Za-z0-9_-]+")
EXTENSION = re.compile(r"/detail/(?:[^/]+/)?([a-p]{32})/?$")


def _phrase(value) -> str:
    return " ".join(re.findall(r"[^\W_]+", value if isinstance(value, str) else "", re.UNICODE)[:12])


def _identity(namespace: str, value) -> str:
    value = value.strip() if isinstance(value, str) else ""
    if value.lower() in {"", "deleted", "[deleted]", "removed", "[removed]", "unknown", "anonymous"}:
        return ""
    return namespace + ":" + value if ID.fullmatch(value) else ""


def _channel(row: dict) -> str:
    author = row.get("author") if isinstance(row.get("author"), dict) else {}
    value = row.get("author_id") or row.get("author_channel_id") or row.get("authorChannelId") or author.get("channel_id") or author.get("channelId")
    # Display names are not accounts. Only the provider's channel identity is used.
    return _identity("youtube", value)


def _video(url: str) -> str:
    p = urlparse(url)
    if p.scheme != "https" or p.username or p.password:
        return ""
    if p.hostname == "youtu.be":
        value = p.path.strip("/")
    elif p.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        value = (parse_qs(p.query).get("v") or [""])[0] if p.path == "/watch" else ""
    else:
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9_-]{11}", value) else ""


def collect(cfg: dict, today: date, followups: list[dict], selected: list[dict],
            state: Path) -> tuple[list[dict], list[str], dict]:
    """Fetch raw pages. ``state`` is reserved for the caller's request journal.

    Candidate product names are background only; they never create user evidence.
    The caller owns AIsa/GoAnyAPI accounting and persistence of returned evidence.
    """
    c = cfg["research"]
    evidence, errors, checks, seen = [], [], [], set()
    cost = 0

    def record(source, query, **extra):
        row = {"source": source, "query": query, "status": "pending", "returned": 0,
               "accepted": 0, "unknown_authors": 0, "discarded": 0,
               "locally_truncated": 0, "provider_text_completeness": "unknown",
               "scope": "first page; not exhaustive", **extra}
        checks.append(row)
        return row

    def failed(check, exc):
        sent = getattr(exc, "request_sent", None)
        check.update(status="not_executed" if sent is False else "failed", error=type(exc).__name__)
        if sent is not None:
            check["request_sent"] = sent
            check["failure_kind"] = getattr(exc, "failure_kind", "unknown")
        # Provider exception bodies can contain request details; retain only their type.
        detail = (check["status"] + " (" + check["failure_kind"] + ")"
                  if sent is not None else type(exc).__name__)
        errors.append(check["source"] + ": " + detail)

    def add(check, row):
        text, url = row.get("text"), row.get("url", "")
        if not isinstance(text, str) or not text.strip() or not opportunities.safe_url(url):
            check["discarded"] += 1
            return
        if not row.get("author"):
            check["unknown_authors"] += 1
        key = (url, text)
        if key in seen:
            check["duplicates"] = check.get("duplicates", 0) + 1
            return
        seen.add(key)
        evidence.append({**row, "query": check["query"], "query_source": check["source"],
                         "id": f"research{len(evidence) + 1}", "observed_on": today.isoformat(),
                         "truncated": False, "provider_text_completeness": "unknown"})
        check["accepted"] += 1

    def call(tool, arguments):
        nonlocal cost
        response, spent = aisa.call([{"call_id": "research", "tool": tool, "arguments": arguments}])
        cost += spent
        if "research" not in response or not isinstance(response["research"], dict):
            raise aisa.AisaError("Research response absent")
        return response["research"]

    def reddit(query, lane, task_id="", task_phrase="", variant=0, constraint_context=""):
        check = record("reddit", query, lane=lane, task_id=task_id, window="month",
                       variant=variant, relevant_candidates=0)
        try:
            data = call(social.REDDIT_TOOL, {"query": query, "sort": "new", "timeframe": "month", "trim": "false"})
            check["response_received"] = True
            rows = data.get("posts")
            if data.get("success") is False or not isinstance(rows, list):
                raise ValueError("Reddit posts missing")
            check["returned"] = len(rows)
            for row in rows:
                if not isinstance(row, dict):
                    check["discarded"] += 1
                    continue
                raw_url = row.get("permalink") or row.get("url") or ""
                url = "https://www.reddit.com" + raw_url if raw_url.startswith("/") else raw_url
                p = urlparse(url)
                if p.hostname not in {"reddit.com", "www.reddit.com", "old.reddit.com"} or "/comments/" not in p.path:
                    check["discarded"] += 1
                    continue
                # Canonical public permalink has no tracking/query parameters.
                url = "https://www.reddit.com" + p.path
                author = row.get("author")
                author = author.get("name") if isinstance(author, dict) else author
                text = "\n".join(x for x in (row.get("title"), row.get("selftext")) if isinstance(x, str) and x)
                if task_phrase and relevance(task_phrase, text, constraint_context=constraint_context)["candidate"]:
                    check["relevant_candidates"] += 1
                add(check, {"kind": "post", "text": text, "url": url,
                            "author": _identity("reddit", author.lower() if isinstance(author, str) else ""),
                            "topic": lane, "task_id": task_id, "created": row.get("created_utc", ""),
                            "context_status": "thread_not_read", "source_record_id": row.get("id") or row.get("post_id") or ""})
            check.update(status="complete")
        except Exception as exc:
            failed(check, exc)
        return check

    patterns = [p for p in c.get("discovery_queries", DEFAULT_DISCOVERY_QUERIES) if isinstance(p, str) and p.strip()]
    daily = min(c.get("reddit_queries_per_day", 4), len(patterns))
    for i in range(daily):
        # Rotate a whole daily block rather than repeating three of yesterday's four.
        reddit(patterns[(today.toordinal() * daily + i) % len(patterns)], "Reddit task discovery")
    if not patterns:
        record("reddit", "", status="unavailable", reason="No discovery patterns configured")

    tasks, phrases, task_ids = [], set(), set()
    for task in followups:
        variants = query_variants(task)
        phrase = variants[0] if variants else ""
        task_id = str(task.get("id") or "")
        if phrase and phrase.lower() not in phrases and (not task_id or task_id not in task_ids):
            constraints = task_constraint_context(task)
            tasks.append((phrase, task_id, "followup", variants, constraints))
            phrases.add(phrase.lower())
            task_ids.add(task_id)
    if not tasks:
        seeds = c.get("seed_task_phrases", SEED_TASKS)
        for seed in seeds:
            phrase = _phrase(seed)
            if phrase and phrase.lower() not in phrases:
                tasks.append((phrase, "", "discovery_seed", query_variants({"search_terms": [phrase]}), seed))
                phrases.add(phrase.lower())
    tasks = tasks[:c.get("pain_tasks_per_day", 3)]
    video_seen, extension_seen = set(), set()
    for phrase, task_id, basis, variants, constraints in tasks:
        # Each alternate is a distinct bounded search, never a retry of a failed request.
        # Keep all returned raw text, including lexical mismatches, for semantic review.
        for index, variant in enumerate(variants[:min(2, max(1, c.get("query_variants_per_task", 2)))]):
            check = reddit(variant, "Task pain discovery", task_id, phrase, index, constraints)
            if check["status"] != "complete" or check["relevant_candidates"] > 0:
                break
        check = record("youtube_search", f"site:youtube.com {phrase} tutorial", task_id=task_id, basis=basis)
        try:
            page, spent = competition.page_one(check["query"], cfg.get("competition", {}).get("location_code", 2840))
            check["response_received"] = True
            cost += spent
            results = page["results"]
            videos, candidates, matched = [], [], set()
            for row in results:
                video = _video(row.get("url", ""))
                if not video or video in matched:
                    continue
                matched.add(video)
                title = row.get("title") if isinstance(row.get("title"), str) else ""
                summary = row.get("description") or row.get("snippet") or ""
                summary = summary if isinstance(summary, str) else ""
                match = relevance(phrase, title + " " + summary, constraint_context=constraints, title=title)
                if not (title + summary).strip():
                    match.update(candidate=False, reason="title/summary unavailable; manual context review required before paid comments")
                candidates.append({"video_id": video, "url": row.get("url"), "title": title,
                                   "summary": summary, "status": "candidate" if match["candidate"] else "skipped",
                                   **match})
                if match["candidate"]:
                    videos.append(video)
            check.update(status="complete", returned=len(results), matched=len(matched),
                         relevant_candidates=len(videos), candidates=candidates,
                         relevance="lexical candidate triage only; not confirmed same-task evidence")
            if not videos:
                check["reason"] = "No title/summary candidate selected; no conclusion about absence of user demand"
        except Exception as exc:
            failed(check, exc)
            videos = []
        for video in videos[:c.get("videos_per_task", 2)]:
            if video in video_seen:
                continue
            video_seen.add(video)
            check = record("youtube_comments", video, task_id=task_id, basis=basis,
                           scope="one top-level comment page; replies not read")
            try:
                rows, spent = social.youtube_comments(video)
                check["response_received"] = True
                cost += spent
                if not isinstance(rows, list):
                    raise ValueError("YouTube comments missing")
                check["returned"] = len(rows)
                for row in rows:
                    cid = row.get("comment_id") or row.get("commentId") or row.get("id") or ""
                    cid = cid if isinstance(cid, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", cid) else ""
                    url = "https://www.youtube.com/watch?" + urlencode({"v": video, "lc": cid}) if cid else f"https://youtu.be/{video}"
                    add(check, {"kind": "comment", "text": row.get("content") or row.get("text") or "",
                                "url": url, "author": _channel(row), "topic": phrase, "task_id": task_id,
                                "created": row.get("created") or row.get("published_time") or row.get("publishedAt") or "",
                                **{field: row.get(field) for field in ("author_url", "author_display_name", "identity_status",
                                   "publication_date", "provider_timestamp", "created_precision", "reply_count", "video_metadata")},
                                "source_time_note": "采集日期不是评论发表日期；相对时间换算值不代表精确日期，历史评论不代表今天新出现的需求。",
                                "context_status": "video_and_replies_not_read", "source_record_id": cid})
                check.update(status="complete")
            except Exception as exc:
                failed(check, exc)

        check = record("chrome_search", f"site:chromewebstore.google.com {phrase}", task_id=task_id, basis=basis)
        if not goanyapi.key():
            check.update(status="unavailable", reason="GOANYAPI_API_KEY missing; Chrome reviews not observed")
            continue
        try:
            results, spent = goanyapi.serp(check["query"])
            check["response_received"] = True
            cost += spent
            extensions = []
            for row in results:
                p = urlparse(row.get("url", ""))
                match = EXTENSION.fullmatch(p.path) if p.scheme == "https" and p.hostname == "chromewebstore.google.com" else None
                if match and match[1] not in extensions:
                    extensions.append(match[1])
            check.update(status="complete", returned=len(results), matched=len(extensions))
        except Exception as exc:
            failed(check, exc)
            extensions = []
        for extension in extensions[:c.get("chrome_extensions_per_task", 2)]:
            if extension in extension_seen:
                continue
            extension_seen.add(extension)
            cursor = ""
            for index in range(c.get("chrome_review_pages", 1)):
                check = record("chrome_reviews", extension, task_id=task_id, page=index + 1,
                               scope="bounded review pages; shared extension URL cannot establish independent evidence")
                try:
                    data, spent = goanyapi.call("chrome-web-store-reviews", extensionId=extension, **({"cursor": cursor} if cursor else {}))
                    check["response_received"] = True
                    cost += spent
                    rows = data.get("comments")
                    if not isinstance(rows, list):
                        raise ValueError("Chrome comments missing")
                    check["returned"] = len(rows)
                    for row in rows:
                        author = row.get("author") if isinstance(row.get("author"), dict) else {}
                        identity = row.get("authorId") or row.get("userId") or author.get("id") or author.get("userId")
                        add(check, {"kind": "comment", "text": row.get("content") or row.get("text") or "",
                                    "url": f"https://chromewebstore.google.com/detail/{extension}",
                                    "author": _identity("chrome", identity), "topic": phrase, "task_id": task_id,
                                    "created": row.get("createdAt", ""), "rating": row.get("rating"),
                                    "context_status": "extension_and_author_context_not_read",
                                    "source_record_id": row.get("id") or row.get("reviewId") or ""})
                    cursor = (data.get("nextCursor") or "") if data.get("hasMore") else ""
                    check.update(status="complete", more_pages=bool(cursor))
                    if not cursor:
                        break
                except Exception as exc:
                    failed(check, exc)
                    break
    return evidence, errors, {"queries": checks, "cost_micros": cost, "research_tasks": len(tasks),
                              "product_context_count": len(selected), "text_policy": "full API text; provider completeness unknown",
                              **followup_summary(checks)}
