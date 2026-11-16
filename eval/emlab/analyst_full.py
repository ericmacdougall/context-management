"""Score the full analyst run (pilot, levels H1, H2, H3 and the H3_top20 secondary variant) per PREREGISTRATION 5.2. New; no LLM.
Rejected result files score as FAILURES (empty ranking: zero recall, a miss on knowledge-update) and stay in every
denominator; ok-only numbers are reported beside them. Rows of the call log with a null tool (the runner's probe) are
excluded. Paired differences use the content-cluster clustered bootstrap (2,000 resamples, seed 16096)."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from .anon import AnonMap
from .build_rag_contexts import level_dates, load_common
from .context import context_metrics, pack_rag
from .metrics import clustered_paired_bootstrap, recall_at_k
from .retrievers import to_sessions

ARMS = ("RRF", "RERANK", "ANALYST", "ANALYST-RERANK-ONLY")
RUNS = (("H1", "H1", "analyst_tasks"), ("H2", "H2", "analyst_tasks"), ("H3", "H3", "analyst_tasks"), ("H3_top20", "H3", "analyst_tasks_top20"))
BUDGETS = (4000, 8000)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def main(dataset, art, sets_path, out_root, in_root, rerank_partial, out_json):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    qmap = {q.qid: q for q in qs}
    low = set(json.load(open(os.path.join(out_root, "lowoverlap_pool_h3.json")))["thresholds"]["100"]["qids"])
    anon = AnonMap(list(ctx.hashes))
    rows_log = [json.loads(l) for l in open(os.path.join(out_root, "analyst_calls.jsonl"), encoding="utf-8")]
    log = [r for r in rows_log if r.get("tool")]
    returned: Dict[tuple, set] = {}
    for r in log:
        returned.setdefault((r["qid"], r["level"]), set()).update(r.get("returned_ids", []))
    rr: Dict[str, Dict[str, List[str]]] = {}
    for r in json.load(open(rerank_partial))["per_question"]:
        rr[r["qid"]] = {lv: r["levels"][lv]["rankings"]["rerank"] for lv in ("H1", "H2", "H3")}
    rerank_n = len(rr)
    out: Dict[str, Any] = {"retriever": "exact in-process; analysts called through the tool daemon", "rerank_questions_available": rerank_n,
                           "probe_rows_excluded": len(rows_log) - len(log), "runs": {}, "cross_check": {}}
    tot_runner_calls = 0
    for dirname, level, tasks_dir in RUNS:
        per: Dict[str, Dict[str, Any]] = {}
        for q in qs:
            tp = os.path.join(out_root, tasks_dir, level, q.qid + ".json")
            if not os.path.exists(tp):
                continue
            rp = {a: os.path.join(in_root, "analyst_results", a, dirname, q.qid + ".json") for a in ("ANALYST", "ANALYST-RERANK-ONLY")}
            if dirname == "H3_top20" and not os.path.exists(rp["ANALYST"]):
                continue
            task = json.load(open(tp))
            cand_open = [c["round_id"] for c in task["candidates"]]
            cand = [anon.real_rid(i) for i in cand_open]
            lvl = ctx.level_sessions(q, level, sets)
            dates, own = level_dates(q, lvl)
            lists: Dict[str, Any] = {"RRF": cand}
            if q.qid in rr and dirname != "H3_top20":
                lists["RERANK"] = rr[q.qid][level]
            info: Dict[str, Any] = {}
            for arm in ("ANALYST", "ANALYST-RERANK-ONLY"):
                if not os.path.exists(rp[arm]):
                    continue
                res = json.load(open(rp[arm]))
                ok_ids = set(cand_open)
                if arm == "ANALYST":
                    ok_ids |= returned.get((q.qid, level), set())
                ids = res.get("ranked_round_ids") or []
                good = [anon.real_rid(i) for i in ids if i in ok_ids] if res.get("status") == "ok" else []
                lists[arm] = good
                tk = res.get("tokens") or {}
                info[arm] = {"status": res.get("status"), "returned": len(ids), "rejected_ids": (len(ids) - len([i for i in ids if i in ok_ids])) if res.get("status") == "ok" else None,
                             "tool_calls": res.get("tool_calls"), "seconds": res.get("seconds"), "out_tokens": tk.get("out"),
                             "cache_creation": tk.get("cache_creation")}
                if arm == "ANALYST":
                    tot_runner_calls += int(res.get("tool_calls") or 0)
            rec: Dict[str, Any] = {"low": q.qid in low, "qtype": q.qtype, "info": info, "arms": {}}
            for arm, ranked in lists.items():
                ranked = [r for r in ranked if r.rsplit(":", 1)[0] in dates and r in texts]
                cut = ranked if arm in ("RRF", "RERANK") else ranked[:20]
                m: Dict[str, Any] = {"session_recall_at10": recall_at_k(to_sessions(ranked)[:10], q.gold_session_ids, 10)}
                for b in BUDGETS:
                    c = context_metrics(q, pack_rag(cut, dates, texts, b))
                    m["b%d" % b] = {k: c[k] for k in ("gold_session_recall", "gold_round_recall", "gold_latest_in_context", "stale_only", "tokens_used")}
                    m["b%d" % b]["any_gold_session"] = 1.0 if (c["gold_session_recall"] or 0) > 0 else 0.0
                rec["arms"][arm] = m
            per[q.qid] = rec
        out["runs"][dirname] = {"n_questions": len(per), "per_question": per}

    def agg(sel: Dict[str, Any], arm: str, ok_only: bool = False):
        rows = [r for r in sel.values() if arm in r["arms"] and (not ok_only or arm not in r["info"] or r["info"][arm]["status"] == "ok")]
        if not rows:
            return None
        ku = [r for r in rows if r["arms"][arm]["b4000"]["gold_latest_in_context"] is not None]
        d: Dict[str, Any] = {"n": len(rows), "session_recall_at10": mean(r["arms"][arm]["session_recall_at10"] for r in rows)}
        for b in BUDGETS:
            k = "b%d" % b
            d[k] = {"any_gold_session": mean(r["arms"][arm][k]["any_gold_session"] for r in rows),
                    "gold_session_recall": mean(r["arms"][arm][k]["gold_session_recall"] for r in rows),
                    "gold_round_recall": mean(r["arms"][arm][k]["gold_round_recall"] for r in rows),
                    "tokens_used": mean(r["arms"][arm][k]["tokens_used"] for r in rows),
                    "ku_n": len(ku), "ku_gold_latest": mean(1.0 if r["arms"][arm][k]["gold_latest_in_context"] else 0.0 for r in ku),
                    "ku_stale_only": mean(1.0 if r["arms"][arm][k]["stale_only"] else 0.0 for r in ku)}
        if arm in ("ANALYST", "ANALYST-RERANK-ONLY"):
            inf = [r["info"][arm] for r in rows if arm in r["info"]]
            oks = [i for i in inf if i["status"] == "ok"]
            d["rejected"] = len(inf) - len(oks)
            d["rejection_rate"] = (len(inf) - len(oks)) / len(inf)
            d["rejected_ids_in_ok_files"] = sum(i["rejected_ids"] or 0 for i in oks)
            d["tool_calls_mean_ok"] = mean(i["tool_calls"] for i in oks)
            d["zero_tool_ok"] = sum(1 for i in oks if not i["tool_calls"])
            d["seconds_mean_ok"] = mean(i["seconds"] for i in oks)
            d["out_tokens_mean_ok"] = mean(i["out_tokens"] for i in oks)
            d["cache_creation_mean_ok"] = mean(i["cache_creation"] for i in oks)
        return d

    def paired(sel, a, b, getter):
        rows = [(ctx.clusters[qid], getter(r["arms"][b]), getter(r["arms"][a])) for qid, r in sel.items()
                if a in r["arms"] and b in r["arms"]]
        if len(rows) < 3:
            return None
        res = clustered_paired_bootstrap(rows, n_boot=2000, seed=16096)
        return {"n": len(rows), "clusters": int(res["effective_n"]), "mean_diff": res["mean_diff"], "ci_low": res["ci_low"], "ci_high": res["ci_high"]}

    g_sr = lambda m: m["session_recall_at10"]
    g_ctx = lambda m: m["b4000"]["gold_session_recall"]
    g_rd = lambda m: m["b4000"]["gold_round_recall"]
    for dirname, run in out["runs"].items():
        per = run["per_question"]
        subsets = {"all": per, "low_overlap": {k: v for k, v in per.items() if v["low"]}, "rest": {k: v for k, v in per.items() if not v["low"]}}
        run["summary"] = {}
        for sname, sel in subsets.items():
            if not sel:
                continue
            run["summary"][sname] = {"n": len(sel)}
            for arm in ARMS:
                a = agg(sel, arm)
                if a:
                    run["summary"][sname][arm] = {"failures_counted": a, "ok_only": agg(sel, arm, True) if arm.startswith("ANALYST") else None}
            run["summary"][sname]["paired"] = {}
            for arm in ("ANALYST", "ANALYST-RERANK-ONLY", "RERANK"):
                for base in ("RRF", "RERANK"):
                    if arm == base or (arm == "RERANK" and base == "RERANK"):
                        continue
                    pr = {"session_recall_at10": paired(sel, arm, base, g_sr), "context4000_gold_session_recall": paired(sel, arm, base, g_ctx),
                          "context4000_gold_round_recall": paired(sel, arm, base, g_rd)}
                    if any(pr.values()):
                        run["summary"][sname]["paired"]["%s minus %s" % (arm, base)] = pr
            if "ANALYST" in run["summary"][sname] and "ANALYST-RERANK-ONLY" in run["summary"][sname]:
                run["summary"][sname]["paired"]["ANALYST minus ANALYST-RERANK-ONLY"] = {
                    "session_recall_at10": paired(sel, "ANALYST", "ANALYST-RERANK-ONLY", g_sr),
                    "context4000_gold_session_recall": paired(sel, "ANALYST", "ANALYST-RERANK-ONLY", g_ctx),
                    "context4000_gold_round_recall": paired(sel, "ANALYST", "ANALYST-RERANK-ONLY", g_rd)}
        run.pop("per_question")
    # cross-check of tool-call counts: runner result files vs my call log
    out["cross_check"] = {"runner_tool_calls_sum_ANALYST_all_runs": tot_runner_calls, "my_log_tool_rows_excluding_probe": len(log)}
    json.dump(out, open(out_json, "w"), indent=1)
    print(json.dumps(out["cross_check"]), "rerank questions", rerank_n)


if __name__ == "__main__":
    main(*sys.argv[1:8])
