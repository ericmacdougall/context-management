"""DESIGN 3.2.11 rerun at H2 on the own node (THRESHOLDS.md section P, "3.2.11"). Own code; the owner's emlab package is
not imported. Vectors are the owner's stored E5 vectors (read, not computed: no model call).

H2 = own haystack + the owner's DL list (pool_sets_pilot_v2.json, 3,900 sessions), minus a DL session whose content
equals an own session or that holds a round whose content equals a gold round of the question.
Session content hash: sha256(json.dumps([[role, content], ...], ensure_ascii=False)); round text: "role: content" lines
joined by newline, a round starting at each user turn.
Hybrid: RRF 60 over exact BM25 top-100 rounds and a dense leg (top-100 chunks rolled up to rounds by first occurrence).
Session recall@10 = share of gold sessions among the first 10 distinct sessions.

usage: python dense_h2.py build | run | run shared
  "run shared" repeats the run read-only against the owner's emlab-rounds-v1 on the shared trial node (same chunk ids,
  same fields), writing dense_h2_shared.json; it separates method differences from index-build variance.
"""
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
import repc  # noqa: E402

ART = os.path.join(repc.H, "out", "emb")
DATA = os.path.join(repc.H, "data", "longmemeval_s_cleaned.json")
SETS = os.path.join(repc.H, "out", "pool_sets_pilot_v2.json")
QIDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "inputs", "pilot102_qids.json")
IDX = "rep16096-vec-chunks"
K = 100
STOP = frozenset(("a an and are as at be but by for if in into is it no not of on or such that the their then there "
                  "these they this to was will with").split())
TOK = re.compile(r"[a-z0-9]+")


def toks(s):
    return [w for w in TOK.findall(s.lower()) if w not in STOP]


def chunks():
    with open(os.path.join(ART, "chunks.jsonl"), encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def build():
    es = repc.rep_es()
    es.req("DELETE", "/" + IDX)
    c, j, _ = es.req("PUT", "/" + IDX, {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1",
                                                     "index.dense_vector.hnsw_filter_heuristic": "acorn"},
                                        "mappings": {"properties": {
                                            "chunk_id": {"type": "keyword"}, "session_id": {"type": "keyword"},
                                            "vector": {"type": "dense_vector", "dims": 384, "index": True, "similarity": "cosine",
                                                       "index_options": {"type": "bbq_hnsw"}},
                                            "vector_int8": {"type": "dense_vector", "dims": 384, "index": True, "similarity": "cosine",
                                                            "index_options": {"type": "int8_hnsw"}}}}})
    assert c == 200, j
    vecs = np.load(os.path.join(ART, "chunk_vectors.npy"), mmap_mode="r")
    buf, t0 = [], time.time()
    for i, ch in enumerate(chunks()):
        v = [round(float(x), 7) for x in vecs[i]]
        buf.append(json.dumps({"index": {"_id": ch["chunk_id"]}}) + "\n" + json.dumps(
            {"chunk_id": ch["chunk_id"], "session_id": ch["session_id"], "vector": v, "vector_int8": v}) + "\n")
        if len(buf) >= 1000:
            c, j, _ = es.req("POST", "/%s/_bulk" % IDX, ndjson="".join(buf), timeout=900)
            assert c == 200 and not j.get("errors"), (c, str(j)[:400])
            buf = []
        if i % 20000 == 0:
            print("at", i, round(time.time() - t0, 1), flush=True)
    if buf:
        c, j, _ = es.req("POST", "/%s/_bulk" % IDX, ndjson="".join(buf), timeout=900)
        assert c == 200 and not j.get("errors"), (c, str(j)[:400])
    es.req("PUT", "/%s/_settings" % IDX, {"index": {"refresh_interval": "1s"}})
    es.req("POST", "/%s/_refresh" % IDX, timeout=1800)
    c, j, _ = es.req("GET", "/%s/_count" % IDX)
    print("count", j.get("count"), "s", round(time.time() - t0, 1), flush=True)


def recall10(sessions_ranked, gold):
    top = []
    for s in sessions_ranked:
        if s not in top:
            top.append(s)
        if len(top) == 10:
            break
    return len(set(top) & set(gold)) / len(gold)


def to_sessions(rounds):
    return [r.rsplit(":", 1)[0] for r in rounds]


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


def rollup_first(chunk_ids):
    seen, out = set(), []
    for c in chunk_ids:
        r = c.split("#")[0]
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def boot(diffs, n=2000, seed=16096):
    rng = random.Random(seed)
    m = len(diffs)
    means = sorted(sum(diffs[rng.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return {"mean_diff": sum(diffs) / m, "ci_low": means[int(0.025 * n)], "ci_high": means[int(0.975 * n) - 1], "n": m}


def run():
    t0 = time.time()
    data = json.load(open(DATA, encoding="utf-8"))
    sets = json.load(open(SETS))
    DL = sets["DL"]
    # content hashes over the whole dataset
    sh, rh = {}, defaultdict(set)
    for rec in data:
        seen = set()
        for sid, sess in zip(rec["haystack_session_ids"], rec["haystack_sessions"]):
            if sid in seen:
                continue
            seen.add(sid)
            if sid not in sh:
                sh[sid] = hashlib.sha256(json.dumps([[t["role"], t["content"]] for t in sess], ensure_ascii=False).encode("utf-8")).hexdigest()
                groups = []
                for t in sess:
                    if t["role"] == "user" or not groups:
                        groups.append([t])
                    else:
                        groups[-1].append(t)
                for g in groups:
                    rh[sid].add(hashlib.sha256("\n".join("%s: %s" % (t["role"], t["content"]) for t in g).encode("utf-8")).hexdigest())
    qids = set(json.load(open(QIDS))["qids"])
    qs = [q for q in data if q["question_id"] in qids and not q["question_id"].endswith("_abs") and q["answer_session_ids"]]
    levels = {}
    for q in qs:
        own = list(dict.fromkeys(q["haystack_session_ids"]))
        own_h = {sh[s] for s in own}
        gold_rounds = set()
        by = {}
        for sid, sess in zip(q["haystack_session_ids"], q["haystack_sessions"]):
            by.setdefault(sid, sess)
        for sid in own:
            groups = []
            for t in by[sid]:
                if t["role"] == "user" or not groups:
                    groups.append([t])
                else:
                    groups[-1].append(t)
            for g in groups:
                if any(t.get("has_answer") for t in g):
                    gold_rounds.add(hashlib.sha256("\n".join("%s: %s" % (t["role"], t["content"]) for t in g).encode("utf-8")).hexdigest())
        os_ = set(own)
        extra = [s for s in DL if s not in os_ and sh.get(s) not in own_h and not (rh.get(s, set()) & gold_rounds)]
        levels[q["question_id"]] = own + extra
    # corpus stats and postings for query terms
    want = set(w for q in qs for w in toks(q["question"]))
    meta, dl, df, post = [], [], Counter(), {w: [] for w in want}
    by_sess = defaultdict(list)
    for i, ch in enumerate(chunks()):
        tk = toks(ch["text"])
        dl.append(len(tk))
        df.update(set(tk))
        meta.append((ch["chunk_id"], ch["round_id"], ch["session_id"]))
        by_sess[ch["session_id"]].append(i)
        for w, n in Counter(t for t in tk if t in want).items():
            post[w].append((i, n))
    N = len(meta)
    avgdl = sum(dl) / N
    vecs = np.load(os.path.join(ART, "chunk_vectors.npy"), mmap_mode="r")
    qv = dict(zip(json.load(open(os.path.join(ART, "queries.json"))), np.load(os.path.join(ART, "query_vectors.npy"))))
    print("prep s", round(time.time() - t0, 1), "questions", len(qs), flush=True)
    shared = len(sys.argv) > 2 and sys.argv[2] == "shared"
    es = repc.shared_es() if shared else repc.rep_es()
    index = "emlab-rounds-v1" if shared else IDX
    heur = es.req("GET", "/%s/_settings" % index, filter_path="**.hnsw_filter_heuristic")[1]
    rng = np.random.default_rng(16096)
    prep = {}
    for q in qs:
        qid = q["question_id"]
        sess = levels[qid]
        mask = np.zeros(N, dtype=bool)
        idx = np.asarray([i for s in sess for i in by_sess.get(s, ())])
        mask[idx] = True
        score = {}
        for w in set(toks(q["question"])):
            idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
            for i, tf in post.get(w, ()):
                if mask[i]:
                    score[i] = score.get(i, 0.0) + idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * dl[i] / avgdl))
        best = {}
        for i, s in score.items():
            r = meta[i][1]
            if r not in best or s > best[r]:
                best[r] = s
        bm = [r for r, _ in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:K]]
        sims = np.asarray(vecs[idx]) @ qv[qid]
        top = np.argsort(-sims, kind="stable")[:K]
        dense_exact = rollup_first([meta[int(idx[t])][0] for t in top])
        rv = rng.standard_normal(384).astype(np.float32)
        rv /= np.linalg.norm(rv)
        simr = np.asarray(vecs[idx]) @ rv
        topr = np.argsort(-simr, kind="stable")[:K]
        dense_random = rollup_first([meta[int(idx[t])][0] for t in topr])
        prep[qid] = (bm, dense_exact, dense_random, sess)

    def metric(qid, dense):
        bm = prep[qid][0]
        gold = [g for g in dict.fromkeys(data_by[qid]["answer_session_ids"])]
        return recall10(to_sessions(rrf([bm, dense])), gold)
    data_by = {q["question_id"]: q for q in qs}
    base = {qid: metric(qid, prep[qid][1]) for qid in prep}
    rand = {qid: metric(qid, prep[qid][2]) for qid in prep}
    n = len(base)
    rows = [{"dense_leg": "exact", "hybrid_sess_recall10": sum(base.values()) / n, "n": n},
            {"dense_leg": "random_vector_control", "hybrid_sess_recall10": sum(rand.values()) / n, "n": n,
             "diff_vs_exact": boot([rand[k] - base[k] for k in base])}]
    per_q = {qid: {"exact": base[qid], "random": rand[qid], "n_sessions": len(prep[qid][3])} for qid in base}
    for field in ("vector", "vector_int8"):
        for nc in (100, 1000):
            for resc in ("none", "oversample3"):
                got, t1 = {}, time.time()
                for q in qs:
                    qid = q["question_id"]
                    knn = {"field": field, "query_vector": [float(x) for x in qv[qid]], "k": K, "num_candidates": nc,
                           "filter": {"terms": {"session_id": prep[qid][3]}},
                           "rescore_vector": {"oversample": 3 if resc == "oversample3" else 0}}
                    c, j, _ = es.req("POST", "/%s/_search" % index, {"size": K, "_source": False, "knn": knn})
                    assert c == 200, (c, str(j)[:300])
                    got[qid] = rollup_first([h["_id"] for h in j["hits"]["hits"]])
                res = {qid: metric(qid, got[qid]) for qid in got}
                for qid in res:
                    per_q[qid]["%s_nc%d_%s" % (field, nc, resc)] = res[qid]
                row = {"field": field, "num_candidates": nc, "rescore": resc, "hybrid_sess_recall10": sum(res.values()) / n,
                       "n": n, "diff_vs_exact": boot([res[k] - base[k] for k in base]), "wall_s": round(time.time() - t1, 1)}
                rows.append(row)
                print(row, flush=True)
    nores = [r for r in rows if r.get("rescore") == "none"]
    worst = min(r["diff_vs_exact"]["mean_diff"] for r in nores)
    bbq_os3 = [r for r in rows if r.get("field") == "vector" and r.get("rescore") == "oversample3"]
    get = lambda f, nc, rs: next(r for r in rows if r.get("field") == f and r.get("num_candidates") == nc and r.get("rescore") == rs)
    checks = {"worst_norescore_loss": worst,
              "a_worst_loss_in_0.04_0.12": 0.04 <= -worst <= 0.12,
              "a_some_norescore_ci_excludes_0": any(r["diff_vs_exact"]["ci_high"] < 0 for r in nores),
              "b_all_bbq_os3_ci_include_0": all(r["diff_vs_exact"]["ci_low"] <= 0 <= r["diff_vs_exact"]["ci_high"] for r in bbq_os3),
              "control_random_loses_ge_0.10": rows[1]["diff_vs_exact"]["mean_diff"] <= -0.10,
              "sensitivity_bbq_nc100_none_below_os3": get("vector", 100, "none")["hybrid_sess_recall10"] < get("vector", 100, "oversample3")["hybrid_sess_recall10"]}
    heur_after = es.req("GET", "/%s/_settings" % index, filter_path="**.hnsw_filter_heuristic")[1]
    c, j, _ = es.req("GET", "/_cat/segments/%s" % index, format="json")
    out = {"stamp": repc.stamp(__file__, {"level": "H2", "K": K, "rrf_k": 60, "bootstrap": "per-question, 2000 draws, seed 16096",
                                          "DL_source": SETS}),
           "es": repc.es_fingerprint(es, index), "heuristic": heur, "heuristic_after": heur_after, "segments": len(j) if isinstance(j, list) else None,
           "level_sizes": {"min": min(len(v) for v in levels.values()), "max": max(len(v) for v in levels.values())},
           "owner_figures": {"H2_exact_hybrid": 0.6950354609929078, "H2_acorn_bbq_nc100_none": 0.617,
                             "claim": "no-rescore loss up to about 0.08 at H2; bbq oversample 3 loss intervals include 0"},
           "rows": rows, "threshold_checks": checks, "per_question": per_q}
    p = repc.save("dense_h2_shared.json" if shared else "dense_h2.json", out)
    print(json.dumps(checks, indent=1), "saved", p, "s", round(time.time() - t0, 1))


if __name__ == "__main__":
    {"build": build, "run": run}[sys.argv[1]]()
