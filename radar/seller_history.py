"""Every seller a payment platform has, back to 2019: the history behind signal ⑧.

    python -m radar history pull   [--platform stripe] [--max-usd 20]
    python -m radar history read   [--workers 32]
    python -m radar history sort   [--max-usd 30]
    python -m radar history        [--out sellers.csv]

`pull` reads every site that links to each platform today, one row per site with the day it
FIRST linked: DataForSEO's one-per-domain backlinks, oldest first, 1,000 a call ($0.06 on
2026-10-14). That day matched the site's referring-domain first_seen for every Polar site checked
(1,704). Stripe had about 143,000 such sites, Gumroad 140,000, Lemon Squeezy 18,000, Polar and
Creem about 2,000 each: about $18 for all five. Sites that took their link down are not in it, and
before mid-2021 the link graph saw few Stripe links. A pull stops at --max-usd and picks up where
it stopped on the next run.

`read` fetches each seller's home page title and description (free, `--workers` at a time;
about 10 sites a second), `sort` has a small model sort the ones that answered by kind (an
estimated $0.005 for 60 sites), and `history` alone counts sellers per month, platform and kind
and, with --out, writes every seller to a CSV. Only links that take money count as sellers (a
Gumroad product page, not a profile; see sellers.PLATFORMS); the rest are kept with pays = 0.

The rows are DataForSEO's: use them for research, do not republish them.
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from . import aisa, sellers

PAGE = 1000
KIND_BATCH = 60
KIND_MICROS = 5000          # an estimate: the chat endpoint does not report its price
READ_CHUNK = 500
READ_BYTES = 64_000      # off the wire: a gzipped head holds the title and description
SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (platform TEXT, host TEXT, first_seen TEXT, page TEXT, link TEXT, rank INTEGER, pays INTEGER,
    title TEXT, description TEXT, read_on TEXT, kind TEXT, kind_by TEXT, PRIMARY KEY (platform, host));
CREATE TABLE IF NOT EXISTS pulls (platform TEXT PRIMARY KEY, token TEXT, rows INTEGER, cost_micros INTEGER, done INTEGER, error TEXT);
"""


def log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def connect(state: Path) -> sqlite3.Connection:
    db = sqlite3.connect(state / "sellers_history.db")
    db.executescript(SCHEMA)
    return db


def page(platform: str, token: str) -> tuple[list[dict], str, int, str]:
    """One call: (sites, the token for the next page, cost in micro-USD, error)."""
    body = {"target": sellers.PLATFORMS[platform][0], "mode": "one_per_domain", "limit": PAGE,
            "order_by": ["first_seen,asc"], "backlinks_status_type": "live"}
    if token:
        body["search_after_token"] = token      # every other field must stay as it was
    data, cost = aisa.call([{"call_id": "h", "tool": sellers.TOOL, "arguments": {"body": [body]}}])
    task = ((data.get("h") or {}).get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        return [], token, cost, str(task.get("status_message") or "no answer")[:160]
    res = sellers.result(data, "h")
    return res.get("items") or [], res.get("search_after_token") or "", cost, ""


def row(platform: str, item: dict) -> tuple:
    host = (item.get("domain_from") or "").lower()
    link_host, path = sellers.split(item.get("url_to") or "")
    pays = sellers.PLATFORMS[platform][1](link_host, path) and not sellers.shared(host)
    return (platform, host, str(item.get("first_seen") or "")[:10], item.get("url_from") or "", item.get("url_to") or "",
            int(item.get("domain_from_rank") or 0), int(bool(pays)))


def progress(db: sqlite3.Connection, platform: str) -> list:
    got = db.execute("SELECT token, rows, cost_micros, done FROM pulls WHERE platform = ?", (platform,)).fetchone()
    return list(got) if got else ["", 0, 0, 0]


def pull_one(db: sqlite3.Connection, platform: str, budget: int) -> dict:
    """Pages of one platform until it is done or `budget` micro-USD are spent; saved after every page."""
    token, rows, cost, done = progress(db, platform)
    spent, error = 0, ""
    while not done and spent < budget:
        items, nxt, c, error = page(platform, token)
        spent, cost = spent + c, cost + c
        if not error:
            db.executemany("INSERT OR IGNORE INTO sites (platform, host, first_seen, page, link, rank, pays) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           [row(platform, i) for i in items if i.get("domain_from")])
            rows, token, done = rows + len(items), nxt, int(len(items) < PAGE or not nxt)
        db.execute("INSERT OR REPLACE INTO pulls VALUES (?, ?, ?, ?, ?, ?)", (platform, token, rows, cost, done, error))
        db.commit()
        log(f"{platform}: {rows:,} sites, ${cost / 1e6:.2f}" + (f", error: {error}" if error else ""))
        if error:
            break
    return {"rows": rows, "done": bool(done), "cost_micros": cost, "spent_micros": spent, "error": error}


def pull(db: sqlite3.Connection, platforms: list[str], max_micros: int) -> dict:
    out, spent = {}, 0
    for platform in platforms:
        out[platform] = pull_one(db, platform, max_micros - spent)
        spent += out[platform]["spent_micros"]
    return {"step": "pull", "platforms": out, "cost_micros": spent}


def read(db: sqlite3.Connection, workers: int) -> dict:
    """Title and description of every seller's home page not read yet."""
    hosts = [h for (h,) in db.execute("SELECT DISTINCT host FROM sites WHERE pays = 1 AND read_on IS NULL")]
    answered, today = 0, date.today().isoformat()
    with ThreadPoolExecutor(workers) as pool:
        for start in range(0, len(hosts), READ_CHUNK):
            chunk = hosts[start:start + READ_CHUNK]
            words = list(pool.map(lambda h: sellers.page_words(h, READ_BYTES), chunk))
            db.executemany("UPDATE sites SET title = ?, description = ?, read_on = ? WHERE host = ?",
                           [(t, d, today, h) for h, (t, d) in zip(chunk, words)])
            db.commit()
            answered += sum(1 for t, d in words if t or d)
            log(f"read {start + len(chunk):,}/{len(hosts):,} sites, {answered:,} answered")
    return {"step": "read", "read": len(hosts), "answered": answered}


def sort(db: sqlite3.Connection, model: str, max_micros: int, workers: int = 8) -> dict:
    """Kinds for the sellers whose page answered, `KIND_BATCH` to a model call, until the estimated budget."""
    rows = [{"host": h, "title": t or "", "description": d or ""} for h, t, d in db.execute(
        "SELECT host, max(title), max(description) FROM sites WHERE pays = 1 AND kind IS NULL AND (title != '' OR description != '') GROUP BY host")]
    batches = [rows[i:i + KIND_BATCH] for i in range(0, len(rows), KIND_BATCH)][:max(0, max_micros) // KIND_MICROS]
    done, by_rules = 0, 0
    with ThreadPoolExecutor(workers) as pool:
        for got, error in pool.map(lambda b: sellers.kinds(model, b), batches):
            db.executemany("UPDATE sites SET kind = ?, kind_by = ? WHERE host = ?", [(k, "rules" if error else "model", h) for h, k in got.items()])
            db.commit()
            done, by_rules = done + len(got), by_rules + (len(got) if error else 0)
            log(f"sorted {done:,}/{len(rows):,} sites" + (f", {by_rules:,} by keyword rules" if by_rules else ""))
    return {"step": "sort", "sorted": done, "by_rules": by_rules, "left": len(rows) - done, "cost_micros": len(batches) * KIND_MICROS}


def summary(db: sqlite3.Connection, out: Path | None = None) -> dict:
    """Sellers per month and platform, per year and kind, how far each pull got; every seller to `out` as CSV."""
    q = lambda sql: [list(r) for r in db.execute(sql)]  # noqa: E731
    result = {"step": "report",
              "pulls": q("SELECT platform, rows, done, cost_micros, error FROM pulls ORDER BY platform"),
              "sellers": q("SELECT platform, count(*), sum(pays) FROM sites GROUP BY platform ORDER BY platform"),
              "by_month": q("SELECT substr(first_seen, 1, 7) m, platform, count(*) FROM sites WHERE pays = 1 GROUP BY m, platform ORDER BY m"),
              "by_year_kind": q("SELECT substr(first_seen, 1, 4) y, coalesce(kind, CASE WHEN read_on IS NULL THEN 'unread' "
                                "WHEN title != '' OR description != '' THEN 'unsorted' ELSE 'no answer' END) k, count(*) "
                                "FROM sites WHERE pays = 1 GROUP BY y, k ORDER BY y, k")}
    if out:
        cols = ["platform", "host", "first_seen", "kind", "title", "description", "page", "link", "rank"]
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(db.execute(f"SELECT {', '.join(cols)} FROM sites WHERE pays = 1 ORDER BY first_seen DESC"))
        result["csv"] = str(out)
    return result


def text(result: dict) -> str:
    """A short Markdown view of what a step did, or of the whole history."""
    if result["step"] != "report":
        return "\n".join(f"{k}: {v}" for k, v in result.items() if k != "step")
    lines = ["| Platform | Sites read | Sellers (link takes money) | Pull done | Cost |", "|---|---|---|---|---|"]
    pulls = {p: (rows, done, cost) for p, rows, done, cost, _ in result["pulls"]}
    for p, n, paying in result["sellers"]:
        rows, done, cost = pulls.get(p, (0, 0, 0))
        lines.append(f"| {sellers.LABEL.get(p, p)} | {n:,} | {paying or 0:,} | {'yes' if done else 'no'} | ${cost / 1e6:.2f} |")
    years = sorted({y for y, _, _ in result["by_year_kind"]})
    kinds = sorted({k for _, k, _ in result["by_year_kind"]})
    cell = {(y, k): n for y, k, n in result["by_year_kind"]}
    if years:
        lines += ["", "| Year | " + " | ".join(kinds) + " |", "|---" * (len(kinds) + 1) + "|"]
        lines += [f"| {y} | " + " | ".join(f"{cell.get((y, k), 0):,}" for k in kinds) + " |" for y in years]
    return "\n".join(lines + (["", f"CSV: {result['csv']}"] if result.get("csv") else []))
