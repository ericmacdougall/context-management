"""Proof runs for items 1-3. Thresholds are in PREREG.json / PREREG-amend-1.json (committed before these ran)."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, List, Tuple

from .canon import cj
from .controls import ArrivalKeyedIngester
from .ingester import Fenced, Ingester
from .portlog import PortLog
from .proofs_common import REPO, copy_log_prefix, event_sig, scripted_conversation, tmpdir, write_artifact
from .testkit import Rig

SEEDS = list(range(101, 121))
POINTS = {"non_stream": ["before_req_log", "after_req_log_before_send", "after_send_before_resp_log", "after_resp_log_before_return", "after_return"],
          "stream": ["before_req_log", "after_req_log_before_send", "mid_stream", "after_resp_log_before_return", "after_return"]}


def _tuples(events: List[dict]) -> List[list]:
    out = []
    for e in events:
        t = json.loads(e["content"]).get("text")
        out.append([e["call_n"], e["side"], e["prov"], e["kind"], e["status"], t])
    return sorted(out, key=lambda x: (x[0], x[1], str(x[5])))


def expected_events(point: str, k: int) -> List[list]:
    ev = []
    for i in range(1, k):
        ev += [[i, "req", "user", "text", "complete", "u%d" % i], [i, "resp", "assistant", "text", "complete", "a%d" % i]]
    if point in ("after_resp_log_before_return", "after_return"):
        ev += [[k, "req", "user", "text", "complete", "u%d" % k], [k, "resp", "assistant", "text", "complete", "a%d" % k]]
    return sorted(ev, key=lambda x: (x[0], x[1], str(x[5])))


def expected_inflight(point: str, k: int) -> List[int]:
    return [k] if point in ("after_req_log_before_send", "after_send_before_resp_log", "mid_stream") else []


def _crash_run(provider: str, stream: bool, point: str, k: int, tap: str) -> dict:
    d = tmpdir()
    try:
        cfg = {"dir": d, "provider": provider, "stream": stream, "point": point, "k": k, "n_calls": 4, "tap": tap}
        r = subprocess.run([sys.executable, "-m", "ingest.crash_worker", json.dumps(cfg)], capture_output=True, text=True, cwd=REPO,
                           env={**os.environ, "PYTHONPATH": REPO}, timeout=60)
        truth = [json.loads(l) for l in open(os.path.join(d, "truth.jsonl"))] if os.path.exists(os.path.join(d, "truth.jsonl")) else []
        log = PortLog(os.path.join(d, "port.db"))
        streams = log.streams()
        ing = Ingester(os.path.join(d, "fresh_ingester.db"), log.blob)       # a fresh process state: new store, only the log survives
        got, comp, gaps = [], {}, []
        for st in streams:
            ing.sync(log, st)
            got += ing.events(st)
            comp = ing.completeness(st)
        completed = sorted({t["n"] for t in truth if t["ev"] == "completed"})
        returned = sorted({t["n"] for t in truth if t["ev"] == "host_received"})
        have_resp = sorted({e["call_n"] for e in got if e["side"] == "resp" and e["status"] == "complete"})
        return {"provider": provider, "stream": stream, "point": point, "k": k, "tap": tap, "exit": r.returncode, "stderr": r.stderr[-200:],
                "got": _tuples(got), "expected": expected_events(point, k), "inflight_got": comp.get("in_flight_calls") or [], "log_streams": len(streams),
                "inflight_expected": expected_inflight(point, k), "stub_completed": completed, "host_received": returned,
                "ingested_complete_resp_calls": have_resp, "blocking_gaps": comp.get("blocking_gaps"), "n_events": len(got), "truth_lines": len(truth)}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def item1(out: str) -> dict:
    runs, ctl = [], []
    for provider in ("anthropic", "openai"):
        for stream in (False, True):
            for point in POINTS["stream" if stream else "non_stream"]:
                for k in (1, 3):
                    runs.append(_crash_run(provider, stream, point, k, "real"))
                    ctl.append(_crash_run(provider, stream, point, k, "late"))
    ok = [r for r in runs if r["exit"] == 137 and r["got"] == r["expected"] and r["inflight_got"] == r["inflight_expected"] and not r["blocking_gaps"]]
    # control: stub-completed turns absent from the ingested events
    def lost(r):                      # completed by the stub, absent as a complete response event
        return [n for n in r["stub_completed"] if n not in r["ingested_complete_resp_calls"]]
    ctl_lost = [r for r in ctl if lost(r)]
    ctl_returned_lost = [r for r in ctl if any(n in r["host_received"] for n in lost(r))]
    real_lost = [r for r in runs if lost(r)]
    summary = {"runs": len(runs), "pass": len(ok), "pass_rate": len(ok) / len(runs),
               "control_runs": len(ctl), "control_completed_turn_absent": len(ctl_lost), "control_returned_turn_lost": len(ctl_returned_lost),
               "real_tap_completed_turn_absent": len(real_lost),
               "real_tap_absent_points": sorted({r["point"] for r in real_lost}),
               "min_events_examined": min(r["n_events"] for r in runs), "min_truth_lines": min(r["truth_lines"] for r in runs)}
    path = write_artifact(out, "item1", {"matrix": "5 points x 4 modes x k in {1,3}", "n_calls": 4}, {"real": runs, "control": ctl}, summary)
    summary["artifact"] = path
    return summary


# --------------------------------------------------------------------------- item 1b: caught failures (not crashes)
def item1b(out: str) -> dict:
    from .stubs import text, tool_use
    cases = []
    for provider in ("anthropic", "openai"):
        for fail in ("drop:6", "error:6", "silent:6", "badjson", "reject"):
            d = tmpdir()
            try:
                if provider == "anthropic":
                    script = [{"content": [text("ok one")]}, {"content": [text("partial answer text here"), tool_use("tu_x", "lookup", {"q": "abc" * 8})],
                                                             "stop": "tool_use", "fail": fail}]
                else:
                    script = [{"message": {"role": "assistant", "content": "ok one"}},
                              {"message": {"role": "assistant", "content": "partial answer text here", "tool_calls": [
                                  {"id": "call_x", "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"q": "abc" * 8})}}]},
                               "stop": "tool_calls", "fail": fail}]
                rig = Rig(d, provider, script, stream=True)
                rig.ask("first")
                raised = None
                rig.msgs.append({"role": "user", "content": "second"})
                try:
                    rig.call(rig.request())
                except Exception as e:
                    raised = type(e).__name__
                rig.sync()
                ev = [e for e in rig.events() if e["call_n"] == 2 and e["side"] == "resp"]
                st = {e["status"] for e in ev}
                tool_calls_complete = [e for e in ev if e["kind"] == "tool_call" and e["status"] == "complete"]
                cases.append({"provider": provider, "fail": fail, "raised": raised, "resp_statuses": sorted(st), "n_resp_events": len(ev),
                              "complete_resp_events": len([e for e in ev if e["status"] == "complete"]),
                              "tool_call_complete": len(tool_calls_complete), "kinds": sorted({e["kind"] for e in ev}),
                              "call2_unconfirmed_or_rejected": [e["kind"] for e in rig.events(include_port=True) if e["prov"] == "port" and e["call_n"] == 2]})
            finally:
                shutil.rmtree(d, ignore_errors=True)
    def good(c):
        if c["fail"] == "reject":
            return c["n_resp_events"] == 0 and "call_rejected" in c["call2_unconfirmed_or_rejected"]
        return c["complete_resp_events"] == 0 and c["tool_call_complete"] == 0 and (c["n_resp_events"] > 0 or c["fail"] == "silent:6")
    summary = {"cases": len(cases), "pass": len([c for c in cases if good(c)]), "failures": [c for c in cases if not good(c)][:3]}
    summary["artifact"] = write_artifact(out, "item1b", {"fails": "drop,error,silent,badjson,reject", "providers": 2}, cases, summary)
    return summary


# --------------------------------------------------------------------------- item 2
def _baseline(tmp: str, seed: int) -> Tuple[Rig, List[dict], List[list]]:
    rig = scripted_conversation(Rig, tmp, 30, seed=seed)
    recs = rig.log.read(rig.stream_id)
    base = Ingester(os.path.join(tmp, "base.db"), rig.log.blob)
    base.deliver(recs)
    base.drain(rig.stream_id)
    return rig, recs, event_sig(base.events(rig.stream_id))


def _plan(recs: List[dict], rnd: random.Random) -> List[List[dict]]:
    deliveries = []
    for r in recs:
        deliveries += [r] * rnd.randint(1, 3)
    rnd.shuffle(deliveries)
    batches, i = [], 0
    while i < len(deliveries):
        n = rnd.randint(1, 7)
        batches.append(deliveries[i:i + n])
        i += n
    return batches


def item2_delivery(out: str) -> dict:
    runs = []
    for seed in SEEDS:
        tmp = tmpdir()
        try:
            rig, recs, base_sig = _baseline(tmp, seed)
            rnd = random.Random(seed)
            batches = _plan(recs, rnd)
            ing = Ingester(os.path.join(tmp, "ing.db"), rig.log.blob)
            arr = ArrivalKeyedIngester(rig.log.blob)
            opened_gaps = 0
            for b in batches:
                ing.deliver(b)
                ing.drain(rig.stream_id)
                opened_gaps = max(opened_gaps, len(ing.gaps(rig.stream_id, open_only=False)))
                arr.deliver(b)
            sig = event_sig(ing.events(rig.stream_id))
            ikeys = [s[0] for s in sig]
            base_blocks = [s[1] for s in base_sig]
            arr_content_order = [r["seq"] for r in arr.rows]
            arr_violation = (len(arr.rows) != len(base_sig)) or arr_content_order != sorted(arr_content_order)
            runs.append({"seed": seed, "n_records": len(recs), "deliveries": sum(len(b) for b in batches), "batches": len(batches),
                         "baseline_sig": base_sig, "got_sig": sig, "dup_ikeys": len(ikeys) - len(set(ikeys)),
                         "watermark": ing.watermark(rig.stream_id)[0], "log_head": rig.log.head(rig.stream_id)[0],
                         "transient_gaps_seen": opened_gaps, "open_gaps_end": len(ing.gaps(rig.stream_id)),
                         "dup_suppressed": ing.stats["dup_event_suppressed"],
                         "control_rows": len(arr.rows), "control_violation": arr_violation})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    ok = [r for r in runs if r["got_sig"] == r["baseline_sig"] and r["dup_ikeys"] == 0 and r["watermark"] == r["log_head"] and r["open_gaps_end"] == 0]
    summary = {"seeds": len(runs), "pass": len(ok), "control_violations": sum(r["control_violation"] for r in runs),
               "min_records": min(r["n_records"] for r in runs), "min_deliveries": min(r["deliveries"] for r in runs),
               "seeds_with_transient_gap": sum(1 for r in runs if r["transient_gaps_seen"] > 0)}
    summary["artifact"] = write_artifact(out, "item2a", {"seeds": SEEDS, "calls": 30, "copies": "1-3", "batch": "1-7"}, runs, summary)
    return summary


def _thread_ingester(path, blob_path, name, stream, barrier, rounds, result, fence=True, unique=True, chunk_q=None, state_guard=True):
    log = PortLog(blob_path)
    times = []

    def after_snapshot(n, s, token, runlen):
        if runlen:
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
    ing = Ingester(path, log.blob, name=name, hooks={"after_snapshot": after_snapshot}, fence=fence, unique=unique, state_guard=state_guard)
    for r in range(rounds):
        chunk_q[r].wait()                     # main has delivered this chunk
        ing.drain(stream)
        times.append(getattr(ing, "last_overlap", None))
        barrier_after.wait()
    result[name] = {"stats": ing.stats, "intervals": times}


def item2_race_threads(out: str, fence=True, unique=True, tag="item2b", state_guard=True) -> dict:
    global barrier_after
    runs = []
    seeds = SEEDS[:10]
    for seed in seeds:
        tmp = tmpdir()
        try:
            rig, recs, base_sig = _baseline(tmp, seed)
            stream = rig.stream_id
            store = os.path.join(tmp, "shared.db")
            seed_ing = Ingester(store, rig.log.blob, name="seed", fence=fence, unique=unique)
            chunks = [recs[i:i + 6] for i in range(0, len(recs), 6)]
            rounds = len(chunks)
            barrier = threading.Barrier(2)
            barrier_after = threading.Barrier(3)
            ready = [threading.Event() for _ in range(rounds)]
            result: Dict[str, dict] = {}
            ts = [threading.Thread(target=_thread_ingester, args=(store, rig.logp, n, stream, barrier, rounds, result, fence, unique, ready, state_guard))
                  for n in ("A", "B")]
            for t in ts:
                t.start()
            for r in range(rounds):
                seed_ing.deliver(chunks[r])
                ready[r].set()
                barrier_after.wait(timeout=30)
            for t in ts:
                t.join()
            chk = Ingester(store, rig.log.blob, name="chk", fence=fence, unique=unique)
            sig = event_sig(chk.events(stream))
            ikeys = [s[0] if unique else s[0].split("|")[0:5].__str__() for s in sig]
            base_keys = [s[0] for s in base_sig]
            iv = []
            for r in range(rounds):
                a, b = result["A"]["intervals"][r], result["B"]["intervals"][r]
                if a and b:
                    iv.append(1 if (a[0] < b[2] and b[0] < a[2]) else 0)      # [lock request, commit] intervals intersect
            fenced = result["A"]["stats"]["fenced"] + result["B"]["stats"]["fenced"]
            norm = [s[0].split("|")[:5] for s in sig]
            dup = len(norm) - len({tuple(x) for x in norm})
            runs.append({"seed": seed, "rounds": rounds, "fenced": fenced, "overlapping_rounds": sum(iv), "intervals_measured": len(iv),
                         "dup_rows": dup, "n_events": len(sig), "n_baseline": len(base_sig),
                         "identical_to_baseline": (sig == base_sig), "watermark": chk.watermark(stream)[0], "log_head": rig.log.head(stream)[0],
                         "applied_by_A": result["A"]["stats"]["records_applied"], "applied_by_B": result["B"]["stats"]["records_applied"]})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    ok = [r for r in runs if r["identical_to_baseline"] and r["dup_rows"] == 0 and r["watermark"] == r["log_head"]]
    summary = {"runs": len(runs), "identical_to_baseline": len(ok), "runs_with_fencing": sum(1 for r in runs if r["fenced"] >= 1),
               "total_fenced": sum(r["fenced"] for r in runs), "runs_with_measured_overlap": sum(1 for r in runs if r["overlapping_rounds"] >= 1),
               "runs_with_duplicates": sum(1 for r in runs if r["dup_rows"] > 0)}
    summary["artifact"] = write_artifact(out, tag, {"seeds": seeds, "threads": 2, "fence": fence, "unique": unique, "state_guard": state_guard}, runs, summary)
    return summary


def _proc_ingester(args):
    store, logp, name, stream, chunk_files, sync_dir, rounds = args
    log = PortLog(logp)
    iv = []

    def after_snapshot(n, s, token, runlen):
        if runlen:
            open(os.path.join(sync_dir, "%s.snap.%d" % (name, token)), "w").close()
            other = "B" if name == "A" else "A"
            t0 = time.time()
            while time.time() - t0 < 5 and not any(f.startswith(other + ".snap.%d" % token) for f in os.listdir(sync_dir)):
                time.sleep(0.001)
    ing = Ingester(store, log.blob, name=name, hooks={"after_snapshot": after_snapshot})
    for r in range(rounds):
        while not os.path.exists(os.path.join(sync_dir, "go.%d" % r)):
            time.sleep(0.001)
        ing.drain(stream)
        iv.append(getattr(ing, "last_overlap", None))
        open(os.path.join(sync_dir, "%s.done.%d" % (name, r)), "w").close()
    return {"name": name, "stats": ing.stats, "iv": iv}


def item2_race_procs(out: str) -> dict:
    runs = []
    seeds = SEEDS[:5]
    ctx = mp.get_context("spawn")
    for seed in seeds:
        tmp = tmpdir()
        try:
            rig, recs, base_sig = _baseline(tmp, seed)
            stream = rig.stream_id
            store = os.path.join(tmp, "shared.db")
            seed_ing = Ingester(store, rig.log.blob, name="seed")
            sync_dir = os.path.join(tmp, "sync")
            os.makedirs(sync_dir)
            chunks = [recs[i:i + 6] for i in range(0, len(recs), 6)]
            pool = ctx.Pool(2)
            res = [pool.apply_async(_proc_ingester, ((store, rig.logp, n, stream, None, sync_dir, len(chunks)),)) for n in ("A", "B")]
            for r, ch in enumerate(chunks):
                seed_ing.deliver(ch)
                open(os.path.join(sync_dir, "go.%d" % r), "w").close()
                t0 = time.time()
                while time.time() - t0 < 30 and not all(os.path.exists(os.path.join(sync_dir, "%s.done.%d" % (n, r))) for n in ("A", "B")):
                    time.sleep(0.001)
            out_ = [x.get(timeout=30) for x in res]
            pool.close()
            pool.join()
            chk = Ingester(store, rig.log.blob, name="chk")
            sig = event_sig(chk.events(stream))
            a, b = out_
            overlap = 0
            for ia, ib in zip(a["iv"], b["iv"]):
                if ia and ib and ia[0] < ib[2] and ib[0] < ia[2]:
                    overlap += 1
            runs.append({"seed": seed, "fenced": a["stats"]["fenced"] + b["stats"]["fenced"], "overlapping_rounds": overlap,
                         "identical_to_baseline": sig == base_sig, "n_events": len(sig), "pids": 2,
                         "applied_by_A": a["stats"]["records_applied"], "applied_by_B": b["stats"]["records_applied"]})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    summary = {"runs": len(runs), "identical_to_baseline": sum(r["identical_to_baseline"] for r in runs),
               "runs_with_fencing": sum(1 for r in runs if r["fenced"] >= 1), "runs_with_measured_overlap": sum(1 for r in runs if r["overlapping_rounds"] >= 1)}
    summary["artifact"] = write_artifact(out, "item2c", {"seeds": seeds, "processes": 2}, runs, summary)
    return summary


def item2_zombie(out: str) -> dict:
    runs = []
    for seed in SEEDS[:10]:
        tmp = tmpdir()
        try:
            rig, recs, base_sig = _baseline(tmp, seed)
            stream = rig.stream_id
            store = os.path.join(tmp, "shared.db")
            Ingester(store, rig.log.blob, name="seed").deliver(recs)
            paused, resume = threading.Event(), threading.Event()
            res = {}

            def zombie():
                log = PortLog(rig.logp)
                z = Ingester(store, log.blob, name="Z", hooks={"after_snapshot": lambda *a: (paused.set(), resume.wait(10))})
                try:
                    z.drain(stream)
                except Exception as e:
                    res["err"] = type(e).__name__
                res["stats"] = z.stats
            t = threading.Thread(target=zombie)
            t.start()
            paused.wait(10)
            b = Ingester(store, rig.log.blob, name="B")
            wm_before = b.watermark(stream)[0]
            b.drain(stream)
            n_after_b = len(b.events(stream))
            resume.set()
            t.join()
            n_final = len(b.events(stream))
            runs.append({"seed": seed, "z_fenced": res["stats"]["fenced"], "z_records_applied": res["stats"]["records_applied"],
                         "events_after_B": n_after_b, "events_final": n_final, "baseline_events": len(base_sig), "wm_before_B": wm_before,
                         "identical_to_baseline": event_sig(b.events(stream)) == base_sig})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    ok = [r for r in runs if r["z_fenced"] >= 1 and r["z_records_applied"] == 0 and r["events_final"] == r["events_after_B"] and r["identical_to_baseline"]]
    summary = {"runs": len(runs), "zombie_rejected_no_writes": len(ok)}
    summary["artifact"] = write_artifact(out, "item2d", {"seeds": SEEDS[:10]}, runs, summary)
    return summary


# --------------------------------------------------------------------------- item 3
class WatermarkFreeReporter:
    """Control: 'complete up to the highest seq seen'."""

    def __init__(self):
        self.max_seen = 0

    def deliver(self, recs):
        for r in recs:
            self.max_seen = max(self.max_seen, r["seq"])

    def complete_up_to(self):
        return self.max_seen


def item3(out: str) -> dict:
    runs = []
    for seed in SEEDS:
        rnd = random.Random(seed)
        tmp = tmpdir()
        try:
            wit = {}
            rig = Rig(tmp, "anthropic", [{"content": [__import__("ingest.stubs", fromlist=["text"]).text("a%d" % i)]} for i in range(20)],
                      tap_kw={"witness": lambda s, q, h: wit.__setitem__(s, (q, h))})
            for i in range(20):
                rig.ask("q%d" % i)
            stream = rig.stream_id
            recs = rig.log.read(stream)
            head = rig.log.head(stream)
            base = Ingester(os.path.join(tmp, "base.db"), rig.log.blob)
            base.deliver(recs)
            base.drain(stream)
            base_sig = event_sig(base.events(stream))
            row = {"seed": seed, "head": head[0]}
            # (a) dropped middle record
            miss = rnd.randint(3, head[0] - 2)
            ia = Ingester(os.path.join(tmp, "a.db"), rig.log.blob)
            others = [r for r in recs if r["seq"] != miss]
            rnd.shuffle(others)
            ctl = WatermarkFreeReporter()
            ia.deliver(others)
            ia.drain(stream)
            ctl.deliver(others)
            comp = ia.completeness(stream)
            row["a"] = {"dropped": miss, "gaps": [(g["kind"], g["from_seq"], g["to_seq"]) for g in ia.gaps(stream)],
                        "claim_head": ia.claim_complete(stream, head[0]), "watermark": ia.watermark(stream)[0], "control_complete_up_to": ctl.complete_up_to()}
            # (d) late arrival closes it
            ia.deliver([r for r in recs if r["seq"] == miss])
            ia.drain(stream)
            row["d"] = {"open_gaps": len(ia.gaps(stream)), "closed_gaps": len([g for g in ia.gaps(stream, open_only=False) if g["closed"]]),
                        "claim_head": ia.claim_complete(stream, head[0]), "identical_to_baseline": event_sig(ia.events(stream)) == base_sig}
            # (b) truncated tail, witness shipped out of band
            m = rnd.randint(4, head[0] - 3)
            tpath = os.path.join(tmp, "trunc.db")
            copy_log_prefix(rig.logp, tpath, m)
            tl = PortLog(tpath)
            ib = Ingester(os.path.join(tmp, "b.db"), tl.blob)
            ib.set_witness(stream, *wit[stream])
            ib.sync(tl, stream)
            ib.check_witness(stream, tl.head(stream), lambda s: (tl.read(stream, s - 1, 1) or [{"hash": None}])[0]["hash"])
            row["b"] = {"cut_at": m, "gaps": [(g["kind"], g["from_seq"], g["to_seq"]) for g in ib.gaps(stream)], "claim_head": ib.claim_complete(stream, head[0]),
                        "claim_cut": ib.claim_complete(stream, m)}
            # (b2) log restored: gap closes
            ib.sync(rig.log, stream)
            row["b2"] = {"open_gaps": len(ib.gaps(stream)), "claim_head": ib.claim_complete(stream, head[0])}
            # (e) truncation without any witness
            ie = Ingester(os.path.join(tmp, "e.db"), tl.blob)
            ie.sync(tl, stream)
            row["e"] = {"gaps": len(ie.gaps(stream)), "claim_cut": ie.claim_complete(stream, m), "claim_head_real": ie.claim_complete(stream, head[0])}
            # (c) host bypasses the port for one call; its answer re-enters history
            d2 = os.path.join(tmp, "c")
            os.makedirs(d2)
            from ingest.stubs import text as T
            rc = Rig(d2, "anthropic", [{"content": [T("a%d" % i)]} for i in range(12)])
            byp = rnd.randint(2, 8)
            for i in range(10):
                if i == byp:
                    rc.msgs.append({"role": "user", "content": "q%d" % i})
                    o = rc.stub.call(rc.request())                  # direct transport call: bypasses the tap
                    rc.msgs.append({"role": "assistant", "content": o["content"]})
                else:
                    rc.ask("q%d" % i)
            rc.sync()
            row["c"] = {"bypass_at": byp, "gaps": [(g["kind"], g["detail"]) for g in rc.ing.gaps(rc.stream_id)], "claim_head": rc.ing.claim_complete(rc.stream_id, rc.log.head(rc.stream_id)[0])}
            # (f) bypass whose answer never re-enters the context (final call, answer discarded)
            d3 = os.path.join(tmp, "f")
            os.makedirs(d3)
            rf = Rig(d3, "anthropic", [{"content": [T("a%d" % i)]} for i in range(12)])
            for i in range(6):
                rf.ask("q%d" % i)
            rf.stub.call({"model": "m", "messages": [{"role": "user", "content": "secret side call"}]})     # never logged, never seen again
            rf.sync()
            row["f"] = {"gaps": len(rf.ing.gaps(rf.stream_id)), "claim_head": rf.ing.claim_complete(rf.stream_id, rf.log.head(rf.stream_id)[0])}
            # (g) a record's body altered in transit
            ig = Ingester(os.path.join(tmp, "g.db"), rig.log.blob)
            tam = [dict(r) for r in recs]
            tm = rnd.randint(3, head[0] - 2)
            tam[tm - 1]["body"] = tam[tm - 1]["body"].replace("q", "Q", 1)
            ig.deliver(tam)
            ig.drain(stream)
            row["g"] = {"tampered": tm, "gaps": [g["kind"] for g in ig.gaps(stream)], "claim_head": ig.claim_complete(stream, head[0])}
            runs.append(row)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    def a_ok(r): return [g[0] for g in r["a"]["gaps"]] == ["seq_hole"] and not r["a"]["claim_head"] and r["a"]["watermark"] == r["a"]["dropped"] - 1
    summary = {"seeds": len(runs),
               "a_dropped_detected": sum(a_ok(r) for r in runs),
               "a_control_reports_complete": sum(r["a"]["control_complete_up_to"] == r["head"] for r in runs),
               "d_late_closes": sum(r["d"]["open_gaps"] == 0 and r["d"]["closed_gaps"] >= 1 and r["d"]["claim_head"] and r["d"]["identical_to_baseline"] for r in runs),
               "b_truncation_detected_with_witness": sum(any(g[0] == "tail_truncated" for g in r["b"]["gaps"]) and not r["b"]["claim_head"] for r in runs),
               "b2_restored_closes": sum(r["b2"]["open_gaps"] == 0 and r["b2"]["claim_head"] for r in runs),
               "c_bypass_detected": sum(any(g[0] == "unlogged_assistant_message" for g in r["c"]["gaps"]) and not r["c"]["claim_head"] for r in runs),
               "e_truncation_no_witness_detected": sum(r["e"]["gaps"] > 0 for r in runs),
               "e_false_complete_claims": sum(r["e"]["claim_cut"] for r in runs),
               "f_hidden_bypass_detected": sum(r["f"]["gaps"] > 0 for r in runs),
               "g_tamper_detected": sum("chain_break" in r["g"]["gaps"] and not r["g"]["claim_head"] for r in runs)}
    summary["artifact"] = write_artifact(out, "item3", {"seeds": SEEDS, "calls": 20}, runs, summary)
    return summary
