"""Round-level Elasticsearch parity (THRESHOLDS.md section P: P1 to P5 and the mismatched-analyzer control).

Everything here is written for this stream; the owner's emlab package is not imported. The exact BM25 follows the spec
written in THRESHOLDS.md (the owner's ExactRetriever as documented: lowercase [a-z0-9]+, 33-word stoplist, no stemming,
k1 1.2, b 0.75, corpus-wide df / avgdl over all chunks, chunk -> round by max score, ties by round id).

usage: python round_parity.py build   # create rep16096-par-chunks on the own node (three analyzers), full corpus
       python round_parity.py run     # compute rankings and overlaps, write results/round_parity.json
"""
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
import repc  # noqa: E402

ART = os.path.join(repc.H, "out", "emb")
DATA = os.path.join(repc.H, "data", "longmemeval_s_cleaned.json")
QIDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "inputs", "pilot102_qids.json")
IDX = "rep16096-par-chunks"
SHARED_IDX = "emlab-rounds-v1"
STOP = ("a an and are as at be but by for if in into is it no not of on or such that the their then there these "
        "they this to was will with").split()
assert len(STOP) == 33
_TOK = re.compile(r"[a-z0-9]+")
_STOP = frozenset(STOP)


def toks(s):
    return [w for w in _TOK.findall(s.lower()) if w not in _STOP]


def chunks():
    with open(os.path.join(ART, "chunks.jsonl"), encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def build():
    es = repc.rep_es()
    es.req("DELETE", "/" + IDX)
    body = {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1",
                         "analysis": {"analyzer": {
                             "matched": {"type": "custom", "tokenizer": "alnum", "filter": ["lowercase", "stop33"]}},
                             "tokenizer": {"alnum": {"type": "pattern", "pattern": "[^A-Za-z0-9]+"}},
                             "filter": {"stop33": {"type": "stop", "stopwords": STOP}}}},
            "mappings": {"properties": {"chunk_id": {"type": "keyword"}, "round_id": {"type": "keyword"},
                                        "session_id": {"type": "keyword"},
                                        "text_en": {"type": "text", "analyzer": "english"},
                                        "text_m": {"type": "text", "analyzer": "matched"},
                                        "text_ws": {"type": "text", "analyzer": "whitespace"}}}}
    c, j, _ = es.req("PUT", "/" + IDX, body)
    assert c == 200, j
    buf, n, t0 = [], 0, time.time()

    def flush():
        nd = "".join(buf)
        c, j, _ = es.req("POST", "/%s/_bulk" % IDX, ndjson=nd, timeout=600)
        assert c == 200 and not j.get("errors"), (c, str(j)[:500])
        buf.clear()
    for ch in chunks():
        buf.append(json.dumps({"index": {"_id": ch["chunk_id"]}}) + "\n" + json.dumps(
            {"chunk_id": ch["chunk_id"], "round_id": ch["round_id"], "session_id": ch["session_id"],
             "text_en": ch["text"], "text_m": ch["text"], "text_ws": ch["text"]}) + "\n")
        n += 1
        if len(buf) >= 2000:
            flush()
    if buf:
        flush()
    es.req("PUT", "/%s/_settings" % IDX, {"index": {"refresh_interval": "1s"}})
    es.req("POST", "/%s/_refresh" % IDX)
    es.req("POST", "/%s/_forcemerge" % IDX, max_num_segments=1, timeout=1800)
    c, j, _ = es.req("GET", "/%s/_count" % IDX)
    print("indexed", n, "count", j.get("count"), "s", round(time.time() - t0, 1))


def rollup_max(pairs, depth):
    best = {}
    for rid, s in pairs:
        if rid not in best or s > best[rid]:
            best[rid] = s
    return [r for r, _ in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:depth]]


def rollup_first(chunk_ids):
    seen, out = set(), []
    for c in chunk_ids:
        r = c.split("#")[0]
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def rrf(lists, k=60):
    sc, first = {}, {}
    for li, lst in enumerate(lists):
        seen = set()
        for pos, d in enumerate(lst):
            if d in seen:
                continue
            seen.add(d)
            sc[d] = sc.get(d, 0.0) + 1.0 / (k + pos + 1)
            first.setdefault(d, (pos, li))
    return [d for d, _ in sorted(sc.items(), key=lambda kv: (-kv[1], first[kv[0]]))]


def minmax_linear(score_dicts, w):
    tot = {}
    for sd, wi in zip(score_dicts, w):
        if not sd:
            continue
        lo, hi = min(sd.values()), max(sd.values())
        for d, s in sd.items():
            tot[d] = tot.get(d, 0.0) + wi * ((s - lo) / (hi - lo) if hi > lo else 1.0)
    return [d for d, _ in sorted(tot.items(), key=lambda kv: (-kv[1], repr(kv[0])))]


def ov(a, b, k=10):
    return len(set(a[:k]) & set(b[:k])) / float(k)


def run():
    t0 = time.time()
    qids = json.load(open(QIDS))["qids"]
    want = set(qids)
    data = {q["question_id"]: q for q in json.load(open(DATA)) if q["question_id"] in want}
    assert len(data) == len(qids)
    # corpus-wide statistics and per-session chunk lists
    meta, dl, df = [], [], Counter()
    by_sess = defaultdict(list)
    need = set(s for q in data.values() for s in q["haystack_session_ids"])
    tf_cache = {}
    for i, ch in enumerate(chunks()):
        tk = toks(ch["text"])
        dl.append(len(tk))
        df.update(set(tk))
        meta.append((ch["chunk_id"], ch["round_id"], ch["session_id"]))
        if ch["session_id"] in need:
            by_sess[ch["session_id"]].append(i)
            tf_cache[i] = Counter(tk)
    N = len(meta)
    avgdl = sum(dl) / N
    vecs = np.load(os.path.join(ART, "chunk_vectors.npy"), mmap_mode="r")
    qorder = json.load(open(os.path.join(ART, "queries.json")))
    qv = dict(zip(qorder, np.load(os.path.join(ART, "query_vectors.npy"))))
    print("corpus", N, "avgdl", round(avgdl, 3), "load s", round(time.time() - t0, 1), flush=True)
    sh, own = repc.shared_es(), repc.rep_es()
    heur_before = sh.req("GET", "/%s/_settings" % SHARED_IDX, filter_path="**.hnsw_filter_heuristic")[1]
    rows = []
    for qid in qids:
        q = data[qid]
        sess = list(dict.fromkeys(q["haystack_session_ids"]))
        idx = [i for s in sess for i in by_sess.get(s, ())]
        qt = set(toks(q["question"]))
        bm = []
        for i in idx:
            tf = tf_cache[i]
            s = 0.0
            for w in qt:
                f = tf.get(w)
                if f:
                    idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl[i] / avgdl))
            bm.append((meta[i][1], s))
        ex_b = rollup_max(bm, 200)
        best = {}
        for r, s in bm:
            best[r] = max(best.get(r, s), s)
        sims = np.asarray(vecs[np.asarray(idx)]) @ qv[qid]
        dpairs = [(meta[i][1], float(s)) for i, s in zip(idx, sims)]
        ex_d = rollup_max(dpairs, 200)
        bestd = {}
        for r, s in dpairs:
            bestd[r] = max(bestd.get(r, s), s)
        flt = [{"terms": {"session_id": sess}}]
        res = {"qid": qid, "n_sessions": len(sess), "n_chunks": len(idx)}

        def es_bm(es, index, field):
            c, j, _ = es.req("POST", "/%s/_search" % index, {"size": 200, "_source": False, "query": {"bool": {
                "must": [{"match": {field: q["question"]}}], "filter": flt}}})
            assert c == 200, (c, str(j)[:300])
            return [h["_id"] for h in j["hits"]["hits"]]
        sh_b_chunks = es_bm(sh, SHARED_IDX, "text")
        sh_b = rollup_first(sh_b_chunks)
        own_en = rollup_first(es_bm(own, IDX, "text_en"))
        own_m = rollup_first(es_bm(own, IDX, "text_m"))
        own_ws = rollup_first(es_bm(own, IDX, "text_ws"))
        knn = {"field": "vector", "query_vector": [float(x) for x in qv[qid]], "k": 200, "num_candidates": 1000, "filter": flt}
        std = {"standard": {"query": {"bool": {"must": [{"match": {"text": q["question"]}}], "filter": flt}}}}
        c, j, _ = sh.req("POST", "/%s/_search" % SHARED_IDX, {"size": 200, "_source": False, "knn": knn})
        sh_knn_chunks = [h["_id"] for h in j["hits"]["hits"]]
        c, j, _ = sh.req("POST", "/%s/_search" % SHARED_IDX, {"size": 200, "_source": False, "retriever": {"rrf": {
            "retrievers": [std, {"knn": knn}], "rank_window_size": 200, "rank_constant": 60}}})
        assert c == 200, (c, str(j)[:300])
        sh_rrf = rollup_first([h["_id"] for h in j["hits"]["hits"]])
        c, j, _ = sh.req("POST", "/%s/_search" % SHARED_IDX, {"size": 200, "_source": False, "retriever": {"linear": {
            "retrievers": [{"retriever": std, "weight": 0.5, "normalizer": "minmax"},
                           {"retriever": {"knn": knn}, "weight": 0.5, "normalizer": "minmax"}], "rank_window_size": 200}}})
        assert c == 200, (c, str(j)[:300])
        sh_lin = rollup_first([h["_id"] for h in j["hits"]["hits"]])
        cl_rrf = rrf([ex_b, ex_d])
        cl_lin = minmax_linear([{r: best[r] for r in ex_b}, {r: bestd[r] for r in ex_d}], [0.5, 0.5])
        es_legs_rrf = rollup_first(rrf([sh_b_chunks, sh_knn_chunks]))
        res.update({
            "P1_shared_bm25_vs_exact": ov(sh_b, ex_b), "P2_own_english_vs_exact": ov(own_en, ex_b),
            "P2b_own_english_vs_shared_bm25": ov(own_en, sh_b),
            "P3_own_matched_vs_exact": ov(own_m, ex_b), "CP_own_whitespace_vs_exact": ov(own_ws, ex_b),
            "P4_client_rrf_vs_es_rrf": ov(cl_rrf, sh_rrf), "P4_client_rrf_same_order10": cl_rrf[:10] == sh_rrf[:10],
            "P4_client_linear_vs_es_linear": ov(cl_lin, sh_lin), "P4_client_linear_same_order10": cl_lin[:10] == sh_lin[:10],
            "P5_client_rrf_of_es_legs_vs_es_rrf": ov(es_legs_rrf, sh_rrf),
            "exact_bm25_top10": ex_b[:10], "shared_bm25_top10": sh_b[:10], "own_matched_top10": own_m[:10],
            "own_ws_top10": own_ws[:10]})
        rows.append(res)
    heur_after = sh.req("GET", "/%s/_settings" % SHARED_IDX, filter_path="**.hnsw_filter_heuristic")[1]
    keys = [k for k in rows[0] if k.startswith(("P", "CP")) and not k.endswith("order10")]
    summ = {k: sum(r[k] for r in rows) / len(rows) for k in keys}
    summ["P4_rrf_same_order10_share"] = sum(r["P4_client_rrf_same_order10"] for r in rows) / len(rows)
    summ["P4_linear_same_order10_share"] = sum(r["P4_client_linear_same_order10"] for r in rows) / len(rows)
    summ["CP_lower_than_P1_share"] = sum(r["CP_own_whitespace_vs_exact"] < r["P1_shared_bm25_vs_exact"] for r in rows) / len(rows)
    owner = {"P1": 0.7509803921568627, "rrf": 0.8166666666666667, "linear": 0.8627450980392157}
    verdict = {
        "P1_within_0.02": abs(summ["P1_shared_bm25_vs_exact"] - owner["P1"]) <= 0.02,
        "P2_within_0.02_of_P1": abs(summ["P2_own_english_vs_exact"] - summ["P1_shared_bm25_vs_exact"]) <= 0.02,
        "P3_ge_0.95": summ["P3_own_matched_vs_exact"] >= 0.95,
        "CP_drops": summ["CP_own_whitespace_vs_exact"] <= summ["P1_shared_bm25_vs_exact"] - 0.10 and summ["CP_lower_than_P1_share"] >= 0.10,
        "P4_rrf_within_0.03": abs(summ["P4_client_rrf_vs_es_rrf"] - owner["rrf"]) <= 0.03,
        "P4_linear_within_0.03": abs(summ["P4_client_linear_vs_es_linear"] - owner["linear"]) <= 0.03,
        "P5_ge_0.95": summ["P5_client_rrf_of_es_legs_vs_es_rrf"] >= 0.95}
    out = {"stamp": repc.stamp(__file__, {"qids": QIDS, "n": len(rows), "depth": 200, "rrf_k": 60, "knn_num_candidates": 1000,
                                          "corpus_chunks": N, "avgdl_exact": avgdl}),
           "es_shared": repc.es_fingerprint(sh, SHARED_IDX), "es_own": repc.es_fingerprint(own, IDX),
           "shared_heuristic_before": heur_before, "shared_heuristic_after": heur_after,
           "owner_figures": owner, "summary": summ, "threshold_checks": verdict, "rows": rows}
    p = repc.save("round_parity.json", out)
    print(json.dumps({"summary": summ, "checks": verdict}, indent=1))
    print("saved", p, "s", round(time.time() - t0, 1))


if __name__ == "__main__":
    {"build": build, "run": run}[sys.argv[1]]()
