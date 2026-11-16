"""P4: findings in a fixed schema, the host check (quote and speaker), and the model-facing rendering. Findings are UNTRUSTED until the host check
verifies the cited quote in the cited round and the speaker. Only host-verified text (the quote span and speaker re-read from the log) is ever
rendered; the analyst's own `claim` text is stored for humans and never injected."""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from memcore.inbox import InboxItem, render_block
from memcore.jsonschema_min import validate
from ctxrt.kernel import MemoryLog

SCHEMA_VERSION = 1
OUTCOMES = ("findings", "nothing_relevant", "dead_end_only")
DEAD_END_REASONS = ("not_relevant", "superseded", "wrong_speaker", "unverifiable", "duplicate", "out_of_scope")

FINDINGS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "outcome", "findings", "dead_ends"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "outcome": {"enum": list(OUTCOMES)},
        "findings": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "additionalProperties": False, "required": ["candidate_id", "event_id", "round", "speaker", "quote", "claim"],
            "properties": {"candidate_id": {"type": "string", "minLength": 1, "maxLength": 80}, "event_id": {"type": "string", "minLength": 1, "maxLength": 80},
                           "round": {"type": "integer", "minimum": 0}, "speaker": {"enum": ["user", "assistant", "tool"]},
                           "quote": {"type": "string", "minLength": 1, "maxLength": 2000}, "claim": {"type": "string", "maxLength": 300}}}},
        "dead_ends": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False, "required": ["candidate_id", "reason"],
            "properties": {"candidate_id": {"type": "string", "minLength": 1, "maxLength": 80}, "reason": {"enum": list(DEAD_END_REASONS)}}}},
    },
}


class SchemaRejected(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s: %s" % (code, detail))
        self.code, self.detail = code, detail


def parse_findings(raw) -> dict:
    """Typed rejection for every malformed output; returns the parsed object when the schema and the outcome rules hold."""
    if not isinstance(raw, str):
        raise SchemaRejected("not_text")
    try:
        obj = json.loads(raw)
    except ValueError:
        raise SchemaRejected("not_json")
    if not isinstance(obj, dict):
        raise SchemaRejected("not_object")
    errs = validate(obj, FINDINGS_SCHEMA)
    if errs:
        raise SchemaRejected("schema_invalid", "; ".join(errs)[:200])
    f, d, o = obj["findings"], obj["dead_ends"], obj["outcome"]
    if o == "findings" and not f:
        raise SchemaRejected("outcome_mismatch", "findings outcome with no findings")
    if o != "findings" and f:
        raise SchemaRejected("outcome_mismatch", "%s outcome with findings" % o)
    if o == "dead_end_only" and not d:
        raise SchemaRejected("outcome_mismatch", "dead_end_only without dead ends")
    return obj


# ---------------------------------------------------------------- host check (quote and speaker), after PROVENANCE_CHECK.md
def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_span(text: str, quote: str) -> Optional[str]:
    """The ORIGINAL span of `text` that equals `quote` after whitespace collapsing and lower-casing, or None."""
    words = quote.split()
    if not words:
        return None
    m = re.search(r"\s+".join(map(re.escape, words)), text, re.I)
    return m.group(0) if m else None


def host_check(log: MemoryLog, finding: dict) -> Tuple[str, Optional[dict]]:
    """Returns (verdict, evidence). verdict: PASS | REVIEW (ambiguous speaker) | REJECT:<code>. Evidence (on PASS) is read from the LOG, not from the analyst:
    the event, its speaker and the verbatim span."""
    ev = log.by_oid(finding["event_id"])
    if ev is None:
        return "REJECT:unknown_event", None
    turns = [e for e in log.upto(log.watermark()) if e["session"] == ev["session"] and e["round"] == finding["round"]]
    if not turns:
        return "REJECT:round_not_found", None
    hits = [(t, quote_span(t["text"], finding["quote"])) for t in turns]
    hits = [(t, s) for t, s in hits if s is not None]
    if not hits:
        return "REJECT:quote_not_in_cited_round", None
    speakers = {t["speaker"] for t, _ in hits}
    if len(speakers) > 1:
        return "REVIEW", {"speakers": sorted(speakers)}
    sp = next(iter(speakers))
    if sp != finding["speaker"]:
        return "REJECT:speaker_mismatch", None
    t, span = hits[0]
    return "PASS", {"event": t, "speaker": sp, "span": span}


# ---------------------------------------------------------------- rendering
def finding_id(question: str, event_oid: str, span: str) -> str:
    import hashlib
    return "f_" + hashlib.sha256(json.dumps([question, event_oid, span]).encode()).hexdigest()[:16]


def to_item(question: str, session: str, ev: dict, span: str, speaker: str, item_ids, now: float, ttl: float = 3600.0) -> InboxItem:
    """An inbox item built ONLY from log-verified data. trust_origin is `transcript`: DATA, never an instruction."""
    return InboxItem(finding_id=finding_id(question, ev["oid"], span), session_id=session, source_event_ids=[ev["oid"]], quote=span[:2000],
                     trust_origin="transcript", item_ids=list(item_ids), created=now, expires=now + ttl)


def render_findings(items: List[InboxItem], turn_id: str) -> str:
    """Empty list renders the EMPTY STRING: an explicit nothing_relevant outcome suppresses injection (no header, no placeholder)."""
    return render_block(items, turn_id) if items else ""


def naive_render_with_claim(items: List[InboxItem], claims: List[str], turn_id: str) -> str:
    """CONTROL: injects the analyst's claim text and a header even when empty."""
    body = "\n".join("ANALYST NOTE: " + c for c in claims)
    return "Analyst findings for turn %s:\n%s" % (turn_id, body or "(none)")
