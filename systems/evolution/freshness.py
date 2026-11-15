"""I3: freshness structure for context blocks. Every item presented as current carries (source decision event, checked-through
watermark); a bounded, deterministic scan of the events AFTER the item's last decision surfaces candidate mentions (lexical on the
item's terms and aliases, plus a vector leg on the stand-in hash embedding) as CHALLENGERS shown beside the item and flagged.
Challengers are never applied: rendering does not touch state. Paraphrase beyond lexical and vector reach is the analyst tier."""
from memsys import fusion
from . import decisions as D, task

STOP = set("the a an is are was were to of and or for in on at by with that this it be as from not we you i they he she our your item".split())
OPERATING_POINTS = {"strict": {"min_shared": 2, "min_frac": 0.5, "cos": 0.6}, "loose": {"min_shared": 1, "min_frac": 0.0, "cos": 0.35}}


def terms(text, aliases=()):
    toks = [t for t in fusion.tokens(text) if t not in STOP and len(t) >= 3 and not (t[0] in "iv" and t[1:].isdigit())]
    core = sorted(set(toks))
    al = sorted({a for x in aliases for a in fusion.tokens(x)} - STOP)
    return core, al


def scan(events, after_seq, head, item_text, aliases=(), bound=2000, point="strict"):
    """events: dicts {seq, kind, trust_origin, body:{text}}. Scans messages with after_seq < seq <= head, newest first, at most `bound`."""
    p = OPERATING_POINTS[point]
    core, al = terms(item_text, aliases)
    want = set(core) | set(al)
    qv = fusion.embed(item_text)
    window = [e for e in events if e["kind"] == "message" and after_seq < e["seq"] <= head]
    window.sort(key=lambda e: -e["seq"])
    truncated = len(window) > bound
    out = []
    for e in window[:bound]:
        txt = e["body"].get("text", "")
        toks = set(fusion.tokens(txt))
        shared = sorted(want & toks)
        frac = len(set(core) & toks) / max(1, len(core))
        cos = fusion.cosine(qv, fusion.embed(txt))
        lex = len(shared) >= p["min_shared"] or (frac >= p["min_frac"] > 0)
        if lex or cos >= p["cos"]:
            out.append({"seq": e["seq"], "trust": e["trust_origin"], "quote": txt[:120], "shared": shared, "cos": round(cos, 3), "via": "lexical" if lex else "vector"})
    out.sort(key=lambda c: (-len(c["shared"]), -c["cos"], c["seq"]))
    return {"challengers": out, "scanned": min(len(window), bound), "truncated": truncated, "checked_through": head}


def last_decision_seq(s, item):
    ch = s["item_chain"].get(item)
    if ch:
        return max(c["seq"] for c in ch)
    return next((a["seq"] for a in s["amendments"] if item in a["touched"]), 0) if s["amendments"] else 0


def render_fresh(s, events, head, aliases=None, bound=2000, point="strict"):
    """Deterministic context block: current items with (source decision, checked-through) and flagged challengers. Pure: does not change s."""
    aliases = aliases or {}
    st = D.item_status(s)
    L = [f"TASK DEFINITION [CURRENT] version {s['version']} | checked through event {head}"]
    for iid in sorted(s["items"]):
        it = s["items"][iid]
        last = last_decision_seq(s, iid)
        src = (s["item_chain"].get(iid) or [{"decision_id": "definition"}])[-1]["decision_id"]
        L.append(f"[CURRENT] {iid} ({it['type']}): {it['text']} | source: decision {src} at event {last} | checked through event {head} | status {st[iid]}")
        r = scan(events, last, head, it["text"], aliases.get(iid, ()), bound, point)
        for c in r["challengers"]:
            L.append(f"   [CHALLENGER? not applied] event {c['seq']} by {c['trust']} ({c['via']}): \"{c['quote']}\"")
        if r["truncated"]:
            L.append(f"   [SCAN TRUNCATED] only the newest {bound} events after event {last} were checked")
    for cid, c in sorted(s["pending"].items()):
        L.append(f"[PENDING, not in effect] {cid} on {c['item']}: {c.get('proposed_text') or 'remove'}")
    return "\n".join(L)
