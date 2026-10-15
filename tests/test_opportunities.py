import copy
import json
from datetime import date
from pathlib import Path

import pytest

from radar import aisa, opportunities as op, opportunity_runner as runner, spending


def lead(name, repo="", url=""):
    return {"term": name, "named": .9, "repo": repo, "known": "", "kinds": ["breakout"],
            "posts": [{"url": url}] if url else []}


def test_aliases_need_shared_source_and_prefix():
    rows = [lead("videojev system", url="https://x.com/a/status/1"),
            lead("videojev system one", url="https://x.com/a/status/1"),
            lead("other app", url="https://x.com/a/status/1"),
            lead("videojev", url="https://x.com/b/status/2")]
    chosen = op.candidates(rows)
    assert len(chosen) == 3
    assert next(r for r in chosen if r["term"] == "videojev system one")["aliases"] == ["videojev system"]


def test_same_owner_does_not_fill_daily_pool():
    rows = [lead(f"app {i}", f"owner/app{i}") for i in range(4)] + [lead("new", "different/new")]
    assert len(op.candidates(rows)) == 3


@pytest.fixture
def evidence():
    return [{"id": f"e{i}", "kind": "post", "text": f"I spend hours manually splitting invoices for client {i} every week.",
             "url": f"https://x.com/user{i}/status/{i}", "author": f"x:user{i}", "topic": "invoices"} for i in (1, 2)]


@pytest.fixture
def proposal(evidence):
    item = {k: "具体内容" for k in ("title", "user", "job", "pain", "web_advantage", "input", "output", "mvp",
                                  "gap", "constraints", "validation", "success_condition", "stop_condition")}
    item.update(web_fit="browser", evidence=[{"id": e["id"], "quote": e["text"], "type": "user_pain"} for e in evidence])
    return {"opportunities": [item], "rejected": []}


@pytest.mark.parametrize("bad", ["fabrication", "same_author", "official", "unsuitable", "missing_field"])
def test_weak_or_unverifiable_demand_is_not_promoted(proposal, evidence, bad):
    item = proposal["opportunities"][0]
    if bad == "fabrication": item["evidence"][0]["quote"] = "A fabricated sentence that does not occur in the source"
    if bad == "same_author": evidence[1]["author"] = evidence[0]["author"]
    if bad == "official": evidence[1]["kind"] = "official"
    if bad == "unsuitable": item["web_fit"] = "unsuitable"
    if bad == "missing_field": item.pop("stop_condition")
    assert not op.parse(json.dumps(proposal), evidence)["opportunities"]


def test_real_quotes_are_retained_and_duplicates_removed(proposal, evidence):
    proposal["opportunities"].append(copy.deepcopy(proposal["opportunities"][0]))
    result = op.parse(json.dumps(proposal), evidence)
    assert len(result["opportunities"]) == 1
    assert result["opportunities"][0]["market_verified"] is False
    assert result["opportunities"][0]["evidence"][0]["url"] == evidence[0]["url"]


def test_second_pass_can_reject_same_job_assumption(monkeypatch, proposal, evidence):
    replies = iter([json.dumps(proposal), json.dumps({"reviews": [{"i": 0, "firsthand": True,
                        "same_job": False, "web_solution": True, "bounded": True, "reason": "不是同一需求"}]})])
    monkeypatch.setattr(aisa, "chat", lambda *a, **k: next(replies))
    result = op.analyze("qwen3.7-flash", evidence)
    assert result["opportunities"] == [] and "同一具体任务" in result["rejected"][-1]["reason"]


def test_rejections_do_not_present_invented_market_facts():
    raw = {"opportunities": [], "rejected": [{"topic": "some app", "reason": "已证明市场成立、竞争饱和"}]}
    result = op.parse(json.dumps(raw), [])
    assert "竞争饱和" not in result["rejected"][0]["reason"]
    assert "尚未验证" in result["rejected"][0]["reason"]


def test_failure_is_not_reported_as_no_opportunity():
    result = {"date": "2026-10-15", "status": "incomplete", "opportunities": [], "errors": ["network failed"]}
    assert "暂时无法判断" in op.render(result)
    assert "本轮样本中没有新增达到深入验证门槛" not in op.render(result)
    result["status"] = "complete"
    result["errors"] = []
    assert "本轮样本中没有新增达到深入验证门槛" in op.render(result)


def payload():
    return {"model": "qwen3.7-flash", "enable_thinking": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "small input"}]}


def test_budget_blocks_before_send_and_cache_prevents_rebilling(tmp_path):
    body = payload()
    calls = []
    send = lambda *args: calls.append(args) or {"choices": [{"finish_reason": "stop"}], "usage": {}}
    j = spending.Journal(tmp_path, .001, {"input": 1, "output": 1}, 1)
    j.request(aisa.CHAT, body, 30, send)
    j.request(aisa.CHAT, body, 30, send)
    assert len(calls) == 1
    with pytest.raises(aisa.AisaError, match="limit"):
        j.request(aisa.CHAT, {**body, "max_tokens": 9000}, 30, send)
    assert len(calls) == 1


def test_failed_request_keeps_reservation_and_never_retries(tmp_path):
    calls = []
    def broken(*args):
        calls.append(args)
        raise TimeoutError("unknown outcome")
    j = spending.Journal(tmp_path, .2, {"input": .2, "output": .7}, 1)
    with pytest.raises(TimeoutError): j.request(aisa.CHAT, payload(), 30, broken)
    j = spending.Journal(tmp_path, .2, {"input": .2, "output": .7}, 1)
    with pytest.raises(aisa.AisaError, match="retry disabled"): j.request(aisa.CHAT, payload(), 30, broken)
    assert j.reserved > 0 and len(calls) == 1


def test_partial_batch_stops_later_paid_work(tmp_path, monkeypatch):
    monkeypatch.setattr(spending, "quote", lambda calls: .003)
    j = spending.Journal(tmp_path, .2, {"input": .2, "output": .7}, 1)
    with pytest.raises(aisa.AisaError, match="partially failed"):
        j.request(aisa.ROUTER, {"calls": [{"tool": op.SEARCH}]}, 30, lambda *a: {"results": []})
    with pytest.raises(aisa.AisaError, match="further paid"):
        j.request(aisa.CHAT, payload(), 30, lambda *a: pytest.fail("must not send"))


def test_insufficient_balance_and_same_day_reuse_do_not_spend(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(runner, "today_local", lambda: date(2026, 10, 15))
    monkeypatch.setattr(spending, "account", lambda: {"balance_usd": .10})
    monkeypatch.setattr(spending, "model_prices", lambda *a: pytest.fail("no model/data request"))
    state, output = tmp_path / "state", tmp_path / "reports" / "20261015-daily.md"
    assert runner.run_daily(cfg, state, output) == 2
    assert "余额不足" in output.read_text(encoding="utf-8")
    monkeypatch.setattr(spending, "account", lambda: pytest.fail("reuse must not request"))
    assert runner.run_daily(cfg, state, output) == 2


def test_active_lock_blocks_concurrent_run(tmp_path, cfg):
    root = tmp_path / "state" / "web-opportunities" / runner.today_local().isoformat()
    root.mkdir(parents=True)
    (root / "run.lock").write_text("active")
    with pytest.raises(aisa.AisaError, match="active or was interrupted"):
        runner.run_daily(cfg, tmp_path / "state", tmp_path / "out.md")


def test_complete_empty_workflow_and_backlog(tmp_path, cfg, monkeypatch):
    cfg["demands"]["enabled"] = False
    day = date(2026, 10, 15)
    monkeypatch.setattr(runner, "today_local", lambda: day)
    monkeypatch.setattr(spending, "account", lambda: {"balance_usd": 1})
    monkeypatch.setattr(spending, "model_prices", lambda *a: {"input": .2, "output": .7})
    source = {"leads": [], "x": {"accounts": 0, "answered": 0}, "hf": {}, "breakout": {}}
    monkeypatch.setattr(runner, "discover", lambda *a: source)
    monkeypatch.setattr(op, "collect", lambda *a: ([], []))
    state, out = tmp_path / "state", tmp_path / "out.md"
    assert runner.run_daily(cfg, state, out) == 0
    assert "本轮样本中没有新增达到深入验证门槛" in out.read_text(encoding="utf-8")
    assert json.loads(out.with_suffix('.json').read_text())["billing"]["charged_usd"] == 0


def test_qwen_budget_mode_does_not_change_other_models(monkeypatch):
    requests = []
    monkeypatch.setattr(aisa, "_post", lambda u, b, t: requests.append(b) or {"choices": [{"message": {"content": "[]"}}]})
    aisa.chat("qwen3.7-flash", "s", "u")
    aisa.chat("another-model", "s", "u")
    assert requests[0]["enable_thinking"] is False and "enable_thinking" not in requests[1]


def test_daily_cannot_silently_bypass_web_budget(cfg, tmp_path):
    from argparse import Namespace
    from radar import __main__ as cli
    cfg["opportunities"]["enabled"] = True
    with pytest.raises(ValueError, match="cannot bypass"):
        cli.cmd_daily(Namespace(skip={"x"}), cfg, tmp_path)


def test_saved_discovery_is_reused_without_recollecting(tmp_path, cfg, monkeypatch):
    cfg["demands"]["enabled"] = False
    day = date(2026, 10, 15)
    monkeypatch.setattr(runner, "today_local", lambda: day)
    monkeypatch.setattr(spending, "account", lambda: {"balance_usd": 1})
    monkeypatch.setattr(spending, "model_prices", lambda *a: {"input": .2, "output": .7})
    monkeypatch.setattr(runner, "discover", lambda *a: pytest.fail("must reuse saved collection"))
    monkeypatch.setattr(op, "collect", lambda *a: ([], []))
    reports = tmp_path / "reports"
    reports.mkdir()
    source = {"leads": [], "x": {"accounts": 0, "answered": 0}, "hf": {}, "breakout": {}}
    spending.save(reports / "20261015-090000-daily.json", source)
    out = tmp_path / "separate-output" / "report.md"
    assert runner.run_daily(cfg, tmp_path / "state", out) == 0
    assert json.loads(out.with_suffix('.json').read_text())["source_report"].endswith("20261015-090000-daily.json")
