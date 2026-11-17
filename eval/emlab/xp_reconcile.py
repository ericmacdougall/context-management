"""Reconcile baselines reported with different values (claims audit). Exploratory; pilot only; no model. New file.
Recomputes, for H1, H2, H3: the flat RRF baseline under four candidate definitions (fusion depth 50 = the tier-0 candidate
list handed to the analyst, 200 = the registered curve, 400 = the RAG contexts, and the top-20-round list), the packed
4,000-token knowledge-update numbers under depth 50 and 400, and re-does the analyst-versus-RRF paired differences on one
definition. Exact in-process retriever."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from .anon import AnonMap
from .build_rag_contexts import level_dates, load_common
from .context import context_metrics, pack_rag
from .fusion import rrf
from .live import ExactRetriever
from .metrics import clustered_paired_bootstrap, recall_at_k
from .pool import with_sessions
from .retrievers import to_sessions

SEED = 16096


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def main(dataset, art, sets_path, out_root, in_root, rerank_snapshot, out_json):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    low = set(json.load(open(os.path.join(out_root, "lowoverlap_pool_h3.json")))["thresholds"]["100"]["qids"])
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    anon = AnonMap(list(ctx.hashes))
    rr = {r["qid"]: {lv: r["levels"][lv]["rankings"]["rerank"] for lv in ("H1", "H2", "H3")} for r in json.load(open(rerank_snapshot))["per_question"]}
    defs = {"D50 (tier-0 candidates)": 50, "D200 (registered curve)": 200, "D400 (RAG contexts)": 400}
    res: Dict[str, Any] = {"retriever": "exact in-process", "levels": {}}
    for lv in ("H1", "H2", "H3"):
        base: Dict[str, Dict[str, List[str]]] = {d: {} for d in defs}
        base["top-20 rounds of D400"] = {}
        arms: Dict[str, Dict[str, List[str]]] = {"ANALYST": {}, "ANALYST-RERANK-ONLY": {}, "RERANK": {}}
        dates_of, gold = {}, {}
        for q in qs:
            ids = ctx.level_sessions(q, lv, sets)
            pq = with_sessions(q, ids)
            b = [r for r, _ in ex.bm25(pq, 400, None)]
            d = [r for r, _ in ex.dense(pq, 400, None)]
            for name, D in defs.items():
                base[name][q.qid] = [r for r, _ in rrf([b[:D], d[:D]], k=60)][:D]
            base["top-20 rounds of D400"][q.qid] = base["D400 (RAG contexts)"][q.qid][:20]
            dates_of[q.qid], _ = level_dates(q, ids)
            gold[q.qid] = q
            for arm in ("ANALYST", "ANALYST-RERANK-ONLY"):
                rj = json.load(open(os.path.join(in_root, "analyst_results", arm, lv, q.qid + ".json")))
                if rj.get("status") == "ok":
                    # analyst ids were validated in analyst_full; here only ids that are candidate or tool-returned matter,
                    # so reuse every id that maps to a known session of the level
                    arms[arm][q.qid] = [anon.real_rid(i) for i in rj.get("ranked_round_ids") or []]
                else:
                    arms[arm][q.qid] = []
            if q.qid in rr:
                arms["RERANK"][q.qid] = rr[q.qid][lv]
        def sr10(L, qid):
            return recall_at_k(to_sessions([r for r in L if r.rsplit(":", 1)[0] in dates_of[qid]])[:10], gold[qid].gold_session_ids, 10)
        def ctx4k(L, qid):
            q = gold[qid]
            items = pack_rag([r for r in L if r.rsplit(":", 1)[0] in dates_of[qid] and r in texts], dates_of[qid], texts, 4000)
            return context_metrics(q, items)
        lvres: Dict[str, Any] = {"baseline_session_recall10": {}, "ku_4k": {}, "paired_vs_baselines": {}}
        qids = [q.qid for q in qs]
        for name, L in base.items():
            vals = {x: sr10(L[x], x) for x in qids}
            lvres["baseline_session_recall10"][name] = {"all": mean(vals.values()), "low_overlap": mean(v for x, v in vals.items() if x in low), "n": len(vals)}
            ku = [ctx4k(L[x], x) for x in qids if gold[x].gold_latest_session_ids]
            lvres["ku_4k"][name] = {"n": len(ku), "latest": mean(1.0 if c["gold_latest_in_context"] else 0.0 for c in ku), "stale_only": mean(1.0 if c["stale_only"] else 0.0 for c in ku)}
        for arm in ("ANALYST", "ANALYST-RERANK-ONLY", "RERANK"):
            av = {x: sr10(arms[arm][x], x) for x in arms[arm]}
            ac = {x: ctx4k(arms[arm][x], x)["gold_session_recall"] for x in arms[arm]}
            lvres["paired_vs_baselines"][arm] = {}
            for name, L in base.items():
                bv = {x: sr10(L[x], x) for x in av}
                bc = {x: ctx4k(L[x], x)["gold_session_recall"] for x in av}
                out = {}
                for sub, ids_ in (("all", list(av)), ("low_overlap", [x for x in av if x in low])):
                    for mname, A, B in (("session_recall_at10", av, bv), ("context4k_gold_session_recall", ac, bc)):
                        r = clustered_paired_bootstrap([(ctx.clusters[x], B[x], A[x]) for x in ids_], n_boot=2000, seed=SEED)
                        out["%s %s" % (sub, mname)] = {"n": len(ids_), "mean_diff": r["mean_diff"], "ci_low": r["ci_low"], "ci_high": r["ci_high"]}
                lvres["paired_vs_baselines"][arm][name] = out
        res["levels"][lv] = lvres
        print("level", lv, json.dumps(lvres["baseline_session_recall10"]), flush=True)
    json.dump(res, open(out_json, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:8])
