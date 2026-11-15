"""Pilot-only comparisons against the Elasticsearch node (chunk level, no tuning to remove differences):
  knn     ES kNN vs exact cosine under both filter heuristics, both vector fields, two num_candidates
  fusion  client-side RRF / linear vs Elasticsearch's own rrf / linear retrievers on the same queries
  e5      external E5 vectors vs the node's built-in E5 endpoint on a round sample and the pilot queries
Run: python3 -m emlab.escompare --action knn|fusion|e5 --es-url U --curl-config F --artifacts A --dataset-path D --out O
"""
from __future__ import annotations

import argparse
import json
import random
from typing import Any, Dict, List, Sequence

from .dataset import SPLIT_SEED, LongMemEvalLoader, select_split
from .es_client import CurlEsClient, EsError
from .fusion import linear, rrf
from .mappings import INDEX_V1, bm25_query, knn_query


def overlap(a: Sequence[str], b: Sequence[str], k: int) -> float:
    return len(set(a[:k]) & set(b[:k])) / float(k)


def _hits(resp) -> List[Dict[str, Any]]:
    return resp["hits"]["hits"]


def _ids(resp) -> List[str]:
    return [h["_id"] for h in _hits(resp)]


def _scores(resp) -> Dict[str, float]:
    return {h["_id"]: float(h["_score"]) for h in _hits(resp)}


def exact_topk(vectors, by_session, chunks, qv, sessions, k):
    import numpy as np
    idx = np.asarray([i for s in sessions for i in by_session.get(s, ())])
    sims = vectors[idx] @ qv
    top = np.argsort(-sims, kind="stable")[:k]
    return [chunks[int(idx[t])]["chunk_id"] for t in top]


def set_heuristic(c: CurlEsClient, index: str, value: str) -> str:
    try:
        c.request("PUT", "/%s/_settings" % index, {"index.dense_vector.hnsw_filter_heuristic": value})
        return "dynamic setting applied"
    except EsError as e:
        return "NOT dynamic or refused: %s" % e


def do_knn(c, qs, ex, qv_all, out):
    rows: List[Dict[str, Any]] = []
    status = {}
    for heur in ("acorn", "fanout"):
        status[heur] = set_heuristic(c, INDEX_V1, heur)
        for field in ("vector", "vector_int8"):
            for nc in (100, 1000):
                rec10, rec50 = [], []
                for q in qs:
                    qv = qv_all[q.qid]
                    sess = q.haystack_session_ids
                    truth = exact_topk(ex.vectors, ex.by_session, ex.chunks, ex.np.asarray(qv), sess, 50)
                    resp = c.search(INDEX_V1, knn_query(list(map(float, qv)), sess, 50, nc, field))
                    got = _ids(resp)
                    rec10.append(overlap(got, truth, 10))
                    rec50.append(len(set(got) & set(truth)) / 50.0)
                rows.append({"heuristic": heur, "field": field, "num_candidates": nc,
                             "recall_true_top10_mean": sum(rec10) / len(rec10),
                             "recall_true_top50_mean": sum(rec50) / len(rec50),
                             "min_top10": min(rec10), "n_questions": len(qs)})
    out["knn"] = {"heuristic_setting_status": status, "rows": rows}


def do_fusion(c, qs, qv_all, out):
    rows = []
    for q in qs:
        qv = list(map(float, qv_all[q.qid]))
        sess = q.haystack_session_ids
        b = bm25_query(q.question, sess, 200)
        k = knn_query(qv, sess, 200, 1000)
        rb, rk = c.search(INDEX_V1, b), c.search(INDEX_V1, k)
        mine_rrf = [d for d, _ in rrf([_ids(rb), _ids(rk)], k=60)]
        mine_lin = [d for d, _ in linear([_scores(rb), _scores(rk)], [0.5, 0.5])]
        std = {"standard": {"query": b["query"]}}
        knn = {"knn": k["knn"]}
        es_rrf = c.search(INDEX_V1, {"size": 50, "_source": False, "retriever": {"rrf": {
            "retrievers": [std, knn], "rank_window_size": 200, "rank_constant": 60}}})
        es_lin = c.search(INDEX_V1, {"size": 50, "_source": False, "retriever": {"linear": {
            "retrievers": [{"retriever": std, "weight": 0.5, "normalizer": "minmax"},
                           {"retriever": knn, "weight": 0.5, "normalizer": "minmax"}], "rank_window_size": 200}}})
        for name, mine, es in (("rrf", mine_rrf, _ids(es_rrf)), ("linear", mine_lin, _ids(es_lin))):
            rows.append({"qid": q.qid, "fusion": name, "top10_overlap": overlap(mine, es, 10),
                         "top50_overlap": overlap(mine, es, 50),
                         "same_top10_order": mine[:10] == es[:10], "same_top1": mine[:1] == es[:1]})
    agg = {}
    for name in ("rrf", "linear"):
        r = [x for x in rows if x["fusion"] == name]
        agg[name] = {"n": len(r), "mean_top10_overlap": sum(x["top10_overlap"] for x in r) / len(r),
                     "mean_top50_overlap": sum(x["top50_overlap"] for x in r) / len(r),
                     "frac_same_top10_order": sum(x["same_top10_order"] for x in r) / len(r),
                     "frac_same_top1": sum(x["same_top1"] for x in r) / len(r)}
    out["fusion"] = {"summary": agg, "rows": rows,
                     "note": "my fusion runs over ES bm25 and knn chunk lists (window 200); ES fuses internally"}


def embed_node(c, texts: List[str], inference_id: str = "emlab-e5") -> List[List[float]]:
    res = []
    for i in range(0, len(texts), 8):
        r = c.request("POST", "/_inference/text_embedding/%s" % inference_id, {"input": texts[i:i + 8]})
        res.extend(x["embedding"] for x in r["text_embedding"])
    return res


def do_e5(c, qs, ex, qv_all, out):
    import numpy as np
    rng = random.Random(SPLIT_SEED)
    gold = [rid for q in qs for rid in q.retrieval_gold_round_ids]
    first_chunk = {}
    for i, ch in enumerate(ex.chunks):
        first_chunk.setdefault(ch["round_id"], i)
    rng.shuffle(gold)
    pool = sorted(rid for rid in first_chunk)
    rng.shuffle(pool)
    sample = list(dict.fromkeys(gold[:100] + pool[:300]))[:200]
    texts = [ex.chunks[first_chunk[r]]["text"] for r in sample]
    mine_p = np.asarray([ex.vectors[first_chunk[r]] for r in sample])
    qq = [q for q in qs][:50]
    mine_q = np.asarray([qv_all[q.qid] for q in qq])
    res: Dict[str, Any] = {"sample_rounds": len(sample), "queries": len(qq)}

    def cos(a, b):
        a, b = np.asarray(a), np.asarray(b)
        return float((a * b).sum() / (np.linalg.norm(a) * np.linalg.norm(b)))

    for pv, pf, qf in (("raw", "%s", "%s"), ("prefixed", "passage: %s", "query: %s")):
        nodep = embed_node(c, [pf % t for t in texts])
        nodeq = embed_node(c, [qf % q.question for q in qq])
        cp = [cos(a, b) for a, b in zip(mine_p, nodep)]
        cq = [cos(a, b) for a, b in zip(mine_q, nodeq)]
        ov = []
        for i in range(len(qq)):
            a = np.argsort(-(mine_p @ mine_q[i]), kind="stable")[:10]
            nb = np.asarray(nodep) @ np.asarray(nodeq[i])
            b = np.argsort(-nb, kind="stable")[:10]
            ov.append(len(set(a.tolist()) & set(b.tolist())) / 10.0)
        res[pv] = {"passage_cosine_mean": sum(cp) / len(cp), "passage_cosine_min": min(cp),
                   "query_cosine_mean": sum(cq) / len(cq), "query_cosine_min": min(cq),
                   "top10_overlap_within_sample_mean": sum(ov) / len(ov)}
    out["e5"] = res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", choices=["knn", "fusion", "e5"], required=True)
    ap.add_argument("--es-url", required=True)
    ap.add_argument("--curl-config", required=True)
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--dataset-path", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    import numpy as np
    from .live import ExactRetriever
    d = a.artifacts.rstrip("/") + "/"
    qs = select_split(list(LongMemEvalLoader(a.dataset_path).questions()), "pilot")  # pilot only
    if a.limit:
        qs = qs[:a.limit]
    qids = json.load(open(d + "queries.json"))
    qv_all = dict(zip(qids, np.load(d + "query_vectors.npy")))
    ex = ExactRetriever(d + "chunks.jsonl", d + "chunk_vectors.npy", d + "queries.json", d + "query_vectors.npy")
    c = CurlEsClient(a.es_url, a.curl_config)
    out: Dict[str, Any] = {"split": "pilot"}
    {"knn": do_knn, "fusion": do_fusion, "e5": do_e5}[a.action](c, qs, ex, qv_all, out) if a.action != "knn" else do_knn(c, qs, ex, qv_all, out)
    json.dump(out, open(a.out, "w"), indent=1)
    print(json.dumps(out.get(a.action, {}).get("rows", out.get(a.action))[:3] if a.action != "e5" else out["e5"])[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
