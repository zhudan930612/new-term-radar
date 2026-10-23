"""Free account/quote checks and a persistent, fail-closed daily request journal.

Reservations limit estimated spend, not the provider's final metered charge.
Only attributable, explicitly zero-charge transient router failures may be retried.
Unknown outcomes and account failures stop all further paid work.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
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


ALLOWED_DATA_TOOLS = {"get_twitter_user_last_tweets", "get_twitter_tweet_advanced_search", "get_reddit_search",
                   "post_dataforseo_serp_youtube_video_comments_live", "post_dataforseo_serp_google_organic_live",
                   "post_dataforseo_labs_google_keyword_overview_live"}
# Live schema verified 2026-10-16: these GET operations are read_only and
# idempotent. Allowed POST data operations may create provider tasks; no retry.
RETRYABLE_READ_ONLY_TOOLS = {"get_twitter_user_last_tweets", "get_twitter_tweet_advanced_search", "get_reddit_search"}


def quote(calls: list[dict]) -> float:
    calls = _calls({"calls": calls})
    responses = control([("batch_quote", {"calls": calls})])
    invalid = "A complete attributable USD quote is required"
    if not isinstance(responses, list) or len(responses) != 1 or not isinstance(responses[0], dict):
        raise aisa.AisaError(invalid)
    result = responses[0]
    if result.get("error") is not None or any(type(result.get(k)) is not int or result[k] != v
            for k, v in (("total_count", len(calls)), ("success_count", len(calls)), ("error_count", 0))):
        raise aisa.AisaError(invalid)
    replies = result.get("results")
    expected = {c["call_id"]: c["tool"] for c in calls}
    if (not isinstance(replies, list) or len(replies) != len(calls) or
            any(not isinstance(r, dict) or not isinstance(r.get("call_id"), str) for r in replies) or
            len({r["call_id"] for r in replies}) != len(replies) or
            {r["call_id"] for r in replies} != set(expected)):
        raise aisa.AisaError(invalid)
    total_micros = 0
    for reply in replies:
        p = reply.get("data")
        if (reply.get("successful") is not True or reply.get("error") is not None or
                reply.get("tool") != expected[reply["call_id"]] or not isinstance(p, dict) or
                p.get("object") != "cost_estimate" or p.get("currency") != "USD"):
            raise aisa.AisaError(invalid)
        estimate, maximum, kind = p.get("estimated_cost_micros_usd"), p.get("max_cost_micros_usd"), p.get("estimate_kind")
        if (not _number(estimate) or not isinstance(kind, str) or kind not in {"exact", "upper_bound", "estimate"} or
                p.get("may_exceed_estimate") is not (kind == "estimate")):
            raise aisa.AisaError(invalid)
        if kind == "estimate":
            # This contract has no guaranteed ceiling. Journal retains its
            # conservative reservation; an estimate is never described as a cap.
            if "max_cost_micros_usd" in p:
                raise aisa.AisaError(invalid)
            amount = estimate
        else:
            if not _number(maximum) or maximum < estimate or (kind == "exact" and maximum != estimate):
                raise aisa.AisaError(invalid)
            amount = maximum
        total_micros += amount
    if not _number(total_micros):
        raise aisa.AisaError(invalid)
    return total_micros / 1e6


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _digest(url: str, body: dict) -> str:
    return hashlib.sha256(json.dumps([url, body], sort_keys=True).encode()).hexdigest()


def _calls(body: dict) -> list[dict]:
    calls = body.get("calls") if isinstance(body, dict) else None
    if (not isinstance(calls, list) or not 1 <= len(calls) <= aisa.MAX_CALLS or
            any(not isinstance(c, dict) or not isinstance(c.get("call_id"), str) or not c["call_id"] or
                c.get("tool") not in ALLOWED_DATA_TOOLS or not isinstance(c.get("arguments"), dict) for c in calls) or
            len({c["call_id"] for c in calls}) != len(calls)):
        raise aisa.AisaError("Malformed or unauthorized paid data request")
    return calls


def _account_error(error) -> bool:
    if not isinstance(error, dict):
        return False
    return (type(error.get("status")) is int and error["status"] in {401, 402, 403} or
            error.get("type") in {"authentication", "authorization", "auth", "unauthorized", "forbidden",
                                  "balance", "billing", "insufficient_balance", "insufficient_credits",
                                  "payment_required", "account", "quota_exceeded"})


def _outcome(kind: str, actual: float | None = None) -> dict:
    return {"outcome": kind, "status": "ok" if kind == "ok" else "failed", "charge_known": actual is not None and kind != "unknown",
            **({"actual_usd": actual} if actual is not None else {}),
            "global_stop": kind in {"unknown", "account_failure"}, "retryable": kind == "retryable_zero"}


def youtube_no_results(data: dict, video_id: str) -> bool:
    """A narrowly identified, completed empty YouTube comment sample (40102).

    DataForSEO documents 40102 as no matching results. This is not a general
    exception for that code, another endpoint, missing fields or null payloads.
    """
    if not isinstance(data, dict) or not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return False
    if any(type(data.get(k)) is not int or data[k] != v for k, v in
           (("status_code", 20000), ("tasks_count", 1), ("tasks_error", 0))):
        return False
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        return False
    task = tasks[0]
    if (type(task.get("status_code")) is not int or task["status_code"] != 40102 or
            type(task.get("result_count")) is not int or task["result_count"] != 1 or
            task.get("path") != ["v3", "serp", "youtube", "video_comments", "live", "advanced"]):
        return False
    query = task.get("data")
    if not isinstance(query, dict) or any(query.get(k) != v for k, v in
            (("api", "serp"), ("function", "live"), ("se", "youtube"), ("se_type", "video_comments"), ("video_id", video_id))):
        return False
    results = task.get("result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return False
    result = results[0]
    return (result.get("video_id") == video_id and result.get("se_domain") == "youtube.com" and
            all(type(result.get(k)) is int and result[k] == 0 for k in ("comments_count", "items_count")) and
            "items" in result and result["items"] is None)


def _youtube_empty_call(call: dict, data: dict) -> bool:
    arguments = call.get("arguments", {}).get("body")
    return (call.get("tool") == "post_dataforseo_serp_youtube_video_comments_live" and
            isinstance(arguments, list) and len(arguments) == 1 and isinstance(arguments[0], dict) and
            youtube_no_results(data, arguments[0].get("video_id")))


def _classify(url: str, body: dict, response, prices: dict) -> dict:
    if not isinstance(response, dict):
        return _outcome("unknown")
    if _account_error(response.get("error")):
        return _outcome("account_failure")
    if url == aisa.CHAT:
        usage = response.get("usage")
        if not isinstance(usage, dict) or any(not _number(usage.get(k)) for k in ("prompt_tokens", "completion_tokens")):
            return _outcome("unknown")
        actual = (usage["prompt_tokens"] * max(prices["input"], prices.get("cache_write", 0)) +
                  usage["completion_tokens"] * prices["output"]) / 1e6
        choices = response.get("choices")
        complete = isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) and choices[0].get("finish_reason") == "stop"
        # actual_usd remains a compatibility accounting amount for budget/cache
        # consumers. Token pricing is NOT a supplier settlement receipt.
        return {**_outcome("ok" if complete else "source_failure", actual), "usage": usage,
                "cost_basis": "token_estimate", "estimated_usd": actual,
                "pricing_snapshot": dict(prices), "settled_usd": None}
    if url != aisa.ROUTER:
        return _outcome("unknown")
    try:
        calls = _calls(body)
    except aisa.AisaError:
        return _outcome("unknown")
    replies = response.get("results")
    if (not isinstance(replies, list) or len(replies) != len(calls) or
            any(not isinstance(r, dict) or not isinstance(r.get("call_id"), str) for r in replies) or
            len({r["call_id"] for r in replies}) != len(replies) or
            {r["call_id"] for r in replies} != {c["call_id"] for c in calls}):
        return _outcome("unknown")
    known = all(_number(r.get("customer_cost_micros_usd")) for r in replies)
    costs = [r["customer_cost_micros_usd"] for r in replies if _number(r.get("customer_cost_micros_usd"))]
    subtotal = sum(costs) if costs else None
    actual = subtotal / 1e6 if subtotal is not None and _number(subtotal) else None
    known = known and actual is not None
    if any(_account_error(r.get("error")) for r in replies):
        return {**_outcome("account_failure", actual), "charge_known": known}
    if not known or any(type(r.get("successful")) is not bool for r in replies):
        return _outcome("unknown", actual)
    counts = {"total_count": len(replies), "success_count": sum(r["successful"] is True for r in replies),
              "error_count": sum(r["successful"] is False for r in replies)}
    if response.get("error") or any(key in response and (type(response[key]) is not int or response[key] != expected)
                                    for key, expected in counts.items()):
        return _outcome("unknown", actual)
    failed = [r for r in replies if r["successful"] is False]
    if failed:
        transient = all(c["tool"] in RETRYABLE_READ_ONLY_TOOLS for c in calls) and len(failed) == len(replies) and all(
            r["customer_cost_micros_usd"] == 0 and isinstance(r.get("error"), dict) and
            r["error"].get("retryable") is True and r["error"].get("type") == "upstream" and
            type(r["error"].get("status")) is int and
            (r["error"]["status"] == 429 or 500 <= r["error"]["status"] <= 599) for r in failed)
        if transient:
            return _outcome("retryable_zero", 0)
        return _outcome("source_failure", actual)
    provider_failed = False
    by_id = {call["call_id"]: call for call in calls}
    for reply in replies:
        data = reply.get("data")
        if not isinstance(data, dict):
            return _outcome("unknown", actual)
        if _youtube_empty_call(by_id[reply["call_id"]], data):
            continue
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks):
            return _outcome("unknown", actual)
        provider_failed |= (data.get("status") == "error" or data.get("success") is False or
                            bool(data.get("tasks_error", 0)) or any(t.get("status_code", 20000) != 20000 for t in tasks))
    return _outcome("source_failure" if provider_failed else "ok", actual)


def _legacy_success(entry: dict, response: dict) -> bool:
    """Retain already settled successes from before request snapshots existed.

    This exception cannot authorize any new send or retry. Historical model
    charges are kept as recorded rather than repriced at today's token rates.
    """
    if entry.get("status") != "ok" or not _number(entry.get("actual_usd")) or not isinstance(response, dict) or response.get("error"):
        return False
    if entry.get("label") == "qwen3.7-flash":
        choices, usage = response.get("choices"), response.get("usage")
        return (isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) and
                choices[0].get("finish_reason") == "stop" and isinstance(usage, dict) and
                all(_number(usage.get(k)) for k in ("prompt_tokens", "completion_tokens")))
    replies = response.get("results")
    if (not isinstance(replies, list) or not replies or
            any(not isinstance(r, dict) or not isinstance(r.get("call_id"), str) or not r["call_id"] or
                r.get("successful") is not True or not _number(r.get("customer_cost_micros_usd")) or
                not isinstance(r.get("data"), dict) for r in replies) or
            len({r["call_id"] for r in replies}) != len(replies)):
        return False
    # Synthetic arguments are only used to validate the old successful envelope;
    # they are never persisted as a real request or sent to the provider.
    tools = entry.get("label", "").split(",")
    if len(tools) != len(replies) or any(tool not in ALLOWED_DATA_TOOLS for tool in tools):
        return False
    request = {"calls": [{"call_id": r["call_id"], "tool": tool, "arguments": {}}
                         for r, tool in zip(replies, tools)]}
    verdict = _classify(aisa.ROUTER, request, response, {})
    return verdict["status"] == "ok" and math.isclose(verdict["actual_usd"], entry["actual_usd"], rel_tol=1e-9, abs_tol=1e-12)


def _immutable(path: Path, raw: bytes) -> None:
    if path.exists():
        if path.read_bytes() != raw:
            raise aisa.AisaError("Attempt evidence already exists with different contents")
        return
    with path.open("xb") as handle:
        handle.write(raw)


def _bytes(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


class JournalError(aisa.AisaError):
    def __init__(self, message: str, failure_kind: str, request_sent: bool = False):
        super().__init__(message)
        self.failure_kind, self.request_sent = failure_kind, request_sent


def billing_summary(entries: list[dict]) -> dict:
    """Read-only attribution, including old journals; never recompute old charges."""
    result = {"provider_reported_usd": 0.0, "model_estimated_usd": 0.0,
              "legacy_unattributed_usd": 0.0, "unknown_reserved_usd": 0.0,
              "unknown_attempts": 0, "attempts": 0,
              "note": "Token estimates and provider response costs are not account balance changes; legacy actual_usd is retained for compatibility."}
    for entry in entries:
        model = entry.get("label") == "qwen3.7-flash"
        for attempt in entry.get("attempts", [entry]):
            result["attempts"] += 1
            amount = attempt.get("actual_usd")
            known = attempt.get("charge_known", entry.get("status") == "ok")
            if not known or attempt.get("status") == "pending" or not _number(amount):
                result["unknown_attempts"] += 1
                result["unknown_reserved_usd"] += max(attempt.get("reserved_usd", 0), amount or 0)
            elif model or attempt.get("cost_basis") == "token_estimate":
                result["model_estimated_usd"] += amount
            elif entry.get("label") in ALLOWED_DATA_TOOLS:
                result["provider_reported_usd"] += amount
            else:
                result["legacy_unattributed_usd"] += amount
    return result


class Journal:
    MAX_ATTEMPTS = 3

    def __init__(self, directory: Path, budget: float | None, prices: dict, balance: float):
        self.directory, self.budget, self.prices = directory, budget, prices
        self.path = directory / "requests.json"
        self.cache_interpretations = []
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._reload()
            self._upgrade_legacy()
            # Only confirmed prior charges are added back to current balance.
            # Released zero-charge reservations must not inflate spend capacity.
            paid = sum(e.get("actual_usd", 0) for e in self.entries)
            available = paid + max(0, balance - 0.01)
            self.limit = min(budget, available) if budget is not None else available

    @contextmanager
    def _locked(self):
        lock = self.directory / "requests.lock"
        try:
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError:
            raise JournalError("Request journal is active or interrupted; inspect requests.lock", "journal_locked") from None
        try:
            with handle:
                handle.write("exclusive request journal lock\n")
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _reload(self):
        self.entries = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(self.entries, list) or len({e["id"] for e in self.entries}) != len(self.entries):
            raise aisa.AisaError("Malformed request journal")

    def _sync(self, entry):
        attempts = entry["attempts"]
        latest = attempts[-1]
        entry.update(status=latest["status"], outcome=latest.get("outcome", "unknown"),
                     retryable=latest.get("retryable", False),
                     global_stop=any(a.get("global_stop", a["status"] == "pending") for a in attempts),
                     reserved_usd=sum(a["reserved_usd"] for a in attempts),
                     actual_usd=sum(a.get("actual_usd", 0) for a in attempts))
        if latest.get("usage") is not None:
            entry["usage"] = latest["usage"]
        if latest.get("error_type"):
            entry["error_type"] = latest["error_type"]

    def _upgrade_legacy(self):
        changed = False
        for entry in self.entries:
            if "attempts" in entry:
                continue
            digest = entry["id"]
            # Snapshot the original entry as well as both raw files. Never reset
            # the first attempt even if an old process stopped after saving a reply.
            _immutable(self.directory / (digest + ".legacy-entry.json"), _bytes(entry))
            attempt = {"number": 1, "reserved_usd": entry["reserved_usd"], "imported_legacy": True,
                       **_outcome("unknown")}
            try:
                request_path = self.directory / (digest + ".request.json")
                response_path = self.directory / (digest + ".json")
                if not request_path.exists() and entry.get("status") == "ok":
                    response_raw = response_path.read_bytes()
                    response = json.loads(response_raw)
                    attempt["response_file"] = digest + ".attempt-1.response.json"
                    _immutable(self.directory / attempt["response_file"], response_raw)
                    if _legacy_success(entry, response):
                        attempt.update(_outcome("ok", entry["actual_usd"]), legacy_request_missing=True)
                    else:
                        attempt["error_type"] = "UnverifiableLegacySuccess"
                    entry["attempts"] = [attempt]
                    self._sync(entry)
                    changed = True
                    continue
                request_raw = request_path.read_bytes()
                request = json.loads(request_raw)
                if _digest(request["url"], request["body"]) != digest:
                    raise ValueError("Legacy digest mismatch")
                attempt["request_file"] = digest + ".attempt-1.request.json"
                _immutable(self.directory / attempt["request_file"], request_raw)
                response_raw = response_path.read_bytes()
                response = json.loads(response_raw)
                attempt["response_file"] = digest + ".attempt-1.response.json"
                _immutable(self.directory / attempt["response_file"], response_raw)
                attempt.update(_classify(request["url"], request["body"], response, self.prices))
                if request["url"] == aisa.CHAT and attempt["status"] == "ok" and entry.get("status") == "ok" and _number(entry.get("actual_usd")):
                    attempt["actual_usd"] = entry["actual_usd"]
            except (OSError, ValueError, KeyError, TypeError, aisa.AisaError) as exc:
                attempt["error_type"] = type(exc).__name__
            entry["attempts"] = [attempt]
            self._sync(entry)
            changed = True
        if changed:
            save(self.path, self.entries)

    @property
    def reserved(self) -> float:
        return sum(a.get("actual_usd", 0) if a.get("charge_known") and a["status"] != "pending" else
                   max(a["reserved_usd"], a.get("actual_usd", 0))
                   for e in self.entries for a in e.get("attempts", [e]))

    def _reserve(self, url, body):
        if url == aisa.CHAT:
            if body.get("model") != "qwen3.7-flash" or body.get("enable_thinking") is not False:
                raise aisa.AisaError("Unbudgeted model or reasoning mode")
            tokens = len(json.dumps(body["messages"], ensure_ascii=False).encode()) + 512
            return ((tokens * max(self.prices["input"], self.prices.get("cache_write", 0)) +
                     body["max_tokens"] * self.prices["output"]) / 1e6 * 1.25, body["model"])
        if url == aisa.ROUTER:
            calls = _calls(body)
            amount = quote(calls)
            if not _number(amount):
                raise aisa.AisaError("Invalid authenticated quote")
            return amount * 1.25, ",".join(c["tool"] for c in calls)
        raise aisa.AisaError("Unknown paid endpoint")

    def _cached_youtube_empty(self, entry, url, body, cached):
        """Read an old known-charge classification without altering audit history."""
        if (url != aisa.ROUTER or entry.get("outcome") != "source_failure" or entry.get("global_stop") or
                not cached.exists() or len(body.get("calls", [])) != 1):
            return None
        latest = entry["attempts"][-1]
        if latest.get("outcome") != "source_failure" or latest.get("charge_known") is not True:
            return None
        try:
            request = json.loads((self.directory / latest["request_file"]).read_text(encoding="utf-8"))
            response = json.loads((self.directory / latest["response_file"]).read_text(encoding="utf-8"))
            if (_digest(request["url"], request["body"]) != entry["id"] or request != {"url": url, "body": body} or
                    json.loads(cached.read_text(encoding="utf-8")) != response):
                return None
            verdict = _classify(url, body, response, self.prices)
            if (verdict["status"] != "ok" or not verdict["charge_known"] or
                    verdict["actual_usd"] != latest.get("actual_usd") or
                    not _youtube_empty_call(body["calls"][0], response["results"][0]["data"])):
                return None
        except (OSError, ValueError, KeyError, TypeError, aisa.AisaError):
            return None
        if any(e.get("global_stop", e["status"] != "ok") for e in self.entries):
            raise JournalError("An unknown or account-level failure blocks cached reinterpretation", "global_block")
        self.cache_interpretations.append({"id": entry["id"], "original_outcome": entry["outcome"],
                                          "interpretation": "completed_empty_youtube_sample", "request_sent": False,
                                          "original_actual_usd": entry["actual_usd"], "response_file": latest["response_file"]})
        return response

    def request(self, url: str, body: dict, timeout: int, send):
        sent = False
        def recorded_send(*args):
            nonlocal sent
            sent = True
            return send(*args)
        try:
            return self._request(url, body, timeout, recorded_send)
        except Exception as exc:
            exc.request_sent = sent
            if not hasattr(exc, "failure_kind"):
                exc.failure_kind = "unknown_outcome" if sent else "not_executed"
            raise

    def _request(self, url, body, timeout, send):
        with self._locked():
            self._reload()
            self._upgrade_legacy()
            digest = _digest(url, body)
            entry = next((e for e in self.entries if e["id"] == digest), None)
            cached = self.directory / (digest + ".json")
            if entry and entry["status"] == "ok" and cached.exists():
                return json.loads(cached.read_text(encoding="utf-8"))
            if entry:
                interpreted = self._cached_youtube_empty(entry, url, body, cached)
                if interpreted is not None:
                    return interpreted
            if entry and (not entry.get("retryable") or len(entry["attempts"]) >= self.MAX_ATTEMPTS or
                          url != aisa.ROUTER or any(c["tool"] not in RETRYABLE_READ_ONLY_TOOLS for c in _calls(body))):
                raise JournalError("Prior request failed or was interrupted; automatic paid retry disabled", "retry_disabled")
            if any(e.get("global_stop", e["status"] != "ok") for e in self.entries):
                raise JournalError("An unknown or account-level failure occurred; further paid calls are stopped", "global_block")
            while True:
                attempt_number = len(entry["attempts"]) + 1 if entry else 1
                if attempt_number > 1:
                    time.sleep(attempt_number - 1)
                reserve, label = self._reserve(url, body)  # Fresh quote on every attempt.
                if self.reserved + reserve > self.limit:
                    raise JournalError("Daily estimated spending limit reached; no request sent", "budget_limit")
                if entry is None:
                    entry = {"id": digest, "label": label, "attempts": []}
                    self.entries.append(entry)
                attempt = {"number": attempt_number, "reserved_usd": reserve, "status": "pending",
                           "outcome": "unknown", "global_stop": True, "charge_known": False,
                           "request_file": f"{digest}.attempt-{attempt_number}.request.json"}
                if url == aisa.CHAT:
                    attempt.update(cost_basis="token_estimate", pricing_snapshot=dict(self.prices), settled_usd=None)
                else:
                    attempt["cost_basis"] = "provider_response"
                entry["attempts"].append(attempt)
                self._sync(entry)
                save(self.path, self.entries)  # Persist pending reservation before sending.
                try:
                    raw_request = _bytes({"url": url, "body": body})
                    _immutable(self.directory / attempt["request_file"], raw_request)
                    original_request = self.directory / (digest + ".request.json")
                    if not original_request.exists():
                        _immutable(original_request, raw_request)
                    print("API: " + label, flush=True)
                    result = send(url, body, timeout)
                    attempt["response_file"] = f"{digest}.attempt-{attempt_number}.response.json"
                    _immutable(self.directory / attempt["response_file"], _bytes(result))
                    attempt.update(_classify(url, body, result, self.prices))
                    if not cached.exists():
                        _immutable(cached, _bytes(result))
                    elif attempt["status"] == "ok":
                        _immutable(self.directory / (digest + ".pre-recovery.json"), cached.read_bytes())
                        save(cached, result)
                except Exception as exc:
                    # A missing/unsaved response never licenses a retry, even if
                    # the exception text claims the service did not charge.
                    kind = "account_failure" if isinstance(exc.__cause__, urllib.error.HTTPError) and exc.__cause__.code in {401, 402, 403} else "unknown"
                    exc.failure_kind = "account_failure" if kind == "account_failure" else "unknown_outcome"
                    attempt.update(_outcome(kind), error_type=type(exc).__name__)
                    self._sync(entry)
                    save(self.path, self.entries)
                    raise
                self._sync(entry)
                save(self.path, self.entries)
                if attempt["status"] == "ok":
                    return result
                if not attempt["retryable"] or attempt_number >= self.MAX_ATTEMPTS:
                    kind = ("retry_exhausted" if attempt["retryable"] else "unknown_outcome" if
                            attempt["outcome"] == "unknown" else attempt["outcome"])
                    raise JournalError("Data request unsuccessful or partially failed; outcome=" + attempt["outcome"], kind)
