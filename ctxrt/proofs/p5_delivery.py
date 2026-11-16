"""P5: inbox delivery end to end with the fencing token. Run: python3 -m ctxrt.proofs.p5_delivery"""
from __future__ import annotations

import random
import threading
import time

from ctxrt.delivery import (CursorOutsideCheckpointDelivery, Delivery, EnqueueOnlyPolicyDelivery, SendThenConfirmDelivery, SharedNativeIdDelivery, TokenlessDelivery)
from ctxrt.proofs.common import check, main
from ctxrt.rundb import Fenced, NestStore
from ctxrt.vendor.pol.enforce import Enforcer
from ctxrt.vendor.pol.labels import Label
from ctxrt.vendor.pol.pdp import Policy, Principal

RUNS = 200
LEASE = 0.04
POLICY_CASES = 200


def newd(cls, **kw):
    return cls(NestStore(":memory:"), lease_s=LEASE, **kw)


def verdict(d, lineages):
    dup = lost = 0
    for lin in lineages:
        c = d.appearances(lin)
        for f in d.master(d.lineage(lin)["logical"]):
            if c[f] > 1:
                dup += 1
            elif c[f] == 0:
                lost += 1
    return dup, lost


def drain(d, lin, tag, n=4):
    time.sleep(LEASE + 0.02)
    for k in range(n):
        d.run_turn(lin, "%s%d" % (tag, k))


# ------------------------------------------------------------------ paused turn (real threads, randomised pauses against a real lease)
def paused(rng, cls):
    d = newd(cls)
    d.new_lineage("S", "L")
    for i in range(rng.randint(1, 3)):
        d.produce("S", "f%d" % i)
    pa, db_ = rng.uniform(0, 0.10), rng.uniform(0, 0.08)
    out = {"a_refused": False, "a_committed": False}

    def A():
        b = d.begin_turn("L", "A")
        a_items = len(b["claim"])
        out["a_items"] = a_items
        time.sleep(pa)
        d.ev("A_commit_attempt", "A")
        try:
            d.commit_turn("L", "A", b)
            out["a_committed"] = True
        except Fenced:
            out["a_refused"] = True

    def B():
        time.sleep(db_)
        b = d.begin_turn("L", "B")
        d.ev("B_claimed", "B", n=len(b["claim"]))
        d.commit_turn("L", "B", b)
    ta, tb = threading.Thread(target=A), threading.Thread(target=B)
    ta.start(); tb.start(); ta.join(); tb.join()
    drain(d, "L", "D")
    dup, lost = verdict(d, ["L"])
    evs = d.events
    bcl = [e for e in evs if e["kind"] == "B_claimed" and e["n"] > 0]
    aat = [e for e in evs if e["kind"] == "A_commit_attempt"]
    zombie = bool(bcl and aat and out.get("a_items", 0) > 0 and bcl[0]["t"] < aat[0]["t"])        # A attempts its commit AFTER B took its items over
    return {"dup": dup, "lost": lost, "zombie": zombie, "zombie_refused": zombie and out["a_refused"], "pause_a": pa, "delay_b": db_}


# ------------------------------------------------------------------ crashed turn
def crash(rng, cls):
    d = newd(cls)
    d.new_lineage("S", "L")
    for i in range(rng.randint(1, 3)):
        d.produce("S", "f%d" % i)
    pt = rng.choice(["before_claim", "after_claim", "after_commit"])
    if hasattr(cls, "crash_after_send"):
        cls.crash_after_send = pt == "after_commit"
    stale = None
    if pt == "after_claim":
        stale = d.begin_turn("L", "A")
    elif pt == "after_commit":
        try:
            d.run_turn("L", "A")
        except RuntimeError:
            pass
        if cls is Delivery:
            d.run_turn("L", "A")          # the restarted turn replays: begin_turn finds the committed turn, claims nothing
    time.sleep(LEASE + 0.02)
    d.run_turn("L", "B")
    zombie_attempt = zombie_refused = 0
    if stale is not None:                  # the crashed process wakes up late with its old token
        zombie_attempt = 1
        try:
            d.commit_turn("L", "A", stale)
        except Fenced:
            zombie_refused = 1
    drain(d, "L", "D")
    dup, lost = verdict(d, ["L"])
    return {"dup": dup, "lost": lost, "pt": pt, "zombie_attempt": zombie_attempt, "zombie_refused": zombie_refused}


# ------------------------------------------------------------------ checkpoint rollback
def rollback(rng, cls):
    d = newd(cls)
    d.new_lineage("S", "L")
    n = 0

    def prod(k):
        nonlocal n
        for _ in range(k):
            d.produce("S", "f%d" % n)
            n += 1
    prod(1)
    for j in range(rng.randint(1, 3)):
        prod(rng.randint(0, 2))
        d.run_turn("L", "T%d" % j)
    cp = d.checkpoint("L")
    for j in range(rng.randint(1, 4)):
        prod(rng.randint(0, 2))
        d.run_turn("L", "U%d" % j)
    after = set(d.appearances("L")) - set(cp["cursor"])
    prod(1)
    inflight = {}
    ready = threading.Event()

    def X():                                  # a turn in flight (claimed under the old generation) when the rollback happens
        b = d.begin_turn("L", "X")
        ready.set()
        time.sleep(random.uniform(0, 0.01))
        try:
            d.commit_turn("L", "X", b)
            inflight["committed"] = True
        except Fenced:
            inflight["refused"] = True
    tx = threading.Thread(target=X)
    tx.start()
    ready.wait()
    time.sleep(random.uniform(0, 0.01))
    d.rollback("L", cp)
    tx.join()
    drain(d, "L", "R")
    dup, lost = verdict(d, ["L"])
    return {"dup": dup, "lost": lost, "rewound_with_delivered": len(after) >= 1, "after_n": len(after), "x_refused": inflight.get("refused", False)}


# ------------------------------------------------------------------ forked session
def fork(rng, cls):
    d = newd(cls)
    d.new_lineage("S", "P")
    n = 0

    def prod(k):
        nonlocal n
        for _ in range(k):
            d.produce("S", "f%d" % n)
            n += 1
    prod(1)
    for j in range(rng.randint(1, 3)):
        prod(rng.randint(0, 2))
        d.run_turn("P", "T%d" % j)
    cp = d.checkpoint("P")
    for j in range(rng.randint(0, 3)):
        prod(rng.randint(0, 2))
        d.run_turn("P", "U%d" % j)
    pending_or_delivered_after = len(set(d.master("S")) - set(cp["cursor"]))
    d.fork("P", "C", cp)
    stop = threading.Event()

    def producer():
        for _ in range(rng.randint(1, 3)):
            time.sleep(rng.uniform(0, 0.01))
            prod(1)

    def branch(lin, k):
        for j in range(k):
            time.sleep(random.uniform(0, 0.01))
            d.run_turn(lin, "%s-b%d" % (lin, j))
    ths = [threading.Thread(target=producer), threading.Thread(target=branch, args=("P", rng.randint(1, 3))), threading.Thread(target=branch, args=("C", rng.randint(1, 3)))]
    [t.start() for t in ths]
    [t.join() for t in ths]
    for lin in ("P", "C"):
        drain(d, lin, "D" + lin)
    dup, lost = verdict(d, ["P", "C"])
    return {"dup": dup, "lost": lost, "forked_with_item_after_cp": pending_or_delivered_after >= 1}


# ------------------------------------------------------------------ policy re-check at delivery
def policy_case(rng, cls):
    sender = Principal("analyst-1", "t1", clearance=3, kind="analyst")
    rcp = Principal("user-1", "t1", clearance=2, kind="human")
    label = Label(tenants=frozenset(["t1"]), sensitivity=1)
    base = Policy(1, grants={"t1": frozenset(["send_inbox"])})
    state = {"policy": base, "label": label}
    enf = Enforcer(lambda: state["policy"])
    d = cls(NestStore(":memory:"), lease_s=LEASE, enforcer=enf, sender=sender, recipient_of=lambda lin: rcp, label_of=lambda fid: state["label"])
    d.new_lineage("S", "L")
    d.produce("S", "f0")
    kind = rng.choice(["none", "revoke_recipient", "deny_tenant", "drop_grant", "tighten_label", "none"])
    if kind == "revoke_recipient":
        state["policy"] = base.tighten(revoked=frozenset(["user-1"]))
    elif kind == "deny_tenant":
        state["policy"] = base.tighten(deny_tenants=frozenset(["t1"]))
    elif kind == "drop_grant":
        state["policy"] = base.tighten(grants={})
    elif kind == "tighten_label":
        state["label"] = Label(tenants=frozenset(["t1"]), sensitivity=3)      # recipient clearance 2 < 3
    d.run_turn("L", "T")
    drain(d, "L", "D", 2)
    delivered = d.appearances("L")["f0"]
    dropped = [x for x in d.store.drops() if x["finding_id"] == "f0"]
    return {"kind": kind, "delivered": delivered, "dropped_reason": dropped[0]["reason"] if dropped else None}


def run_seed(seed, params):
    rng = random.Random(seed)
    random.seed(seed)
    rows, m = [], {}
    sched = {"paused": (paused, Delivery, TokenlessDelivery), "crash": (crash, Delivery, SendThenConfirmDelivery),
             "rollback": (rollback, Delivery, CursorOutsideCheckpointDelivery), "fork": (fork, Delivery, SharedNativeIdDelivery)}
    for name, (fn, real, ctrl) in sched.items():
        for tag, cls in (("real", real), ("ctrl", ctrl)):
            rs = []
            for i in range(RUNS):
                r = fn(rng, cls)
                rs.append(r)
                rows.append(dict(r, schedule=name, arm=tag, i=i))
            m["%s_%s" % (name, tag)] = {"runs": len(rs), "dup_runs": sum(r["dup"] > 0 for r in rs), "lost_runs": sum(r["lost"] > 0 for r in rs),
                                        **({"zombie": sum(r["zombie"] for r in rs), "zombie_refused": sum(r["zombie_refused"] for r in rs)} if name == "paused" else {}),
                                        **({"zombie_attempt": sum(r["zombie_attempt"] for r in rs), "zombie_refused": sum(r["zombie_refused"] for r in rs),
                                            "after_commit_runs": sum(r["pt"] == "after_commit" for r in rs), "dup_after_commit": sum(r["dup"] > 0 for r in rs if r["pt"] == "after_commit")} if name == "crash" else {}),
                                        **({"rewound": sum(r["rewound_with_delivered"] for r in rs), "x_refused": sum(r["x_refused"] for r in rs)} if name == "rollback" else {}),
                                        **({"forked_with_item": sum(r["forked_with_item_after_cp"] for r in rs)} if name == "fork" else {})}
    for tag, cls in (("real", Delivery), ("ctrl", EnqueueOnlyPolicyDelivery)):
        rs = [policy_case(rng, cls) for _ in range(POLICY_CASES)]
        for r in rs:
            rows.append(dict(r, schedule="policy", arm=tag))
        tight = [r for r in rs if r["kind"] != "none"]
        none = [r for r in rs if r["kind"] == "none"]
        m["policy_" + tag] = {"cases": len(rs), "tightened": len(tight), "tightened_delivered": sum(r["delivered"] > 0 for r in tight), "tightened_with_drop_reason": sum(bool(r["dropped_reason"]) for r in tight),
                              "untightened": len(none), "untightened_delivered": sum(r["delivered"] == 1 for r in none)}
    return rows, m


def evaluate(m, params):
    out = []
    for s in ("paused", "crash", "rollback", "fork"):
        r, c = m[s + "_real"], m[s + "_ctrl"]
        out.append(check("%s: exactly once in surviving lineage (0 dup, 0 lost, 100%% of runs)" % s, r, r["dup_runs"] == 0 and r["lost_runs"] == 0 and r["runs"] == RUNS))
    p = m["paused_real"]
    out += [check("paused witness: >=30 runs with a stale-token commit after takeover", [p["zombie"], p["runs"]], p["zombie"] >= 30),
            check("paused: stale-token commits refused 100%", [p["zombie_refused"], p["zombie"]], p["zombie_refused"] == p["zombie"] > 0)]
    cr = m["crash_real"]
    out += [check("crash witness: late zombie commit attempted and refused", [cr["zombie_attempt"], cr["zombie_refused"]], cr["zombie_attempt"] > 0 and cr["zombie_refused"] == cr["zombie_attempt"]),
            check("crash witness: crash after commit exercised", cr["after_commit_runs"], cr["after_commit_runs"] >= 30)]
    rb = m["rollback_real"]
    out += [check("rollback witness: >=100 runs rewound with an item delivered after the checkpoint", rb["rewound"], rb["rewound"] >= 100),
            check("rollback: in-flight old-generation commits refused", [rb["x_refused"], rb["runs"]], rb["x_refused"] > 0)]
    fk = m["fork_real"]
    out.append(check("fork witness: >=100 runs forked with an undelivered-to-child item", fk["forked_with_item"], fk["forked_with_item"] >= 100))
    out += [check("control paused (tokenless) duplicates >=10%", [m["paused_ctrl"]["dup_runs"], RUNS], m["paused_ctrl"]["dup_runs"] / RUNS >= 0.10),
            check("control crash (send then confirm) duplicates >=10% of runs", [m["crash_ctrl"]["dup_runs"], RUNS], m["crash_ctrl"]["dup_runs"] / RUNS >= 0.10),
            check("control rollback (cursor outside checkpoint) loses >=10%", [m["rollback_ctrl"]["lost_runs"], RUNS], m["rollback_ctrl"]["lost_runs"] / RUNS >= 0.10),
            check("control fork (shared native id) loses >=10%", [m["fork_ctrl"]["lost_runs"], RUNS], m["fork_ctrl"]["lost_runs"] / RUNS >= 0.10)]
    pr, pc = m["policy_real"], m["policy_ctrl"]
    out += [check("policy: tightened between enqueue and delivery -> 0 delivered", [pr["tightened_delivered"], pr["tightened"]], pr["tightened"] >= 100 and pr["tightened_delivered"] == 0),
            check("policy canary: untightened policy DOES deliver the same item", [pr["untightened_delivered"], pr["untightened"]], pr["untightened"] > 0 and pr["untightened_delivered"] == pr["untightened"]),
            check("policy: dropped items carry a recorded reason", [pr["tightened_with_drop_reason"], pr["tightened"]], pr["tightened_with_drop_reason"] == pr["tightened"]),
            check("control enqueue-only policy delivers tightened items >=10%", [pc["tightened_delivered"], pc["tightened"]], pc["tightened_delivered"] / max(1, pc["tightened"]) >= 0.10)]
    return out


if __name__ == "__main__":
    main("p5_delivery", run_seed, evaluate, {"runs": RUNS, "lease_s": LEASE, "policy_cases": POLICY_CASES}, claim="inbox exactly-once into the surviving lineage under pause, crash, rollback, fork; policy re-checked at delivery")
