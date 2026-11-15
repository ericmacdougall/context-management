"""Gate 3 on dev2 (GATE3.md sections 4 and 6). One run. Usage: python3 -m phasef.gate3 <stage> ...
Stages in order: extract budget goldpairs expand judge1 verify links oracle read nolabel judge audit report
The v5 recipe is used exactly as frozen at 2b15450 (candidates_v5 + CAND_V5, link-v4.1 stage 1, link-v5-verify stage 2)."""
import json
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import data, links as L, rescue_v5 as R5, run_pilot as R, run_v3 as V3
from .facts_v2 import build_extract_prompt_v2, parse_extraction_v2
from .judge import build_batch_prompt, parse_batch
from .llm import LLM
from .prompts import (EXPAND_RECIPE, EXPAND_SYSTEM, EXPAND_TEMPLATE, EXTRACT_SYSTEM, EXTRACT_V2_RECIPE, JUDGE_RECIPE, JUDGE_SYSTEM, LINK_RECIPE,
                      LINK_SYSTEM, LINKAUDIT_RECIPE, LINKAUDIT_SYSTEM, READER_SYSTEM, READER_V12_RECIPE)

OUT = R5.OUT
CAP = 565  # 345 before gate 3 + 220 allowed by main
GOOD = R5.GOOD
IDS = os.path.join(R.OUT, "dev2_ids.json")
S_PATH, O_PATH = os.path.join(OUT, "dev2_s.json"), os.path.join(OUT, "dev2_oracle.json")
FACTS = os.path.join(OUT, "facts_dev2_raw.jsonl")
DEC = os.path.join(OUT, "decisions_dev2.json")
GOLDP = os.path.join(OUT, "dev2_goldpairs.json")
CUES = os.path.join(OUT, "dev2_cues.json")
RES = os.path.join(OUT, "results_dev2.jsonl")
AUD = os.path.join(OUT, "linkaudit_dev2.json")
TAG = {"A": ("link-v4.1/sonnet", "link-v5-verify/sonnet"), "B": ("link-v4.1/sonnet#B", "link-v5-verify/sonnet#B")}


def llm(recipe):
    os.makedirs(R.NEUTRAL_CWD, exist_ok=True)
    return LLM(R.CACHE, os.path.join(OUT, "ledger.jsonl"), cap=CAP, concurrency=3, recipe=recipe, cwd=R.NEUTRAL_CWD)


def ids():
    return json.load(open(IDS))


def recs():
    return data.load(S_PATH), data.load(O_PATH)


def jload(p, default):
    return json.load(open(p)) if os.path.exists(p) else default


def jsave(p, obj):
    json.dump(obj, open(p + ".tmp", "w"), indent=1)
    os.replace(p + ".tmp", p)


def uniq_sessions(S):
    u = {}
    for q in ids():
        for sid, d, turns in data.sessions_of(S[q]):
            u.setdefault(R._skey(sid, d, turns), (sid, d, turns))
    return u


def stage_extract():
    S, _ = recs()
    u = uniq_sessions(S)
    have = set(f["session_key"] for f in V3.raw_lines(FACTS))
    todo = [k for k in sorted(u) if k not in have]
    batches = [[u[k] for k in todo[i:i + 16]] for i in range(0, len(todo), 16)]
    print("dev2 sessions", len(u), "to extract", len(todo), "calls", len(batches))
    m = llm(EXTRACT_V2_RECIPE)

    def run(ib):
        i, b = ib
        c = m.call(build_extract_prompt_v2(b), EXTRACT_SYSTEM, "sonnet", "GATE3-EXTRACT", "x%d" % i, "extract")
        entries = len(re.findall(r'"session_id":\s*"', c["result"]))
        try:
            per, _ = parse_extraction_v2(c["result"], b)
        except Exception as e:
            per = None
        return i, b, per, entries, V3.strict_ok(c["result"]), c
    stats = {"calls": 0, "in": 0, "out": 0, "bad": []}
    with ThreadPoolExecutor(3) as ex, open(FACTS, "a") as fh:
        for i, b, per, entries, strict, c in ex.map(run, list(enumerate(batches))):
            stats["calls"] += 1
            stats["in"] += c["in_tokens"] + c["cache_creation"] + c["cache_read"]
            stats["out"] += c["out_tokens"]
            if per is None or not strict or entries != len(b):
                stats["bad"].append(i)
            if per is None:
                continue
            for item, fs in zip(b, per):
                for f in fs:
                    g = dict(f)
                    g["asserted_at"], g["valid_from"] = f["asserted_at"].isoformat(), f["valid_from"].isoformat()
                    g["session_key"] = R._skey(*item)
                    fh.write(json.dumps(g) + "\n")
    print("extraction", stats)
    jsave(os.path.join(OUT, "dev2_extract_stats.json"), stats)


def per_facts():
    per = {}
    for f in V3.raw_lines(FACTS):
        f["asserted_at"], f["valid_from"] = datetime.fromisoformat(f["asserted_at"]), datetime.fromisoformat(f["valid_from"])
        per.setdefault(f["session_key"], []).append(f)
    return per


def qdata():
    S, _ = recs()
    per = per_facts()
    out = {}
    for q in ids():
        facts, by = V3.question_facts(S, q, per)
        out[q] = (facts, by, R5.candidates_v5(facts, R5.CAND_V5))
    return S, out


def calls_used():
    return llm("x").calls_used()


def stage_budget():
    S, qd = qdata()
    P = sum(len(c) for _, _, c in qd.values())
    need = 2 * -(-P // 15) + 12
    used = calls_used()
    fixed_left = 0 if os.path.exists(GOLDP) else 16
    fixed_left += 0 if os.path.exists(CUES) else 16
    fixed_left += 48 + 8
    left = CAP - used
    ok = need <= left - fixed_left
    print("P", P, "link judging+audit need", need, "calls left", left, "fixed still to spend", fixed_left, "BUDGET OK" if ok else "SHORTFALL %d" % (need - (left - fixed_left)))
    jsave(os.path.join(OUT, "dev2_budget.json"), {"P": P, "need": need, "left": left, "fixed_left": fixed_left, "ok": ok})
    if not ok:
        sys.exit(2)


GOLD_RECIPE = "goldpair-v1"
GOLD_SYSTEM = "You output only valid JSON, no prose, no code fences."
GOLD_TEMPLATE = """A user asked a memory system a knowledge-update question: the answer depends on a value that changed over time. Below are the question, the correct answer, the full text of the user's chat sessions that contain the evidence, and a numbered list of facts a memory extractor took from those sessions.

Identify which numbered facts state the OLD value (the earlier value that was later changed) and which state the NEW value (the value the correct answer relies on). Name only facts that actually state that value of that attribute. If no fact in the list states the old value, use "none" for old; likewise for new.

Question (asked on {date}): {question}
Correct answer: {answer}

Sessions:
{sessions}

Extracted facts:
{facts}

Output JSON only: {{"old": [numbers] or "none", "new": [numbers] or "none"}}"""


def stage_goldpairs():
    S, qd = qdata()
    m = llm(GOLD_RECIPE)
    got = jload(GOLDP, {})

    def one(q):
        facts, by, _ = qd[q]
        gold = list(dict.fromkeys(S[q]["answer_session_ids"]))
        gs = [s for s in data.chrono(data.sessions_of(S[q])) if s[0] in gold]
        gf = [f for f in facts if f["session_id"] in gold]
        flist = "\n".join("%d. [session %s, %s] %s (quote: \"%s\")" % (i, f["session_id"], f["asserted_at"].date(), L.stmt(f), f.get("quote", "")) for i, f in enumerate(gf, 1))
        p = GOLD_TEMPLATE.format(date=S[q]["question_date"], question=S[q]["question"], answer=S[q]["answer"], sessions=data.render_sessions(gs), facts=flist or "(none)")
        c = m.call(p, GOLD_SYSTEM, "opus", "GATE3-GOLD", q, "goldpair")
        rec = {"raw": c["result"], "n_facts": len(gf), "old": None, "new": None, "error": None}
        try:
            t = c["result"]
            obj = json.loads(t[t.find("{"):t.rfind("}") + 1])
            for side in ("old", "new"):
                v = obj[side]
                if v == "none" or v == []:
                    rec[side] = "none"
                else:
                    nums = [int(x) for x in v]
                    if not nums or any(n < 1 or n > len(gf) for n in nums):
                        raise ValueError("fact number outside list: %r" % v)
                    rec[side] = [L.fid(gf[n - 1]) for n in nums]
        except Exception as e:
            rec["error"] = str(e)[:200]
        return q, rec
    with ThreadPoolExecutor(3) as ex:
        for q, rec in ex.map(one, [q for q in ids() if q not in got]):
            got[q] = rec
    jsave(GOLDP, got)
    for q in ids():
        r = got[q]
        print(q, "old", r["old"], "new", r["new"], "err", r["error"])


def stage_expand():
    S, _ = recs()
    m, out = llm(EXPAND_RECIPE), jload(CUES, {})
    for q in ids():
        if q in out:
            continue
        c = m.call(EXPAND_TEMPLATE.format(date=S[q]["question_date"], question=S[q]["question"]), EXPAND_SYSTEM, "sonnet", "GATE3-EXPAND", q, "expand")
        t = c["result"]
        out[q] = json.loads(t[t.find("{"):t.rfind("}") + 1])["cue_keys"]
    jsave(CUES, out)
    print("expanded", len(out))


def pk(pair, tag):
    return "%s|%s|%s" % (pair[0], pair[1], tag)


def all_pairs(qd):
    return sorted([dict(c, by=by, qid=q) for q, (facts, by, cands) in qd.items() for c in cands], key=lambda c: c["pair"])


def stage_judge1(batch=15):
    S, qd = qdata()
    dec = jload(DEC, {})
    m = llm(LINK_RECIPE)
    for run, seed in (("A", None), ("B", 16096)):
        tag = TAG[run][0]
        todo = [c for c in all_pairs(qd) if pk(c["pair"], tag) not in dec]
        if seed is not None:
            random.Random(seed).shuffle(todo)
        chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
        print("stage 1 run", run, "pairs", len(todo), "calls", len(chunks))

        def one(ch):
            c = m.call(L.build_link_prompt(ch, None), LINK_SYSTEM, "sonnet", "GATE3-LINK" + run, ch[0]["qid"], "linkjudge")
            return ch, L.parse_labels(c["result"], len(ch), True)
        with ThreadPoolExecutor(3) as ex:
            for ch, labs in ex.map(one, chunks):
                for p, (lab, why) in zip(ch, labs):
                    dec[pk(p["pair"], tag)] = {"label": lab, "reason": why}
        jsave(DEC, dec)


def stage_verify(batch=12):
    S, qd = qdata()
    dec = jload(DEC, {})
    m = llm(R5.VERIFY_RECIPE)
    for run, seed in (("A", None), ("B", 7)):
        t1, t2 = TAG[run]
        todo = [c for c in all_pairs(qd) if dec.get(pk(c["pair"], t1), {}).get("label") in GOOD and pk(c["pair"], t2) not in dec]
        if seed is not None:
            random.Random(seed).shuffle(todo)
        chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
        print("stage 2 run", run, "pairs", len(todo), "calls", len(chunks))

        def one(ch):
            c = m.call(R5.build_verify_prompt(ch), R5.VERIFY_SYSTEM, "sonnet", "GATE3-VERIFY" + run, ch[0]["qid"], "verify")
            return ch, R5.parse_verify(c["result"], len(ch))
        with ThreadPoolExecutor(3) as ex:
            for ch, labs in ex.map(one, chunks):
                for p, (lab, fields, raw) in zip(ch, labs):
                    dec[pk(p["pair"], t2)] = {"label": lab, "fields": fields, "raw": raw, "final": R5.verified_label(lab, fields)}
        jsave(DEC, dec)


def run_final(dec, pair, run):
    t1, t2 = TAG[run]
    d = dec.get(pk(pair, t1))
    if not d:
        return None
    if d["label"] not in GOOD:
        return d["label"]
    v = dec.get(pk(pair, t2))
    return v["final"] if v else "unverified"


def final_decisions(cands, dec):
    """GATE3 section 6 item 4: a link only when both runs end in replaces/pending; replaces only if both say replaces."""
    out = {}
    for c in cands:
        a, b = run_final(dec, c["pair"], "A"), run_final(dec, c["pair"], "B")
        if a in GOOD and b in GOOD:
            out[c["pair"]] = "replaces" if a == b == "replaces" else "pending"
        else:
            out[c["pair"]] = a if a not in GOOD else "unrepeated"
    return out


def stage_links():
    S, qd = qdata()
    dec = jload(DEC, {})
    s1flip = finalflip = npos = 0
    nlinks = 0
    for q, (facts, by, cands) in qd.items():
        for c in cands:
            a1 = dec[pk(c["pair"], TAG["A"][0])]["label"] in GOOD
            b1 = dec[pk(c["pair"], TAG["B"][0])]["label"] in GOOD
            s1flip += a1 != b1
            fa, fb = run_final(dec, c["pair"], "A") in GOOD, run_final(dec, c["pair"], "B") in GOOD
            finalflip += fa != fb
            npos += fa or fb
        nlinks += sum(1 for v in final_decisions(cands, dec).values() if v in GOOD)
    P = sum(len(c) for _, _, c in qd.values())
    print("pairs", P, "stage-1 positive/negative flips", s1flip, "final link/no-link flips", finalflip, "of", npos, "pairs linked by either run; final links", nlinks)
    jsave(os.path.join(OUT, "dev2_flips.json"), {"pairs": P, "stage1_flips": s1flip, "final_flips": finalflip, "either_run_links": npos, "final_links": nlinks})


def resolved(q, qd, dec):
    facts, by, cands = qd[q]
    fs = [dict(f) for f in facts]
    fs, links = L.derive_status(fs, {p: l for p, l in final_decisions(cands, dec).items()})
    return fs, by, links


def save(r):
    with open(RES, "a") as fh:
        fh.write(json.dumps(r) + "\n")


def results():
    out = {}
    for r in V3.raw_lines(RES):
        out.setdefault((r["arm"], r["qid"]), {}).update(r)
    return out


def stage_oracle():
    S, O = recs()
    m = llm(READER_V12_RECIPE)
    have = results()

    def one(q):
        ctx = data.render_sessions(data.chrono(data.sessions_of(O[q])))
        c = m.call(V3.read_prompt(ctx, S[q]), READER_SYSTEM, "sonnet", "GATE3-ORACLE", q, "read")
        return {"arm": "ORACLE", "qid": q, "hypothesis": c["result"], "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out_tokens": c["out_tokens"]}
    with ThreadPoolExecutor(3) as ex:
        for r in ex.map(one, [q for q in ids() if ("ORACLE", q) not in have]):
            save(r)


def read_arm(arm):
    S, qd = qdata()
    dec = jload(DEC, {})
    cues = json.load(open(CUES))
    m = llm(READER_V12_RECIPE)
    have = results()

    def one(q):
        fs, by, _ = resolved(q, qd, dec)
        qdate = data.parse_date(S[q]["question_date"])
        if arm == "FACTS5":
            ctx, top = V3.build_context_v3(S[q]["question"], cues[q], qdate, fs, by)
        else:
            ctx, top = R5.context_nolabel(S[q]["question"], cues[q], qdate, fs, by)
        c = m.call(V3.read_prompt(ctx, S[q]), READER_SYSTEM, "sonnet", "GATE3-" + arm, q, "read")
        return {"arm": arm, "qid": q, "hypothesis": c["result"], "shown": [f["_i"] for f in top],
                "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out_tokens": c["out_tokens"]}
    with ThreadPoolExecutor(3) as ex:
        for r in ex.map(one, [q for q in ids() if (arm, q) not in have]):
            save(r)


def stage_read():
    read_arm("FACTS5")


def stage_nolabel():
    read_arm("NOLABEL")


def stage_judge(batch=6):
    S, _ = recs()
    res = results()
    todo = sorted((r for r in res.values() if "hypothesis" in r and "label" not in r), key=lambda r: (r["arm"], r["qid"]))
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    m = llm(JUDGE_RECIPE)

    def one(ch):
        items = [(S[r["qid"]]["question_type"], S[r["qid"]]["question"], str(S[r["qid"]]["answer"]), r["hypothesis"], r["qid"].endswith("_abs")) for r in ch]
        c = m.call(build_batch_prompt(items), JUDGE_SYSTEM, "opus", "GATE3-JUDGE", "g" + ch[0]["arm"] + ch[0]["qid"], "judge")
        return ch, parse_batch(c["result"], len(ch))
    with ThreadPoolExecutor(3) as ex:
        for ch, labs in ex.map(one, chunks):
            for r, lab in zip(ch, labs):
                save({"arm": r["arm"], "qid": r["qid"], "label": lab})
    print("judged", len(todo))


def stage_audit(batch=10):
    S, qd = qdata()
    dec = jload(DEC, {})
    items = []
    for q, (facts, by, cands) in qd.items():
        d = final_decisions(cands, dec)
        for c in cands:
            if d[c["pair"]] in GOOD:
                o, n = c["old"], c["new"]
                items.append({"qid": q, "pair": list(c["pair"]), "judge": d[c["pair"]], "d1": str(o["asserted_at"].date()), "s1": L.stmt(o), "r1": L.round_text(by, o),
                              "d2": str(n["asserted_at"].date()), "s2": L.stmt(n), "r2": L.round_text(by, n)})
    total = len(items)
    if total > 80:
        items = random.Random(16096).sample(items, 80)
    m = llm(LINKAUDIT_RECIPE)
    chunks = [items[i:i + batch] for i in range(0, len(items), batch)]

    def one(ch):
        c = m.call(L.build_audit_prompt(ch, None), LINKAUDIT_SYSTEM, "opus", "GATE3-AUDIT", ch[0]["qid"], "audit")
        return ch, L.parse_labels(c["result"], len(ch))
    res = []
    with ThreadPoolExecutor(3) as ex:
        for ch, labs in ex.map(one, chunks):
            res += [dict(x, audit=l) for x, l in zip(ch, labs)]
    jsave(AUD, {"total_links": total, "audited": res})
    print("audited", len(res), "of", total)


def stage_report():
    S, qd = qdata()
    dec = jload(DEC, {})
    res = results()
    gp = json.load(open(GOLDP))
    aud = json.load(open(AUD))["audited"]
    w = R5.wilson
    Q = ids()
    n = len(Q)
    lab = lambda arm, q: res.get((arm, q), {}).get("label")
    f5 = [q for q in Q if lab("FACTS5", q)]
    orc = [q for q in Q if lab("ORACLE", q)]
    nl = [q for q in Q if lab("NOLABEL", q)]
    out = []
    out.append("G1 FACTS5 %s | ORACLE %s | pass %s | FACTS5 failures %s | ORACLE failures %s" % (w(len(f5), n), w(len(orc), n), len(f5) >= 14 and len(f5) >= len(orc) - 1,
                                                                                         [q for q in Q if q not in f5], [q for q in Q if q not in orc]))
    g2 = g3 = 0
    f2, f3 = [], []
    for q in Q:
        fs, by, links = resolved(q, qd, dec)
        B, cov = V3.gold_value_fact(S[q], fs)
        shown = set(res[("FACTS5", q)]["shown"])
        if B is not None and B["_i"] in shown:
            g2 += 1
            earlier = any(x in S[q]["question"].lower() for x in V3.EARLIER)
            if B["status"] in ("current", "pending-update") or (earlier and B["status"] == "superseded"):
                g3 += 1
            else:
                f3.append("%s(%s)" % (q, B["status"]))
        else:
            f2.append("%s(cov %.2f)" % (q, cov))
            f3.append(q)
    out.append("G2 %s pass %s failures %s" % (w(g2, n), g2 >= 14, f2))
    out.append("G3 %s pass %s failures %s" % (w(g3, n), g3 >= 13, f3))
    p = sum(1 for x in aud if x["audit"] in GOOD)
    h = sum(1 for x in aud if x["audit"] in ("unrelated", "restates"))
    na = len(aud)
    out.append("G4 precision %s pass %s" % (w(p, na), na > 0 and p / float(na) >= 0.90))
    out.append("G4b harmful %s rate %.3f pass %s" % (w(h, na), h / float(na) if na else 0, na > 0 and h / float(na) <= 0.05))
    for x in aud:
        if x["audit"] not in GOOD:
            out.append("   FALSE LINK %s %s -> %s | %s => %s" % (x["qid"], x["judge"], x["audit"], x["s1"][:70], x["s2"][:70]))
    g5, f5l, miss = 0, [], []
    for q in Q:
        r = gp[q]
        if r["error"]:
            f5l.append("%s(goldpair error)" % q)
            continue
        if r["old"] == "none" or r["new"] == "none":
            miss.append("%s(old %s, new %s)" % (q, "none" if r["old"] == "none" else "ok", "none" if r["new"] == "none" else "ok"))
            f5l.append("%s(extraction miss)" % q)
            continue
        d = final_decisions(qd[q][2], dec)
        hit = any(l in GOOD and ((p_[0] in r["old"] and p_[1] in r["new"]) or (p_[1] in r["old"] and p_[0] in r["new"])) for p_, l in d.items())
        if hit:
            g5 += 1
        else:
            cand = any((c["pair"][0] in r["old"] and c["pair"][1] in r["new"]) or (c["pair"][1] in r["old"] and c["pair"][0] in r["new"]) for c in qd[q][2])
            f5l.append("%s(%s)" % (q, "judged no link" if cand else "not a candidate"))
    out.append("G5 %s pass %s failures %s | extraction misses %s" % (w(g5, n), g5 >= 12, f5l, miss))
    out.append("DIAG NOLABEL %s vs FACTS5 %s; answers that differ (FACTS5/NOLABEL): %s" % (w(len(nl), n), w(len(f5), n),
                                                                                       [(q, bool(lab("FACTS5", q)), bool(lab("NOLABEL", q))) for q in Q if bool(lab("FACTS5", q)) != bool(lab("NOLABEL", q))]))
    out.append("flips " + json.dumps(jload(os.path.join(OUT, "dev2_flips.json"), {})))
    out.append("pairs per question " + json.dumps({q: len(qd[q][2]) for q in Q}))
    print("\n".join(out))
    open(os.path.join(OUT, "gate3_report.txt"), "w").write("\n".join(out) + "\n")


if __name__ == "__main__":
    for st in sys.argv[1:]:
        globals()["stage_" + st]()
