"""RAG contexts at pooled levels and analyst-arm contexts from result files (pilot only; no LLM; new file).

  rag      <out>/contexts[_b8000]/rag/<level>/<qid>.json for H1, H2, H3 at budgets 4000 and 8000
  analyst  <out>/contexts/<ARM>/<level>/<qid>.json from <in>/analyst_results/<ARM>/<level>/<qid>.json
All ids are opaque and every file passes the id guard. Only `ranked_round_ids` of a result file is used; ids that are
neither task candidates nor ids returned by a tool call for that question and level are rejected and counted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

from .analyst_score import allowed_ids, call_stats
from .anon import AnonMap, opaque_rid, opaque_sid
from .build_contexts import FMT, synthetic_date
from .context import context_metrics, pack_rag, write_context
from .dataset import LongMemEvalLoader, select_split
from .pool import PoolContext, with_sessions
from .retrievers import run_arm

LEVELS = ("H1", "H2", "H3")


def level_dates(q, ids, qdate_fmt=FMT):
    own = {}
    for t in q.turns:
        own.setdefault(t.session_id, t.date)
    earliest = min(own.values())
    return {s: (own[s] if s in own else synthetic_date(q.qid, s, earliest, q.question_date)).strftime(qdate_fmt) for s in ids}, set(own)


def anonymise(items, own, real_sids_dates=None):
    out = []
    for i in items:
        d = dict(i, item_id=opaque_rid(i["round_id"]), round_id=opaque_rid(i["round_id"]), session_id=opaque_sid(i["session_id"]))
        d["synthetic_date"] = i["session_id"] not in own
        out.append(d)
    return out


def load_common(dataset, art, sets_path):
    all_qs = list(LongMemEvalLoader(dataset).questions())
    qs = select_split(all_qs, "pilot")
    ctx = PoolContext(all_qs)
    sets = json.load(open(sets_path))
    texts = {}
    for line in open(art.rstrip("/") + "/rounds.jsonl", encoding="utf-8"):
        r = json.loads(line)
        texts[r["round_id"]] = r["text"]
    return all_qs, qs, ctx, sets, texts


def build_rag(dataset, art, sets_path, out_root):
    from .live import ExactRetriever
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    rows = []
    for q in qs:
        for lv in LEVELS:
            ids = ctx.level_sessions(q, lv, sets)
            dates, own = level_dates(q, ids)
            ranked = run_arm(ex, with_sessions(q, ids), "rrf", 400, "off", rrf_k=60)
            for budget, root in ((4000, os.path.join(out_root, "contexts")), (8000, os.path.join(out_root, "contexts_b8000"))):
                items = pack_rag(ranked, dates, texts, budget)
                m = context_metrics(q, items)
                write_context(root, "rag", lv, q, budget, anonymise(items, own), set(ctx.hashes), {x.qid for x in all_qs})
                m.update({"qid": q.qid, "qtype": q.qtype, "arm": "rag", "level": lv, "budget": budget, "abstention": q.is_abstention})
                rows.append(m)
    json.dump(rows, open(os.path.join(out_root, "rag_context_metrics.json"), "w"), indent=1)
    return len(rows)


def build_analyst(dataset, art, sets_path, out_root, in_root, calls_log, arm):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    anon = AnonMap(list(ctx.hashes))
    rows = []
    for q in qs:
        for lv in LEVELS:
            rp = os.path.join(in_root, "analyst_results", arm, lv, q.qid + ".json")
            if not os.path.exists(rp):
                continue
            res = json.load(open(rp))
            row = {"qid": q.qid, "level": lv, "arm": arm, "status": res.get("status")}
            if res.get("status") != "ok":
                rows.append(row)
                continue
            task = json.load(open(os.path.join(out_root, "analyst_tasks", lv, q.qid + ".json")))
            ok = allowed_ids(task, calls_log, q.qid, lv) if arm == "ANALYST" else {c["round_id"] for c in task["candidates"]}
            ids = res.get("ranked_round_ids") or []
            good = [anon.real_rid(i) for i in ids if i in ok]
            lvl_ids = ctx.level_sessions(q, lv, sets)
            dates, own = level_dates(q, lvl_ids)
            good = [r for r in good if r.rsplit(":", 1)[0] in dates and r in texts][:20]
            items = pack_rag(good, dates, texts, 4000)
            m = context_metrics(q, items)
            write_context(os.path.join(out_root, "contexts"), arm, lv, q, 4000, anonymise(items, own), set(ctx.hashes), {x.qid for x in all_qs})
            m.update(row)
            m.update({"rejected_ids": len(ids) - len([i for i in ids if i in ok]), "returned": len(ids),
                      "tool_calls": res.get("tool_calls"), "seconds": res.get("seconds"), "abstention": q.is_abstention})
            rows.append(m)
    json.dump(rows, open(os.path.join(out_root, "analyst_context_metrics_%s.json" % arm), "w"), indent=1)
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", choices=["rag", "analyst"], required=True)
    ap.add_argument("--dataset-path", required=True)
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--sets", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--in-root")
    ap.add_argument("--calls-log")
    ap.add_argument("--arm", choices=["ANALYST", "ANALYST-RERANK-ONLY"])
    a = ap.parse_args(argv)
    if a.action == "rag":
        print("rag contexts metrics rows", build_rag(a.dataset_path, a.artifacts, a.sets, a.out))
    else:
        print("analyst rows", build_analyst(a.dataset_path, a.artifacts, a.sets, a.out, a.in_root, a.calls_log, a.arm))
    return 0


if __name__ == "__main__":
    sys.exit(main())
