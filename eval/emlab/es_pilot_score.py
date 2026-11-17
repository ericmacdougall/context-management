"""ES-pilot scorer: recomputes EVERY figure from rows.jsonl.gz and ce.jsonl (no figure is read from a summary), applies the
committed thresholds (ES-PILOT-THRESHOLDS.md) and writes score.json. Pure stdlib + the repo's metric functions."""
from __future__ import annotations

import gzip
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional

from .metrics import clustered_paired_bootstrap, recall_at_k
from .retrievers import to_sessions

SEED = 16096
NBOOT = 2000
HEAD = 20
ARMS = ("bm25", "dense", "rrf", "ce")
LEVELS = ("H0", "H1", "H2", "H3")
TEST_N = (350, 299)
PILOT_N = 94


def sess10(rounds: List[str], gold: List[str]) -> float:
    return recall_at_k(to_sessions(rounds)[:10], gold, 10)


def overlap_rounds(a: List[str], b: List[str]) -> float:
    return len(set(a[:10]) & set(b[:10])) / 10.0


def overlap_sessions(a: List[str], b: List[str]) -> float:
    return len(set(to_sessions(a)[:10]) & set(to_sessions(b)[:10])) / 10.0


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def classify(mean_diff: float, lo: float, hi: float, band_mean: float = 0.03, band_int: float = 0.05) -> str:
    if abs(mean_diff) > band_mean:
        return "MEAN-FAIL"
    if lo < -band_int or hi > band_int:
        return "WIDTH-FAIL"
    return "HOLDS"


def project(mean_diff: float, lo: float, hi: float, n: int, band_int: float = 0.05) -> Dict[str, Any]:
    h = (hi - lo) / 2.0 * math.sqrt(PILOT_N / n)
    return {"n": n, "half_width": h, "lo": mean_diff - h, "hi": mean_diff + h, "inside": bool(mean_diff - h >= -band_int and mean_diff + h <= band_int)}


def load(run_dir: str):
    rows = [json.loads(l) for l in gzip.open(os.path.join(run_dir, "rows.jsonl.gz"), "rt")]
    ce = {}
    p = os.path.join(run_dir, "ce.jsonl")
    if os.path.exists(p):
        with open(p) as fh:
            for l in fh:
                j = json.loads(l)
                ce[(j["qid"], j["level"])] = j
    return rows, ce


def lists_for(row: Dict[str, Any], arm: str, ce: Dict) -> Optional[Dict[str, List[str]]]:
    if arm != "ce":
        return {"exact": row["exact"][arm], "es": row["es"][arm]}
    rec = ce.get((row["qid"], row["level"]))
    if rec is None:
        return None
    return {"exact": rec["ce_exact"] + row["exact"]["rrf"][HEAD:], "es": rec["ce_es"] + row["es"]["rrf"][HEAD:]}


def score(run_dir: str, prior_es: Optional[str] = None, prior_ce: Optional[str] = None) -> Dict[str, Any]:
    rows, ce = load(run_dir)
    out: Dict[str, Any] = {"run_dir": run_dir, "n_rows": len(rows), "levels": {}}
    try:
        out["meta"] = json.load(open(os.path.join(run_dir, "retrieve_meta.json")))
    except FileNotFoundError:
        out["meta"] = None
    for lv in LEVELS:
        rl = [r for r in rows if r["level"] == lv]
        L: Dict[str, Any] = {"n": len(rl), "arms": {}}
        for arm in ARMS:
            pairs, ov_r, ov_s, short_es, short_ex = [], [], [], 0, 0
            for r in rl:
                ls = lists_for(r, arm, ce)
                if ls is None:
                    continue
                pairs.append((r["cluster"], sess10(ls["exact"], r["gold"]), sess10(ls["es"], r["gold"])))
                ov_r.append(overlap_rounds(ls["exact"], ls["es"]))
                ov_s.append(overlap_sessions(ls["exact"], ls["es"]))
                short_es += len(r["es"]["bm25" if arm == "bm25" else "dense"]) < 200 if arm in ("bm25", "dense") else len(r["es"]["rrf"]) < 200
                short_ex += len(r["exact"]["bm25" if arm == "bm25" else "dense"]) < 200 if arm in ("bm25", "dense") else len(r["exact"]["rrf"]) < 200
            if not pairs:
                continue
            b = clustered_paired_bootstrap(pairs, n_boot=NBOOT, seed=SEED)
            cls = classify(b["mean_diff"], b["ci_low"], b["ci_high"])
            tight = classify(b["mean_diff"], b["ci_low"], b["ci_high"], 0.02, 0.04)
            L["arms"][arm] = {"n": len(pairs), "clusters": int(b["effective_n"]), "exact": mean(p[1] for p in pairs), "es": mean(p[2] for p in pairs),
                              "mean_diff": b["mean_diff"], "ci_low": b["ci_low"], "ci_high": b["ci_high"], "class": cls, "class_tight_0.02_0.04": tight,
                              "projection": [project(b["mean_diff"], b["ci_low"], b["ci_high"], n) for n in TEST_N],
                              "projection_tight": [project(b["mean_diff"], b["ci_low"], b["ci_high"], n, 0.04) for n in TEST_N],
                              "round_top10_overlap": mean(ov_r), "session_top10_overlap": mean(ov_s),
                              "short_lists_es": short_es, "short_lists_exact": short_ex}
        if all(("ce_exact" in c) for c in ce.values()) and ce:
            same = [ce[(r["qid"], lv)] for r in rl if (r["qid"], lv) in ce]
            L["ce_head_identical_set"] = sum(1 for c in same if c["head_same_set"])
            L["ce_head_identical_order"] = sum(1 for c in same if c["head_same_order"])
            L["ce_heads"] = len(same)
        # controls
        ctl: Dict[str, Any] = {}
        for name in ("ctl_whitespace", "ctl_keyword"):
            pairs = [(r["cluster"], sess10(r["exact"]["bm25"], r["gold"]), sess10(r[name], r["gold"])) for r in rl]
            b = clustered_paired_bootstrap(pairs, n_boot=NBOOT, seed=SEED)
            ctl[name] = {"es_recall": mean(p[2] for p in pairs), "mean_diff_to_exact_bm25": b["mean_diff"], "ci_low": b["ci_low"], "ci_high": b["ci_high"]}
        wq: Dict[str, Any] = {}
        for arm in ("bm25", "dense", "rrf"):
            real_es = mean(sess10(r["es"][arm], r["gold"]) for r in rl)
            w_es = mean(sess10(r["wrong_es"][arm], r["gold"]) for r in rl)
            w_ex = mean(sess10(r["wrong_exact"][arm], r["gold"]) for r in rl)
            wq[arm] = {"es_real": real_es, "es_wrong": w_es, "exact_wrong": w_ex, "pass_half": bool(real_es > 0 and w_es <= 0.5 * real_es)}
        L["controls"] = ctl
        L["wrong_query"] = wq
        out["levels"][lv] = L
    # thresholds T4.2 and T4.3
    out["control_checks"] = {
        "whitespace_H0_le_-0.10": bool(out["levels"]["H0"]["controls"]["ctl_whitespace"]["mean_diff_to_exact_bm25"] <= -0.10),
        "keyword_le_0.05_all_levels": all(out["levels"][lv]["controls"]["ctl_keyword"]["es_recall"] <= 0.05 for lv in LEVELS),
        "wrong_query_half_all": all(out["levels"][lv]["wrong_query"][a]["pass_half"] for lv in LEVELS for a in ("bm25", "dense", "rrf")),
    }
    # T3 recommendation (mechanical)
    fails = [(lv, a, v["class"]) for lv in LEVELS for a, v in out["levels"][lv]["arms"].items() if v["class"] != "HOLDS"]
    mean_fail = [f for f in fails if f[2] == "MEAN-FAIL"]
    width = [f for f in fails if f[2] == "WIDTH-FAIL"]
    width_out = [f for f in width if not all(p["inside"] for p in out["levels"][f[0]]["arms"][f[1]]["projection"])]
    complete = all(a in out["levels"][lv]["arms"] for lv in LEVELS for a in ARMS)
    if mean_fail:
        rec = "MEAN-FAIL"
    elif width_out:
        rec = "KEEP-MEAN-CLAUSE-AND-STATE-INTERVAL-LIMIT"
    else:
        rec = "KEEP"
    tight_ok = False
    if rec == "KEEP":
        tight_ok = all(v["class_tight_0.02_0.04"] == "HOLDS" and all(p["inside"] for p in v["projection_tight"])
                       for lv in LEVELS for v in out["levels"][lv]["arms"].values())
    out["recommendation"] = {"category": rec, "complete_all_arms": complete, "mean_fails": mean_fail, "width_fails": width,
                             "width_fails_still_outside_at_test_n": width_out, "tighten_0.02_0.04_supported": tight_ok}
    # T4.7 reproduction
    if prior_es and os.path.exists(prior_es):
        pr = json.load(open(prior_es))["levels"]
        rep = {}
        for lv in LEVELS:
            for arm, ek, xk in (("bm25", "es_bm25", "exact_bm25"), ("dense", "es_dense", "exact_dense"), ("rrf", "es_rrf", "exact_rrf")):
                a = out["levels"][lv]["arms"][arm]
                rep["%s/%s" % (lv, arm)] = {"exact_diff": a["exact"] - pr[lv]["mean_session_recall10"][xk], "es_diff": a["es"] - pr[lv]["mean_session_recall10"][ek]}
        out["reproduction_prior_es"] = {"max_abs": max(max(abs(v["exact_diff"]), abs(v["es_diff"])) for v in rep.values()), "cells": rep,
                                        "within_0.002": all(abs(v["exact_diff"]) <= 0.002 and abs(v["es_diff"]) <= 0.002 for v in rep.values())}
    if prior_ce and os.path.exists(prior_ce) and complete:
        pc = json.load(open(prior_ce))["levels"]
        rep = {lv: out["levels"][lv]["arms"]["ce"]["exact"] - pc[lv]["rerank"]["session_recall@10"]["mean"] for lv in LEVELS}
        out["reproduction_prior_ce"] = {"cells": rep, "within_0.005": all(abs(v) <= 0.005 for v in rep.values())}
    return out


if __name__ == "__main__":
    d = sys.argv[1]
    res = score(d, sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
    json.dump(res, open(os.path.join(d, "score.json"), "w"), indent=1)
    print(json.dumps({"recommendation": res["recommendation"], "control_checks": res["control_checks"]}, indent=1))
