"""One detached, resumable batch job for the analyst arms. Run ONLY under the harness capped runner, for example:
  HARNESS_LOCAL_OK="analyst arms, capped, concurrency 2" python3 ~/.claude/hooks/run_capped.py --max-rss-gb 4 --max-procs 24 --timeout-s 14400 -- \\
      python3 -u -m phasef.analyst_job --phase canary --tasks-dir DIR --backend "ssh ... prog" --lowoverlap-ids FILE --canary-level LEVEL ...
Phases: canary (both arms, canary level, low-overlap questions) -> rest (both arms at --levels for all questions, canary items skipped) -> secondary (--secondary-level, one arm, low-overlap).
Rules: no retries and no prompt changes; a guard block or a rejected result is a recorded failure for that question and arm; every call is ledgered; stop file ends the loop cleanly;
own call counter (cap 620). Task files: <tasks-dir>/<level>/<question id>.json."""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import run_pilot as R
from .analyst import make_llm, result_path, run_analyst, run_rerank, write_result

ARMS = ("ANALYST-RERANK-ONLY", "ANALYST")
CAP = 620


def plan(phase, questions, lowoverlap, levels, canary_level, secondary_level, secondary_arm):
    """Ordered work items (arm, level, qid)."""
    canary = [(a, canary_level, q) for q in lowoverlap for a in ARMS]
    if phase == "canary":
        return canary
    if phase == "rest":
        done = set(canary)
        return [(a, lv, q) for lv in levels for q in questions for a in ARMS if (a, lv, q) not in done]
    if phase == "secondary":
        return [(secondary_arm, secondary_level, q) for q in lowoverlap]
    raise ValueError(phase)


def process(item, a, runner=None):
    arm, level, qid = item
    task = json.load(open(os.path.join(a.tasks_dir, level, qid + ".json")))
    cache = os.path.join(a.work, "cache")
    ledger = os.path.join(a.work, "ledger.jsonl")
    if arm == "ANALYST":
        rec = run_analyst(task, os.path.join(a.work, "run"), ["--backend", a.backend], cache, ledger, CAP, R.NEUTRAL_CWD, forbidden=a.forbidden, runner=runner)
    else:
        m = make_llm(cache, ledger, CAP, R.NEUTRAL_CWD, forbidden=a.forbidden, runner=runner)
        rec = run_rerank(task, m, a.forbidden)
    rec["arm"], rec["level"] = arm, level  # the file layout is keyed by the item, not by what the task file says
    rec["question_id"] = qid
    return write_result(a.out, rec), rec


def push(a, paths):
    """One ssh per batch: tar of the new result files streamed to the host (creates only analyst_results under ~/emlab-16096/in)."""
    if not paths or not a.remote:
        return
    rel = [os.path.relpath(p, a.out) for p in paths]
    lst = os.path.join(a.work, "push_list.txt")
    open(lst, "w").write("\n".join(rel) + "\n")
    tar = subprocess.Popen(["tar", "-C", a.out, "-cf", "-", "-T", lst], stdout=subprocess.PIPE)
    r = subprocess.run(["ssh", "-F", os.path.expanduser("~/.ssh/config_fleetops"), a.remote, "mkdir -p ~/emlab-16096/in && tar -C ~/emlab-16096/in -xf -"], stdin=tar.stdout, capture_output=True, text=True)
    tar.stdout.close()
    tar.wait()
    if r.returncode != 0:
        print("PUSH FAILED (will retry with the next batch):", r.stderr[-200:], flush=True)
        raise RuntimeError("push failed")


def run(a, runner=None):
    questions = json.load(open(a.questions_file))
    low = json.load(open(a.lowoverlap_ids))
    items = [i for i in plan(a.phase, questions, low, a.levels, a.canary_level, a.secondary_level, a.secondary_arm) if not os.path.exists(result_path(a.out, i[0], i[1], i[2]))]
    print("phase %s: %d items to run" % (a.phase, len(items)), flush=True)
    pending, stats = [], {"ok": 0, "failed": 0}
    t0 = time.time()

    def one(item):
        if os.path.exists(a.stop_file):
            return None
        try:
            return process(item, a, runner)
        except Exception as e:  # a crashed call is recorded as a failure for this item; no retry
            rec = {"question_id": item[2], "level": item[1], "arm": item[0], "status": "failed", "error": str(e)[:200]}
            return write_result(a.out, rec), rec
    with ThreadPoolExecutor(a.concurrency) as ex:
        for k, r in enumerate(ex.map(one, items)):
            if r is None:
                continue
            path, rec = r
            stats["ok" if rec.get("status") == "ok" else "failed"] += 1
            pending.append(path)
            if len(pending) >= a.push_every:
                try:
                    push(a, pending)
                    pending = []
                except RuntimeError:
                    pass
            if k % 20 == 0:
                print("progress", k, "of", len(items), stats, "elapsed %.0fs" % (time.time() - t0), flush=True)
    try:
        push(a, pending)
    except RuntimeError:
        pass
    print("PHASE DONE", a.phase, stats, "stopped" if os.path.exists(a.stop_file) else "complete", flush=True)
    return stats


def parse(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["canary", "rest", "secondary"])
    ap.add_argument("--tasks-dir", required=True)
    ap.add_argument("--backend", required=True, help='tool program command, e.g. "ssh -F cfg t2-dev /path/prog"')
    ap.add_argument("--questions-file", required=True, help="JSON list of the 94 question ids")
    ap.add_argument("--lowoverlap-ids", required=True, help="JSON list of the 23 low-overlap question ids")
    ap.add_argument("--canary-level", required=True)
    ap.add_argument("--levels", nargs="+", default=["H1", "H2", "H3"])
    ap.add_argument("--secondary-level", default=None)
    ap.add_argument("--secondary-arm", default="ANALYST")
    ap.add_argument("--work", default=os.path.join(R.OUT, "analyst_job"))
    ap.add_argument("--out", default=os.path.join(R.OUT, "analyst_job", "out"))
    ap.add_argument("--remote", default="t2-dev")
    ap.add_argument("--push-every", type=int, default=40)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--stop-file", default=os.path.join(R.OUT, "analyst_job", "STOP"))
    a = ap.parse_args(argv)
    os.makedirs(a.work, exist_ok=True)
    a.forbidden = set(json.load(open(os.path.join(R.OUT, "all_session_ids.json")))) if os.path.exists(os.path.join(R.OUT, "all_session_ids.json")) else set()
    return a


if __name__ == "__main__":
    run(parse())
