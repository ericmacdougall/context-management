"""B2: rebuild-equals-live over randomised logs. Live path: incremental apply with a JSON snapshot and restore every k events.
Rebuild path: fold of the whole log from genesis. Compared: state, manifest hash, outcome list. Controls: a live engine whose
authority map is edited out of band (not in the log), and a live engine that drops refused events from its log.
usage: python -m exp.b2_rebuild"""
import copy
import json
import random

from exp import common  # noqa: F401
from exp.common import write_result
from pol.core import REFUSED, apply, canon, current_table, explain, genesis, manifest_hash

SEEDS = [1, 2, 3, 4, 5]
PER_SEED = 600
PRINCIPALS = {"u1": "human", "u2": "human", "u3": "human", "a1": "agent"}
AUTHORITY = {"r1": ["u1", "u2"], "r2": ["u1"], "r3": ["u1", "a1"]}
ADMINS = ["u1"]
REQS = list(AUTHORITY)


def gen_events(rng, n):
    st = genesis(PRINCIPALS, AUTHORITY, ADMINS)
    evs, cnt = [], [0]

    def nid():
        cnt[0] += 1
        return "e%d" % cnt[0]

    def mk(**k):
        i = nid()
        d = {"id": i, "idem": i}
        d.update(k)
        return d

    def head(r):
        return st["reqs"][r]["version"]

    def pick_based(r):
        h = head(r)
        x = rng.random()
        return h if x < 0.65 else max(0, h - 1) if x < 0.85 else rng.randint(0, h + 2)

    def chan(pid):
        ok = "agent_api" if PRINCIPALS[pid] == "agent" else rng.choice(["owner_ui", "owner_cli"])
        return ok if rng.random() < 0.88 else rng.choice(["chat", "agent_api", "owner_ui"])

    def one():
        pid = rng.choice(list(PRINCIPALS))
        r = rng.choice(REQS)
        q = st["reqs"][r]
        t = rng.choices(["propose", "remark", "confirm", "decide", "supersede", "retract", "revoke", "grant"], [14, 10, 8, 26, 8, 5, 1.5, 2.5])[0]
        b = pick_based(r)
        if t == "propose":
            return mk(type=t, req=r, principal=pid, value="v%d" % rng.randrange(50), based_on=b)
        if t == "remark":
            return mk(type=t, req=r, principal=pid, value="v%d" % rng.randrange(50), channel=rng.choice(["chat", "owner_ui", "agent_api"]), based_on=b)
        if t == "confirm":
            pend = [p["id"] for p in q["pending"]] or ["nope"]
            return mk(type=t, req=r, principal=pid, pending=rng.choice(pend), channel=chan(pid), based_on=b)
        if t == "decide":
            verb = rng.choices(["accept", "reject", "amend", "resolve"], [40, 15, 15, 30])[0]
            ev = mk(type=t, req=r, principal=pid, verb=verb, channel=chan(pid), based_on=b if verb != "resolve" else head(r) if rng.random() < 0.8 else b)
            props = [p["id"] for p in q["proposals"]] or ["nope"]
            if verb in ("accept", "reject", "amend"):
                ev["proposal"] = rng.choice(props)
            if verb == "amend":
                ev["value"] = "v%d" % rng.randrange(50)
            if verb == "resolve":
                if q["contested"] and rng.random() < 0.85:
                    ev["choose"] = rng.choice([c["event"] for c in q["contested"]["candidates"]])
                else:
                    ev["value"] = "v%d" % rng.randrange(50)
            return ev
        if t == "supersede":
            return mk(type=t, req=r, principal=pid, value="v%d" % rng.randrange(50), channel=chan(pid), based_on=b)
        if t == "retract":
            return mk(type=t, req=r, principal=pid, channel=chan(pid), based_on=b)
        if t == "revoke":
            return mk(type=t, principal=rng.choice(["u1", "u1", "u2"]), target=rng.choice(["u2", "u3", "a1"]))
        return mk(type=t, principal="u1", req=r, target=rng.choice(["u2", "u3", "a1"]))

    def push(ev):
        nonlocal st
        evs.append(ev)
        st, _c = apply(st, ev)

    while len(evs) < n:
        x = rng.random()
        if x < 0.07 and evs:
            push(copy.deepcopy(rng.choice(evs)))                                     # redelivery
        elif x < 0.09 and evs:
            e = copy.deepcopy(rng.choice(evs))
            e["id"] = nid()
            e["value"] = "tampered"                                                  # same idem key, other payload
            push(e)
        elif x < 0.22:                                                               # concurrent siblings: two decisions at the same base by different principals
            r = rng.choice(REQS)
            q = st["reqs"][r]
            props = [p["id"] for p in q["proposals"] if p["status"] == "open"]
            auth = [p for p in st["authority"][r] if PRINCIPALS[p] == "human" and p not in st["revoked"]]
            if props and len(auth) >= 2:
                a, b = rng.sample(auth, 2)
                base = q["version"]
                push(mk(type="decide", req=r, principal=a, verb="accept", proposal=rng.choice(props), channel="owner_ui", based_on=base))
                push(mk(type="supersede", req=r, principal=b, value="v%d" % rng.randrange(50), channel="owner_cli", based_on=base))
            else:
                push(mk(type="propose", req=r, principal=rng.choice(list(PRINCIPALS)), value="v%d" % rng.randrange(50), based_on=q["version"]))
        else:
            push(one())
    return evs


def run_live(events, rng, oob=False, drop_refused=False):
    st = genesis(PRINCIPALS, AUTHORITY, ADMINS)
    log, outs, snaps = [], [], 0
    k = rng.randint(1, 9)
    oob_at = rng.randrange(max(1, len(events))) if oob else -1
    for i, ev in enumerate(events):
        if i == oob_at:
            st["authority"]["r2"] = sorted(set(st["authority"]["r2"]) | {"u2"})      # out-of-band edit: not a logged grant
        st, code = apply(st, ev)
        outs.append(code)
        if not (drop_refused and code in REFUSED):
            if code != "duplicate":
                log.append(ev)
        if (i + 1) % k == 0:
            st = json.loads(json.dumps(st))                                           # crash, restore from the snapshot, continue
            snaps += 1
    return st, log, outs, snaps


def rebuild(log):
    st = genesis(PRINCIPALS, AUTHORITY, ADMINS)
    outs = []
    for ev in log:
        st, c = apply(st, ev)
        outs.append(c)
    return st, outs


def compare(events, rng, **kw):
    live, log, outs_live, snaps = run_live(events, rng, **kw)
    reb, outs_reb = rebuild(log)
    # the recorded outcomes of events that were logged must equal the rebuilt outcomes (duplicates are not logged)
    logged_codes = [c for c, e in zip(outs_live, events) if c != "duplicate" and not (kw.get("drop_refused") and c in REFUSED)]
    same_state = canon(live) == canon(reb)
    same_hash = manifest_hash(live, log) == manifest_hash(reb, log)
    same_out = logged_codes == outs_reb
    return {"state": same_state, "hash": same_hash, "outcomes": same_out, "snaps": snaps, "live": live, "codes": outs_live}


def main():
    arms = {"enforced": {}, "ctl_out_of_band_authority": {"oob": True}, "ctl_drop_refused_events": {"drop_refused": True}}
    res = {}
    for name, kw in arms.items():
        r = {"logs": 0, "state_equal": 0, "hash_equal": 0, "outcomes_equal": 0, "all_equal": 0, "snapshots_restored": 0, "contested_logs": 0, "events": 0,
             "codes": {}, "explain_ok": 0}
        for seed in SEEDS:
            for i in range(PER_SEED):
                rng = random.Random(seed * 100000 + i)
                n = rng.randint(5, 80)
                events = gen_events(rng, n)
                c = compare(events, random.Random(rng.random()), **kw)
                r["logs"] += 1
                r["events"] += len(events)
                r["state_equal"] += c["state"]
                r["hash_equal"] += c["hash"]
                r["outcomes_equal"] += c["outcomes"]
                r["all_equal"] += c["state"] and c["hash"] and c["outcomes"]
                r["snapshots_restored"] += c["snaps"]
                for code in c["codes"]:
                    r["codes"][code] = r["codes"].get(code, 0) + 1
                r["contested_logs"] += "contested_opened" in c["codes"]
                if name == "enforced":
                    # "what is current and why" is consistent with the table for every requirement
                    ok = True
                    tbl = {row["req"]: row for row in current_table(c["live"])}
                    for req in AUTHORITY:
                        ex = explain(c["live"], req)
                        row = tbl[req]
                        ok &= ex["value"] == row["value"] and ex["status"] == row["status"] and ex["decision_event"] == row["decision_event"] and ex["version"] == row["version"]
                        ok &= (ex["contested"] is None) == (row["contested"] is None)
                    r["explain_ok"] += ok
        res[name] = r
    e = res["enforced"]
    ok = (e["all_equal"] == e["logs"] and e["logs"] >= 3000 and e["contested_logs"] >= 0.10 * e["logs"] and e["snapshots_restored"] >= 3000 and e["explain_ok"] == e["logs"]
          and all(e["codes"].get(c, 0) > 0 for c in ("stale", "unauthorized", "self_approval", "duplicate", "idem_conflict", "contested_opened", "resolved", "decided_accept",
                                                  "pending_created", "confirmed_applied", "revoked", "granted", "principal_revoked", "channel_mismatch")))
    ctl = {k: {"mismatch_rate": 1 - v["all_equal"] / v["logs"], "meets_floor": (1 - v["all_equal"] / v["logs"]) >= 0.10} for k, v in res.items() if k != "enforced"}
    ctl_ok = all(c["meets_floor"] for c in ctl.values())
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    data = {"verdict": verdict, "arms": res, "controls": ctl, "pass_conditions_met": ok, "controls_meet_floor": ctl_ok,
            "not_proven": "in-memory reducer only here; the Postgres rebuild is B3; logs are generated by one generator (adversarial but not exhaustive)"}
    write_result("b2_rebuild", {"seeds": SEEDS, "per_seed": PER_SEED, "length": "5..80"}, data)
    print(verdict, {k: e[k] for k in ("logs", "all_equal", "contested_logs", "snapshots_restored", "events", "explain_ok")}, ctl)
    print(sorted(e["codes"].items(), key=lambda x: -x[1]))


if __name__ == "__main__":
    main()
