"""Replay saved real sources with a separate capped journal; never collects data.

Run from the repository using its installed Python environment. Credentials must
already be in the environment. Output is resumable and must not be deleted to retry.
"""
import argparse
import json
from datetime import date
from pathlib import Path

from radar import aisa, demands, opportunities, spending


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--budget", type=float, default=.10)
    parser.add_argument("--ledger-file", default="demand-ledger.json")
    args = parser.parse_args()
    if not 0 < args.budget <= .10:
        raise ValueError("Validation budget must be in (0, 0.10]")
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    before = spending.account()
    prices = spending.model_prices("qwen3.7-flash")
    journal = spending.Journal(args.out / "api", args.budget, prices, before["balance_usd"])
    spending.save(args.out / "prices.json", prices)
    with aisa.managed_requests(journal):
        result = demands.process("qwen3.7-flash", evidence, args.out / args.ledger_file, args.date)
    after = spending.account()
    result.update(date=args.date.isoformat(), evidence=evidence, selected=[],
                  status="complete" if not result["analysis_errors"] else "incomplete",
                  errors=result["analysis_errors"], budget_usd=args.budget,
                  billing={"before": before, "after": after, "charged_usd": round(before["balance_usd"] - after["balance_usd"], 6)},
                  reserved_usd=journal.reserved, mode="historical real-source replay; no data collection")
    by_id = {r["original_id"]: r for r in result["dispositions"]}
    comparison = [{**g, "actual": by_id.get(g["id"], {}).get("category", "missing"),
                   "match": by_id.get(g["id"], {}).get("category") == g["expected"]} for g in gold]
    metrics = {"sources": len(evidence), "gold_labels": len(gold),
               "category_agreement": sum(c["match"] for c in comparison),
               "gold_needs": sum(c["expected"] == "need" for c in comparison),
               "gold_needs_retained": sum(c["expected"] == c["actual"] == "need" for c in comparison),
               "promotion_as_need": sum(c["expected"] == "promotion" and c["actual"] == "need" for c in comparison),
               "tasks": len(result["tasks"]), "shortlist": len(result["opportunities"]),
               "analysis_errors": result["analysis_errors"], "reserved_usd": journal.reserved,
               "charged_usd": result["billing"]["charged_usd"], "balance_usd": after["balance_usd"],
               "caveat": "独立审查标签是此样本的验收依据，不是市场真值或总体召回率"}
    spending.save(args.out / "result.json", result)
    spending.save(args.out / "comparison.json", {"metrics": metrics, "rows": comparison})
    (args.out / "result.md").write_text(opportunities.render(result), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == "__main__":
    main()
