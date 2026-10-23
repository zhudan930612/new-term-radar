"""Read saved request/response ledgers; no network or model calls.

Estimate the recorded work removable by extract_only. This is not a new model
replay and does not claim improved extraction quality or supplier settlements.
"""
import argparse
import json
from pathlib import Path


def study(state: Path, since: str, until: str) -> dict:
    days = []
    for path in sorted((state / "web-opportunities").glob("*/api/requests.json")):
        day = path.parent.parent.name
        if not since <= day <= until:
            continue
        row = {"date": day, "model_attempts": 0, "extraction_attempts": 0,
               "model_estimated_usd": 0.0, "extraction_estimated_usd": 0.0, "unknown_attempts": 0}
        for entry in json.loads(path.read_text(encoding="utf-8")):
            if entry.get("label") != "qwen3.7-flash":
                continue
            for a in entry.get("attempts", [entry]):
                row["model_attempts"] += 1
                if not a.get("charge_known"):
                    row["unknown_attempts"] += 1
                cost = a.get("actual_usd", 0) if a.get("charge_known") else 0
                row["model_estimated_usd"] += cost
                request = path.parent / a.get("request_file", "missing.json")
                if not request.is_file():
                    continue
                body = json.loads(request.read_text(encoding="utf-8")).get("body", {})
                system = next((m.get("content", "") for m in body.get("messages", []) if m.get("role") == "system"), "")
                if "这是初次需求提取" in system:
                    row["extraction_attempts"] += 1
                    row["extraction_estimated_usd"] += cost
        days.append(row)
    return {"method": "saved request ledger counterfactual; no new model output or quality claim",
            "paid_calls": 0, "days": days}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--since", required=True)
    p.add_argument("--until", required=True)
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    result = study(args.state, args.since, args.until)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text)
