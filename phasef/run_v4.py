"""Phase F v4 (pairwise content link decisions). Usage: python3 -m phasef.run_v4 <set: dev|dev2> <stage> ...
Stages: prepare extract expand oracle candidates judgelinks links audit read judge report   (see GATE2.md)"""
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor

from . import data, links as L, run_pilot as R, run_v3 as V3
from .llm import LLM
from .prompts import LINK_RECIPE, LINK_SYSTEM, LINKAUDIT_RECIPE, LINKAUDIT_SYSTEM, READER_SYSTEM, READER_V12_RECIPE

OUT = R.OUT
CEILING = 520
NAME = "dev"
SAMPLE = {"dev": 60, "dev2": 80}


def configure(name):
    global NAME
    NAME = name
    if name == "dev2":
        V3.KU_IDS = os.path.join(OUT, "dev2_ids.json")
        V3.S16, V3.O16 = os.path.join(OUT, "dev2_s.json"), os.path.join(OUT, "dev2_oracle.json")
        V3.FACTS_V2, V3.FACTS_V3 = os.path.join(OUT, "none_v2.jsonl"), os.path.join(OUT, "facts_dev2_raw.jsonl")
        V3.RES, V3.CUES = os.path.join(OUT, "results_dev2.jsonl"), os.path.join(OUT, "dev2_cues.json")
    else:
        V3.RES = os.path.join(OUT, "results_v4.jsonl")  # dev answers from the v3 gate run (reader-v1.2, ORACLE3)
    V3.llm = llm


def llm(recipe):
    os.makedirs(R.NEUTRAL_CWD, exist_ok=True)
    return LLM(R.CACHE, os.path.join(OUT, "ledger_v2.jsonl"), cap=CEILING, concurrency=3, recipe=recipe, cwd=R.NEUTRAL_CWD)


def dec_path():
    return os.path.join(OUT, "decisions_%s.json" % NAME)


def load_dec():
    return json.load(open(dec_path())) if os.path.exists(dec_path()) else {}


def per_facts():
    per = {}
    for path in ([V3.FACTS_V2, V3.FACTS_V3] if NAME == "dev" else [V3.FACTS_V3]):
        for f in V3.raw_lines(path):
            from datetime import datetime
            f["asserted_at"], f["valid_from"] = datetime.fromisoformat(f["asserted_at"]), datetime.fromisoformat(f["valid_from"])
            per.setdefault(f["session_key"], []).append(f)
    return per


def qdata():
    S, _ = V3.gate()
    per = per_facts()
    out = {}
    for q in V3.ids():
        facts, by = V3.question_facts(S, q, per)
        out[q] = (facts, by, L.candidates(facts))
    return S, out


def stage_prepare():
    for src, dst in ((os.path.join(R.DATA, "longmemeval_s_cleaned.json"), V3.S16), (os.path.join(R.DATA, "longmemeval_oracle.json"), V3.O16)):
        recs = data.extract_records(src, V3.ids())
        assert len(recs) == len(V3.ids())
        json.dump(recs, open(dst, "w"))
    print("prepared", len(V3.ids()))


def stage_extract():
    V3.stage_extract()


def stage_expand():
    V3.stage_expand()


def stage_oracle():
    V3.stage_oracle()


def gold_pair(rec, facts):
    """(X, Y) per GATE2.md: X = best answer-coverage fact in the gold sessions, Y = most similar fact in a different gold session."""
    X, cov = V3.gold_value_fact(rec, facts)
    if X is None:
        return None, None
    gold = set(rec["answer_session_ids"])
    import math
    pool = [f for f in facts if f["modality"] in L.OK_MOD]
    toks = {id(f): L.tokset(f) for f in pool}
    df = {}
    for f in pool:
        for t in toks[id(f)]:
            df[t] = df.get(t, 0) + 1
    idf = lambda t: math.log(1 + len(pool) / float(df[t]))
    if id(X) not in toks:
        toks[id(X)] = L.tokset(X)
    cands = [f for f in pool if f["session_id"] in gold and f["session_key"] != X["session_key"]]
    if not cands:
        return None, None

    def sim(a, b):
        u = toks[id(a)] | toks[id(b)]
        return sum(idf(t) for t in toks[id(a)] & toks[id(b)] if t in df) / (sum(idf(t) for t in u if t in df) or 1.0)
    Y = max(cands, key=lambda f: (sim(X, f), L.fid(f)))
    return X, Y


def stage_candidates():
    S, qd = qdata()
    tot, rec_n, n_ok = 0, 0, 0
    for q, (facts, by, cands) in qd.items():
        A, B = gold_pair(S[q], facts)
        have = A is not None and B is not None
        inc = have and tuple(sorted((L.fid(A), L.fid(B)))) in {c["pair"] for c in cands}
        tot += len(cands)
        n_ok += 1 if have else 0
        rec_n += 1 if inc else 0
        print(q, "facts", len(facts), "pairs", len(cands), "gold pair", "none" if not have else ("in candidates" if inc else "MISSED"))
    print("total pairs", tot, "gold pair identified %d/%d, in candidates %d/%d" % (n_ok, len(qd), rec_n, len(qd)), L.CAND)


def stage_judgelinks(batch=15):
    S, qd = qdata()
    dec = load_dec()
    m = llm(LINK_RECIPE)
    tag = LINK_RECIPE + "/sonnet"
    jobs = []
    for q, (facts, by, cands) in qd.items():
        todo = [c for c in cands if "%s|%s|%s" % (c["pair"][0], c["pair"][1], tag) not in dec]
        todo.sort(key=lambda c: c["pair"])
        for i in range(0, len(todo), batch):
            jobs.append((q, by, todo[i:i + batch]))
    print("pairs to judge", sum(len(j[2]) for j in jobs), "calls", len(jobs))

    def one(job):
        q, by, ps = job
        c = m.call(L.build_link_prompt(ps, by), LINK_SYSTEM, "sonnet", "LINK", q, "linkjudge")
        return ps, L.parse_labels(c["result"], len(ps), True), c
    tin = tout = 0
    with ThreadPoolExecutor(3) as ex:
        for ps, labs, c in ex.map(one, jobs):
            tin += c["in_tokens"] + c["cache_creation"] + c["cache_read"]
            tout += c["out_tokens"]
            for p, (lab, why) in zip(ps, labs):
                dec["%s|%s|%s" % (p["pair"][0], p["pair"][1], tag)] = {"label": lab, "reason": why}
    json.dump(dec, open(dec_path(), "w"))
    print("judge tokens in", tin, "out", tout)


VERIFY = True
VTAG = LINK_RECIPE + "+verify/sonnet"


def decisions_for(cands):
    dec = load_dec()
    tag = LINK_RECIPE + "/sonnet"
    out = {}
    for c in cands:
        d = dec.get("%s|%s|%s" % (c["pair"][0], c["pair"][1], tag))
        if d:
            lab = d["label"]
            if VERIFY and lab in ("replaces", "pending"):
                v = dec.get("%s|%s|%s" % (c["pair"][0], c["pair"][1], VTAG))
                if not v or v["label"] not in ("replaces", "pending"):
                    lab = "unverified"  # a link needs a second, independently shuffled judge call to agree
            out[c["pair"]] = lab
    return out


def stage_verify(batch=10):
    S, qd = qdata()
    dec = load_dec()
    m = llm(LINK_RECIPE + "-verify")
    todo = []
    for q, (facts, by, cands) in qd.items():
        for c in cands:
            k = "%s|%s|%s" % (c["pair"][0], c["pair"][1], LINK_RECIPE + "/sonnet")
            if dec.get(k, {}).get("label") in ("replaces", "pending") and "%s|%s|%s" % (c["pair"][0], c["pair"][1], VTAG) not in dec:
                todo.append(dict(c, by=by, qid=q))
    random.Random(16096).shuffle(todo)  # different neighbours than in the first judging call
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]

    def one(ch):
        c = m.call(L.build_link_prompt(ch, None), LINK_SYSTEM, "sonnet", "LINKVERIFY", ch[0]["qid"], "verify")
        return ch, L.parse_labels(c["result"], len(ch), True), c
    with ThreadPoolExecutor(3) as ex:
        for ch, labs, c in ex.map(one, chunks):
            for p, (lab, why) in zip(ch, labs):
                dec["%s|%s|%s" % (p["pair"][0], p["pair"][1], VTAG)] = {"label": lab, "reason": why}
    json.dump(dec, open(dec_path(), "w"))
    print("verified", len(todo), "calls", len(chunks))


def resolved(S, q, facts, by, cands):
    facts = [dict(f) for f in facts]
    return L.derive_status(facts, decisions_for(cands))


def x_linked(S, q, facts, cands):
    """GATE2 G5: the answer fact X has a replaces/pending link to a fact in a different gold session."""
    X, cov = V3.gold_value_fact(S[q], facts)
    if X is None:
        return None
    fx = L.fid(X)
    ids = {L.fid(f): f for f in facts}
    gold = set(S[q]["answer_session_ids"])
    for p, lab in decisions_for(cands).items():
        if fx in p and lab in ("replaces", "pending"):
            o = ids[p[0] if p[1] == fx else p[1]]
            if o["session_id"] in gold and o["session_key"] != X["session_key"]:
                return True
    return False


def stage_links():
    S, qd = qdata()
    from collections import Counter
    labs, link_n, hit, have = Counter(), 0, 0, 0
    for q, (facts, by, cands) in qd.items():
        fs, links = resolved(S, q, facts, by, cands)
        g = x_linked(S, q, fs, cands)
        d = decisions_for(cands)
        have += 1 if g is not None else 0
        hit += 1 if g else 0
        link_n += len(links)
        labs.update(d.values())
        print(q, "pairs", len(cands), "links", len(links), "gold X linked", g, dict(Counter(d.values())))
    print("pairs", sum(len(v[2]) for v in qd.values()), "links", link_n, "gold X linked %d of %d with an answer fact (non-abstention questions: %d)" % (hit, have, sum(1 for q in qd if not q.endswith("_abs"))), dict(labs))


def audit_items(qd, S):
    items = []
    for q, (facts, by, cands) in qd.items():
        d = decisions_for(cands)
        for c in cands:
            if d.get(c["pair"]) in ("replaces", "pending"):
                o, n = c["old"], c["new"]
                items.append({"qid": q, "judge": d[c["pair"]], "d1": str(o["asserted_at"].date()), "s1": L.stmt(o), "r1": L.round_text(by, o),
                              "d2": str(n["asserted_at"].date()), "s2": L.stmt(n), "r2": L.round_text(by, n)})
    return items


def stage_audit(batch=10):
    S, qd = qdata()
    items = audit_items(qd, S)
    total = len(items)
    if total > SAMPLE[NAME]:
        items = random.Random(16096).sample(items, SAMPLE[NAME])
    m = llm(LINKAUDIT_RECIPE)
    chunks = [items[i:i + batch] for i in range(0, len(items), batch)]

    def one(ch):
        c = m.call(L.build_audit_prompt(ch, None), LINKAUDIT_SYSTEM, "opus", "LINKAUDIT", ch[0]["qid"], "audit")
        return ch, L.parse_labels(c["result"], len(ch)), c
    res, tin, tout = [], 0, 0
    with ThreadPoolExecutor(3) as ex:
        for ch, labs, c in ex.map(one, chunks):
            tin += c["in_tokens"] + c["cache_creation"] + c["cache_read"]
            tout += c["out_tokens"]
            res += [dict(x, audit=l) for x, l in zip(ch, labs)]
    json.dump({"total_links": total, "audited": res}, open(os.path.join(OUT, "linkaudit_%s.json" % NAME), "w"))
    from emlab.metrics import wilson_interval
    ok = sum(1 for x in res if x["audit"] in ("replaces", "pending"))
    rs = sum(1 for x in res if x["audit"] == "restates")
    print("links total %d audited %d; precision %d/%d Wilson95 [%.2f, %.2f]; auditor restates %d; judge-vs-audit labels" % ((total, len(res), ok, len(res)) + wilson_interval(ok, len(res)) + (rs,)),
          {k: sum(1 for x in res if x["audit"] == k) for k in L.LABELS}, "tokens in", tin, "out", tout)
    for x in res:
        if x["audit"] not in ("replaces", "pending"):
            print("  FALSE", x["qid"], x["judge"], "->", x["audit"], "|", x["s1"][:60], "=>", x["s2"][:60])


def stage_read():
    S, qd = qdata()
    cues = json.load(open(V3.CUES))
    m = llm(READER_V12_RECIPE)

    def one(q):
        facts, by, cands = qd[q]
        fs, _ = resolved(S, q, facts, by, cands)
        ctx, top = V3.build_context_v3(S[q]["question"], cues[q], data.parse_date(S[q]["question_date"]), fs, by)
        c = m.call(V3.read_prompt(ctx, S[q]), READER_SYSTEM, "sonnet", "FACTS4", q, "read")
        r = {"arm": "FACTS4", "qid": q, "hypothesis": c["result"], "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out_tokens": c["out_tokens"], "shown": [f["_i"] for f in top]}
        V3.save(r)
        return r
    with ThreadPoolExecutor(3) as ex:
        for r in ex.map(one, V3.ids()):
            print(r["qid"], r["in_tokens"], r["hypothesis"][-90:].replace("\n", " "))


def stage_judge():
    V3.stage_judge()


def stage_report():
    from emlab.metrics import wilson_interval
    S, qd = qdata()
    res = V3.last_results()
    ids = V3.ids()
    n = len(ids)
    pr = lambda k, n_: "%d/%d Wilson95 [%.2f, %.2f]" % ((k, n_) + wilson_interval(k, n_))
    orc = "ORACLE3"
    fk = sum(1 for q in ids if res[("FACTS4", q)]["label"])
    ok = sum(1 for q in ids if res[(orc, q)]["label"])
    g2 = g3 = g5 = 0
    f2, f3, f5 = [], [], []
    for q in ids:
        facts, by, cands = qd[q]
        fs, links = resolved(S, q, facts, by, cands)
        gv, cov = V3.gold_value_fact(S[q], fs)
        shown = set(res[("FACTS4", q)]["shown"])
        if gv is not None and gv["_i"] in shown:
            g2 += 1
            earlier = any(w in S[q]["question"].lower() for w in V3.EARLIER)
            if gv["status"] in ("current", "pending-update") or (earlier and gv["status"] == "superseded"):
                g3 += 1
            else:
                f3.append("%s(%s)" % (q, gv["status"]))
        else:
            f2.append(q)
            f3.append(q)
        if x_linked(S, q, fs, cands):
            g5 += 1
        else:
            f5.append(q)
    aud = json.load(open(os.path.join(OUT, "linkaudit_%s.json" % NAME)))["audited"]
    p = sum(1 for x in aud if x["audit"] in ("replaces", "pending"))
    r6 = sum(1 for x in aud if x["audit"] == "restates")
    print("G1 FACTS4 %s | ORACLE %s | pass %s | failures %s" % (pr(fk, n), pr(ok, n), fk >= 14 and fk >= ok - 1, [q for q in ids if not res[("FACTS4", q)]["label"]]))
    print("G2 %s pass %s failures %s" % (pr(g2, n), g2 >= 14, f2))
    print("G3 %s pass %s failures %s" % (pr(g3, n), g3 >= 13, f3))
    print("G4 %s pass %s" % (pr(p, len(aud)), (p / float(len(aud)) >= 0.9) if aud else False))
    print("G5 %s pass %s failures %s" % (pr(g5, n), g5 >= 12, f5))
    print("G6 restates among audited links %s" % pr(r6, len(aud)))
    print("pairs judged per question", {q: len(qd[q][2]) for q in ids})


if __name__ == "__main__":
    configure(sys.argv[1])
    for st in sys.argv[2:]:
        globals()["stage_" + st]()
