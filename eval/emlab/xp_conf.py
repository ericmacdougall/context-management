"""E3 retrieval-confidence calibration (exploratory, pilot only, no model; see EXPLORATORY.md). New file."""
from __future__ import annotations

import hashlib
import json
import sys
from typing import Dict, List

import numpy as np

from .dataset import LongMemEvalLoader, select_split
from .fusion import rrf
from .live import ExactRetriever
from .pool import PoolContext, with_sessions
from .retrievers import to_sessions
from .xp_common import cluster_wilson

SEED = 16096
FULL = 200


def auroc(y: np.ndarray, p: np.ndarray) -> float:
    pos, neg = y == 1, y == 0
    n1, n0 = int(pos.sum()), int(neg.sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(p, kind="stable")
    sp = p[order]
    ranks = np.empty(len(p))
    i = 0
    while i < len(sp):
        j = i
        while j + 1 < len(sp) and sp[j + 1] == sp[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 30) -> np.ndarray:
    n, d = X.shape
    A = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(d + 1)
    reg = np.eye(d + 1) * l2
    reg[0, 0] = 0.0
    for _ in range(iters):
        z = A @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        g = A.T @ (p - y) + reg @ w
        H = A.T @ (A * (p * (1 - p))[:, None]) + reg + 1e-8 * np.eye(d + 1)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(np.hstack([np.ones((len(X), 1)), X]) @ w, -30, 30)))


def cv_oof(X: np.ndarray, y: np.ndarray, folds: np.ndarray) -> np.ndarray:
    oof = np.zeros(len(y))
    for f in np.unique(folds):
        tr, te = folds != f, folds == f
        mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
        w = fit_logistic((X[tr] - mu) / sd, y[tr])
        oof[te] = predict(w, (X[te] - mu) / sd)
    return oof


def cluster_folds(qids: List[str], clusters: Dict[str, str], k: int = 5) -> np.ndarray:
    cl = sorted({clusters[q] for q in qids}, key=lambda c: hashlib.sha256(("16096:cv:%s" % c).encode()).hexdigest())
    pos = {c: i % k for i, c in enumerate(cl)}
    return np.asarray([pos[clusters[q]] for q in qids])


def features(ex, q, ids):
    pq = with_sessions(q, ids)
    bm = ex.bm25(pq, FULL, None)
    de = ex.dense(pq, FULL, None)
    bl, dl = [r for r, _ in bm], [r for r, _ in de]
    fused = [r for r, _ in rrf([bl, dl], k=60)]
    top10 = to_sessions(fused)[:10]
    bs, ds = to_sessions(bl)[:10], to_sessions(dl)[:10]
    s = [x for _, x in de]
    b = [x for _, x in bm]
    feats = [s[0], s[0] - s[min(9, len(s) - 1)], (b[min(9, len(b) - 1)] / b[0]) if b and b[0] > 0 else 0.0, len(set(bs) & set(ds))]
    return feats, top10


def main(dataset, art, sets_path, out_json):
    all_qs = list(LongMemEvalLoader(dataset).questions())
    pilot = select_split(all_qs, "pilot")
    ctx = PoolContext(all_qs)
    sets = json.load(open(sets_path))
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(pilot)
    clusters = ctx.clusters
    rng = np.random.default_rng(SEED)
    out: Dict[str, object] = {"retriever": "exact in-process; embedder intfloat/multilingual-e5-small rev 614241f; pilot only", "levels": {}}
    for lv in ("H0", "H1", "H2", "H3"):
        rows, abst = [], []
        for q in pilot:
            ids = list(q.haystack_session_ids) if lv == "H0" else ctx.level_sessions(q, lv, sets)
            f, top10 = features(ex, q, ids)
            if q.is_abstention:
                abst.append(f)
                continue
            if not q.retrieval_gold_session_ids:
                continue
            rows.append((q.qid, f, 1.0 if set(top10) & set(q.gold_session_ids) else 0.0))
        qids = [r[0] for r in rows]
        X = np.asarray([r[1] for r in rows], dtype=np.float64)
        y = np.asarray([r[2] for r in rows])
        folds = cluster_folds(qids, clusters)
        oof = cv_oof(X, y, folds)
        real = auroc(y, oof)
        null = np.empty(1000)
        for b in range(1000):
            yp = rng.permutation(y)
            null[b] = auroc(yp, cv_oof(X, yp, folds))
        null = null[~np.isnan(null)]
        p975 = float(np.percentile(null, 97.5))
        cut = np.percentile(oof, 70)
        conf = oof >= cut
        prec = cluster_wilson({qids[i]: y[i] for i in range(len(y)) if conf[i]}, clusters) if conf.any() else None
        Xa = np.asarray(abst) if abst else None
        out["levels"][lv] = {
            "n": len(y), "n_clusters": len({clusters[q] for q in qids}), "base_rate": float(y.mean()), "n_negatives": int((y == 0).sum()),
            "auroc_cv": real, "null_mean": float(null.mean()), "null_p97_5": p975, "null_sd": float(null.std()),
            "pass": bool(real > p975), "confident_30pct": {"precision": prec, "predicted_prob_cut": float(cut),
                                                            "dense_top1_cosine_mean_in_confident": float(X[conf, 0].mean()) if conf.any() else None},
            "answerable_feature_means": X.mean(0).tolist(), "abstention_feature_means": Xa.mean(0).tolist() if Xa is not None else None,
            "n_abstention": len(abst), "features": ["dense_top1_cos", "dense_margin_top1_top10", "bm25_ratio_top10_over_top1", "agreement_top10"]}
        print(lv, json.dumps({k: out["levels"][lv][k] for k in ("n", "base_rate", "n_negatives", "auroc_cv", "null_p97_5", "pass")}), flush=True)
    json.dump(out, open(out_json, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:5])
