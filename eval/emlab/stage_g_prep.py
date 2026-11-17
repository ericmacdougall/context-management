"""Stage G inputs (pilot; no model call): part A task files (shuffled depth-D candidates, text cut at 400 characters),
the 77-question selection of part B and its three plain-text contexts. Rules are in STAGE-G.md (committed before this ran)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys
from collections import Counter
from typing import Any, Dict, List

from .anon import AnonMap, opaque_rid, opaque_sid
from .build_rag_contexts import level_dates, load_common
from .context import est_tokens, pack_rag, render_round
from .guard import check as guard_check
from .live import ExactRetriever
from .pool import with_sessions
from .retrievers import run_arm

CUT = 400
SANE = 60000


def cut_text(t: str) -> str:
    return t if len(t) <= CUT else t[:CUT] + " [cut]"


def est_prompt(cands: List[Dict[str, str]]) -> int:
    return sum(math.ceil(len(c["text"]) / 4.0) + 30 for c in cands) + 700


def select77(qs, low: set) -> Dict[str, Any]:
    kus = [q.qid for q in qs if q.qtype == "knowledge-update" and not q.is_abstention]
    chosen_low = sorted(x.qid for x in qs if x.qid in low)
    rest = [q for q in qs if q.qid not in set(chosen_low) | set(kus)]
    by: Dict[str, List[str]] = {}
    for q in rest:
        by.setdefault(q.qtype, []).append(q.qid)
    need = 77 - len(chosen_low) - len(kus)
    tot = sum(len(v) for v in by.values())
    quota = {t: need * len(v) / tot for t, v in by.items()}
    alloc = {t: int(math.floor(x)) for t, x in quota.items()}
    left = need - sum(alloc.values())
    for t in sorted(by, key=lambda t: (-(quota[t] - alloc[t]), t))[:left]:
        alloc[t] += 1
    picked = []
    for t, ids in by.items():
        order = sorted(ids, key=lambda i: hashlib.sha256(("16096:G:" + i).encode()).hexdigest())
        picked += order[:alloc[t]]
    return {"low_overlap": chosen_low, "knowledge_update": sorted(kus), "stratified": sorted(picked), "allocation": alloc,
            "remaining_by_type": {t: len(v) for t, v in by.items()}, "all": sorted(set(chosen_low) | set(kus) | set(picked))}


def main(dataset, art, sets_path, out_root, in_root):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    low = set(json.load(open(os.path.join(out_root, "lowoverlap_pool_h3.json")))["thresholds"]["100"]["qids"])
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    anon = AnonMap(list(ctx.hashes))
    qmap = {q.qid: q for q in qs}
    report: Dict[str, Any] = {"cut_chars": CUT, "sane_tokens": SANE}
    ranks400: Dict[str, List[str]] = {}
    for q in qs:
        ids = ctx.level_sessions(q, "H3", sets)
        ranks400[q.qid] = run_arm(ex, with_sessions(q, ids), "rrf", 400, "off", rrf_k=60)
    sizes: Dict[int, List[int]] = {}
    for D in (400, 300):
        sizes[D] = [est_prompt([{"text": cut_text(texts[r])} for r in ranks400[q.qid][:D]]) for q in qs]
    chosen = 400 if max(sizes[400]) <= SANE else 300
    report["sizes"] = {str(D): {"max": max(v), "mean": sum(v) / len(v), "min": min(v)} for D, v in sizes.items()}
    report["depth_D"] = chosen
    report["candidates_per_question_min"] = min(len(ranks400[q.qid]) for q in qs)
    # ---- part A task files
    tdir = os.path.join(out_root, "analyst_tasks_g", "H3")
    os.makedirs(tdir, exist_ok=True)
    for q in qs:
        ids = ctx.level_sessions(q, "H3", sets)
        dates, own = level_dates(q, ids)
        top = ranks400[q.qid][:chosen]
        order = list(range(1, len(top) + 1))
        random.Random("16096:G:" + q.qid).shuffle(order)
        cands = [{"round_id": opaque_rid(top[i - 1]), "text": cut_text(texts[top[i - 1]]), "session_id": opaque_sid(top[i - 1].rsplit(":", 1)[0]),
                  "date": dates[top[i - 1].rsplit(":", 1)[0]]} for i in order]
        task = {"_meta": {"question_id": q.qid, "level": "H3", "variant": "g", "depth": len(top), "shuffle_seed": "16096:G:" + q.qid,
                          "shuffle": order, "note": "_meta is for the runner only and must never be passed to a model"},
                "question": q.question, "question_date": q.question_date.strftime("%Y-%m-%d %H:%M"), "candidates": cands}
        guard_check(task, set(ctx.hashes), {x.qid for x in all_qs})
        json.dump(task, open(os.path.join(tdir, q.qid + ".json"), "w"))
    est = [est_prompt(json.load(open(os.path.join(tdir, q.qid + ".json")))["candidates"]) for q in qs]
    report["written_task_estimate"] = {"max": max(est), "mean": sum(est) / len(est), "n_files": len(est)}
    # ---- part B selection and contexts
    sel = select77(qs, low)
    report["selection"] = sel
    bdir = os.path.join(out_root, "stage_g", "B")
    for arm in ("RRF", "ANALYST", "ORACLE"):
        os.makedirs(os.path.join(bdir, arm), exist_ok=True)
    meta_rows = []
    for qid in sel["all"]:
        q = qmap[qid]
        ids = ctx.level_sessions(q, "H3", sets)
        dates, own = level_dates(q, ids)
        # RRF: existing registered RAG context (depth 400, 4,000 tokens)
        rag = json.load(open(os.path.join(out_root, "contexts", "rag", "H3", qid + ".json")))
        t_rrf = "\n\n".join(i["text"] for i in rag["items"])
        # ANALYST: existing 50-candidate analyst result
        rj = json.load(open(os.path.join(in_root, "analyst_results", "ANALYST", "H3", qid + ".json")))
        t_an = None
        if rj.get("status") == "ok":
            task50 = json.load(open(os.path.join(out_root, "analyst_tasks", "H3", qid + ".json")))
            ok = {c["round_id"] for c in task50["candidates"]}
            calls = [json.loads(l) for l in open(os.path.join(out_root, "analyst_calls.jsonl"))]
            for c in calls:
                if c.get("qid") == qid and c.get("level") == "H3" and c.get("variant", "") == "":
                    ok |= set(c.get("returned_ids", []))
            good = [anon.real_rid(i) for i in (rj.get("ranked_round_ids") or []) if i in ok]
            items = pack_rag([r for r in good if r in texts][:20], dates, texts, 4000)
            t_an = "\n\n".join(i["text"] for i in sorted(items, key=lambda i: (i["date"], i["round_index"], i["rank"])))
        # ORACLE: evidence rounds of gold sessions, chronological, strict prefix at 4,000 tokens
        gold_rounds = [r for r in q.rounds if r.session_id in set(q.gold_session_ids) and r.has_answer]
        gold_rounds.sort(key=lambda r: (dates.get(r.session_id, ""), r.index))
        used, parts = 0, []
        for r in gold_rounds:
            body = render_round(dates[r.session_id], r.text)
            if used + est_tokens(body) > 4000:
                break
            parts.append(body)
            used += est_tokens(body)
        t_or = "\n\n".join(parts)
        for arm, t in (("RRF", t_rrf), ("ANALYST", t_an), ("ORACLE", t_or)):
            if t is None:
                continue
            guard_check({"text": t}, set(ctx.hashes))
            open(os.path.join(bdir, arm, qid + ".txt"), "w", encoding="utf-8").write(t)
        meta_rows.append({"question_id": qid, "question_type": q.qtype,
                          "stratum": "low_overlap" if qid in low else ("knowledge_update" if qid in sel["knowledge_update"] else "stratified"),
                          "analyst_context": t_an is not None, "oracle_items": len(parts), "oracle_gold_evidence_rounds": len(gold_rounds),
                          "oracle_truncated": len(parts) < len(gold_rounds), "rrf_tokens": sum(i["tokens"] for i in rag["items"]),
                          "oracle_tokens": used})
    json.dump(meta_rows, open(os.path.join(bdir, "question_ids.json"), "w"), indent=1)
    report["part_b"] = {"n": len(meta_rows), "by_type": dict(Counter(r["question_type"] for r in meta_rows)),
                        "by_stratum": dict(Counter(r["stratum"] for r in meta_rows)),
                        "analyst_rejected_no_context": [r["question_id"] for r in meta_rows if not r["analyst_context"]],
                        "oracle_truncated": [r["question_id"] for r in meta_rows if r["oracle_truncated"]]}
    json.dump(report, open(os.path.join(out_root, "stage_g", "prep_report.json"), "w"), indent=1)
    print(json.dumps({k: report[k] for k in ("sizes", "depth_D", "written_task_estimate", "part_b")}))


if __name__ == "__main__":
    main(*sys.argv[1:6])
