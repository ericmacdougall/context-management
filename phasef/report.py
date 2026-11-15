"""Micro-pilot report: accuracy with Wilson intervals, tokens per question by stage, wall time, calls, extrapolation.
Usage: python3 -m phasef.report <pilot_stats.json>"""
import json
import math
import os
import statistics
import sys

from emlab.metrics import wilson_interval

from . import data, run_pilot as R

OVERHEAD = 11000  # per-call harness overhead (cached prompt), measured from the smallest call


def ledger():
    return [json.loads(l) for l in open(os.path.join(R.OUT, "ledger_v2.jsonl"))]


def tot(r):
    return r["in_tokens"] + r["cache_creation"] + r["cache_read"]


def msd(xs):
    return (statistics.mean(xs), statistics.stdev(xs) if len(xs) > 1 else 0.0)


def main(stats_path):
    res = {k: v for k, v in R.load_results().items() if "label" in v}
    led = ledger()
    print("calls used (counter, includes 10 contaminated ORACLE calls from the first attempt):", R.llm("x").calls_used())
    rows = {}
    for arm in ("ORACLE", "FULL", "FACTS"):
        rs = [v for (a, q), v in res.items() if a == arm]
        k, n = sum(1 for r in rs if r["label"]), len(rs)
        lo, hi = wilson_interval(k, n) if n else (0, 0)
        rd = [r for r in led if r["arm"] == arm and r["stage"] == "read"]
        rin = [tot(r) for r in rd]
        rfresh = [r["in_tokens"] + r["cache_creation"] for r in rd]
        rout = [r["out_tokens"] for r in rd]
        sec = [r["seconds"] for r in rd]
        print("\n%s: %d/%d correct, Wilson95 [%.2f, %.2f]" % (arm, k, n, lo, hi))
        print("  per-question label:", {r["qid"]: r["label"] for r in rs})
        print("  read in total mean %.0f sd %.0f | fresh(non-cache-read) mean %.0f | out mean %.0f sd %.0f | seconds mean %.1f"
              % (msd(rin) + (msd(rfresh)[0],) + msd(rout) + (msd(sec)[0],)))
        j_in = [r["judge_in"] for r in rs]
        j_out = [r["judge_out"] for r in rs]
        print("  judge per item: in mean %.0f out mean %.0f (batched, apportioned)" % (statistics.mean(j_in), statistics.mean(j_out)))
        rows[arm] = {"k": k, "n": n, "read_in": msd(rin), "read_out": msd(rout), "j_in": statistics.mean(j_in), "j_out": statistics.mean(j_out)}
    ex = [r for r in led if r["stage"] == "extract"]
    if ex:
        nses = 192
        s_in = sum(tot(r) for r in ex)
        s_fresh = sum(r["in_tokens"] + r["cache_creation"] for r in ex)
        s_out = sum(r["out_tokens"] for r in ex)
        print("\nEXTRACTION: %d calls, %d sessions: in total %d (fresh %d), out %d, seconds %.0f total" % (len(ex), nses, s_in, s_fresh, s_out, sum(r["seconds"] for r in ex)))
        print("  per question (4 questions, 48 sessions each): in %.0f out %.0f" % (s_in / 4.0, s_out / 4.0))
        per_batch_net = [(tot(r) - OVERHEAD) / 8.0 for r in ex]
        per_s_in, per_s_out = statistics.mean(per_batch_net), s_out / float(nses)
        print("  per session net of overhead: in %.0f (sd across batches %.0f), out %.0f" % (per_s_in, statistics.pstdev(per_batch_net), per_s_out))
        st = json.load(open(stats_path))
        print("\nEXTRAPOLATION (from n=10 reader calls and 24 extraction calls; pilot-split size stats only; labelled estimate)")
        for label, uniq, qn in (("102 pilot questions", st["unique_sessions"], 102), ("500 questions (19,195 unique sessions; mean chars/session taken from the pilot sample)", 19195, 500)):
            calls_ex = math.ceil(uniq / 8.0)
            ex_in = uniq * per_s_in + calls_ex * OVERHEAD
            ex_out = uniq * per_s_out
            print("  %s: extraction %d unique sessions -> %d calls, in %.1fM out %.1fM tokens" % (label, uniq, calls_ex, ex_in / 1e6, ex_out / 1e6))
            for arm in ("ORACLE", "FULL"):
                m, sd = rows[arm]["read_in"]
                print("    %s reading: %d calls, in %.1fM (+-%.1fM per-question sd scaled), out %.2fM" % (arm, qn, qn * m / 1e6, sd * math.sqrt(qn) / 1e6, qn * rows[arm]["read_out"][0] / 1e6))
            if "FACTS" in rows and rows["FACTS"]["n"]:
                m, sd = rows["FACTS"]["read_in"]
                print("    FACTS reading: %d calls, in %.2fM" % (qn, qn * m / 1e6))
            print("    judging: %d questions x 3 arms / 6 per call = %d calls" % (qn, math.ceil(qn * 3 / 6.0)))


if __name__ == "__main__":
    main(sys.argv[1])
