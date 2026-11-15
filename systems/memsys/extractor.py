"""Deterministic rule-based stand-in for the LLM extractor.

It exists so the plumbing (projection, supersession, erasure) can be tested.
It says nothing about extraction quality. Trust comes from the event envelope
(trust_origin), never from the text.

Recipe v1: raw attribute names. Recipe v2: spelling-normalised attributes
(favourite->favorite, colour->color) and a cue field; this changes which claims
share a slot, so v1 and v2 produce different supersession results.
"""
import hashlib
import re

RECIPES = (1, 2)

_PREFIXES = ("As you said, ", "Pasted: ", "According to the page, ", "Decided: ")
# Quote/paste markers lower the trust of a claim even when the envelope says user_typed: text a user typed that
# merely quotes someone else is not the user's own assertion.
_MARKER_TRUST = {"As you said, ": "assistant", "Pasted: ": "user_supplied_external", "According to the page, ": "web"}
_ASSERT = re.compile(r"^(?P<s>[A-Z][\w]*(?: [A-Z][\w]*)*)'s (?P<a>[a-z][\w ]*?)(?: for (?P<sc>[a-z][\w ]*?))? is (?P<v>.+?)\.?$")
_HYPO = re.compile(r"^(?:If|Suppose) (?P<s>[A-Z][\w]*(?: [A-Z][\w]*)*)'s (?P<a>[a-z][\w ]*?)(?: for (?P<sc>[a-z][\w ]*?))? (?:were|was) (?P<v>.+?)\.?$")
_PROPOSE = re.compile(r"^Proposal: set (?P<s>[A-Z][\w]*(?: [A-Z][\w]*)*)'s (?P<a>[a-z][\w ]*?)(?: for (?P<sc>[a-z][\w ]*?))? to (?P<v>.+?)\.?$")
_RENAME = re.compile(r"^(?P<s>[A-Z][\w]*(?: [A-Z][\w]*)*) is now called (?P<v>[A-Z][\w]*(?: [A-Z][\w]*)*)\.?$")

_V2_SPELL = (("favourite", "favorite"), ("colour", "color"), ("neighbour", "neighbor"))


def _attr(a: str, recipe: int) -> str:
    a = " ".join(a.split())
    if recipe >= 2:
        for old, new in _V2_SPELL:
            a = a.replace(old, new)
    return a


def fact_id(tenant_id, recipe, event_hash, ordinal) -> str:
    """Derivation-addressed id: a pure function of the inputs and the recipe,
    not of the plaintext object (which would be guessable after shredding)."""
    return hashlib.sha256(f"{tenant_id}|{recipe}|{event_hash}|{ordinal}".encode()).hexdigest()[:32]


def extract(recipe, ev, text):
    """ev: dict with tenant_id, session_id, seq, event_id, hash, ts_event(iso str),
    trust_origin, actor, subject_id. Returns a list of claim dicts (no ids of
    plaintext). One event yields at most one claim."""
    t = text.strip()
    marker = None
    for p in _PREFIXES:
        if t.startswith(p):
            t = t[len(p):]
            marker = _MARKER_TRUST.get(p)
            break
    modality = None
    m = _HYPO.match(t)
    if m:
        modality = "hypothetical"
    if not m:
        m = _PROPOSE.match(t)
        if m:
            modality = "proposed"
    if not m:
        m = _RENAME.match(t)
        if m:
            d = {"subject": m["s"], "predicate": "renamed_to", "scope": "*", "object": m["v"], "modality": "asserted"}
            return [_finish(recipe, ev, d, 0, marker)]
    if not m:
        m = _ASSERT.match(t)
        if m:
            modality = "decided" if text.strip().startswith("Decided: ") else "asserted"
    if not m:
        return []
    d = {"subject": m["s"], "predicate": _attr(m["a"], recipe), "scope": (m["sc"] or "*"),
         "object": m["v"].strip(), "modality": modality}
    return [_finish(recipe, ev, d, 0, marker)]


def _finish(recipe, ev, d, ordinal, marker=None):
    d = dict(d)
    d["id"] = fact_id(ev["tenant_id"], recipe, ev["hash"], ordinal)
    d["trust_origin"] = ev["trust_origin"]
    if marker is not None:
        from .rules import TRUST_RANK
        if TRUST_RANK[marker] < TRUST_RANK[d["trust_origin"]]:
            d["trust_origin"] = marker
    d["author"] = ev["actor"]
    d["valid_from"] = ev["ts_event"]
    d["input_event_ids"] = [ev["event_id"]]
    d["subject_id"] = ev["subject_id"]
    d["recipe_version"] = recipe
    d["order_key"] = f"{ev['ts_event']}|{ev['session_id']}|{ev['seq']:012d}|{ordinal:03d}"
    d["cue"] = f"{d['subject']} {d['predicate']}".lower() if recipe >= 2 else None
    return d
