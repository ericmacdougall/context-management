"""ES-pilot (fresh run). Pilot split only; no model API call; the shared node is reached ONLY through GuardedEs (read-only).

  retrieve : per (question, level) the exact and Elasticsearch round lists for BM25, dense and RRF 60 at depth 200, the two
             analyzer-mismatch controls and the wrong-query control; writes rows.jsonl.gz plus the node state before and after.
  rerank   : the local cross-encoder over the top 20 of each engine's RRF, one (qid, round) cache; writes ce.jsonl.gz.
Everything the scorer needs is in the rows; nothing is aggregated here. See ES-PILOT-PLAN.md and ES-PILOT-THRESHOLDS.md."""
from __future__ import annotations

import gzip
import json
import random
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from .build_rag_contexts import load_common
from .es_client import CurlEsClient
from .es_guard import GuardedEs
from .es_verify import state
from .fusion import rrf
from .live import ExactRetriever
from .mappings import INDEX_V1
from .pool import with_sessions

SEED = 16096
DEPTH = 200
FETCH = 600
HEAD = 20  # the registered RERANK_TOP_N
LEVELS = ("H0", "H1", "H2", "H3")


def rollup(hits: Sequence[Dict[str, Any]], limit: int) -> List[str]:
    seen, out = set(), []
    for h in hits:
        r = h["_source"]["round_id"]
        if r not in seen:
            seen.add(r)
            out.append(r)
            if len(out) >= limit:
                break
    return out


def level_filter(level: str, ids: Sequence[str], all_sessions: set) -> Dict[str, Any]:
    if level != "H3":
        return {"terms": {"session_id": list(ids)}}
    excl = sorted(all_sessions - set(ids))
    return {"bool": {"must_not": [{"terms": {"session_id": excl}}]}} if excl else {"match_all": {}}


def es_lists(search, index: str, text: str, qvec: Sequence[float], filt: Dict[str, Any], analyzer: Optional[str] = None) -> Dict[str, List[str]]:
    """The one Elasticsearch code path of the pilot (also used by the needle control). `search(index, body) -> response`."""
    m: Dict[str, Any] = {"query": text}
    if analyzer:
        m["analyzer"] = analyzer
    bm = rollup(search(index, {"size": FETCH, "_source": ["round_id", "session_id"],
                               "query": {"bool": {"must": [{"match": {"text": m}}], "filter": [filt]}}})["hits"]["hits"], DEPTH)
    out = {"bm25": bm}
    if analyzer is None:
        dn = rollup(search(index, {"size": FETCH, "_source": ["round_id", "session_id"],
                                   "knn": {"field": "vector", "query_vector": [float(x) for x in qvec], "k": FETCH, "num_candidates": 1000,
                                           "filter": filt, "rescore_vector": {"oversample": 3}}})["hits"]["hits"], DEPTH)
        out["dense"] = dn
        out["rrf"] = [d for d, _ in rrf([bm, dn], k=60)][:DEPTH]
    return out


def derange_across_clusters(qids: List[str], clusters: Dict[str, str], seed: int) -> Dict[str, str]:
    rng = random.Random(seed)
    p = list(range(len(qids)))
    while True:
        rng.shuffle(p)
        if all(clusters[qids[i]] != clusters[qids[p[i]]] for i in range(len(qids))):
            return {qids[i]: qids[p[i]] for i in range(len(qids))}


def retrieve(dataset, art, sets_path, es_url, curl_config, out_dir, stamp_path):
    stamp = json.load(open(stamp_path))
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    qmap = {q.qid: q for q in qs}
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    g = GuardedEs(CurlEsClient(es_url, curl_config, timeout=300), INDEX_V1)
    before = state(g, INDEX_V1)
    all_sessions = set(ctx.hashes)
    wrong = derange_across_clusters([q.qid for q in qs], ctx.clusters, SEED)
    t0 = time.time()
    n = 0
    with gzip.open(out_dir.rstrip("/") + "/rows.jsonl.gz", "wt") as fh:
        for q in qs:
            for lv in LEVELS:
                ids = list(q.haystack_session_ids) if lv == "H0" else ctx.level_sessions(q, lv, sets)
                filt = level_filter(lv, ids, all_sessions)
                wq = qmap[wrong[q.qid]]

                def exact(qq):
                    pq = with_sessions(qq, ids)
                    eb = [d for d, _ in ex.bm25(pq, DEPTH, None)]
                    ed = [d for d, _ in ex.dense(pq, DEPTH, None)]
                    return {"bm25": eb, "dense": ed, "rrf": [d for d, _ in rrf([eb, ed], k=60)][:DEPTH]}
                row = {"qid": q.qid, "cluster": ctx.clusters[q.qid], "level": lv, "gold": list(q.gold_session_ids), "n_level_sessions": len(ids),
                       "exact": exact(q), "es": es_lists(g.search, INDEX_V1, q.question, ex.qvec[q.qid], filt),
                       "ctl_whitespace": es_lists(g.search, INDEX_V1, q.question, ex.qvec[q.qid], filt, "whitespace")["bm25"],
                       "ctl_keyword": es_lists(g.search, INDEX_V1, q.question, ex.qvec[q.qid], filt, "keyword")["bm25"],
                       "wrong_qid": wq.qid, "wrong_exact": exact(wq),
                       "wrong_es": es_lists(g.search, INDEX_V1, wq.question, ex.qvec[wq.qid], filt)}
                fh.write(json.dumps(row) + "\n")
                n += 1
            if n % 40 == 0:
                print("rows", n, "%.0fs" % (time.time() - t0), flush=True)
    after = state(g, INDEX_V1)
    meta = {"stamp": stamp, "rows": n, "questions": len(qs), "seed": SEED, "depth": DEPTH, "fetch": FETCH, "seconds": time.time() - t0,
            "state_before": before, "state_after": after, "request_log": dict(g.log), "search_flags": g.search_flags,
            "wrong_query_map_sha": __import__("hashlib").sha256(json.dumps(wrong, sort_keys=True).encode()).hexdigest()}
    json.dump(meta, open(out_dir.rstrip("/") + "/retrieve_meta.json", "w"), indent=1)
    print(json.dumps({k: meta[k] for k in ("rows", "seconds", "request_log", "search_flags")}), flush=True)


def rerank(art, models, rows_path, out_dir, stamp_path, dataset, threads="16"):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    torch.set_num_threads(int(threads))
    name = "BAAI/bge-reranker-v2-m3"
    tok = AutoTokenizer.from_pretrained(name, cache_dir=models, local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(name, cache_dir=models, local_files_only=True).eval()
    a = art.rstrip("/") + "/"
    chunk_text: Dict[str, List[str]] = {}
    for line in open(a + "chunks.jsonl", encoding="utf-8"):
        c = json.loads(line)
        chunk_text.setdefault(c["round_id"], []).append(c["text"])
    from .dataset import LongMemEvalLoader
    qtext = {q.qid: q.question for q in LongMemEvalLoader(dataset).questions()}
    rows = [json.loads(l) for l in gzip.open(rows_path, "rt")]
    by_q: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_q.setdefault(r["qid"], []).append(r)
    stamp = json.load(open(stamp_path))
    out_path = out_dir.rstrip("/") + "/ce.jsonl"
    done = set()
    try:
        for l in open(out_path):
            j = json.loads(l)
            done.add((j["qid"], j["level"]))
    except FileNotFoundError:
        pass
    fh = open(out_path, "a")
    t0 = time.time()
    for qi, (qid, rs) in enumerate(sorted(by_q.items())):
        if all((qid, r["level"]) in done for r in rs):
            continue
        need = []
        for r in rs:
            for key in ("exact", "es"):
                for d in r[key]["rrf"][:HEAD]:
                    if d not in need:
                        need.append(d)
        pairs = [(d, t) for d in need for t in chunk_text[d]]
        scores: List[float] = []
        for i in range(0, len(pairs), 8):
            b = pairs[i:i + 8]
            enc = tok([qtext[qid]] * len(b), [t for _, t in b], padding=True, truncation="only_second", max_length=512, return_tensors="pt")
            with torch.inference_mode():
                scores.extend(model(**enc).logits.view(-1).tolist())
        best: Dict[str, float] = {}
        for (d, _), s in zip(pairs, scores):
            best[d] = max(best.get(d, float("-inf")), s)
        for r in rs:
            rec = {"qid": qid, "level": r["level"], "stamp": stamp["commit"]}
            for key in ("exact", "es"):
                head = r[key]["rrf"][:HEAD]
                order = sorted(range(len(head)), key=lambda j: (-best[head[j]], j))
                rec["ce_" + key] = [head[j] for j in order]
            rec["head_same_set"] = set(r["exact"]["rrf"][:HEAD]) == set(r["es"]["rrf"][:HEAD])
            rec["head_same_order"] = r["exact"]["rrf"][:HEAD] == r["es"]["rrf"][:HEAD]
            rec["scores"] = {d: best[d] for d in set(r["exact"]["rrf"][:HEAD]) | set(r["es"]["rrf"][:HEAD])}
            fh.write(json.dumps(rec) + "\n")
        fh.flush()
        print("ce", qi + 1, len(by_q), "%d rounds %.0fs" % (len(need), time.time() - t0), flush=True)
    fh.close()


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "retrieve":
        retrieve(*sys.argv[2:9])
    elif cmd == "rerank":
        rerank(*sys.argv[2:9])
    else:
        raise SystemExit("usage: retrieve|rerank")
