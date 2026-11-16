"""STAGE X0 extraction canary runner (design: eval/STAGE-X0.md at 24e442c, inputs 7637377). One session per call, prompt extract-v2-anon UNCHANGED,
session ids are already opaque (never hashed again), the key directory out/stage_x0_key is never read. Cap 200 calls, stop at 12M input tokens, concurrency 2, no retries.
Phases: canary (first 10 of run_order) then rest (the other 190). Run under run_capped from the neutral directory."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import data, run_pilot as R
from .facts import _json_from
from .llm import LLM, PromptLeak
from .prompts import (EXTRACT_SYSTEM, EXTRACT_V2_CAP30_RECIPE, EXTRACT_V2_CAP30_TEMPLATE, EXTRACT_V2_RECIPE, EXTRACT_V2_TEMPLATE, EXTRACT_V3_PREF_RECIPE, EXTRACT_V3_PREF_TEMPLATE)

X = os.path.join(R.OUT, "stage_x0")
CAP = 200
TOKEN_STOP = 12_000_000
VERSION = "extract-v2-anon"
TEMPLATE = EXTRACT_V2_TEMPLATE
SHA16 = hashlib.sha256((EXTRACT_SYSTEM + "\n" + EXTRACT_V2_TEMPLATE).encode()).hexdigest()[:16]
SHA16_CAP30 = hashlib.sha256((EXTRACT_SYSTEM + "\n" + EXTRACT_V2_CAP30_TEMPLATE).encode()).hexdigest()[:16]
SHA16_PREF = hashlib.sha256((EXTRACT_SYSTEM + "\n" + EXTRACT_V3_PREF_TEMPLATE).encode()).hexdigest()[:16]
REPLICATE = None  # distinct cache namespace for a fresh identical-prompt run; the records keep the same prompt_version and prompt_sha16


def set_variant(name):
    """'base' = extract-v2-anon, 'cap30' = extract-v2-cap30 (only the fact cap number differs)."""
    global VERSION, TEMPLATE, SHA16, RECIPE
    if name == "cap30":
        VERSION, TEMPLATE, SHA16, RECIPE = EXTRACT_V2_CAP30_RECIPE, EXTRACT_V2_CAP30_TEMPLATE, SHA16_CAP30, EXTRACT_V2_CAP30_RECIPE
    elif name == "base":
        VERSION, TEMPLATE, SHA16, RECIPE = "extract-v2-anon", EXTRACT_V2_TEMPLATE, hashlib.sha256((EXTRACT_SYSTEM + "\n" + EXTRACT_V2_TEMPLATE).encode()).hexdigest()[:16], EXTRACT_V2_RECIPE
    elif name == "pref":
        VERSION, TEMPLATE, SHA16, RECIPE = EXTRACT_V3_PREF_RECIPE, EXTRACT_V3_PREF_TEMPLATE, SHA16_PREF, EXTRACT_V3_PREF_RECIPE
MODEL = "claude-sonnet-5-5"
PRICE_TABLE = "claude CLI total_cost_usd (notional, subscription path)"
REMOTE_IN = "~/emlab-16096/in/stage_x0"
FIELDS = ("subject", "slot", "predicate", "object")
RECIPE = EXTRACT_V2_RECIPE


def render_session(sid, date, turns):
    """Same format as phasef.facts.render_for_extraction but with the id exactly as given (it is already opaque)."""
    rs = data.rounds_of(sid, turns, date)
    return "### SESSION id=%s date=%s\n%s" % (sid, date, "\n".join("[r%d] %s" % (r.index, r.text) for r in rs))


def build_prompt(sid, date, turns, template=None):
    return (template or TEMPLATE).format(n=1, sessions=render_session(sid, date, turns))


def variant_cfg(name, replicate=None):
    """Per-call configuration (thread safe: no module globals are touched)."""
    t = {"base": (EXTRACT_V2_TEMPLATE, "extract-v2-anon", EXTRACT_V2_RECIPE), "cap30": (EXTRACT_V2_CAP30_TEMPLATE, EXTRACT_V2_CAP30_RECIPE, EXTRACT_V2_CAP30_RECIPE),
         "pref": (EXTRACT_V3_PREF_TEMPLATE, EXTRACT_V3_PREF_RECIPE, EXTRACT_V3_PREF_RECIPE)}[name]
    return {"template": t[0], "version": t[1], "recipe": t[2] + ("#rep" + replicate if replicate else ""), "replicate": replicate,
            "sha16": hashlib.sha256((EXTRACT_SYSTEM + "\n" + t[0]).encode()).hexdigest()[:16]}


def parse_one(text, sid):
    """-> (facts list, dropped, repaired). Raises ValueError when no usable JSON object or no entry for this session."""
    t = text.strip()
    a, b = t.find("{"), t.rfind("}")
    body = t[a:b + 1] if a >= 0 and b >= 0 else ""
    repaired = False
    try:
        json.loads(body)
    except ValueError:
        repaired = True
    obj = _json_from(text)  # raises ValueError when there is no JSON object; closes missing brackets
    ent = [e for e in obj.get("sessions", []) if e.get("session_id") == sid]
    if not ent:
        raise ValueError("no entry for the session in the output")
    facts, dropped = [], 0
    for f in ent[0].get("facts", []):
        if not isinstance(f, dict) or not all(k in f for k in FIELDS):
            dropped += 1
            continue
        g = dict(f)
        rnd = f.get("round")
        g["round"] = rnd if isinstance(rnd, int) else 0
        g["session_id"] = sid
        g["round_id"] = "%s:%d" % (sid, g["round"])
        facts.append(g)
    return facts, dropped, repaired


def load_inputs():
    order = json.load(open(os.path.join(X, "run_order.json")))["order"]
    return order


def fetch(stage="stage_x0"):
    """Read only: the session files and run_order.json of the stage (never the key directory)."""
    ssh = ["ssh", "-F", os.path.expanduser("~/.ssh/config_fleetops"), "t2-dev"]
    raw = subprocess.run(ssh + ["cd ~/emlab-16096/out && tar -cf - %s/sessions %s/run_order.json" % (stage, stage)], capture_output=True, check=True).stdout
    tarfile.open(fileobj=io.BytesIO(raw)).extractall(R.OUT)
    print("sessions", len(os.listdir(os.path.join(R.OUT, stage, "sessions"))), "order", len(json.load(open(os.path.join(R.OUT, stage, "run_order.json")))["order"]))


class Meter:
    def __init__(self):
        self.total, self.calls, self._lock = 0, 0, threading.Lock()

    def add(self, n):
        with self._lock:
            self.total += n
            self.calls += 1

    def over(self):
        return self.total > TOKEN_STOP


def run_one(idx, sid, a, meter, runner=None, cfg=None):
    sess = json.load(open(os.path.join(X, "sessions", sid + ".json")))
    assert sess["session_id"] == sid
    turns = sess["turns"]
    cfg = cfg or {"template": TEMPLATE, "version": VERSION, "recipe": RECIPE + ("#rep" + REPLICATE if REPLICATE else ""), "replicate": REPLICATE, "sha16": SHA16}
    prompt = build_prompt(sid, sess["date"], turns, cfg["template"])
    rec = {"session_id": sid, "order_index": idx, "status": "ok", "reject_reason": None, "prompt_version": cfg["version"], "prompt_sha16": cfg["sha16"], "model": MODEL, "input_tokens": 0, "output_tokens": 0,
           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "seconds": 0.0, "cost_usd": None, "dropped": 0, "facts": [], "raw_output_path": None,
           "replicate": cfg["replicate"], "session_chars": sum(len(t["content"]) for t in turns), "session_rounds": len(data.rounds_of(sid, turns, sess["date"])), "n_facts": 0, "error": None}
    rawdir = os.path.join(X, "raw")
    os.makedirs(rawdir, exist_ok=True)
    llm = LLM(os.path.join(X, "cache"), os.path.join(X, "ledger.jsonl"), cap=CAP, concurrency=2, recipe=cfg["recipe"], cwd=R.NEUTRAL_CWD, runner=runner)
    try:
        c = llm.call(prompt, EXTRACT_SYSTEM, "sonnet", "X0", sid, "extract")
    except PromptLeak as e:
        return dict(rec, status="rejected", reject_reason="guard: %s" % str(e)[:150], error=str(e)[:200])
    except Exception as e:
        return dict(rec, status="call_error", error=str(e)[:300])
    meter.add(c["in_tokens"] + c["cache_creation"] + c["cache_read"])
    tag = (cfg.get("arm") + "_") if cfg.get("arm") else ""
    rawp = os.path.join(X, "raw", tag + sid + ".txt")
    open(rawp, "w").write(c["result"])
    rec.update({"input_tokens": c["in_tokens"], "output_tokens": c["out_tokens"], "cache_read_input_tokens": c["cache_read"], "cache_creation_input_tokens": c["cache_creation"],
                "seconds": c["seconds"], "cost_usd": c["cost_usd"], "raw_output_path": "raw/%s%s.txt" % (tag, sid)})
    try:
        facts, dropped, repaired = parse_one(c["result"], sid)
    except Exception as e:
        return dict(rec, status="parse_failure", error=str(e)[:200])
    return dict(rec, status="repaired" if repaired else "ok", facts=facts, dropped=dropped, n_facts=len(facts))


def push(paths_rel):
    if not paths_rel:
        return
    lst = os.path.join(X, "push_list.txt")
    open(lst, "w").write("\n".join(paths_rel) + "\n")
    tar = subprocess.Popen(["tar", "-C", os.path.join(X, "out"), "-cf", "-", "-T", lst], stdout=subprocess.PIPE, env=dict(os.environ, COPYFILE_DISABLE="1"))
    r = subprocess.run(["ssh", "-F", os.path.expanduser("~/.ssh/config_fleetops"), "t2-dev", "mkdir -p %s && tar -C %s -xf -" % (REMOTE_IN, REMOTE_IN)], stdin=tar.stdout, capture_output=True, text=True)
    tar.stdout.close()
    tar.wait()
    if r.returncode != 0:
        print("PUSH FAILED:", r.stderr[-200:], flush=True)


def canary_check(recs, n=20, min_parse=0.95, max_prompt=40000):
    """X1 gate on the first n results: parse rate (ok or repaired) at least 95 percent and no prompt over 40,000 input tokens (prompt = total input minus the 10.5k per-call overhead).
    Returns (continue?, reason)."""
    first = recs[:n]
    if len(first) < n:
        return True, "not enough results yet"
    rate = sum(1 for r in first if r["status"] in ("ok", "repaired")) / float(n)
    if rate < min_parse:
        return False, "parse rate %.2f below %.2f in the first %d calls" % (rate, min_parse, n)
    big = [r for r in first if r["input_tokens"] + r["cache_creation_input_tokens"] + r["cache_read_input_tokens"] - 10500 > max_prompt]
    if big:
        return False, "prompt over %d tokens in %d of the first %d calls" % (max_prompt, len(big), n)
    return True, "canary ok: parse rate %.2f" % rate


def confirm_no_earlier_cache(order, cfg=None):
    """No X1 prompt may have an entry in any earlier stage's cache: compares request hashes against every phasef-out/*/cache directory except this stage's."""
    from .llm import request_hash
    cfg = cfg or variant_cfg("cap30")
    earlier = set()
    for d in os.listdir(R.OUT):
        c = os.path.join(R.OUT, d, "cache")
        if os.path.isdir(c) and os.path.abspath(os.path.join(R.OUT, d)) != os.path.abspath(X):
            earlier |= {f[:-5] for f in os.listdir(c) if f.endswith(".json")}
    clashes = []
    for sid in order:
        sess = json.load(open(os.path.join(X, "sessions", sid + ".json")))
        key = request_hash("sonnet", cfg["recipe"], EXTRACT_SYSTEM, build_prompt(sid, sess["date"], sess["turns"], cfg["template"]))
        if key in earlier:
            clashes.append(sid)
    return clashes


def not_run_record(idx, sid):
    return {"session_id": sid, "order_index": idx, "status": "not_run", "reject_reason": None, "prompt_version": VERSION, "prompt_sha16": SHA16, "model": MODEL, "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "seconds": 0.0, "cost_usd": None, "dropped": 0, "facts": [], "raw_output_path": None, "n_facts": 0, "error": None}


def write_not_run(order, lo, hi):
    """Placeholder records for every session in [lo, hi) without a record, so the scorer's denominators stay complete."""
    out = []
    for i in range(lo, hi):
        p = os.path.join(X, "out", order[i] + ".json")
        if not os.path.exists(p):
            json.dump(not_run_record(i, order[i]), open(p, "w"))
            out.append(order[i])
    return out


def run(a, runner=None):
    order = load_inputs()
    lo, hi = (0, 10) if a.phase == "canary" else ((0, len(order)) if a.phase == "all" else (10, 200))
    if a.gate:
        clashes = confirm_no_earlier_cache(order, variant_cfg("base" if VERSION == "extract-v2-anon" else ("pref" if VERSION == "extract-v3-pref" else "cap30"), REPLICATE))
        if clashes:
            print("ABORT: %d prompts have an entry in an earlier stage's cache, e.g. %s" % (len(clashes), clashes[:3]), flush=True)
            return {}
        print("confirmed: none of the %d prompts has an entry in any earlier stage's cache" % len(order), flush=True)
    items = [(i, order[i]) for i in range(lo, hi) if not os.path.exists(os.path.join(X, "out", order[i] + ".json"))]
    print("phase %s: %d sessions" % (a.phase, len(items)), flush=True)
    meter = Meter()
    for l in (open(os.path.join(X, "ledger.jsonl")) if os.path.exists(os.path.join(X, "ledger.jsonl")) else []):
        r = json.loads(l)
        meter.total += r.get("in_tokens", 0) + r.get("cache_creation", 0) + r.get("cache_read", 0)
        meter.calls += 1
    t0 = time.time()
    started = datetime.utcnow().isoformat() + "Z"
    os.makedirs(os.path.join(X, "out", "raw"), exist_ok=True)
    done = []
    gate = {"checked": False}

    def one(item):
        idx, sid = item
        if os.path.exists(a.stop_file) or meter.over():
            return None
        rec = run_one(idx, sid, a, meter, runner)
        p = os.path.join(X, "out", sid + ".json")
        json.dump(rec, open(p, "w"))
        if rec["raw_output_path"]:
            open(os.path.join(X, "out", rec["raw_output_path"]), "w").write(open(os.path.join(X, rec["raw_output_path"])).read())
        return rec

    stats = {}
    with ThreadPoolExecutor(a.concurrency) as ex:
        for rec in ex.map(one, items):
            if rec is None:
                continue
            stats[rec["status"]] = stats.get(rec["status"], 0) + 1
            done.append(rec)
            if a.gate and not gate["checked"] and len(done) >= 20:
                gate["checked"] = True
                okc, why = canary_check(sorted(done, key=lambda r: r["order_index"]))
                print("CANARY GATE:", why, flush=True)
                if not okc:
                    open(a.stop_file, "w").write("canary gate: " + why)
            if len(done) % 40 == 0 and a.remote:
                push(["%s.json" % r["session_id"] for r in done] + [r["raw_output_path"] for r in done if r["raw_output_path"]])
    ended = datetime.utcnow().isoformat() + "Z"
    placeholders = write_not_run(order, 0, len(order)) if a.phase in ("rest", "all") else []
    if placeholders:
        done += [{"session_id": sid, "raw_output_path": None} for sid in placeholders]
        stats["not_run"] = len(placeholders)
    prior = json.load(open(os.path.join(X, "out", "_run.json"))) if os.path.exists(os.path.join(X, "out", "_run.json")) else {"wall_seconds": 0.0, "started": started}
    runfile = {"concurrency": a.concurrency, "started": prior["started"], "ended": ended, "wall_seconds": round(prior.get("wall_seconds", 0) + time.time() - t0, 1), "calls_made": meter.calls, "cap": CAP,
               "price_table": PRICE_TABLE, "model": MODEL, "input_tokens_total": meter.total, "token_stop": TOKEN_STOP, "stopped": os.path.exists(a.stop_file) or meter.over()}
    json.dump(runfile, open(os.path.join(X, "out", "_run.json"), "w"))
    if a.phase == "rest" and not runfile["stopped"]:
        pass
    if a.remote:
        push(["%s.json" % r["session_id"] for r in done] + [r["raw_output_path"] for r in done if r["raw_output_path"]] + ["_run.json"])
    print("PHASE DONE", a.phase, stats, "calls", meter.calls, "input tokens %.2fM" % (meter.total / 1e6), flush=True)
    return stats


def run_jobs(a, runner=None):
    """One invocation following run_order.json["jobs"] exactly (arms interleaved session by session). Records go to out/<ARM>/<id>.json, raw outputs to out/<ARM>/raw/."""
    jobs = json.load(open(os.path.join(X, "run_order.json")))["jobs"]
    meter = Meter()
    t0, started = time.time(), datetime.utcnow().isoformat() + "Z"
    arms = {"S": "cap30", "P": "pref"}
    stats = {}
    done = []

    def one(job):
        arm, sid, idx = job["arm"], job["session_id"], job["order_index"]
        if os.path.exists(a.stop_file) or meter.over():
            return None
        cfg = variant_cfg(arms[arm], job.get("replicate") or None)
        cfg["arm"] = arm
        rec = run_one(idx, sid, a, meter, runner, cfg)
        rec["arm"] = arm
        os.makedirs(os.path.join(X, "out", arm, "raw"), exist_ok=True)
        json.dump(rec, open(os.path.join(X, "out", arm, sid + ".json"), "w"))
        if rec["raw_output_path"]:
            rel = "%s/raw/%s" % (arm, os.path.basename(rec["raw_output_path"]))
            open(os.path.join(X, "out", rel), "w").write(open(os.path.join(X, rec["raw_output_path"])).read())
            rec2 = dict(rec, raw_output_path=rel)
            json.dump(rec2, open(os.path.join(X, "out", arm, sid + ".json"), "w"))
        return rec

    with ThreadPoolExecutor(a.concurrency) as ex:
        for job, rec in zip(jobs, ex.map(one, jobs)):
            if rec is None:
                continue
            stats[(rec["arm"], rec["status"])] = stats.get((rec["arm"], rec["status"]), 0) + 1
            done.append(rec)
    for job in jobs:  # not_run placeholders keep the denominators complete
        p = os.path.join(X, "out", job["arm"], job["session_id"] + ".json")
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            json.dump(dict(not_run_record(job["order_index"], job["session_id"]), arm=job["arm"], replicate=job.get("replicate")), open(p, "w"))
            stats[(job["arm"], "not_run")] = stats.get((job["arm"], "not_run"), 0) + 1
    ended = datetime.utcnow().isoformat() + "Z"
    json.dump({"concurrency": a.concurrency, "started": started, "ended": ended, "wall_seconds": round(time.time() - t0, 1), "calls_made": meter.calls, "cap": CAP, "price_table": PRICE_TABLE, "model": MODEL,
               "input_tokens_total": meter.total, "token_stop": TOKEN_STOP}, open(os.path.join(X, "out", "_run.json"), "w"))
    if a.remote:
        push([os.path.relpath(os.path.join(dp, f), os.path.join(X, "out")) for dp, _, fs in os.walk(os.path.join(X, "out")) for f in fs])
    print("JOBS DONE", {"%s/%s" % k: v for k, v in sorted(stats.items())}, "calls", meter.calls, "input tokens %.2fM" % (meter.total / 1e6), flush=True)
    return stats


def parse(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["canary", "rest", "all", "jobs"], required=True)
    ap.add_argument("--variant", choices=["base", "cap30", "pref"], default="base")
    ap.add_argument("--stage", default="stage_x0", help="directory name under phasef-out and under the host in/ and out/ trees")
    ap.add_argument("--cap", type=int, default=200)
    ap.add_argument("--token-stop", type=float, default=12e6)
    ap.add_argument("--gate", action="store_true", help="X1: after the first 20 results stop unless parse rate >= 95 percent and every prompt <= 40,000 tokens")
    ap.add_argument("--replicate", default=None, help="replicate id: separate cache namespace, same prompt_version and prompt_sha16 in the records")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--remote", default="t2-dev")
    ap.add_argument("--stop-file", default=None)
    a = ap.parse_args(argv)
    configure(a)
    a.stop_file = a.stop_file or os.path.join(X, "STOP")
    return a


def configure(a):
    global X, CAP, REMOTE_IN, REPLICATE, TOKEN_STOP
    TOKEN_STOP = int(a.token_stop)
    REPLICATE = a.replicate
    X = os.path.join(R.OUT, a.stage)
    CAP = a.cap
    REMOTE_IN = "~/emlab-16096/in/%s" % a.stage
    set_variant(a.variant)


if __name__ == "__main__":
    if sys.argv[1:2] == ["fetch"]:
        fetch(*(sys.argv[2:3] or ["stage_x0"]))
    else:
        a = parse()
        run_jobs(a) if a.phase == "jobs" else run(a)
