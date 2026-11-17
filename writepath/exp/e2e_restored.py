"""The three end-to-end tests restored from the original brief (THRESHOLDS amendment 14). Real processes, random
timing (seeded), critical instants on the database clock; races judged as in exp/races.py (amendment 3: verdict over
overlapping runs only, >= 20 overlapping runs per arm, Wilson 95% bounds reported).

E1 alias fence between two projectors. P1 (epoch e) reads a batch of events, then waits a random delay before its
   bulk; P2 takes the projector lease over (force, epoch e+1) after a random delay, which moves the ES write alias to
   w<e+1>, and projects only after P1 has finished. P1 tags every document it sends (stale_writer=P1).
   Overlap = P1 read before P2's alias CAS AND P1 bulk after it. Property: no document tagged by P1 exists in ES, and
   after P2 runs every event of the tenant is in ES exactly once.
   Control (one patch, in P2's process): the alias CAS adds w<e+1> without removing w<e>.
E2 concurrent same-idempotency-key submissions. K=4 processes submit the SAME request with the SAME key in the same
   session (same lease token) at one start instant plus jitter. Overlap = at least two append calls overlap in time.
   Property: exactly one event carries the key, every process got that event id back (accepted or duplicate), and
   the session chain verifies. Control (one patch, in the client processes): the client drops the key (each attempt
   gets a fresh one, the pre-fix behaviour named in FACTORY-MAP RES-04).
   store_only arm (thresholded too): the client's own pre-check (_existing) removed, so only the store (events_guard
   WPS14, unique index, chain lock) stands between the processes: exactly one event must still carry the key.
E3 replay of a decision under a new key (the lost-response retry). A client approves item `deploy` from typed message
   S under key k1, loses the response, retries with k1 (must be 'duplicate', the same event), then regenerates its key
   to k2, re-reads the head (now its own approval) and submits the same approval again from S. Sequential (R runs): a
   concurrent replay is also stopped by the head compare-and-set, so only the sequential re-read isolates the
   single-use-source rule. Property: one decision for S, head version 1, the k2 replay refused (WPS13).
   Control (one mechanism): the single-use-source check and its unique index removed (exp/review_fix F5 c1 patch).
usage: python -m exp.e2e_restored [E1 E2 E3] [N]"""
import json
import multiprocessing as mp
import random
import sys
import time

from exp import races
from exp.common import write_result
from exp.patches import patched, NOOP
from exp.races import _now, wilson
from exp.util import tenant, msg, session, es_count_all, env

SEED0 = 16096 * 7
K_PROC = 4
N_EVENTS = 6


# ---------------------------------------------------------------- E1
def _e1_p1(t, delay, q):
    from wp import projector
    conn, ks, es = env()
    marks = {}

    def hk(name, data=None):
        if name == "mirror_after_read" and "read" not in marks:
            marks["read"] = _now(conn)
        if name == "mirror_before_bulk" and "bulk" not in marks:
            for _, doc in data:
                doc["stale_writer"] = "P1"
            time.sleep(delay)
            marks["bulk"] = _now(conn)
    real_bulk = es.bulk_fenced

    def bulk_timed(actions):                 # amendment 14a: the instant P1's bulk has returned (DB clock)
        try:
            return real_bulk(actions)
        finally:
            marks.setdefault("bulk_done", _now(conn))
    es.bulk_fenced = bulk_timed
    p = projector.Projector(conn, ks, es, t, hook=hk)
    marks["step"] = p.step()
    marks["stale_bulk_items"] = p.stats["stale_bulk_items"]
    marks["docs_written"] = p.stats["docs_written"]
    q.put(("p1", marks))


def _e1_p2(t, delay, arm, p1_done, q):
    from wp import projector
    conn, ks, es = env()
    time.sleep(delay)
    if arm == "control":                    # PATCH: alias CAS without removing the old write alias
        real = es.aliases
        es.aliases = lambda actions: real([a for a in actions if "remove" not in a])
    p = projector.Projector(conn, ks, es, t)
    cas_start = _now(conn)                  # amendment 14a: before the forced takeover's CAS
    ok = p.start(force=True)
    m = {"cas_start": cas_start, "fenced_at": _now(conn), "epoch": p.lease.epoch if ok else None}
    p1_done.wait(60)
    p.run(max_rounds=50); p.stop()
    q.put(("p2", m))


def e1(arm, seed):
    rng = random.Random(seed)
    from wp import log, projector
    conn, ks, es = env()
    t = tenant("e1")
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"note {i}", "U") for i in range(N_EVENTS)])
    q, done = mp.Queue(), mp.Event()
    a = mp.Process(target=_e1_p1, args=(t, rng.uniform(0.3, 2.5), q))
    b = mp.Process(target=_e1_p2, args=(t, rng.uniform(0.1, 1.5), arm, done, q))
    a.start(); b.start()
    a.join(60); done.set(); b.join(120)
    got = dict(q.get(timeout=5) for _ in range(2))
    p1, p2 = got["p1"], got["p2"]
    planted = es_count_all(es, t, {"term": {"stale_writer.keyword": "P1"}})
    total = es_count_all(es, t, {"term": {"kind": "message"}})
    overlap = bool(p1.get("read") and p2.get("fenced_at") and p1["read"] < p2["fenced_at"] < p1.get("bulk", 0))
    return {"holds": e1_holds(e1_window(p1, p2), planted, total), "window": e1_window(p1, p2), "overlap": overlap,
            "p1_docs_in_es": planted, "events_in_es": total, "instants": {"p1": p1, "p2": p2}}


def e1_window(p1, p2):
    """Amendment 14a. before_takeover: P1's bulk returned before P2's CAS began (P1 was the live writer). in_flight: P1's
    bulk and P2's CAS intervals intersect. after_takeover: everything else (P1 sent after the CAS, or never sent)."""
    sent, done, cas0, cas1 = p1.get("bulk"), p1.get("bulk_done"), p2.get("cas_start"), p2.get("fenced_at")
    if done is not None and cas0 is not None and done < cas0:
        return "before_takeover"
    if sent is not None and cas1 is not None and sent < cas1:
        return "in_flight"
    return "after_takeover"


def e1_holds(window, planted, total):
    if window == "before_takeover":
        return total == N_EVENTS                # P1's documents are the legitimate projection
    return planted == 0 and total == N_EVENTS   # strict: in_flight is never excused


# ---------------------------------------------------------------- E2
def _e2_client(t, epoch, go_at, jitter, mode, q):
    from wp import log, lease as L
    conn, ks, es = env()
    if mode == "store_only":                # client pre-check removed: only the store decides
        log._existing = lambda *a, **k: {}
    lease = L.Lease(t, L.session_scope("s1"), epoch, "c", None)
    ev = msg(1, "submit order 42", "U", idem_key="order-42")
    if mode == "control":                   # PATCH: the client drops the idempotency key
        ev.pop("idem_key")
    while time.time() < go_at + jitter:
        pass
    t0 = _now(conn)
    try:
        r = log.append(conn, ks, lease, [ev])
        out = ("accepted", r["accepted"][0]) if r["accepted"] else ("duplicate", r["duplicates"][0]) if r["duplicates"] else ("other", None)
    except Exception as ex:  # noqa: BLE001
        out = (type(ex).__name__, None)
    q.put((t0, _now(conn), out))


def _e2(mode, seed):
    rng = random.Random(seed)
    from wp import db, log
    conn, ks, es = env()
    t = tenant("e2")
    s = session(conn, t, ttl=120)
    log.append(conn, ks, s, [msg(0, "opening", "U")])
    q = mp.Queue()
    go = time.time() + 2.0                  # every child is up before the start instant (spawn takes ~1 s)
    ps = [mp.Process(target=_e2_client, args=(t, s.epoch, go, rng.uniform(0.0, 0.03), mode, q)) for _ in range(K_PROC)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
    res = [q.get(timeout=5) for _ in ps]
    iv = sorted((a, b) for a, b, _ in res)
    overlap = any(iv[i + 1][0] < iv[i][1] for i in range(len(iv) - 1))
    adm = db.admin()
    n_rows = adm.execute("select count(*) from events where tenant_id=%s and session_id='s1' and kind='message' and ts_event=%s",
                         (t, msg(1, "", "U")["ts_event"])).fetchone()[0]
    ids = {eid for _, _, (_, eid) in res if eid}
    ok_chain = log.verify_chain(conn, t, "s1", ks=ks)[0]
    outcomes = [o for _, _, (o, _) in res]
    holds = n_rows == 1 and (mode == "store_only" or len(ids) == 1) and ok_chain
    return {"holds": holds, "overlap": overlap, "rows_with_request": n_rows, "distinct_ids_returned": len(ids), "chain_ok": ok_chain,
            "outcomes": {o: outcomes.count(o) for o in set(outcomes)}}


def e2(arm, seed):
    return _e2("shipped" if arm == "fenced" else "control", seed)


def e2_store_only(n):
    runs = []
    for i in range(n):
        seed = SEED0 * 1000 + 900 + i
        try:
            r = _e2("store_only", seed)
        except Exception as ex:  # noqa: BLE001
            r = {"holds": None, "overlap": False, "error": f"{type(ex).__name__}: {str(ex)[:160]}"}
        r["seed"] = seed
        runs.append(r)
    ov = [r for r in runs if r["overlap"]]
    k = sum(r["holds"] is True for r in ov)
    return {"runs_total": n, "overlapped": len(ov), "holds_overlapped": k, "violated_all": sum(r["holds"] is False for r in runs),
            "errors": sum(r["holds"] is None for r in runs), "wilson95_hold_rate_overlapped": wilson(k, len(ov)), "runs": runs}


# ---------------------------------------------------------------- E3
def e3_once():
    from wp import db, log
    conn, ks, _ = env()
    t = tenant("e3")
    s = session(conn, t)
    src = log.append(conn, ks, s, [msg(0, "yes, approve the deploy", "U")])["accepted"][0]
    ts = msg(1, "", "U")["ts_event"]
    first = log.decide(conn, ks, s, "U", "deploy", "approve", None, {"v": 3}, src, "k1", ts)
    retry_same = log.decide(conn, ks, s, "U", "deploy", "approve", None, {"v": 3}, src, "k1", ts)
    hv = log.head(conn, ks, t, "deploy")[1]
    try:
        replay = log.decide(conn, ks, s, "U", "deploy", "approve", hv, {"v": 3}, src, "k2", ts)[0]
    except log.StaleDecision as e:
        replay = "refused:" + str(e)[:5]
    n_dec = db.admin().execute("select count(*) from events where tenant_id=%s and kind='decision' and source_event_id=%s", (t, src)).fetchone()[0]
    version = log.head(conn, ks, t, "deploy")[0]
    return {"first": first[0], "retry_same_key": retry_same[0], "retry_same_event": retry_same[1] == first[1], "replay_new_key": replay,
            "decisions_for_source": n_dec, "head_version": version,
            "holds": replay.startswith("refused") and n_dec == 1 and version == 1}


def e3(r):
    from exp.review_fix import F5_CONTROLS
    out = {"R": r}
    out["fenced"] = [e3_once() for _ in range(r)]
    ap, rv = F5_CONTROLS["c1_second_decision_same_source"]
    with patched(ap, rv):
        out["control"] = [e3_once() for _ in range(r)]
    f_ok = all(x["holds"] and x["first"] == "accepted" and x["retry_same_key"] == "duplicate" and x["retry_same_event"]
               and x["replay_new_key"].startswith("refused:WPS13") for x in out["fenced"])
    c_viol = all(not x["holds"] for x in out["control"])
    out["patch"] = "events_guard single-use-source check removed and index decision_source_once dropped (F5 c1)"
    out["verdict"] = "PASS" if f_ok and c_viol else "FAIL"
    return out


races.RACES["E1"] = (e1, lambda: NOOP, "alias CAS adds the new write alias without removing the old one (in P2's process)")
races.RACES["E2"] = (e2, lambda: NOOP, "the client drops the idempotency key (in the client processes)")

if __name__ == "__main__":
    mp.set_start_method("spawn")
    which = [a for a in sys.argv[1:] if a in ("E1", "E2", "E3")] or ["E1", "E2", "E3"]
    N = int(next((a for a in sys.argv[1:] if a.isdigit()), "40"))
    for name in which:
        if name == "E3":
            res = e3(5)
        else:
            res = races.run(name, N)
            if name == "E1":                    # amendment 14a: window counts per arm
                for a in ("fenced", "control"):
                    w = {}
                    for x in res[a]["runs"]:
                        k = x.get("window", "error")
                        w[k] = w.get(k, 0) + 1
                    res[a]["windows"] = w
                    res[a]["in_flight_violated"] = sum(x.get("window") == "in_flight" and x["holds"] is False for x in res[a]["runs"])
            if name == "E2":
                so = e2_store_only(N)
                res["store_only"] = so
                if res["verdict"] == "PASS" and (so["overlapped"] < 20 or so["violated_all"] or so["errors"]):
                    res["verdict"] = "FAIL"         # amendment 14: store_only needs >= 20 overlapped, 0 violations, 0 errors, else FAIL
        summ = {a: {k: v for k, v in res[a].items() if k != "runs"} for a in ("fenced", "control", "store_only")
                if isinstance(res.get(a), dict)}
        print(name, res["verdict"], json.dumps(summ, default=str)[:1200], flush=True)
        write_result(f"e2e_{name}", res)
