"""config.json: what to watch. Copy config.example.json and edit it.

Everything has a default except the X accounts and the sitemaps, which only you can
choose. Secrets are never read from this file; they come from the environment.
"""
from __future__ import annotations

import json
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
    "gsc": {},
    "google_trends": {"enabled": False, "geo": "US"},
    "demands": {"enabled": True, "free_sources": True, "technology_discovery": False},
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
    if not (0 < c["budget_usd"] <= 1 and 1 <= c["candidates_per_day"] <= 6 and
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
