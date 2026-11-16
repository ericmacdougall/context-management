"""E4 candidate ceiling, E5 (a)-(c) first-stage variants with wrong-query controls, E6 length shortcut (exploratory,
pilot only, no model; see EXPLORATORY.md round 2). New file. Exact in-process retrieval at level H3 on its own chunk
arrays (BM25 k1 1.2 b 0.75 over chunk postings, E5 cosine); NOT Elasticsearch."""
from __future__ import annotations

import json
import math
import os
import pickle
import sys
from collections import Counter
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from .anon import AnonMap
from .build_rag_contexts import load_common
from .fusion import rrf
from .live import ExactRetriever
from .metrics import clustered_paired_bootstrap, recall_at_k, wilson_interval
from .retrievers import to_sessions, tokens
from .xp_common import cluster_wilson, derangement_list

SEED = 16096
KS = (50, 100, 200, 400, 1000)
DEPTH = 1000


class Engine:
    def __init__(self, art: str, ex: ExactRetriever, texts: Dict[str, str]):
        a = art.rstrip("/") + "/"
        pk = pickle.load(open(a + "postings.pkl", "rb"))
        self.post, self.dl, self.n = pk["post"], np.asarray(pk["dl"], dtype=np.float64), pk["n"]
        self.avgdl = float(self.dl.mean())
        self.ex, self.texts = ex, texts
        self.V = ex.vectors
        rid = [c["round_id"] for c in ex.chunks]
        self.rounds = sorted(set(rid))
        ridx = {r: i for i, r in enumerate(self.rounds)}
        self.round_of_chunk = np.fromiter((ridx[r] for r in rid), dtype=np.int64, count=len(rid))
        self.sess_of_round = [r.rsplit(":", 1)[0] for r in self.rounds]
        self.session_len: Dict[str, float] = {}
        for c, d in zip(ex.chunks, self.dl):
            self.session_len[c["session_id"]] = self.session_len.get(c["session_id"], 0.0) + float(d)
        self.df = {w: len(p[0]) for w, p in self.post.items()}

    def mask(self, session_ids: Sequence[str]) -> np.ndarray:
        m = np.zeros(self.n, dtype=bool)
        for s in session_ids:
            ids = self.ex.by_session.get(s)
            if ids:
                m[ids] = True
        return m

    def _rollup(self, pos: np.ndarray, scores: np.ndarray, limit: int) -> List[Tuple[str, float]]:
        if len(pos) == 0:
            return []
        order = np.argsort(-scores, kind="stable")
        r = self.round_of_chunk[pos[order]]
        _, first = np.unique(r, return_index=True)
        first.sort()
        out = []
        for j in first[:limit]:
            out.append((self.rounds[int(r[j])], float(scores[order[j]])))
        return out

    def bm25(self, weighted_terms: Dict[str, float], mask: np.ndarray, limit: int = DEPTH):
        score = np.zeros(self.n)
        for w, m in weighted_terms.items():
            p = self.post.get(w)
            if p is None:
                continue
            idx, tf = p
            keep = mask[idx]
            idx, tf = idx[keep], tf[keep].astype(np.float64)
            df = self.df[w]
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            score[idx] += m * idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * self.dl[idx] / self.avgdl))
        pos = np.nonzero(score > 0)[0]
        return self._rollup(pos, score[pos], limit)

    def dense_scores(self, qvec, mask):
        pos = np.nonzero(mask)[0]
        return pos, np.asarray(self.V[pos]) @ np.asarray(qvec, dtype=np.float32)

    def dense(self, qvec, mask, limit: int = DEPTH):
        pos, sims = self.dense_scores(qvec, mask)
        return self._rollup(pos, sims, limit)

    def flat(self, qtext, qvec, mask):
        b = self.bm25({w: 1.0 for w in set(tokens(qtext))}, mask)
        d = self.dense(qvec, mask)
        fused = rrf([[r for r, _ in b], [r for r, _ in d]], k=60)
        return fused, b, d


def idf_of(eng: Engine, w: str) -> float:
    df = eng.df.get(w, 0)
    return math.log(1 + (eng.n - df + 0.5) / (df + 0.5))


def boosted_terms(eng: Engine, qtext: str) -> Dict[str, float]:
    terms = set(tokens(qtext))
    idfs = {w: idf_of(eng, w) for w in terms if w in eng.post}
    if not idfs:
        return {w: 1.0 for w in terms}
    mean = sum(idfs.values()) / len(idfs)
    words = qtext.replace("?", " ").replace(",", " ").split()
    cap = {x.strip(".'\"()").lower() for i, x in enumerate(words) if i > 0 and x[:1].isupper()}
    out = {}
    for w in terms:
        m = (idfs.get(w, 0.0) / mean) if mean > 0 else 1.0
        if w.isdigit() or w in cap:
            m *= 1.5
        out[w] = m
    return out


def variant_a(eng, qtext, qvec, mask):
    fused, _, _ = eng.flat(qtext, qvec, mask)
    top = fused[:400]
    ssum: Dict[str, float] = {}
    for r, s in top:
        sid = r.rsplit(":", 1)[0]
        ssum[sid] = ssum.get(sid, 0.0) + s
    order = sorted(top, key=lambda x: (-ssum[x[0].rsplit(":", 1)[0]], -x[1], x[0]))
    return [(r, s) for r, s in order] + [x for x in fused[400:]]


def variant_b(eng, qtext, qvec, mask):
    b = eng.bm25(boosted_terms(eng, qtext), mask)
    d = eng.dense(qvec, mask)
    return rrf([[r for r, _ in b], [r for r, _ in d]], k=60)


def variant_c(eng, qtext, qvec, mask):
    fused, _, _ = eng.flat(qtext, qvec, mask)
    top5 = [r for r, _ in fused[:5]]
    qt = set(tokens(qtext))
    tf: Counter = Counter()
    for r in top5:
        tf.update(tokens(eng.texts[r]))
    cand = sorted(((c * idf_of(eng, w), w) for w, c in tf.items() if w not in qt and w in eng.post), reverse=True)[:8]
    terms = {w: 1.0 for w in qt}
    terms.update({w: 0.4 for _, w in cand})
    b = eng.bm25(terms, mask)
    pos, sims = eng.dense_scores(qvec, mask)
    top3 = pos[np.argsort(-sims, kind="stable")[:3]]
    q2 = np.asarray(qvec, dtype=np.float32) + 0.5 * np.asarray(eng.V[top3]).mean(0)
    q2 = q2 / (np.linalg.norm(q2) + 1e-12)
    d = eng.dense(q2, mask)
    return rrf([[r for r, _ in b], [r for r, _ in d]], k=60)


VARIANTS = {"a_session_sum_pooling": variant_a, "b_rare_term_boost": variant_b, "c_prf_rocchio": variant_c}


def metrics_at(ranking: Sequence[Tuple[str, float]], gold: set, k: int) -> Dict[str, float]:
    top = [r for r, _ in ranking[:k]]
    sess = {r.rsplit(":", 1)[0] for r in top}
    return {"hit": 1.0 if sess & gold else 0.0, "session_recall": len(sess & gold) / len(gold), "sessions": float(len(sess))}


def sr10(ranking, gold) -> float:
    return recall_at_k(to_sessions([r for r, _ in ranking])[:10], list(gold), 10)


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def main(dataset, art, sets_path, out_root, rerank_snapshot, out_json):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    low = set(json.load(open(os.path.join(out_root, "lowoverlap_pool_h3.json")))["thresholds"]["100"]["qids"])
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    qvec = ex.qvec
    eng = Engine(art, ex, texts)
    clusters = ctx.clusters
    qid_list = [q.qid for q in qs]
    der = derangement_list(len(qid_list), SEED)
    wrong = {qid_list[i]: qid_list[der[i]] for i in range(len(qid_list))}
    qtext = {q.qid: q.question for q in qs}
    res: Dict[str, Any] = {"retriever": "exact in-process (own chunk-array BM25 + E5 cosine); not Elasticsearch", "level": "H3",
                           "n": len(qs), "low_overlap_n": len(low & set(qid_list))}
    ranks: Dict[str, Dict[str, Any]] = {}
    masks = {}
    for q in qs:
        ids = ctx.level_sessions(q, "H3", sets)
        masks[q.qid] = (ids, eng.mask(ids))
    # ---------------- E4 and baselines
    flat_r, bm_r, de_r = {}, {}, {}
    for q in qs:
        flat_r[q.qid], bm_r[q.qid], de_r[q.qid] = eng.flat(q.question, qvec[q.qid], masks[q.qid][1])
    gold = {q.qid: set(q.gold_session_ids) for q in qs}
    e4: Dict[str, Any] = {}
    for name, R in (("BM25", bm_r), ("dense", de_r), ("flat RRF", flat_r)):
        e4[name] = {}
        for sub, ids_ in (("all", qid_list), ("low_overlap", [x for x in qid_list if x in low]), ("rest", [x for x in qid_list if x not in low])):
            row = {}
            for k in KS:
                ms = [metrics_at(R[x], gold[x], k) for x in ids_]
                w = cluster_wilson({x: m["hit"] for x, m in zip(ids_, ms)}, clusters)
                row[str(k)] = {"hit": w["rate"], "hit_wilson": [w["wilson_low"], w["wilson_high"]], "session_recall": mean(m["session_recall"] for m in ms),
                               "mean_distinct_sessions": mean(m["sessions"] for m in ms), "n": len(ids_), "n_clusters": w["n_clusters"]}
            e4[name][sub] = row
    # cross-encoder v2 rankings (head 20 reordered, tail = RRF) at K=20, 50, 100 for finished questions
    rr = {}
    for r in json.load(open(rerank_snapshot))["per_question"]:
        rr[r["qid"]] = [(x, 0.0) for x in r["levels"]["H3"]["rankings"]["rerank"]]
    e4["cross-encoder v2 (head 20)"] = {}
    for sub, ids_ in (("all", [x for x in qid_list if x in rr]), ("low_overlap", [x for x in qid_list if x in rr and x in low])):
        e4["cross-encoder v2 (head 20)"][sub] = {str(k): {"hit": mean(metrics_at(rr[x], gold[x], k)["hit"] for x in ids_),
                                                         "session_recall": mean(metrics_at(rr[x], gold[x], k)["session_recall"] for x in ids_), "n": len(ids_)}
                                                 for k in (20, 50, 100)}
    res["e4"] = e4
    print("E4 done", flush=True)
    # ---------------- E5 (a)-(c) with controls
    e5: Dict[str, Any] = {}
    flat_wrong = {}
    for q in qs:
        w = wrong[q.qid]
        flat_wrong[q.qid] = eng.flat(qtext[w], qvec[w], masks[q.qid][1])[0]
    for name, fn in VARIANTS.items():
        real_val, wrong_val = {}, {}
        for q in qs:
            m = masks[q.qid][1]
            rv = fn(eng, q.question, qvec[q.qid], m)
            real_val[q.qid] = (metrics_at(rv, gold[q.qid], 50)["session_recall"], metrics_at(rv, gold[q.qid], 50)["hit"], sr10(rv, gold[q.qid]))
            w = wrong[q.qid]
            wv = fn(eng, qtext[w], qvec[w], m)
            wrong_val[q.qid] = (metrics_at(wv, gold[q.qid], 50)["session_recall"], metrics_at(wv, gold[q.qid], 50)["hit"], sr10(wv, gold[q.qid]))
        flat_val = {q.qid: (metrics_at(flat_r[q.qid], gold[q.qid], 50)["session_recall"], metrics_at(flat_r[q.qid], gold[q.qid], 50)["hit"], sr10(flat_r[q.qid], gold[q.qid])) for q in qs}
        flatw_val = {q.qid: (metrics_at(flat_wrong[q.qid], gold[q.qid], 50)["session_recall"], metrics_at(flat_wrong[q.qid], gold[q.qid], 50)["hit"], sr10(flat_wrong[q.qid], gold[q.qid])) for q in qs}
        out: Dict[str, Any] = {}
        for sub, ids_ in (("low_overlap", [x for x in qid_list if x in low]), ("all", qid_list)):
            def diff(a_, b_, i):
                rows = [(clusters[x], b_[x][i], a_[x][i]) for x in ids_]
                r = clustered_paired_bootstrap(rows, n_boot=2000, seed=SEED)
                return {"mean_diff": r["mean_diff"], "ci_low": r["ci_low"], "ci_high": r["ci_high"], "clusters": int(r["effective_n"])}
            out[sub] = {"session_recall_at50_rounds": {"variant": mean(real_val[x][0] for x in ids_), "flat": mean(flat_val[x][0] for x in ids_),
                                                         "diff": diff(real_val, flat_val, 0)},
                        "hit_at50_rounds": {"variant": mean(real_val[x][1] for x in ids_), "flat": mean(flat_val[x][1] for x in ids_), "diff": diff(real_val, flat_val, 1)},
                        "session_recall_at10": {"variant": mean(real_val[x][2] for x in ids_), "flat": mean(flat_val[x][2] for x in ids_), "diff": diff(real_val, flat_val, 2)},
                        "control_wrong_query": {"variant": mean(wrong_val[x][0] for x in ids_), "flat_wrong_query": mean(flatw_val[x][0] for x in ids_),
                                                "diff_vs_flat_wrong": diff(wrong_val, flatw_val, 0)}}
        d = out["low_overlap"]["session_recall_at50_rounds"]["diff"]
        cd = out["low_overlap"]["control_wrong_query"]["diff_vs_flat_wrong"]
        out["held"] = bool(d["mean_diff"] >= 0.05 and d["ci_low"] > 0)
        out["control_credited_gain"] = bool(cd["mean_diff"] >= 0.05)
        e5[name] = out
        print("E5", name, json.dumps({"low": out["low_overlap"]["session_recall_at50_rounds"], "held": out["held"], "control_gain": cd["mean_diff"]}), flush=True)
    res["e5"] = e5

    # ---------------- E6 length shortcut
    slen = eng.session_len
    rows6: Dict[str, Dict[str, Any]] = {}
    arms_lists: Dict[str, Dict[str, List[str]]] = {"BM25": {x: [r for r, _ in bm_r[x]] for x in qid_list}, "dense": {x: [r for r, _ in de_r[x]] for x in qid_list},
                                                    "RRF (flat)": {x: [r for r, _ in flat_r[x]] for x in qid_list}}
    anon = AnonMap(list(ctx.hashes))
    for x in qid_list:
        tp = os.path.join(out_root, "analyst_tasks", "H3", x + ".json")
        task = json.load(open(tp))
        arms_lists.setdefault("RRF top-50 candidates", {})[x] = [anon.real_rid(c["round_id"]) for c in task["candidates"]]
    for arm in ("ANALYST", "ANALYST-RERANK-ONLY"):
        arms_lists[arm] = {}
        for x in qid_list:
            rp = os.path.join("/home/eric/emlab-16096/in/analyst_results", arm, "H3", x + ".json")
            rj = json.load(open(rp))
            arms_lists[arm][x] = [anon.real_rid(i) for i in (rj.get("ranked_round_ids") or [])] if rj.get("status") == "ok" else []
    arms_lists["cross-encoder v2"] = {x: [r for r, _ in rr[x]] for x in qid_list if x in rr}
    matched: Dict[str, set] = {}
    lens_only = {}
    for q in qs:
        ids = masks[q.qid][0]
        gl = [slen[s] for s in q.gold_session_ids if s in slen]
        lo, hi = 0.75 * min(gl), 1.25 * max(gl)
        matched[q.qid] = {s for s in ids if lo <= slen.get(s, 0) <= hi} | set(q.gold_session_ids)
        by_len = sorted(ids, key=lambda s: (-slen.get(s, 0), s))
        lens_only[q.qid] = recall_at_k(by_len[:10], q.gold_session_ids, 10)
    res["e6"] = {"length_only_session_recall_at10": {"all": mean(lens_only.values()), "low_overlap": mean(lens_only[x] for x in qid_list if x in low)},
                 "matched_pool_size_mean": mean(len(matched[x]) for x in qid_list), "level_pool_size_mean": mean(len(masks[x][0]) for x in qid_list), "arms": {}}
    per_arm_vals: Dict[str, Dict[str, Tuple[float, float]]] = {}
    for arm, L in arms_lists.items():
        vals = {}
        for x in L:
            sess = to_sessions(L[x])
            full = recall_at_k(sess[:10], list(gold[x]), 10)
            mat = recall_at_k([s for s in sess if s in matched[x]][:10], list(gold[x]), 10)
            vals[x] = (full, mat)
        per_arm_vals[arm] = vals
        res["e6"]["arms"][arm] = {"n": len(vals), "unmatched_session_recall10": mean(v[0] for v in vals.values()),
                                  "length_matched_filtered_session_recall10": mean(v[1] for v in vals.values()),
                                  "low_overlap_unmatched": mean(v[0] for x, v in vals.items() if x in low), "low_overlap_matched": mean(v[1] for x, v in vals.items() if x in low)}
    # re-retrieval inside the matched pool for BM25, dense, flat RRF
    rer: Dict[str, Dict[str, float]] = {"BM25": {}, "dense": {}, "RRF (flat)": {}}
    for q in qs:
        m2 = eng.mask(sorted(matched[q.qid]))
        f, b, d = eng.flat(q.question, qvec[q.qid], m2)
        rer["BM25"][q.qid] = sr10(b, gold[q.qid])
        rer["dense"][q.qid] = sr10(d, gold[q.qid])
        rer["RRF (flat)"][q.qid] = sr10(f, gold[q.qid])
    for arm, v in rer.items():
        res["e6"]["arms"][arm]["re_retrieval_in_matched_pool_session_recall10"] = mean(v.values())
        res["e6"]["arms"][arm]["low_overlap_re_retrieval_in_matched_pool"] = mean(v[x] for x in v if x in low)
    # paired differences under both evaluations
    def pdiff(a_, b_, idx, subset):
        ids_ = [x for x in subset if x in per_arm_vals[a_] and x in per_arm_vals[b_]]
        r = clustered_paired_bootstrap([(clusters[x], per_arm_vals[b_][x][idx], per_arm_vals[a_][x][idx]) for x in ids_], n_boot=2000, seed=SEED)
        return {"n": len(ids_), "mean_diff": r["mean_diff"], "ci_low": r["ci_low"], "ci_high": r["ci_high"]}
    res["e6"]["paired"] = {}
    for a_, b_ in (("ANALYST", "RRF top-50 candidates"), ("cross-encoder v2", "RRF (flat)"), ("ANALYST", "cross-encoder v2"), ("ANALYST-RERANK-ONLY", "RRF top-50 candidates")):
        res["e6"]["paired"]["%s minus %s" % (a_, b_)] = {
            "unmatched_all": pdiff(a_, b_, 0, qid_list), "matched_all": pdiff(a_, b_, 1, qid_list),
            "unmatched_low": pdiff(a_, b_, 0, [x for x in qid_list if x in low]), "matched_low": pdiff(a_, b_, 1, [x for x in qid_list if x in low])}
    json.dump(res, open(out_json, "w"), indent=1)
    print("done", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:7])
