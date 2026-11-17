"""ES-pilot (V2 Part E rule-setting): Elasticsearch Basic-features parity against the exact retriever. Pilot only; no model call.
Reads the owned index emlab-rounds-v1 on the original node only; writes nothing. See EXPLORATORY.md for the rule."""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import numpy as np

from .build_rag_contexts import load_common
from .es_client import CurlEsClient
from .escompare import set_heuristic
from .fusion import rrf
from .live import ExactRetriever
from .mappings import INDEX_V1
from .metrics import clustered_paired_bootstrap, recall_at_k
from .pool import with_sessions
from .retrievers import to_sessions

SEED = 16096
DEPTH = 200
FETCH = 600


def rollup(hits, limit: int) -> List[str]:
    seen, out = set(), []
    for h in hits:
        r = h["_source"]["round_id"]
        if r not in seen:
            seen.add(r)
            out.append(r)
            if len(out) >= limit:
                break
    return out


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def main(dataset, art, sets_path, out_root, es_url, cfg, out_json):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    c = CurlEsClient(es_url, cfg, timeout=300)
    heur = {"acorn": set_heuristic(c, INDEX_V1, "acorn")}
    all_sessions = set(ctx.hashes)
    res: Dict[str, Any] = {"retriever": "Elasticsearch 9.5.6 Basic features (index emlab-rounds-v1) vs exact in-process", "n": len(qs),
                           "heuristic_readback": heur, "levels": {}}
    timing: Dict[str, List[float]] = {}

    def search(body):
        t0 = time.time()
        r = c.search(INDEX_V1, body)
        timing.setdefault("took_ms", []).append(r.get("took", 0))
        timing.setdefault("wall_s", []).append(time.time() - t0)
        return r["hits"]["hits"]

    def run_variant(variant_heur: Optional[str]):
        if variant_heur and variant_heur not in heur:
            heur[variant_heur] = set_heuristic(c, INDEX_V1, variant_heur)

    for lv in ("H0", "H1", "H2", "H3"):
        rows: Dict[str, Dict[str, Dict[str, Any]]] = {k: {} for k in ("exact_bm25", "exact_dense", "exact_rrf", "es_bm25", "es_dense", "es_rrf",
                                                                       "es_dense_fanout", "es_rrf_fanout", "ctl_whitespace", "ctl_keyword")}
        head_overlap: List[float] = []
        top10_overlap = {"bm25": [], "dense": [], "rrf": []}
        for q in qs:
            ids = list(q.haystack_session_ids) if lv == "H0" else ctx.level_sessions(q, lv, sets)
            pq = with_sessions(q, ids)
            excl = sorted(all_sessions - set(ids))
            filt = ({"terms": {"session_id": ids}} if lv != "H3" else
                    ({"bool": {"must_not": [{"terms": {"session_id": excl}}]}} if excl else {"match_all": {}}))
            eb = [d for d, _ in ex.bm25(pq, DEPTH, None)]
            ed = [d for d, _ in ex.dense(pq, DEPTH, None)]
            er = [d for d, _ in rrf([eb, ed], k=60)][:DEPTH]
            gold = list(q.gold_session_ids)

            def bm(analyzer=None):
                m = {"query": q.question}
                if analyzer:
                    m["analyzer"] = analyzer
                return rollup(search({"size": FETCH, "_source": ["round_id"], "query": {"bool": {"must": [{"match": {"text": m}}], "filter": [filt]}}}), DEPTH)

            def kn():
                return rollup(search({"size": FETCH, "_source": ["round_id"], "knn": {"field": "vector", "query_vector": [float(x) for x in ex.qvec[q.qid]],
                                                                                 "k": FETCH, "num_candidates": 1000, "filter": filt,
                                                                                 "rescore_vector": {"oversample": 3}}}), DEPTH)
            sb = bm()
            sd = kn()
            sr = [d for d, _ in rrf([sb, sd], k=60)][:DEPTH]
            for name, L in (("exact_bm25", eb), ("exact_dense", ed), ("exact_rrf", er), ("es_bm25", sb), ("es_dense", sd), ("es_rrf", sr)):
                rows[name][q.qid] = recall_at_k(to_sessions(L)[:10], gold, 10)
            rows["ctl_whitespace"][q.qid] = recall_at_k(to_sessions(bm("whitespace"))[:10], gold, 10)
            rows["ctl_keyword"][q.qid] = recall_at_k(to_sessions(bm("keyword"))[:10], gold, 10)
            top10_overlap["bm25"].append(len(set(to_sessions(eb)[:10]) & set(to_sessions(sb)[:10])) / 10.0)
            top10_overlap["dense"].append(len(set(to_sessions(ed)[:10]) & set(to_sessions(sd)[:10])) / 10.0)
            top10_overlap["rrf"].append(len(set(to_sessions(er)[:10]) & set(to_sessions(sr)[:10])) / 10.0)
            head_overlap.append(len(set(er[:20]) & set(sr[:20])) / 20.0)
        # secondary: fanout variant of the kNN heuristic
        run_variant("fanout")
        for q in qs:
            ids = list(q.haystack_session_ids) if lv == "H0" else ctx.level_sessions(q, lv, sets)
            excl = sorted(all_sessions - set(ids))
            filt = ({"terms": {"session_id": ids}} if lv != "H3" else
                    ({"bool": {"must_not": [{"terms": {"session_id": excl}}]}} if excl else {"match_all": {}}))
            sd = rollup(search({"size": FETCH, "_source": ["round_id"], "knn": {"field": "vector", "query_vector": [float(x) for x in ex.qvec[q.qid]],
                                                                             "k": FETCH, "num_candidates": 1000, "filter": filt, "rescore_vector": {"oversample": 3}}}), DEPTH)
            sb = rollup(search({"size": FETCH, "_source": ["round_id"], "query": {"bool": {"must": [{"match": {"text": {"query": q.question}}}], "filter": [filt]}}}), DEPTH)
            sr = [d for d, _ in rrf([sb, sd], k=60)][:DEPTH]
            rows["es_dense_fanout"][q.qid] = recall_at_k(to_sessions(sd)[:10], list(q.gold_session_ids), 10)
            rows["es_rrf_fanout"][q.qid] = recall_at_k(to_sessions(sr)[:10], list(q.gold_session_ids), 10)
        run_variant("acorn")
        set_heuristic(c, INDEX_V1, "acorn")
        qids = [q.qid for q in qs]
        lvres: Dict[str, Any] = {"mean_session_recall10": {k: mean(v.values()) for k, v in rows.items() if v}, "paired": {},
                                 "top10_session_overlap": {k: mean(v) for k, v in top10_overlap.items()}, "rrf_head20_round_overlap": mean(head_overlap)}
        for arm, es, exa in (("BM25", "es_bm25", "exact_bm25"), ("dense", "es_dense", "exact_dense"), ("RRF", "es_rrf", "exact_rrf"),
                             ("dense (fanout)", "es_dense_fanout", "exact_dense"), ("RRF (fanout)", "es_rrf_fanout", "exact_rrf"),
                             ("control whitespace analyzer", "ctl_whitespace", "exact_bm25"), ("control keyword analyzer", "ctl_keyword", "exact_bm25")):
            r = clustered_paired_bootstrap([(ctx.clusters[x], rows[exa][x], rows[es][x]) for x in qids], n_boot=2000, seed=SEED)
            hold = bool(abs(r["mean_diff"]) <= 0.03 and r["ci_low"] >= -0.05 and r["ci_high"] <= 0.05)
            lvres["paired"][arm] = {"mean_diff": r["mean_diff"], "ci_low": r["ci_low"], "ci_high": r["ci_high"], "clusters": int(r["effective_n"]), "rule_holds": hold}
        res["levels"][lv] = lvres
        print(lv, json.dumps({k: (round(v["mean_diff"], 3), v["rule_holds"]) for k, v in lvres["paired"].items()}), flush=True)
    res["timing_indicative"] = {k: mean(v) for k, v in timing.items()}
    res["control_ok"] = {"whitespace_h0_diff_le_-0.10": bool(res["levels"]["H0"]["paired"]["control whitespace analyzer"]["mean_diff"] <= -0.10),
                         "keyword_le_0.05_all_levels": all(res["levels"][lv]["mean_session_recall10"]["ctl_keyword"] <= 0.05 for lv in res["levels"])}
    json.dump(res, open(out_json, "w"), indent=1)
    print("controls", res["control_ok"], flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:8])
