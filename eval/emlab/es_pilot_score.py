"""ES-pilot scorer: recomputes EVERY figure from rows.jsonl.gz and ce.jsonl (no figure is read from a summary), applies the
committed thresholds (ES-PILOT-THRESHOLDS.md) and writes score.json. Pure stdlib + the repo's metric functions."""
from __future__ import annotations

import gzip
import hashlib
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


WITHHELD = "WITHHELD-INCOMPLETE-EVIDENCE"
ROW_KEYS = ("qid", "cluster", "level", "n_level_sessions", "gold", "exact", "es", "ctl_whitespace", "ctl_keyword", "wrong_exact", "wrong_es")
CE_KEYS = ("qid", "level", "ce_exact", "ce_es", "head_same_set", "head_same_order")


def load(run_dir: str):
    with gzip.open(os.path.join(run_dir, "rows.jsonl.gz"), "rt") as fh:
        rows = [json.loads(l) for l in fh]
    ce_list: List[Dict[str, Any]] = []
    p = os.path.join(run_dir, "ce.jsonl")
    if os.path.exists(p):
        with open(p) as fh:
            ce_list = [json.loads(l) for l in fh if l.strip()]
    ce = {}
    for j in ce_list:
        ce[(j.get("qid"), j.get("level"))] = j
    return rows, ce, ce_list


def _strs(x) -> bool:
    return isinstance(x, list) and all(isinstance(i, str) for i in x)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def load_manifest(run_dir: str, manifest_path: Optional[str] = None):
    """The committed pilot manifest (expected question ids, clusters, levels, artifact hashes). Returns (manifest or None, sha256 or None, problems)."""
    path = manifest_path or os.path.join(run_dir, "pilot_manifest.json")
    if not os.path.exists(path):
        return None, None, ["no pilot manifest at %s" % os.path.basename(path)]
    raw = open(path, "rb").read()
    try:
        m = json.loads(raw)
        ok = (isinstance(m.get("qids"), list) and isinstance(m.get("clusters"), dict) and isinstance(m.get("levels"), list)
              and isinstance(m.get("artifacts"), dict) and set(m["qids"]) == set(m["clusters"]) and len(set(m["qids"])) == len(m["qids"]))
    except (ValueError, AttributeError):
        ok = False
    if not ok:
        return None, hashlib.sha256(raw).hexdigest(), ["pilot manifest unreadable or malformed"]
    return m, hashlib.sha256(raw).hexdigest(), []


def file_sha256(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def validate(rows: List[Dict[str, Any]], ce_list: List[Dict[str, Any]], manifest: Optional[Dict[str, Any]] = None, artifact_hashes: Optional[Dict[str, Optional[str]]] = None):
    """Evidence completeness and authenticity. Returns (row_problems, ce_problems). No recommendation may be given when either is non-empty:
    the evidence must match the committed pilot manifest EXACTLY (question ids, clusters, levels, artifact hashes), every (question, level) must
    appear exactly once in every arm's evidence, consumed fields must have the right types (finite numbers, strings), and every cross-encoder head
    must be a reordering of the RRF head it was made from, ordered by its recorded scores."""
    rp: List[str] = []
    cp: List[str] = []
    if manifest is None:
        return ["no valid pilot manifest"], cp
    for name, want in sorted(manifest["artifacts"].items()):
        got = (artifact_hashes or {}).get(name)
        if got != want:
            rp.append("artifact %s hash %s does not match the stamped %s" % (name, (got or "absent")[:12], str(want)[:12]))
    seen: Dict[Any, int] = {}
    for r in rows:
        miss = [k for k in ROW_KEYS if k not in r]
        if miss:
            rp.append("row %s/%s missing keys %s" % (r.get("qid"), r.get("level"), miss))
            continue
        if not (isinstance(r["qid"], str) and isinstance(r["level"], str) and isinstance(r["cluster"], str) and _strs(r["gold"])):
            rp.append("row %s/%s has non-string qid, level, cluster or gold" % (r["qid"], r["level"]))
            continue
        for side in ("exact", "es", "wrong_exact", "wrong_es"):
            if not isinstance(r[side], dict):
                rp.append("row %s/%s %s is not a mapping" % (r["qid"], r["level"], side))
                continue
            for a in ("bm25", "dense", "rrf"):
                if a not in r[side]:
                    rp.append("row %s/%s lacks %s.%s" % (r["qid"], r["level"], side, a))
                elif not _strs(r[side][a]):
                    rp.append("row %s/%s %s.%s is not a list of round ids" % (r["qid"], r["level"], side, a))
        for k in ("ctl_whitespace", "ctl_keyword"):
            if not _strs(r[k]):
                rp.append("row %s/%s %s is not a list of round ids" % (r["qid"], r["level"], k))
        seen[(r["qid"], r["level"])] = seen.get((r["qid"], r["level"]), 0) + 1
    for k, n in sorted(seen.items(), key=repr):
        if n > 1:
            rp.append("duplicate row %s x%d" % (k, n))
    levels = [lv for lv in manifest["levels"]]
    present = sorted({k[1] for k in seen})
    if present != sorted(levels):
        rp.append("levels present %s, manifest has %s" % (present, sorted(levels)))
    want_q = set(manifest["qids"])
    for lv in levels:
        got_q = {k[0] for k in seen if k[1] == lv}
        if got_q != want_q:
            rp.append("question set at %s differs from the manifest (%d missing, %d unexpected)" % (lv, len(want_q - got_q), len(got_q - want_q)))
    nls: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        nls.setdefault(r.get("qid"), {})[r.get("level")] = r.get("n_level_sessions")
    for q, d in sorted(nls.items(), key=repr):  # the haystack grows with the level, so consistently swapped level labels cannot pass
        seq = [d.get(lv) for lv in levels]
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in seq) or not all(x < y for x, y in zip(seq, seq[1:])):
            rp.append("n_level_sessions of %s does not increase strictly with the level (%s)" % (q, seq))
    for r in rows:
        if isinstance(r.get("qid"), str) and r.get("qid") in manifest["clusters"] and r.get("cluster") != manifest["clusters"][r["qid"]]:
            rp.append("cluster of %s at %s is %r, manifest says %r" % (r["qid"], r.get("level"), r.get("cluster"), manifest["clusters"][r["qid"]]))
    if rp:
        return rp[:200], cp
    rowmap = {(r["qid"], r["level"]): r for r in rows}
    if not ce_list:
        cp.append("no cross-encoder records")
    cseen: Dict[Any, int] = {}
    for c in ce_list:
        miss = [k for k in CE_KEYS if k not in c]
        if miss:
            cp.append("ce record %s/%s missing keys %s" % (c.get("qid"), c.get("level"), miss))
            continue
        k = (c["qid"], c["level"])
        cseen[k] = cseen.get(k, 0) + 1
        if k not in rowmap:
            cp.append("ce record %s has no row" % (k,))
            continue
        r = rowmap[k]
        if not (_strs(c["ce_exact"]) and _strs(c["ce_es"]) and isinstance(c["scores"], dict)):
            cp.append("ce record %s has malformed heads or scores" % (k,))
            continue
        if not all(_num(v) for v in c["scores"].values()):
            cp.append("ce record %s has a non-finite or non-numeric score" % (k,))
            continue
        heads = {}
        for key, side in (("ce_exact", "exact"), ("ce_es", "es")):
            src = r[side]["rrf"][:HEAD]
            heads[key] = src
            if sorted(c[key]) != sorted(src):
                cp.append("%s %s is not a reordering of the %s RRF head" % (k, key, side))
        if set(c["scores"]) != set(heads["ce_exact"]) | set(heads["ce_es"]):
            cp.append("%s scores do not cover exactly the two heads" % (k,))
        else:
            for key in ("ce_exact", "ce_es"):
                src = heads[key]
                want = [src[j] for j in sorted(range(len(src)), key=lambda j: (-c["scores"][src[j]], j))]
                if c[key] != want:
                    cp.append("%s %s order disagrees with its recorded scores" % (k, key))
        if bool(c["head_same_set"]) != (set(r["exact"]["rrf"][:HEAD]) == set(r["es"]["rrf"][:HEAD])):
            cp.append("%s head_same_set disagrees with the rows" % (k,))
        if bool(c["head_same_order"]) != (r["exact"]["rrf"][:HEAD] == r["es"]["rrf"][:HEAD]):
            cp.append("%s head_same_order disagrees with the rows" % (k,))
    for k, n in sorted(cseen.items(), key=repr):
        if n > 1:
            cp.append("duplicate ce record %s x%d" % (k, n))
    for k in sorted(rowmap, key=repr):
        if k not in cseen:
            cp.append("missing ce record %s" % (k,))
    return rp, cp[:200]


def lists_for(row: Dict[str, Any], arm: str, ce: Dict) -> Optional[Dict[str, List[str]]]:
    if arm != "ce":
        return {"exact": row["exact"][arm], "es": row["es"][arm]}
    rec = ce.get((row["qid"], row["level"]))
    if rec is None:
        return None
    return {"exact": rec["ce_exact"] + row["exact"]["rrf"][HEAD:], "es": rec["ce_es"] + row["es"]["rrf"][HEAD:]}


def score(run_dir: str, prior_es: Optional[str] = None, prior_ce: Optional[str] = None, manifest_path: Optional[str] = None) -> Dict[str, Any]:
    """Any exception or evidence problem yields WITHHELD-INCOMPLETE-EVIDENCE, never a traceback and never a positive category."""
    try:
        return _score(run_dir, prior_es, prior_ce, manifest_path)
    except Exception as e:  # malformed evidence that slipped past validate()
        return {"run_dir": run_dir, "levels": {}, "evidence": {"complete": False, "problems": ["exception while scoring: %s: %s" % (type(e).__name__, str(e)[:200])], "n_problems": 1},
                "recommendation": {"category": WITHHELD, "complete_all_arms": False, "evidence_complete": False, "reason": "exception"}, "control_checks": None}


def _score(run_dir: str, prior_es: Optional[str], prior_ce: Optional[str], manifest_path: Optional[str]) -> Dict[str, Any]:
    manifest, manifest_sha, mproblems = load_manifest(run_dir, manifest_path)
    artifact_hashes = {n: file_sha256(os.path.join(run_dir, n)) for n in ("rows.jsonl.gz", "ce.jsonl")}
    try:
        rows, ce, ce_list = load(run_dir)
    except (ValueError, OSError) as e:
        return {"run_dir": run_dir, "levels": {}, "evidence": {"complete": False, "problems": mproblems + ["evidence unreadable: %s" % type(e).__name__], "n_problems": 1},
                "recommendation": {"category": WITHHELD, "complete_all_arms": False, "evidence_complete": False, "reason": "unreadable evidence"}, "control_checks": None}
    row_problems, ce_problems = validate(rows, ce_list, manifest, artifact_hashes)
    row_problems = mproblems + row_problems
    problems = row_problems + ce_problems
    if problems:  # no number is computed from evidence that failed validation
        return {"run_dir": run_dir, "n_rows": len(rows), "levels": {}, "manifest_sha256": manifest_sha, "artifact_sha256": artifact_hashes,
                "evidence": {"complete": False, "problems": problems[:50], "n_problems": len(problems)},
                "recommendation": {"category": WITHHELD, "complete_all_arms": False, "evidence_complete": False, "reason": "evidence failed validation"}, "control_checks": None}
    out: Dict[str, Any] = {"run_dir": run_dir, "n_rows": len(rows), "levels": {}, "manifest_sha256": manifest_sha, "manifest_source": manifest.get("source"),
                           "artifact_sha256": artifact_hashes, "evidence": {"complete": True, "problems": [], "n_problems": 0}}
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
    if problems:
        rec = WITHHELD
    elif mean_fail:
        rec = "MEAN-FAIL"
    elif width_out:
        rec = "KEEP-MEAN-CLAUSE-AND-STATE-INTERVAL-LIMIT"
    else:
        rec = "KEEP"
    tight_ok = False
    if rec == "KEEP":  # only reachable with complete evidence
        tight_ok = all(v["class_tight_0.02_0.04"] == "HOLDS" and all(p["inside"] for p in v["projection_tight"])
                       for lv in LEVELS for v in out["levels"][lv]["arms"].values())
    out["recommendation"] = {"category": rec, "complete_all_arms": complete and not problems, "evidence_complete": not problems, "mean_fails": mean_fail, "width_fails": width,
                             "width_fails_still_outside_at_test_n": width_out, "tighten_0.02_0.04_supported": tight_ok}
    # T4.7 reproduction: an explicit state in every case (CHECKED, NOT_CHECKED when no path was supplied, PATH_ABSENT when a path was supplied but is missing)
    if not prior_es:
        out["reproduction_prior_es"] = {"status": "NOT_CHECKED"}
    elif not os.path.exists(prior_es):
        out["reproduction_prior_es"] = {"status": "PATH_ABSENT", "path": prior_es}
        problems.append("prior ES results path absent: %s" % prior_es)
    else:
        with open(prior_es) as fh:
            pr = json.load(fh)["levels"]
        rep = {}
        for lv in LEVELS:
            for arm, ek, xk in (("bm25", "es_bm25", "exact_bm25"), ("dense", "es_dense", "exact_dense"), ("rrf", "es_rrf", "exact_rrf")):
                a = out["levels"][lv]["arms"][arm]
                rep["%s/%s" % (lv, arm)] = {"exact_diff": a["exact"] - pr[lv]["mean_session_recall10"][xk], "es_diff": a["es"] - pr[lv]["mean_session_recall10"][ek]}
        out["reproduction_prior_es"] = {"status": "CHECKED", "max_abs": max(max(abs(v["exact_diff"]), abs(v["es_diff"])) for v in rep.values()), "cells": rep,
                                        "within_0.002": all(abs(v["exact_diff"]) <= 0.002 and abs(v["es_diff"]) <= 0.002 for v in rep.values())}
    if not prior_ce:
        out["reproduction_prior_ce"] = {"status": "NOT_CHECKED"}
    elif not os.path.exists(prior_ce):
        out["reproduction_prior_ce"] = {"status": "PATH_ABSENT", "path": prior_ce}
        problems.append("prior cross-encoder results path absent: %s" % prior_ce)
    elif not complete:
        out["reproduction_prior_ce"] = {"status": "NOT_CHECKED_INCOMPLETE_ARMS"}
    else:
        with open(prior_ce) as fh:
            pc = json.load(fh)["levels"]
        rep = {lv: out["levels"][lv]["arms"]["ce"]["exact"] - pc[lv]["rerank"]["session_recall@10"]["mean"] for lv in LEVELS}
        out["reproduction_prior_ce"] = {"status": "CHECKED", "cells": rep, "within_0.005": all(abs(v) <= 0.005 for v in rep.values())}
    if problems and out["recommendation"]["category"] != WITHHELD:  # a supplied prior path that is missing also withholds
        out["recommendation"] = {"category": WITHHELD, "complete_all_arms": False, "evidence_complete": False, "reason": "evidence problems",
                                 "mean_fails": mean_fail, "width_fails": width}
    out["evidence"] = {"complete": not problems, "problems": problems[:50], "n_problems": len(problems)}
    return out


if __name__ == "__main__":
    d = sys.argv[1]
    res = score(d, sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
    json.dump(res, open(os.path.join(d, "score.json"), "w"), indent=1)
    print(json.dumps({"recommendation": res["recommendation"], "control_checks": res["control_checks"]}, indent=1))
