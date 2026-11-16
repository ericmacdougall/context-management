"""P3: pull tool contract. Run: python3 -m ctxrt.proofs.p3_pull"""
from __future__ import annotations

import json
import random
import threading
import time

from memcore.guard import EgressGuard, EgressLeak
from ctxrt import gen
from ctxrt.decisions import DecisionLog
from ctxrt.kernel import MemoryLog
from ctxrt.proofs.common import check, main
from ctxrt.pulltools import MAX_CHARS, NO_RESULT_REASONS, PullTools, tf_score, toks

STORES, PAGINATION_RUNS, CHAINS, FORGED = 300, 100, 300, 1000
PRED = ["region", "owner", "budget", "deadline", "vendor"]
SUBJ = ["staging database", "billing service", "release train", "support rota", "data lake"]
VAL = ["Dublin", "Frankfurt", "Lisbon", "Tallinn", "Oslo", "Madrid", "Vienna", "Prague"]


def ts(i):
    return "2026-%02d-%02dT%02d:00:00Z" % (1 + i // 600, 1 + (i // 24) % 25, i % 24)


def make_store(rng):
    sess = "conv-%08x" % rng.getrandbits(32)
    log, dec = MemoryLog(secret=bytes(rng.getrandbits(8) for _ in range(8))), DecisionLog()
    n = rng.randint(5, 40)
    for i in range(n):
        big = rng.random() < 0.10
        txt = gen.text(rng, 3, 30) + " staging database"
        if big:
            txt = "staging database " + ("lorem ipsum 日本語 " * rng.randint(2000, 8000))[: rng.randint(60000, 200000)]
        log.append(sess, ts(i), rng.choice(["user", "assistant", "tool"]), txt, entities=rng.sample(["db", "billing", "Priya"], rng.randint(0, 2)))
    return sess, log, dec


def forbidden(sess, log):
    keys = [e["key"] for e in log.events[:50]]
    return [sess] + keys


def leak_scan(out_json, sess, keys):
    if sess in out_json or any(k in out_json for k in keys):
        return "internal_key_or_session"
    for bad in ('"seq"', '"_id"', '"session"', '"key"'):
        if bad in out_json:
            return "internal_field"
    return None


def counts_run(rng):
    c = dict(outputs=0, over_bound=0, truncated_flag_missing=0, leaks=0, huge_open=0, huge_open_chunks=0, no_results=0, no_results_bad=0, forged=0, forged_ok=0,
             guard_canary_blocked=0, truncated_witness=0, typed_bad=0)
    for _ in range(STORES):
        sess, log, dec = make_store(rng)
        guard = EgressGuard(real_ids=[sess], require_real_ids=True)
        tools = PullTools(log, dec, secret=b"cur-%d" % rng.getrandbits(16))
        small = PullTools(log, dec, secret=b"cur2", max_chars=1200)
        surf, ssurf = tools.surface(guard), small.surface(guard)
        keys = forbidden(sess, log)

        def call(s, name, args, limit=MAX_CHARS):
            r = s.call_tool(name, args)
            j = json.dumps(r, ensure_ascii=False)
            c["outputs"] += 1
            if len(j) > limit:
                c["over_bound"] += 1
            if leak_scan(j, sess, keys):
                c["leaks"] += 1
            return r
        for q in ["staging database", "database", "Priya", gen.text(rng, 1, 4)]:
            r = call(surf, "search", {"query": q, "limit": rng.randint(1, 10)})
            if r["status"] == "ok":
                cur, pages = r.get("next_cursor"), 0
                while cur and pages < 4:
                    r = call(surf, "search", {"query": q, "cursor": cur}); cur = r.get("next_cursor") if r["status"] == "ok" else None; pages += 1
                for it in r.get("results", [])[:2]:
                    o = call(surf, "open", {"id": it["id"]})
                    if o["status"] == "ok" and o["next_offset"] is not None:
                        c["huge_open"] += 1
                        off = 0
                        while off is not None and c["huge_open_chunks"] < 10 ** 9:
                            o2 = call(surf, "open", {"id": it["id"], "offset": off})
                            c["huge_open_chunks"] += 1
                            if o2["next_offset"] is not None and not o2["truncated"]:
                                c["truncated_flag_missing"] += 1
                            off = o2["next_offset"]
            rs = call(ssurf, "search", {"query": q, "limit": 10}, limit=1200)
            if rs["status"] == "ok" and rs.get("truncated"):
                c["truncated_witness"] += 1
        call(surf, "timeline", {"entity": "db"}); call(surf, "timeline", {"entity": "no-such-entity-zzz"})
        # typed nothing found
        for args in [{"query": "zzqx wvut"}, {"query": "staging", "as_of": "2000-01-01T00:00:00Z"}]:
            r = call(surf, "search", args)
            c["no_results"] += 1
            if not (isinstance(r, dict) and r.get("status") == "no_results" and r.get("reason") in NO_RESULT_REASONS):
                c["no_results_bad"] += 1
        r = call(PullTools(MemoryLog(), DecisionLog()).surface(guard), "search", {"query": "anything"})
        c["no_results"] += 1
        if not (r.get("status") == "no_results" and r.get("reason") == "empty_store"):
            c["no_results_bad"] += 1
        r = call(surf, "current_value", {"subject": "nothing here", "predicate": "x"})
        c["no_results"] += 1
        if not (r.get("status") == "no_results" and r.get("reason") in NO_RESULT_REASONS):
            c["no_results_bad"] += 1
        # forged ids
        real = log.events[0]["oid"]
        for _ in range(FORGED // STORES + 1):
            kind = rng.choice(["rand", "flip", "other_store", "internal_key"])
            if kind == "rand":
                fid = "m_%020x" % rng.getrandbits(80)
            elif kind == "flip":
                i = rng.randrange(2, len(real)); fid = real[:i] + ("0" if real[i] != "0" else "1") + real[i + 1:]
            elif kind == "other_store":
                fid = "m_" + "%020x" % rng.getrandbits(80)
            else:
                fid = log.events[0]["key"]
            r = call(surf, "open", {"id": fid})
            c["forged"] += 1
            c["forged_ok"] += r == {"status": "not_found"}
        # guard canary: an event containing the real session id must be refused by the guard
        log.append(sess, ts(999), "user", "staging database note mentions " + sess)
        try:
            surf.call_tool("search", {"query": "mentions"})
        except EgressLeak:
            c["guard_canary_blocked"] += 1
    return c


# ---------------------------------------------------------------- pagination under concurrent appends
def live_offset_page(log, kind, q, offset, limit):
    """CONTROL C1: offset pagination over the LIVE log (no snapshot)."""
    evs = log.upto(log.watermark())
    qt = toks(q)
    if kind == "search":
        cand = sorted(((-tf_score(qt, e["text"]), e["seq"], e) for e in evs if tf_score(qt, e["text"])), key=lambda t: t[:2])
    else:
        cand = sorted(((e["ts"], e["seq"], e) for e in evs if q in e["text"].lower()), key=lambda t: t[:2])
    return [t[2]["oid"] for t in cand[offset: offset + limit]], len(cand) > offset + limit


def pagination_run(rng, kind, use_snapshot):
    log, dec = MemoryLog(), DecisionLog()
    for i in range(rng.randint(30, 60)):
        log.append("s", ts(rng.randrange(0, 900)), "user", "staging " + " ".join(rng.choice(["database", "alpha", "beta"]) for _ in range(rng.randint(0, 3))))
    tools = PullTools(log, dec)
    stop = threading.Event()
    appended = []

    def appender():
        for _ in range(rng.randint(15, 40)):
            if stop.is_set():
                break
            time.sleep(rng.uniform(0, 0.002))
            log.append("s", ts(rng.randrange(0, 900)), "user", "staging database " + " ".join(rng.choice(["database", "alpha", "staging"]) for _ in range(rng.randint(0, 3))))
            appended.append(log.watermark())
    th = threading.Thread(target=appender)
    q = "staging database"
    seen, snap, first_wm, last_wm, pages = [], None, None, None, 0
    th.start()
    if use_snapshot:
        cur = None
        while True:
            r = tools.search(q, limit=4, cursor=cur) if kind == "search" else tools.timeline("staging", limit=4, cursor=cur)
            if pages == 0:
                snap = tools._untok(r["checked_through"])["wm"]
                first_wm = log.watermark()
            pages += 1
            last_wm = log.watermark()
            if r["status"] != "ok":
                break
            seen += [x["id"] for x in r["results"]]
            cur = r["next_cursor"]
            if not cur:
                break
            time.sleep(rng.uniform(0, 0.002))
    else:
        off = 0
        while True:
            with log._lock:
                if pages == 0:
                    snap = log.watermark()
                ids, more = live_offset_page(log, kind, q if kind == "search" else "staging", off, 4)
            if pages == 0:
                first_wm = log.watermark()
            pages += 1
            last_wm = log.watermark()
            seen += ids
            off += 4
            if not more:
                break
            time.sleep(rng.uniform(0, 0.002))
    stop.set(); th.join()
    exp_events = [e for e in log.upto(snap) if (tf_score(toks(q), e["text"]) if kind == "search" else "staging" in e["text"].lower())]
    exp = {e["oid"] for e in exp_events}
    dup = len(seen) - len(set(seen))
    skip = len(exp - set(seen))
    return {"kind": kind, "snapshot": use_snapshot, "pages": pages, "dup": dup, "skip": skip, "overlapped": last_wm > first_wm and last_wm > snap, "appends_between": last_wm - first_wm,
            "expected": len(exp), "seen": len(seen), "ok": dup == 0 and skip == 0}


# ---------------------------------------------------------------- current value
def chain_case(rng):
    subj, pred = rng.choice(SUBJ), rng.choice(PRED)
    k = rng.randint(2, 5)
    vals = rng.sample(VAL, k)
    log, dec = MemoryLog(), DecisionLog()
    times = sorted(rng.sample(range(1, 400), k + 1))
    retract = rng.random() < 0.2
    contested = (not retract) and rng.random() < 0.1
    for i, v in enumerate(vals):
        t = ts(times[i])
        last = i == k - 1
        # old statements are written so that they match the query "<subject> <predicate>" strongly; the change statement matches weakly or strongly at random
        elab = " %s %s %s confirmed." % (pred, subj, pred)
        if last:
            txt = rng.choice(["Update: the %s %s moved to %s." % (subj, pred, v), "The %s %s is now %s." % (subj, pred, v), "The %s %s is now %s.%s" % (subj, pred, v, elab)])
        else:
            txt = "The %s %s is %s.%s" % (subj, pred, v, elab if rng.random() < 0.5 else "")
        seq = log.append("s", t, "user", txt, meta={"value": v})
        dec.set(subj, pred, v, t, log.events[seq - 1]["key"])
    tools = PullTools(log, dec)
    if retract:
        dec.retract(subj, pred, ts(times[k]), None)
    if contested:
        # a second authorised principal decides against the same base version the head moved from: the reducer opens `contested`
        base = dec._live["reqs"][subj + "|" + pred if False else "%s|%s" % (subj.lower(), pred.lower())]["last"]["based_on"]
        dec.set(subj, pred, "CONFLICT", ts(times[k]), None, principal="owner2", based_on=base)
    return subj, pred, vals, times, retract, contested, log, dec, tools


def chain_run(rng):
    c = dict(chains=0, state_wrong=0, missing_meta=0, asof_checked=0, ctrl_chains=0, ctrl_wrong=0, retract_ok=0, retract_n=0, contested_ok=0, contested_n=0, decision_only_ok=0, decision_only_n=0)
    for _ in range(CHAINS):
        subj, pred, vals, times, retract, contested, log, dec, tools = chain_case(rng)
        c["chains"] += 1
        r = tools.current_value(subj, pred)
        if retract:
            c["retract_n"] += 1; c["retract_ok"] += r["status"] == "retracted"
            ok = r["status"] == "retracted"
        elif contested:
            c["contested_n"] += 1; c["contested_ok"] += r["status"] == "contested" and "value" not in r
            ok = r["status"] == "contested"
        else:
            ok = r["status"] == "ok" and r["value"] == vals[-1] and r["decision_event"] and r["checked_through"] and r["evidence"]
        if not ok:
            c["state_wrong"] += 1
        # as_of in the past: expected from an independent fold
        if not retract and not contested:
            j = rng.randrange(0, len(vals))
            asof = ts(times[j])
            r2 = tools.current_value(subj, pred, asof)
            c["asof_checked"] += 1
            if not (r2["status"] == "ok" and r2["value"] == vals[j]):
                c["state_wrong"] += 1
            # decision-state-only: an index that holds NO statement events must not change the answer
            only = PullTools(MemoryLog(), dec)
            c["decision_only_n"] += 1
            c["decision_only_ok"] += only.current_value(subj, pred)["value"] == vals[-1]
            # control C2: search-backed current value = top hit for the subject+predicate query
            top = tools.search("%s %s" % (subj, pred), limit=1)["results"][0]
            guess = log.by_oid(top["id"])["meta"]["value"]
            c["ctrl_chains"] += 1
            c["ctrl_wrong"] += guess != vals[-1]
    return c


def run_seed(seed, params):
    rng = random.Random(seed)
    rows, met = [], {}
    met["counts"] = counts_run(rng)
    pag = {"snapshot": [], "live_offset": []}
    for i in range(PAGINATION_RUNS):
        kind = "search" if i % 2 == 0 else "timeline"
        for snap, key in ((True, "snapshot"), (False, "live_offset")):
            r = pagination_run(rng, kind, snap)
            pag[key].append(r)
            rows.append(dict(r, run=i))
    met["pagination"] = {k: {"runs": len(v), "overlapped": sum(x["overlapped"] for x in v), "violations": sum(not x["ok"] for x in v),
                             "violations_overlapped": sum((not x["ok"]) and x["overlapped"] for x in v), "dup": sum(x["dup"] > 0 for x in v), "skip": sum(x["skip"] > 0 for x in v),
                             "dup_or_skip_runs": sum((x["dup"] > 0 or x["skip"] > 0) for x in v)} for k, v in pag.items()}
    met["chains"] = chain_run(rng)
    rows.append({"counts": met["counts"], "chains": met["chains"]})
    return rows, met


def evaluate(met, params):
    c, p, ch = met["counts"], met["pagination"], met["chains"]
    s, o = p["snapshot"], p["live_offset"]
    return [
        check("bounded: 0 results over 6000 chars", [c["over_bound"], c["outputs"]], c["over_bound"] == 0 and c["outputs"] >= 5000),
        check("bounded: truncated flag set whenever open cut text", c["truncated_flag_missing"], c["truncated_flag_missing"] == 0 and c["huge_open"] > 0),
        check("witness: forced-small-bound instance truncated at least once", c["truncated_witness"], c["truncated_witness"] > 0),
        check("opaque: 0 internal keys/session ids/fields in outputs", [c["leaks"], c["outputs"]], c["leaks"] == 0),
        check("guard canary: planted real session id blocked", c["guard_canary_blocked"], c["guard_canary_blocked"] == STORES),
        check("forged ids 100% typed not_found", [c["forged_ok"], c["forged"]], c["forged"] >= FORGED and c["forged_ok"] == c["forged"]),
        check("typed nothing found 100%", [c["no_results_bad"], c["no_results"]], c["no_results"] >= 300 and c["no_results_bad"] == 0),
        check("pagination: snapshot cursor 0 dup/skip in all runs", s, s["violations"] == 0),
        check("pagination overlap floor >=30% of runs", s["overlapped"] / s["runs"], s["overlapped"] / s["runs"] >= 0.30),
        check("control C1 offset pagination dup_or_skip >=10% of overlapped runs", [o["dup_or_skip_runs"], o["overlapped"]], o["overlapped"] > 0 and o["violations_overlapped"] / o["overlapped"] >= 0.10),
        check("current_value equals decision-state fold 100%", [ch["state_wrong"], ch["chains"]], ch["state_wrong"] == 0 and ch["chains"] >= CHAINS),
        check("current_value independent of search index (decision-only store)", [ch["decision_only_ok"], ch["decision_only_n"]], ch["decision_only_ok"] == ch["decision_only_n"] > 0),
        check("canary: retraction and contested typed", [ch["retract_ok"], ch["retract_n"], ch["contested_ok"], ch["contested_n"]], ch["retract_n"] > 0 and ch["retract_ok"] == ch["retract_n"] and ch["contested_n"] > 0 and ch["contested_ok"] == ch["contested_n"]),
        check("control C2 search-backed current_value wrong >=10%", [ch["ctrl_wrong"], ch["ctrl_chains"]], ch["ctrl_wrong"] / max(1, ch["ctrl_chains"]) >= 0.10),
    ]


if __name__ == "__main__":
    main("p3_pull", run_seed, evaluate, {"stores": STORES, "pagination_runs": PAGINATION_RUNS, "chains": CHAINS, "forged": FORGED}, claim="pull tool contract")
