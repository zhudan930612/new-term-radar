"""Free Google Trends browser handoff, only after the evidence shortlist.

This module never performs network requests. The scheduled agent visits the bounded
plan through its browser tools, preserves visible evidence, then imports observations.
Paid trend estimates must never be substituted for Google Trends website evidence.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from . import spending


STATUSES = {"observed", "no_data", "blocked", "unavailable"}
LABELS = {"pending": "待浏览器验证", "running": "已开始验证，尚未导入结果",
          "waiting_validation": "等待原文上下文与搜索意图核验",
          "complete": "已完成网页核验", "partial": "部分数据未能核验",
          "skipped": "未查询", "disabled": "未启用"}


def prepare(result: dict, cfg: dict) -> dict:
    """Add a stable plan once, without revisiting completed/failed observations."""
    if "google_trends" in result:
        return result
    c = cfg.get("google_trends", {})
    stage = {"provider": "Google Trends website", "mode": "free_browser", "geo": c.get("geo", "US"),
             "api_cost_usd": 0, "status": "skipped", "items": [], "omitted": []}
    result["google_trends"] = stage
    if not c.get("enabled", False):
        stage.update(status="disabled", reason="配置未启用")
    elif result.get("status") != "complete":
        stage["reason"] = "需求采集或筛选尚未完成，不发起趋势查询"
    elif not any(o.get("review_status") != "withdrawn" for o in result.get("opportunities", [])):
        stage["reason"] = "今天没有入选机会，不查询 Google Trends"
    elif result.get("workflow_version") == 2 and result.get("opportunity_validation", {}).get("status") not in {"complete", "partial"}:
        stage.update(status="waiting_validation", reason="先核对原文上下文与搜索意图，再查询对应短语")
    else:
        end = date.fromisoformat(result["date"]) - timedelta(days=1)
        for i, opportunity in enumerate(result["opportunities"][:3]):
            if opportunity.get("review_status") == "withdrawn":
                continue
            if result.get("workflow_version") == 2 and not any(r.get("opportunity_index") == i and r.get("trends_ready")
                    for r in result.get("opportunity_validation", {}).get("items", [])):
                stage["omitted"].append({"opportunity_index": i, "reason": "原文独立性或搜索意图未确认，保留待核实，不查询 Trends"})
                continue
            keywords = opportunity.get("keywords", [])
            keywords = keywords if isinstance(keywords, list) else []
            keyword = next((k.strip() for k in keywords if isinstance(k, str) and
                            1 <= len(k.strip()) <= 80 and not re.search(r"[\n\r,<>]", k)), "")
            if not keyword:
                stage["omitted"].append({"opportunity_index": i, "reason": "缺少可用搜索短语，未猜测关键词"})
                continue
            for window, days in (("90d", 90), ("12m", 365)):
                start = end - timedelta(days=days - 1)
                query = {"hl": "en-US", "geo": stage["geo"], "date": f"{start} {end}", "q": keyword}
                stage["items"].append({"id": f"t{i+1}-{window}", "opportunity_index": i,
                                       "keyword": keyword, "geo": stage["geo"], "window": window,
                                       "start": start.isoformat(), "end": end.isoformat(), "status": "pending",
                                       "url": "https://trends.google.com/trends/explore?" + urlencode(query)})
        stage["status"] = "pending" if stage["items"] else "partial"
    return result


def write_result(path: Path, result: dict, output: Path | None = None) -> None:
    from .opportunities import render
    spending.save(path, result)
    # Always keep a canonical report beside the authoritative result.
    path.with_suffix(".md").write_text(render(result), encoding="utf-8")
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render(result), encoding="utf-8")
        spending.save(output.with_suffix(".json"), result)


def trend_direction(series: list, item: dict, source: str) -> str:
    """Use actual visible/CSV points, never infer a numeric curve from prose."""
    if not series:
        return "unknown"
    if not isinstance(series, list) or len(series) < 8:
        raise ValueError("A direction requires at least 8 time-series points")
    previous, values = "", []
    for point in series:
        day, value = point.get("date", ""), point.get("value")
        date.fromisoformat(day)
        if not item["start"] <= day <= item["end"] or day <= previous:
            raise ValueError("Trend dates must be ordered and within the planned window")
        if type(value) not in (int, float) or not 0 <= value <= 100:
            raise ValueError("Trend values must be normalized 0..100")
        # Store the verbatim CSV row or visible tooltip text alongside each point.
        raw = point.get("source_quote", "")
        if not isinstance(raw, str) or len(raw) < 3 or raw not in source or str(value) not in raw or day not in raw:
            raise ValueError("Trend point needs a matching source quote")
        previous = day
        values.append(value)
    span = (date.fromisoformat(item["end"]) - date.fromisoformat(item["start"])).days
    if ((date.fromisoformat(series[0]["date"]) - date.fromisoformat(item["start"])).days > span / 4 or
        (date.fromisoformat(item["end"]) - date.fromisoformat(series[-1]["date"])).days > span / 4):
        raise ValueError("Series must cover both ends of the planned window")
    if sum(v > 0 for v in values) < 8:
        return "insufficient"
    n = max(2, len(values) // 4)
    first, last = sum(values[:n]) / n, sum(values[-n:]) / n
    if first == 0:
        return "insufficient"
    ratio = last / first
    return "rising" if ratio > 1.15 else "falling" if ratio < .85 else "stable"


def validate_observations(stage: dict, payload: dict, root: Path, report_date: str) -> list[dict]:
    if payload.get("date") != report_date:
        raise ValueError("Observations must match the report date")
    rows = payload.get("items")
    if not isinstance(rows, list) or len(rows) != len(stage["items"]):
        raise ValueError("Record every planned page, including failures")
    planned = {item["id"]: item for item in stage["items"]}
    seen, checked = set(), []
    for row in rows:
        key = row.get("id")
        if key not in planned or key in seen:
            raise ValueError("Unknown or repeated Trends observation")
        seen.add(key)
        item = planned[key]
        status = row.get("status")
        if status not in STATUSES or row.get("url") != item["url"]:
            raise ValueError("Observation must use the planned Google Trends URL")
        stamp = datetime.fromisoformat(row.get("observed_at", ""))
        if stamp.utcoffset() is None or stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat() != report_date:
            raise ValueError("Observation timestamp must include timezone and match this day")
        note = row.get("note", "")
        if not isinstance(note, str) or not 1 <= len(note) <= 500:
            raise ValueError("A short factual observation or failure reason is required")
        source, artifact = "", row.get("evidence_file", "")
        if artifact:
            path = (root / artifact).resolve()
            if not path.is_relative_to((root / "trends-evidence").resolve()) or path.suffix not in {".txt", ".csv", ".md"}:
                raise ValueError("Evidence must be a local text/CSV file under trends-evidence")
            if path.stat().st_size > 2_000_000:
                raise ValueError("Evidence file too large")
            source = path.read_text(encoding="utf-8-sig")
        quote = row.get("source_quote", "")
        if status in {"observed", "no_data"} and (not isinstance(quote, str) or len(quote) < 8 or quote not in source):
            raise ValueError("Successful or no-data observations require saved visible evidence")
        series, related = row.get("series", []), row.get("related_queries", [])
        if not isinstance(series, list) or not isinstance(related, list) or len(related) > 10:
            raise ValueError("Malformed trend series or related queries")
        if status != "observed" and (series or related):
            raise ValueError("Failed/no-data pages cannot contain claimed trend measurements")
        direction = trend_direction(series, item, source)
        for query in related:
            if (not isinstance(query, dict) or query.get("kind") not in {"top", "rising"} or
                not isinstance(query.get("query"), str) or not query["query"].strip() or query["query"] not in source):
                raise ValueError("Related queries must occur in saved page evidence")
        related_status = row.get("related_status", "unreadable")
        if (related_status not in {"observed", "no_data", "unreadable"} or (related and related_status != "observed") or
            (related_status == "observed" and not related)):
            raise ValueError("Invalid related-query status")
        checked.append({**item, "status": status, "observed_at": row["observed_at"], "note": note,
                        "evidence_file": artifact, "source_quote": quote, "direction": direction,
                        "series": series, "related_queries": related, "related_status": related_status})
    return checked


def command(action: str, path: Path, cfg: dict, observations: Path | None = None,
            output: Path | None = None) -> int:
    """Local-only state transition; exclusive claim prevents repeated browser work."""
    root = path.parent
    if (root / "run.lock").exists():
        raise ValueError("Daily run still active; do not change its result")
    lock = root / "trends-edit.lock"
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        prepare(result, cfg)
        stage = result["google_trends"]
        if action == "claim":
            if stage["status"] != "pending":
                print(json.dumps({"claimed": False, "status": stage["status"]}, ensure_ascii=False))
                return 0
            stage["status"] = "running"
            stage["started_at"] = datetime.now(timezone.utc).isoformat()
        elif action == "import":
            if stage["status"] != "running" or observations is None:
                raise ValueError("Claim the pending plan first; completed/failed runs cannot be retried")
            payload = json.loads(observations.read_text(encoding="utf-8-sig"))
            checked = validate_observations(stage, payload, root, result["date"])
            stage["items"] = checked
            stage["status"] = "complete" if (not stage["omitted"] and all(
                row["status"] == "no_data" or (row["status"] == "observed" and
                row["direction"] != "unknown" and row["related_status"] != "unreadable")
                for row in checked)) else "partial"
            # The original accepted demands and market_verified flags are untouched.
        elif action != "prepare":
            raise ValueError("Unknown Trends action")
        backup = root / "result-before-trends.json"
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        write_result(path, result, output)
        print(json.dumps({"claimed": action == "claim", **stage}, ensure_ascii=False, indent=2))
        return 0
    finally:
        lock.unlink(missing_ok=True)


def render_stage(result: dict) -> list[str]:
    stage = result.get("google_trends")
    if not stage:
        return []
    lines = ["## Google Trends 验证", "", f"- 状态：{LABELS.get(stage['status'], stage['status'])}；市场：{stage['geo'] or '全球'}。",
             "- 免费网页验证；第三方趋势 API 费用 $0。浏览器执行仍使用当前 Codex 任务额度。"]
    if stage.get("reason"):
        lines += ["- " + stage["reason"]]
    directions = {"unknown": "曲线未能可靠提取，方向待验证", "insufficient": "可用非零数据不足，不能判断需求大小",
                  "rising": "末段均值高于首段", "falling": "末段均值低于首段", "stable": "首末段均值接近"}
    statuses = {"pending": "待验证", "observed": "已读取", "no_data": "页面显示数据不足",
                "blocked": "访问受阻", "unavailable": "无法读取"}
    for row in stage["items"]:
        title = result["opportunities"][row["opportunity_index"]]["title"]
        lines += ["", f"### {title} · {row['keyword']} · {'近 90 天' if row['window'] == '90d' else '近 12 个月（365 天）'}", "",
                  f"- [Google Trends 原页]({row['url']})；{row['start']} 至 {row['end']}；{statuses[row['status']]}。"]
        if row.get("note"):
            lines += ["- 网页观察：" + " ".join(row["note"].split())]
        if row["status"] == "observed":
            lines += ["- 走势：" + directions[row["direction"]] + "。"]
            if row["direction"] in {"rising", "falling", "stable"}:
                lines += ["- 算法口径：已读取数据首尾各四分之一均值比较，±15% 内为接近；不代表持续增长或季节性结论。"]
            related = row.get("related_queries", [])
            lines += ["- 相关搜索：" + ("；".join(f"{q['query']}（{q['kind']}）" for q in related) if related else
                      "没有已核对的相关词，可能是无数据或未能读取。")]
        if row.get("evidence_file"):
            lines += ["- 证据文件（相对当天状态目录）：`" + row["evidence_file"] + "`"]
    for row in stage["omitted"]:
        lines += ["- " + result["opportunities"][row["opportunity_index"]]["title"] + "：" + row["reason"]]
    lines += ["", "趋势指数是相对热度，不是搜索次数。数据不足或访问失败不等于没有需求；高热度不证明低竞争或付费意愿。", ""]
    return lines
