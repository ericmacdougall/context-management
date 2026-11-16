"""E5(d): cross-encoder (bge-reranker-v2-m3, chunk-max) over the flat top K rounds, K = 200 (then 400 only if 200 shows the
committed minimum gain), on the 23 low-overlap questions at H3, with the wrong-query control and per-question cost. New;
exploratory; pilot only; no model API (local reranker). A stop-file (<out>.stop) ends the run."""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List

import numpy as np

from .live import ExactRetriever
from .metrics import clustered_paired_bootstrap, recall_at_k
from .retrievers import to_sessions
from .xp_cand import Engine, SEED, mean
from .xp_common import derangement_list
from .build_rag_contexts import load_common


def main(dataset, art, sets_path, out_root, models, out_json, ks="200,400", threads="12"):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    torch.set_num_threads(int(threads))
    name = "BAAI/bge-reranker-v2-m3"
    tok = AutoTokenizer.from_pretrained(name, cache_dir=models)
    model = AutoModelForSequenceClassification.from_pretrained(name, cache_dir=models).eval()
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    low = json.load(open(os.path.join(out_root, "lowoverlap_pool_h3.json")))["thresholds"]["100"]["qids"]
    qmap = {q.qid: q for q in qs}
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    eng = Engine(art, ex, texts)
    chunk_texts: Dict[str, List[str]] = {}
    for c in ex.chunks:
        chunk_texts.setdefault(c["round_id"], []).append(c["text"])
    qid_all = [q.qid for q in qs]
    der = derangement_list(len(qid_all), SEED)
    wrong = {qid_all[i]: qid_all[der[i]] for i in range(len(qid_all))}
    stop = out_json + ".stop"

    def rerank(query: str, rounds: List[str]):
        pairs = [(r, t) for r in rounds for t in chunk_texts[r]]
        scores: List[float] = []
        for i in range(0, len(pairs), 8):
            b = pairs[i:i + 8]
            enc = tok([query] * len(b), [t for _, t in b], padding=True, truncation="only_second", max_length=512, return_tensors="pt")
            with torch.inference_mode():
                scores.extend(model(**enc).logits.view(-1).tolist())
        best: Dict[str, float] = {}
        for (r, _), s in zip(pairs, scores):
            best[r] = max(best.get(r, float("-inf")), s)
        return sorted(rounds, key=lambda r: (-best[r], rounds.index(r))), len(pairs)

    res: Dict[str, Any] = {"retriever": "exact in-process candidates + local cross-encoder; not Elasticsearch", "level": "H3", "n": len(low), "k": {}}
    for K in [int(k) for k in ks.split(",")]:
        rows = []
        for qid in low:
            if os.path.exists(stop):
                break
            q = qmap[qid]
            ids = ctx.level_sessions(q, "H3", sets)
            mask = eng.mask(ids)
            flat = [r for r, _ in eng.flat(q.question, ex.qvec[qid], mask)[0]]
            t0 = time.time()
            rer, pairs = rerank(q.question, flat[:K])
            secs = time.time() - t0
            new = rer + flat[K:]
            g = list(q.gold_session_ids)
            row = {"qid": qid, "flat_sr10": recall_at_k(to_sessions(flat)[:10], g, 10), "rerank_sr10": recall_at_k(to_sessions(new)[:10], g, 10),
                   "seconds": secs, "pairs": pairs, "gold_in_candidates": any(r.rsplit(":", 1)[0] in set(g) for r in flat[:K])}
            if K == 200:
                w = qmap[wrong[qid]]
                fw = [r for r, _ in eng.flat(w.question, ex.qvec[w.qid], mask)[0]]
                rw, _ = rerank(w.question, fw[:K])
                row["control_flat_wrong_sr10"] = recall_at_k(to_sessions(fw)[:10], g, 10)
                row["control_rerank_wrong_sr10"] = recall_at_k(to_sessions(rw + fw[K:])[:10], g, 10)
            rows.append(row)
            res["k"][str(K)] = {"rows": rows}
            json.dump(res, open(out_json + ".partial", "w"))
            print(K, qid, row["flat_sr10"], row["rerank_sr10"], round(secs, 1), flush=True)
        if len(rows) < len(low):
            break
        diff = clustered_paired_bootstrap([(ctx.clusters[r["qid"]], r["flat_sr10"], r["rerank_sr10"]) for r in rows], n_boot=2000, seed=SEED)
        summ: Dict[str, Any] = {"flat_sr10": mean(r["flat_sr10"] for r in rows), "rerank_sr10": mean(r["rerank_sr10"] for r in rows),
                                "diff": {k: diff[k] for k in ("mean_diff", "ci_low", "ci_high")}, "seconds_per_question_mean": mean(r["seconds"] for r in rows),
                                "pairs_per_question_mean": mean(r["pairs"] for r in rows),
                                "gold_in_candidates": sum(1 for r in rows if r["gold_in_candidates"]),
                                "held": bool(diff["mean_diff"] >= 0.05 and diff["ci_low"] > 0)}
        if K == 200:
            cd = clustered_paired_bootstrap([(ctx.clusters[r["qid"]], r["control_flat_wrong_sr10"], r["control_rerank_wrong_sr10"]) for r in rows], n_boot=2000, seed=SEED)
            summ["control_wrong_query"] = {"flat": mean(r["control_flat_wrong_sr10"] for r in rows), "rerank": mean(r["control_rerank_wrong_sr10"] for r in rows),
                                           "diff": {k: cd[k] for k in ("mean_diff", "ci_low", "ci_high")}, "gain_credited": bool(cd["mean_diff"] >= 0.05)}
        res["k"][str(K)] = {"rows": rows, "summary": summ}
        json.dump(res, open(out_json, "w"), indent=1)
        print("K", K, json.dumps(summ), flush=True)
        if K == 200 and not summ["held"]:
            break  # K = 400 only if 200 showed the committed minimum gain


if __name__ == "__main__":
    main(*sys.argv[1:8])
