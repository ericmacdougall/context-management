"""Score STAGE-X0 (pilot extraction canary; no model call, no judge). Rules: STAGE-X0.md (committed before the calls).
Inputs: runner records in/stage_x0/<opaque session id>.json (+ _run.json), the session files and key written by emlab.stage_x0_prep.
Failures (parse failure, rejection, call error, missing record) are counted and stay in every denominator; nothing is retried."""
from __future__ import annotations

import json
import math
import os
import random
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence

from . import provenance_x04 as prov
from .retrievers import tokens

SEED = 16096
FACT_CAP = 12
CAL = (6100, 22500)
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_WS = re.compile(r"\s+")


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def dist(xs) -> Optional[Dict[str, float]]:
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    q = lambda p: v[min(len(v) - 1, int(math.floor(p * (len(v) - 1))))]
    return {"n": len(v), "mean": sum(v) / len(v), "median": q(0.5), "p90": q(0.9), "max": v[-1], "min": v[0], "sum": sum(v)}


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) < 3:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0 + 1
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return None if sx == 0 or sy == 0 else sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy)


def norm_text(s: str) -> str:
    return _WS.sub(" ", re.sub(r"[^\w\s.]", " ", str(s).lower())).strip()


_KEY_STOP = frozenset("a an and are as at be but by for if in into is it no not of on or such that the their then there these they this to was will with you your i my me we our".split())


def answer_match(gold: str, text: str) -> Dict[str, bool]:
    """CRUDE heuristic, labelled as such. string: the normalised gold string occurs in the normalised text. key: if the gold answer has numbers,
    every number occurs among the text's numbers; otherwise at least 60 percent of the gold's content tokens occur in the text and at least
    one matched token has 5 or more characters."""
    g, t = norm_text(gold), norm_text(text)
    s = bool(g) and g in t
    gn = [x.replace(",", "") for x in _NUM.findall(str(gold))]
    tn = {x.replace(",", "") for x in _NUM.findall(str(text))}
    if gn:
        k = all(x in tn for x in gn)
    else:
        gt = [w for w in re.findall(r"[a-z0-9]+", g) if w not in _KEY_STOP and len(w) > 1]
        tt = set(re.findall(r"[a-z0-9]+", t))
        hit = [w for w in gt if w in tt]
        k = bool(gt) and len(hit) / len(gt) >= 0.6 and any(len(w) >= 5 for w in hit)
    return {"string": s, "key": s or k}


def bm25_rank(docs: List[List[str]], query: List[str], k1: float = 1.2, b: float = 0.75) -> List[int]:
    n = len(docs)
    avg = (sum(len(d) for d in docs) / n) if n else 0.0
    df: Counter = Counter()
    tfs = []
    for d in docs:
        c = Counter(d)
        tfs.append(c)
        df.update(c.keys())
    scores = []
    for i, d in enumerate(docs):
        s = 0.0
        for w in set(query):
            f = tfs[i].get(w, 0)
            if f:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(d) / (avg or 1.0)))
        scores.append(s)
    return sorted(range(n), key=lambda i: (-scores[i], i))


def strat_mean(vals_by_bin: Dict[int, List[float]], weights: Dict[int, float]) -> float:
    return sum(weights[b] * (sum(v) / len(v)) for b, v in vals_by_bin.items() if v)


def extrapolate(vals_by_bin: Dict[int, List[float]], n_calls: int, boots: int = 2000, seed: int = SEED) -> Dict[str, float]:
    """Stratified estimator over the equal-share whole-pool length strata; 95% interval by resampling within strata."""
    w = {b: 1.0 / len(vals_by_bin) for b in vals_by_bin}
    est = strat_mean(vals_by_bin, w)
    rng = random.Random(seed)
    draws = []
    for _ in range(boots):
        res_b = {}
        for b_, v_ in vals_by_bin.items():
            if v_:
                res_b[b_] = [v_[rng.randrange(len(v_))] for _ in v_]
        draws.append(strat_mean(res_b, w))
    draws.sort()
    return {"per_call": est, "total": est * n_calls, "total_low": draws[int(0.025 * boots)] * n_calls, "total_high": draws[int(0.975 * boots) - 1] * n_calls}


def main(dataset, art, sets_path, out_root, in_root, out_json):
    from .build_rag_contexts import load_common
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qmap = {q.qid: q for q in qs}
    key = json.load(open(os.path.join(out_root, "stage_x0_key", "selection_key.json")))
    prep = json.load(open(os.path.join(out_root, "stage_x0", "prep_report.json")))
    edges = prep["strata_edges_chars"]
    srows = {r["opaque_session_id"]: r for r in key["sessions"]}
    sess_files = {o: json.load(open(os.path.join(out_root, "stage_x0", "sessions", o + ".json"), encoding="utf-8")) for o in srows}
    run_meta_p = os.path.join(in_root, "stage_x0", "_run.json")
    run_meta = json.load(open(run_meta_p)) if os.path.exists(run_meta_p) else {}
    recs: Dict[str, Dict[str, Any]] = {}
    for o in srows:
        p = os.path.join(in_root, "stage_x0", o + ".json")
        recs[o] = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"session_id": o, "status": "missing"}
    binof = lambda c: sum(1 for e in edges if c > e)
    OVERHEAD = 10500  # fixed harness overhead per call stated by the runner (about 10.5k tokens); used only for the DERIVED estimate
    for r in recs.values():
        if r.get("status") not in ("missing", "not_run", None):
            r["total_input_tokens"] = sum(int(r.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            r["derived_input_tokens_no_overhead"] = max(0, r["total_input_tokens"] - OVERHEAD)
    res: Dict[str, Any] = {"pilot": True, "confirmatory": False, "n_sessions": len(srows), "run_meta": run_meta}
    status = Counter(r.get("status") for r in recs.values())
    res["status_counts"] = dict(status)
    done = [o for o, r in recs.items() if r.get("status") in ("ok", "repaired")]
    called = [o for o, r in recs.items() if r.get("status") not in ("missing", "not_run", None)]
    res["calls_recorded"] = len(called)

    # ---------- 1. time, tokens, cost (every called record, failures included) + per length bin
    def col(name, ids):
        return [recs[o].get(name) for o in ids]
    perf = {k: dist(col(k, called)) for k in ("seconds", "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "total_input_tokens", "derived_input_tokens_no_overhead", "cost_usd")}
    by_bin: Dict[int, List[str]] = {}
    for o in called:
        by_bin.setdefault(binof(srows[o]["chars"]), []).append(o)
    res["per_call"] = {"all_called": perf,
                       "by_length_stratum": {str(b): {"n": len(ids), "chars_mean": mean(srows[o]["chars"] for o in ids), "seconds": dist(col("seconds", ids)),
                                                      "input_tokens": dist(col("input_tokens", ids)), "total_input_tokens": dist(col("total_input_tokens", ids)), "output_tokens": dist(col("output_tokens", ids)),
                                                      "cost_usd": dist(col("cost_usd", ids))} for b, ids in sorted(by_bin.items())},
                       "spearman_chars_vs_input_tokens": spearman([srows[o]["chars"] for o in called if recs[o].get("input_tokens") is not None], [recs[o]["input_tokens"] for o in called if recs[o].get("input_tokens") is not None]),
                       "spearman_chars_vs_seconds": spearman([srows[o]["chars"] for o in called if recs[o].get("seconds") is not None], [recs[o]["seconds"] for o in called if recs[o].get("seconds") is not None])}
    # extrapolation from the RANDOM non-gold sample only (equal-share whole-pool length strata)
    rnd = [o for o in called if srows[o]["group"] == "random_non_gold"]
    ext: Dict[str, Any] = {"basis": "random non-gold sample, %d called sessions of %d; stratified estimator over equal-share whole-pool length quintiles; 95%% interval by resampling within strata" % (len(rnd), prep["n_random"]), "calls": {}}
    for name in ("seconds", "input_tokens", "total_input_tokens", "derived_input_tokens_no_overhead", "output_tokens", "cost_usd"):
        vb: Dict[int, List[float]] = {}
        for o in rnd:
            v = recs[o].get(name)
            if v is not None:
                vb.setdefault(binof(srows[o]["chars"]), []).append(float(v))
        for n in CAL:
            if vb:
                ext["calls"].setdefault(str(n), {})[name] = extrapolate(vb, n)
    wall = run_meta.get("wall_seconds")
    ext["concurrency_in_run"] = run_meta.get("concurrency")
    ext["observed_wall_seconds_per_call"] = (wall / len(called)) if (wall and called) else None
    if ext["observed_wall_seconds_per_call"]:
        for n in CAL:
            ext["calls"].setdefault(str(n), {})["wall_hours_at_observed_throughput"] = ext["observed_wall_seconds_per_call"] * n / 3600.0
    for n in CAL:
        c = ext["calls"].get(str(n), {}).get("seconds")
        if c:
            ext["calls"][str(n)]["sequential_hours"] = c["total"] / 3600.0
    # DERIVED estimate (labelled): cost without the fixed overhead. OLS of the notional cost on (total input tokens, output tokens) over all called
    # records (no intercept), then the fitted cost with the input reduced by the overhead. The runner's cost_usd is not itemised by token kind, so this is a model.
    pts = [(float(recs[o]["total_input_tokens"]), float(recs[o]["output_tokens"] or 0), float(recs[o]["cost_usd"])) for o in called if recs[o].get("cost_usd") is not None]
    if len(pts) >= 5:
        a11 = sum(x * x for x, _, _ in pts); a12 = sum(x * y for x, y, _ in pts); a22 = sum(y * y for _, y, _ in pts)
        b1 = sum(x * c for x, _, c in pts); b2 = sum(y * c for _, y, c in pts)
        det = a11 * a22 - a12 * a12
        if det:
            ci = (b1 * a22 - b2 * a12) / det
            co = (a11 * b2 - a12 * b1) / det
            ss_res = sum((c - ci * x - co * y) ** 2 for x, y, c in pts)
            mc = sum(c for _, _, c in pts) / len(pts)
            ss_tot = sum((c - mc) ** 2 for _, _, c in pts)
            vb_d: Dict[int, List[float]] = {}
            for o in rnd:
                if recs[o].get("cost_usd") is not None:
                    vb_d.setdefault(binof(srows[o]["chars"]), []).append(max(0.0, ci * recs[o]["derived_input_tokens_no_overhead"] + co * float(recs[o]["output_tokens"] or 0)))
            ext["derived_cost_no_overhead"] = {"label": "DERIVED ESTIMATE, not a measurement: OLS of notional cost on (total input tokens, output tokens), then input reduced by the fixed 10,500-token overhead",
                                              "cost_per_input_token": ci, "cost_per_output_token": co, "r_squared": (1 - ss_res / ss_tot) if ss_tot else None,
                                              "calls": {str(n): extrapolate(vb_d, n) for n in CAL} if vb_d else None}
    res["extrapolation"] = ext

    # ---------- 2. facts per session against length; cap
    fps = {o: len(recs[o].get("facts") or []) for o in done}
    res["facts_per_session"] = {"dist_all_done": dist(list(fps.values())), "cap": FACT_CAP, "at_cap_share": mean(1.0 if v >= FACT_CAP else 0.0 for v in fps.values()) if fps else None,
                                "zero_facts_share": mean(1.0 if v == 0 else 0.0 for v in fps.values()) if fps else None,
                                "spearman_vs_chars": spearman([srows[o]["chars"] for o in fps], [fps[o] for o in fps]),
                                "by_length_stratum": {str(b): {"n": len([o for o in ids if o in fps]), "mean_facts": mean(fps[o] for o in ids if o in fps), "at_cap_share": mean(1.0 if fps[o] >= FACT_CAP else 0.0 for o in ids if o in fps)} for b, ids in sorted(by_bin.items())},
                                "by_group": {g: dist([fps[o] for o in fps if srows[o]["group"] == g]) for g in ("weak_list", "low_overlap", "knowledge_update", "random_non_gold")},
                                "note": "the extraction prompt caps output at 12 facts per session, so long sessions cannot yield more; the share at the cap says how binding it is"}
    cov = {"capped": [], "uncapped": []}
    def round_cover(o):
        groups = prov.rounds_of_turns(sess_files[o]["turns"])
        cited = sorted({f.get("round") for f in recs[o].get("facts") or [] if isinstance(f.get("round"), int) and 0 <= f.get("round") < len(groups)})
        n = len(groups)
        last_third = [r for r in range(n) if r >= (2 * n) // 3]
        return {"rounds": n, "cited_rounds": len(cited), "share_rounds_cited": len(cited) / n if n else None,
                "last_third_cited": sum(1 for r in cited if r in set(last_third)), "last_third_n": len(last_third), "max_cited_fraction": (max(cited) + 1) / n if cited and n else None}
    for o in fps:
        (cov["capped" if fps[o] >= FACT_CAP else "uncapped"]).append(round_cover(o))
    res["cap_coverage_no_model"] = {name: {"n": len(v), "mean_rounds": mean(x["rounds"] for x in v), "mean_share_rounds_cited": mean(x["share_rounds_cited"] for x in v),
                                         "mean_max_cited_fraction": mean(x["max_cited_fraction"] for x in v),
                                         "share_with_any_fact_in_last_third": mean(1.0 if x["last_third_cited"] > 0 else 0.0 for x in v)} for name, v in cov.items()}
    res["cap_coverage_no_model"]["note"] = "facts lost to the cap cannot be counted from this run; this only shows whether facts of capped sessions reach the end of the session"
    res["dropped_entries"] = sum(int(recs[o].get("dropped") or 0) for o in done)

    # ---------- 3. quote and speaker check (vendored phasef checker, commit 27a4c64)
    sess = {o: prov.rounds_of_turns(sess_files[o]["turns"]) for o in sess_files}
    all_facts = []
    for o in done:
        for f in recs[o].get("facts") or []:
            all_facts.append(dict(f, session_id=o))
    rows = prov.check_all(all_facts, sess) if all_facts else []
    a_pass = sum(1 for r in rows if r["a"] == prov.PASS)
    b = Counter(r["b"] for r in rows)
    b_among_a = Counter(r["b"] for r in rows if r["a"] == prov.PASS)
    lo, hi = prov.wilson(a_pass, len(rows)) if rows else (0.0, 0.0)
    by_s: Dict[str, List[bool]] = {}
    for f, r in zip(all_facts, rows):
        by_s.setdefault(f["session_id"], []).append(r["a"] == prov.PASS)
    full = sum(1 for v in by_s.values() if all(v))
    res["provenance"] = {"checker": "phasef commit 27a4c64 phasef/provenance.py, vendored unchanged (sha256 adde67b4...)", "facts": len(rows), "sessions_with_facts": len(by_s),
                         "a_quote_verbatim_in_cited_round": {"pass": a_pass, "rate": (a_pass / len(rows)) if rows else None, "wilson_low": lo, "wilson_high": hi},
                         "b_speaker_all_facts": dict(b), "b_speaker_among_a_pass": dict(b_among_a), "ambiguous_counted_separately": b.get("ambiguous", 0),
                         "sessions_all_quotes_pass": {"n": full, "of": len(by_s)},
                         "controls": prov.controls(all_facts, sess, rows) if rows else None,
                         "by_group_a_rate": {g: mean(1.0 if r["a"] == prov.PASS else 0.0 for f, r in zip(all_facts, rows) if srows[f["session_id"]]["group"] == g) for g in ("weak_list", "low_overlap", "knowledge_update", "random_non_gold")},
                         "event_time_present_share": mean(1.0 if f.get("event_time") else 0.0 for f in all_facts) if all_facts else None,
                         "schema_complete_share": mean(1.0 if all(k in f for k in ("subject", "slot", "predicate", "object", "quote", "author", "round")) else 0.0 for f in all_facts) if all_facts else None,
                         "quote_longer_than_12_words": sum(1 for f in all_facts if len(str(f.get("quote", "")).split()) > 12)}

    # ---------- 4. gold-answer heuristic on facts (crude), split by group
    def fact_text(f):
        return " ".join(str(f.get(k, "")) for k in ("quote", "subject", "predicate", "object"))
    gq: Dict[str, Dict[str, Any]] = {}
    for g, qids in (("weak_list", key["weak_list_questions"]), ("low_overlap", key["low_overlap_other_questions"]), ("knowledge_update", key["knowledge_update_questions"])):
        for x in qids:
            q = qmap[x]
            sids = [o for o, r in srows.items() if x in r["questions"]]
            ans = str(q.answer)
            raw_hit = {"string": False, "key": False}
            fact_hit = {"string": False, "key": False}
            n_called = 0
            for o in sids:
                full_text = " ".join(t["content"] for t in sess_files[o]["turns"])
                m = answer_match(ans, full_text)
                raw_hit = {k: raw_hit[k] or m[k] for k in m}
                if o in done:
                    n_called += 1
                    for f in recs[o].get("facts") or []:
                        m2 = answer_match(ans, fact_text(f))
                        fact_hit = {k: fact_hit[k] or m2[k] for k in m2}
            gq[x] = {"group": g, "gold_sessions": len(sids), "gold_sessions_extracted": n_called, "raw_session_text_contains": raw_hit, "any_fact_contains": fact_hit}
    res["gold_answer_heuristic"] = {"label": "CRUDE substring and token heuristic, not a judge; 'key' = string match or all numbers or 60% of content tokens",
                                    "by_group": {g: {"questions": sum(1 for v in gq.values() if v["group"] == g),
                                                     "raw_text_string": sum(1 for v in gq.values() if v["group"] == g and v["raw_session_text_contains"]["string"]),
                                                     "raw_text_key": sum(1 for v in gq.values() if v["group"] == g and v["raw_session_text_contains"]["key"]),
                                                     "fact_string": sum(1 for v in gq.values() if v["group"] == g and v["any_fact_contains"]["string"]),
                                                     "fact_key": sum(1 for v in gq.values() if v["group"] == g and v["any_fact_contains"]["key"]),
                                                     "both_raw_and_fact_key": sum(1 for v in gq.values() if v["group"] == g and v["any_fact_contains"]["key"] and v["raw_session_text_contains"]["key"])} for g in ("weak_list", "low_overlap", "knowledge_update")},
                                    "per_question": gq}

    # ---------- 5. toy lookup for the weak-list questions (200-session pool; NOT the whole pool)
    fact_docs, fact_owner = [], []
    fact_docs2 = []
    for o in done:
        for f in recs[o].get("facts") or []:
            fact_docs.append(tokens(" ".join(str(f.get(k, "")) for k in ("subject", "predicate", "object"))))
            fact_docs2.append(tokens(fact_text(f) + " " + " ".join(f.get("cue_keys") or [])))
            fact_owner.append(o)
    round_docs, round_owner = [], []
    for o, groups in sess.items():
        for g in groups:
            round_docs.append(tokens(" ".join("%s: %s" % (t["role"], t["content"]) for t in g)))
            round_owner.append(o)
    look: Dict[str, Any] = {}
    for x in key["weak_list_questions"]:
        gold_o = {o for o, r in srows.items() if x in r["questions"]}
        qt = tokens(qmap[x].question)
        entry: Dict[str, Any] = {"gold_sessions": len(gold_o), "gold_sessions_with_facts": len([o for o in gold_o if o in fps and fps[o] > 0])}
        for name, docs, owner in (("facts_statement", fact_docs, fact_owner), ("facts_statement_quote_cues", fact_docs2, fact_owner), ("raw_rounds_control", round_docs, round_owner)):
            order = bm25_rank(docs, qt) if docs else []
            rank = next((i + 1 for i, d in enumerate(order) if owner[d] in gold_o), None)
            entry[name] = {"best_gold_rank": rank, "top10_has_gold": bool(rank and rank <= 10), "n_docs": len(docs)}
        look[x] = entry
    res["weak_list_toy_lookup"] = {"label": "toy pool of the 200 extracted sessions, NOT the whole pool; the fact text is findable when present; control = BM25 over raw round text of the same 200 sessions; BM25 k1 1.2 b 0.75, registered tokeniser",
                                   "questions": len(look), "top10_facts": sum(1 for v in look.values() if v["facts_statement"]["top10_has_gold"]),
                                   "top10_facts_with_quote_cues": sum(1 for v in look.values() if v["facts_statement_quote_cues"]["top10_has_gold"]),
                                   "top10_raw_rounds_control": sum(1 for v in look.values() if v["raw_rounds_control"]["top10_has_gold"]), "per_question": look}
    json.dump(res, open(out_json, "w"), indent=1)
    print("done")


if __name__ == "__main__":
    main(*sys.argv[1:7])
