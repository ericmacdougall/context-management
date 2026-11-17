"""Score the analyst canary (pilot, H3, the 23 pool-level low-overlap questions) against the retrieval arms. New; no LLM.
A pipeline check on 23 questions, NOT a result. Rejected result files score as FAILURES (empty ranking: zero recall, a
miss on knowledge-update) and stay in the denominator; ok-only numbers are reported next to them. The runner's invalid
probe row in the call log (tool null) is excluded explicitly."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from .anon import AnonMap
from .build_rag_contexts import level_dates, load_common
from .context import context_metrics, pack_rag
from .metrics import recall_at_k
from .retrievers import to_sessions

BUDGET = 4000


def probe_free_log(path: str):
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    real = [r for r in rows if r.get("tool")]
    return real, len(rows) - len(real)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def main(dataset, art, sets_path, out_root, in_root, rerank_partial, out_json):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    low = json.load(open(os.path.join(out_root, "lowoverlap_pool_h3.json")))["thresholds"]["100"]["qids"]
    qmap = {q.qid: q for q in qs}
    anon = AnonMap(list(ctx.hashes))
    calls, excluded = probe_free_log(os.path.join(out_root, "analyst_calls.jsonl"))
    returned: Dict[str, set] = {}
    secs: Dict[str, float] = {}
    ncalls: Dict[str, int] = {}
    for r in calls:
        returned.setdefault(r["qid"], set()).update(r.get("returned_ids", []))
        secs[r["qid"]] = secs.get(r["qid"], 0.0) + float(r.get("seconds", 0))
        ncalls[r["qid"]] = ncalls.get(r["qid"], 0) + 1
    rr = {r["qid"]: r["levels"]["H3"]["rankings"] for r in json.load(open(rerank_partial))["per_question"]}
    rows: List[Dict[str, Any]] = []
    shape_problems: List[str] = []
    for qid in low:
        q = qmap[qid]
        task = json.load(open(os.path.join(out_root, "analyst_tasks", "H3", qid + ".json")))
        cand = [anon.real_rid(c["round_id"]) for c in task["candidates"]]
        lvl = ctx.level_sessions(q, "H3", sets)
        dates, own = level_dates(q, lvl)
        arms: Dict[str, Any] = {"RRF (tier 0, top 50)": cand, "RERANK (cross-encoder, top-n 20)": rr.get(qid, {}).get("rerank")}
        info: Dict[str, Any] = {}
        for arm in ("ANALYST", "ANALYST-RERANK-ONLY"):
            rp = os.path.join(in_root, "analyst_results", arm, "H3", qid + ".json")
            res = json.load(open(rp))
            for k in ("status", "ranked_round_ids", "tool_calls", "tokens", "seconds"):
                if res.get("status") == "ok" and k not in res:
                    shape_problems.append("%s %s missing %s" % (arm, qid, k))
            ok_ids = {c["round_id"] for c in task["candidates"]}
            if arm == "ANALYST":
                ok_ids |= returned.get(qid, set())
            ids = res.get("ranked_round_ids") or []
            good = [anon.real_rid(i) for i in ids if i in ok_ids] if res.get("status") == "ok" else []
            info[arm] = {"status": res.get("status"), "rejected_ids": (len(ids) - len([i for i in ids if i in ok_ids])) if res.get("status") == "ok" else None,
                         "tool_calls_runner": res.get("tool_calls"), "tokens": res.get("tokens"), "seconds_runner": res.get("seconds")}
            arms[arm] = good
        row: Dict[str, Any] = {"qid": qid, "qtype": q.qtype, "arms": {}, "info": info, "my_log_calls": ncalls.get(qid, 0),
                               "my_log_tool_seconds": secs.get(qid, 0.0)}
        for name, ranked in arms.items():
            ranked = [r for r in (ranked or []) if r.rsplit(":", 1)[0] in dates and r in texts]
            sess10 = to_sessions(ranked)[:10]
            m = context_metrics(q, pack_rag(ranked[:20] if name.startswith("ANALYST") else ranked, dates, texts, BUDGET))
            m["session_recall_at10"] = recall_at_k(sess10, q.gold_session_ids, 10)
            m["gold_in_candidates_top50"] = None
            row["arms"][name] = m
        row["gold_session_in_tier0_top50"] = any(r.rsplit(":", 1)[0] in set(q.gold_session_ids) for r in cand)
        rows.append(row)
    names = list(rows[0]["arms"])
    summ: Dict[str, Any] = {}
    for n in names:
        sel_all = rows
        ok_only = [r for r in rows if not n.startswith("ANALYST") or r["info"][n]["status"] == "ok"]
        def agg(sel):
            ku = [r for r in sel if r["arms"][n]["gold_latest_in_context"] is not None]
            return {"n": len(sel), "session_recall_at10": mean(r["arms"][n]["session_recall_at10"] for r in sel),
                    "context4000_gold_session_recall": mean(r["arms"][n]["gold_session_recall"] for r in sel),
                    "context4000_gold_round_recall": mean(r["arms"][n]["gold_round_recall"] for r in sel),
                    "ku_n": len(ku), "ku_gold_latest_in_context": mean(1.0 if r["arms"][n]["gold_latest_in_context"] else 0.0 for r in ku),
                    "tokens_used": mean(r["arms"][n]["tokens_used"] for r in sel)}
        summ[n] = {"all_23_failures_counted": agg(sel_all), "ok_only": agg(ok_only)}
    use = {}
    for arm in ("ANALYST", "ANALYST-RERANK-ONLY"):
        oks = [r for r in rows if r["info"][arm]["status"] == "ok"]
        use[arm] = {"ok": len(oks), "rejected": len(rows) - len(oks),
                    "tool_calls_mean": mean(r["info"][arm]["tool_calls_runner"] for r in oks),
                    "questions_with_zero_tool_calls": sum(1 for r in oks if not r["info"][arm]["tool_calls_runner"]),
                    "rejected_ids_total": sum(r["info"][arm]["rejected_ids"] or 0 for r in oks),
                    "seconds_runner_mean": mean(r["info"][arm]["seconds_runner"] for r in oks),
                    "output_tokens_mean": mean((r["info"][arm]["tokens"] or {}).get("out") for r in oks)}
    use["my_log_tool_calls_mean_over_analyst_ok"] = mean(r["my_log_calls"] for r in rows if r["info"]["ANALYST"]["status"] == "ok")
    res = {"label": "CANARY: pipeline check on 23 questions, not a result", "level": "H3", "n_questions": len(rows),
           "probe_rows_excluded_from_call_log": excluded, "shape_problems": shape_problems,
           "gold_session_in_tier0_top50": sum(1 for r in rows if r["gold_session_in_tier0_top50"]),
           "summary": summ, "tool_use": use, "per_question": rows}
    json.dump(res, open(out_json, "w"), indent=1)
    print(json.dumps({k: res[k] for k in ("label", "n_questions", "probe_rows_excluded_from_call_log", "shape_problems", "gold_session_in_tier0_top50")}))
    for n, v in summ.items():
        print(n, json.dumps(v["all_23_failures_counted"]), "| ok-only n=%d" % v["ok_only"]["n"], json.dumps(v["ok_only"]))
    print(json.dumps(use))


if __name__ == "__main__":
    main(*sys.argv[1:8])
