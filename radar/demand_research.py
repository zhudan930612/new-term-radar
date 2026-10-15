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
        check.update(status="failed", error=type(exc).__name__)
        # Provider exception bodies can contain request details; retain only their type.
        errors.append(check["source"] + ": " + type(exc).__name__)

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
        evidence.append({**row, "id": f"research{len(evidence) + 1}", "observed_on": today.isoformat(),
                         "truncated": False, "provider_text_completeness": "unknown"})
        check["accepted"] += 1

    def call(tool, arguments):
        nonlocal cost
        response, spent = aisa.call([{"call_id": "research", "tool": tool, "arguments": arguments}])
        cost += spent
        if "research" not in response or not isinstance(response["research"], dict):
            raise aisa.AisaError("Research response absent")
        return response["research"]

    def reddit(query, lane, task_id=""):
        check = record("reddit", query, lane=lane, task_id=task_id, window="month")
        try:
            data = call(social.REDDIT_TOOL, {"query": query, "sort": "new", "timeframe": "month", "trim": False})
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
                add(check, {"kind": "post", "text": text, "url": url,
                            "author": _identity("reddit", author.lower() if isinstance(author, str) else ""),
                            "topic": lane, "task_id": task_id, "created": row.get("created_utc", ""),
                            "context_status": "thread_not_read", "source_record_id": row.get("id") or row.get("post_id") or ""})
            check["status"] = "complete"
        except Exception as exc:
            failed(check, exc)

    patterns = [p for p in cfg.get("asks", {}).get("patterns", []) if isinstance(p, str) and p.strip()]
    for i in range(min(c.get("reddit_queries_per_day", 4), len(patterns))):
        reddit(patterns[(today.toordinal() + i) % len(patterns)], "Reddit task discovery")
    if not patterns:
        record("reddit", "", status="unavailable", reason="No discovery patterns configured")

    tasks, phrases = [], set()
    for task in followups:
        phrase = next((_phrase(x) for x in task.get("search_terms", []) if _phrase(x)), "")
        if phrase and phrase.lower() not in phrases:
            tasks.append((phrase, str(task.get("id") or ""), "followup"))
            phrases.add(phrase.lower())
    if not tasks:
        seeds = c.get("seed_task_phrases", SEED_TASKS)
        for seed in seeds:
            phrase = _phrase(seed)
            if phrase and phrase.lower() not in phrases:
                tasks.append((phrase, "", "discovery_seed"))
                phrases.add(phrase.lower())
    tasks = tasks[:c.get("pain_tasks_per_day", 3)]
    video_seen, extension_seen = set(), set()
    for phrase, task_id, basis in tasks:
        reddit(f'"{phrase}" (frustrating OR manually OR "does not work" OR "wish")', "Task pain discovery", task_id)
        check = record("youtube_search", f"site:youtube.com {phrase} tutorial", task_id=task_id, basis=basis)
        try:
            page, spent = competition.page_one(check["query"], cfg.get("competition", {}).get("location_code", 2840))
            cost += spent
            results = page["results"]
            videos = list(dict.fromkeys(v for row in results if (v := _video(row.get("url", "")))))
            check.update(status="complete", returned=len(results), matched=len(videos))
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
                                "context_status": "video_and_replies_not_read", "source_record_id": cid})
                check["status"] = "complete"
            except Exception as exc:
                failed(check, exc)

        check = record("chrome_search", f"site:chromewebstore.google.com {phrase}", task_id=task_id, basis=basis)
        if not goanyapi.key():
            check.update(status="unavailable", reason="GOANYAPI_API_KEY missing; Chrome reviews not observed")
            continue
        try:
            results, spent = goanyapi.serp(check["query"])
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
                              "successful_followups": sorted({r["task_id"] for r in checks if r.get("task_id") and r["status"] == "complete"})}
