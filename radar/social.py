"""Where people ask for things in their own words: Reddit posts and YouTube comments, through AIsa.

reddit(): AIsa's Reddit search with full post text and account identity.
youtube_comments(): DataForSEO comments through AIsa. Prices are quoted at runtime.
Both are read for research: quote them, do not republish them.
"""
from __future__ import annotations

import re

from . import aisa

REDDIT_TOOL = "get_reddit_search"
COMMENTS_TOOL = "post_dataforseo_serp_youtube_video_comments_live"


def reddit(query: str, time: str = "week", sort: str = "top", limit: int = 25) -> tuple[list[dict], int]:
    data, cost = aisa.call([{"call_id": "r", "tool": REDDIT_TOOL, "arguments": {
        "query": query, "sort": sort, "timeframe": time, "trim": False}}])
    posts = []
    payload = data.get("r") or {}
    if payload.get("success") is False or not isinstance(payload.get("posts"), list):
        raise aisa.AisaError("Reddit response unavailable")
    for p in payload["posts"][:limit]:
        if p.get("over_18"):
            continue
        posts.append({"id": p.get("id") or p.get("post_id") or "", "title": " ".join(str(p.get("title") or "").split()),
                      "text": str(p.get("selftext") or ""), "score": int(p.get("score") or 0),
                      "comments": int(p.get("num_comments") or 0), "subreddit": p.get("subreddit") or "",
                      "url": ("https://www.reddit.com" + p["permalink"] if str(p.get("permalink", "")).startswith("/")
                              else p.get("url") or ""), "created": float(p.get("created_utc") or 0),
                      "author": p.get("author") or "", "author_id": p.get("author_fullname") or ""})
    return posts, cost


def youtube_comments(video_id: str) -> tuple[list[dict], int]:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) is None:
        raise ValueError("Invalid YouTube video ID")
    data, cost = aisa.call([{"call_id": "y", "tool": COMMENTS_TOOL, "arguments": {"body": [
        {"video_id": video_id, "location_code": 2840, "language_code": "en"}]}}])
    tasks = (data.get("y") or {}).get("tasks") or []
    if not isinstance(tasks, list) or not tasks or any(
            not isinstance(t, dict) or t.get("status_code", 20000) != 20000 or
            not isinstance(t.get("result"), list) or not t["result"] or any(
                not isinstance(r, dict) or not isinstance(r.get("items"), list) for r in t["result"])
            for t in tasks):
        raise aisa.AisaError("YouTube comments response unavailable")
    rows = [row for task in tasks for result in task["result"] for row in result.get("items", [])
            if row.get("type") == "youtube_comment"]
    comments = []
    for row in rows:
        author_url = str(row.get("author_url") or "")
        channel = re.fullmatch(r"https://(?:www\.)?youtube\.com/channel/([A-Za-z0-9_-]+)", author_url)
        comment_id = str(row.get("comment_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", comment_id):
            comment_id = ""
        comments.append({"text": str(row.get("text") or ""), "likes": int(row.get("likes_count") or 0),
                         "author_url": author_url, "author_id": channel[1] if channel else "",
                         "id": comment_id, "created": row.get("timestamp") or "",
                         "url": (f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}" if comment_id
                                 else f"https://youtu.be/{video_id}")})
    return comments, cost
