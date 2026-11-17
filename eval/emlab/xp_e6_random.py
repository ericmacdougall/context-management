"""E6 size-matched random control (exploratory, pilot only, no model). New file.
For each pilot question at H3: the length-matched pool of E6 (gold lengths widened by 25%) has m sessions; this control
draws, over several seeds, a RANDOM subset of the level's sessions with the same size m (gold sessions always kept), and
evaluates every arm in it exactly like the length-matched pool (list arms: the ranked list filtered to the subset, first 10
sessions; BM25, dense and flat RRF also by re-retrieval inside the subset). The lift of the length-matched pool over the
random pool of the same size is the part not explained by the pool simply being smaller."""
from __future__ import annotations

import json
import os
import random
import sys
from typing import Any, Dict, List

import numpy as np

from .anon import AnonMap
from .build_rag_contexts import load_common
from .live import ExactRetriever
from .metrics import clustered_paired_bootstrap, recall_at_k
from .retrievers import to_sessions
from .xp_cand import Engine, SEED, mean, sr10

SEEDS = (16096, 16097, 16098, 16099, 16100, 16101, 16102, 16103)


def main(dataset, art, sets_path, out_root, in_root, rerank_final, out_json):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    low = set(json.load(open(os.path.join(out_root, "lowoverlap_pool_h3.json")))["thresholds"]["100"]["qids"])
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    eng = Engine(art, ex, texts)
    qvec = ex.qvec
    anon = AnonMap(list(ctx.hashes))
    qids = [q.qid for q in qs]
    gold = {q.qid: set(q.gold_session_ids) for q in qs}
    slen = eng.session_len
    lists: Dict[str, Dict[str, List[str]]] = {"BM25": {}, "dense": {}, "RRF (flat)": {}, "RRF top-50 candidates": {}, "ANALYST": {}, "ANALYST-RERANK-ONLY": {}, "cross-encoder v2": {}}
    masks, ids_of, matched = {}, {}, {}
    rr = {r["qid"]: r["levels"]["H3"]["rankings"]["rerank"] for r in json.load(open(rerank_final))["per_question"]}
    for q in qs:
        ids = ctx.level_sessions(q, "H3", sets)
        ids_of[q.qid] = ids
        m = eng.mask(ids)
        f, b, d = eng.flat(q.question, qvec[q.qid], m)
        lists["BM25"][q.qid], lists["dense"][q.qid], lists["RRF (flat)"][q.qid] = [r for r, _ in b], [r for r, _ in d], [r for r, _ in f]
        task = json.load(open(os.path.join(out_root, "analyst_tasks", "H3", q.qid + ".json")))
        lists["RRF top-50 candidates"][q.qid] = [anon.real_rid(c["round_id"]) for c in task["candidates"]]
        for arm in ("ANALYST", "ANALYST-RERANK-ONLY"):
            rj = json.load(open(os.path.join(in_root, "analyst_results", arm, "H3", q.qid + ".json")))
            lists[arm][q.qid] = [anon.real_rid(i) for i in (rj.get("ranked_round_ids") or [])] if rj.get("status") == "ok" else []
        lists["cross-encoder v2"][q.qid] = rr[q.qid]
        gl = [slen[s] for s in q.gold_session_ids if s in slen]
        lo, hi = 0.75 * min(gl), 1.25 * max(gl)
        matched[q.qid] = {s for s in ids if lo <= slen.get(s, 0) <= hi} | gold[q.qid]

    def filt_sr10(L, x, subset):
        return recall_at_k([s for s in to_sessions(L) if s in subset][:10], list(gold[x]), 10)

    def evaluate(subset_of):
        out: Dict[str, Dict[str, float]] = {arm: {} for arm in lists}
        re_: Dict[str, Dict[str, float]] = {"BM25": {}, "dense": {}, "RRF (flat)": {}}
        for q in qs:
            sub = subset_of(q)
            for arm, L in lists.items():
                out[arm][q.qid] = filt_sr10(L[q.qid], q.qid, sub)
            m2 = eng.mask(sorted(sub))
            f, b, d = eng.flat(q.question, qvec[q.qid], m2)
            re_["BM25"][q.qid], re_["dense"][q.qid], re_["RRF (flat)"][q.qid] = sr10(b, gold[q.qid]), sr10(d, gold[q.qid]), sr10(f, gold[q.qid])
        return out, re_

    res: Dict[str, Any] = {"retriever": "exact in-process; not Elasticsearch", "n": len(qs), "low_overlap_n": len(low & set(qids)), "seeds": list(SEEDS)}
    full = {arm: {x: recall_at_k(to_sessions(L[x])[:10], list(gold[x]), 10) for x in L} for arm, L in lists.items()}
    mat, mat_re = evaluate(lambda q: matched[q.qid])
    seed_vals, seed_re = [], []
    for s in SEEDS:
        def rand_subset(q, s=s):
            r = random.Random("%d:%s" % (s, q.qid))
            others = [x for x in ids_of[q.qid] if x not in gold[q.qid]]
            k = max(0, len(matched[q.qid]) - len(gold[q.qid]))
            return set(r.sample(others, k)) | gold[q.qid]
        v, v_re = evaluate(rand_subset)
        seed_vals.append(v)
        seed_re.append(v_re)
        print("seed", s, flush=True)
    arms_out: Dict[str, Any] = {}
    for arm in lists:
        qs_arm = list(lists[arm])
        rs = {x: mean(sv[arm][x] for sv in seed_vals) for x in qs_arm}
        row = {"n": len(qs_arm), "full_pool": mean(full[arm][x] for x in qs_arm), "length_matched": mean(mat[arm][x] for x in qs_arm),
               "random_same_size_mean_over_seeds": mean(rs.values()),
               "random_same_size_seed_means": [mean(sv[arm][x] for x in qs_arm) for sv in seed_vals],
               "low_overlap": {"full": mean(full[arm][x] for x in qs_arm if x in low), "length_matched": mean(mat[arm][x] for x in qs_arm if x in low),
                               "random": mean(rs[x] for x in qs_arm if x in low)}}
        if arm in mat_re:
            rrs = {x: mean(sr[arm][x] for sr in seed_re) for x in qs_arm}
            row["re_retrieval"] = {"length_matched": mean(mat_re[arm].values()), "random_same_size": mean(rrs.values())}
        arms_out[arm] = row
    res["arms"] = arms_out
    comps = (("ANALYST", "RRF top-50 candidates"), ("ANALYST-RERANK-ONLY", "RRF top-50 candidates"), ("cross-encoder v2", "RRF (flat)"), ("ANALYST", "cross-encoder v2"))
    res["paired"] = {}
    for a_, b_ in comps:
        ids_ = [x for x in qids if x in lists[a_] and x in lists[b_]]
        def pd(valsA, valsB, subset):
            r = clustered_paired_bootstrap([(ctx.clusters[x], valsB[x], valsA[x]) for x in subset], n_boot=2000, seed=SEED)
            return {"n": len(subset), "mean_diff": r["mean_diff"], "ci_low": r["ci_low"], "ci_high": r["ci_high"]}
        rsA = {x: mean(sv[a_][x] for sv in seed_vals) for x in ids_}
        rsB = {x: mean(sv[b_][x] for sv in seed_vals) for x in ids_}
        lowids = [x for x in ids_ if x in low]
        res["paired"]["%s minus %s" % (a_, b_)] = {
            "full_pool": pd(full[a_], full[b_], ids_), "length_matched": pd(mat[a_], mat[b_], ids_), "random_same_size": pd(rsA, rsB, ids_),
            "full_pool_low": pd(full[a_], full[b_], lowids), "length_matched_low": pd(mat[a_], mat[b_], lowids), "random_same_size_low": pd(rsA, rsB, lowids)}
    json.dump(res, open(out_json, "w"), indent=1)
    print("done", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:8])
