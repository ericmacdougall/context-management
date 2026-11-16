"""Score STAGE-G (pilot; no model call). Part A: A-DIG and A-REORDER at H3 against RRF depth 400 and the cross-encoder.
Part B: judged accuracy of three contexts (RRF, ANALYST, ORACLE) with a gold-string heuristic beside it and a 40-item audit
file. Rules: STAGE-G.md (committed before the calls). Failures (rejections, crashes, missing files) are counted as failures."""
from __future__ import annotations

import json
import os
import random
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

from .anon import AnonMap
from .build_rag_contexts import level_dates, load_common
from .context import context_metrics, pack_rag
from .metrics import clustered_paired_bootstrap, recall_at_k
from .pool import with_sessions
from .retrievers import run_arm, to_sessions
from .xp_common import cluster_wilson

SEED = 16096
DIG_N = 3


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.]", " ", str(s).lower())).strip()


def string_match(gold: str, answer: str) -> bool:
    """Gold-string heuristic: the normalised gold string occurs in the normalised answer, or every number of the gold
    appears among the numbers of the answer (numbers are compared with separators removed)."""
    g, a = normalise(gold), normalise(answer)
    if g and g in a:
        return True
    gn = [x.replace(",", "") for x in _NUM.findall(str(gold))]
    an = {x.replace(",", "") for x in _NUM.findall(str(answer))}
    return bool(gn) and all(x in an for x in gn)


def final_line(text: str) -> str:
    m = list(re.finditer(r"answer\s*:\s*(.+)", text or "", re.I))
    return m[-1].group(1).strip() if m else (text or "").strip().splitlines()[-1] if (text or "").strip() else ""


def followed(tool_calls: Optional[int]) -> bool:
    return (tool_calls or 0) >= DIG_N


def audit_sample(rows: List[Dict[str, Any]], n_each: int = 20, seed: int = SEED) -> List[Dict[str, Any]]:
    """20 judged yes and 20 judged no (or all if fewer), seeded, stable."""
    out = []
    for verdict in (True, False):
        pool = sorted((r for r in rows if r["judge_yes"] is verdict), key=lambda r: (r["question_id"], r["arm"]))
        random.Random("%d:audit:%s" % (seed, verdict)).shuffle(pool)
        out += pool[:n_each]
    return out


def main(dataset, art, sets_path, out_root, in_root, rerank_final, out_json):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    qmap = {q.qid: q for q in qs}
    low = set(json.load(open(os.path.join(out_root, "lowoverlap_pool_h3.json")))["thresholds"]["100"]["qids"])
    anon = AnonMap(list(ctx.hashes))
    calls = [json.loads(l) for l in open(os.path.join(out_root, "analyst_calls.jsonl"), encoding="utf-8")]
    calls = [c for c in calls if c.get("tool")]
    rr = {r["qid"]: r["levels"]["H3"]["rankings"]["rerank"] for r in json.load(open(rerank_final))["per_question"]}
    res: Dict[str, Any] = {"retriever": "exact in-process", "pilot": True, "confirmatory": False}

    # ----------------------------- part A
    from .live import ExactRetriever
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    lists: Dict[str, Dict[str, List[str]]] = {k: {} for k in ("A-DIG", "A-REORDER", "RRF depth 300", "RRF depth 400", "cross-encoder v2", "ANALYST (50, unshuffled)")}
    info: Dict[str, Dict[str, Any]] = {"A-DIG": {}, "A-REORDER": {}}
    dates_of: Dict[str, Dict[str, str]] = {}
    weak: Dict[str, bool] = {}
    for q in qs:
        ids = ctx.level_sessions(q, "H3", sets)
        dates_of[q.qid], _ = level_dates(q, ids)
        full = run_arm(ex, with_sessions(q, ids), "rrf", 400, "off", rrf_k=60)
        lists["RRF depth 300"][q.qid] = full[:300]
        lists["RRF depth 400"][q.qid] = full
        lists["cross-encoder v2"][q.qid] = rr[q.qid]
        old = json.load(open(os.path.join(in_root, "analyst_results", "ANALYST", "H3", q.qid + ".json")))
        t50 = json.load(open(os.path.join(out_root, "analyst_tasks", "H3", q.qid + ".json")))
        ok50 = {c["round_id"] for c in t50["candidates"]}
        for c in calls:
            if c["qid"] == q.qid and c["level"] == "H3" and c.get("variant", "") == "":
                ok50 |= set(c.get("returned_ids", []))
        lists["ANALYST (50, unshuffled)"][q.qid] = ([anon.real_rid(i) for i in (old.get("ranked_round_ids") or []) if i in ok50] if old.get("status") == "ok" else [])
        tg = json.load(open(os.path.join(out_root, "analyst_tasks_g300", "H3", q.qid + ".json")))
        cand_open = {c["round_id"] for c in tg["candidates"]}
        weak[q.qid] = not any(anon.real_rid(c["round_id"]).rsplit(":", 1)[0] in set(q.gold_session_ids) for c in tg["candidates"])
        for arm in ("A-DIG", "A-REORDER"):
            rp = os.path.join(in_root, "analyst_results", arm, "H3_g300", q.qid + ".json")
            if not os.path.exists(rp):
                lists[arm][q.qid] = []
                info[arm][q.qid] = {"status": "missing", "tool_calls": None}
                continue
            rj = json.load(open(rp))
            ok = set(cand_open)
            if arm == "A-DIG":
                for c in calls:
                    if c["qid"] == q.qid and c["level"] == "H3" and c.get("variant", "") == "g":
                        ok |= set(c.get("returned_ids", []))
            ids_out = rj.get("ranked_round_ids") or []
            good = [anon.real_rid(i) for i in ids_out if i in ok][:20] if rj.get("status") == "ok" else []
            lists[arm][q.qid] = good
            logn = sum(1 for c in calls if c["qid"] == q.qid and c["level"] == "H3" and c.get("variant", "") == "g300")
            info[arm][q.qid] = {"status": rj.get("status"), "tool_calls": rj.get("tool_calls"), "log_calls": logn if arm == "A-DIG" else 0,
                                "rejected_ids": len(ids_out) - len([i for i in ids_out if i in ok]) if rj.get("status") == "ok" else None,
                                "seconds": rj.get("seconds"), "out_tokens": (rj.get("tokens") or {}).get("out")}
    per: Dict[str, Dict[str, Dict[str, Any]]] = {k: {} for k in lists}
    for k, L in lists.items():
        for q in qs:
            ranked = [r for r in L[q.qid] if r.rsplit(":", 1)[0] in dates_of[q.qid] and r in texts]
            cut = ranked[:20] if k in ("A-DIG", "A-REORDER", "ANALYST (50, unshuffled)") else ranked
            c = context_metrics(q, pack_rag(cut, dates_of[q.qid], texts, 4000))
            per[k][q.qid] = {"sr10": recall_at_k(to_sessions(ranked)[:10], q.gold_session_ids, 10), "any": 1.0 if (c["gold_session_recall"] or 0) > 0 else 0.0,
                             "sess": c["gold_session_recall"], "round": c["gold_round_recall"], "latest": c["gold_latest_in_context"], "stale": c["stale_only"]}
    subsets = {"all": [q.qid for q in qs], "low_overlap": [q.qid for q in qs if q.qid in low], "rest": [q.qid for q in qs if q.qid not in low]}
    summ: Dict[str, Any] = {}
    for sname, ids_ in subsets.items():
        summ[sname] = {"n": len(ids_), "arms": {}, "paired": {}}
        for k in lists:
            rows = [per[k][x] for x in ids_]
            ku = [r for r in rows if r["latest"] is not None]
            summ[sname]["arms"][k] = {"sr10": mean(r["sr10"] for r in rows), "any_gold_4k": mean(r["any"] for r in rows), "gold_session_4k": mean(r["sess"] for r in rows),
                                      "gold_round_4k": mean(r["round"] for r in rows), "ku_n": len(ku),
                                      "ku_latest": mean(1.0 if r["latest"] else 0.0 for r in ku), "ku_stale_only": mean(1.0 if r["stale"] else 0.0 for r in ku)}
        for a_, b_ in (("A-DIG", "A-REORDER"), ("A-REORDER", "cross-encoder v2"), ("A-DIG", "cross-encoder v2"), ("A-DIG", "RRF depth 300"), ("A-REORDER", "RRF depth 300"),
                       ("A-DIG", "RRF depth 400"), ("A-REORDER", "RRF depth 400"),
                       ("A-DIG", "ANALYST (50, unshuffled)"), ("A-REORDER", "ANALYST (50, unshuffled)")):
            d = {}
            for m in ("sr10", "sess", "round"):
                r = clustered_paired_bootstrap([(ctx.clusters[x], per[b_][x][m] or 0.0, per[a_][x][m] or 0.0) for x in ids_], n_boot=2000, seed=SEED)
                d[m] = {"mean_diff": r["mean_diff"], "ci_low": r["ci_low"], "ci_high": r["ci_high"], "clusters": int(r["effective_n"])}
            summ[sname]["paired"]["%s minus %s" % (a_, b_)] = d
    dig = info["A-DIG"]
    oks = {x: v for x, v in dig.items() if v["status"] == "ok"}
    res["part_a"] = {"summary": summ, "status_counts": {arm: {s: sum(1 for v in info[arm].values() if v["status"] == s) for s in {v["status"] for v in info[arm].values()}} for arm in info},
                     "compliance": {"ok_files": len(oks), "followed_ge_3_calls": sum(1 for v in oks.values() if followed(v["tool_calls"])),
                                    "rate": (sum(1 for v in oks.values() if followed(v["tool_calls"])) / len(oks)) if oks else None,
                                    "tool_call_distribution": {str(n): sum(1 for v in oks.values() if (v["tool_calls"] or 0) == n) for n in range(0, 13)},
                                    "weak_list_by_gold_questions": sum(1 for x in oks if weak[x]), "weak_and_dug": sum(1 for x, v in oks.items() if weak[x] and followed(v["tool_calls"])),
                                    "gold_present_questions": sum(1 for x in oks if not weak[x]), "gold_present_and_not_dug": sum(1 for x, v in oks.items() if not weak[x] and not followed(v["tool_calls"]))},
                     "rejection_rate": {arm: (sum(1 for v in info[arm].values() if v["status"] != "ok") / len(info[arm])) for arm in info}}

    # ----------------------------- part B
    bdir = os.path.join(in_root, "stage_g", "B")
    qids_b = [r["question_id"] for r in json.load(open(os.path.join(out_root, "stage_g", "B", "question_ids.json")))]
    rows_b: List[Dict[str, Any]] = []
    for arm in ("RRF", "ANALYST", "ORACLE"):
        for x in qids_b:
            p = os.path.join(bdir, arm, x + ".json")
            q = qmap[x]
            rec: Dict[str, Any] = {"question_id": x, "arm": arm, "qtype": q.qtype}
            if not os.path.exists(p):
                rec.update({"status": "missing", "judge_yes": False, "heuristic": False, "failure": True})
            else:
                j = json.load(open(p))
                ok = j.get("status") == "ok" and j.get("judge") in ("yes", "no")
                fa = j.get("final_answer") or final_line(j.get("reader_answer", ""))
                rec.update({"status": j.get("status"), "judge_yes": (j.get("judge") == "yes") if ok else False, "failure": not ok,
                            "heuristic": (string_match(q.answer, fa) if q.qtype != "single-session-preference" else None), "reader_answer": j.get("reader_answer"),
                            "final_answer": fa, "gold": q.answer, "question": q.question})
            rows_b.append(rec)
    accs: Dict[str, Any] = {}
    stratum_of = {r["question_id"]: r["stratum"] for r in json.load(open(os.path.join(out_root, "stage_g", "B", "question_ids.json")))}
    for arm in ("RRF", "ANALYST", "ORACLE"):
        rr_ = [r for r in rows_b if r["arm"] == arm]
        accs[arm] = {"n": len(rr_), "failures": sum(1 for r in rr_ if r["failure"]),
                     "judged_accuracy": cluster_wilson({r["question_id"]: 1.0 if r["judge_yes"] else 0.0 for r in rr_}, ctx.clusters),
                     "heuristic_accuracy": mean(1.0 if r["heuristic"] else 0.0 for r in rr_ if r["heuristic"] is not None),
                     "heuristic_n": sum(1 for r in rr_ if r["heuristic"] is not None),
                     "judge_heuristic_disagreement": mean((1.0 if r["judge_yes"] != r["heuristic"] else 0.0) for r in rr_ if r["heuristic"] is not None),
                     "by_stratum": {s: mean(1.0 if r["judge_yes"] else 0.0 for r in rr_ if stratum_of[r["question_id"]] == s) for s in ("low_overlap", "knowledge_update", "stratified")}}
    paired_b: Dict[str, Any] = {}
    for a_, b_ in (("ORACLE", "RRF"), ("ANALYST", "RRF"), ("ORACLE", "ANALYST")):
        va = {r["question_id"]: (1.0 if r["judge_yes"] else 0.0) for r in rows_b if r["arm"] == a_}
        vb = {r["question_id"]: (1.0 if r["judge_yes"] else 0.0) for r in rows_b if r["arm"] == b_}
        r = clustered_paired_bootstrap([(ctx.clusters[x], vb[x], va[x]) for x in qids_b], n_boot=2000, seed=SEED)
        paired_b["%s minus %s" % (a_, b_)] = {"mean_diff": r["mean_diff"], "ci_low": r["ci_low"], "ci_high": r["ci_high"], "clusters": int(r["effective_n"])}
    res["part_b"] = {"n_questions": len(qids_b), "arms": accs, "paired_judged_accuracy": paired_b, "judge_validated": False,
                     "note": "provisional: the judge is not yet validated (V2 gate J1)"}
    aud = audit_sample([r for r in rows_b if not r["failure"]])
    os.makedirs(os.path.join(out_root, "stage_g", "B"), exist_ok=True)
    order = list(range(len(aud)))
    random.Random("%d:audit:order" % SEED).shuffle(order)
    json.dump([{"item": i + 1, "question_type": aud[k]["qtype"], "question": aud[k]["question"], "gold_answer": aud[k]["gold"], "reader_final_answer": aud[k]["final_answer"],
                "judge_verdict": "yes" if aud[k]["judge_yes"] else "no"} for i, k in enumerate(order)], open(os.path.join(out_root, "stage_g", "B", "audit_40.json"), "w"), indent=1)
    json.dump({str(i + 1): {"question_id": aud[k]["question_id"], "arm": aud[k]["arm"]} for i, k in enumerate(order)}, open(os.path.join(out_root, "stage_g", "B", "audit_40_key.json"), "w"), indent=1)
    json.dump(res, open(out_json, "w"), indent=1)
    print("done")


if __name__ == "__main__":
    main(*sys.argv[1:8])
