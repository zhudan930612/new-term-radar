"""Optional second data source: GoAnyAPI (goanyapi.com), for what AIsa's tools do not give.

Used for two things: Google Trends' *related queries* for a name, the terms real people
searched together with it, and a 30-day daily Trends series for one country, which
arrives settled up to yesterday (DataForSEO's worldwide series does not). Key: GOANYAPI_API_KEY. Without it every caller skips
this source and the radar runs as before.

One credit is $0.001; a related-queries lookup costs 6 (October 2026). Calls take 20 to
55 seconds and may fail with a timeout or a 502. Failed paid requests are not retried
automatically; failures remain explicit. The service is young (first released August
2026): treat it as an enhancement, not a dependency.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
import os
import urllib.error
import urllib.parse
import urllib.request

from . import UA

BASE = os.environ.get("GOANYAPI_BASE_URL", "https://api.goanyapi.com") + "/api/v1/"
TIMEOUT = 90
TRIES = 1
MICROS_PER_CREDIT = 1000
_REQUEST_SESSION = ContextVar('goany_request_session', default=None)


@contextmanager
def managed_requests(session):
    token = _REQUEST_SESSION.set(session)
    try:
        yield
    finally:
        _REQUEST_SESSION.reset(token)


class GoAnyError(RuntimeError):
    pass


def key() -> str:
    return os.environ.get("GOANYAPI_API_KEY", "").strip()


def call(path: str, **params) -> tuple[dict, int]:
    """One GET. Returns (data, cost in micro-USD)."""
    session = _REQUEST_SESSION.get()
    return session.request(BASE, path, params, _send) if session else _send(path, **params)


def _send(path: str, **params) -> tuple[dict, int]:
    req = urllib.request.Request(BASE + path + "?" + urllib.parse.urlencode(params), headers={
        "authorization": f"Bearer {key()}", "accept": "application/json", "user-agent": UA})
    error = ""
    for _ in range(TRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                body = json.load(res)
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code}"
            if exc.code < 500:
                break
            continue
        except (OSError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            continue
        data = body.get("data") or {}
        if body.get("code") != "ok":
            raise GoAnyError(str(body.get("message") or body)[:160])
        return data, int(data.get("costCredits") or data.get("creditsUsed") or 0) * MICROS_PER_CREDIT
    raise GoAnyError(error[:160])


def related_queries(term: str, geo: str = "US", window: str = "today 1-m") -> tuple[dict[str, list[str]], int]:
    """{"rising": [...], "top": [...]}: what people searched on Google together with `term`."""
    data, cost = call("google-trends", q=term, date=window, geo=geo, data_type="RELATED_QUERIES")
    rq = data.get("relatedQueries") or {}
    return {k: [" ".join(str(x.get("query", "")).lower().split()) for x in rq.get(k) or []] for k in ("rising", "top")}, cost


def daily_series(term: str, geo: str = "US", window: str = "today 1-m") -> tuple[list[tuple[date, int]], int]:
    """[(day, value)] for `term` in one country, the last month by day."""
    data, cost = call("google-trends", q=term, date=window, geo=geo, data_type="TIMESERIES")
    out = []
    for p in (data.get("interestOverTime") or {}).get("timeline_data") or []:
        try:
            day = datetime.fromtimestamp(int(p["timestamp"]), timezone.utc).date()
            out.append((day, int(p["values"][0]["extracted_value"])))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out, cost


def serp(q: str, gl: str = "US") -> tuple[list[dict], int]:
    """Google's organic results for a query (2 credits): [{"url", "title", "rank"}]."""
    data, cost = call("serp", q=q, gl=gl, hl="en")
    return [{"url": o.get("link") or "", "title": o.get("title") or "", "rank": o.get("rank")}
            for o in (data.get("result") or {}).get("organic") or []], cost


def intitle(term: str, window: str = "m") -> tuple[int | None, int]:
    """How many pages Google holds with `term` in the title, from the last week or month (4 credits)."""
    data, cost = call("google-intitle", intitle=term, gl="us", qdr=window)
    count = data.get("intitleCount")
    return (int(count) if isinstance(count, (int, float)) or str(count).isdigit() else None), cost


def advertisers(keyword: str) -> tuple[list[str], int]:
    """Domains and advertisers that run Google ads for a keyword (Ads transparency, 4 credits)."""
    data, cost = call("transparency", keyword=keyword)
    names = [str(a.get("name") or a) if isinstance(a, dict) else str(a) for a in data.get("advertisers") or []]
    return list(dict.fromkeys([*(data.get("domains") or []), *names])), cost


def chrome_reviews(extension_id: str, cursor: str = "") -> tuple[list[dict], str, int]:
    """One page of a Chrome Web Store extension's reviews (3 credits): ([{"rating", "text", "date"}], next cursor, cost)."""
    params = {"extensionId": extension_id, **({"cursor": cursor} if cursor else {})}
    data, cost = call("chrome-web-store-reviews", **params)
    rows = [{"rating": c.get("rating"), "text": " ".join(str(c.get("content") or "").split()), "date": str(c.get("createdAt") or "")[:10]}
            for c in data.get("comments") or []]
    return rows, (data.get("nextCursor") or "") if data.get("hasMore") else "", cost
