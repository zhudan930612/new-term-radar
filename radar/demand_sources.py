"""Task-first discovery without named-product or popularity requirements."""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import UA, breakout, opportunities
from .research_queries import followup_summary, query_variants

TASK_QUERIES = [
    '("I manually" OR "we manually" OR "takes hours") (spreadsheet OR invoice OR report) -filter:retweets',
    '("I need" OR "looking for a tool") (batch OR bulk) (pdf OR images OR files) -filter:retweets',
    '(editing OR subtitles) (manually OR "takes hours" OR "2 hours") -filter:retweets',
    '("copy paste" OR "copying and pasting") (clients OR report OR data) -filter:retweets',
    '("手动整理" OR "每次都要" OR "太耗时间") (表格 OR 文件 OR 字幕 OR 发票) -filter:retweets',
]
GITHUB_QUERIES = [
    'is:issue is:open is:public "use case" "spreadsheet"',
    'is:issue is:open is:public "manually" "export"',
    'is:issue is:open is:public "batch" "pdf"',
    'is:issue is:open is:public "workflow" "subtitle"',
]


def query_phrase(task: dict) -> str:
    # Stored/model-generated terms cannot inject search operators or change scope.
    variants = query_variants(task)
    return variants[0] if variants else ""


def collect(selected: list[dict], today: date, query_count: int, followups: list[dict],
            free_sources: bool = True, research_config: dict | None = None) -> tuple[list[dict], list[str], dict]:
    evidence, errors, checks = [], [], []
    seen = set()

    def add(row):
        text = row.get("text", "")
        url = row.get("url", "")
        if not isinstance(text, str) or not text.strip() or not opportunities.safe_url(url) or (url, text) in seen:
            return False
        seen.add((url, text))
        evidence.append({**row, "id": f"e{len(evidence)+1}", "text": text,
                         "truncated": bool(row.get("truncated", False)), "locally_truncated": False,
                         "provider_text_completeness": row.get("provider_text_completeness", "unknown"),
                         "observed_on": today.isoformat()})
        return True

    since = (today - timedelta(days=30)).isoformat()
    # Reserve a discovery slot for targeted second-evidence search; no product gate.
    usable = [(t, query_phrase(t)) for t in followups if query_phrase(t)][:min(1, query_count)]
    queries = [(TASK_QUERIES[(today.toordinal() + i) % len(TASK_QUERIES)], "任务发现", "")
               for i in range(query_count - len(usable))]
    queries += [(f'{phrase} ("I" OR "need" OR "manually" OR "problem") -filter:retweets', "定向补证", task["id"])
                for task, phrase in usable]
    for query, topic, task_id in queries:
        record = {"source": "x", "query": query + f" since:{since}", "lane": topic, "task_id": task_id,
                  "accepted": 0, "scope": "first returned page; up to 12 eligible posts; not exhaustive"}
        try:
            rows = opportunities.tweets(record["query"])
            record["response_received"] = True
            considered = 0
            for tw in rows:
                considered += 1
                if tw.get("isRetweet") or tw.get("text", "").startswith("RT @") or (tw.get("author") or {}).get("isAutomated"):
                    continue
                if add({"kind": "post", "text": tw.get("text", ""), "url": tw.get("url", ""),
                     "author": "x:" + (tw.get("author") or {}).get("userName", "").lower(),
                      "topic": topic, "task_id": task_id, "query": record["query"], "query_source": "x",
                      "created": tw.get("createdAt", ""), "context_status": "thread_not_read"}):
                    record["accepted"] += 1
                if record["accepted"] >= 12:
                    break
            record.update(status="complete", returned=len(rows), considered=considered)
        except Exception as exc:
            record.update(status="failed", error=type(exc).__name__)
            if getattr(exc, "request_sent", None) is not None:
                record["request_sent"] = exc.request_sent
            errors.append(topic + ": " + type(exc).__name__)
        checks.append(record)
    # Free, task-led cross-repository requests, irrespective of repo age/stars/name.
    if free_sources:
        query = GITHUB_QUERIES[today.toordinal() % len(GITHUB_QUERIES)] + f" updated:>={since}"
        record = {"source": "github", "query": query, "lane": "任务发现"}
        try:
            response = breakout._get("/search/issues?" + urllib.parse.urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": 10}))
            if response.get("incomplete_results"):
                raise ValueError("GitHub search incomplete")
            for row in response.get("items", []):
                user = row.get("user") or {}
                if "pull_request" in row or user.get("type") == "Bot":
                    continue
                add({"kind": "issue", "text": row.get("title", "") + "\n" + (row.get("body") or ""),
                     "url": row.get("html_url", ""), "author": "github:" + user.get("login", "").lower(),
                     "topic": "跨仓库任务", "created": row.get("created_at", ""),
                     "updated": row.get("updated_at", ""), "issue_state": row.get("state"),
                     "association": row.get("author_association"), "context_status": "comments_not_read"})
            record.update(status="complete", returned=len(response.get("items", [])))
        except Exception as exc:
            record.update(status="failed", error=type(exc).__name__)
            errors.append("GitHub 任务来源: " + type(exc).__name__)
        checks.append(record)
        # HN public discussions add a source outside the product's own promotion/issue stream.
        phrase = ["manual spreadsheet", "looking for tool", "editing workflow", "invoice processing"][today.toordinal() % 4]
        record = {"source": "hackernews", "query": phrase, "lane": "任务发现"}
        try:
            cutoff = int(datetime.combine(today - timedelta(days=90), datetime.min.time(), timezone.utc).timestamp())
            url = "https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode({
                "query": phrase, "tags": "comment", "hitsPerPage": 10, "numericFilters": f"created_at_i>{cutoff}"})
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=25) as response:
                rows = json.load(response)["hits"]
            for row in rows:
                add({"kind": "comment", "text": html.unescape(re.sub(r"<[^>]+>", " ", row.get("comment_text") or "")),
                     "url": "https://news.ycombinator.com/item?id=" + str(row["objectID"]),
                     "author": "hn:" + row.get("author", "").lower(), "topic": "公开任务讨论",
                     "created": row.get("created_at", ""), "context_status": "parent_thread_not_read"})
            record.update(status="complete", returned=len(rows))
        except Exception as exc:
            record.update(status="failed", error=type(exc).__name__)
            errors.append("HN 任务来源: " + type(exc).__name__)
        checks.append(record)
    # Technology discovery is optional context, not an admission requirement for tasks.
    context, context_errors = opportunities.collect(selected, today, 0)
    for row in context:
        add(row)
    errors += context_errors
    return evidence, errors, {"queries": checks, "task_queries": len(queries), "product_candidates": len(selected),
                             "text_policy": "full returned text; provider completeness unknown",
                             **followup_summary(checks)}
