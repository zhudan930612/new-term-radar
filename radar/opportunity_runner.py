"""Idempotent daily orchestration; the legacy popularity report remains available."""
from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import aisa, breakout, demands, demand_sources, google_trends, opportunities, opportunity_validation, report, sources, spending, store


def today_local():
    return datetime.now(timezone(timedelta(hours=8))).date()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def available_snapshot(directory: Path, day) -> tuple[dict | None, str]:
    for path in sorted(directory.glob(day.strftime("%Y%m%d") + "*-daily*.json"), reverse=True):
        data = read(path)
        if "leads" in data and all(k in data for k in ("x", "hf", "breakout")):
            return data, str(path)
    return None, ""


def discover(cfg: dict, state: Path) -> dict:
    blocks = {}
    for name, fn in (("x", sources.x_new), ("hf", sources.hf_new), ("breakout", breakout.breakouts)):
        print("采集：" + name, flush=True)
        try:
            blocks[name] = fn(cfg, state)
        except Exception as exc:
            blocks[name] = {"error": str(exc)[:160]}
    lead, judged = report.leads(cfg, blocks)
    return {**blocks, "leads": lead, "judge": judged}


def pool_candidates(source: dict, pool: dict, day) -> dict:
    for row in source["leads"]:
        if row.get("named", 0) < .8 or row.get("known"):
            continue
        key = row.get("repo") or opportunities.normalize(row["term"])
        old = pool.get(key, {})
        pool[key] = {**old, "lead": row, "last_seen": day.isoformat(), "first_seen": old.get("first_seen", day.isoformat())}
    cutoff = (day - timedelta(days=14)).isoformat()
    return {k: v for k, v in pool.items() if v["last_seen"] >= cutoff}


def run_daily(cfg: dict, state: Path, output: Path, from_report: Path | None = None) -> int:
    day = today_local()
    c = cfg["opportunities"]
    root = state / "web-opportunities" / day.isoformat()
    root.mkdir(parents=True, exist_ok=True)
    final = root / "result.json"
    if (root / "trends-edit.lock").exists():
        raise aisa.AisaError("Daily result is being validated; do not overwrite it")

    def deliver(result):
        if result.get("workflow_version") == 2:
            opportunity_validation.prepare(result)
        if "google_trends" not in result:
            if final.exists():
                backup = root / "result-before-trends.json"
                if not backup.exists():
                    backup.write_bytes(final.read_bytes())
            google_trends.prepare(result, cfg)
            google_trends.write_result(final, result, output)
        else:
            # A cached daily render must not overwrite a concurrent browser import.
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(opportunities.render(result), encoding="utf-8")
            spending.save(output.with_suffix(".json"), result)
        print(opportunities.render(result))
        return 0 if result["status"] == "complete" else 2

    if final.exists():
        print("复用今天的 Web 机会结果；未新增付费调用。", flush=True)
        return deliver(read(final))
    if (state / "demand-ledger.audit.lock").exists():
        raise aisa.AisaError("Demand ledger audit is active; do not start another daily run")
    lock = root / "run.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise aisa.AisaError("This day's run is active or was interrupted; inspect run.lock before any retry") from None
    handle.write(datetime.now(timezone.utc).isoformat())
    handle.close()
    result = {"date": day.isoformat(), "timezone": "Asia/Taipei", "status": "incomplete", "errors": [],
              "budget_usd": c["budget_usd"], "opportunities": [], "rejected": [], "selected": [], "evidence": []}
    before, journal = None, None
    try:
        before = spending.account()
        result["billing"] = {"before": before}
        if before["balance_usd"] <= .01 or (c["budget_usd"] is not None and before["balance_usd"] < c["budget_usd"] + .01):
            raise aisa.AisaError("余额不足以预留本轮预计费用及 $0.01 余量，已停止付费采集")
        prices = spending.model_prices(c["model"])
        spending.save(root / "model-prices.json", prices)
        journal = spending.Journal(root / "api", c["budget_usd"], prices, before["balance_usd"])
        # Keep existing source state recoverable without recursively copying this run.
        backup = root / "state-before"
        backup.mkdir(exist_ok=True)
        for path in state.iterdir():
            if path.is_file():
                shutil.copy2(path, backup / path.name)
        with aisa.managed_requests(journal):
            reports_dir = cfg["_dir"] / "reports"
            source, source_path = (read(from_report), str(from_report)) if from_report else available_snapshot(reports_dir, day)
            if source is None:
                if cfg.get("demands", {}).get("enabled", True) and not cfg.get("demands", {}).get("technology_discovery", False):
                    source = {"leads": [], "x": {"skipped": True}, "hf": {}, "breakout": {},
                              "note": "任务需求为主入口；名称发现仅复用已有线索"}
                else:
                    local_cfg = copy.deepcopy(cfg)
                    local_cfg["judge"]["model"] = c["model"]
                    source = discover(local_cfg, state)
            elif from_report and not from_report.name.startswith(day.strftime("%Y%m%d")):
                raise ValueError("Only today's snapshot can seed a current daily run")
            result["source_report"] = source_path
            result["discovery"] = source
            spending.save(root / "discovery.json", source)
            for key in ("x", "hf", "breakout", "judge"):
                if source.get(key, {}).get("error"):
                    result["errors"].append(f"{key}: {source[key]['error']}")
            x = source.get("x", {})
            if not x.get("skipped") and x.get("answered") != x.get("accounts"):
                result["errors"].append("X 账号采集不完整")
            if any(r.get("named") is None for r in source["leads"]):
                result["errors"].append("部分候选未完成评分")
            pool_path = state / "web-candidate-pool.json"
            pool = pool_candidates(source, read(pool_path) if pool_path.exists() else {}, day)
            # Bring yesterday's unreviewed candidates into the backlog on first use.
            if not pool_path.exists():
                old, _ = available_snapshot(reports_dir, day - timedelta(days=1))
                if old:
                    for key, value in pool_candidates(old, {}, day - timedelta(days=1)).items():
                        pool.setdefault(key, value)
            cutoff = (day - timedelta(days=c["recheck_days"])).isoformat()
            eligible = [v["lead"] for v in pool.values() if v.get("reviewed", "") <= cutoff]
            selected = opportunities.candidates(eligible, c["candidates_per_day"])
            result["selected"] = selected
            print("核对需求：" + ", ".join(r["term"] for r in selected), flush=True)
            ledger_path = state / "demand-ledger.json"
            if cfg.get("demands", {}).get("enabled", True):
                followups = demands.followups(ledger_path, day, max(1, cfg.get("research", {}).get("pain_tasks_per_day", 1)))
                evidence, errors, collection = demand_sources.collect(selected, day, c["problem_queries_per_day"],
                    followups, cfg.get("demands", {}).get("free_sources", True))
                result["collection"] = collection
                if cfg.get("research", {}).get("enabled"):
                    from . import demand_research
                    extra, failed, coverage = demand_research.collect(cfg, day, followups, selected, state)
                    seen = {(row["url"], row["text"]) for row in evidence}
                    for row in extra:
                        identity = (row["url"], row["text"])
                        if identity not in seen:
                            evidence.append({**row, "id": f"e{len(evidence) + 1}"})
                            seen.add(identity)
                    errors += failed
                    result["research_collection"] = coverage
                    collection["successful_followups"] = list(dict.fromkeys(
                        collection["successful_followups"] + coverage.get("successful_followups", [])))
                demands.record_searches(ledger_path, collection["successful_followups"], day)
            else:
                evidence, errors = opportunities.collect(selected, day, c["problem_queries_per_day"])
            result["evidence"] = evidence
            result["errors"] += errors
            spending.save(root / "evidence.json", evidence)
            assessment = (demands.process(c["model"], evidence, ledger_path, day) if cfg.get("demands", {}).get("enabled", True)
                          else opportunities.analyze(c["model"], evidence))
            result.update(assessment)
            result["semantic_audit"] = {"status": "pending", "reason": "低价模型仅预处理；需独立核对全部来源及任务去向"}
            result["errors"] += assessment.get("analysis_errors", [])
            if not result["errors"]:
                result["status"] = "awaiting_audit" if assessment.get("workflow_version") == 2 else "complete"
                reviewed_terms = {s["term"] for s in selected} | {a for s in selected for a in s["aliases"]}
                for value in pool.values():
                    if value["lead"]["term"] in reviewed_terms:
                        value["reviewed"] = day.isoformat()
            spending.save(pool_path, pool)
            conn = store.connect(state)
            try:
                store.record(conn, report.to_record(cfg, source["leads"]), today=day)
            finally:
                conn.close()
    except Exception as exc:
        result["errors"].append(str(exc)[:240])
        result["status"] = "incomplete"
    finally:
        if journal:
            result["reserved_usd"] = journal.reserved
        if before:
            try:
                after = spending.account()
                result["billing"].update(after=after, charged_usd=round(before["balance_usd"] - after["balance_usd"], 6))
            except Exception:
                result["errors"].append("运行后余额核验失败；费用只保留请求记录，不能声称实际扣费已核实")
                result["status"] = "incomplete"
        # Even failed days are saved. Scheduler must not turn repeated wakeups into retries.
        spending.save(final, result)
        lock.unlink(missing_ok=True)
    return deliver(result)
