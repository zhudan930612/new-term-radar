"""Bounded YouTube identity followups, imported only inside a full source audit."""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from .demand_research import _video
from .opportunity_validation import check_evidence


def checks(evidence: list[dict]) -> list[dict]:
    return [{"source_id": r["source_id"], "video_url": r["url"], "profile_url": r.get("author_url", ""),
             "quote": r["text"], "missing": [k for k, absent in
                (("channel_identity", not r.get("author")), ("comment_locator", not r.get("source_record_id"))) if absent],
             "next_action": "Read the exact comment and its author profile; save original text, comment URL and channel ID. If blocked, retain unknown; do not buy more comments."}
            for r in evidence if _video(r.get("url", "")) and r.get("kind") == "comment" and
            (not r.get("author") or not r.get("source_record_id"))]


def apply(ledger: dict, rows: list[dict], scope: list[str], root, day: str) -> None:
    if not isinstance(rows, list) or len({r.get("source_id") for r in rows}) != len(rows):
        raise ValueError("Identity observations must have unique source IDs")
    checked = []
    for row in rows:
        sid = row.get("source_id")
        if sid not in scope:
            raise ValueError("Identity observation outside source audit")
        source = ledger["sources"][sid]["source"]
        video = _video(source["url"])
        if not video or source.get("kind") != "comment" or not source.get("author_url"):
            raise ValueError("Identity correction requires a known YouTube comment and profile link")
        comment, profile = row["comment"], row["profile"]
        check_evidence(comment, root)
        check_evidence(profile, root, source["author_url"])
        channel = row.get("channel_id", "")
        parsed = urlsplit(comment["url"])
        cid = parse_qs(parsed.query).get("lc", [""])[0]
        if (_video(comment["url"]) != video or not re.fullmatch(r"[A-Za-z0-9_.-]+", cid) or
                not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel)):
            raise ValueError("Exact same-video comment URL and immutable channel ID required")
        # Bind the person to the particular original, not another comment on the page.
        if source["text"] not in comment["quote"] or source["author_url"] not in comment["quote"]:
            raise ValueError("Comment quote must bind the original text to its observed profile")
        if f"/channel/{channel}" not in profile["quote"]:
            raise ValueError("Profile quote must show the immutable channel identity")
        for obs in (comment, profile):
            stamp = datetime.fromisoformat(obs["observed_at"])
            if stamp.tzinfo is None or stamp.date().isoformat() < day or stamp > datetime.now(timezone.utc):
                raise ValueError("Identity observation time must be actual and no earlier than the audit date")
        checked.append((sid, {**source, "url": comment["url"], "source_record_id": cid,
                              "author": "youtube:" + channel, "author_id": channel,
                              "identity_status": "source_verified", "context_status": "comment_and_profile_read"},
                        {**copy.deepcopy(row), "file_hashes": {obs["evidence_file"]:
                            hashlib.sha256((root / obs["evidence_file"]).read_bytes()).hexdigest() for obs in (comment, profile)}}))
    for sid, source, observation in checked:
        entry = ledger["sources"][sid]
        if entry["source"] != source:
            entry.setdefault("source_history", []).append(copy.deepcopy(entry["source"]))
            entry["source"] = source
            entry.setdefault("identity_observations", []).append(observation)
