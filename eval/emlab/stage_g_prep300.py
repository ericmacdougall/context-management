"""Stage G part A task files at depth 300 (STAGE-G.md Section 8, Amendment 1). Pilot; no model call.
Writes out/analyst_tasks_g300/H3/<qid>.json (new shuffle, same seed rule, ranks 1..300) and a size report estimated at the
MEASURED 2.47 characters per token over the rendered candidate lines plus the fixed prompt text. The depth-400 files are untouched."""
from __future__ import annotations

import json
import os
import random
import sys

from .anon import opaque_rid, opaque_sid
from .build_rag_contexts import level_dates, load_common
from .guard import check as guard_check
from .live import ExactRetriever
from .pool import with_sessions
from .retrievers import run_arm
from .stage_g_prep import cut_text

DEPTH = 300
CHARS_PER_TOKEN = 2.47  # measured on 078150f1 at depth 400: 66,388 tokens
FIXED_PROMPT_CHARS = 2800  # 700 tokens of fixed prompt text at 4 chars, a generous bound for the fixed text


def line(c):
    return "[%s] %s %s\n%s\n\n" % (c["round_id"], c["date"], c["session_id"], c["text"])


def est_tokens(cands):
    return int((sum(len(line(c)) for c in cands) + FIXED_PROMPT_CHARS) / CHARS_PER_TOKEN) + 1


def main(dataset, art, sets_path, out_root):
    all_qs, qs, ctx, sets, texts = load_common(dataset, art, sets_path)
    qs = [q for q in qs if q.retrieval_gold_session_ids]
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    ex.prepare_terms(qs)
    tdir = os.path.join(out_root, "analyst_tasks_g300", "H3")
    os.makedirs(tdir, exist_ok=True)
    sizes, sizes400 = {}, {}
    for q in qs:
        ids = ctx.level_sessions(q, "H3", sets)
        dates, _ = level_dates(q, ids)
        ranked = run_arm(ex, with_sessions(q, ids), "rrf", 400, "off", rrf_k=60)

        def mk(rids):
            return [{"round_id": opaque_rid(r), "text": cut_text(texts[r]), "session_id": opaque_sid(r.rsplit(":", 1)[0]),
                     "date": dates[r.rsplit(":", 1)[0]]} for r in rids]
        sizes400[q.qid] = est_tokens(mk(ranked[:400]))
        top = ranked[:DEPTH]
        order = list(range(1, len(top) + 1))
        random.Random("16096:G:" + q.qid).shuffle(order)
        cands = mk([top[i - 1] for i in order])
        sizes[q.qid] = est_tokens(cands)
        task = {"_meta": {"question_id": q.qid, "level": "H3", "variant": "g300", "depth": len(top), "shuffle_seed": "16096:G:" + q.qid,
                          "shuffle": order, "note": "_meta is for the runner only and must never be passed to a model"},
                "question": q.question, "question_date": q.question_date.strftime("%Y-%m-%d %H:%M"), "candidates": cands}
        guard_check(task, set(ctx.hashes), {x.qid for x in all_qs})
        json.dump(task, open(os.path.join(tdir, q.qid + ".json"), "w"))
    rep = {"depth": DEPTH, "chars_per_token": CHARS_PER_TOKEN, "n_files": len(sizes),
           "est_tokens_depth300": {"max": max(sizes.values()), "mean": sum(sizes.values()) / len(sizes), "min": min(sizes.values())},
           "est_tokens_depth400_same_method": {"max": max(sizes400.values()), "mean": sum(sizes400.values()) / len(sizes400)},
           "calibration_078150f1_depth400": {"estimate": sizes400.get("078150f1"), "measured": 66388},
           "largest_over_60000": sorted(x for x, v in sizes.items() if v > 60000)}
    json.dump(rep, open(os.path.join(out_root, "stage_g", "prep_report_g300.json"), "w"), indent=1)
    print(json.dumps(rep))


if __name__ == "__main__":
    main(*sys.argv[1:5])
