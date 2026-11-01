"""Offline regression checks for the 0.5.0 / demand-workflow integration."""
import json
from datetime import date

import pytest

from radar import __main__ as cli, competition, opportunity_runner


def blocked(*args, **kwargs):
    pytest.fail("Legacy paid research must not run on this path")


def write_config(tmp_path, cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({k: v for k, v in cfg.items() if k != "_dir"}))
    return str(path)


@pytest.mark.parametrize("web", [True, False])
@pytest.mark.parametrize("forced", [True, False])
def test_existing_free_skip_set_blocks_new_paid_signals(cfg, tmp_path, monkeypatch, web, forced):
    class Monday(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 19)

    monkeypatch.setattr(cli, "date", Monday)
    cfg["opportunities"]["enabled"] = web
    # Undo the suite's default disabling of paid steps, so it cannot mask this regression.
    cfg["asks"]["weekday"] = cfg["revenue"]["weekday"] = cfg["sellers"]["weekday"] = 0
    cfg["tutorials"]["max_leads"] = 3
    cfg["builders"]["weekday"] = cfg["money"]["weekday"] = 0
    cfg["competition"]["max_rows"] = 8
    for module, name in [(cli.asks, "asks"), (cli.revenue, "products"), (cli.pains, "tutorial_asks"),
                         (cli.new_sites, "new_sites"), (cli.review, "review"), (cli.usecases, "run"),
                         (competition, "assess"), (cli.sellers, "sellers"), (cli.sellers, "builders"),
                         (cli.feeds, "feeds"), (cli.money, "money"), (cli.sidelights, "evidence"), (cli.sidelights, "tails")]:
        monkeypatch.setattr(module, name, blocked)
    monkeypatch.setattr(cli.sources, "hf_new", lambda *a: {"models": 1, "spaces": 1, "terms": [
        {"term": "demo video workflow", "note": "synthetic free source", "examples": [], "posts": [
            {"text": "Here's how to make a video step by step", "likes": 1200, "url": "https://example.com/tutorial"}]}]})
    def score(rows, model):
        for row in rows:
            row["named"] = .9
        return {"judged": len(rows)}

    monkeypatch.setattr(cli.report.judge, "score", score)
    rendered = []

    def render(cfg, leads, blocks, gsc, review, summary, extra):
        assert leads[0]["one_click"] == "tutorial"
        assert all(blocks[k] == {"skipped": True} for k in ("ask", "revenue", "newsite", "seller", "builder", "feed", "money"))
        assert extra["sidelights"] == extra["tails"] == {"skipped": True}
        assert extra["tutorials"] == extra["usecases"] == {"skipped": True}
        rendered.append(True)
        return "Offline free report"

    monkeypatch.setattr(cli.report, "daily", render)
    argv = ["daily", "--config", write_config(tmp_path, cfg), "--state", str(tmp_path / "state"),
            "--skip", "x,newsites,review,judge,usecases,breakout,sitemaps,gsc"]
    if forced:
        argv += ["--asks", "--revenue", "--sellers", "--feeds"]
    assert cli.main(argv) == 0
    assert rendered == [True]


@pytest.mark.parametrize("command", ["daily", "opportunities"])
def test_web_routes_keep_the_budgeted_runner(cfg, tmp_path, monkeypatch, command):
    cfg["opportunities"]["enabled"] = command == "daily"
    for module, name in [(cli.asks, "asks"), (cli.revenue, "products"), (cli.pains, "tutorial_asks"),
                         (competition, "assess"), (cli.sellers, "sellers"), (cli.sellers, "builders"),
                         (cli.feeds, "feeds"), (cli.money, "money"), (cli.sidelights, "evidence"), (cli.sidelights, "tails")]:
        monkeypatch.setattr(module, name, blocked)
    called = []

    def run(config, state, output, snapshot):
        assert config["opportunities"]["enabled"]
        assert config["opportunities"]["budget_usd"] == .2
        assert config["demands"]["enabled"]
        assert all(k in config for k in ("asks", "pains", "revenue", "competition", "google_trends"))
        called.append(True)
        return 2  # awaiting_audit must propagate to the caller

    monkeypatch.setattr(opportunity_runner, "run_daily", run)
    assert cli.main([command, "--config", write_config(tmp_path, cfg), "--state", str(tmp_path / "state")]) == 2
    assert called == [True]


def test_pains_command_survives_cli_merge(cfg, tmp_path, monkeypatch, capsys):
    called = []

    def run(config, keyword, extensions):
        called.append((keyword, extensions))
        return {"keyword": keyword, "gaps": []}

    monkeypatch.setattr(cli.pains, "run", run)
    assert cli.main(["pains", "invoice splitting", "--extension", "example-extension", "--json",
                     "--config", write_config(tmp_path, cfg), "--state", str(tmp_path / "state")]) == 0
    assert called == [("invoice splitting", ["example-extension"])]
    assert json.loads(capsys.readouterr().out)["gaps"] == []


def test_legacy_daily_keeps_explicit_research_routes(cfg, tmp_path, monkeypatch):
    cfg["opportunities"]["enabled"] = False
    cfg["tutorials"]["max_leads"] = 3
    called = []

    def response(name):
        def run(*args, **kwargs):
            called.append(name)
            return {"rows": [], "terms": [], "leads": []}
        return run

    for module, name, label in [(cli.asks, "asks", "asks"), (cli.revenue, "products", "revenue"),
                                (cli.pains, "tutorial_asks", "tutorials"), (cli.usecases, "run", "usecases")]:
        monkeypatch.setattr(module, name, response(label))
    monkeypatch.setattr(cli.report, "daily", lambda *a: "Offline legacy report")
    assert cli.main(["daily", "--asks", "--revenue", "--config", write_config(tmp_path, cfg),
                     "--state", str(tmp_path / "state"),
                     "--skip", "x,hf,breakout,sitemaps,newsites,gsc,review,judge"]) == 0
    assert set(called) == {"asks", "revenue", "tutorials", "usecases"}

@pytest.mark.parametrize("command", ["opportunities", "research-opportunities", "trends"])
def test_explicit_web_command_does_not_fall_back_to_sample_config(tmp_path, monkeypatch, command):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(opportunity_runner, "run_daily", blocked)
    with pytest.raises(SystemExit, match="config.json not found"):
        cli.main([command])


def test_serp_default_preserves_old_request_and_explicit_device_remains_available(monkeypatch):
    from radar import aisa
    calls = []
    def capture(batch):
        calls.append(batch[0]["arguments"]["body"][0])
        return {"serp": {"tasks": [{"status_code": 20000, "result": [{"items": []}]}]}}, 0
    monkeypatch.setattr(aisa, "call", capture)
    competition.page_one("split invoices", 2840)
    assert calls[-1] == {"keyword": "split invoices", "location_code": 2840, "language_code": "en", "depth": 10}
    competition.page_one("split invoices", 2840, "de", "mobile")
    assert calls[-1]["language_code"] == "de" and calls[-1]["device"] == "mobile"


def test_rest_passthrough_is_rejected_before_managed_request(monkeypatch):
    from radar import aisa
    monkeypatch.setattr(aisa, "_post", blocked)
    with aisa.managed_requests(object()), pytest.raises(aisa.AisaError, match="not supported"):
        aisa.rest("v3/domain_analytics/whois/overview/live", {"limit": 1})

@pytest.mark.parametrize("status,code", [("awaiting_audit", 2), ("incomplete", 2), ("complete", 0)])
def test_mcp_reports_saved_workflow_state_without_retry(tmp_path, monkeypatch, status, code):
    from pathlib import Path
    pytest.importorskip("mcp")
    from radar import mcp_server
    calls = []
    def run(args):
        calls.append(args)
        out = Path(args[args.index("--out") + 1])
        out.write_text("待审报告", encoding="utf-8")
        out.with_suffix(".json").write_text(json.dumps({"status": status, "semantic_audit": {"status": "pending"},
            "errors": ["source unavailable"]}), encoding="utf-8")
        return code
    monkeypatch.setattr(mcp_server.cli, "main", run)
    got = mcp_server._run("daily", [])
    assert len(calls) == 1 and got["exit_code"] == code and got["status"] == status
    assert got["semantic_audit"] == {"status": "pending"} and got["errors"] == ["source unavailable"]
    assert got["report_markdown"] == "待审报告"


def test_mcp_missing_config_becomes_tool_error(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from radar import mcp_server
    monkeypatch.setenv("RADAR_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(mcp_server.ToolError, match="not found"):
        mcp_server._keyword_table(["synthetic"], {})
    with pytest.raises(mcp_server.ToolError, match="not found"):
        mcp_server._run("daily", [])
