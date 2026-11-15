"""FACTS arm, recipe v2: slot-keyed extraction parsing, question cue expansion, retrieval = lexical + cue-to-cue + slot lookup."""
import math

from .data import parse_date, rounds_of
from .facts import MODALITIES, _date, _json_from, as_of_filter, render_for_extraction, toks
from .prompts import EXTRACT_V2_TEMPLATE
from .supersede_v2 import norm_slot


def build_extract_prompt_v2(batch):
    return EXTRACT_V2_TEMPLATE.format(n=len(batch), sessions="\n\n".join(render_for_extraction(*s) for s in batch))


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def parse_extraction_v2(text, batch):
    obj = _json_from(text)
    slots = {}
    for pos, (sid, d, _) in enumerate(batch):
        slots.setdefault(sid, []).append(pos)
    used = {s: 0 for s in slots}
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
            if not all(k in f for k in ("subject", "predicate", "object", "slot")):
                dropped += 1
                continue
            out[pos].append({
                "subject": str(f["subject"]), "slot": str(f["slot"]), "predicate": str(f["predicate"]), "object": str(f["object"]),
                "cardinality": "single" if f.get("cardinality") == "single" else "multi",
                "value": _num(f.get("value")), "unit": f.get("unit"), "event_time": f.get("event_time"),
                "asserted_at": asserted, "valid_from": _date(f.get("event_time"), asserted),
                "modality": f.get("modality") if f.get("modality") in MODALITIES else "asserted",
                "scope": str(f.get("scope", "")), "author": "assistant" if f.get("author") == "assistant" else "user",
                "session_id": sid, "round": f.get("round") if isinstance(f.get("round"), int) else 0,
                "quote": str(f.get("quote", ""))[:200], "cue_keys": [str(c) for c in (f.get("cue_keys") or [])][:6]})
    return out, dropped


def score_v2(question, q_cues, facts, k=20, cue_weight=2.0, slot_weight=3.0):
    n = len(facts)
    docs = []
    for f in facts:
        body = set(toks(" ".join((f["subject"], f["predicate"], f["object"]))))
        cues = set(t for c in f["cue_keys"] for t in toks(c))
        slot = set(toks(f["slot"].replace("_", " ").replace(".", " ")))
        docs.append((body, cues, slot))
    df = {}
    for b, c, s in docs:
        for t in b | c | s:
            df[t] = df.get(t, 0) + 1
    q = set(toks(question))
    qc = set(t for c in q_cues for t in toks(c))
    idf = lambda t: math.log(1 + n / float(df[t]))
    scored = []
    for f, (b, c, s) in zip(facts, docs):
        sc = sum(idf(t) for t in q if t in b)
        sc += cue_weight * sum(idf(t) for t in (q | qc) if t in c)   # question tokens and expanded cues against fact cue keys
        sc += slot_weight * sum(idf(t) for t in (q | qc) if t in s)  # direct slot lookup
        if sc > 0:
            scored.append((sc, f))
    scored.sort(key=lambda x: (-x[0], x[1]["asserted_at"]))
    return [f for _, f in scored[:k]]


def format_fact_v2(f):
    span = "stated %s" % f["asserted_at"].date()
    if f.get("event_time"):
        span += ", event %s" % f["event_time"]
    if f.get("valid_to"):
        span += ", replaced %s" % f["valid_to"].date()
    return "- [%s] %s | %s = %s (%s; %s; slot %s; session %s round %d; quote: \"%s\")" % (
        f["status"].upper(), f["subject"], f["predicate"], f["object"], span, f["modality"], f["slot"], f["session_id"], f["round"], f["quote"])


def build_context_v2(question, q_cues, qdate, facts, sessions_by_id, max_rounds=20):
    vis = [f for f in as_of_filter(facts, qdate) if f["modality"] != "question"]
    top = score_v2(question, q_cues, vis)
    lines = ["Memory facts (most relevant first). CURRENT = latest known value; SUPERSEDED = replaced by a later statement of the same slot:"]
    lines += [format_fact_v2(f) for f in top]
    cited, seen = [], set()
    for f in top:
        key = (f["session_id"], f["round"])
        if key not in seen and len(cited) < max_rounds:
            seen.add(key)
            cited.append(key)
    cited.sort(key=lambda k: (parse_date(sessions_by_id[k[0]][0]), k[1]))
    lines.append("\nSource excerpts (verbatim rounds cited by the facts):")
    for sid, ri in cited:
        d, turns = sessions_by_id[sid]
        r = next((x for x in rounds_of(sid, turns, d) if x.index == ri), None)
        if r:
            lines.append("### Session %s | date: %s | round %d\n%s" % (sid, d, ri, r.text))
    return "\n".join(lines), {"facts_shown": len(top), "rounds_cited": len(cited), "top": top}
