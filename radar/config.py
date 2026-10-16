"""config.json: what to watch. Copy config.example.json and edit it.

Everything has a default except the X accounts and the sitemaps, which only you can
choose. Secrets are never read from this file; they come from the environment.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import i18n, terms

DEFAULTS = {
    "lang": "en",
    "x_accounts": {"labs": [], "bloggers": []},
    "sitemaps": {},
    "topics": {},
    "known_terms": [],
    "stop_words": [],
    "judge": {"model": "gpt-4.1-mini", "min_score": 0.5},
    "breakout": {"window_days": 14, "min_stars": 300, "velocity_min": 200, "delta_min": 400},
    "new_sites": {"queries": {"ai": "%ai%"}, "weekday": 0, "window_days": 90, "min_etv": 1000, "min_keywords": 20},
    "review": {"after_days": 14, "per_day": 5},
    "usecases": {"per_day": 5, "model": "claude-sonnet-4-6", "min_volume": 50, "trends_max": 10, "recheck_days": 3,
                 "location_code": 2840, "benchmark": "gpts", "min_daily": 300, "related": True, "related_geo": "US",
                 "country_trends": True, "country_min_volume": 1000, "country_max": 8},
    "competition": {"max_rows": 8, "weak_dr": 30, "location_code": 2840},
    "revenue": {"weekday": 0, "pages_growth": 3, "pages_new": 3, "min_revenue_usd": 500, "category": "", "max_leads": 10},
    "asks": {"weekday": 0, "time": "week", "min_score": 2, "model": "gpt-4.1-mini", "min_volume": 50, "max_leads": 12,
             "location_code": 2840, "patterns": [
                 '"is there an ai tool"', '"is there an ai that"', '"is there a tool that"', '"any ai tool for"',
                 '"looking for an ai tool"', '"i wish there was a tool"', '"is there a way to automatically"',
                 '"how do i automate"', '"tool that can automatically"', '"recommend an ai"', '"best ai tool for"',
                 '"ai tool that can"']},
    "pains": {"model": "gpt-4.1-mini", "extensions": 5, "review_pages": 2, "max_stars": 3, "reddit_time": "year", "videos": 3,
              "reddit_queries": ["{k} alternative", "{k} frustrating", '"{k}" "doesn\'t work"', "{k} wish there was"]},
    "tutorials": {"max_leads": 3, "videos": 3, "model": "gpt-4.1-mini"},
    "gsc": {},
    "google_trends": {"enabled": False, "geo": "US"},
    "demands": {"enabled": True, "free_sources": True, "technology_discovery": False},
    "research": {"enabled": False, "reddit_queries_per_day": 4, "pain_tasks_per_day": 3,
                 "query_variants_per_task": 2,
                 "videos_per_task": 2, "chrome_extensions_per_task": 2, "chrome_review_pages": 1,
                 "seed_task_phrases": ["batch document conversion", "invoice data extraction", "subtitle editing"]},
    "opportunities": {"enabled": False, "model": "qwen3.7-flash", "budget_usd": 0.20,
                      "candidates_per_day": 2, "problem_queries_per_day": 4, "recheck_days": 7},
}


def load(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"{path} not found: copy config.example.json to {path.name} and edit it")
    raw = json.loads(path.read_text())
    cfg = {}
    for k, default in DEFAULTS.items():
        v = raw.get(k, default)
        cfg[k] = {**default, **v} if isinstance(default, dict) and default and isinstance(v, dict) else v
    cfg["_dir"] = path.resolve().parent
    gt = cfg["google_trends"]
    if (not isinstance(gt.get("enabled"), bool) or not isinstance(gt.get("geo"), str) or
        (gt["geo"] and (len(gt["geo"]) != 2 or not gt["geo"].isascii() or not gt["geo"].isupper() or not gt["geo"].isalpha()))):
        raise ValueError("Google Trends geo must be a two-letter country code or empty for worldwide")
    c = cfg["opportunities"]
    if not isinstance(cfg["demands"], dict) or any(not isinstance(v, bool) for v in cfg["demands"].values()):
        raise ValueError("Demand options must be booleans")
    budget = c["budget_usd"]
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or
                               not math.isfinite(budget) or budget <= 0):
        raise ValueError("Opportunity budget must be a positive amount or null for balance-limited research")
    research = cfg["research"]
    from .research_queries import DEFAULT_DISCOVERY_QUERIES
    research.setdefault("discovery_queries", list(DEFAULT_DISCOVERY_QUERIES))
    if not isinstance(research.get("enabled"), bool):
        raise ValueError("research.enabled must be boolean")
    for key, maximum in {"reddit_queries_per_day": 12, "pain_tasks_per_day": 6, "videos_per_task": 5,
                         "chrome_extensions_per_task": 5, "chrome_review_pages": 3}.items():
        if type(research.get(key)) is not int or not 0 <= research[key] <= maximum:
            raise ValueError("Invalid research sampling limit: " + key)
    if not isinstance(research.get("seed_task_phrases"), list) or any(
        not isinstance(v, str) or not v.strip() or len(v) > 80 for v in research["seed_task_phrases"]):
        raise ValueError("Research seed phrases must be nonempty strings up to 80 characters")
    if (type(research.get("query_variants_per_task")) is not int or
            not 1 <= research["query_variants_per_task"] <= 2):
        raise ValueError("Research query variants must be 1 or 2")
    if (not isinstance(research["discovery_queries"], list) or not research["discovery_queries"] or
            len(research["discovery_queries"]) > 24 or any(not isinstance(v, str) or not v.strip() or
            len(v) > 240 for v in research["discovery_queries"])):
        raise ValueError("Research discovery queries must contain 1 to 24 bounded strings")
    if not (1 <= c["candidates_per_day"] <= 6 and
            1 <= c["problem_queries_per_day"] <= 4 and c["recheck_days"] >= 1):
        raise ValueError("Invalid opportunities limits")
    i18n.set_lang(cfg["lang"])
    terms.add_stop(cfg["stop_words"])
    return cfg


def known(cfg: dict, term: str) -> str:
    """The entry of known_terms that already covers this term, if any: what you have a
    page for, or never want reported."""
    for k in cfg["known_terms"]:
        k = k.lower()
        if k and (k in term or term in k):
            return k
    return ""
