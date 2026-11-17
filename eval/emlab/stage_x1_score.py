"""Score STAGE-X1 (extraction at volume on the pilot split; no model call, no judge). Rules and the decision rule: STAGE-X1.md (committed before the calls).
The only decision: facts-as-candidates BM25 top-10 against the raw-round control for the 11 weak-list questions over the 1,000-session pool."""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

from . import provenance_x04 as prov
from .retrievers import tokens
from .stage_x0_score import bm25_rank, dist, extrapolate, mean, spearman
from .xp_x0c_posthoc import classify_ungrounded

CAL = (6100, 22500)
OVERHEAD = 10500
PRIMARY_MIN_HITS = 6


def find_other_round(quote: str, groups, idx: int) -> Optional[int]:
    q = prov.norm(quote)
    for i, g in enumerate(groups):
        if i != idx and q and q in prov.norm(" ".join(t["content"] for t in g)):
            return i
    return None


def repair_fact(f: Dict[str, Any], groups) -> Dict[str, Any]:
    """Mechanical post-processing (fixed before the run). -> the fact with `grounding` in {verbatim, punct_cited, other_round, paraphrase, ungrounded}, `author` filled
    from the speaker of the quote when the field is missing (a unique speaker; otherwise left as `unknown`), `round` re-pointed for other_round."""
    g = dict(f)
    q = g.get("quote", "")
    idx = g.get("round")
    ok, sp = prov.anchor(q, groups, idx)
    if ok:
        g["grounding"] = "verbatim"
    else:
        c = classify_ungrounded(q, groups, idx)
        g["grounding"] = c
        if c == "other_round":
            j = find_other_round(q, groups, idx)
            if j is not None:
                g["round"] = j
                ok, sp = prov.anchor(q, groups, j)
    if "author" not in g or g.get("author") not in ("user", "assistant"):
        g["author"] = (list(sp)[0] if len(sp) == 1 else "unknown")
        g["author_filled"] = True
    return g


def kept(f: Dict[str, Any]) -> bool:
    return f.get("grounding") in ("verbatim", "punct_cited", "other_round")


def stmt(f: Dict[str, Any]) -> List[str]:
    return tokens(" ".join(str(f.get(k, "")) for k in ("subject", "predicate", "object")))


def stmt_plus(f: Dict[str, Any]) -> List[str]:
    return tokens(" ".join(str(f.get(k, "")) for k in ("subject", "predicate", "object", "quote")) + " " + " ".join(f.get("cue_keys") or []))


def first_rank(order: List[int], owner: List[str], gold: set) -> Optional[int]:
    return next((i + 1 for i, d in enumerate(order) if owner[d] in gold), None)


def evaluate(queries: Dict[str, str], gold: Dict[str, set], fact_docs, fact_owner, round_docs, round_owner, round_ids) -> Dict[str, Any]:
    out = {}
    for x, text in queries.items():
        qt = tokens(text)
        fo = bm25_rank(fact_docs[0], qt) if fact_docs[0] else []
        f2 = bm25_rank(fact_docs[1], qt) if fact_docs[1] else []
        ro = bm25_rank(round_docs, qt) if round_docs else []
        g = gold[x]
        rf, rf2, rr = first_rank(fo, fact_owner, g), first_rank(f2, fact_owner, g), first_rank(ro, round_owner, g)
        # combined arm: 5 best facts and 5 best raw rounds as one capped list of 10 items
        comb = any(fact_owner[d] in g for d in fo[:5]) or any(round_owner[d] in g for d in ro[:5])
        out[x] = {"facts_best_gold_rank": rf, "facts_top10": bool(rf and rf <= 10), "facts_plus_best_gold_rank": rf2, "facts_plus_top10": bool(rf2 and rf2 <= 10),
                  "raw_best_gold_rank": rr, "raw_top10": bool(rr and rr <= 10), "combined_5_5_top10": comb}
    return out


def tally(per: Dict[str, Any]) -> Dict[str, int]:
    return {"n": len(per), "facts": sum(1 for v in per.values() if v["facts_top10"]), "facts_plus": sum(1 for v in per.values() if v["facts_plus_top10"]),
            "raw": sum(1 for v in per.values() if v["raw_top10"]), "combined": sum(1 for v in per.values() if v["combined_5_5_top10"])}


def main(dataset, art, sets_path, out_root, in_root, out_json):
    from .build_rag_contexts import load_common
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qmap = {q.qid: q for q in qs}
    k0 = json.load(open(os.path.join(out_root, "stage_x0_key", "selection_key.json")))
    k1 = json.load(open(os.path.join(out_root, "stage_x1_key", "selection_key.json")))
    prep0 = json.load(open(os.path.join(out_root, "stage_x0", "prep_report.json")))
    edges = prep0["strata_edges_chars"]
    binof = lambda c: sum(1 for e in edges if c > e)
    rows0 = {r["opaque_session_id"]: r for r in k0["sessions"]}
    rows1 = {r["opaque_session_id"]: r for r in k1["sessions"]}
    sess_turns = {}
    for o in rows0:
        sess_turns[o] = json.load(open(os.path.join(out_root, "stage_x0", "sessions", o + ".json"), encoding="utf-8"))["turns"]
    for o in rows1:
        sess_turns[o] = json.load(open(os.path.join(out_root, "stage_x1", "sessions", o + ".json"), encoding="utf-8"))["turns"]
    groups = {o: prov.rounds_of_turns(t) for o, t in sess_turns.items()}
    L = lambda p: json.load(open(p, encoding="utf-8"))
    res: Dict[str, Any] = {"pilot": True, "confirmatory": False}
    # ---------------- X1 records
    recs = {}
    for o in rows1:
        p = os.path.join(in_root, "stage_x1", o + ".json")
        recs[o] = L(p) if os.path.exists(p) else {"status": "missing", "facts": []}
    run_meta_p = os.path.join(in_root, "stage_x1", "_run.json")
    run_meta = L(run_meta_p) if os.path.exists(run_meta_p) else {}
    res["run_meta"] = run_meta
    res["status_counts"] = dict(Counter(r.get("status") for r in recs.values()))
    done = [o for o in recs if recs[o].get("status") in ("ok", "repaired")]
    called = [o for o in recs if recs[o].get("status") not in ("missing", "not_run", None)]
    for o in called:
        r = recs[o]
        r["total_input_tokens"] = sum(int(r.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        r["input_no_overhead"] = max(0, r["total_input_tokens"] - OVERHEAD)
    # ---------------- 1. volume measures (800 calls)
    fps = {o: len(recs[o].get("facts") or []) for o in done}
    raw_facts = [dict(f, session_id=o) for o in done for f in (recs[o].get("facts") or [])]
    repaired = [repair_fact(f, groups[f["session_id"]]) for f in raw_facts]
    g = Counter(f["grounding"] for f in repaired)
    nraw = len(repaired)
    a_raw = g["verbatim"]
    a_rep = g["verbatim"] + g["punct_cited"] + g["other_round"]
    lo_r, hi_r = prov.wilson(a_raw, nraw) if nraw else (0.0, 0.0)
    lo_p, hi_p = prov.wilson(a_rep, nraw) if nraw else (0.0, 0.0)
    au = Counter(f.get("author") for f in raw_facts)
    res["volume"] = {"calls_recorded": len(called), "sessions_done": len(done), "repaired_status": sum(1 for o in done if recs[o].get("status") == "repaired"),
                     "parse_failures": sum(1 for o in called if recs[o].get("status") == "parse_failure"), "dropped_entries": sum(int(recs[o].get("dropped") or 0) for o in done),
                     "facts_total": nraw, "facts_per_session": dist(list(fps.values())), "at_cap_30": sum(1 for v in fps.values() if v >= 30), "zero_facts": sum(1 for v in fps.values() if v == 0),
                     "spearman_chars_vs_facts": spearman([rows1[o]["chars"] for o in fps], [fps[o] for o in fps]),
                     "quote_check_raw": {"verbatim_in_cited_round": a_raw, "rate": (a_raw / nraw) if nraw else None, "wilson_low": lo_r, "wilson_high": hi_r},
                     "quote_check_repaired": {"grounded_after_repair": a_rep, "rate": (a_rep / nraw) if nraw else None, "wilson_low": lo_p, "wilson_high": hi_p, "classes": dict(g)},
                     "author_field": {"user": au.get("user", 0), "assistant": au.get("assistant", 0), "missing": nraw - au.get("user", 0) - au.get("assistant", 0)},
                     "user_share_of_facts": (au.get("user", 0) / float(nraw)) if nraw else None,
                     "author_filled_from_speaker": sum(1 for f in repaired if f.get("author_filled")), "author_unknown_after_fill": sum(1 for f in repaired if f.get("author") == "unknown"),
                     "event_time_present_share": mean(1.0 if f.get("event_time") else 0.0 for f in raw_facts) if raw_facts else None,
                     "quotes_over_12_words": sum(1 for f in raw_facts if len(str(f.get("quote", "")).split()) > 12)}
    # ---------------- 2. cost and time by length stratum; long sessions flagged; tokens first
    def col(ids, k):
        return [recs[o].get(k) for o in ids if recs[o].get(k) is not None]
    rnd = [o for o in called if rows1[o]["kind"] == "random"]
    lng = [o for o in called if rows1[o]["kind"] == "long"]
    cost = {"all_random": {k: dist(col(rnd, k)) for k in ("seconds", "total_input_tokens", "input_no_overhead", "output_tokens", "cost_usd")},
            "long_sessions_flagged": {k: dist(col(lng, k)) for k in ("seconds", "total_input_tokens", "input_no_overhead", "output_tokens", "cost_usd")},
            "by_stratum_random": {str(b): {k: dist(col([o for o in rnd if rows1[o]["stratum"] == b], k)) for k in ("seconds", "total_input_tokens", "output_tokens")} for b in range(1, 6)},
            "note": "the cost_usd figure follows the cache state of the prompt and is not used for comparisons; token counts are"}
    res["cost_and_time"] = cost
    ext = {"basis": "the %d random (not flagged long) sessions, equal-share whole-pool length strata; 95%% interval by resampling within strata; the long sessions are reported separately" % len(rnd), "calls": {}}
    for name in ("seconds", "total_input_tokens", "input_no_overhead", "output_tokens"):
        vb: Dict[int, List[float]] = {}
        for o in rnd:
            v = recs[o].get(name)
            if v is not None:
                vb.setdefault(rows1[o]["stratum"], []).append(float(v))
        if vb:
            for n in CAL:
                ext["calls"].setdefault(str(n), {})[name] = extrapolate(vb, n)
    wall = run_meta.get("wall_seconds")
    ext["observed_wall_seconds_per_call"] = (wall / len(called)) if (wall and called) else None
    if ext["observed_wall_seconds_per_call"]:
        for n in CAL:
            ext["calls"].setdefault(str(n), {})["wall_hours_at_observed_throughput"] = ext["observed_wall_seconds_per_call"] * n / 3600.0
    ext["concurrency_in_run"] = run_meta.get("concurrency")
    res["extrapolation_refreshed"] = ext
    # ---------------- 3. the 1,000-session pool
    cap30: Dict[str, List[Dict[str, Any]]] = {}
    for o in rows0:
        p = os.path.join(in_root, "stage_x0b", o + ".json")
        if os.path.exists(p):
            cap30[o] = L(p)["facts"]
    for o in rows0:
        p = os.path.join(in_root, "stage_x0c", "S", o + ".json")
        if o not in cap30 and os.path.exists(p):
            cap30[o] = L(p)["facts"]
    pool_facts_raw: List[Dict[str, Any]] = []
    for o in rows0:
        fs = cap30[o] if o in cap30 else L(os.path.join(in_root, "stage_x0", o + ".json"))["facts"]
        pool_facts_raw += [dict(f, session_id=o, src="x0_cap30" if o in cap30 else "x0_cap12") for f in fs]
    pool_facts_raw += [dict(f, src="x1") for f in raw_facts]
    pool_rep = [repair_fact(f, groups[f["session_id"]]) for f in pool_facts_raw]
    gold_x0 = [o for o, r in rows0.items() if r["group"] != "random_non_gold"]
    capped_gold12 = [o for o in gold_x0 if o not in cap30 and len(L(os.path.join(in_root, "stage_x0", o + ".json"))["facts"]) >= 12]
    res["pool"] = {"sessions": len(rows0) + len(rows1), "x0_sessions": len(rows0), "x0_with_cap30_output": len(cap30), "x0_cap12_only": len(rows0) - len(cap30),
                   "x1_sessions_done": len(done), "facts_raw": len(pool_facts_raw), "facts_kept_after_repair": sum(1 for f in pool_rep if kept(f)),
                   "facts_dropped_paraphrase_or_ungrounded": sum(1 for f in pool_rep if not kept(f)),
                   "gold_sessions_in_pool": len(gold_x0), "gold_sessions_at_cap12_without_cap30_output": len(capped_gold12),
                   "mean_facts_per_session_x0_part": mean(len([f for f in pool_rep if f["session_id"] == o]) for o in list(rows0)[:200]),
                   "mean_facts_per_session_x1_part": (len([f for f in pool_rep if f["src"] == "x1"]) / float(max(1, len(done)))),
                   "mismatch_note": "the gold sessions' facts come mostly from cap 12 (fewer facts per session than the 800 cap-30 sessions), which gives the distractors more chances: conservative for the test"}
    # query sets
    weak = k0["weak_list_questions"]
    low_other = k0["low_overlap_other_questions"]
    ku = k0["knowledge_update_questions"]
    gold_sess = {x: {o for o, r in rows0.items() if x in r["questions"]} for x in weak + low_other + ku}
    queries = {x: qmap[x].question for x in gold_sess}

    def build(session_filter, repaired_only: bool):
        fd, fd2, fo = [], [], []
        for f in pool_rep:
            if f["session_id"] not in session_filter or (repaired_only and not kept(f)):
                continue
            fd.append(stmt(f))
            fd2.append(stmt_plus(f))
            fo.append(f["session_id"])
        rd, ro = [], []
        for o in session_filter:
            for gi in groups[o]:
                rd.append(tokens(" ".join("%s: %s" % (t["role"], t["content"]) for t in gi)))
                ro.append(o)
        return (fd, fd2), fo, rd, ro

    all_ids = set(rows0) | set(rows1)
    toy_ids = set(rows0)
    out_tests: Dict[str, Any] = {}
    for label, ids in (("pool_1000", all_ids), ("toy_200", toy_ids)):
        for rep_only, rname in ((True, "repaired_pool"), (False, "raw_pool")):
            fdocs, fo, rd, ro = build(ids, rep_only)
            per = evaluate({x: queries[x] for x in gold_sess}, gold_sess, fdocs, fo, rd, ro, None)
            out_tests["%s|%s" % (label, rname)] = {"weak_list": tally({x: per[x] for x in weak}), "low_overlap_other": tally({x: per[x] for x in low_other}),
                                                   "knowledge_update": tally({x: per[x] for x in ku}), "per_question": per}
    res["tests"] = out_tests
    # wrong-query control (repaired 1,000 pool, primary variant): each weak question's gold looked up with the NEXT weak question's text
    fdocs, fo, rd, ro = build(all_ids, True)
    wq = {x: queries[weak[(i + 1) % len(weak)]] for i, x in enumerate(weak)}
    perw = evaluate(wq, {x: gold_sess[x] for x in weak}, fdocs, fo, rd, ro, None)
    res["wrong_query_control"] = {"facts_top10": sum(1 for v in perw.values() if v["facts_top10"]), "raw_top10": sum(1 for v in perw.values() if v["raw_top10"]), "n": len(perw)}
    prim = out_tests["pool_1000|repaired_pool"]["weak_list"]
    met = prim["facts"] >= prim["raw"] and prim["facts"] >= PRIMARY_MIN_HITS
    res["decision"] = {"rule": "facts top-10 hits at least the raw control's AND at least 6 of 11 (primary variant: statement; repaired pool; 1,000 sessions)",
                       "facts_hits": prim["facts"], "raw_hits": prim["raw"], "n": prim["n"], "min_hits": PRIMARY_MIN_HITS, "verdict": "ASK_FOR_6100" if met else "STOP_BEFORE_VOLUME",
                       "also_on_raw_pool": {"facts": out_tests["pool_1000|raw_pool"]["weak_list"]["facts"], "raw": out_tests["pool_1000|raw_pool"]["weak_list"]["raw"]}}
    json.dump(res, open(out_json, "w"), indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main(*sys.argv[1:7])
