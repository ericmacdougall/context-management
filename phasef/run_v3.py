"""Phase F v3 gate run (see GATE.md). Stages: prepare extract canon expand oracle read judge audit analyse. All calls cached; cap 240 total."""
import json
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import data, run_pilot as R
from .canon import build_prompt as canon_prompt, parse_mapping
from .facts import _json_from
from .facts_v2 import build_extract_prompt_v2, parse_extraction_v2, score_v2
from .judge import build_batch_prompt, parse_batch
from .llm import LLM
from .prompts import (AUDIT_RECIPE, AUDIT_SYSTEM, AUDIT_TEMPLATE, CANON_RECIPE, CANON_SYSTEM, EXPAND_RECIPE, EXPAND_SYSTEM, EXPAND_TEMPLATE,
                      EXTRACT_SYSTEM, EXTRACT_V2_RECIPE, JUDGE_RECIPE, JUDGE_SYSTEM, READER_SYSTEM, READER_TEMPLATE_V12, READER_V12_RECIPE)
from .supersede_v2 import norm_slot
from .supersede_v3 import resolve_v3, slot_rate

CEILING = 240
OUT = R.OUT
KU_IDS = os.path.join(OUT, "gate16_ids.json")
S16, O16 = os.path.join(OUT, "gate16_s.json"), os.path.join(OUT, "gate16_oracle.json")
FACTS_V2, FACTS_V3 = os.path.join(OUT, "facts_v2_raw.jsonl"), os.path.join(OUT, "facts_v3_raw.jsonl")
CANON, CUES, RES = os.path.join(OUT, "gate_canon.json"), os.path.join(OUT, "gate_cues.json"), os.path.join(OUT, "results_v4.jsonl")
EARLIER = ("initially", "originally", "previously", "used to", "before", "first")


def llm(recipe):
    os.makedirs(R.NEUTRAL_CWD, exist_ok=True)
    return LLM(R.CACHE, os.path.join(OUT, "ledger_v2.jsonl"), cap=CEILING, concurrency=3, recipe=recipe, cwd=R.NEUTRAL_CWD)


def ids():
    return json.load(open(KU_IDS))


def gate():
    return data.load(S16), data.load(O16)


def stage_prepare():
    from emlab.dataset import LongMemEvalLoader, select_split
    qs = list(LongMemEvalLoader(os.path.join(R.DATA, "longmemeval_s_cleaned.json")).questions())
    ku = sorted(q.qid for q in select_split(qs, "pilot") if q.qtype == "knowledge-update")
    del qs
    json.dump(ku, open(KU_IDS, "w"))
    for src, dst in ((os.path.join(R.DATA, "longmemeval_s_cleaned.json"), S16), (os.path.join(R.DATA, "longmemeval_oracle.json"), O16)):
        recs = data.extract_records(src, ku)
        assert len(recs) == len(ku)
        json.dump(recs, open(dst, "w"))
    print(len(ku), ku)


def raw_lines(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def all_session_keys():
    S, _ = gate()
    uniq = {}
    for q in ids():
        for sid, d, turns in data.sessions_of(S[q]):
            uniq.setdefault(R._skey(sid, d, turns), (sid, d, turns))
    return uniq


def strict_ok(text):
    try:
        t = text.strip()
        json.loads(t[t.find("{"):t.rfind("}") + 1])
        return True
    except ValueError:
        return False


def stage_extract():
    uniq = all_session_keys()
    have = set(f["session_key"] for f in raw_lines(FACTS_V2))
    # sessions cached in v2 raw that produced zero facts would be re-extracted; accept (counted)
    todo = [k for k in sorted(uniq) if k not in have]
    print("unique sessions in gate set", len(uniq), "already extracted (v2 cache)", len(uniq) - len(todo), "to extract", len(todo))
    m = llm(EXTRACT_V2_RECIPE)

    def run(batch, tag):
        c = m.call(build_extract_prompt_v2(batch), EXTRACT_SYSTEM, "sonnet", "FACTS3", tag, "extract")
        present = len(set(re.findall(r'"session_id":\s*"', c["result"]))) and len(re.findall(r'"session_id":\s*"', c["result"]))
        try:
            per, dropped = parse_extraction_v2(c["result"], batch)
        except Exception as e:
            per, dropped = None, str(e)
        return {"tag": tag, "n": len(batch), "entries": present, "strict": strict_ok(c["result"]), "per": per, "dropped": dropped,
                "in": c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out": c["out_tokens"], "sec": c["seconds"], "batch": batch}

    first = [[uniq[k] for k in todo[i:i + 16]] for i in (0, 16, 32)]
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(lambda a: run(*a), [(b, "g16_%d" % i) for i, b in enumerate(first)]))
    safe = all(r["strict"] and r["per"] is not None and r["entries"] == r["n"] for r in res)
    for r in res:
        print("batch16", r["tag"], "sessions", r["n"], "entries", r["entries"], "strict_json", r["strict"], "facts", sum(len(x) for x in r["per"]) if r["per"] else None, "out", r["out"], "in", r["in"], "sec", r["sec"])
    print("BATCH16 SAFE:", safe)
    size = 16 if safe else 8
    rest = todo[48:]
    batches = [[uniq[k] for k in rest[i:i + size]] for i in range(0, len(rest), size)]
    with ThreadPoolExecutor(3) as ex:
        res += list(ex.map(lambda a: run(*a), [(b, "g%d_%d" % (size, i)) for i, b in enumerate(batches)]))
    bad = [r["tag"] for r in res if r["per"] is None or not r["strict"] or r["entries"] != r["n"]]
    print("batches", len(res), "incomplete/repaired:", bad)
    with open(FACTS_V3, "w") as fh:
        for r in res:
            if r["per"] is None:
                continue
            for item, fs in zip(r["batch"], r["per"]):
                for f in fs:
                    g = dict(f)
                    g["asserted_at"], g["valid_from"] = f["asserted_at"].isoformat(), f["valid_from"].isoformat()
                    g["session_key"] = R._skey(*item)
                    fh.write(json.dumps(g) + "\n")
    tot_in, tot_out = sum(r["in"] for r in res), sum(r["out"] for r in res)
    ns = sum(r["n"] for r in res)
    print("extraction calls", len(res), "sessions", ns, "in", tot_in, "out", tot_out, "per session net in", (tot_in - 10500 * len(res)) / float(ns), "out", tot_out / float(ns))


def load_facts():
    per = {}
    for path in (FACTS_V2, FACTS_V3):
        for f in raw_lines(path):
            f["asserted_at"], f["valid_from"] = datetime.fromisoformat(f["asserted_at"]), datetime.fromisoformat(f["valid_from"])
            per.setdefault(f["session_key"], []).append(f)
    return per


def question_facts(S, q, per):
    ss = data.chrono(data.sessions_of(S[q]))
    flat, order = [], 0
    for sid, d, turns in ss:
        for f in per.get(R._skey(sid, d, turns), []):
            g = dict(f)
            g["order"] = order
            order += 1
            flat.append(g)
    return flat, {s[0]: (s[1], s[2]) for s in ss}


def stage_canon():
    S, _ = gate()
    per = load_facts()
    m = llm(CANON_RECIPE)
    out = {}

    def one(q):
        facts, _ = question_facts(S, q, per)
        keys, prompt = canon_prompt(facts)
        c = m.call(prompt, CANON_SYSTEM, "sonnet", "FACTS3", q, "canon")
        try:
            return q, parse_mapping(c["result"], keys), None, len(keys), c
        except Exception as e:
            return q, None, str(e), len(keys), c
    with ThreadPoolExecutor(3) as ex:
        for q, mp, err, nk, c in ex.map(one, ids()):
            out[q] = {"mapping": mp, "error": err}
            print(q, "keys", nk, "valid" if mp else "INVALID " + str(err)[:80], "merged", (nk - len(set(mp.values()))) if mp else None, "in", c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out", c["out_tokens"])
    json.dump(out, open(CANON, "w"))


def stage_expand():
    S, _ = gate()
    m, out = llm(EXPAND_RECIPE), {}
    for q in ids():
        c = m.call(EXPAND_TEMPLATE.format(date=S[q]["question_date"], question=S[q]["question"]), EXPAND_SYSTEM, "sonnet", "FACTS2", q, "expand")
        t = c["result"]
        out[q] = json.loads(t[t.find("{"):t.rfind("}") + 1])["cue_keys"]
    json.dump(out, open(CUES, "w"))
    print("expanded", len(out))


def resolved(S, q, per, canon):
    facts, by = question_facts(S, q, per)
    mp = (canon.get(q) or {}).get("mapping")
    facts, links = resolve_v3(facts, mp)
    return facts, by, links


def label(f, facts):
    st = f["status"]
    if st == "superseded":
        y = facts[f["superseded_by"]]
        return "SUPERSEDED by later statement (%s): %s" % (y["asserted_at"].date(), y["object"][:80])
    if st == "pending-update":
        h = facts[f["pending_of"]]
        return "PENDING-UPDATE (change to a value already stated on %s: %s)" % (h["asserted_at"].date(), h["object"][:80])
    if st == "current":
        extra = ""
        if f["pending_updates"]:
            extra = "; has pending update: " + "; ".join("%s (%s)" % (facts[i]["object"][:60], facts[i]["asserted_at"].date()) for i in f["pending_updates"][:2])
        return "CURRENT" + extra
    if st == "disputed":
        return "DISPUTED"
    return f["modality"].upper()


def build_context_v3(question, cues, qdate, facts, by):
    vis = [f for f in facts if f["modality"] != "question" and f["asserted_at"].date() <= qdate.date()]
    top = score_v2(question, cues, vis)
    lines = ["Memory facts, most relevant first. Each shows the date of the session that stated it and a status label (labels are advisory; every fact is shown):"]
    for f in top:
        lines.append("- [%s] stated %s%s | %s: %s = %s (session %s round %d; quote: \"%s\")" % (
            label(f, facts), f["asserted_at"].date(), ", event %s" % f["event_time"] if f.get("event_time") else "", f["subject"], f["predicate"], f["object"], f["session_id"], f["round"], f["quote"]))
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


def read_prompt(ctx, rec):
    return READER_TEMPLATE_V12.format(context=ctx, date=rec["question_date"], question=rec["question"])


def save(r):
    with open(RES, "a") as fh:
        fh.write(json.dumps(r) + "\n")


def stage_oracle():
    S, O = gate()
    m = llm(READER_V12_RECIPE)

    def one(q):
        ctx = data.render_sessions(data.chrono(data.sessions_of(O[q])))
        c = m.call(read_prompt(ctx, S[q]), READER_SYSTEM, "sonnet", "ORACLE3", q, "read")
        r = {"arm": "ORACLE3", "qid": q, "hypothesis": c["result"], "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out_tokens": c["out_tokens"]}
        save(r)
        return r
    with ThreadPoolExecutor(3) as ex:
        for r in ex.map(one, ids()):
            print(r["qid"], r["in_tokens"], r["hypothesis"][-100:].replace("\n", " "))


def stage_read():
    S, _ = gate()
    per, canon, cues = load_facts(), json.load(open(CANON)), json.load(open(CUES))
    m = llm(READER_V12_RECIPE)

    def one(q):
        facts, by, links = resolved(S, q, per, canon)
        ctx, top = build_context_v3(S[q]["question"], cues[q], data.parse_date(S[q]["question_date"]), facts, by)
        c = m.call(read_prompt(ctx, S[q]), READER_SYSTEM, "sonnet", "FACTS3", q, "read")
        r = {"arm": "FACTS3", "qid": q, "hypothesis": c["result"], "in_tokens": c["in_tokens"] + c["cache_creation"] + c["cache_read"], "out_tokens": c["out_tokens"], "shown": [f["_i"] for f in top]}
        save(r)
        return r
    with ThreadPoolExecutor(3) as ex:
        for r in ex.map(one, ids()):
            print(r["qid"], r["in_tokens"], r["hypothesis"][-100:].replace("\n", " "))


def last_results():
    out = {}
    for r in raw_lines(RES):
        out[(r["arm"], r["qid"])] = out.get((r["arm"], r["qid"]), {})
        out[(r["arm"], r["qid"])].update(r)
    return out


def stage_judge(batch=6):
    S, _ = gate()
    res = last_results()
    todo = sorted((r for r in res.values() if "hypothesis" in r and "label" not in r), key=lambda r: (r["arm"], r["qid"]))
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    m = llm(JUDGE_RECIPE)

    def one(ch):
        items = [(S[r["qid"]]["question_type"], S[r["qid"]]["question"], str(S[r["qid"]]["answer"]), r["hypothesis"], r["qid"].endswith("_abs")) for r in ch]
        c = m.call(build_batch_prompt(items), JUDGE_SYSTEM, "opus", "JUDGE", "g" + ch[0]["arm"] + ch[0]["qid"], "judge")
        return ch, parse_batch(c["result"], len(ch))
    with ThreadPoolExecutor(3) as ex:
        for ch, labs in ex.map(one, chunks):
            for r, lab in zip(ch, labs):
                save(dict(r, label=lab))
    print("judged", len(todo))


def answer_tokens(a):
    return set(t for t in re.findall(r"[a-z0-9]+", str(a).lower()) if t not in {"a", "an", "the", "of", "in", "my", "to", "i", "on", "at", "is", "was", "and"})


def gold_value_fact(rec, facts):
    ans = answer_tokens(rec["answer"])
    gold = set(rec["answer_session_ids"])
    best, bs = None, 0.0
    for f in facts:
        if f["session_id"] in gold and ans:
            cov = len(ans & answer_tokens(f["object"] + " " + f.get("quote", ""))) / float(len(ans))
            if cov > bs or (cov == bs and best is not None and f["asserted_at"] > best["asserted_at"]):
                best, bs = f, cov
    return (best if bs >= 0.5 else None), bs


def audit_items(S, per, canon):
    items = []
    for q in ids():
        facts, by, links = resolved(S, q, per, canon)
        for kind, a, b in links:
            items.append({"qid": q, "kind": kind, "slot": facts[b]["canon"], "d1": str(facts[a]["asserted_at"].date()), "q1": facts[a]["quote"],
                          "d2": str(facts[b]["asserted_at"].date()), "q2": facts[b]["quote"]})
    return items


def stage_audit(batch=20):
    S, _ = gate()
    items = audit_items(S, load_facts(), json.load(open(CANON)))
    total = len(items)
    if total > 60:
        items = random.Random(16096).sample(items, 60)
    m = llm(AUDIT_RECIPE)
    labs = []
    for i in range(0, len(items), batch):
        ch = items[i:i + batch]
        prompt = "\n\n".join(AUDIT_TEMPLATE.format(i=j + 1, slot=x["slot"], d1=x["d1"], q1=x["q1"], d2=x["d2"], q2=x["q2"]) for j, x in enumerate(ch))
        c = m.call(prompt, AUDIT_SYSTEM, "opus", "AUDIT", "a%d" % i, "audit")
        labs += parse_batch(c["result"], len(ch))
    json.dump({"total_links": total, "audited": [dict(x, yes=l) for x, l in zip(items, labs)]}, open(os.path.join(OUT, "gate_audit.json"), "w"))
    print("links", total, "audited", len(items), "yes", sum(labs))


def stage_analyse():
    from emlab.metrics import wilson_interval
    S, _ = gate()
    per, canon, cues = load_facts(), json.load(open(CANON)), json.load(open(CUES))
    res = last_results()
    n = len(ids())
    fk = sum(1 for q in ids() if res[("FACTS3", q)]["label"])
    ok = sum(1 for q in ids() if res[("ORACLE3", q)]["label"])
    g1 = fk >= 14 and fk >= ok - 1
    g2 = g3 = 0
    fails = {"G1": [q for q in ids() if not res[("FACTS3", q)]["label"]], "G2": [], "G3": []}
    before, after = {"n": 0, "hit": 0}, {"n": 0, "hit": 0}
    for q in ids():
        facts, by, links = resolved(S, q, per, canon)
        gv, cov = gold_value_fact(S[q], facts)
        shown = set(res[("FACTS3", q)]["shown"])
        if gv is not None and gv["_i"] in shown:
            g2 += 1
            earlier = any(w in S[q]["question"].lower() for w in EARLIER)
            if gv["status"] in ("current", "pending-update") or (earlier and gv["status"] == "superseded"):
                g3 += 1
            else:
                fails["G3"].append("%s(%s)" % (q, gv["status"]))
        else:
            fails["G2"].append("%s(cov %.2f%s)" % (q, cov, "" if gv else ", no gold-value fact"))
            fails["G3"].append(q)
        for key, acc in (("slot_norm", before), ("canon", after)):
            for f in facts:
                f["slot_norm"] = norm_slot(f["slot"])
            r = slot_rate(facts, key)
            acc["n"] += r["n"]
            acc["hit"] += r["hit"]
    aud = json.load(open(os.path.join(OUT, "gate_audit.json")))
    yes = sum(1 for x in aud["audited"] if x["yes"])
    na = len(aud["audited"])
    pr = lambda k, n_: "%d/%d Wilson95 [%.2f, %.2f]" % ((k, n_) + wilson_interval(k, n_))
    print("G1 FACTS3 %s | ORACLE %s | pass %s | failures %s" % (pr(fk, n), pr(ok, n), g1, fails["G1"]))
    print("G2 %s | pass %s | failures %s" % (pr(g2, n), g2 >= 14, fails["G2"]))
    print("G3 %s | pass %s | failures %s" % (pr(g3, n), g3 >= 12, fails["G3"]))
    print("G4 %s | pass %s | total links %d, kinds %s" % (pr(yes, na), (yes / float(na) >= 0.9) if na else False, aud["total_links"],
                                                          {k: sum(1 for x in aud["audited"] if x["kind"] == k) for k in ("supersession", "pending")}))
    print("G5 single-state facts in a cross-session slot: before canon %d/%d (%.1f%%), after %d/%d (%.1f%%)" % (
        before["hit"], before["n"], 100.0 * before["hit"] / max(1, before["n"]), after["hit"], after["n"], 100.0 * after["hit"] / max(1, after["n"])))
    print("canon invalid:", [q for q in ids() if not canon[q]["mapping"]])


if __name__ == "__main__":
    for st in sys.argv[1:]:
        globals()["stage_" + st]()
