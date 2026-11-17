"""E2 two-stage retrieval (exploratory, pilot only, no model; see EXPLORATORY.md). New file.
Stage 1 ranks SESSIONS (session BM25 + mean-pooled dense, RRF 60); stage 2 runs the existing round-level hybrid inside the
top S sessions. Exact in-process retriever only; no date, as-of or id field is used."""
from __future__ import annotations

import json
import math
import pickle
import random
import sys
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .context import context_metrics, pack_rag
from .dataset import LongMemEvalLoader, select_split
from .fusion import rrf
from .live import ExactRetriever
from .metrics import clustered_paired_bootstrap, recall_at_k
from .pool import PoolContext, with_sessions
from .xp_common import cluster_wilson, derangement_list
from .retrievers import run_arm, to_sessions, tokens

GRID = (500, 1000, 2000, 4000, 8000, 16000, 32000)
SEED = 16096


class SessionIndex:
    def __init__(self, art: str, ex: ExactRetriever):
        a = art.rstrip("/") + "/"
        self.sessions = sorted(ex.by_session)
        self.sidx = {s: i for i, s in enumerate(self.sessions)}
        n = len(ex.chunks)
        self.chunk2s = np.fromiter((self.sidx[c["session_id"]] for c in ex.chunks), dtype=np.int64, count=n)
        self.ns = len(self.sessions)
        self.slen = np.bincount(self.chunk2s, weights=np.asarray(ex.dl, dtype=np.float64), minlength=self.ns)
        self.avg = float(self.slen.mean())
        pk = pickle.load(open(a + "postings.pkl", "rb"))
        self.post = pk["post"]
        order = np.argsort(self.chunk2s, kind="stable")
        starts = np.searchsorted(self.chunk2s[order], np.arange(self.ns))
        V = np.asarray(ex.vectors)
        sums = np.add.reduceat(V[order], starts, axis=0)
        norm = np.linalg.norm(sums, axis=1, keepdims=True)
        self.mean_vec = (sums / np.maximum(norm, 1e-12)).astype(np.float32)

    def bm25(self, terms: Sequence[str]) -> np.ndarray:
        score = np.zeros(self.ns)
        for w in set(terms):
            p = self.post.get(w)
            if p is None:
                continue
            idx, tf = p
            tfs = np.bincount(self.chunk2s[idx], weights=tf.astype(np.float64), minlength=self.ns)
            df = int(np.count_nonzero(tfs))
            idf = math.log(1 + (self.ns - df + 0.5) / (df + 0.5))
            nz = tfs > 0
            score[nz] += idf * tfs[nz] * 2.2 / (tfs[nz] + 1.2 * (0.25 + 0.75 * self.slen[nz] / self.avg))
        return score

    def dense(self, qvec) -> np.ndarray:
        return self.mean_vec @ np.asarray(qvec, dtype=np.float32)


def derangement(n: int, seed: int) -> np.ndarray:
    return np.asarray(derangement_list(n, seed))


def stage1(si: SessionIndex, q, qvec, ids: Sequence[str], perm: Optional[np.ndarray] = None) -> List[str]:
    idx = np.asarray([si.sidx[s] for s in ids if s in si.sidx])
    bm, de = si.bm25(tokens(q.question)), si.dense(qvec)
    if perm is not None:  # control: each session id carries another session's document
        bm, de = bm[perm], de[perm]
    sb, sd = bm[idx], de[idx]
    ob = [ids_i for ids_i in np.argsort(-sb, kind="stable")]
    od = [ids_i for ids_i in np.argsort(-sd, kind="stable")]
    names = [si.sessions[i] for i in idx]
    lb = [names[j] for j in ob if sb[j] > 0] or [names[j] for j in ob[:1]]
    ld = [names[j] for j in od]
    return [s for s, _ in rrf([lb, ld], k=60)]


def two_stage(ex, si, q, qvec, ids, S, perm=None, inject=None):
    top = stage1(si, q, qvec, ids, perm)[:S]
    if inject:
        top = list(dict.fromkeys(top + [g for g in inject if g in set(ids)]))
    ranked = run_arm(ex, with_sessions(q, top), "rrf", 200, "off")
    return ranked, top


def auroc_len(slen_by_id: Dict[str, float], ids: Sequence[str], gold: set):
    vals = np.asarray([slen_by_id[s] for s in ids], dtype=np.float64)
    order = np.argsort(vals, kind="stable")
    ranks = np.empty(len(vals))
    sv = vals[order]
    i = 0
    while i < len(sv):
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    g = [k for k, s in enumerate(ids) if s in gold]
    return ranks, g


def main(dataset, art, sets_path, out_root, out_json):
    all_qs = list(LongMemEvalLoader(dataset).questions())
    qs = [q for q in select_split(all_qs, "pilot") if q.retrieval_gold_session_ids]
    ctx = PoolContext(all_qs)
    sets = json.load(open(sets_path))
    low = set(json.load(open(out_root.rstrip("/") + "/lowoverlap_pool_h3.json"))["thresholds"]["100"]["qids"])
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    si = SessionIndex(art, ex)
    qv = dict(zip(json.load(open(a + "queries.json")), np.load(a + "query_vectors.npy")))
    texts = {}
    for line in open(a + "rounds.jsonl", encoding="utf-8"):
        r = json.loads(line)
        texts[r["round_id"]] = r["text"]
    clusters = ctx.clusters
    perm = derangement(si.ns, SEED)
    slen_id = {s: float(si.slen[i]) for s, i in si.sidx.items()}
    res: Dict[str, Any] = {"retriever": "exact in-process (BM25 + E5 cosine); not Elasticsearch", "split": "pilot", "level": "H3",
                           "n_questions": len(qs), "low_overlap_n": len([q for q in qs if q.qid in low])}

    # ---- label-blind probe (session length vs gold-ness), 1,000-permutation null
    rng = np.random.default_rng(SEED)
    perq = []
    for q in qs:
        ids = ctx.level_sessions(q, "H3", sets)
        ranks, g = auroc_len(slen_id, ids, set(q.gold_session_ids))
        N, k = len(ids), len(g)
        if k == 0 or k == N:
            continue
        u = ranks[g].sum() - k * (k + 1) / 2.0
        perq.append((N, k, u / (k * (N - k)), ranks))
    real_mean = sum(p[2] for p in perq) / len(perq)
    null_means = np.zeros(1000)
    for N, k, _, ranks in perq:
        for b in range(1000):
            samp = rng.choice(N, size=k, replace=False)
            null_means[b] += (ranks[samp].sum() - k * (k + 1) / 2.0) / (k * (N - k))
    null_means /= len(perq)
    res["label_blind_probe"] = {"feature": "session length in tokens (gold vs other level sessions)", "mean_auroc": real_mean,
                                "null_p2.5": float(np.percentile(null_means, 2.5)), "null_p97.5": float(np.percentile(null_means, 97.5)),
                                "length_shortcut_present": bool(real_mean < np.percentile(null_means, 2.5) or real_mean > np.percentile(null_means, 97.5)),
                                "n_questions": len(perq)}
    print(json.dumps(res["label_blind_probe"]), flush=True)

    # ---- E2 at H3
    flat: Dict[str, float] = {}
    flat_rank: Dict[str, List[str]] = {}
    for q in qs:
        ids = ctx.level_sessions(q, "H3", sets)
        rk = run_arm(ex, with_sessions(q, ids), "rrf", 400, "off")
        flat_rank[q.qid] = rk
        flat[q.qid] = recall_at_k(to_sessions(rk), q.gold_session_ids, 10)
    variants: Dict[str, Dict[str, Any]] = {}
    for S in (20, 50):
        for name, kw in (("two_stage", {}), ("control_shuffled", {"perm": perm}), ("planted_gold_oracle", {"inject": True})):
            if name == "planted_gold_oracle" and S != 50:
                continue
            per, rec_s, hit = {}, {}, {}
            for q in qs:
                ids = ctx.level_sessions(q, "H3", sets)
                rk, top = two_stage(ex, si, q, qv[q.qid], ids, S, kw.get("perm"), q.gold_session_ids if kw.get("inject") else None)
                per[q.qid] = recall_at_k(to_sessions(rk), q.gold_session_ids, 10)
                rec_s[q.qid] = len(set(top) & set(q.gold_session_ids)) / len(set(q.gold_session_ids))
                hit[q.qid] = 1.0 if per[q.qid] and per[q.qid] > 0 else 0.0
            diff = clustered_paired_bootstrap([(clusters[q.qid], flat[q.qid], per[q.qid]) for q in qs], n_boot=2000, seed=SEED)
            sub = lambda ids_: {k: v for k, v in per.items() if k in ids_}
            lowq = {q.qid for q in qs if q.qid in low}
            otherq = {q.qid for q in qs} - lowq
            m = lambda d: sum(d.values()) / len(d)
            variants["%s_S%d" % (name, S)] = {
                "mean_session_recall10": m(per), "flat_mean": m(flat), "diff_vs_flat": {k: diff[k] for k in ("mean_diff", "ci_low", "ci_high", "lower_one_sided", "effective_n")},
                "stage1_recall_at_S": m(rec_s),
                "low_overlap_mean": m(sub(lowq)), "low_overlap_flat_mean": m({k: flat[k] for k in lowq}),
                "other_mean": m(sub(otherq)), "other_flat_mean": m({k: flat[k] for k in otherq}),
                "hit_rate_all": cluster_wilson(hit, clusters), "hit_rate_low_overlap": cluster_wilson({k: hit[k] for k in lowq}, clusters),
                "hit_rate_flat_all": cluster_wilson({k: (1.0 if flat[k] > 0 else 0.0) for k in flat}, clusters),
                "hit_rate_flat_low_overlap": cluster_wilson({k: (1.0 if flat[k] > 0 else 0.0) for k in lowq}, clusters),
                "pass_rule": bool(diff["mean_diff"] >= 0.10 and diff["ci_low"] > 0)}
            print(name, S, json.dumps({k: variants["%s_S%d" % (name, S)][k] for k in ("mean_session_recall10", "flat_mean", "diff_vs_flat", "stage1_recall_at_S", "pass_rule")}), flush=True)
    res["e2"] = variants

    # ---- X01 by-product: presence vs budget at H1, H2, H3
    pres: Dict[str, Any] = {}
    for lv in ("H1", "H2", "H3"):
        rows: Dict[str, Dict[int, Dict[str, float]]] = {"flat": {b: {} for b in GRID}, "two_stage_S50": {b: {} for b in GRID},
                                                       "planted_gold_first": {b: {} for b in GRID}}
        for q in qs:
            ids = ctx.level_sessions(q, lv, sets)
            dates = {s: "2023-01-01 00:00" for s in ids}
            rk = run_arm(ex, with_sessions(q, ids), "rrf", 400, "off")
            rk2, _ = two_stage(ex, si, q, qv[q.qid], ids, 50)
            gold_round = [r for r in q.retrieval_gold_round_ids if r in texts]
            planted = gold_round[:1] + [r for r in rk if r not in gold_round[:1]]
            for name, ranking in (("flat", rk), ("two_stage_S50", rk2), ("planted_gold_first", planted)):
                for b in GRID:
                    items = pack_rag(ranking, dates, texts, b)
                    sess = {i["session_id"] for i in items}
                    gs = set(q.gold_session_ids)
                    rows[name][b][q.qid] = {"any": 1.0 if sess & gs else 0.0, "all": 1.0 if gs <= sess else 0.0}
        pres[lv] = {}
        for name in rows:
            pres[lv][name] = {}
            for b in GRID:
                d = rows[name][b]
                pres[lv][name][str(b)] = {"any_gold_session": cluster_wilson({k: v["any"] for k, v in d.items()}, clusters),
                                          "all_gold_sessions": cluster_wilson({k: v["all"] for k, v in d.items()}, clusters)}
        print("budget", lv, flush=True)
    res["x01_budget_presence"] = pres
    json.dump(res, open(out_json, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:6])
