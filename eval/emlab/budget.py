"""Budget proposal for the answer-level pilot (new file; pilot only, no LLM): RAG session recall at H0 as the
packed-context budget grows. Rule: the smallest grid budget whose next-larger grid budget adds less than 0.01 of
mean gold-session recall. Packing = strict prefix of the RRF-60 ranking (emlab.context.pack_rag)."""
from __future__ import annotations

import json
import sys

from .context import context_metrics, pack_rag
from .dataset import LongMemEvalLoader, select_split
from .live import ExactRetriever
from .retrievers import run_arm

GRID = (500, 1000, 2000, 4000, 8000, 16000, 32000)


def main(dataset, art, out):
    qs = [q for q in select_split(list(LongMemEvalLoader(dataset).questions()), "pilot") if q.retrieval_gold_round_ids]
    a = art.rstrip("/") + "/"
    ex = ExactRetriever(a + "chunks.jsonl", a + "chunk_vectors.npy", a + "queries.json", a + "query_vectors.npy")
    texts = {}
    for line in open(a + "rounds.jsonl", encoding="utf-8"):
        r = json.loads(line)
        texts[r["round_id"]] = r["text"]
    rows = {b: {"sess": [], "rnd": [], "tok": []} for b in GRID}
    for q in qs:
        dates = {t.session_id: t.date.strftime("%Y-%m-%d") for t in q.turns}
        ranked = run_arm(ex, q, "rrf", 400, "off", rrf_k=60)
        for b in GRID:
            m = context_metrics(q, pack_rag(ranked, dates, texts, b))
            rows[b]["sess"].append(m["gold_session_recall"])
            rows[b]["rnd"].append(m["gold_round_recall"])
            rows[b]["tok"].append(m["tokens_used"])
    n = len(qs)
    table = [{"budget": b, "gold_session_recall": sum(rows[b]["sess"]) / n, "gold_round_recall": sum(rows[b]["rnd"]) / n,
              "mean_tokens_used": sum(rows[b]["tok"]) / n} for b in GRID]
    def pick(th):
        for i, r in enumerate(table[:-1]):
            if table[i + 1]["gold_session_recall"] - r["gold_session_recall"] < th:
                return r["budget"]
        return None
    json.dump({"split": "pilot", "level": "H0", "n": n,
               "rule_as_first_proposed": "smallest budget whose next grid step adds < 0.01 session recall",
               "proposed_budget_0.01": pick(0.01),
               "rule_pilot_chosen": "same rule with threshold 0.03, chosen on pilot data on 2026-11-15 AFTER the 0.01 rule failed to fire; before any context existed",
               "proposed_budget_0.03": pick(0.03), "table": table}, open(out, "w"), indent=1)
    print(json.dumps(table), pick(0.01), pick(0.03))


if __name__ == "__main__":
    main(*sys.argv[1:4])
