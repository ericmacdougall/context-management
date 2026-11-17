"""Between-draw spread of the shared distractor sets (pilot only): rerun H1 and H2 with extra seeded draws and
report RRF-60 session recall@10 and NDCG@10 per draw. New; no LLM."""
from __future__ import annotations

import json
import sys

from .dataset import LongMemEvalLoader, select_split
from .live import ExactRetriever
from .metrics import ndcg_at_k, recall_at_k
from .pool import PoolContext, with_sessions
from .retrievers import run_arm, to_sessions


def main(dataset, art, sets_path, out, seeds="16096,16097,16098"):
    all_qs = list(LongMemEvalLoader(dataset).questions())
    qs = [q for q in select_split(all_qs, "pilot") if q.retrieval_gold_session_ids]
    ctx = PoolContext(all_qs)
    L = json.load(open(sets_path))["L"]
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    rows = []
    for seed in [int(s) for s in seeds.split(",")]:
        sets = ctx.shared_distractors(select_split(all_qs, "pilot"), "pilot", L, seed=seed)
        for lv in ("H1", "H2"):
            r10, n10 = [], []
            for q in qs:
                pq = with_sessions(q, ctx.level_sessions(q, lv, sets))
                ss = to_sessions(run_arm(ex, pq, "rrf", 200, "off", rrf_k=60))
                r10.append(recall_at_k(ss, q.gold_session_ids, 10))
                n10.append(ndcg_at_k(ss, q.gold_session_ids, 10))
            rows.append({"seed": seed, "level": lv, "rrf_session_recall10": sum(r10) / len(r10),
                         "rrf_session_ndcg10": sum(n10) / len(n10), "n": len(qs)})
            print(rows[-1], flush=True)
    json.dump({"split": "pilot", "rows": rows}, open(out, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:6])
