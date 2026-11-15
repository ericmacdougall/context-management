"""FACTS arm: per-session extraction (recipe extract-v1), parsing, code-side supersession, lexical + cue-key retrieval."""
import json
import math
import re
from datetime import datetime

from .anon import opaque_sid
from .data import parse_date, render_session, rounds_of
from .prompts import EXTRACT_SYSTEM, EXTRACT_TEMPLATE
from .supersede import resolve

MODALITIES = ("asserted", "decided", "proposed", "hypothetical", "question")
_STOP = set("a an the of to is are was were be been has have had in at on for and or my i me you your do does did what which who how when where "
            "many much it its that this with from by as about than then there their they we our".split())


def render_for_extraction(sid, date_str, turns):
    rs = rounds_of(sid, turns, date_str)
    body = "\n".join("[r%d] %s" % (r.index, r.text) for r in rs)
    return "### SESSION id=%s date=%s\n%s" % (opaque_sid(sid), date_str, body)


def build_extract_prompt(batch):
    """batch: [(sid, date_str, turns)]"""
    return EXTRACT_TEMPLATE.format(n=len(batch), sessions="\n\n".join(render_for_extraction(*s) for s in batch))


def _json_from(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("no JSON object in extractor output: %r" % text[:200])
    body = t[a:b + 1]
    try:
        return json.loads(body)
    except ValueError:
        return json.loads(body + _missing_closers(body))  # the model sometimes stops short of its closing brackets


def _missing_closers(text):
    stack, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    return "".join(reversed(stack))


def _date(s, fallback):
    if not s:
        return fallback
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except ValueError:
        return fallback


def parse_extraction(text, batch):
    """Returns ([fact list per batch position], dropped). Output entries are matched to batch items by session id in order of
    appearance, so the same session id occurring twice in one batch (with different dates, as in LongMemEval) is kept apart.
    Unknown ids and malformed facts are dropped and counted."""
    obj = _json_from(text)
    slots = {}
    for pos, (sid, d, _) in enumerate(batch):
        slots.setdefault(sid, []).append(pos)
    used = {sid: 0 for sid in slots}
    out, dropped = [[] for _ in batch], 0
    for ent in obj.get("sessions", []):
        sid = ent.get("session_id")
        if sid not in slots or used[sid] >= len(slots[sid]):
            dropped += 1
            continue
        pos = slots[sid][used[sid]]
        used[sid] += 1
        asserted = parse_date(batch[pos][1])
        for f in ent.get("facts", []):
            if not all(k in f for k in ("subject", "predicate", "object")):
                dropped += 1
                continue
            mod = f.get("modality") if f.get("modality") in MODALITIES else "asserted"
            out[pos].append({
                "subject": str(f["subject"]), "predicate": str(f["predicate"]), "object": str(f["object"]),
                "valid_from": _date(f.get("valid_from"), asserted), "valid_to_stated": f.get("valid_to"),
                "asserted_at": asserted, "modality": mod, "scope": str(f.get("scope", "")),
                "author": "assistant" if f.get("author") == "assistant" else "user", "session_id": sid,
                "round": f.get("round") if isinstance(f.get("round"), int) else 0,
                "cue_keys": [str(c) for c in (f.get("cue_keys") or [])][:6]})
    return out, dropped


def toks(s):
    return [t for t in re.findall(r"[a-z0-9]+", str(s).lower()) if t not in _STOP and len(t) > 1]


def score_facts(question, facts, k=20, cue_weight=2.0):
    """Simple lexical match over subject/predicate/object (weight 1) and cue keys (weight cue_weight), idf-weighted over the fact set."""
    n = len(facts)
    docs = []
    for f in facts:
        body = set(toks(" ".join((f["subject"], f["predicate"], f["object"]))))
        cues = set(t for c in f["cue_keys"] for t in toks(c))
        docs.append((body, cues))
    df = {}
    for body, cues in docs:
        for t in body | cues:
            df[t] = df.get(t, 0) + 1
    q = set(toks(question))
    scored = []
    for f, (body, cues) in zip(facts, docs):
        s = sum(math.log(1 + n / float(df[t])) * (1.0 if t in body else 0.0) + math.log(1 + n / float(df[t])) * (cue_weight if t in cues else 0.0)
                for t in q if t in df)
        if s > 0:
            scored.append((s, f))
    scored.sort(key=lambda x: (-x[0], x[1]["asserted_at"]))
    return [f for _, f in scored[:k]]


def as_of_filter(facts, question_date):
    return [f for f in facts if f["asserted_at"].date() <= question_date.date()]


def format_fact(f):
    status = f["status"].upper()
    span = "valid from %s" % f["valid_from"].date()
    if f.get("valid_to"):
        span += " to %s (superseded)" % f["valid_to"].date()
    return "- [%s] %s | %s | %s (%s; %s, %s; stated in session %s round %d)" % (
        status, f["subject"], f["predicate"], f["object"], span, f["modality"], f["author"], opaque_sid(f["session_id"]), f["round"])


def build_context(question, qdate, facts, sessions_by_id, max_rounds=20):
    """facts: resolved facts for the question's haystack. Returns (context string, info)."""
    vis = as_of_filter(facts, qdate)
    top = score_facts(question, [f for f in vis if f["modality"] != "question"])
    lines = ["Memory facts (most relevant first; status CURRENT means the latest known value as of today, SUPERSEDED means replaced by a later fact):"]
    lines += [format_fact(f) for f in top]
    cited, seen = [], set()
    for f in top:
        key = (f["session_id"], f["round"])
        if key not in seen and len(cited) < max_rounds:
            seen.add(key)
            cited.append(key)
    cited.sort(key=lambda k: (sessions_by_id[k[0]][0], k[1]))
    lines.append("\nSource excerpts (verbatim rounds cited by the facts):")
    for sid, ri in cited:
        d, turns = sessions_by_id[sid]
        rs = rounds_of(sid, turns, d)
        r = next((x for x in rs if x.index == ri), None)
        if r:
            lines.append("### Session %s | date: %s | round %d\n%s" % (opaque_sid(sid), d, ri, r.text))
    return "\n".join(lines), {"facts_shown": len(top), "rounds_cited": len(cited)}


def resolve_all(per_session, order_sessions):
    """Flatten per-session facts in haystack order and run code-side supersession."""
    flat, order = [], 0
    for sid in order_sessions:
        for f in per_session.get(sid, []):
            f = dict(f)
            f["order"] = order
            order += 1
            flat.append(f)
    return resolve(flat)
