"""Free account/quote checks and a persistent, fail-closed daily request journal.

Reservations limit estimated spend, not the provider's final metered charge.
No retry of a failed or interrupted paid request is automatic.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import urllib.request
from pathlib import Path

from . import aisa


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def _control(requests: list[tuple[str, dict]]) -> list[dict]:
    # Optional MCP extra; already installed by the local setup. Never log headers.
    try:
        try:
            import httpx2 as httpx
        except ImportError:
            import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise aisa.AisaError("Web opportunities requires the mcp extra for free balance/quote checks") from exc
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {aisa.key()}"}, timeout=45) as client:
        async with streamable_http_client("https://mcp.aisa.one/mcp", http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                out = []
                for name, arguments in requests:
                    result = await session.call_tool(name, arguments)
                    raw = result.model_dump(by_alias=True)
                    if raw.get("isError", raw.get("is_error", False)):
                        raise aisa.AisaError("Free AIsa control request failed")
                    value = raw.get("structuredContent", raw.get("structured_content"))
                    if not value:
                        value = json.loads(next(c["text"] for c in raw["content"] if c["type"] == "text"))
                    out.append(value)
                return out


def control(requests: list[tuple[str, dict]]) -> list[dict]:
    try:
        return asyncio.run(_control(requests))
    except Exception as exc:
        # SDK exception groups may contain headers; expose only the exception type.
        raise aisa.AisaError(f"Free AIsa control unavailable ({type(exc).__name__})") from exc


def account() -> dict:
    result = control([("use", {"operation_id": "account", "arguments": {}, "max_price_usd": 0})])[0]
    if not result.get("successful"):
        raise aisa.AisaError("Account check unsuccessful")
    data = result["data"]
    balance = data["balance"]["available_usd"]
    if not isinstance(balance, (int, float)) or not math.isfinite(balance):
        raise aisa.AisaError("Account balance unavailable")
    return {"balance_usd": balance, "as_of": data.get("as_of"), "usage": data.get("usage", {}).get("today", {})}


def model_prices(model: str) -> dict:
    if model != "qwen3.7-flash":
        raise aisa.AisaError("Opportunity budget supports only the verified qwen3.7-flash model")
    with urllib.request.urlopen(f"https://aisa.one/api/pricing/models/{model}", timeout=30) as response:
        data = json.load(response)["data"]["model"]
    prices = data["price"]["customer"]
    if data.get("trial_unavailable") or data.get("pricing_status") != "priced":
        raise aisa.AisaError("Configured model is no longer eligible for the current balance")
    if not all(isinstance(prices.get(k), (int, float)) and 0 < prices[k] < 100 for k in ("input", "output")):
        raise aisa.AisaError("Unrecognized model pricing")
    return prices


def quote(calls: list[dict]) -> float:
    allowed = {"get_twitter_user_last_tweets", "get_twitter_tweet_advanced_search"}
    if any(c["tool"] not in allowed for c in calls):
        raise aisa.AisaError("This daily workflow does not authorize that paid data operation")
    results = control([("get_details", {"operation_id": c["tool"], "arguments": c["arguments"], "with_quote": True}) for c in calls])
    total = 0.0
    for result in results:
        # Single-operation get_details returns the contract directly.
        if "results" in result:
            result = result["results"][0]
        p = result.get("price", {})
        amount = p.get("amount_usd")
        if p.get("model") != "quoted" or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
            raise aisa.AisaError("A complete authenticated quote is required")
        total += amount
    return total


class Journal:
    def __init__(self, directory: Path, budget: float, prices: dict, balance: float):
        self.directory, self.budget, self.prices = directory, budget, prices
        self.limit = min(budget, max(0, balance - 0.01))
        self.path = directory / "requests.json"
        self.entries = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    @property
    def reserved(self) -> float:
        return sum(max(e["reserved_usd"], e.get("actual_usd", 0)) for e in self.entries)

    def request(self, url: str, body: dict, timeout: int, send):
        digest = hashlib.sha256(json.dumps([url, body], sort_keys=True).encode()).hexdigest()
        old = next((e for e in self.entries if e["id"] == digest), None)
        cached = self.directory / (digest + ".json")
        if old:
            if old["status"] == "ok" and cached.exists():
                return json.loads(cached.read_text(encoding="utf-8"))
            raise aisa.AisaError("Prior request failed or was interrupted; automatic paid retry disabled")
        if any(e["status"] != "ok" for e in self.entries):
            raise aisa.AisaError("A prior paid request failed; further paid calls are stopped")
        if url == aisa.CHAT:
            if body.get("model") != "qwen3.7-flash" or body.get("enable_thinking") is not False:
                raise aisa.AisaError("Unbudgeted model or reasoning mode")
            # UTF-8 bytes are a conservative input-token reservation, with overhead.
            tokens = len(json.dumps(body["messages"], ensure_ascii=False).encode()) + 512
            reserve = (tokens * max(self.prices["input"], self.prices.get("cache_write", 0)) +
                       body["max_tokens"] * self.prices["output"]) / 1e6 * 1.25
            label = body["model"]
        elif url == aisa.ROUTER:
            reserve = quote(body["calls"]) * 1.25
            label = ",".join(c["tool"] for c in body["calls"])
        else:
            raise aisa.AisaError("Unknown paid endpoint")
        if self.reserved + reserve > self.limit:
            raise aisa.AisaError("Daily estimated spending limit reached; no request sent")
        entry = {"id": digest, "label": label, "reserved_usd": reserve, "status": "pending"}
        self.entries.append(entry)
        save(self.path, self.entries)  # Reserve BEFORE sending, including timeouts.
        save(self.directory / (digest + ".request.json"), {"url": url, "body": body})
        print("API: " + label, flush=True)
        try:
            result = send(url, body, timeout)
            save(cached, result)
            if "calls" in body:
                replies = result.get("results", [])
                if len(replies) != len(body["calls"]) or any(not r.get("successful") for r in replies):
                    raise aisa.AisaError("Data batch partially failed")
                if any((r.get("data") or {}).get("status") == "error" for r in replies):
                    raise aisa.AisaError("Upstream data provider returned an error")
                entry["actual_usd"] = sum(int(r.get("customer_cost_micros_usd") or 0) for r in replies) / 1e6
            else:
                if result.get("choices", [{}])[0].get("finish_reason") != "stop":
                    raise aisa.AisaError("Model response incomplete")
                entry["usage"] = result.get("usage", {})
                usage = entry["usage"]
                entry["actual_usd"] = (usage.get("prompt_tokens", 0) * max(self.prices["input"], self.prices.get("cache_write", 0)) +
                                       usage.get("completion_tokens", 0) * self.prices["output"]) / 1e6
            entry["status"] = "ok"
            return result
        except Exception as exc:
            entry["status"] = "failed"
            entry["error_type"] = type(exc).__name__
            raise
        finally:
            save(self.path, self.entries)
