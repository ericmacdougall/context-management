"""Proof runs for PREREG-amend-2 (A2 sliding/compacting hosts, A3 states, A4 seed/backfill, A5 block storage, A6 stream contract,
A7 flat cost). Thresholds are in PREREG-amend-2.json."""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from typing import Dict, List

from memcore.guard import EgressGuard

from .canon import Hasher
from .ingester import Ingester
from .portlog import PortLog
from .proofs_common import REPO, asst_msg, spec_text, tmpdir, write_artifact
from .scan import ReferenceScanner
from .stubs import Stub, anth_msg, oai_resp, text
from .tap import CrashPlan, PortTap, SimulatedCrash, StaticIdentity
from .testkit import KEY, Rig, make_injector

SEEDS = list(range(101, 121))


def _boom():
    raise SimulatedCrash()


# ------------------------------------------------------------------ A2
def item11(out: str) -> dict:
    runs = []
    for provider in ("anthropic", "openai"):
        for kind, window in (("window4", 4), ("window8", 8), ("compacting", 4)):
            for n in (20, 60):
                d = tmpdir()
                try:
                    rig = Rig(d, provider, [spec_text(provider, "a%d" % i) for i in range(n)])
                    if kind == "compacting":
                        rig.call_kw = {"new_count": 1}       # host-authored summary: representable only when the host declares its boundary
                    for i in range(n):
                        rig.msgs.append({"role": "user", "content": "q%d" % i})
                        o = rig.call(rig.request())
                        rig.msgs.append(asst_msg(provider, o))
                        if kind == "compacting":
                            if len(rig.msgs) > window:
                                rig.msgs[:] = [{"role": "user", "content": "SUMMARY-%d of earlier chat" % i}] + rig.msgs[-window:]
                        else:
                            while len(rig.msgs) > window:
                                rig.msgs.pop(0)
                                rig.msgs.pop(0)
                    rig.sync()
                    ev = rig.events()
                    tx = lambda e: json.loads(e["content"]).get("text") or json.loads(e["content"]).get("content")
                    users = [tx(e) for e in ev if e["prov"] == "user" and "SUMMARY" not in e["content"]]
                    assts = [tx(e) for e in ev if e["prov"] == "assistant"]
                    summ = [e for e in ev if "SUMMARY-" in e["content"]]
                    runs.append({"provider": provider, "host": kind, "calls": n, "user_events": len(users), "distinct_user": len(set(users)),
                                 "assistant_events": len(assts), "distinct_assistant": len(set(assts)),
                                 "summary_events": len(summ), "summary_provs": sorted({e["prov"] for e in summ}),
                                 "blocking_gaps": [g["kind"] for g in rig.ing.gaps(rig.stream_id) if g["blocking"]],
                                 "state": rig.ing.completeness(rig.stream_id)["state"],
                                 "ambiguous_calls": rig.ing.completeness(rig.stream_id)["ambiguous_calls"]})
                finally:
                    shutil.rmtree(d, ignore_errors=True)
    ok = [r for r in runs if r["user_events"] == r["calls"] == r["distinct_user"] and r["assistant_events"] == r["calls"] == r["distinct_assistant"]
          and not r["blocking_gaps"] and r["summary_provs"] in ([], ["host"]) and (r["host"] != "compacting" or r["summary_events"] > 0)]
    S = {"runs": len(runs), "pass": len(ok), "failing_first_baseline_aa30130": {"window4_user_events_for_20": 54, "window8_user_events_for_20": 80},
         "runs_with_ambiguity_flag": sum(1 for r in runs if r["ambiguous_calls"])}
    S["artifact"] = write_artifact(out, "item11", {"hosts": ["window4", "window8", "compacting"], "calls": [20, 60], "providers": 2}, runs, S)
    return S


# ------------------------------------------------------------------ A3 / A4 / A5
def item10(out: str) -> dict:
    R: Dict[str, list] = {"states": [], "recover": [], "seed": [], "backfill": [], "storage": []}
    for provider in ("anthropic", "openai"):
        for mode in ("nonstream", "stream"):
            d = tmpdir()
            try:
                point = "after_send_before_resp_log" if mode == "nonstream" else "mid_stream"
                rig = Rig(d, provider, [spec_text(provider, "a1")], stream=(mode == "stream"), tap_kw={"crash": CrashPlan(point, _boom, at=2 if mode == "stream" else None)})
                rig.msgs.append({"role": "user", "content": "q1"})
                try:
                    rig.call(rig.request())
                except SimulatedCrash:
                    pass
                rig.sync()
                c = rig.ing.completeness(rig.stream_id)
                R["states"].append({"provider": provider, "mode": mode, "state": c["state"], "unconfirmed": c["unconfirmed_calls"],
                                    "claim_complete": rig.ing.claim_complete(rig.stream_id, c["watermark"]),
                                    "claim_except": list(rig.ing.claim_complete_except(rig.stream_id, c["watermark"]))})
            finally:
                shutil.rmtree(d, ignore_errors=True)
    for seed in SEEDS[:10]:
        provider = "anthropic" if seed % 2 else "openai"
        d = tmpdir()
        try:
            rig = Rig(d, provider, [spec_text(provider, "a1")], tap_kw={"crash": CrashPlan("after_send_before_resp_log", _boom)})
            rig.msgs.append({"role": "user", "content": "q1"})
            try:
                rig.call(rig.request())
            except SimulatedCrash:
                pass
            resp = (lambda *_: anth_msg([text("recovered")], "end_turn", "msg_rec")) if provider == "anthropic" else (lambda *_: oai_resp({"role": "assistant", "content": "recovered"}, "stop", "chatcmpl-rec"))
            k = rig.tap.recover_unconfirmed(resp)
            rig.sync()
            R["recover"].append({"seed": seed, "recovered": k, "state": rig.ing.completeness(rig.stream_id)["state"], "provs": [e["prov"] for e in rig.events()]})
        finally:
            shutil.rmtree(d, ignore_errors=True)
    import random
    for seed in SEEDS:
        rnd = random.Random(seed)
        provider = "anthropic" if seed % 2 else "openai"
        d = tmpdir()
        try:
            nh = rnd.randint(1, 5)
            hist = []
            for i in range(nh):
                hist.append({"role": "user", "content": "old q%d" % i})
                hist.append({"role": "assistant", "content": [text("old a%d" % i)]} if provider == "anthropic" else {"role": "assistant", "content": "old a%d" % i})
            row = {"seed": seed, "history_msgs": len(hist)}
            for variant in ("good", "bad"):
                dd = os.path.join(d, variant)
                os.makedirs(dd)
                rig = Rig(dd, provider, [spec_text(provider, "a1")])
                rig.ing = Ingester(os.path.join(dd, "events.db"), rig.log.blob, seed_verifier=Hasher(KEY).h)
                if variant == "bad":
                    rig.tap.hasher = Hasher(b"other key")
                rig.tap.seed(hist)
                rig.tap.hasher = rig.hasher
                rig.msgs = list(hist) + [{"role": "user", "content": "new q"}]
                rig.call(rig.request())
                rig.sync()
                ev = [e for e in rig.events() if e["prov"] in ("user", "assistant")]
                row[variant] = {"gap_kinds": sorted({g["kind"] for g in rig.ing.gaps(rig.stream_id)}), "chat_events": len(ev),
                                "old_history_events": sum(1 for e in ev if "old " in e["content"]), "state": rig.ing.completeness(rig.stream_id)["state"]}
            R["seed"].append(row)
            row = {"seed": seed}
            for variant, key in (("authorised", b"bk"), ("unauthorised", b"wrong")):
                dd = os.path.join(d, "bf_" + variant)
                os.makedirs(dd)
                rig = Rig(dd, provider, [spec_text(provider, "a%d" % i) for i in range(6)])
                rig.ing = Ingester(os.path.join(dd, "events.db"), rig.log.blob, backfill_verifier=Hasher(b"bk").h)
                rig.ask("q0")
                rig.msgs.append({"role": "user", "content": "q1"})
                o = rig.stub.call(rig.request())
                rig.msgs.append(asst_msg(provider, o))
                rig.ask("q2")
                rig.sync()
                gk = [g for g in rig.ing.gaps(rig.stream_id) if g["kind"] == "unlogged_assistant_message"]
                gkey = json.loads(gk[0]["detail"])["key"]
                rig.tap.backfill("unlogged_assistant_message", gkey, "ops:alice", Hasher(key))
                rig.sync()
                closed = [g for g in rig.ing.gaps(rig.stream_id, open_only=False) if g["closed"]]
                row[variant] = {"detected": len(gk), "open_blocking_after": len([g for g in rig.ing.gaps(rig.stream_id) if g["blocking"]]),
                                "closed_by": [g["closed_by"] for g in closed]}
            R["backfill"].append(row)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    from memcore.ports import Turn
    for seed in SEEDS:
        provider = "anthropic" if seed % 2 else "openai"
        d = tmpdir()
        try:
            inj = make_injector(("Dana likes tea PLANT%d" % seed,))
            rig = Rig(d, provider, [spec_text(provider, "ok")] * 3)
            for turn in range(3):
                i = inj.build(Turn(session_id="sess1", user_text="Dana tea", time="2026-01-01T00:00:00Z"))
                rig.tap.register_issued(i)
                rig.msgs.append({"role": "user", "content": i.block + "\nDana tea %d" % turn})
                o = rig.call(rig.request())
                rig.msgs.append(asst_msg(provider, o))
                if turn == 1:
                    rig.tap = PortTap(rig.stub, provider, rig.log, rig.identity, rig.hasher, ReferenceScanner())     # restart
            rig.sync()
            tables = sorted(r[0] for r in rig.log.conn().execute("SELECT name FROM sqlite_master WHERE type='table'"))
            ev = rig.events()
            blob_has = rig.log.conn().execute("SELECT COUNT(*) FROM blobs WHERE body LIKE ?", ("%%PLANT%d%%" % seed,)).fetchone()[0]
            log_has = rig.log.conn().execute("SELECT COUNT(*) FROM port_log WHERE body LIKE ?", ("%%PLANT%d%%" % seed,)).fetchone()[0]
            try:
                rig.log.conn().execute("UPDATE blobs SET body='x'")
                upd = "allowed"
            except Exception:
                upd = "refused"
            R["storage"].append({"seed": seed, "provider": provider, "tables": tables, "host_events": len([e for e in ev if e["prov"] == "host"]),
                                 "gaps": len(rig.ing.gaps(rig.stream_id)), "events_with_block_text": sum(1 for e in ev if "PLANT" in e["content"]),
                                 "block_text_in_blobs": blob_has, "block_text_in_chained_records": log_has, "blob_update": upd})
        finally:
            shutil.rmtree(d, ignore_errors=True)
    S = {
        "states_ok": sum(1 for r in R["states"] if r["state"] == "complete_except_unconfirmed" and r["unconfirmed"] == [1] and not r["claim_complete"] and r["claim_except"][0]),
        "states_runs": len(R["states"]),
        "recover_ok": sum(1 for r in R["recover"] if r["recovered"] == 1 and r["state"] == "complete" and r["provs"] == ["user", "assistant"]), "recover_runs": len(R["recover"]),
        "seed_good_ok": sum(1 for r in R["seed"] if r["good"]["gap_kinds"] == [] and r["good"]["old_history_events"] == 0 and r["good"]["chat_events"] == 2 and r["good"]["state"] == "complete"),
        "seed_bad_gap": sum(1 for r in R["seed"] if "seed_invalid" in r["bad"]["gap_kinds"] and r["bad"]["state"] == "incomplete"), "seed_runs": len(R["seed"]),
        "backfill_authorised_closes": sum(1 for r in R["backfill"] if r["authorised"]["detected"] == 1 and r["authorised"]["open_blocking_after"] == 0 and r["authorised"]["closed_by"] == ["backfill:ops:alice"]),
        "backfill_unauthorised_closes": sum(1 for r in R["backfill"] if r["unauthorised"]["open_blocking_after"] == 0), "backfill_runs": len(R["backfill"]),
        "storage_ok": sum(1 for r in R["storage"] if "issued" not in r["tables"] and r["host_events"] == 0 and r["gaps"] == 0 and r["events_with_block_text"] == 0 and r["blob_update"] == "refused"),
        "storage_runs": len(R["storage"]),
        "block_text_in_blobs_runs": sum(1 for r in R["storage"] if r["block_text_in_blobs"] > 0), "block_text_in_chained_record_bodies": sum(r["block_text_in_chained_records"] for r in R["storage"])}
    S["artifact"] = write_artifact(out, "item10", {"seeds": SEEDS}, R, S)
    return S


# ------------------------------------------------------------------ A6
def item8(out: str) -> dict:
    runs = []
    for provider in ("anthropic", "openai"):
        for delivery in ("live", "buffered"):
            for at in (3, 6):
                for rep in (1, 2):
                    d = tmpdir()
                    try:
                        cfg = {"mode": "stream_contract", "dir": d, "provider": provider, "delivery": delivery, "at": at}
                        r = subprocess.run([sys.executable, "-m", "ingest.crash_worker", json.dumps(cfg)], capture_output=True, text=True, cwd=REPO,
                                           env={**os.environ, "PYTHONPATH": REPO}, timeout=60)
                        truth = [json.loads(l) for l in open(os.path.join(d, "truth.jsonl"))]
                        host = len([t for t in truth if t["ev"] == "host_event"])
                        log = PortLog(os.path.join(d, "port.db"))
                        ing = Ingester(os.path.join(d, "f.db"), log.blob)
                        resp_events = 0
                        for st in log.streams():
                            ing.sync(log, st)
                            resp_events += len([e for e in ing.events(st) if e["side"] == "resp"])
                        runs.append({"provider": provider, "delivery": delivery, "crash_at_event": at, "rep": rep, "exit": r.returncode, "host_events_received": host,
                                     "response_events_in_memory": resp_events})
                    finally:
                        shutil.rmtree(d, ignore_errors=True)
    live = [r for r in runs if r["delivery"] == "live"]
    buf = [r for r in runs if r["delivery"] == "buffered"]
    S = {"runs": len(runs), "live_host_saw_deltas_memory_has_none": sum(1 for r in live if r["exit"] == 137 and r["host_events_received"] >= 1 and r["response_events_in_memory"] == 0), "live_runs": len(live),
         "buffered_host_saw_nothing": sum(1 for r in buf if r["exit"] == 137 and r["host_events_received"] == 0 and r["response_events_in_memory"] == 0), "buffered_runs": len(buf)}
    # latency: tap off vs live vs buffered, 20 events x 2 ms
    lat = {}
    for label, delivery in (("tap_off", None), ("live", "live"), ("buffered", "buffered")):
        d = tmpdir()
        try:
            stub = Stub("anthropic", [spec_text("anthropic", "x" * 114)] * 40, event_delay_s=0.002)
            tap = PortTap(stub, "anthropic", PortLog(os.path.join(d, "p.db")), StaticIdentity("t", "s"), Hasher(KEY), ReferenceScanner(), stream_delivery=delivery or "live")
            req = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
            first, total = [], []
            for _ in range(30):
                t0 = time.perf_counter()
                it = stub.stream(req) if delivery is None else tap.call(req, stream=True)
                f = None
                for _ev in it:
                    if f is None:
                        f = time.perf_counter() - t0
                total.append(time.perf_counter() - t0)
                first.append(f)
            lat[label] = {"first_event_ms_median": 1000 * statistics.median(first), "total_ms_median": 1000 * statistics.median(total)}
        finally:
            shutil.rmtree(d, ignore_errors=True)
    S["latency_indicative"] = lat
    S["artifact"] = write_artifact(out, "item8", {"crash_at_events": [3, 6], "providers": 2}, {"crash": runs, "latency": lat}, S)
    return S


# ------------------------------------------------------------------ A7
def item9(out: str) -> dict:
    """Flat per-call cost to 200 calls. 'persistent' host keeps its message objects (the tap uses an identity fast path for messages
    older than the last 8, re-verifying the last 8 by content); 'rebuilt' host constructs new dicts every call (reported, not passed)."""
    res, raw = {}, {}
    for provider in ("anthropic", "openai"):
        for mode in ("persistent", "rebuilt"):
            d = tmpdir()
            try:
                n = 200
                stub = Stub(provider, [spec_text(provider, "answer " * 10)] * n, record=False)
                log = PortLog(os.path.join(d, "p.db"))
                tap = PortTap(stub, provider, log, StaticIdentity("t", "s"), Hasher(KEY), ReferenceScanner(), guard=EgressGuard(real_ids=["conv-zzz-1"]),
                              injector=make_injector(("Dana likes tea",)), clock=lambda: "2026-01-01T00:00:00Z")
                msgs: List[dict] = []
                t_on = []
                for i in range(n):
                    msgs.append({"role": "user", "content": "tell me about Dana tea number %d" % i})
                    req = {"model": "m", "max_tokens": 64, "messages": list(msgs) if mode == "persistent" else [dict(m) for m in msgs]}
                    t0 = time.perf_counter()
                    o = tap.call(req)
                    t_on.append(time.perf_counter() - t0)
                    msgs.append(asst_msg(provider, o))
                m = statistics.median
                xs = list(range(n))
                mx, my = sum(xs) / n, sum(t_on) / n
                slope = sum((x - mx) * (y - my) for x, y in zip(xs, t_on)) / sum((x - mx) ** 2 for x in xs)
                key = provider + "_" + mode
                res[key] = {"median_ms_calls_11_30": 1000 * m(t_on[10:30]), "median_ms_calls_91_110": 1000 * m(t_on[90:110]), "median_ms_calls_181_200": 1000 * m(t_on[180:]),
                            "ratio_181_200_over_11_30": m(t_on[180:]) / m(t_on[10:30]), "slope_us_per_call_index": 1e6 * slope, "load1": os.getloadavg()[0]}
                raw[key] = t_on
            finally:
                shutil.rmtree(d, ignore_errors=True)
    res["pass_persistent_host"] = {p: res[p + "_persistent"]["ratio_181_200_over_11_30"] <= 1.5 and res[p + "_persistent"]["median_ms_calls_181_200"] <= 5.0 for p in ("anthropic", "openai")}
    res["artifact"] = write_artifact(out, "item9", {"calls": 200, "injector": True, "guard": True, "stub_delay": 0, "stub_records": False}, raw, res)
    return res
