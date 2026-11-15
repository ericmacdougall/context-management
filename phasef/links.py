"""Link recipe v4: pairwise content-based link decisions. Candidate generation in code, decisions by a Sonnet judge, status labels derived in code.

CANDIDATE FORMULA (tuned on the dev set only; parameters in CAND below):
  T(f) = stop-filtered lemma tokens of slot + predicate + cue keys + subject.  idf over the question's own facts.
  sim(a, b) = sum idf(T(a) & T(b)) / sum idf(T(a) | T(b)).
  A pair is a candidate when the facts are in different sessions, both modality asserted/decided/proposed, and
  sim >= CAND['theta']. Each fact keeps its top CAND['per_fact'] partners; the union is cut to the CAND['max_pairs'] highest-sim pairs per question.
"""
import hashlib
import json
import math
import re

from .prompts import (LINK_TEMPLATE_HEAD, LINK_TEMPLATE_PAIR, LINKAUDIT_HEAD, LINKAUDIT_PAIR)
from .supersede_v2 import FILLER, lemma

CAND = {"theta": 0.12, "per_fact": 5, "max_pairs": 40}
ROUND_CAP = 3000
OK_MOD = ("asserted", "decided", "proposed")
LABELS = ("replaces", "pending", "restates", "adds", "unrelated")


def fid(f):
    return hashlib.sha1(("%s|%s|%s|%s" % (f["session_key"], f["round"], f["slot"], f["object"])).encode()).hexdigest()[:12]


def tokset(f):
    text = " ".join([f["slot"].replace("_", " ").replace(".", " "), f["predicate"], f["subject"]] + list(f["cue_keys"]))
    return set(lemma(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in FILLER and len(t) > 1)


def order_key(f):
    return (f["asserted_at"], f.get("order", 0))


def candidates(facts, cand=None):
    cand = cand or CAND
    pool = [f for f in facts if f["modality"] in OK_MOD]  # any cardinality: the extractor mislabels counts as multi (dev finding)
    toks = {id(f): tokset(f) for f in pool}
    df = {}
    for f in pool:
        for t in toks[id(f)]:
            df[t] = df.get(t, 0) + 1
    n = float(len(pool)) or 1.0
    idf = lambda t: math.log(1 + n / df[t])
    scored = {}
    for i, a in enumerate(pool):
        mine = []
        for b in pool:
            if b["session_id"] == a["session_id"] and b["session_key"] == a["session_key"]:
                continue
            ta, tb = toks[id(a)], toks[id(b)]
            inter = ta & tb
            if not inter:
                continue
            union = ta | tb
            s = sum(idf(t) for t in inter) / sum(idf(t) for t in union)
            if s >= cand["theta"]:
                mine.append((s, b))
        mine.sort(key=lambda x: (-x[0], fid(x[1])))
        for s, b in mine[: cand["per_fact"]]:
            k = tuple(sorted((fid(a), fid(b))))
            scored[k] = (s, a, b)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1][0], kv[0]))[: cand["max_pairs"]]
    out = []
    for k, (s, a, b) in ranked:
        old, new = sorted((a, b), key=order_key)
        out.append({"pair": k, "sim": s, "old": old, "new": new})
    return out


def round_text(by, f):
    d, turns = by[f["session_id"]]
    from .data import rounds_of
    rs = rounds_of(f["session_id"], turns, d)
    r = next((x for x in rs if x.index == f["round"]), rs[0] if rs else None)
    t = r.text if r else ""
    return t if len(t) <= ROUND_CAP else t[:ROUND_CAP] + " [cut]"


def stmt(f):
    return "%s | %s = %s" % (f["subject"], f["predicate"], f["object"])


def build_link_prompt(pairs, by):
    body = "".join(LINK_TEMPLATE_PAIR.format(i=i, d1=p["old"]["asserted_at"].date(), s1=stmt(p["old"]), q1=p["old"].get("quote", ""), r1=round_text(p.get("by", by), p["old"]),
                                             d2=p["new"]["asserted_at"].date(), s2=stmt(p["new"]), q2=p["new"].get("quote", ""), r2=round_text(p.get("by", by), p["new"]))
                   for i, p in enumerate(pairs, 1))
    return LINK_TEMPLATE_HEAD + body + "\nNow output one line per pair."


def build_audit_prompt(items, by_of):
    body = "".join(LINKAUDIT_PAIR.format(i=i, d1=x["d1"], s1=x["s1"], r1=x["r1"], d2=x["d2"], s2=x["s2"], r2=x["r2"]) for i, x in enumerate(items, 1))
    return LINKAUDIT_HEAD + body + "\nNow output one line per pair: '<number>: <label>'."


def parse_labels(text, n, with_reason=False):
    got = {}
    for m in re.finditer(r"^\s*(\d+)\s*[:.)]\s*(replaces|pending|restates|adds|unrelated)\b[ \t]*\|?[ \t]*(.*)$", text, re.I | re.M):
        got.setdefault(int(m.group(1)), (m.group(2).lower(), m.group(3).strip()))
    missing = [i for i in range(1, n + 1) if i not in got]
    if missing:
        raise ValueError("missing pairs %s in %r" % (missing, text[:200]))
    return [got[i] if with_reason else got[i][0] for i in range(1, n + 1)]


def derive_status(facts, decisions):
    """decisions: {(fid_a, fid_b): label}. Sets status/superseded_by/pending_of/pending_updates on facts (advisory; nothing is removed)."""
    ids = {fid(f): f for f in facts}
    for i, f in enumerate(facts):
        f["_i"] = i
        f["superseded_by"], f["pending_of"], f["pending_updates"], f["valid_to"] = None, None, [], None
        f["status"] = "current" if f["modality"] in ("asserted", "decided") else "inactive"
    repl, pend = {}, []
    for (a, b), lab in sorted(decisions.items()):
        if a not in ids or b not in ids or lab not in ("replaces", "pending"):
            continue
        old, new = sorted((ids[a], ids[b]), key=order_key)
        if lab == "replaces":
            repl.setdefault(old["_i"], []).append(new)
        else:
            pend.append((old, new))
    links = []
    for oi, news in repl.items():
        old = facts[oi]
        news.sort(key=order_key)
        good = [n for n in news if not (n["author"] == "assistant" and old["author"] == "user")]
        for n in news:
            if n not in good:
                n["status"] = "disputed"  # lower trust replacing higher
        if good:
            top = good[-1]
            if len(good) > 1 and good[-2]["asserted_at"].date() == top["asserted_at"].date() and fid(good[-2]) != fid(top) and good[-2]["object"].lower() != top["object"].lower():
                good[-2]["status"] = top["status"] = "disputed"
            old["status"], old["superseded_by"], old["valid_to"] = "superseded", top["_i"], top["asserted_at"]
        for n in news:
            links.append(("replaces", old["_i"], n["_i"]))
    for old, new in pend:
        if new["status"] in ("current", "inactive"):
            new["status"], new["pending_of"] = "pending-update", old["_i"]
        old["pending_updates"].append(new["_i"])
        links.append(("pending", old["_i"], new["_i"]))
    return facts, links
