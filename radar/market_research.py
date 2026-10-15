"""Paid search evidence and optional revenue context, after source/intent validation.

These observations cannot promote demands or prove market demand. A persisted
started stage and the shared daily request journal prevent automatic paid retries.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import aisa, competition, goanyapi, google_trends, revenue, spending


TERMINAL = {"running", "complete", "partial", "incomplete"}


def ready_items(result: dict) -> list[dict]:
    if (result.get("status") != "complete" or result.get("workflow_version") != 2 or
            result.get("semantic_audit", {}).get("status") != "complete"):
        return []
    validation = result.get("opportunity_validation", {})
    if validation.get("status") not in {"complete", "partial"}:
        return []
    opportunities = result.get("opportunities", [])
    ready, seen = [], set()
    for row in validation.get("items", []):
        index = row.get("opportunity_index")
        if (type(index) is not int or not 0 <= index < len(opportunities) or index in seen or
                row.get("trends_ready") is not True or row.get("status") != "observed"):
            continue
        opportunity = opportunities[index]
        keyword = row.get("keyword", "")
        if (opportunity.get("review_status") == "withdrawn" or
                not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 80 or
                re.search(r"[\n\r,<>]", keyword) or keyword.strip() not in opportunity.get("keywords", [])):
            continue
        ready.append({"opportunity_index": index, "keyword": keyword.strip(), "market_verified": False})
        seen.add(index)
    return ready[:3]


class EvidenceSession:
    """Keep successful provider evidence, including cached replies, with its query."""
    def __init__(self, journal: spending.Journal, root: Path):
        self.journal, self.root = journal, root
        self.replies: list[dict] = []
        self.errors: list[str] = []

    def request(self, url: str, body: dict, timeout: int, send):
        try:
            answer = self.journal.request(url, body, timeout, send)
        except Exception as exc:
            self.errors.append(type(exc).__name__)
            raise
        calls = {c["call_id"]: c for c in body.get("calls", [])}
        for reply in answer.get("results", []):
            call = calls.get(reply.get("call_id"), {})
            if reply.get("successful") and call.get("tool") in {competition.SERP_TOOL, competition.KD_TOOL}:
                number = len(self.replies) + 1
                filename = f"market-research-evidence/provider-{number}.json"
                record = {"tool": call["tool"], "arguments": call["arguments"], "data": reply.get("data") or {},
                          "cost_micros": reply.get("customer_cost_micros_usd") or 0, "evidence_file": filename}
                spending.save(self.root / filename, record)
                self.replies.append(record)
        return answer


def _serp_evidence(session: EvidenceSession, keyword: str) -> list[dict]:
    return [r for r in session.replies if r["tool"] == competition.SERP_TOOL and
            any(q.get("keyword") == keyword for q in r["arguments"].get("body", []))]


def _organic_rows(records: list[dict]) -> list[dict]:
    return [item for record in records for task in record["data"].get("tasks", [])
            for result in task.get("result") or [] for item in result.get("items") or []
            if item.get("type") == "organic"]


def _domain(url: str) -> str:
    parsed = urlparse(url if "://" in url else "https://" + url)
    return (parsed.hostname or "").lower().removeprefix("www.")


def _revenue_context(cfg: dict, state: Path, result: dict, stage: dict) -> None:
    context = {"status": "unavailable", "products": [], "market_verified": False,
               "meaning": "产品营收仅为背景；域名重合不证明同一任务、需求规模或该机会能够盈利。",
               "billing": "TrustMRR 的订阅或调用费用独立于 AIsa，未包含在 AIsa 费用中。"}
    stage["revenue"] = context
    if not revenue.key():
        context["reason"] = "missing_TRUSTMRR_API_KEY"
    else:
        try:
            response = revenue.products(cfg, state / "market-research", date.fromisoformat(result["date"]))
            if response.get("skipped") or response.get("error"):
                context["reason"] = "provider_unavailable"
            else:
                context.update(status="observed", products=response.get("products", []),
                               read=response.get("read"), first_run=response.get("first_run"))
        except Exception as exc:
            context.update(status="failed", error_type=type(exc).__name__)
    for item in stage["items"]:
        domains = {_domain(r.get("url") or r.get("domain", "")) for r in item.get("serp_results", [])}
        matches = [i for i, product in enumerate(context["products"])
                   if _domain(product.get("website", "")) and _domain(product["website"]) in domains]
        item["revenue_context"] = {"status": "candidate_domain_overlap" if matches else "unknown",
                                   "product_indexes": matches, "same_task_verified": False,
                                   "market_verified": False}


def command(result_path: Path, cfg: dict, state: Path, output: Path | None = None) -> int:
    root = result_path.parent
    if (root / "run.lock").exists() or (state / "demand-ledger.audit.lock").exists():
        raise ValueError("Daily collection or demand audit is active")
    lock = root / "trends-edit.lock"
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        if (root / "run.lock").exists() or (state / "demand-ledger.audit.lock").exists():
            raise ValueError("Daily collection or demand audit is active")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        previous = result.get("market_research", {})
        if previous.get("status") in TERMINAL:
            print(json.dumps({"reused": True, "status": previous["status"]}))
            return 2 if previous["status"] in {"running", "incomplete"} else 0
        items = ready_items(result)
        stage = {"status": "skipped", "items": items, "market_verified": False,
                 "reason": "只有原文上下文、独立性及搜索意图通过的入选机会才研究竞争和营收背景。"}
        result["market_research"] = stage
        if not items:
            google_trends.write_result(result_path, result, output)
            print(json.dumps({"status": "skipped", "items": 0}))
            return 0
        backup = root / "result-before-market-research.json"
        if not backup.exists():
            backup.write_bytes(result_path.read_bytes())
        stage.update(status="running", started_at=datetime.now(timezone.utc).isoformat(), errors=[])
        # Persist BEFORE any control or paid request; a crash requires inspection.
        google_trends.write_result(result_path, result, output)
        journal, session, before = None, None, None
        has_goany = bool(goanyapi.key())
        rows = [{"phrase": item["keyword"], "decision": "watch"} for item in items]
        try:
            before = spending.account()
            prices = spending.model_prices(cfg["opportunities"]["model"])
            journal = spending.Journal(root / "api", cfg["opportunities"].get("budget_usd"), prices, before["balance_usd"])
            initial_actual = sum(e.get("actual_usd", 0) for e in journal.entries)
            session = EvidenceSession(journal, root)
            research_cfg = copy.deepcopy(cfg)
            research_cfg["competition"].update(max_rows=3, location_code=2840)
            with aisa.managed_requests(session):
                total_micros, errors = competition.assess(rows, research_cfg, state / "market-research")
            if errors:
                stage["errors"].append({"error_type": "ProviderRequestError", "count": len(errors),
                                        "request_error_types": sorted(set(session.errors))})
            aisa_returned = sum(r["cost_micros"] for r in session.replies)
            stage["external_billing"] = {"goanyapi_reported_usd": max(0, total_micros - aisa_returned) / 1e6,
                                         "excluded_from_aisa": True,
                                         "note": "供应商返回的费用不包含未返回账单的失败请求；TrustMRR 费用另计。"}
        except Exception as exc:
            stage["errors"].append({"error_type": type(exc).__name__})
        finally:
            if journal is not None:
                stage["aisa_journal"] = {"actual_usd": round(sum(e.get("actual_usd", 0) for e in journal.entries) - initial_actual, 8),
                                          "daily_reserved_usd": journal.reserved,
                                          "request_journal": "api/requests.json"}
            if before is not None:
                stage["aisa_account_before"] = before
                try:
                    after = spending.account()
                    stage["aisa_account_after"] = after
                    stage["aisa_account_balance_change_usd"] = round(before["balance_usd"] - after["balance_usd"], 8)
                    stage["account_change_note"] = "账户余额差可能包括并发请求；不能全部归因本阶段。"
                except Exception as exc:
                    stage["errors"].append({"error_type": type(exc).__name__, "operation": "account_after"})
        for item, row in zip(items, rows):
            records = _serp_evidence(session, item["keyword"]) if session else []
            organic = _organic_rows(records)
            detail = copy.deepcopy(row.get("competition") or {})
            # The upstream heuristic calls missing KD/DR crowded; unknown is safer.
            if not organic or (detail.get("weak") is None and detail.get("kd") is None):
                detail["verdict"] = "unknown"
            missing = []
            if not has_goany:
                missing += ["domain_rating", "advertisers", "intitle"]
            else:
                missing += [name for name, field in (("domain_rating", "weak"), ("advertisers", "paid"), ("intitle", "built"))
                            if detail.get(field) is None]
            if detail.get("kd") is None:
                missing.append("keyword_difficulty")
            if 0 < detail.get("rated_results", 0) < detail.get("total_results", 0):
                missing.append("domain_rating_coverage")
            if not organic:
                missing.append("organic_results")
            item.update(status="partial" if missing else "complete", competition=detail, missing=missing,
                        serp_results=organic, evidence_files=[r["evidence_file"] for r in records],
                        kd_evidence_files=[r["evidence_file"] for r in session.replies if r["tool"] == competition.KD_TOOL] if session else [],
                        competition_note="首页结构和 KD/DR 只是竞争线索，缺失值不能视作无竞争或容易获客。",
                        optional_key_status="configured" if has_goany else "missing_GOANYAPI_API_KEY")
        _revenue_context(cfg, state, result, stage)
        available = any(i["serp_results"] for i in items) or stage["revenue"]["status"] == "observed"
        stage["status"] = ("incomplete" if not available else "partial" if stage["errors"] or
                           stage["revenue"]["status"] != "observed" or any(i["missing"] for i in items) else "complete")
        stage["finished_at"] = datetime.now(timezone.utc).isoformat()
        google_trends.write_result(result_path, result, output)
        print(json.dumps({"status": stage["status"], "items": len(items), "market_verified": False}))
        return 2 if stage["status"] == "incomplete" else 0
    finally:
        lock.unlink(missing_ok=True)
