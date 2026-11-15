"""Links rescue (GoodQA #16096). Usage: python3 -m phasef.rescue_v5 <stage> ...

Stages:
  reaudit   measure the Opus link auditor's self-agreement on the final v4 dev link set (two more audits: a shuffled
            re-run of linkaudit-v1 and a shuffled run of an equivalent, reworded rubric).
  agree     print pairwise agreement and flips across the three audits (no calls).

All outputs go to phasef-out/rescue/. Calls go through phasef/llm.py with a separate ledger and a cap of 467
(287 used before the rescue + 180 allowed).
"""
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor

from . import links as L, run_pilot as R
from .llm import LLM
from .prompts import LINKAUDIT_HEAD, LINKAUDIT_RECIPE, LINKAUDIT_SYSTEM

OUT = os.path.join(R.OUT, "rescue")
CAP = 467
GOOD = ("replaces", "pending")

# Equivalent rubric, reworded (same five labels, same meaning, different words and order of explanation).
LINKAUDIT_ALT_RECIPE = "linkaudit-v1-alt"
LINKAUDIT_ALT_HEAD = """Each numbered pair below shows two facts that a memory system connected: one taken from an EARLIER chat round and one from a LATER chat round, with both rounds in full. Read the rounds and decide what the LATER fact does to the EARLIER one. Pick exactly one label:
- unrelated: they are about different attributes or different entities
- adds: both are true together (another item, event or detail), nothing is overturned
- restates: LATER says the same value again, perhaps in other words
- pending: LATER announces a plan or intention to change that same attribute
- replaces: LATER gives a new, different value for the same attribute of the same entity, and that new value is the current state

"""


def lemma_v5(t):
    """v4 lemma plus one fix: 'stored' -> 'store' (v4 gave 'stor', so stored/store never matched; dev finding 07741c45)."""
    for suf, rep in (("ies", "y"), ("ing", ""), ("ored", "ore"), ("ed", ""), ("es", ""), ("s", "")):
        if len(t) > 4 and t.endswith(suf):
            return t[: -len(suf)] + rep
    return t


def tokset_v5(f, fix=True):
    import re
    from .supersede_v2 import FILLER
    text = " ".join([f["slot"].replace("_", " ").replace(".", " "), f["predicate"], f["subject"]] + list(f["cue_keys"]))
    lf = lemma_v5 if fix else L.lemma
    return set(lf(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in FILLER and len(t) > 1)


def candidates_v5(facts, cand):
    """As links.candidates (same sim formula), plus: optional lemma fix, and each fact's top `protect` partners survive the
    per-question max_pairs cut first (v4 cut by global sim, so facts with only weak partners lost all of them: 5c40ec5b)."""
    import math
    pool = [f for f in facts if f["modality"] in L.OK_MOD]
    toks = {id(f): tokset_v5(f, cand["fix"]) for f in pool}
    df = {}
    for f in pool:
        for t in toks[id(f)]:
            df[t] = df.get(t, 0) + 1
    n = float(len(pool)) or 1.0
    idf = lambda t: math.log(1 + n / df[t])
    scored, prot = {}, set()
    for a in pool:
        mine = []
        for b in pool:
            if b["session_id"] == a["session_id"] and b["session_key"] == a["session_key"]:
                continue
            inter = toks[id(a)] & toks[id(b)]
            if not inter:
                continue
            s = sum(idf(t) for t in inter) / sum(idf(t) for t in toks[id(a)] | toks[id(b)])
            if s >= cand["theta"]:
                mine.append((s, b))
        mine.sort(key=lambda x: (-x[0], L.fid(x[1])))
        for r, (s, b) in enumerate(mine[: cand["per_fact"]]):
            k = tuple(sorted((L.fid(a), L.fid(b))))
            scored[k] = (s, a, b)
            if r < cand["protect"]:
                prot.add(k)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1][0], kv[0]))
    keep = [kv for kv in ranked if kv[0] in prot][: cand["max_pairs"]]
    have = set(k for k, _ in keep)
    for kv in ranked:
        if len(keep) >= cand["max_pairs"]:
            break
        if kv[0] not in have:
            keep.append(kv)
            have.add(kv[0])
    keep.sort(key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for k, (s, a, b) in keep:
        old, new = sorted((a, b), key=L.order_key)
        out.append({"pair": k, "sim": s, "old": old, "new": new})
    return out


CAND_V5 = {"theta": 0.12, "per_fact": 5, "max_pairs": 40, "protect": 0, "fix": True}  # iteration 1
CAND_WIDE = {"theta": 0.10, "per_fact": 5, "max_pairs": 50, "protect": 1, "fix": True}  # iteration 2: each fact's best partner survives the cut

# Stage 2: attribute-explicit verifier. Every v4 false link was two related but different attributes read as one
# (office vs home location, work vs home zip, relationship vs employer, one video plan vs another). The v4.1 output
# format asks for ONE attribute name, which hides that mismatch; this prompt makes each side name its own entity and
# attribute and answer the identity questions before it may say replaces or pending. 'unsure' is an explicit abstain.
VERIFY_RECIPE = "link-v5-verify"
VERIFY_SYSTEM = "You are a careful analyst of a user's memory records. Follow the output format exactly."
VERIFY_HEAD = """A memory system proposes that the LATER fact below changes the EARLIER fact. Each pair shows the two facts, their quotes, the full chat rounds they came from and the session dates. For each pair, answer these questions in order:

E1: the entity the EARLIER fact is about, and its attribute (for example: user / home city; Rachel / relationship to user; user / number of bikes owned).
E2: the entity and attribute of the LATER fact, named the same way, from its own round (do not copy E1 unless it truly is the same).
same_entity: yes or no.
same_attribute: yes only if it is literally the same attribute, not merely related. A workplace and a home are different attributes. A relationship and an employer are different attributes. Two different plans, purchases, trips, videos or projects are different attributes.
both_true: yes if both values can hold at the same time for that attribute; no if the LATER value means the EARLIER value no longer holds.
label: replaces (same_entity yes, same_attribute yes, both_true no, LATER states the new current value); pending (same_entity yes, same_attribute yes, LATER is a plan or intention to change the EARLIER value, not yet done); none (anything else); unsure (the rounds do not settle it).

Output exactly one line per pair: '<number>: <label> | E1=<entity / attribute>; E2=<entity / attribute>; same_entity=<yes|no>; same_attribute=<yes|no>; both_true=<yes|no>'. Nothing else.

"""


def build_verify_prompt(pairs):
    body = "".join(L.LINK_TEMPLATE_PAIR.format(i=i, d1=p["old"]["asserted_at"].date(), s1=L.stmt(p["old"]), q1=p["old"].get("quote", ""), r1=L.round_text(p["by"], p["old"]),
                                               d2=p["new"]["asserted_at"].date(), s2=L.stmt(p["new"]), q2=p["new"].get("quote", ""), r2=L.round_text(p["by"], p["new"]))
                   for i, p in enumerate(pairs, 1))
    return VERIFY_HEAD + body + "\nNow output one line per pair."


def parse_verify(text, n):
    """Returns per pair (label, fields). A link is asserted only when label and fields agree (checked in code, not trusted)."""
    import re
    got = {}
    for m in re.finditer(r"^\s*(\d+)\s*[:.)]\s*(replaces|pending|none|unsure)\b[ \t]*\|?[ \t]*(.*)$", text, re.I | re.M):
        fields = dict((k.strip().lower(), v.strip().lower()) for k, v in re.findall(r"(same_entity|same_attribute|both_true)\s*=\s*(yes|no)", m.group(3), re.I))
        got.setdefault(int(m.group(1)), (m.group(2).lower(), fields, m.group(3).strip()))
    missing = [i for i in range(1, n + 1) if i not in got]
    if missing:
        raise ValueError("missing pairs %s in %r" % (missing, text[:200]))
    return [got[i] for i in range(1, n + 1)]


def verified_label(label, fields):
    """Code-side consistency: replaces needs same entity, same attribute, not both true; pending needs same entity and attribute."""
    if label not in ("replaces", "pending"):
        return "none" if label == "none" else "unsure"
    if fields.get("same_entity") != "yes" or fields.get("same_attribute") != "yes":
        return "none"
    if label == "replaces" and fields.get("both_true") != "no":
        return "none"
    return label


def llm(recipe):
    os.makedirs(R.NEUTRAL_CWD, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    return LLM(R.CACHE, os.path.join(OUT, "ledger.jsonl"), cap=CAP, concurrency=3, recipe=recipe, cwd=R.NEUTRAL_CWD)


def build_alt_audit_prompt(items):
    body = "".join(L.LINKAUDIT_PAIR.format(i=i, d1=x["d1"], s1=x["s1"], r1=x["r1"], d2=x["d2"], s2=x["s2"], r2=x["r2"]) for i, x in enumerate(items, 1))
    return LINKAUDIT_ALT_HEAD + body + "\nNow output one line per pair: '<number>: <label>'."


def run_audit(items, seed, alt, model="opus", batch=10, tag="reaudit"):
    """Audit items in a seeded shuffled order; returns labels aligned with the input order."""
    order = list(range(len(items)))
    random.Random(seed).shuffle(order)
    recipe = LINKAUDIT_ALT_RECIPE if alt else LINKAUDIT_RECIPE
    m = llm(recipe)
    chunks = [order[i:i + batch] for i in range(0, len(order), batch)]

    def one(ch):
        its = [items[j] for j in ch]
        p = build_alt_audit_prompt(its) if alt else L.build_audit_prompt(its, None)
        c = m.call(p, LINKAUDIT_SYSTEM, model, "RESCUE-AUDIT", its[0]["qid"], tag)
        return ch, L.parse_labels(c["result"], len(ch)), c
    labels, tin, tout, fresh = [None] * len(items), 0, 0, 0
    with ThreadPoolExecutor(3) as ex:
        for ch, labs, c in ex.map(one, chunks):
            tin += c["in_tokens"] + c["cache_creation"] + c["cache_read"]
            tout += c["out_tokens"]
            fresh += 0 if c.get("cached") else 1
            for j, lab in zip(ch, labs):
                labels[j] = lab
    return labels, {"calls": len(chunks), "fresh_calls": fresh, "in": tin, "out": tout}


def stage_reaudit():
    src = json.load(open(os.path.join(OUT, "v4_final_links.json")))
    items = src["items"]
    a0 = [x["audit"] for x in json.load(open(os.path.join(R.OUT, "linkaudit_dev.json")))["audited"]]
    a1, u1 = run_audit(items, 1, alt=False)
    a2, u2 = run_audit(items, 2, alt=True)
    json.dump({"A0_original": a0, "A1_shuffled": a1, "A2_reworded_shuffled": a2, "usage": [u1, u2]},
              open(os.path.join(OUT, "reaudit_v4final.json"), "w"), indent=1)
    print("usage", u1, u2)
    stage_agree()


def binary(lab):
    return lab in GOOD


def stage_agree():
    d = json.load(open(os.path.join(OUT, "reaudit_v4final.json")))
    src = json.load(open(os.path.join(OUT, "v4_final_links.json")))["items"]
    mine = json.load(open(os.path.join(OUT, "my_labels_v4final.json")))["labels"]
    runs = [("A0", d["A0_original"]), ("A1", d["A1_shuffled"]), ("A2", d["A2_reworded_shuffled"])]
    n = len(src)
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            (na, a), (nb, b) = runs[i], runs[j]
            ex = sum(1 for x, y in zip(a, b) if x == y)
            bi = sum(1 for x, y in zip(a, b) if binary(x) == binary(y))
            print("%s vs %s: exact-label agreement %d/%d, precise/not agreement %d/%d" % (na, nb, ex, n, bi, n))
    for name, a in runs:
        print("%s precision %d/%d" % (name, sum(1 for x in a if binary(x)), n))
    print("links whose precise/not verdict flips across audits, or that any audit rejects:")
    for k in range(n):
        labs = [a[k] for _, a in runs]
        if len(set(binary(x) for x in labs)) > 1 or not all(binary(x) for x in labs):
            x = src[k]
            print("  #%d %s %s | %s => %s | mine: %s" % (k, x["qid"], labs, x["s1"][:55], x["s2"][:55], mine[str(k)][0]))


# ---------------- v5 dev pipeline ----------------
ITER = "it1"
VERIFY_MODEL = "sonnet"
PACK = False
S1_TAG = "link-v4.1/sonnet"


def dec_path():
    return os.path.join(OUT, "decisions_dev.json")


def load_dec():
    if os.path.exists(dec_path()):
        return json.load(open(dec_path()))
    return json.load(open(os.path.join(R.OUT, "decisions_dev.json")))  # start from the owner's cached decisions (read only)


def save_dec(dec):
    tmp = dec_path() + ".tmp"
    json.dump(dec, open(tmp, "w"))
    os.replace(tmp, dec_path())


def pk(pair, tag):
    return "%s|%s|%s" % (pair[0], pair[1], tag)


def qdata():
    from . import run_v4 as V4
    V4.configure("dev")
    S, _ = V4.V3.gate()
    per = V4.per_facts()
    out = {}
    for q in V4.V3.ids():
        facts, by = V4.V3.question_facts(S, q, per)
        out[q] = (facts, by, candidates_v5(facts, CAND_V5))
    return S, out


def stage_candidates():
    S, qd = qdata()
    dec = load_dec()
    tot = sum(len(c) for _, _, c in qd.values())
    new = sum(1 for _, _, cs in qd.values() for c in cs if pk(c["pair"], S1_TAG) not in dec)
    print("v5 candidates", tot, "without a stage-1 decision", new, CAND_V5)


def stage_judge1(batch=15):
    """Stage 1 = the v4.1 Sonnet judge, unchanged (recipe link-v4.1); only pairs without a cached decision are sent."""
    from .prompts import LINK_RECIPE, LINK_SYSTEM
    assert LINK_RECIPE + "/sonnet" == S1_TAG
    S, qd = qdata()
    dec = load_dec()
    m = llm(LINK_RECIPE)
    jobs = []
    if PACK:  # iteration 2: new pairs packed across questions in pair-id order (fewer calls; each pair carries its own rounds)
        todo = sorted([dict(c, by=by, qid=q) for q, (facts, by, cands) in qd.items() for c in cands if pk(c["pair"], S1_TAG) not in dec], key=lambda c: c["pair"])
        for i in range(0, len(todo), batch):
            jobs.append((todo[i]["qid"], None, todo[i:i + batch]))
    else:
        for q, (facts, by, cands) in qd.items():
            todo = sorted([c for c in cands if pk(c["pair"], S1_TAG) not in dec], key=lambda c: c["pair"])
            for i in range(0, len(todo), batch):
                jobs.append((q, by, todo[i:i + batch]))
    print("stage-1 pairs", sum(len(j[2]) for j in jobs), "calls", len(jobs))

    def one(job):
        q, by, ps = job
        c = m.call(L.build_link_prompt(ps, by), LINK_SYSTEM, "sonnet", "RESCUE-LINK", q, "linkjudge")
        return ps, L.parse_labels(c["result"], len(ps), True), c
    with ThreadPoolExecutor(3) as ex:
        for ps, labs, c in ex.map(one, jobs):
            for p, (lab, why) in zip(ps, labs):
                dec[pk(p["pair"], S1_TAG)] = {"label": lab, "reason": why}
    save_dec(dec)


def vtag():
    return VERIFY_RECIPE + "/" + VERIFY_MODEL


def stage_verify(batch=12):
    """Stage 2 on every stage-1 positive; batches in pair-id order (deterministic), one call per batch."""
    S, qd = qdata()
    dec = load_dec()
    todo = []
    for q, (facts, by, cands) in qd.items():
        for c in cands:
            if dec.get(pk(c["pair"], S1_TAG), {}).get("label") in GOOD and pk(c["pair"], vtag()) not in dec:
                todo.append(dict(c, by=by, qid=q))
    todo.sort(key=lambda c: c["pair"])
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    print("stage-2 pairs", len(todo), "calls", len(chunks), "model", VERIFY_MODEL)
    m = llm(VERIFY_RECIPE)
    usage = {"in": 0, "out": 0, "calls": 0}

    def one(ch):
        c = m.call(build_verify_prompt(ch), VERIFY_SYSTEM, VERIFY_MODEL, "RESCUE-VERIFY", ch[0]["qid"], "verify")
        return ch, parse_verify(c["result"], len(ch)), c
    with ThreadPoolExecutor(3) as ex:
        for ch, labs, c in ex.map(one, chunks):
            usage["in"] += c["in_tokens"] + c["cache_creation"] + c["cache_read"]
            usage["out"] += c["out_tokens"]
            usage["calls"] += 1
            for p, (lab, fields, raw) in zip(ch, labs):
                dec[pk(p["pair"], vtag())] = {"label": lab, "fields": fields, "raw": raw, "final": verified_label(lab, fields)}
    save_dec(dec)
    print("stage-2 usage", usage)


def decisions_v5(cands, dec=None):
    dec = dec if dec is not None else load_dec()
    out = {}
    for c in cands:
        d = dec.get(pk(c["pair"], S1_TAG))
        if not d:
            continue
        lab = d["label"]
        if lab in GOOD:
            v = dec.get(pk(c["pair"], vtag()))
            lab = v["final"] if v else "unverified"
        out[c["pair"]] = lab
    return out


def x_linked(S, q, facts, cands, dec):
    from . import run_v3 as V3
    X, cov = V3.gold_value_fact(S[q], facts)
    if X is None:
        return None, None
    fx = L.fid(X)
    ids = {L.fid(f): f for f in facts}
    gold = set(S[q]["answer_session_ids"])
    for p, lab in sorted(decisions_v5(cands, dec).items()):
        if fx in p and lab in GOOD:
            o = ids[p[0] if p[1] == fx else p[1]]
            if o["session_id"] in gold and o["session_key"] != X["session_key"]:
                return True, p
    return False, None


def audit_items(qd):
    dec = load_dec()
    items = []
    for q, (facts, by, cands) in qd.items():
        d = decisions_v5(cands, dec)
        for c in cands:
            if d.get(c["pair"]) in GOOD:
                o, n = c["old"], c["new"]
                items.append({"qid": q, "pair": list(c["pair"]), "judge": d[c["pair"]], "d1": str(o["asserted_at"].date()), "s1": L.stmt(o), "r1": L.round_text(by, o),
                              "d2": str(n["asserted_at"].date()), "s2": L.stmt(n), "r2": L.round_text(by, n)})
    return items


def audit_path():
    return os.path.join(OUT, "linkaudit_dev_%s.json" % ITER)


def stage_audit(batch=10):
    """GATE3 S1/S2: one linkaudit-v1 Opus audit, original (question, candidate) order, batches of 10."""
    S, qd = qdata()
    items = audit_items(qd)
    total = len(items)
    if total > 80:
        items = random.Random(16096).sample(items, 80)
    import glob
    prior = {}  # one audit per link: a link already audited in an earlier rescue iteration keeps that label
    for path in sorted(glob.glob(os.path.join(OUT, "linkaudit_dev_it*.json"))):
        if path != audit_path():
            for x in json.load(open(path))["audited"]:
                prior[tuple(x["pair"])] = x["audit"]
    res = [dict(x, audit=prior[tuple(x["pair"])], reused=True) for x in items if tuple(x["pair"]) in prior]
    items = [x for x in items if tuple(x["pair"]) not in prior]
    print("audit: reused", len(res), "new", len(items))
    m = llm(LINKAUDIT_RECIPE)
    chunks = [items[i:i + batch] for i in range(0, len(items), batch)]

    def one(ch):
        c = m.call(L.build_audit_prompt(ch, None), LINKAUDIT_SYSTEM, "opus", "RESCUE-AUDIT", ch[0]["qid"], "audit")
        return ch, L.parse_labels(c["result"], len(ch)), c
    with ThreadPoolExecutor(3) as ex:
        for ch, labs, c in ex.map(one, chunks):
            res += [dict(x, audit=l) for x, l in zip(ch, labs)]
    json.dump({"iter": ITER, "verify_model": VERIFY_MODEL, "total_links": total, "audited": res}, open(audit_path(), "w"), indent=1)
    stage_report()


def wilson(k, n):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval"))
    from emlab.metrics import wilson_interval
    return "%d/%d [%.2f, %.2f]" % ((k, n) + wilson_interval(k, n))


def stage_report():
    S, qd = qdata()
    dec = load_dec()
    aud = json.load(open(audit_path()))["audited"] if os.path.exists(audit_path()) else []
    amap = {tuple(x["pair"]): x["audit"] for x in aud}
    from collections import Counter
    labs = Counter()
    s3, s3c, miss = 0, 0, []
    for q, (facts, by, cands) in qd.items():
        d = decisions_v5(cands, dec)
        labs.update(d.values())
        if q.endswith("_abs"):
            continue
        hit, p = x_linked(S, q, facts, cands, dec)
        if hit:
            s3 += 1
            s3c += 1 if amap.get(p) in GOOD else 0
        else:
            miss.append(q)
    n_aud = len(aud)
    prec = sum(1 for x in aud if x["audit"] in GOOD)
    harm = sum(1 for x in aud if x["audit"] in ("unrelated", "restates"))
    print("ITER", ITER, "verifier", VERIFY_MODEL, "| pairs", sum(len(v[2]) for v in qd.values()), "labels", dict(labs))
    if n_aud:
        print("S1 precision %s pass %s (audited %d, need >= 20)" % (wilson(prec, n_aud), prec / float(n_aud) >= 0.90 and n_aud >= 20, n_aud))
        print("S2 harmful %s rate %.3f pass %s" % (wilson(harm, n_aud), harm / float(n_aud), harm / float(n_aud) <= 0.05))
        for x in aud:
            if x["audit"] not in GOOD:
                print("   FALSE", x["qid"], x["judge"], "->", x["audit"], "|", x["s1"][:60], "=>", x["s2"][:60])
    print("S3 gold update linked %s pass %s misses %s | S3-correct (audited precise) %d" % (wilson(s3, 15), s3 >= 13, miss, s3c))


def stage_ordercheck(batch=12):
    """Re-run stage 2 on every stage-1 positive with a seeded shuffled batching (seed 7) and report verdict flips (order independence)."""
    S, qd = qdata()
    dec = load_dec()
    todo = [dict(c, by=by, qid=q) for q, (facts, by, cands) in qd.items() for c in cands if dec.get(pk(c["pair"], S1_TAG), {}).get("label") in GOOD]
    todo.sort(key=lambda c: c["pair"])
    random.Random(7).shuffle(todo)
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    m = llm(VERIFY_RECIPE)
    flips, same = [], 0

    def one(ch):
        c = m.call(build_verify_prompt(ch), VERIFY_SYSTEM, VERIFY_MODEL, "RESCUE-ORDER", ch[0]["qid"], "ordercheck")
        return ch, parse_verify(c["result"], len(ch))
    with ThreadPoolExecutor(3) as ex:
        for ch, labs in ex.map(one, chunks):
            for p, (lab, fields, raw) in zip(ch, labs):
                a = dec[pk(p["pair"], vtag())]["final"]
                b = verified_label(lab, fields)
                if (a in GOOD) == (b in GOOD):
                    same += 1
                else:
                    flips.append((p["qid"], a, b, L.stmt(p["old"])[:50], L.stmt(p["new"])[:50]))
    json.dump({"same": same, "n": len(todo), "flips": flips}, open(os.path.join(OUT, "ordercheck_%s.json" % ITER), "w"), indent=1)
    print("order check: link/no-link verdict unchanged on %d of %d stage-1 positives" % (same, len(todo)))
    for f in flips:
        print("  FLIP", f)


# ---------------- diagnostics (not links; never counted toward S1-S3) ----------------
def stage_d1():
    """D1: linkaudit-v1 on X of 5c40ec5b against the two Alex facts of the earlier gold session."""
    S, qd = qdata()
    facts, by, _ = qd["5c40ec5b"]
    ids = {L.fid(f): f for f in facts}
    X = ids["8413bda91be7"]
    items = []
    for oid in ("dfb26b04e911", "f23e5b4449f3"):
        o = ids[oid]
        items.append({"qid": "5c40ec5b", "d1": str(o["asserted_at"].date()), "s1": L.stmt(o), "r1": L.round_text(by, o),
                      "d2": str(X["asserted_at"].date()), "s2": L.stmt(X), "r2": L.round_text(by, X)})
    m = llm(LINKAUDIT_RECIPE)
    c = m.call(L.build_audit_prompt(items, None), LINKAUDIT_SYSTEM, "opus", "RESCUE-D1", "5c40ec5b", "diag")
    labs = L.parse_labels(c["result"], len(items))
    json.dump([dict(x, audit=l) for x, l in zip(items, labs)], open(os.path.join(OUT, "d1_5c40ec5b.json"), "w"), indent=1)
    for x, l in zip(items, labs):
        print("D1", l, "|", x["s1"][:70], "=>", x["s2"][:60])


RES_NOLABEL = os.path.join(OUT, "results_nolabel.jsonl")


def context_nolabel(question, cues, qdate, facts, by):
    """V3.build_context_v3 with the link label replaced by the fact's modality (dated facts only); same facts, same order."""
    from . import data, run_v3 as V3
    vis = [f for f in facts if f["modality"] != "question" and f["asserted_at"].date() <= qdate.date()]
    top = V3.score_v2(question, cues, vis)
    lines = ["Memory facts, most relevant first. Each shows the date of the session that stated it and whether it was stated as a fact, a decision or a plan:"]
    for f in top:
        lines.append("- [%s] stated %s%s | %s: %s = %s (session %s round %d; quote: \"%s\")" % (
            f["modality"].upper(), f["asserted_at"].date(), ", event %s" % f["event_time"] if f.get("event_time") else "", f["subject"], f["predicate"], f["object"], f["session_id"], f["round"], f["quote"]))
    cited, seen = [], set()
    for f in top:
        k = (f["session_id"], f["round"])
        if k not in seen and len(cited) < 20:
            seen.add(k)
            cited.append(k)
    cited.sort(key=lambda k: (data.parse_date(by[k[0]][0]), k[1]))
    lines.append("\nSource excerpts (verbatim rounds cited by the facts):")
    for sid, ri in cited:
        d, turns = by[sid]
        r = next((x for x in data.rounds_of(sid, turns, d) if x.index == ri), None)
        if r:
            lines.append("### Session %s | date: %s | round %d\n%s" % (sid, d, ri, r.text))
    return "\n".join(lines), top


def stage_d2read():
    from . import data, run_v3 as V3
    from .prompts import READER_SYSTEM, READER_V12_RECIPE
    S, _ = V3.gate()  # dev defaults: gate16 files, v2/v3 facts, gate_cues.json
    per, canon, cues = V3.load_facts(), json.load(open(V3.CANON)), json.load(open(V3.CUES))
    base = {r["qid"]: r for r in V3.raw_lines(os.path.join(R.OUT, "results_v4.jsonl")) if r["arm"] == "FACTS3" and "shown" in r}
    m = llm(READER_V12_RECIPE)

    def one(q):
        facts, by, _ = V3.resolved(S, q, per, canon)  # same fact list and indices as FACTS3; labels are not used below
        ctx, top = context_nolabel(S[q]["question"], cues[q], data.parse_date(S[q]["question_date"]), facts, by)
        same = [f["_i"] for f in top] == base[q]["shown"]
        c = m.call(V3.read_prompt(ctx, S[q]), READER_SYSTEM, "sonnet", "NOLABEL", q, "read")
        return {"arm": "NOLABEL", "qid": q, "hypothesis": c["result"], "same_facts_as_facts3": same,
                "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out_tokens": c["out_tokens"]}
    with ThreadPoolExecutor(3) as ex:
        rows = list(ex.map(one, V3.ids()))
    with open(RES_NOLABEL, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print("same facts as FACTS3:", sum(r["same_facts_as_facts3"] for r in rows), "of", len(rows))


def stage_d2judge(batch=6):
    from . import run_v3 as V3
    from .judge import build_batch_prompt, parse_batch
    from .prompts import JUDGE_RECIPE, JUDGE_SYSTEM
    S, _ = V3.gate()
    rows = [json.loads(l) for l in open(RES_NOLABEL)]
    rows.sort(key=lambda r: r["qid"])
    chunks = [rows[i:i + batch] for i in range(0, len(rows), batch)]
    m = llm(JUDGE_RECIPE)

    def one(ch):
        items = [(S[r["qid"]]["question_type"], S[r["qid"]]["question"], str(S[r["qid"]]["answer"]), r["hypothesis"], r["qid"].endswith("_abs")) for r in ch]
        c = m.call(build_batch_prompt(items), JUDGE_SYSTEM, "opus", "RESCUE-JUDGE", "gNOLABEL" + ch[0]["qid"], "judge")
        return ch, parse_batch(c["result"], len(ch))
    out = []
    with ThreadPoolExecutor(3) as ex:
        for ch, labs in ex.map(one, chunks):
            out += [dict(r, label=l) for r, l in zip(ch, labs)]
    with open(RES_NOLABEL, "w") as fh:
        for r in out:
            fh.write(json.dumps(r) + "\n")
    base = {}
    for r in V3.raw_lines(os.path.join(R.OUT, "results_v4.jsonl")):
        base.setdefault((r["arm"], r["qid"]), {}).update(r)
    k = sum(1 for r in out if r["label"])
    print("NOLABEL correct", wilson(k, len(out)), "| FACTS3", sum(1 for q in V3.ids() if base[("FACTS3", q)]["label"]), "| ORACLE3", sum(1 for q in V3.ids() if base[("ORACLE3", q)]["label"]))
    print("NOLABEL wrong:", [r["qid"] for r in out if not r["label"]])


if __name__ == "__main__":
    args = sys.argv[1:]
    for a in list(args):
        if a.startswith("--iter="):
            ITER = a.split("=", 1)[1]
            args.remove(a)
        elif a == "--wide":
            CAND_V5 = CAND_WIDE
            PACK = True
            args.remove(a)
        elif a.startswith("--verify-model="):
            VERIFY_MODEL = a.split("=", 1)[1]
            assert VERIFY_MODEL in ("sonnet", "opus")
            args.remove(a)
    for st in args:
        globals()["stage_" + st]()
