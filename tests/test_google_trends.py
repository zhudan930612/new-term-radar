import copy
import json
from datetime import date, timedelta

import pytest

from radar import google_trends as gt, opportunities, opportunity_runner, spending


@pytest.fixture
def shortlisted(cfg):
    cfg["google_trends"] = {"enabled": True, "geo": "US"}
    return {"date": "2026-10-15", "status": "complete", "opportunities": [
        {"title": "发票拆分", "keywords": ["split invoice pdf", "invoice splitter"],
         "evidence": [], "market_verified": False}], "errors": []}


def save_plan(tmp_path, cfg, shortlisted):
    path = tmp_path / "result.json"
    spending.save(path, shortlisted)
    gt.command("prepare", path, cfg)
    return path, json.loads(path.read_text(encoding="utf-8"))


def observations(result, root, status="observed"):
    rows = []
    (root / "trends-evidence").mkdir(exist_ok=True)
    for item in result["google_trends"]["items"]:
        start, end = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
        days = (end - start).days
        series = []
        for i in range(8):
            day, value = (start + timedelta(days=days * i // 7)).isoformat(), 10 + i * 10
            series.append({"date": day, "value": value, "source_quote": f"{day},{value}"})
        source = "Interest over time\nsplit pdf pages\n" + "\n".join(p["source_quote"] for p in series)
        artifact = f"trends-evidence/{item['id']}.txt"
        (root / artifact).write_text(source, encoding="utf-8")
        rows.append({"id": item["id"], "url": item["url"], "status": status,
                     "observed_at": "2026-10-15T10:01:00+08:00", "note": "读取网页证据",
                     "evidence_file": artifact, "source_quote": "Interest over time",
                     "series": series if status == "observed" else [],
                     "related_status": "observed" if status == "observed" else "unreadable",
                     "related_queries": [{"query": "split pdf pages", "kind": "rising"}] if status == "observed" else []})
    return {"date": result["date"], "items": rows}


def test_no_shortlist_or_incomplete_means_no_queries(cfg, shortlisted):
    empty = {**shortlisted, "opportunities": []}
    assert gt.prepare(empty, cfg)["google_trends"]["items"] == []
    failed = {**shortlisted, "status": "incomplete"}
    assert gt.prepare(failed, cfg)["google_trends"]["items"] == []


def test_only_accepted_opportunities_and_bounded_windows(cfg, shortlisted):
    shortlisted["selected"] = [{"term": "must not query"}]
    shortlisted["rejected"] = [{"topic": "also must not query"}]
    stage = gt.prepare(shortlisted, cfg)["google_trends"]
    assert len(stage["items"]) == 2
    assert {x["keyword"] for x in stage["items"]} == {"split invoice pdf"}
    assert all("geo=US" in x["url"] and x["end"] == "2026-10-14" for x in stage["items"])
    many = copy.deepcopy(shortlisted)
    many.pop("google_trends")
    many["opportunities"] *= 5
    assert len(gt.prepare(many, cfg)["google_trends"]["items"]) == 6


def test_missing_keywords_not_guessed_and_cached_plan_unchanged(cfg, shortlisted):
    shortlisted["opportunities"][0]["keywords"] = "wrong schema"
    stage = gt.prepare(shortlisted, cfg)["google_trends"]
    assert stage["status"] == "partial" and not stage["items"]
    cfg["google_trends"]["geo"] = "TW"
    assert gt.prepare(shortlisted, cfg)["google_trends"]["geo"] == "US"


def test_claim_import_retains_original_demands_and_prevents_repeat(tmp_path, cfg, shortlisted, capsys):
    path, result = save_plan(tmp_path, cfg, shortlisted)
    gt.command("claim", path, cfg)
    gt.command("claim", path, cfg)
    assert '"claimed": false' in capsys.readouterr().out
    data = observations(result, tmp_path)
    obs = tmp_path / "observations.json"
    spending.save(obs, data)
    output = tmp_path / "reports" / "daily.md"
    gt.command("import", path, cfg, obs, output)
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["google_trends"]["status"] == "complete"
    assert all(x["direction"] == "rising" for x in final["google_trends"]["items"])
    assert final["opportunities"] == shortlisted["opportunities"]
    assert output.with_suffix(".json").exists() and path.with_suffix(".md").exists()
    assert "Google Trends 原页" in output.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Claim"):
        gt.command("import", path, cfg, obs)


@pytest.mark.parametrize("bad", ["url", "date", "quote", "series", "related", "path", "duplicate", "window"])
def test_reject_unverifiable_or_mismatched_observations(tmp_path, cfg, shortlisted, bad):
    result = gt.prepare(shortlisted, cfg)
    data = observations(result, tmp_path)
    item = data["items"][0]
    if bad == "url": item["url"] = "https://api.dataforseo.com/estimated"
    if bad == "date": data["date"] = "2026-10-14"
    if bad == "quote": item["source_quote"] = "invented observation"
    if bad == "series": item["series"][0]["value"] = 101
    if bad == "related": item["related_queries"][0]["query"] = "invented related term"
    if bad == "path": item["evidence_file"] = "../outside.txt"
    if bad == "duplicate": data["items"][1] = item
    if bad == "window": item["series"][0]["date"] = "2000-01-01"
    with pytest.raises(ValueError):
        gt.validate_observations(result["google_trends"], data, tmp_path, result["date"])


def test_blocked_preserves_opportunity_and_never_means_no_demand(tmp_path, cfg, shortlisted):
    path, result = save_plan(tmp_path, cfg, shortlisted)
    gt.command("claim", path, cfg)
    data = observations(result, tmp_path, "blocked")
    obs = tmp_path / "observations.json"
    spending.save(obs, data)
    gt.command("import", path, cfg, obs)
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["google_trends"]["status"] == "partial"
    text = opportunities.render(final)
    assert "今天新增 1 个" in text and "访问受阻" in text and "今天没有" not in text


def test_visible_chart_without_series_does_not_fabricate_direction(tmp_path, cfg, shortlisted):
    result = gt.prepare(shortlisted, cfg)
    data = observations(result, tmp_path)
    for row in data["items"]:
        row["series"] = []
    checked = gt.validate_observations(result["google_trends"], data, tmp_path, result["date"])
    assert all(row["direction"] == "unknown" for row in checked)


def test_existing_daily_result_can_get_plan_without_any_paid_calls(tmp_path, cfg, shortlisted, monkeypatch):
    monkeypatch.setattr(opportunity_runner, "today_local", lambda: date(2026, 10, 15))
    monkeypatch.setattr(spending, "account", lambda: pytest.fail("no network on cached daily"))
    state = tmp_path / "state"
    path = state / "web-opportunities" / "2026-10-15" / "result.json"
    spending.save(path, shortlisted)
    output = tmp_path / "reports" / "daily.md"
    assert opportunity_runner.run_daily(cfg, state, output) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["google_trends"]["status"] == "pending"
    assert (path.parent / "result-before-trends.json").exists()


def test_active_daily_lock_prevents_supplement(tmp_path, cfg, shortlisted):
    path, _ = save_plan(tmp_path, cfg, shortlisted)
    (tmp_path / "run.lock").write_text("active")
    with pytest.raises(ValueError, match="active"):
        gt.command("claim", path, cfg)
