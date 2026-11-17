"""Score STAGE-X0B (10-call cap-30 re-extraction check; no model call, no judge). Rules: STAGE-X0B.md (committed before the calls).
10 sessions give a direction, not a rate. Failures (non-ok status, missing record) are counted and stay in every denominator."""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List

from . import provenance_x04 as prov
from .stage_x0_score import answer_match, mean, norm_text

CAP_OLD, CAP_NEW = 12, 30
CAL = (6100, 22500)


def quote_match(a: str, b: str, min_len: int = 8) -> bool:
    """Quotes match iff, after normalisation, they are equal or one contains the other (the shorter at least min_len characters)."""
    x, y = norm_text(a), norm_text(b)
    if not x or not y:
        return False
    if x == y:
        return True
    s, l = (x, y) if len(x) <= len(y) else (y, x)
    return len(s) >= min_len and s in l


def stability(old_facts: List[Dict[str, Any]], new_facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    kept = sum(1 for o in old_facts if any(quote_match(o.get("quote", ""), n.get("quote", "")) for n in new_facts))
    new_only = [n for n in new_facts if not any(quote_match(o.get("quote", ""), n.get("quote", "")) for o in old_facts)]
    return {"old": len(old_facts), "new": len(new_facts), "old_reappearing": kept, "new_without_old_match": len(new_only), "beyond_old_count": len(new_facts) - len(old_facts)}


def fact_text(f):
    return " ".join(str(f.get(k, "")) for k in ("quote", "subject", "predicate", "object"))


def main(dataset, art, sets_path, out_root, in_root, out_json):
    from .build_rag_contexts import load_common
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qmap = {q.qid: q for q in qs}
    key = json.load(open(os.path.join(out_root, "stage_x0b_key", "selection_key.json")))
    k0 = json.load(open(os.path.join(out_root, "stage_x0_key", "selection_key.json")))
    prep0 = json.load(open(os.path.join(out_root, "stage_x0", "prep_report.json")))
    x0score_p = os.path.join(out_root, "stage_x0_score.json")
    rows = {r["opaque_session_id"]: r for r in key["sessions"]}
    rows0 = {r["opaque_session_id"]: r for r in k0["sessions"]}
    sess_files = {o: json.load(open(os.path.join(out_root, "stage_x0b", "sessions", o + ".json"), encoding="utf-8")) for o in rows}
    new = {}
    for o in rows:
        p = os.path.join(in_root, "stage_x0b", o + ".json")
        new[o] = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"status": "missing"}
    old = {o: json.load(open(os.path.join(in_root, "stage_x0", o + ".json"), encoding="utf-8")) for o in rows}
    res: Dict[str, Any] = {"pilot": True, "confirmatory": False, "n_sessions": len(rows), "direction_not_rate": True}
    res["status_counts"] = {s: sum(1 for r in new.values() if r.get("status") == s) for s in {r.get("status") for r in new.values()}}
    done = [o for o in rows if new[o].get("status") in ("ok", "repaired")]
    # facts, stability, cap share
    per = {}
    for o in rows:
        nf = new[o].get("facts") or [] if o in done else []
        per[o] = dict(stability(old[o].get("facts") or [], nf), group=rows[o]["group"], why=rows[o]["why"], status=new[o].get("status"), at_new_cap=len(nf) >= CAP_NEW)
    res["per_session"] = per
    res["facts"] = {"mean_old": mean(v["old"] for v in per.values()), "mean_new": mean(v["new"] for v in per.values()), "mean_beyond_old": mean(v["beyond_old_count"] for v in per.values()),
                    "sessions_with_more_than_old": sum(1 for v in per.values() if v["new"] > v["old"]), "at_new_cap_30": sum(1 for v in per.values() if v["at_new_cap"]),
                    "old_reappearing_total": sum(v["old_reappearing"] for v in per.values()), "old_total": sum(v["old"] for v in per.values()),
                    "stability_rate": (sum(v["old_reappearing"] for v in per.values()) / float(sum(v["old"] for v in per.values()))) if done else None,
                    "new_without_old_match_total": sum(v["new_without_old_match"] for v in per.values())}
    # provenance on the new facts
    sess = {o: prov.rounds_of_turns(sess_files[o]["turns"]) for o in sess_files}
    facts = [dict(f, session_id=o) for o in done for f in (new[o].get("facts") or [])]
    r = prov.check_all(facts, sess) if facts else []
    a_pass = sum(1 for x in r if x["a"] == prov.PASS)
    lo, hi = prov.wilson(a_pass, len(r)) if r else (0.0, 0.0)
    new_only_keys = set()
    for o in done:
        for f in new[o].get("facts") or []:
            if not any(quote_match(g.get("quote", ""), f.get("quote", "")) for g in old[o].get("facts") or []):
                new_only_keys.add((o, f.get("quote", ""), f.get("round")))
    a_new_only = [x for f, x in zip(facts, r) if (f["session_id"], f.get("quote", ""), f.get("round")) in new_only_keys]
    res["provenance_new_facts"] = {"facts": len(r), "a_pass": a_pass, "a_rate": (a_pass / len(r)) if r else None, "wilson_low": lo, "wilson_high": hi,
                                   "b": {k: sum(1 for x in r if x["b"] == k) for k in ("pass", "ambiguous", "mismatch", "undefined")},
                                   "facts_without_old_match": len(a_new_only), "a_pass_among_those": sum(1 for x in a_new_only if x["a"] == prov.PASS),
                                   "quotes_over_12_words": sum(1 for f in facts if len(str(f.get("quote", "")).split()) > 12),
                                   "controls": prov.controls(facts, sess, r) if r else None}
    # the two missing answers, string and key heuristic, over all gold sessions of the question (new facts where re-extracted, old facts otherwise)
    ans: Dict[str, Any] = {}
    for q in ("fca70973", "gpt4_ab202e7f"):
        gold_sessions = [o for o, rr in rows0.items() if q in rr["questions"]]
        gold_answer = str(qmap[q].answer)

        def hit(use_new: bool):
            h = {"string": False, "key": False}
            for o in gold_sessions:
                fs = (new[o].get("facts") if (use_new and o in rows and o in done) else old.get(o, {}).get("facts") if o in old else json.load(open(os.path.join(in_root, "stage_x0", o + ".json"))).get("facts")) or []
                for f in fs:
                    m = answer_match(gold_answer, fact_text(f))
                    h = {k: h[k] or m[k] for k in m}
            return h
        ans[q] = {"gold_sessions": len(gold_sessions), "re_extracted": sum(1 for o in gold_sessions if o in rows), "cap12_facts": hit(False), "with_cap30_facts": hit(True)}
    res["missing_answers"] = ans
    # per call, same sessions
    def col(d, k):
        return [d[o].get(k) for o in done]
    for_d = {}
    for k in ("seconds", "output_tokens", "cost_usd", "cache_creation_input_tokens", "cache_read_input_tokens", "input_tokens"):
        a, b = col(old, k), col(new, k)
        pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
        for_d[k] = {"cap12_mean": mean(x for x, _ in pairs), "cap30_mean": mean(y for _, y in pairs), "mean_diff": mean(y - x for x, y in pairs), "ratio": (sum(y for _, y in pairs) / sum(x for x, _ in pairs)) if pairs and sum(x for x, _ in pairs) else None}
    res["per_call_same_sessions"] = for_d
    # implied change to the extrapolation: only capped sessions change; pool capped share from the X0 random sample by stratum (equal-share quintiles)
    edges = prep0["strata_edges_chars"]
    binof = lambda c: sum(1 for e in edges if c > e)
    shares = {}
    for o, rr in rows0.items():
        if rr["group"] == "random_non_gold":
            n = len(json.load(open(os.path.join(in_root, "stage_x0", o + ".json"))).get("facts") or [])
            b = binof(rr["chars"])
            s = shares.setdefault(b, [0, 0])
            s[0] += 1 if n >= CAP_OLD else 0
            s[1] += 1
    pool_share = sum(v[0] / float(v[1]) for v in shares.values()) / len(shares) if shares else None
    imp: Dict[str, Any] = {"pool_capped_share_estimate": pool_share, "capped_share_by_stratum": {str(b): v[0] / float(v[1]) for b, v in sorted(shares.items())},
                           "basis": "mean paired per-call difference over these 10 capped sessions (DIRECTION ONLY: 10 sessions, 7 of them gold, are not a random draw of capped pool sessions) times the capped share of the pool; sessions below the cap are assumed unchanged"}
    x0s = json.load(open(x0score_p)) if os.path.exists(x0score_p) else None
    for n in CAL:
        e = {}
        for name in ("output_tokens", "seconds", "cost_usd"):
            d = for_d[name]["mean_diff"]
            extra = n * pool_share * d if (d is not None and pool_share is not None) else None
            base = x0s["extrapolation"]["calls"][str(n)][name]["total"] if x0s else None
            e[name] = {"extra_total": extra, "cap12_total": base, "cap30_total": (base + extra) if (base is not None and extra is not None) else None}
        imp["calls_%d" % n] = e
    res["implied_extrapolation_change"] = imp
    json.dump(res, open(out_json, "w"), indent=1)
    print("done")


if __name__ == "__main__":
    main(*sys.argv[1:7])
