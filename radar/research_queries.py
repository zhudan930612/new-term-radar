"""Pure, bounded search planning; lexical matches are never demand evidence."""
from __future__ import annotations

import re

QUERY_STRATEGY_VERSION = 1
DEFAULT_DISCOVERY_QUERIES = [
    '(bookkeeper OR accountant) (reconcile OR reconciliation) manually',
    '(teacher OR tutor) (grading OR worksheets) "takes hours"',
    '(freelancer OR agency) (invoice OR report) "copy paste"',
    '(shop OR seller) (inventory OR orders) spreadsheet',
    '(researcher OR student) (references OR bibliography) manually',
    '(creator OR editor) (subtitles OR captions) manually',
    '("small business" OR nonprofit) (forms OR records) "every time"',
    '("I have to" OR "we have to") (export OR convert OR transfer) manually',
]

STOP = set("a an the in on at to for of from with without and or not i we my our it is are be can how when by that this into using automatically automatic manually tool tools".split())
ALIASES = {
    "find": "search", "locate": "search", "lookup": "search", "searching": "search",
    "excel": "spreadsheet", "spreadsheets": "spreadsheet", "workbook": "spreadsheet", "workbooks": "spreadsheet",
    "ppt": "presentation", "powerpoint": "presentation", "slides": "presentation", "presentations": "presentation",
    "formulas": "formula", "autofill": "extend", "extending": "extend", "adding": "add", "rows": "row",
    "invoices": "invoice", "bills": "invoice", "documents": "document", "files": "file",
    "images": "image", "photos": "image", "photo": "image", "pictures": "image",
    "subtitles": "subtitle", "captions": "subtitle", "caption": "subtitle",
    "editing": "edit", "edited": "edit", "conversion": "convert", "converting": "convert",
    "extraction": "extract", "extracting": "extract", "deleting": "remove", "delete": "remove", "removing": "remove",
    "silent": "silence", "pauses": "silence", "bookmarks": "bookmark", "headings": "heading",
    "formatting": "format", "formatted": "format", "reconciliation": "reconcile", "reconciling": "reconcile",
    "exporting": "export", "exports": "export", "importing": "import", "merging": "merge", "combine": "merge",
    "sharing": "share", "tracking": "track", "generating": "generate", "generation": "generate",
    "recognition": "recognize", "detecting": "detect", "detection": "detect", "books": "book",
    "videos": "video", "clips": "video", "clip": "video", "cropping": "crop", "resizing": "resize",
    "recommendations": "recommend", "recommendation": "recommend", "recommended": "recommend",
    "verifying": "verify", "checking": "check", "rules": "rule", "conflicts": "conflict",
    "hiding": "hide", "unhiding": "unhide", "protecting": "protect", "linking": "link", "linked": "link",
}
ACTIONS = set("search extend add sum convert extract remove edit format reconcile export import merge share track generate recognize detect crop resize split compare sort sync repair translate annotate position recommend verify check hide unhide protect link".split())
CONTEXT = set("content task workflow data new next result results ai online local offline protected editable batch bulk precise raw".split())
SYNONYMS = {"search": "find", "find": "search", "spreadsheet": "Excel", "spreadsheets": "Excel",
            "presentations": "slides", "invoices": "bills", "subtitles": "captions", "captions": "subtitles",
            "remove": "delete", "editing": "edit", "convert": "conversion", "extract": "extraction",
            "merge": "combine", "photos": "images", "bookmarks": "outline"}
CONSTRAINT_PATTERNS = {
    "protected": r"\b(?:protected|locked|protect|lock)\b|受保护|锁定(?:的)?(?:列|单元格|表格)",
    "offline": r"\b(?:offline|without (?:an? )?internet|no internet)\b|离线|不联网|无需联网",
    "local": r"\b(?:local|locally|on.device|desktop)\b|本地|本机|设备端",
    "read_only": r"\bread.only\b|只读",
    "no_upload": r"\b(?:no uploads?|without uploading|never upload)\b|(?:不|不能|不得|禁止|无需)上传",
    "free": r"\b(?:free|no.cost)\b|免费|零费用|无需付费",
    "no_paid_api": r"\b(?:without paid (?:ai )?apis?|no (?:paid (?:ai )?api|token) costs?)\b|无付费\s*(?:AI\s*)?API|无token(?:费用|成本)",
    "batch": r"\b(?:batch|bulk)\b|批量",
    "mac": r"(?<![a-z0-9])(?:mac|macos|macbook|os x)(?![a-z0-9])",
    "windows": r"(?<![a-z0-9])windows(?![a-z0-9])",
    "linux": r"(?<![a-z0-9])linux(?![a-z0-9])",
    "audio_only": r"\b(?:audio.only|pure audio|audio files?)\b|纯音频|仅音频",
}
CONSTRAINT_CONFLICTS = {
    "protected": r"\b(?:unprotect\w*|unlocked|remove protection)\b|取消保护|解除保护",
    "offline": r"\b(?:not offline|online.only|requires? (?:an? )?internet|cloud.only)\b|必须联网|仅在线|仅云端",
    "local": r"\b(?:cloud.only|server.only)\b|仅云端|仅服务器",
    "read_only": r"\b(?:requires? write|write access required)\b|需要写入权限",
    "no_upload": r"\b(?:upload required|requires? upload)\b|必须上传|需要上传",
    "free": r"\b(?:paid.only|payment required|subscription required)\b|必须付费|需要付费订阅",
    "no_paid_api": r"\b(?:paid (?:ai )?api required|requires? paid (?:ai )?api)\b|需要付费API",
    "audio_only": r"\b(?:video.only|audio not supported)\b|仅支持视频|不支持音频",
}


def explicit_constraint_text(value: str) -> str:
    """Unknowns and questions are not affirmative requirements, in either language."""
    if not isinstance(value, str):
        return ""
    clauses = re.split(r"[;；。\n\r,，、]+", value)
    uncertain = r"未知|待核实|待确认|待验证|不确定|未说明|未明确|是否|可能|\b(?:unknown|uncertain|whether|maybe|might)\b|not (?:known|specified|sure)"
    not_required = r"(?:无需|无须|不要求|不需要|没有).*(?:离线|本地|保护|只读|批量)|\b(?:offline|local|protected|read.only|batch) (?:is )?not required\b|\bno (?:offline|local|batch) requirement\b"
    return "\n".join(c for c in clauses if not re.search(uncertain, c, re.I) and not re.search(not_required, c, re.I))


def task_constraint_context(task: dict) -> str:
    terms = [s for s in task.get("search_terms", []) if isinstance(s, str)]
    return "\n".join(explicit_constraint_text(x) for x in
                     terms + [task.get("constraint", ""), task.get("context", "")])


def words(value) -> list[str]:
    if not isinstance(value, str):
        return []
    # Model terms are text, never search operators or injected domains.
    value = re.sub(r"https?://\S+|\b\w+:\S+", " ", value)
    return [w for w in re.findall(r"[^\W_]+", value.lower(), re.UNICODE) if w not in STOP]


def short_phrase(value) -> str:
    return " ".join(words(value)[:6])


def query_variants(task: dict) -> list[str]:
    research = task.get("research") if isinstance(task.get("research"), dict) else {}
    approved = research.get("query_variants", [])
    values = approved if isinstance(approved, list) and approved else task.get("search_terms", [])
    compact = (lambda value: " ".join(words(value))) if approved else short_phrase
    variants = list(dict.fromkeys(compact(x) for x in values if compact(x)))[:2 if approved else 3]
    if not variants:
        return []
    if not approved:
        base = variants[0]
        synonym = " ".join(SYNONYMS.get(w, w) for w in base.split())
        variants += [synonym, base + " problem", synonym + " workaround"]
        variants = list(dict.fromkeys(variants))
    attempts = task.get("search_attempts", research.get("search_attempts", 0))
    attempts = max(0, attempts) if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
    offset = attempts % len(variants)
    variants = variants[offset:] + variants[:offset]
    history = []
    for owner in (research, task):
        for key in ("query_history", "search_history"):
            if isinstance(owner.get(key), list):
                history.extend(owner[key])
    tried = set()
    for item in history:
        entries = item.get("queries", [item]) if isinstance(item, dict) else [item]
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                # Missing-key/quote-block records were not attempted searches.
                if entry.get("status") and not (entry["status"] == "complete" or
                        entry.get("request_sent") is True or entry.get("response_received") is True):
                    continue
                value = entry.get("query", "")
            else:
                value = entry
            tried.add(" ".join(words(value)))
    variants.sort(key=lambda value: " ".join(words(value)) in tried)
    return variants


def relevance(phrase: str, text: str, *, constraint_context: str = "", title: str = "") -> dict:
    """Conservative search triage only; no source or single-user need is discarded."""
    wanted = {ALIASES.get(w, w) for w in words(phrase)}
    present = {ALIASES.get(w, w) for w in words(text)}
    actions = wanted & ACTIONS
    objects = wanted - ACTIONS - CONTEXT - {"problem", "workaround", "help", "tutorial"}
    # A phrase may not name a known action; do not invent one. Require two meaningful
    # object/context overlaps in that case and expose the weaker check to the auditor.
    object_match = bool(objects & present)
    action_match = bool(actions & present)
    if "extend" in actions and "add" in present and "formula" in present:
        action_match = True
    task_match = object_match and (action_match if actions else len(wanted & present) >= 2)
    title_terms = {ALIASES.get(w, w) for w in words(title)}
    title_actions = title_terms & ACTIONS
    title_action_match = bool(title_actions & actions) or (
        "extend" in actions and "add" in title_actions and "formula" in title_terms)
    title_action_conflict = bool(actions and title_actions and not title_action_match)
    explicit = explicit_constraint_text(phrase + "\n" + constraint_context)
    required = {name for name, pattern in CONSTRAINT_PATTERNS.items() if re.search(pattern, explicit, re.I)}
    missing = {name for name in required if not re.search(CONSTRAINT_PATTERNS[name], text, re.I)}
    contradicted = {name for name in required if name in CONSTRAINT_CONFLICTS and
                   re.search(CONSTRAINT_CONFLICTS[name], text, re.I)}
    relevant = task_match and not missing and not contradicted and not title_action_conflict
    reason = ("title describes a different action; summary overlap needs manual context review before paid comments" if title_action_conflict else
              "explicit constraint contradicted; manual context review required before paid comments" if contradicted else
              "constraint unknown from title/summary; manual context review required before paid comments" if missing else
              "lexical object/action candidate; human same-task review required" if task_match else
              "missing task object/action in available title or text; not evidence of absent demand")
    return {"candidate": relevant, "matched_terms": sorted(wanted & present),
            "action_terms": sorted(actions), "object_terms": sorted(objects),
            "title_action_conflict": title_action_conflict,
            "match_status": "candidate" if relevant else "partial" if task_match else "unrelated",
            "constraint_status": "conflict" if contradicted else "constraint_unknown" if missing else
                                 "present_in_metadata" if required else "not_required",
            "required_constraints": sorted(required), "missing_constraints": sorted(missing),
            "contradicted_constraints": sorted(contradicted), "reason": reason}


def followup_summary(checks: list[dict]) -> dict:
    outcomes = [{k: value for k, value in row.items() if k in {
        "task_id", "source", "query", "status", "request_sent", "response_received", "returned", "accepted", "relevant_candidates",
        "relevance", "variant", "reason", "failure_kind"}} for row in checks if row.get("task_id")]
    attempted = sorted({r["task_id"] for r in outcomes
                        if r["status"] == "complete" or r.get("request_sent") is True or r.get("response_received") is True})
    evidence = sorted({r["task_id"] for r in outcomes if r.get("accepted", 0) > 0})
    for row in outcomes:
        row["outcome"] = ("raw_evidence_pending_audit" if row.get("accepted", 0) else
                          "empty" if row["status"] == "complete" and row.get("returned") == 0 else
                          "no_raw_evidence" if row["status"] == "complete" else row["status"])
    return {"attempted_followups": attempted, "evidence_followups": evidence, "confirmed_followups": [],
            "followup_outcomes": outcomes, "successful_followups": attempted,
            "successful_followups_semantics": "compatibility alias for attempted_followups, not confirmed evidence",
            "query_strategy_version": QUERY_STRATEGY_VERSION}
