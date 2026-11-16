"""P5: inbox delivery end to end with the fencing token. Run: python3 -m ctxrt.proofs.p5_delivery
Schedules (real threads, randomised pauses against a real lease): paused turn, crashed turn, checkpoint rollback, fork, MIXED CLAIM LIMITS (F1), the ANALYST
PATH under rollback (F2), finding-id collision across logical sessions (F2b), a turn id reused after rollback (F10). Policy arms depend on the vendored
policy point and are NOT PROVEN until the rescued policy point lands; they run behind PolicyBridge (sender, recipient, label derived server-side).
Delivery truth is read from the raw committed transcript rows, which are stored per run so recompute.py can re-derive duplicates and losses."""
from __future__ import annotations

import json
import random
import threading
import time

from memcore.inbox import InboxItem
from ctxrt.analyst import AnalystRun, ScriptedModel
from ctxrt.delivery import (AllowAllForTests, CursorOutsideCheckpointDelivery, Delivery, DeliverySink, DirectAnalystSink, EnqueueOnlyPolicyDelivery, PartialConfirmDelivery, PolicyBridge,
                            ReusedTurnIdDelivery, SendThenConfirmDelivery, SharedNativeIdDelivery, TokenlessDelivery)
from ctxrt.kernel import MemoryLog
from ctxrt.proofs.common import check, main
from ctxrt.rundb import Fenced, NestStore, RunDB
from ctxrt.vendor.pol.enforce import Enforcer
from ctxrt.vendor.pol.labels import Label
from ctxrt.vendor.pol.pdp import Policy, Principal

SECRET = b"p5-test-secret-0123456789abcdef"
RUNS = 200
LEASE = 0.04
POLICY_TIGHT = 200
POLICY_CANARY = 50
AUTHOR = ("analyst-A", "m-analyst")


def newd(cls, **kw):
    kw.setdefault("policy", AllowAllForTests())
    return cls(NestStore(":memory:"), lease_s=LEASE, **kw)


def raw(d, lineages):
    out = {"master": {}, "transcript": {}}
    for lin in lineages:
        L = d.lineage(lin)
        out["master"][L["logical"]] = d.master(L["logical"])
        out["transcript"][lin] = [{"turn_id": t, "gen": g, "alive": a, "ids": json.loads(i)} for t, g, i, a in d.store.db.execute("SELECT turn_id, gen, finding_ids, alive FROM d_transcript WHERE lineage=? ORDER BY seq", (lin,)).fetchall()]
    return out


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


def paused(rng, cls):
    d = newd(cls)
    d.new_lineage("S", "L")
    for i in range(rng.randint(1, 3)):
        d.produce("S", "f%d" % i)
    pa, db_ = rng.uniform(0, 0.10), rng.uniform(0, 0.08)
    out = {"a_refused": False, "a_committed": False}

    def A():
        b = d.begin_turn("L", "A")
        out["a_items"] = len(b["claim"])
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
    bcl = [e for e in d.events if e["kind"] == "B_claimed" and e["n"] > 0]
    aat = [e for e in d.events if e["kind"] == "A_commit_attempt"]
    zombie = bool(bcl and aat and out.get("a_items", 0) > 0 and bcl[0]["t"] < aat[0]["t"])
    return {"dup": dup, "lost": lost, "zombie": zombie, "zombie_refused": zombie and out["a_refused"], "raw": raw(d, ["L"])}


def mixed_limits(rng, cls):
    """F1: turn A claims everything (limit 5) and pauses past its lease; turn B claims with limit 1 and takes the oldest item; A then commits."""
    d = newd(cls)
    d.new_lineage("S", "L")
    nf = rng.randint(2, 4)
    for i in range(nf):
        d.produce("S", "f%d" % i)
    pa = rng.uniform(LEASE + 0.03, LEASE + 0.10)
    out = {"a_refused": False}

    def A():
        b = d.begin_turn("L", "A", limit=5)
        time.sleep(pa)
        d.ev("A_commit_attempt", "A")
        try:
            d.commit_turn("L", "A", b)
        except Fenced:
            out["a_refused"] = True

    def B():
        time.sleep(LEASE + rng.uniform(0.005, 0.02))
        b = d.begin_turn("L", "B", limit=1)
        d.ev("B_claimed", "B", n=len(b["claim"]))
        d.commit_turn("L", "B", b)
    ta, tb = threading.Thread(target=A), threading.Thread(target=B)
    ta.start(); tb.start(); ta.join(); tb.join()
    drain(d, "L", "D", n=6)
    dup, lost = verdict(d, ["L"])
    bcl = [e for e in d.events if e["kind"] == "B_claimed" and e["n"] > 0]
    aat = [e for e in d.events if e["kind"] == "A_commit_attempt"]
    partial = bool(bcl and aat and bcl[0]["t"] < aat[0]["t"])        # B took part of A's claim before A committed
    return {"dup": dup, "lost": lost, "partial_takeover": partial, "a_refused": out["a_refused"], "raw": raw(d, ["L"])}


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
            d.run_turn("L", "A")
    time.sleep(LEASE + 0.02)
    d.run_turn("L", "B")
    zombie_attempt = zombie_refused = 0
    if stale is not None:
        zombie_attempt = 1
        try:
            d.commit_turn("L", "A", stale)
        except Fenced:
            zombie_refused = 1
    drain(d, "L", "D")
    dup, lost = verdict(d, ["L"])
    return {"dup": dup, "lost": lost, "pt": pt, "zombie_attempt": zombie_attempt, "zombie_refused": zombie_refused, "raw": raw(d, ["L"])}


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

    def X():
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
    return {"dup": dup, "lost": lost, "rewound_with_delivered": len(after) >= 1, "after_n": len(after), "x_refused": inflight.get("refused", False), "raw": raw(d, ["L"])}


def analyst_finding(d, sink, k, logical="S"):
    """A REAL AnalystRun produces one finding through `sink`."""
    log = MemoryLog(SECRET)
    log.append("conv-a", "2026-01-01T00:00:00Z", "user", "the office is in City%d." % k, speaker="user", round=1)
    db = RunDB(d.store)
    db.open_step("step-A", 10 ** 6)
    r = db.launch(logical, "step-A", "q%d" % k, 1, "v1", 5, "o")
    fin = json.dumps({"schema_version": 1, "outcome": "findings", "dead_ends": [], "findings": [{"candidate_id": "c", "event_id": log.events[0]["oid"], "round": 1, "speaker": "user", "quote": "the office is in City%d" % k, "claim": "x"}]})
    run = AnalystRun(db, r["key"], r["epoch"], ScriptedModel([{"type": "final", "text": fin}]), {}, "q%d" % k, logical, log, sink, author=AUTHOR, readable_sessions=frozenset(["conv-a"]))
    run.run()
    return run


def analyst_rollback(rng, cls, sink_cls=None):
    """F2: the finding comes from the analyst write gate; rollback must re-offer it (exactly once in the surviving lineage)."""
    d = newd(cls)
    d.new_lineage("S", "L")
    sink = (sink_cls or DeliverySink)(d, "S") if sink_cls is None else sink_cls(d, "L")
    d.produce("S", "p0")
    d.run_turn("L", "T0")
    cp = d.checkpoint("L")
    for k in range(rng.randint(1, 2)):
        analyst_finding(d, sink, k)
    d.run_turn("L", "U0")
    delivered_after = len(set(d.appearances("L")) - set(cp["cursor"]))
    d.rollback("L", cp)
    drain(d, "L", "R")
    # a finding that EXISTS only through the analyst path must be counted in `master` (the durable record), not only in inbox rows
    master = set(d.master("S"))
    seen = d.appearances("L")
    dup = sum(1 for f in seen if seen[f] > 1)
    lost = sum(1 for f in master if seen[f] == 0)
    inbox_only_lost = delivered_after - sum(1 for f in seen if f not in cp["cursor"])
    return {"dup": dup, "lost": lost + max(0, inbox_only_lost), "delivered_after_cp": delivered_after, "raw": raw(d, ["L"])}


def id_collision(rng, cls):
    """F2b: the same finding id in two logical sessions; a rollback in the second must still re-offer it."""
    d = newd(cls)
    d.new_lineage("S1", "L1")
    d.new_lineage("S2", "L2")
    d.produce("S1", "same-id")
    d.produce("S2", "same-id")
    cp = d.checkpoint("L2")
    d.run_turn("L1", "T")
    d.run_turn("L2", "T")
    d.rollback("L2", cp)
    drain(d, "L2", "R")
    drain(d, "L1", "R")
    dup, lost = 0, 0
    for lin, logical in (("L1", "S1"), ("L2", "S2")):
        c = d.appearances(lin)
        for f in ("same-id",):
            dup += c[f] > 1
            lost += c[f] == 0
    return {"dup": dup, "lost": lost, "raw": raw(d, ["L1", "L2"])}


def turn_id_reuse(rng, cls):
    """F10: after a rollback the host reuses turn id `T7`; the dead turn's block must not be replayed. `shown` counts what the model is shown."""
    d = newd(cls)
    d.new_lineage("S", "L")
    d.produce("S", "p0")
    d.run_turn("L", "T0")
    cp = d.checkpoint("L")
    d.produce("S", "fA")
    d.run_turn("L", "T7")
    d.rollback("L", cp)
    shown = []
    for tid in ("T7", "T8", "T9"):
        time.sleep(0.001)
        r = d.run_turn("L", tid)
        shown += r["finding_ids"]
        if tid == "T7" and r["finding_ids"] == [] and not d.stored_turn("L", "T7"):
            pass
    time.sleep(LEASE + 0.02)
    for tid in ("T10", "T11"):
        shown += d.run_turn("L", tid)["finding_ids"]
    dup = shown.count("fA") > 1
    lost = d.appearances("L")["fA"] == 0
    return {"dup": int(dup), "lost": int(lost), "shown_fA": shown.count("fA"), "raw": raw(d, ["L"])}


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
    return {"dup": dup, "lost": lost, "forked_with_item_after_cp": pending_or_delivered_after >= 1, "raw": raw(d, ["P", "C"])}


# ------------------------------------------------------------------ policy re-check at delivery (NOT PROVEN until the policy rescue lands)
def policy_case(rng, cls, kind):
    base = Policy(1, grants={"t1": frozenset(["send_inbox"])})
    state = {"policy": base}
    enf = Enforcer(lambda: state["policy"])
    senders = {"analyst-A": Principal("analyst-A", "t1", clearance=3, kind="analyst")}
    rcp = Principal("user-1", "t1", clearance=2, kind="human")
    bridge = PolicyBridge(enf, lambda agent: senders[agent], lambda lin: rcp)
    d = cls(NestStore(":memory:"), lease_s=LEASE, policy=bridge)
    d.new_lineage("S", "L")
    d.produce("S", "f0", label=Label(tenants=frozenset(["t1"]), sensitivity=1).to_dict(), author=AUTHOR)
    if kind == "revoke_recipient":
        state["policy"] = base.tighten(revoked=frozenset(["user-1"]))
    elif kind == "deny_tenant":
        state["policy"] = base.tighten(deny_tenants=frozenset(["t1"]))
    elif kind == "drop_grant":
        state["policy"] = base.tighten(grants={})
    elif kind == "tighten_label":          # the stored (server-side) label changes; delivery re-reads it
        d.store.db.execute("UPDATE d_findings SET label=? WHERE finding_id='f0'", (json.dumps(Label(tenants=frozenset(["t1"]), sensitivity=3).to_dict()),))
    d.run_turn("L", "T")
    drain(d, "L", "D", 2)
    delivered = d.appearances("L")["f0"]
    dropped = [x for x in d.store.drops() if x["finding_id"] == "f0"]
    return {"kind": kind, "delivered": delivered, "dropped_reason": dropped[0]["reason"] if dropped else None}


def no_policy_case(rng):
    d = Delivery(NestStore(":memory:"), lease_s=LEASE, policy=None)
    d.new_lineage("S", "L")
    d.produce("S", "f0", author=AUTHOR)
    d.run_turn("L", "T")
    return {"delivered": d.appearances("L")["f0"]}


SCHEDULES = {
    "paused": (paused, Delivery, TokenlessDelivery),
    "crash": (crash, Delivery, SendThenConfirmDelivery),
    "rollback": (rollback, Delivery, CursorOutsideCheckpointDelivery),
    "fork": (fork, Delivery, SharedNativeIdDelivery),
    "mixed": (mixed_limits, Delivery, PartialConfirmDelivery),
    "analyst_rollback": (analyst_rollback, Delivery, "direct"),
    "turn_reuse": (turn_id_reuse, Delivery, ReusedTurnIdDelivery),
}


def run_seed(seed, params):
    rng = random.Random(seed)
    random.seed(seed)
    rows, m = [], {}
    for name, (fn, real, ctrl) in SCHEDULES.items():
        for tag, cls in (("real", real), ("ctrl", ctrl)):
            rs = []
            for i in range(RUNS):
                if name == "analyst_rollback" and tag == "ctrl":
                    r = analyst_rollback_direct(rng)
                else:
                    r = fn(rng, cls)
                rs.append(r)
                rows.append(dict(r, schedule=name, arm=tag, i=i))
            agg = {"runs": len(rs), "dup_runs": sum(r["dup"] > 0 for r in rs), "lost_runs": sum(r["lost"] > 0 for r in rs)}
            if name == "paused":
                agg.update(zombie=sum(r["zombie"] for r in rs), zombie_refused=sum(r["zombie_refused"] for r in rs))
            if name == "mixed":
                agg.update(partial=sum(r["partial_takeover"] for r in rs), a_refused=sum(r["a_refused"] for r in rs))
            if name == "crash":
                agg.update(zombie_attempt=sum(r["zombie_attempt"] for r in rs), zombie_refused=sum(r["zombie_refused"] for r in rs), after_commit_runs=sum(r["pt"] == "after_commit" for r in rs))
            if name == "rollback":
                agg.update(rewound=sum(r["rewound_with_delivered"] for r in rs), x_refused=sum(r["x_refused"] for r in rs))
            if name == "fork":
                agg.update(forked_with_item=sum(r["forked_with_item_after_cp"] for r in rs))
            if name == "analyst_rollback":
                agg.update(delivered_after=sum(r["delivered_after_cp"] > 0 for r in rs))
            m["%s_%s" % (name, tag)] = agg
    for tag, cls in (("real", Delivery), ("ctrl", id_collision_control)):
        rs = [id_collision(rng, cls) for _ in range(RUNS)]
        rows += [dict(r, schedule="id_collision", arm=tag) for r in rs]
        m["id_collision_" + tag] = {"runs": len(rs), "dup_runs": sum(r["dup"] > 0 for r in rs), "lost_runs": sum(r["lost"] > 0 for r in rs)}
    # policy arms (NOT PROVEN until the policy rescue lands)
    kinds = ["revoke_recipient", "deny_tenant", "drop_grant", "tighten_label"]
    for tag, cls in (("real", Delivery), ("ctrl", EnqueueOnlyPolicyDelivery)):
        tight = [policy_case(rng, cls, kinds[i % 4]) for i in range(POLICY_TIGHT)]
        canary = [policy_case(rng, cls, "none") for _ in range(POLICY_CANARY)]
        rows += [dict(r, schedule="policy", arm=tag) for r in tight + canary]
        m["policy_" + tag] = {"tightened": len(tight), "tightened_delivered": sum(r["delivered"] > 0 for r in tight), "tightened_with_drop_reason": sum(bool(r["dropped_reason"]) for r in tight),
                              "untightened": len(canary), "untightened_delivered": sum(r["delivered"] == 1 for r in canary)}
    npc = [no_policy_case(rng) for _ in range(50)]
    m["no_policy"] = {"cases": len(npc), "delivered": sum(r["delivered"] for r in npc)}
    return rows, m


class id_collision_control(Delivery):
    """CONTROL (F2b): finding ids are global (the 1d19cd8 d_findings primary key): the second logical session's copy is not stored."""

    def ingest_finding(self, logical, item, author, run_key=None, label=None):
        if self.store.db.execute("SELECT 1 FROM d_findings WHERE finding_id=?", (item["finding_id"],)).fetchone():
            with self.store.transaction():
                new = False
                for lin in self.live_lineages(logical):
                    new |= self.svc.enqueue(InboxItem(**dict(item, session_id=self.lineage(lin)["inbox_sid"])))
                return new
        return Delivery.ingest_finding(self, logical, item, author, run_key, label)


def analyst_rollback_direct(rng):
    d = newd(Delivery)
    d.new_lineage("S", "L")
    sink = DirectAnalystSink(d, "L")
    d.produce("S", "p0")
    d.run_turn("L", "T0")
    cp = d.checkpoint("L")
    for k in range(rng.randint(1, 2)):
        analyst_finding(d, sink, k)
    d.run_turn("L", "U0")
    # the analyst findings exist only as inbox rows; the durable master has none of them
    delivered_after = len(set(d.appearances("L")) - set(cp["cursor"]))
    d.rollback("L", cp)
    drain(d, "L", "R")
    seen = d.appearances("L")
    survivors = sum(1 for f in seen if f not in cp["cursor"])
    return {"dup": 0, "lost": int(delivered_after > survivors), "delivered_after_cp": delivered_after, "raw": raw(d, ["L"])}


def evaluate(m, params):
    out = []
    for s in SCHEDULES:
        r = m[s + "_real"]
        out.append(check("%s: exactly once in surviving lineage (0 dup, 0 lost, 100%% of runs)" % s, r, r["dup_runs"] == 0 and r["lost_runs"] == 0 and r["runs"] == RUNS))
    ic = m["id_collision_real"]
    out.append(check("id collision across logical sessions: exactly once in each", ic, ic["dup_runs"] == 0 and ic["lost_runs"] == 0))
    p = m["paused_real"]
    out += [check("paused witness: >=30 runs with a stale-token commit after takeover", [p["zombie"], p["runs"]], p["zombie"] >= 30),
            check("paused: stale-token commits refused 100%", [p["zombie_refused"], p["zombie"]], p["zombie_refused"] == p["zombie"] > 0)]
    mx = m["mixed_real"]
    out += [check("mixed limits witness: >=100 runs where B took part of A's claim before A committed", [mx["partial"], mx["runs"]], mx["partial"] >= 100),
            check("mixed limits: A's partial-confirm commit refused in every such run", [mx["a_refused"], mx["partial"]], mx["a_refused"] >= mx["partial"] > 0)]
    cr = m["crash_real"]
    out += [check("crash witness: late zombie commit attempted and refused", [cr["zombie_attempt"], cr["zombie_refused"]], cr["zombie_attempt"] > 0 and cr["zombie_refused"] == cr["zombie_attempt"]),
            check("crash witness: crash after commit exercised", cr["after_commit_runs"], cr["after_commit_runs"] >= 30)]
    rb = m["rollback_real"]
    out += [check("rollback witness: >=100 runs rewound with an item delivered after the checkpoint", rb["rewound"], rb["rewound"] >= 100),
            check("rollback: in-flight old-generation commits refused", [rb["x_refused"], rb["runs"]], rb["x_refused"] > 0)]
    ar = m["analyst_rollback_real"]
    out.append(check("analyst path witness: >=100 runs with an analyst finding delivered after the checkpoint", ar["delivered_after"], ar["delivered_after"] >= 100))
    fk = m["fork_real"]
    out.append(check("fork witness: >=100 runs forked with an undelivered-to-child item", fk["forked_with_item"], fk["forked_with_item"] >= 100))
    ctrls = [("paused", "paused (tokenless) duplicates", "dup_runs"), ("crash", "crash (send then confirm) duplicates", "dup_runs"), ("rollback", "rollback (cursor outside checkpoint) loses", "lost_runs"),
             ("fork", "fork (shared native id) loses", "lost_runs"), ("mixed", "mixed limits (1d19cd8 partial-confirm commit) duplicates", "dup_runs"),
             ("analyst_rollback", "analyst path (direct enqueue, no durable record) loses on rollback", "lost_runs"), ("turn_reuse", "reused turn id replays the dead turn (shown twice)", "dup_runs")]
    for s, label, key in ctrls:
        out.append(check("control %s >=10%%" % label, [m[s + "_ctrl"][key], RUNS], m[s + "_ctrl"][key] / RUNS >= 0.10))
    ic2 = m["id_collision_ctrl"]
    out.append(check("control id collision (global finding id) loses >=10%", [ic2["lost_runs"], RUNS], ic2["lost_runs"] / RUNS >= 0.10))
    pr, pc = m["policy_real"], m["policy_ctrl"]
    tag = "[NOT PROVEN until the policy rescue lands] "
    out += [check(tag + "policy: tightened between enqueue and delivery -> 0 delivered (>=200 tightened cases)", [pr["tightened_delivered"], pr["tightened"]], pr["tightened"] >= 200 and pr["tightened_delivered"] == 0),
            check(tag + "policy canary: untightened policy DOES deliver the same item", [pr["untightened_delivered"], pr["untightened"]], pr["untightened"] > 0 and pr["untightened_delivered"] == pr["untightened"]),
            check(tag + "policy: dropped items carry a recorded reason", [pr["tightened_with_drop_reason"], pr["tightened"]], pr["tightened_with_drop_reason"] == pr["tightened"]),
            check(tag + "control enqueue-only policy delivers tightened items >=10%", [pc["tightened_delivered"], pc["tightened"]], pc["tightened_delivered"] / max(1, pc["tightened"]) >= 0.10),
            check("no policy bridge configured: nothing is delivered (fail closed)", m["no_policy"], m["no_policy"]["delivered"] == 0)]
    return out


if __name__ == "__main__":
    main("p5_delivery", run_seed, evaluate, {"runs": RUNS, "lease_s": LEASE, "policy_tightened": POLICY_TIGHT, "policy_canary": POLICY_CANARY,
                                             "policy_status": "NOT PROVEN until the rescued policy point lands", "concurrency": "one in-process SQLite connection behind a lock"},
         claim="inbox exactly-once into the surviving lineage under pause, crash, rollback, fork, mixed claim limits, analyst path, id collision, reused turn id")
