"""Score STAGE-X2 (no model call except the LOCAL E5-small embedding model on CPU). Rules, thresholds and the decision criteria: STAGE-X2.md (committed before the calls).
Enlarged pool = the 1,000 sessions of Stage X1 + the new gold sessions of Stage X2; all pilot questions with retrieval gold are evaluated."""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

try:
    import numpy as np
except ImportError:  # local checkout has no numpy; runs on t2-dev
    np = None

from . import provenance_x04 as prov
from .metrics import clustered_paired_bootstrap
from .retrievers import tokens
from .stage_x0_score import bm25_rank
from .stage_x1_score import kept, repair_fact, stmt
from .timeparse import in_range, parse_question_range
from .xp_x1_dense import Embedder, rrf_merge, session_order

SEED = 16096
FMT = "%Y/%m/%d (%a) %H:%M"
ARMS = ("rrf_raw", "rrf_facts", "fused_facts_raw")  # control, facts, one fused list
CONTROL = "rrf_raw"
DATE_SLACK_DAYS = 3  # the registered date arm widens the parsed range by this many days on both sides
D1_MIN_MEAN, D1_MIN_LOWER = -0.02, -0.05
D2_MIN_MEAN, D2_MIN_QUESTIONS = 0.15, 3
D3_MAX_WRONG = 0.05
PREF_STOP = frozenset("a an and are as at be but by for if in into is it no not of on or such that the their then there these they this to was will with would user may might also".split())


def in_window(day: date, rng, slack: int = DATE_SLACK_DAYS) -> bool:
    return (rng[0] - timedelta(days=slack)) <= day <= (rng[1] + timedelta(days=slack))


def view(order: List[Any], owner, gold: set, ks=(10, 20, 50)) -> Dict[str, Any]:
    """Item-level and session-level (best position per session) metrics of one ranked list for one question."""
    items = [owner(d) for d in order]
    first = next((i + 1 for i, s in enumerate(items) if s in gold), None)
    so = session_order(order, owner)
    pos = {s: i + 1 for i, s in enumerate(so)}
    gr = sorted(pos[s] for s in gold if s in pos)
    out = {"item_hit10": bool(first and first <= 10), "best_gold_session_rank": gr[0] if gr else None, "n_gold": len(gold)}
    for k in ks:
        out["sess_any_%d" % k] = bool(gr and gr[0] <= k)
        out["sess_all_%d" % k] = bool(len(gr) == len(gold) and gr[-1] <= k)
        out["sess_frac_%d" % k] = sum(1 for r in gr if r <= k) / float(len(gold))
    return out


def paired(rows_ctrl: Dict[str, float], rows_arm: Dict[str, float], clusters: Dict[str, str]) -> Dict[str, Any]:
    qids = sorted(rows_ctrl)
    if not qids:
        return {"n": 0}
    r = clustered_paired_bootstrap([(clusters[x], float(rows_ctrl[x]), float(rows_arm[x])) for x in qids], n_boot=2000, seed=SEED)
    return {"n": len(qids), "mean_ctrl": sum(rows_ctrl[x] for x in qids) / len(qids), "mean_arm": sum(rows_arm[x] for x in qids) / len(qids), "mean_diff": r["mean_diff"],
            "ci_low": r["ci_low"], "ci_high": r["ci_high"], "clusters": int(r["effective_n"]), "wins": sum(1 for x in qids if rows_arm[x] > rows_ctrl[x]), "losses": sum(1 for x in qids if rows_arm[x] < rows_ctrl[x])}


def main(dataset, art, sets_path, out_root, in_root, model_dir, out_json):
    from .build_rag_contexts import load_common
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    qmap = {q.qid: q for q in qs}
    H = ctx.hashes
    L = lambda p: json.load(open(p, encoding="utf-8"))
    k0, k1, k2 = (L(os.path.join(out_root, d, "selection_key.json")) for d in ("stage_x0_key", "stage_x1_key", "stage_x2_key"))
    rows0 = {r["opaque_session_id"]: r for r in k0["sessions"]}
    rows1 = {r["opaque_session_id"]: r for r in k1["sessions"]}
    rows2 = {r["opaque_session_id"]: r for r in k2["sessions"]}
    new_gold = {o: r for o, r in rows2.items() if r["kind"] == "new_gold"}
    hash_to_o = {r["content_hash"]: o for o, r in list(rows0.items()) + list(rows1.items()) + list(new_gold.items())}
    sfile = {}
    for o in rows0:
        sfile[o] = L(os.path.join(out_root, "stage_x0", "sessions", o + ".json"))
    for o in rows1:
        sfile[o] = L(os.path.join(out_root, "stage_x1", "sessions", o + ".json"))
    for o in new_gold:
        sfile[o] = L(os.path.join(out_root, "stage_x2", "sessions", o + ".json"))
    groups = {o: prov.rounds_of_turns(f["turns"]) for o, f in sfile.items()}
    # ---- fact pool: X0 (cap-30 output where one exists, else cap 12; a Stage X2 cap-30 re-extraction replaces the cap-12 output), X1, X2 new gold
    def facts_of(path):
        return L(path)["facts"] if os.path.exists(path) else None
    raw_facts = []
    n_reextracted = 0
    for o in rows0:
        fs = facts_of(os.path.join(in_root, "stage_x2", o + ".json")) if (o in rows2 and rows2[o]["kind"] == "reextract_cap30") else None
        if fs is not None:
            n_reextracted += 1
        for p in ([os.path.join(in_root, "stage_x0b", o + ".json"), os.path.join(in_root, "stage_x0c", "S", o + ".json"), os.path.join(in_root, "stage_x0", o + ".json")] if fs is None else []):
            fs = facts_of(p)
            if fs is not None:
                break
        raw_facts += [dict(f, session_id=o) for f in fs]
    for o in rows1:
        raw_facts += [dict(f, session_id=o) for f in facts_of(os.path.join(in_root, "stage_x1", o + ".json"))]
    n_new_done = 0
    for o in new_gold:
        fs = facts_of(os.path.join(in_root, "stage_x2", o + ".json"))
        if fs is not None:
            n_new_done += 1
            raw_facts += [dict(f, session_id=o) for f in fs]
    facts = [f for f in (repair_fact(f, groups[f["session_id"]]) for f in raw_facts) if kept(f)]
    sess_ids = list(sfile)
    rounds = [(o, i, " ".join("%s: %s" % (t["role"], t["content"]) for t in g)) for o in sess_ids for i, g in enumerate(groups[o])]
    # ---- questions, strata, gold (opaque session ids of gold contents)
    gold = {q.qid: {hash_to_o[H[g]] for g in q.gold_session_ids if H[g] in hash_to_o} for q in qs}
    complete = [x for x in gold if len(gold[x]) == len({H[g] for g in qmap[x].gold_session_ids})]
    weak, low_other, ku = k0["weak_list_questions"], k0["low_overlap_other_questions"], k0["knowledge_update_questions"]
    strata = {"weak_list": [x for x in weak if x in complete], "low_overlap_other": [x for x in low_other if x in complete], "knowledge_update": [x for x in ku if x in complete]}
    strata["rest"] = [x for x in complete if x not in set(sum(strata.values(), []))]
    types: Dict[str, List[str]] = {}
    for x in complete:
        types.setdefault(qmap[x].qtype, []).append(x)
    emb = Embedder(model_dir)
    qvec = {x: v for x, v in zip(complete, emb.embed(["query: " + qmap[x].question for x in complete]))}
    fv = emb.embed(["passage: " + " ".join(str(f.get(k, "")) for k in ("subject", "predicate", "object")) for f in facts])
    ch_text, ch_round = [], []
    for ri, (_, _, text) in enumerate(rounds):
        for c in emb.chunks(text):
            ch_text.append("passage: " + c)
            ch_round.append(ri)
    cv = emb.embed(ch_text)
    ch_round = np.array(ch_round)
    f_owner = np.array([f["session_id"] for f in facts])
    r_owner = np.array([o for o, _, _ in rounds])
    f_tok = [stmt(f) for f in facts]
    r_tok = [tokens(t) for _, _, t in rounds]
    owner = lambda d: (f_owner[d[1]] if d[0] == "f" else r_owner[d[1]])

    def lists(text_q: str, qv: np.ndarray, ids: set) -> Dict[str, List[Any]]:
        fi = np.array([i for i in range(len(facts)) if f_owner[i] in ids])
        ri = np.array([i for i in range(len(rounds)) if r_owner[i] in ids])
        bf = [("f", int(fi[j])) for j in bm25_rank([f_tok[i] for i in fi], tokens(text_q))]
        df = [("f", int(fi[j])) for j in np.argsort(-(fv[fi] @ qv), kind="stable")]
        br = [("r", int(ri[j])) for j in bm25_rank([r_tok[i] for i in ri], tokens(text_q))]
        rs = np.full(len(rounds), -9.0, dtype="float32")
        np.maximum.at(rs, ch_round, cv @ qv)
        dr = [("r", int(ri[j])) for j in np.argsort(-rs[ri], kind="stable")]
        rf, rr = rrf_merge([bf, df]), rrf_merge([br, dr])
        return {"rrf_facts": rf, "rrf_raw": rr, "fused_facts_raw": rrf_merge([rf, rr])}

    res: Dict[str, Any] = {"pilot": True, "confirmatory": False, "pool": {"sessions": len(sess_ids), "facts": len(facts), "rounds": len(rounds), "chunks": len(ch_text),
                                                                           "new_gold_done": n_new_done, "new_gold_expected": len(new_gold), "reextracted_cap30_done": n_reextracted,
                                                                           "gold_sessions_in_pool": len({o for g in gold.values() for o in g}), "questions_evaluated": len(complete), "questions_total": len(qs)}}
    all_ids = set(sess_ids)
    toy_ids = set(rows0)
    per_full: Dict[str, Dict[str, Any]] = {}
    for x in complete:
        lst = lists(qmap[x].question, qvec[x], all_ids)
        per_full[x] = {a: view(lst[a], owner, gold[x]) for a in ARMS}
    # ---- primary and strata (session-level hit@10), paired against the control with cluster intervals
    def table(group: List[str], metric: str) -> Dict[str, Any]:
        return {a: paired({x: per_full[x][CONTROL][metric] for x in group}, {x: per_full[x][a][metric] for x in group}, ctx.clusters) for a in ARMS if a != CONTROL}
    prim = "sess_any_10"
    res["primary_sess_any_10"] = {"all": table(complete, prim)}
    res["by_stratum"] = {k: table(v, prim) for k, v in strata.items()}
    res["by_type"] = {k: table(v, prim) for k, v in types.items()}
    res["item_hit10_all"] = table(complete, "item_hit10")
    res["control_levels"] = {"all": sum(per_full[x][CONTROL][prim] for x in complete) / max(1, len(complete))}
    low = strata["weak_list"] + strata["low_overlap_other"]
    res["low_overlap_23"] = {"sess_any_10": table(low, prim), "n": len(low)}
    # ---- aggregation view (multi-session): all gold sessions within top K sessions beside any
    multi = [x for x in complete if qmap[x].qtype == "multi-session"]
    res["aggregation_multi_session"] = {"n": len(multi), "gold_sessions_mean": sum(len(gold[x]) for x in multi) / max(1, len(multi)),
                                        "by_arm": {a: {k: sum(per_full[x][a][k] for x in multi) / max(1, len(multi)) for k in ("sess_any_10", "sess_all_10", "sess_all_20", "sess_all_50", "sess_frac_10", "sess_frac_50")} for a in ARMS}}
    # ---- wrong-query control: each question's gold looked up with the text of the next question (cyclic over `complete`), session-level any@10
    wq = {}
    for i, x in enumerate(complete):
        y = complete[(i + 1) % len(complete)]
        if gold[x] & gold[y]:
            y = complete[(i + 2) % len(complete)]
        lst = lists(qmap[y].question, qvec[y], all_ids)
        wq[x] = {a: view(lst[a], owner, gold[x])["sess_any_10"] for a in ARMS}
    res["wrong_query_control"] = {a: sum(wq[x][a] for x in complete) / max(1, len(complete)) for a in ARMS}
    # ---- scaling: the questions whose gold is entirely in the Stage X0 200 sessions, at 200 sessions and at the full pool
    toy_q = [x for x in complete if gold[x] <= toy_ids]
    per_toy = {}
    for x in toy_q:
        lst = lists(qmap[x].question, qvec[x], toy_ids)
        per_toy[x] = {a: view(lst[a], owner, gold[x]) for a in ARMS}
    res["scaling_same_questions"] = {"n": len(toy_q), "pool_200": {a: sum(per_toy[x][a][prim] for x in toy_q) / max(1, len(toy_q)) for a in ARMS},
                                     "pool_full": {a: sum(per_full[x][a][prim] for x in toy_q) / max(1, len(toy_q)) for a in ARMS}}
    # ---- decision criteria D1 to D3 (arm B = fused_facts_raw and arm A = rrf_facts, each against the control)
    dec = {}
    for a in ("fused_facts_raw", "rrf_facts"):
        al = res["primary_sess_any_10"]["all"][a]
        lw = res["low_overlap_23"]["sess_any_10"][a]
        d1 = al["mean_diff"] >= D1_MIN_MEAN and al["ci_low"] >= D1_MIN_LOWER
        d2 = lw["mean_diff"] >= D2_MIN_MEAN and (lw["wins"] - lw["losses"]) >= D2_MIN_QUESTIONS
        d3 = res["wrong_query_control"][a] <= D3_MAX_WRONG
        dec[a] = {"D1_no_harm_all": d1, "D2_benefit_low_overlap": d2, "D3_wrong_query": d3, "pass_all": bool(d1 and d2 and d3)}
    res["decision"] = dec
    # ---- exploratory registered date arm: deterministic date range from the question text, hard filter of sessions by session date +/- slack
    qdate_dates = {}
    for x in complete:
        own = {}
        for t in qmap[x].turns:
            own.setdefault(H[t.session_id], t.date)
        qdate_dates[x] = own
    stored = {o: datetime.strptime(sfile[o]["date"], FMT) for o in sfile}
    hash_of = {o: r["content_hash"] for o, r in list(rows0.items()) + list(rows1.items()) + list(new_gold.items())}
    date_rows, parsed = {}, {}
    for x in complete:
        rng = parse_question_range(qmap[x].question, qmap[x].question_date)
        if rng is None:
            continue
        parsed[x] = rng
        own = qdate_dates[x]
        sdate = lambda o: (own[hash_of[o]] if hash_of[o] in own else stored[o]).date()
        ids = {o for o in sess_ids if in_window(sdate(o), rng)}
        if not (gold[x] & ids):
            date_rows[x] = {"gold_in_window": False, "window_sessions": len(ids), "arm_filtered": {a: False for a in ARMS}, "arm_unfiltered": {a: per_full[x][a][prim] for a in ARMS}}
            continue
        lst = lists(qmap[x].question, qvec[x], ids)
        date_rows[x] = {"gold_in_window": True, "window_sessions": len(ids), "arm_filtered": {a: view(lst[a], owner, gold[x])[prim] for a in ARMS}, "arm_unfiltered": {a: per_full[x][a][prim] for a in ARMS}}
    res["date_arm_exploratory"] = {"rule": "emlab.timeparse.parse_question_range (registered), window widened by %d days, session date = the date in the question's own haystack when the session is there, else the stored date" % DATE_SLACK_DAYS,
                                   "temporal_questions": len(types.get("temporal-reasoning", [])), "questions_with_resolvable_range": len(parsed),
                                   "resolvable_by_type": dict(Counter(qmap[x].qtype for x in parsed)), "questions_total": len(complete),
                                   "gold_in_window_rate": (sum(1 for v in date_rows.values() if v["gold_in_window"]) / float(max(1, len(date_rows)))),
                                   "mean_window_sessions": (sum(v["window_sessions"] for v in date_rows.values()) / float(max(1, len(date_rows)))), "pool_sessions": len(sess_ids),
                                   "by_arm": {a: {"unfiltered_sess_any_10": sum(v["arm_unfiltered"][a] for v in date_rows.values()) / float(max(1, len(date_rows))),
                                                  "filtered_sess_any_10": sum(v["arm_filtered"][a] for v in date_rows.values()) / float(max(1, len(date_rows)))} for a in ARMS},
                                   "temporal_resolvable": {a: {"n": sum(1 for x in parsed if qmap[x].qtype == "temporal-reasoning"),
                                                               "unfiltered": sum(date_rows[x]["arm_unfiltered"][a] for x in parsed if qmap[x].qtype == "temporal-reasoning"),
                                                               "filtered": sum(date_rows[x]["arm_filtered"][a] for x in parsed if qmap[x].qtype == "temporal-reasoning")} for a in ARMS},
                                   "per_question": {x: date_rows[x] for x in date_rows}}
    # ---- exploratory: preference questions, how much of the gold content is user-stated against assistant-stated
    pref = {}
    for x in types.get("single-session-preference", []):
        rub = {w for w in re.findall(r"[a-z0-9']+", str(qmap[x].answer).lower()) if w not in PREF_STOP and len(w) >= 4}
        u = a_ = 0
        for o in gold[x]:
            for t in sfile[o]["turns"]:
                hit = sum(1 for w in set(re.findall(r"[a-z0-9']+", t["content"].lower())) if w in rub)
                if t["role"] == "user":
                    u += hit
                else:
                    a_ += hit
        gf = [f for f in facts if f["session_id"] in gold[x]]
        ov = lambda f: len(rub & set(re.findall(r"[a-z0-9']+", " ".join(str(f.get(k, "")) for k in ("subject", "predicate", "object", "quote")).lower())))
        pref[x] = {"rubric_tokens": len(rub), "rubric_token_hits_in_user_turns": u, "in_assistant_turns": a_, "user_share_of_hits": (u / float(u + a_)) if (u + a_) else None,
                   "gold_facts": len(gf), "user_authored_facts": sum(1 for f in gf if f.get("author") == "user"),
                   "facts_overlapping_rubric_2plus": {"user": sum(1 for f in gf if ov(f) >= 2 and f.get("author") == "user"), "assistant": sum(1 for f in gf if ov(f) >= 2 and f.get("author") == "assistant")}}
    res["preference_exploratory"] = {"questions": len(pref), "per_question": pref,
                                     "user_share_of_rubric_hits_pooled": (sum(v["rubric_token_hits_in_user_turns"] for v in pref.values()) / float(max(1, sum(v["rubric_token_hits_in_user_turns"] + v["in_assistant_turns"] for v in pref.values())))) if pref else None}
    res["per_question_primary"] = {x: {a: {k: per_full[x][a][k] for k in ("sess_any_10", "best_gold_session_rank", "n_gold")} for a in ARMS} for x in complete}
    json.dump(res, open(out_json, "w"), indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main(*sys.argv[1:8])
