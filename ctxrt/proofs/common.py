"""Shared proof driver: run each seed, collect per-case rows + per-seed metrics, evaluate criteria, write hashed JSONL and a summary."""
from __future__ import annotations

import sys
import time

from ctxrt import harness


def check(name, value, ok, **kw):
    d = {"criterion": name, "value": value, "ok": bool(ok)}
    d.update(kw)
    return d


def main(name, run_seed, evaluate, params, seeds=None, claim=""):
    seeds = seeds or harness.SEEDS
    st = harness.stamp(dict(params, seeds=seeds))
    per_seed, arts = [], []
    for s in seeds:
        t0 = time.time()
        rows, metrics = run_seed(s, params)
        art = harness.write_cases(name, s, rows)
        arts.append(art)
        crit = evaluate(metrics, params)
        per_seed.append({"seed": s, "metrics": metrics, "criteria": crit, "artifact": art, "seconds": round(time.time() - t0, 1)})
        print("seed", s, "->", ["%s:%s" % (c["criterion"], "ok" if c["ok"] else "FAIL") for c in crit if not c["ok"]] or "all criteria ok", file=sys.stderr)
    verdict_items = {}
    for ps in per_seed:
        for c in ps["criteria"]:
            verdict_items.setdefault(c["criterion"], []).append(c["ok"])
    verdict = {k: ("PASS" if all(v) else "FAIL") for k, v in verdict_items.items()}
    overall = "PASS" if all(x == "PASS" for x in verdict.values()) else "FAIL"
    doc = {"proof": name, "claim": claim, "stamp": st, "per_seed": per_seed, "verdict_by_criterion": verdict, "overall": overall}
    p = harness.write_summary(name, doc)
    print(name, overall, "->", p, file=sys.stderr)
    return doc
