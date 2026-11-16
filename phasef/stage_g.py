"""STAGE G runner (design: eval/STAGE-G.md, rules fixed at 797a4a7, filled in at d1a0e38). Part A: A-DIG (tools) and A-REORDER (no tools) over 400 shuffled candidates at H3.
Part B: reader then judge over three blind context arms. Run only under run_capped from the neutral directory. Separate counters: part A cap 188, part B cap 460 (total at most 648 of 650).
Prompt texts of part A are final and used byte for byte (versions analyst-prompt-g1, reorder-prompt-g1). No retries, failures recorded, stop file, no session persistence."""
import hashlib
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import data, run_pilot as R
from .analyst import (ANALYST_SYSTEM, RERANK_SYSTEM, analyst_extra_args, guarded, make_llm, mcp_config, parse_analyst, qid_of, record_block, write_result, MAX_RESULTS, BUDGET)
from .analyst_job import push
from .judge import build_batch_prompt, parse_batch
from .llm import PromptLeak, check_prompt
from .prompts import JUDGE_SYSTEM, READER_SYSTEM, READER_TEMPLATE_V12, READER_V12_RECIPE

G = os.path.join(R.OUT, "stage_g")
V_DIG, V_REORDER = "analyst-prompt-g1", "reorder-prompt-g1"
CAP_A, CAP_B = 190, 460
LEVEL = "H3_g300"
CAND_N = 300
TASK_DIR = "A300"
ARMS_B = ("RRF", "ANALYST", "ORACLE")
OVERHEAD = 10500
MAX_PROMPT_TOKENS = 60000

DIG_TEMPLATE = ("Question (asked on {question_date}): {question}\n\nThe memory search returned the {n} candidate rounds below, in arbitrary order (round id in brackets). "
                "Many of them are probably not relevant, and the evidence needed to answer may be missing from them. You have tools: search(query, k), open_round(round_id), open_session(session_id), timeline(term). "
                "Rule: first judge how many of the candidates actually contain information that helps answer the question. If fewer than 3 do, you MUST make at least 3 tool calls (for example searches with reworded queries, "
                "or opening the sessions of promising candidates) before you answer; if 3 or more do, tool calls are optional. Use at most 12 tool calls in total. Then return at most 20 round ids, best first, each with a one-line reason. "
                "Round ids may come from the candidates or from tool results.\n\nOutput exactly: {\"results\": [{\"round_id\": \"<id>\", \"reason\": \"<one line>\"}, ...]}\n\nCandidates:\n\n{candidates}")
REORDER_TEMPLATE = ("Question (asked on {question_date}): {question}\n\nBelow are {n} candidate rounds from a user's chat history, in arbitrary order, each with its round id in brackets and its text cut to its first 400 characters. "
                    "Most of them are probably not relevant. Return at most 20 round ids, the rounds most likely to contain the information needed to answer the question, best first, each with a one-line reason. "
                    "Use only the given ids, each at most once.\n\nOutput exactly: {\"results\": [{\"round_id\": \"<id>\", \"reason\": \"<one line>\"}, ...]}\n\nCandidates:\n\n{candidates}")


def cand_lines_g(task):
    """'[{round id}] {date} {session id}\\n{text}'; the text is already cut by the eval owner and is NOT cut again."""
    return "\n\n".join("[%s] %s %s\n%s" % (c["round_id"], c["date"], c["session_id"], c["text"]) for c in task["candidates"])


def _fill(template, task):
    out = template
    for k, v in (("{question_date}", task["question_date"]), ("{question}", task["question"]), ("{n}", str(len(task["candidates"]))), ("{candidates}", cand_lines_g(task))):
        out = out.replace(k, v)
    return out


def build_dig_prompt(task):
    return _fill(DIG_TEMPLATE, task)


def build_reorder_prompt(task):
    return _fill(REORDER_TEMPLATE, task)


def llm_dir(part):
    return os.path.join(G, part, "cache")


def forbidden_ids():
    p = os.path.join(R.OUT, "all_session_ids.json")
    return set(json.load(open(p))) if os.path.exists(p) else set()


def result_base(a):
    return os.path.join(G, "A300", "out")


def stop_requested(a):
    return os.path.exists(a.stop_file)


def prompt_tokens_from(rec_tokens):
    return rec_tokens["in"] + rec_tokens["cache_creation"] + rec_tokens["cache_read"] - OVERHEAD


# ---------------------------------------------------------------- part A
def run_a_item(arm, task, a, runner=None):
    qid, _ = qid_of(task)
    cache, ledger = llm_dir("A"), os.path.join(G, "A", "ledger.jsonl")
    forb = a.forbidden
    key = hashlib.sha1(qid.encode()).hexdigest()[:10]
    base = {"question_id": qid, "level": LEVEL, "arm": arm, "prompt_version": V_DIG if arm == "A-DIG" else V_REORDER}
    allowed = {c["round_id"] for c in task["candidates"]}
    if arm == "A-REORDER":
        prompt, system = build_reorder_prompt(task), RERANK_SYSTEM
        m = make_llm(cache, ledger, CAP_A, R.NEUTRAL_CWD, recipe=V_REORDER, forbidden=forb, runner=runner)
        extra_log = None
    else:
        prompt, system = build_dig_prompt(task), ANALYST_SYSTEM
        run_dir = os.path.join(G, "A300", "run")
        os.makedirs(run_dir, exist_ok=True)
        extra_log = os.path.join(run_dir, key + ".toolcalls.jsonl")
        if os.path.exists(extra_log):
            os.remove(extra_log)
        cfg = mcp_config(os.path.join(run_dir, key + ".mcp.json"), extra_log, ["--backend", a.backend.format(qid=qid)])
        m = make_llm(cache, ledger, CAP_A, R.NEUTRAL_CWD, analyst_extra_args(cfg), recipe=V_DIG + "+mcp", forbidden=forb, runner=runner)
    try:
        guarded(task, prompt, system, forb, build_dig_prompt if arm == "A-DIG" else build_reorder_prompt)
    except PromptLeak as e:
        return record_block(m, arm, qid, LEVEL, "part_a", e)
    c = m.call(prompt, system, "sonnet", arm, qid, "stage_g_a")
    tok = {"in": c["in_tokens"], "cache_creation": c["cache_creation"], "cache_read": c["cache_read"], "out": c["out_tokens"]}
    base.update({"tokens": tok, "seconds": c["seconds"], "tool_calls": 0, "prompt_tokens_measured": prompt_tokens_from(tok) if arm == "A-REORDER" else None})
    if arm == "A-DIG":
        calls = [json.loads(l) for l in open(extra_log)] if os.path.exists(extra_log) else []
        done = [x for x in calls if not x.get("refused")]
        base.update({"tool_calls": len(done), "refused_calls": len(calls) - len(done), "tool_log": [{k: x.get(k) for k in ("tool", "args", "ms", "error")} for x in done], "num_turns": c.get("num_turns")})
        allowed |= {i for x in done for i in x.get("round_ids", [])}
        if len(done) > BUDGET:
            return dict(base, status="rejected", error="tool budget exceeded", ranked_round_ids=[])
    try:
        out = parse_analyst(c["result"], allowed)
    except Exception as e:
        return dict(base, status="rejected", error=str(e)[:200], ranked_round_ids=[])
    return dict(base, status="ok", ranked_round_ids=[r for r, _ in out][:MAX_RESULTS], reasons={r: w for r, w in out})


def tool_error_seen(rec):
    return any(x.get("error") for x in rec.get("tool_log", []) or [])


def part_a_items(qids, arms=("A-REORDER", "A-DIG")):
    return [(arm, q) for q in qids for arm in arms]


# ---------------------------------------------------------------- part B
def arm_key(qid, arm):
    return hashlib.sha1(("16096:G:armkey:%s:%s" % (qid, arm)).encode()).hexdigest()[:8]


def neutral_ctx_path(qid, arm):
    return os.path.join(G, "B", "neutral", hashlib.sha1(("16096:G:ctx:%s:%s" % (qid, arm)).encode()).hexdigest()[:12] + ".txt")


def b_items(qids):
    """Seeded permutation of all (question, arm) items; arm order within a question is shuffled by the same permutation."""
    items = [(q, arm) for q in qids for arm in ARMS_B]
    random.Random("16096:G:order").shuffle(items)
    return items


def final_answer(text):
    m = list(re.finditer(r"Answer:", text))
    return text[m[-1].end():].strip() if m else text.strip()


def run_b_item(qid, arm, qrec, a, runner=None):
    src = os.path.join(G, "B", arm, qid + ".txt")
    rec = {"question_id": qid, "arm": arm, "arm_key": arm_key(qid, arm), "status": "ok", "reader_answer": "", "final_answer": "", "judge": None, "judge_status": "skipped",
           "tokens": {"in": 0, "out": 0, "cache_creation": 0, "cache_read": 0}, "seconds": 0.0}
    cache, ledger = llm_dir("B"), os.path.join(G, "B", "ledger.jsonl")
    if not os.path.exists(src):
        return dict(rec, status="failed_no_context")  # no calls, scored wrong by the eval owner
    os.makedirs(os.path.dirname(neutral_ctx_path(qid, arm)), exist_ok=True)
    nctx = neutral_ctx_path(qid, arm)
    if not os.path.exists(nctx):
        open(nctx, "w").write(open(src).read())
    context = open(nctx).read()
    prompt = READER_TEMPLATE_V12.format(context=context, date=qrec["question_date"], question=qrec["question"])
    mr = make_llm(cache, ledger, CAP_B, R.NEUTRAL_CWD, recipe=READER_V12_RECIPE, forbidden=a.forbidden, runner=runner)
    try:
        check_prompt(prompt, READER_SYSTEM, a.forbidden)
        check_prompt(READER_TEMPLATE_V12.format(context="", date=qrec["question_date"], question=qrec["question"]), READER_SYSTEM, {qid, "H3", "own_haystack", "H0", "H1", "H2"}, patterns=False)
    except PromptLeak as e:
        mr_ = mr
        with open(ledger, "a") as fh:
            fh.write(json.dumps({"arm": arm, "qid": qid, "stage": "guard_block:reader", "error": str(e)[:200]}) + "\n")
        return dict(rec, status="blocked_guard")
    t0 = time.time()
    try:
        c = mr.call(prompt, READER_SYSTEM, "sonnet", arm, qid, "reader")
    except Exception as e:
        return dict(rec, status="failed_reader", error=str(e)[:200])
    rec["reader_answer"], rec["final_answer"] = c["result"], final_answer(c["result"])
    tok = {"in": c["in_tokens"], "out": c["out_tokens"], "cache_creation": c["cache_creation"], "cache_read": c["cache_read"]}
    secs = c["seconds"]
    mj = make_llm(cache, ledger, CAP_B, R.NEUTRAL_CWD, recipe="judge-g1", forbidden=a.forbidden, runner=runner)
    jprompt = build_batch_prompt([(qrec["question_type"], qrec["question"], qrec["answer"], c["result"], qid.endswith("_abs"))])
    try:
        check_prompt(jprompt, JUDGE_SYSTEM, a.forbidden)
        j = mj.call(jprompt, JUDGE_SYSTEM, "opus", arm, qid, "judge")
        tok = {k: tok[k] + v for k, v in (("in", j["in_tokens"]), ("out", j["out_tokens"]), ("cache_creation", j["cache_creation"]), ("cache_read", j["cache_read"]))}
        secs += j["seconds"]
        try:
            rec["judge"] = "yes" if parse_batch(j["result"], 1)[0] else "no"
            rec["judge_status"] = "ok"
        except Exception:
            rec["judge_status"] = "malformed"
    except PromptLeak as e:
        rec["judge_status"] = "blocked_guard"
    except Exception as e:
        rec["judge_status"] = "failed"
    rec["tokens"], rec["seconds"] = tok, round(secs, 2)
    return rec


# ---------------------------------------------------------------- driver
def canary_ids():
    """Rule (stated before the run): part A = the first 2 low-overlap ids and the first 3 other ids, in sorted order; part B = the first sorted id of the knowledge-update stratum and of the low-overlap stratum that has an ANALYST context."""
    low = json.load(open(os.path.join(R.OUT, "analyst_job", "low23.json")))
    q94 = sorted(json.load(open(os.path.join(R.OUT, "analyst_job", "q94.json"))))
    a = sorted(low)[:2] + [q for q in q94 if q not in set(low)][:3]
    qb = json.load(open(os.path.join(G, "B", "question_ids.json")))
    ku = sorted(x["question_id"] for x in qb if x["stratum"] == "knowledge_update")[0]
    lo = sorted(x["question_id"] for x in qb if x["stratum"] == "low_overlap" and x["analyst_context"])[0]
    return a, [ku, lo]


def run(a, runner=None):
    qa_all = sorted(json.load(open(os.path.join(R.OUT, "analyst_job", "q94.json"))))
    qb_all = [x["question_id"] for x in json.load(open(os.path.join(G, "B", "question_ids.json")))]
    questions = json.load(open(os.path.join(G, "B", "questions.json")))
    ca, cb = canary_ids()
    qa = ca if a.phase == "canary" else [q for q in qa_all if q not in ca]
    qb = set(cb) if a.phase == "canary" else set(qb_all) - set(cb)
    items_a = [i for i in part_a_items(qa) if not os.path.exists(os.path.join(result_base(a), "analyst_results", i[0], LEVEL, i[1] + ".json"))] if a.part in ("A", "AB") else []
    done_b = lambda q, arm: os.path.exists(os.path.join(G, "B", "records", arm, q + ".json"))
    items_b = [(q, arm) for q, arm in b_items(qb_all) if q in qb and not done_b(q, arm)] if a.part in ("B", "AB") else []
    print("phase %s: part A %d items, part B %d items (%d calls max)" % (a.phase, len(items_a), len(items_b), 2 * len(items_b)), flush=True)
    stats = {"A_ok": 0, "A_failed": 0, "B_ok": 0, "B_failed": 0}
    meter = TokenMeter(initial_input_tokens(), a.ceiling)
    skipped = {"A": [], "B": []}

    def tok_in(tok):
        return tok.get("in", 0) + tok.get("cache_creation", 0) + tok.get("cache_read", 0)

    def one_a(item):
        arm, q = item
        if stop_requested(a) or meter.over():
            skipped["A"].append(item)
            return None
        task = json.load(open(os.path.join(G, TASK_DIR, "H3", q + ".json")))
        try:
            rec = run_a_item(arm, task, a, runner)
        except Exception as e:
            rec = {"question_id": q, "level": LEVEL, "arm": arm, "status": "failed", "error": str(e)[:200], "ranked_round_ids": []}
        meter.add(tok_in(rec.get("tokens", {})))
        if rec.get("prompt_tokens_measured") and rec["prompt_tokens_measured"] > MAX_PROMPT_TOKENS:
            open(a.stop_file, "w").write("prompt too large: %s" % rec["prompt_tokens_measured"])
        if tool_error_seen(rec):
            open(a.stop_file, "w").write("tool wrapper error on variant g300: stop and report")
        return write_result(result_base(a), dict(rec, arm=arm, level=LEVEL, question_id=q)), rec

    def one_b(item):
        q, arm = item
        if stop_requested(a) or meter.over():
            skipped["B"].append(item)
            return None
        try:
            rec = run_b_item(q, arm, questions[q], a, runner)
        except Exception as e:
            rec = {"question_id": q, "arm": arm, "arm_key": arm_key(q, arm), "status": "failed", "error": str(e)[:200], "judge": None, "judge_status": "skipped"}
        meter.add(tok_in(rec.get("tokens", {})))
        p = os.path.join(G, "B", "records", arm, q + ".json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump(rec, open(p, "w"))
        return p, rec

    class NS:
        pass
    ns_a, ns_b = NS(), NS()
    ns_a.work = ns_b.work = os.path.join(G, "work")
    ns_a.remote = ns_b.remote = a.remote
    ns_a.out, ns_b.out = result_base(a), os.path.join(G, "B", "records")
    os.makedirs(ns_a.work, exist_ok=True)

    def drain(fn, items, tag, ns, pusher, ok_fn):
        pending, t0 = [], time.time()
        with ThreadPoolExecutor(a.concurrency) as ex:
            for k, r in enumerate(ex.map(fn, items)):
                if r is None:
                    continue
                path, rec = r
                stats[tag + ("_ok" if ok_fn(rec) else "_failed")] += 1
                pending.append(path)
                if a.remote and len(pending) >= 40:
                    pusher(ns, pending)
                    pending = []
                if k % 20 == 0:
                    print("%s progress %d of %d" % (tag, k, len(items)), stats, "input tokens so far %.1fM" % (meter.total / 1e6), "elapsed %.0fs" % (time.time() - t0), flush=True)
        if a.remote:
            pusher(ns, pending)

    drain(one_b, items_b, "B", ns_b, push_b, lambda r: r["status"] == "ok" and r["judge_status"] == "ok")  # part B first: its cost is predictable
    drain(one_a, items_a, "A", ns_a, push, lambda r: r["status"] == "ok")  # part A interleaved question by question
    json.dump({"input_tokens_total": meter.total, "ceiling": a.ceiling, "ceiling_hit": meter.over(), "unspent_A": skipped["A"], "unspent_B": skipped["B"]}, open(os.path.join(G, "unspent.json"), "w"))
    print("PHASE DONE", a.phase, stats, "input tokens %.2fM" % (meter.total / 1e6), "ceiling hit" if meter.over() else ("stopped" if stop_requested(a) else "complete"), flush=True)
    return stats


class TokenMeter:
    """Running total of input tokens (uncached, cache creation and cache reads) over ALL stage-G calls, including those already made."""

    def __init__(self, initial, ceiling):
        import threading
        self.total, self.ceiling, self._lock = initial, ceiling, threading.Lock()

    def add(self, n):
        with self._lock:
            self.total += n

    def over(self):
        return self.total > self.ceiling


def initial_input_tokens():
    total = 0
    for part in ("A", "B"):
        p = os.path.join(G, part, "ledger.jsonl")
        if os.path.exists(p):
            for l in open(p):
                r = json.loads(l)
                total += r.get("in_tokens", 0) + r.get("cache_creation", 0) + r.get("cache_read", 0)
    return total


def push_b(ns, paths):
    """Records go to ~/emlab-16096/in/stage_g/B/<ARM>/<id>.json (one tar over one ssh)."""
    import subprocess
    if not paths:
        return
    rel = [os.path.relpath(p, ns.out) for p in paths]
    lst = os.path.join(ns.work, "push_list_b.txt")
    open(lst, "w").write("\n".join(rel) + "\n")
    tar = subprocess.Popen(["tar", "-C", ns.out, "-cf", "-", "-T", lst], stdout=subprocess.PIPE, env=dict(os.environ, COPYFILE_DISABLE="1"))
    r = subprocess.run(["ssh", "-F", os.path.expanduser("~/.ssh/config_fleetops"), ns.remote, "mkdir -p ~/emlab-16096/in/stage_g/B && tar -C ~/emlab-16096/in/stage_g/B -xf -"], stdin=tar.stdout, capture_output=True, text=True)
    tar.stdout.close()
    tar.wait()
    if r.returncode != 0:
        print("PUSH B FAILED:", r.stderr[-200:], flush=True)


def parse(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["canary", "rest"], required=True)
    ap.add_argument("--part", choices=["A", "B", "AB"], default="AB")
    ap.add_argument("--backend", default="ssh -F /Users/eric/.ssh/config_fleetops t2-dev /home/eric/emlab-16096/bin/analyst_tool --question {qid} --level H3 --variant g300")
    ap.add_argument("--remote", default="t2-dev")
    ap.add_argument("--ceiling", type=float, default=40e6, help="stop cleanly when the running total of input tokens passes this")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--stop-file", default=os.path.join(G, "STOP.g300"))
    a = ap.parse_args(argv)
    a.forbidden = forbidden_ids()
    return a


if __name__ == "__main__":
    run(parse())
