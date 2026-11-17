"""How many sessions does the round-level residual rule remove from pilot levels? (structural; new)"""
import json
import sys

from .dataset import LongMemEvalLoader, select_split
from .pool import PoolContext


def main(dataset, sets_path, out):
    allq = list(LongMemEvalLoader(dataset).questions())
    qs = select_split(allq, "pilot")
    ctx = PoolContext(allq)
    sets = json.load(open(sets_path))
    rows = {}
    for lv, extra_of in (("H1", lambda q: sets["D500"]), ("H2", lambda q: sets["DL"]),
                         ("H3", lambda q: [s for s in ctx.pool if ctx.hashes[s] not in ctx.cluster_hay[ctx.clusters[q.qid]]])):
        hit = {}
        for q in qs:
            gr = ctx.gold_round_h[q.qid]
            n = [s for s in extra_of(q) if ctx.round_h.get(s, set()) & gr]
            if n:
                hit[q.qid] = len(n)
        rows[lv] = {"questions_affected": len(hit), "sessions_removed_total": sum(hit.values()), "by_question": hit}
    json.dump(rows, open(out, "w"), indent=1)
    print({k: (v["questions_affected"], v["sessions_removed_total"]) for k, v in rows.items()})


if __name__ == "__main__":
    main(*sys.argv[1:4])
