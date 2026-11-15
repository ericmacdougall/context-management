"""Structural report of the amended splits (ids, counts, content clusters; no retrieval result, no text)."""
import json
import sys
from collections import Counter

from .dataset import LongMemEvalLoader, full_split_assignment, split_assignment
from .pool import PoolContext


def main(dataset, out):
    qs = list(LongMemEvalLoader(dataset).questions())
    old = split_assignment(qs)
    new = full_split_assignment(qs)
    ctx = PoolContext(qs)
    t = {q.qid: q for q in qs}
    rep = {"old_counts": dict(Counter(old.values())), "new_counts": dict(Counter(new.values()))}
    for name in ("pilot", "dev2", "test", "held_back"):
        ids = sorted(q for q, v in new.items() if v == name)
        rep[name] = {"n": len(ids), "by_type": dict(Counter(t[q].qtype for q in ids)),
                     "abstention": sum(t[q].is_abstention for q in ids),
                     "content_clusters": len({ctx.clusters[q] for q in ids})}
        if name in ("dev2", "held_back"):
            rep[name]["ids"] = ids
    test = [q for q, v in new.items() if v == "test"]
    rep["test_primary_strata_nonabstention_clusters"] = {
        ty: len({ctx.clusters[q] for q in test if t[q].qtype == ty and not t[q].is_abstention})
        for ty in ("knowledge-update", "temporal-reasoning")}
    rep["test_primary_strata_nonabstention_questions"] = {
        ty: sum(1 for q in test if t[q].qtype == ty and not t[q].is_abstention) for ty in ("knowledge-update", "temporal-reasoning")}
    rep["pilot_content_clusters_multi"] = sum(1 for c, n in Counter(ctx.clusters[q] for q, v in new.items() if v == "pilot").items() if n > 1)
    rep["content_clusters_total"] = len(set(ctx.clusters.values()))
    rep["pool"] = {"session_ids": len(ctx.hashes), "distinct_content": len(ctx.pool), "gold_content_hashes": len(ctx.gold_hashes)}
    json.dump(rep, open(out, "w"), indent=1)
    print(json.dumps(rep)[:3000])


if __name__ == "__main__":
    main(*sys.argv[1:3])
