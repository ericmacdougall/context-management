"""Write analyst scope files and task files (pilot, levels H1-H3). Nothing written reveals gold, the answer, or any
other question. New in phase 3; no LLM.

  scope  <out>/analyst_scope/<level>/<qid>.json  session ids and the date shown for THIS question (real for own
         sessions, synthetic for distractors); used by emlab.analyst_tools to bound every tool call
  task   <out>/analyst_tasks/<level>/<qid>.json  question, question date, tier-0 RRF top-50 candidates (opaque ids)
         (secondary variant: n_cand=20 into analyst_tasks_top20)
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from .anon import opaque_rid, opaque_sid
from .guard import check as guard_check
from .build_contexts import FMT, synthetic_date
from .dataset import LongMemEvalLoader, select_split
from .live import ExactRetriever
from .pool import PoolContext, with_sessions
from .retrievers import run_arm

TOOLS_HELP = ("python -m emlab.analyst_tools --artifacts <A> --scope-dir <out>/analyst_scope --level <L> --qid <Q> "
              "--log <runlog> < request.json ; request: {\"tool\": \"search|open_round|open_session|timeline|search_facts\", "
              "\"args\": {...}}")
CAND_TEXT = 2000  # characters of round text handed to the analyst per candidate (the runner caps further at 1200)
FORBIDDEN = ("gold", "answer", "has_answer", "stale")


def make_task(qid: str, level: str, question: str, qdate: str, cands: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Shape agreed with the runner: {question_id, question, question_date, level, candidates:[{round_id, text,
    session_id, date}]}. Ids are OPAQUE (emlab.anon); no gold, no answer, no other question."""
    return {"question_id": qid, "question": question, "question_date": qdate, "level": level, "candidates": cands}


def main(dataset, art, sets_path, out_root, n_cand="50", tasks_dir="analyst_tasks"):
    all_qs = list(LongMemEvalLoader(dataset).questions())
    qs = select_split(all_qs, "pilot")
    sets = json.load(open(sets_path))
    ctx = PoolContext(all_qs)
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    texts = {}
    for line in open(a + "rounds.jsonl", encoding="utf-8"):
        r = json.loads(line)
        texts[r["round_id"]] = r["text"]
    for q in qs:
        own = {}
        for t in q.turns:
            own.setdefault(t.session_id, t.date)
        earliest = min(own.values())
        for lv in ("H1", "H2", "H3"):
            ids = ctx.level_sessions(q, lv, sets)
            dates = {s: (own[s] if s in own else synthetic_date(q.qid, s, earliest, q.question_date)).strftime(FMT) for s in ids}
            ranked = run_arm(ex, with_sessions(q, ids), "rrf", int(n_cand), "off", rrf_k=60)
            cands = [{"round_id": opaque_rid(r), "text": texts[r][:CAND_TEXT], "session_id": opaque_sid(r.rsplit(":", 1)[0]),
                      "date": dates[r.rsplit(":", 1)[0]]} for r in ranked]
            for sub, obj in (("analyst_scope", {"question_id": q.qid, "level": lv, "question_date": q.question_date.strftime(FMT),
                                                 "sessions": dates}),
                             (tasks_dir, make_task(q.qid, lv, q.question, q.question_date.strftime(FMT), cands))):
                if sub != "analyst_scope":  # the scope file is read only by the tool daemon, never by a model
                    guard_check(obj, set(ctx.hashes))
                d = os.path.join(out_root, sub, lv)
                os.makedirs(d, exist_ok=True)
                json.dump(obj, open(os.path.join(d, q.qid + ".json"), "w"))
    print("written", len(qs) * 3, "task and scope files")


if __name__ == "__main__":
    main(*sys.argv[1:7])
