"""config.json: what to watch. Copy config.example.json and edit it.

Everything has a default except the X accounts and the sitemaps, which only you can
choose. Secrets are never read from this file; they come from the environment. With no
config.json at all, the radar runs on the example it ships with (EXAMPLE) and says so.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from . import i18n, terms

# The repository's config.example.json, copied into the package so an installed radar
# (uvx, pip) runs without a clone. A test keeps the two files identical.
EXAMPLE = Path(__file__).with_name("config.example.json")
DEFAULT = Path("config.json")

DEFAULTS = {
    "lang": "en",
    "x_accounts": {"labs": [], "bloggers": []},
    "sitemaps": {},
    "topics": {},
    "known_terms": [],
    "stop_words": [],
    "judge": {"model": "gpt-4.1-mini", "min_score": 0.5},
    "breakout": {"window_days": 14, "min_stars": 300, "velocity_min": 200, "delta_min": 400},
    "new_sites": {"queries": {"ai": "%ai%"}, "weekday": 0, "window_days": 90, "min_etv": 1000, "min_keywords": 20,
                  "words": 10},
    "review": {"after_days": 14, "per_day": 5},
    "usecases": {"per_day": 5, "model": "claude-sonnet-4-6", "min_volume": 50, "trends_max": 10, "recheck_days": 3,
                 "location_code": 2840, "benchmark": "gpts", "min_daily": 300, "related": True, "related_geo": "US",
                 "country_trends": True, "country_min_volume": 1000, "country_max": 8},
    "competition": {"max_rows": 8, "weak_dr": 30, "location_code": 2840, "page_age": True},
    # for the keyword table's prioritize and watch rows (radar/wordage.py, markets.py, serpshape.py)
    "word_age": {"enabled": True, "time_range": "past_5_years", "new_days": 180},
    "markets": {"enabled": True, "codes": ["US", "DE", "FR", "JP"], "difficulty": True, "top_region": True, "locations": {}},
    "serp_shape": {"enabled": True, "location_code": 2840, "language_code": "en", "device": "desktop",
                   "platforms": ["github.com", "medium.com", "sites.google.com", "*.github.io", "reddit.com", "youtube.com", "wikipedia.org"]},
    # 0.7: your own rank and the twelve months (acted-on rows), the exact-match .com (prioritized rows),
    # and what AI answers recommend for ⑦'s asks (radar/yours.py, curve.py, dotcom.py, ai_answers.py)
    "yours": {"enabled": True, "domains": [], "days": 28},
    "year_curve": {"enabled": True},
    "dot_com": {"enabled": True, "max_rows": 8},
    "ai_answers": {"enabled": False, "engines": ["chatgpt"], "max_rows": 6, "prompt": "What is the best tool to {job}?",
                   "location_code": 2840, "language_code": "en"},
    "revenue": {"weekday": 0, "pages_growth": 3, "pages_new": 3, "min_revenue_usd": 500, "category": "", "max_leads": 10},
    "asks": {"extra_sources": ["x", "stackexchange", "freelancer"],
             "x_query": '("is there a tool" OR "is there an app" OR "any tool that" OR "looking for a tool" OR "wish there was an app")',
             "x_min_likes": 10, "stack_all": ["softwarerecs"], "max_age_days": 14, "stack_sites": ["stackoverflow", "superuser", "webapps"], "stack_titles": ["automate"],
             "freelancer_queries": ["automate", "automation", "scrape", "bulk", "data entry"],
             "weekday": 0, "time": "week", "min_score": 2, "model": "gpt-4.1-mini", "min_volume": 50, "max_leads": 12,
             "location_code": 2840, "patterns": [
                 '"is there an ai tool"', '"is there an ai that"', '"is there a tool that"', '"any ai tool for"',
                 '"looking for an ai tool"', '"i wish there was a tool"', '"is there a way to automatically"',
                 '"how do i automate"', '"tool that can automatically"', '"recommend an ai"', '"best ai tool for"',
                 '"ai tool that can"']},
    "pains": {"model": "gpt-4.1-mini", "extensions": 5, "review_pages": 2, "max_stars": 3, "reddit_time": "year", "videos": 3, "apps": 3,
              "reddit_queries": ["{k} alternative", "{k} frustrating", '"{k}" "doesn\'t work"', "{k} wish there was"]},
    "tutorials": {"max_leads": 3, "videos": 3, "model": "gpt-4.1-mini"},
    "sellers": {"weekday": 0, "platforms": ["stripe", "polar", "lemonsqueezy", "gumroad", "creem", "paddle", "dodo", "waffo"],
                "per_platform": 100,
                "max_leads": 12, "visits": True, "rank_max": 350, "max_visits": 300000, "pool": 60, "model": "gpt-4.1-mini"},
    "builders": {"weekday": 0, "platforms": ["lovable", "v0", "beehiiv"], "per_platform": 100, "rank_max": 350, "pool": 40,
                 "max_leads": 12},
    "feeds": {"enabled": ["producthunt", "hn", "openrouter", "appstore", "awesome", "google_new", "trending", "rising"],   # civitai: anime checkpoints, mostly
              "weekly": ["google_new", "rising"], "weekday": 0, "max_per_feed": 10, "hn_points": 30,
              "app_genres": [6007, 6008, 6027, 6002],      # Productivity, Photo & Video, Graphics & Design, Utilities
              "awesome": ["https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md"],
              "google_new": ["figma.com/community/plugin", "theresanaiforthat.com/ai/", "chromewebstore.google.com/detail"],
              "trending": ["douyin", "tiktok", "tiktok_tags", "reddit"],
              "seeds": ["ai generator", "ai detector", "ai converter", "ai maker", "ai checker", "ai editor", "ai translator", "ai agent"]},
    "money": {"weekday": 0, "portfolio_sites": 12, "max_dr": 60, "max_sisters": 30,
              "ad_terms": ["ai ppt", "claude skills", "mcp server", "claude powerpoint"]},
    "sidelights": {"names": 5, "whois_names": 2, "whois_days": 30, "products": 8, "min_volume": 50, "location_code": 2840},
    "gsc": {},
    "google_trends": {"enabled": False, "geo": "US"},
    "demands": {"enabled": True, "free_sources": True, "technology_discovery": False, "preprocessing_mode": "full"},
    "research": {"enabled": False, "reddit_queries_per_day": 4, "pain_tasks_per_day": 3,
                 "query_variants_per_task": 2,
                 "videos_per_task": 2, "chrome_extensions_per_task": 2, "chrome_review_pages": 1,
                 "seed_task_phrases": ["batch document conversion", "invoice data extraction", "subtitle editing"]},
    "opportunities": {"enabled": False, "model": "qwen3.7-flash", "budget_usd": 0.20,
                      "candidates_per_day": 2, "problem_queries_per_day": 4, "recheck_days": 7},
}


def load(path: Path | None = None) -> dict:
    """`path`, or with None ./config.json, or the shipped example when there is none."""
    raw, base = _read(path)
    cfg = {}
    for k, default in DEFAULTS.items():
        v = raw.get(k, default)
        cfg[k] = {**default, **v} if isinstance(default, dict) and default and isinstance(v, dict) else v
    cfg["_dir"] = base
    gt = cfg["google_trends"]
    if (not isinstance(gt.get("enabled"), bool) or not isinstance(gt.get("geo"), str) or
        (gt["geo"] and (len(gt["geo"]) != 2 or not gt["geo"].isascii() or not gt["geo"].isupper() or not gt["geo"].isalpha()))):
        raise ValueError("Google Trends geo must be a two-letter country code or empty for worldwide")
    c = cfg["opportunities"]
    if not isinstance(cfg["demands"], dict) or any(not isinstance(v, bool) for k, v in cfg["demands"].items() if k != "preprocessing_mode"):
        raise ValueError("Demand options must be booleans")
    if cfg["demands"].get("preprocessing_mode") not in {"full", "extract_only"}:
        raise ValueError("Demand preprocessing_mode must be full or extract_only")
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


def _read(path: Path | None) -> tuple[dict, Path]:
    """The raw settings and the directory files named in them are relative to. A path given
    must exist; only the default falls back to the example, with a note on stderr."""
    if path is None and not DEFAULT.exists():
        print(f"No config.json here: using the example list of X accounts and sitemaps that ships with "
              f"the radar. Write a config.json in {Path.cwd()} to use your own.", file=sys.stderr)
        return json.loads(EXAMPLE.read_text()), Path.cwd()
    path = path or DEFAULT
    if not path.exists():
        raise SystemExit(f"{path} not found: copy config.example.json to {path.name} and edit it")
    return json.loads(path.read_text()), path.resolve().parent


def known(cfg: dict, term: str) -> str:
    """The entry of known_terms that already covers this term, if any: what you have a
    page for, or never want reported."""
    for k in cfg["known_terms"]:
        k = k.lower()
        if k and (k in term or term in k):
            return k
    return ""
