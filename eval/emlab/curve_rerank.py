"""Pilot rerank arm at pooled levels H0..H3 (top-n 20), with per-question time. New file; no LLM."""
from __future__ import annotations

import json
import sys
import time

from .corpus import cluster_ids
from .dataset import LongMemEvalLoader, select_split
from .live import ExactRetriever
from .pool import cluster_haystacks, level_session_ids, pool_universe, with_sessions, LEVELS
from .rerank import RerankingWrapper
from .retrievers import run_arm
from .runner import DEPTH, question_metrics

TOP_N = 20


def main(dataset, art, models, sets_path, out):
    all_qs = list(LongMemEvalLoader(dataset).questions())
    qs = [q for q in select_split(all_qs, "pilot") if q.retrieval_gold_round_ids]
    sets = json.load(open(sets_path))
    cl = cluster_ids(all_qs)
    ch = cluster_haystacks(all_qs, cl)
    uni = pool_universe(all_qs)
    a = art.rstrip("/") + "/"
    base = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    base.prepare_terms(qs)
    chunks = {}
    for c in base.chunks:
        chunks.setdefault(c["round_id"], []).append(c["text"])
    retr = RerankingWrapper(base, chunks, models)
    res = {"top_n": TOP_N, "arms": ["rrf", "rerank"], "levels": {}, "seconds_rerank_per_question": {}, "per_question": []}
    secs = {l: [] for l in LEVELS}
    per_q = []
    for qi, q in enumerate(qs):
        rec = {"qid": q.qid, "levels": {}}
        for lv in LEVELS:
            pq = with_sessions(q, level_session_ids(q, uni, ch[cl[q.qid]], lv, sets))
            rec["levels"][lv] = {"rrf": question_metrics(q, run_arm(retr, pq, "rrf", DEPTH, "off"))}
            t0 = time.time()
            rec["levels"][lv]["rerank"] = question_metrics(q, run_arm(retr, pq, "rerank", DEPTH, "off", rerank_top=TOP_N))
            secs[lv].append(time.time() - t0)
        per_q.append(rec)
        print("done", qi + 1, len(qs), flush=True)
        json.dump({"partial": True, "per_question": per_q}, open(out + ".partial", "w"))
    for lv in LEVELS:
        res["levels"][lv] = {}
        for arm in ("rrf", "rerank"):
            m = {}
            for metric in per_q[0]["levels"][lv][arm]:
                xs = [r["levels"][lv][arm][metric] for r in per_q if r["levels"][lv][arm][metric] is not None]
                m[metric] = {"mean": sum(xs) / len(xs) if xs else None, "n": len(xs)}
            res["levels"][lv][arm] = m
        v = sorted(secs[lv])
        res["seconds_rerank_per_question"][lv] = {"mean": sum(v) / len(v), "median": v[len(v) // 2], "max": v[-1], "n": len(v),
                                                   "note": "includes bm25+dense+fusion+rerank, cached scores reused across levels"}
    res["per_question"] = per_q
    json.dump(res, open(out, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:6])
