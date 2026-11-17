"""Render the ES-PILOT.md tables from score.json (no figure is typed by hand). Usage: python3 -m emlab.es_pilot_tables <score.json>"""
from __future__ import annotations

import json
import sys

LV = ("H0", "H1", "H2", "H3")
ARMN = {"bm25": "BM25", "dense": "dense kNN", "rrf": "RRF 60", "ce": "cross-encoder (top 20 of RRF)"}


def main(path: str) -> None:
    d = json.load(open(path))
    print("| level | arm | exact | ES | diff ES-exact [95% interval] | class | projected interval at n=350 / 299 | round top-10 overlap | session top-10 overlap | short ES lists |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for lv in LV:
        for a in ("bm25", "dense", "rrf", "ce"):
            v = d["levels"][lv]["arms"].get(a)
            if not v:
                continue
            p = v["projection"]
            print("| %s | %s | %.3f | %.3f | %+.3f [%+.3f, %+.3f] | %s | [%+.3f, %+.3f] / [%+.3f, %+.3f] | %.3f | %.3f | %d |" % (
                lv, ARMN[a], v["exact"], v["es"], v["mean_diff"], v["ci_low"], v["ci_high"], v["class"], p[0]["lo"], p[0]["hi"], p[1]["lo"], p[1]["hi"],
                v["round_top10_overlap"], v["session_top10_overlap"], v["short_lists_es"]))
    print()
    print("| level | cross-encoder heads with the same top-20 set / same order (of n) |")
    print("|---|---|")
    for lv in LV:
        L = d["levels"][lv]
        if "ce_heads" in L:
            print("| %s | %d / %d (of %d) |" % (lv, L["ce_head_identical_set"], L["ce_head_identical_order"], L["ce_heads"]))
    print()
    print("| level | whitespace-analyzer BM25: ES recall, diff to exact [interval] | keyword-analyzer BM25 ES recall | wrong-query ES recall BM25 / dense / RRF (real ES recall) | wrong-query exact recall BM25 / dense / RRF |")
    print("|---|---|---|---|---|")
    for lv in LV:
        L = d["levels"][lv]
        w, k, q = L["controls"]["ctl_whitespace"], L["controls"]["ctl_keyword"], L["wrong_query"]
        print("| %s | %.3f, %+.3f [%+.3f, %+.3f] | %.3f | %s | %s |" % (
            lv, w["es_recall"], w["mean_diff_to_exact_bm25"], w["ci_low"], w["ci_high"], k["es_recall"],
            " / ".join("%.3f (%.3f)" % (q[a]["es_wrong"], q[a]["es_real"]) for a in ("bm25", "dense", "rrf")),
            " / ".join("%.3f" % q[a]["exact_wrong"] for a in ("bm25", "dense", "rrf"))))
    print()
    print("recommendation:", json.dumps(d["recommendation"]))
    print("control_checks:", json.dumps(d["control_checks"]))
    for key in ("reproduction_prior_es", "reproduction_prior_ce"):
        if key in d:
            print(key, json.dumps({k: v for k, v in d[key].items() if k != "cells"}), json.dumps(d[key].get("cells") if key.endswith("ce") else ""))


if __name__ == "__main__":
    main(sys.argv[1])
