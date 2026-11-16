"""B1: exhaustive exploration of every reachable state of a small decision-core scenario, with the invariants of pol.invariants
asserted in every state and on every transition; controls remove one rule at a time and must yield counterexamples.
usage: python -m exp.b1_modelcheck [depth]"""
import os
import sys
import time
from collections import deque

from exp import common  # noqa: F401
from exp.common import write_items, write_result
from pol.core import OUTCOME_CODES, apply, canon, genesis, sha
from pol.invariants import state_invariants, transition_invariants
from pol.testkit import prepare

DEPTH = 6
PRINCIPALS = {"u1": "human", "u2": "human", "a1": "agent", "n1": "analyst"}
AUTHORITY = {"r1": ["u1", "u2"], "r2": ["u1"], "r3": ["u1", "a1", "n1"]}
ADMINS = ["u1"]


def E(type, id, req=None, principal=None, **kw):
    d = {"type": type, "id": id, "idem": kw.pop("idem", id), "principal": principal}
    if req:
        d["req"] = req
    d.update(kw)
    return d


POOL = [
    E("propose", "P1", "r1", "a1", value="A", based_on=0),
    E("propose", "P2", "r1", "u2", value="B", based_on=0),
    E("propose", "P3", "r3", "a1", value="C", based_on=0),
    E("decide", "D1", "r1", "u1", verb="accept", proposal="P1", channel="owner_ui", based_on=0),
    E("decide", "D2", "r1", "u2", verb="accept", proposal="P2", channel="owner_ui", based_on=0),
    E("decide", "D3", "r1", "u1", verb="reject", proposal="P1", channel="owner_ui", based_on=0),
    E("decide", "D4", "r3", "a1", verb="accept", proposal="P3", channel="agent_api", based_on=0),
    E("decide", "D5", "r1", "a1", verb="accept", proposal="P1", channel="agent_api", based_on=0),
    E("supersede", "S1", "r1", "u1", value="Z", channel="owner_ui", based_on=1),
    E("retract", "T1", "r1", "u1", channel="owner_ui", based_on=1),
    E("decide", "D8", "r3", "u1", verb="accept", proposal="P3", channel="owner_ui", based_on=0),
    E("decide", "D8b", "r3", "u1", verb="accept", proposal="P3", channel="owner_ui", based_on=1),
    E("remark", "R1", "r2", "u1", value="X", channel="chat", based_on=0),
    E("remark", "R2", "r3", "u1", value="Y", channel="chat", based_on=0),
    E("remark", "R3", "r2", "u1", value="W", channel="owner_ui", based_on=0),
    E("confirm", "K1", "r3", "a1", pending="R2", channel="agent_api", based_on=0),
    E("confirm", "K2", "r2", "u1", pending="R3", channel="owner_ui", based_on=0),
    E("confirm", "K3", "r2", "u1", pending="R1", channel="owner_ui", based_on=0),
    E("confirm", "K3b", "r2", "u1", pending="R1", channel="owner_ui", based_on=1),
    E("confirm", "K4", "r2", "u1", pending="RX", channel="owner_ui", based_on=0),
    E("decide", "RES1", "r1", "u2", verb="resolve", choose="D2", channel="owner_ui", based_on=2),
    E("decide", "RES2", "r1", "u1", verb="resolve", choose="D1", channel="owner_ui", based_on=2),
    E("decide", "RES3", "r2", "u1", verb="resolve", choose="D1", channel="owner_ui", based_on=0),
    E("decide", "RES4", "r1", "u1", verb="resolve", choose="NOPE", channel="owner_ui", based_on=2),
    E("decide", "D9", "r1", "u1", verb="amend", proposal="P2", value="B2", channel="owner_ui", based_on=0),
    E("decide", "D10", "r1", "u2", verb="accept", proposal="P1", channel="owner_ui", based_on=0),
    E("decide", "Dc", "r1", "u1", verb="accept", proposal="P2", channel="chat", based_on=0),
    E("decide", "Dx", "r9", "u1", verb="accept", proposal="P1", channel="owner_ui", based_on=0),
    E("decide", "Dp", "r1", "u1", verb="accept", proposal="PX", channel="owner_ui", based_on=0),
    E("decide", "Dbad", "r1", "u1", verb="bogus", channel="owner_ui", based_on=0),
    E("decide", "D1", "r1", "u1", idem="D1", verb="accept", proposal="P2", channel="owner_ui", based_on=0),      # same idem key as D1, other payload
    E("retract", "T2", "r2", "u1", channel="owner_ui", based_on=0),
    E("supersede", "S2", "r1", "u1", value="Q", channel="owner_ui", based_on=2),
    E("revoke", "REV", None, "u1", target="u2"),
    E("grant", "GR", "r2", "u1", target="u2"),
    E("supersede", "S3", "r2", "u2", value="V", channel="owner_ui", based_on=0),
    E("confirm", "K5", "r2", "u1", pending="R1", channel="chat", based_on=0),
    E("grant", "GR1", "r1", "u1", target="a1"),
    E("grant", "GR3", "r3", "u1", target="u2"),
    E("propose", "P4", "r3", "u1", value="H", based_on=0),
    E("supersede", "AG1", "r3", "a1", value="AV", channel="agent_api", based_on=0),
    E("retract", "AG2", "r3", "a1", channel="agent_api", based_on=1),
    E("decide", "AG3", "r3", "a1", verb="amend", proposal="P4", value="AV2", channel="agent_api", based_on=0),
    E("decide", "AG4", "r3", "a1", verb="accept", proposal="P4", channel="agent_api", based_on=0),
    # a third principal kind (analyst): same restrictions as an agent
    E("supersede", "N1", "r3", "n1", value="NV", channel="agent_api", based_on=0),
    E("decide", "N2", "r3", "n1", verb="accept", proposal="P3", channel="agent_api", based_on=0),
    E("decide", "N3", "r3", "n1", verb="accept", proposal="P4", channel="anything", based_on=0),
    # symbolic bindings: "HEAD" is resolved to the requirement's version at delivery, so every guarded transition is reachable
    E("decide", "D2h", "r1", "u2", verb="accept", proposal="P2", channel="owner_ui", based_on="HEAD"),
    E("decide", "D3h", "r1", "u1", verb="reject", proposal="P1", channel="owner_ui", based_on="HEAD"),
    E("supersede", "S1h", "r1", "u1", value="Zh", channel="owner_ui", based_on="HEAD"),
    E("supersede", "S4h", "r1", "u2", value="Zu2", channel="owner_cli", based_on="HEAD"),
    E("retract", "T1h", "r1", "u1", channel="owner_ui", based_on="HEAD"),
    E("decide", "D10h", "r1", "u2", verb="accept", proposal="P1", channel="owner_cli", based_on="HEAD"),
    E("decide", "AGJ", "r1", "a1", verb="reject", proposal="P2", channel="agent_api", based_on="HEAD"),
    E("decide", "D10b", "r1", "u2", verb="accept", proposal="P1", channel="owner_cli", based_on=1),
    E("decide", "Xh", "r1", "u1", verb="accept", proposal="P1", channel="owner_ui", based_on=0, proposal_hash="bad"),
    E("supersede", "Xt", "r1", "u1", value="T", channel="owner_ui", based_on=0, tenant="T2"),
    E("supersede", "Xu", "r1", "u1", value="U", channel="owner_ui", based_on=0, label=None),
    E("propose", "P1", "r1", "u2", idem="P1dup", value="A2", based_on=0),       # a second proposal under an existing id (id uniqueness)
]
# the second D1 clashes with the id of the first: keep ids unique, only the idem key is shared
POOL[30] = dict(POOL[30], id="D1x")


SCENARIOS = {
    "r1": ["P1", "P2", "D1", "D2", "D3", "D5", "S1", "T1", "RES1", "RES2", "RES4", "D9", "D10", "Dc", "Dp", "Dbad", "D1x", "S2", "REV", "GR1", "D2h", "D3h", "S1h", "S4h", "T1h", "D10h", "AGJ", "D10b", "Xh", "Xt", "Xu"],
    "r2": ["R1", "R3", "K2", "K3", "K3b", "K4", "K5", "RES3", "T2", "S3", "REV", "GR", "Dx"],
    "r3": ["P3", "R2", "D4", "D8", "D8b", "K1", "REV", "GR3", "P4", "AG1", "AG2", "AG3", "AG4", "N1", "N2", "N3"],
}


def pool_of(ids):
    return [e for e in POOL if e["id"] in ids or (e["id"] == "D1x" and "D1x" in ids)]


def deliver(st, ev):
    """Resolve the symbolic binding and fill tenant, labels and proposal hashes the way a well-behaved client would."""
    if ev.get("based_on") == "HEAD":
        ev = dict(ev, based_on=st["reqs"][ev["req"]]["version"] if ev.get("req") in st["reqs"] else 0)
    return prepare(st, ev)


def liveness(st, flags):
    """L1: whatever agents do, a human with authority can resolve every contested requirement in one step."""
    from pol.core import apply as ap
    bad = []
    agent_evs = [e for e in POOL if PRINCIPALS.get(e["principal"]) != "human"]
    for r, q in st["reqs"].items():
        if q["status"] != "contested":
            continue
        for ae in [None] + agent_evs:
            s2 = st
            if ae is not None:
                s2, _c = ap(s2, deliver(s2, ae), flags)
            q2 = s2["reqs"][r]
            if q2["status"] != "contested":
                continue
            res = prepare(s2, {"type": "decide", "id": "LIVE", "idem": "LIVE", "req": r, "principal": "u1", "verb": "resolve", "choose": q2["contested"]["candidates"][0]["event"],
                               "channel": "owner_ui", "based_on": q2["version"]})
            _post, code = ap(s2, res, flags)
            if code != "resolved":
                bad.append("L1_contested_not_resolvable")
    return bad


def explore(flags, depth, collect_cex=True, max_cex=3, pool=None):
    pool = POOL if pool is None else pool
    POOLX = pool
    st0 = genesis('T1', PRINCIPALS, AUTHORITY, ADMINS)
    key0 = sha(st0)
    seen = {key0}
    parent = {key0: None}
    frontier = deque([(key0, st0, 0)])
    stats = {"states": 1, "transitions": 0, "max_depth": 0, "codes": {}, "violations": 0, "violated": {}, "cex": [], "contested_reached": 0, "resolved_reached": 0}
    for bad in state_invariants(st0):
        stats["violations"] += 1
    while frontier:
        key, st, d = frontier.popleft()
        stats["max_depth"] = max(stats["max_depth"], d)
        if d >= depth:
            continue
        for i, ev0 in enumerate(POOLX):
            ev = deliver(st, ev0)
            post, code = apply(st, ev, flags)
            stats["transitions"] += 1
            stats["codes"][code] = stats["codes"].get(code, 0) + 1
            bad = transition_invariants(st, ev, post, code)
            k2 = sha(post)
            if k2 not in seen:
                seen.add(k2)
                parent[k2] = (key, i)
                bad = bad + state_invariants(post)
                if any(q["status"] == "contested" for q in post["reqs"].values()):
                    bad = bad + liveness(post, flags)
                stats["states"] += 1
                if any(q["status"] == "contested" for q in post["reqs"].values()):
                    stats["contested_reached"] += 1
                if d + 1 < depth:
                    frontier.append((k2, post, d + 1))
            if bad:
                stats["violations"] += 1
                for b in set(bad):
                    stats["violated"][b] = stats["violated"].get(b, 0) + 1
                if collect_cex and len(stats["cex"]) < max_cex:
                    trace, k = [POOLX[i]["id"]], key
                    while parent[k] is not None:
                        k, j = parent[k][0], parent[k][1]
                        trace.append(POOLX[j]["id"])
                    stats["cex"].append({"invariants": sorted(set(bad)), "trace": list(reversed(trace)), "outcome": code})
    stats["resolved_reached"] = stats["codes"].get("resolved", 0)
    return stats


CONTROLS = [("remove_self_approval_check", frozenset(["self_approval"]), "I2_agent_head_move"),
            ("remove_staleness_check", frozenset(["staleness"]), "I4_decision_not_bound_to_head"),
            ("remove_idempotency_key", frozenset(["idempotency"]), "I6_redelivery_changed_state"),
            ("remove_authority_check", frozenset(["authority_check"]), "I1_authority"),
            ("remark_is_decision", frozenset(["remark_is_decision"]), "I3_remark_or_proposal_changed_value"),
            ("last_writer_wins", frozenset(["lww"]), "I7_two_applied_decisions_same_base"),
            ("remove_channel_check", frozenset(["channel_check"]), "I11_confirmation_not_typed_on_separate_owner_channel"),
            ("agents_may_contest", frozenset(["agent_contest"]), "I14_contest_held_open_by_agent_or_unbounded"),
            ("allow_duplicate_event_ids", frozenset(["unique_ids"]), "I13_duplicate_event_id_applied"),
            ("ignore_proposal_status_in_sibling", frozenset(["source_open"]), "I12_value_from_closed_proposal")]


SCEN_DEPTH = {"r1": 6, "r2": 8, "r3": 8}
JOINT_DEPTH = 4
if os.environ.get("B1_QUICK"):       # DEV only: shallow depths; the result file is stamped DEV when the tree is dirty and records these depths
    SCEN_DEPTH = {"r1": 5, "r2": 5, "r3": 5}
    JOINT_DEPTH = 3


def task(args):
    name, flags, scen, depth = args
    pl = POOL if scen == "joint" else pool_of(SCENARIOS[scen])
    t0 = time.time()
    s = explore(flags, depth, pool=pl)
    s["seconds"] = round(time.time() - t0, 1)
    return name, scen, depth, len(pl), s


def merge(rows):
    tot = {"states": 0, "transitions": 0, "violations": 0, "violated": {}, "codes": {}, "cex": [], "contested_reached": 0, "per_scenario": {}}
    for _n, scen, depth, npool, s in rows:
        tot["states"] += s["states"]
        tot["transitions"] += s["transitions"]
        tot["violations"] += s["violations"]
        tot["contested_reached"] += s["contested_reached"]
        for k, v in s["violated"].items():
            tot["violated"][k] = tot["violated"].get(k, 0) + v
        for k, v in s["codes"].items():
            tot["codes"][k] = tot["codes"].get(k, 0) + v
        tot["cex"] += [dict(c, scenario=scen) for c in s["cex"]]
        tot["per_scenario"][scen] = {"pool": npool, "depth": depth, "states": s["states"], "transitions": s["transitions"], "violations": s["violations"], "seconds": s["seconds"]}
    tot["cex"].sort(key=lambda c: len(c["trace"]))
    return tot


def main():
    import multiprocessing as mp
    arms = [("enforced", frozenset())] + [(n, f) for n, f, _e in CONTROLS]
    tasks = [(n, f, sc, d) for n, f in arms for sc, d in list(SCEN_DEPTH.items()) + ([("joint", JOINT_DEPTH)] if n == "enforced" else [("joint", 3)])]
    t0 = time.time()
    with mp.Pool(min(len(tasks), mp.cpu_count())) as pool:
        rows = pool.map(task, tasks, chunksize=1)
    by = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)
    good = merge(by["enforced"])
    ctl = {}
    for name, flags, expect in CONTROLS:
        s = merge(by[name])
        ctl[name] = {"states": s["states"], "violations": s["violations"], "violated": s["violated"], "expected_invariant": expect,
                     "expected_found": expect in s["violated"], "shortest_counterexample": s["cex"][0] if s["cex"] else None}
    UNREACHABLE = {"candidates_full"}     # needs nine humans; covered by tests/test_review_cap.py
    missing = sorted(set(OUTCOME_CODES) - set(good["codes"]) - UNREACHABLE)
    ok = (good["violations"] == 0 and good["states"] >= 1000 and good["transitions"] >= 20000 and not missing and good["contested_reached"] > 0
          and good["codes"].get("resolved", 0) > 0)
    ctl_ok = all(c["expected_found"] for c in ctl.values())
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    data = {"verdict": verdict, "scenarios": good["per_scenario"], "enforced": good, "outcome_codes_not_reached": missing, "controls": ctl,
            "pass_conditions_met": ok, "controls_found_expected_invariant": ctl_ok, "seconds_total": round(time.time() - t0, 1),
            "pools": {k: v for k, v in SCENARIOS.items()},
            "not_proven": "bounded exhaustive search: per-requirement scenarios to depth 6 (r1) and 8 (r2, r3), the joint three-requirement pool only to depth 4 (to depth 3 for controls); "
                          "the per-requirement decomposition is sound only because the reducer touches one requirement plus global revoke/grant and unique idem keys, which each scenario includes; "
                          "events carry fixed binding versions so concurrency is modelled as arbitrary delivery order, not parallel transactions (B3 covers those); three principals; "
                          "two humans, one agent, so peer-agent approval (two agents) is not reached"}
    rows = []
    for name, flags, expect in CONTROLS:
        c0 = ctl[name]["shortest_counterexample"]
        rows.append({"control": name, "flags": sorted(flags), "expected": expect, "trace": c0["trace"] if c0 else None, "scenario": c0.get("scenario") if c0 else None})
    for scen, v in good["per_scenario"].items():
        rows.append({"enforced_scenario": scen, **v})
    data["items"] = dict(zip(("file", "sha256"), write_items("b1_modelcheck", rows)))
    write_result("b1_modelcheck", {"scen_depth": SCEN_DEPTH, "joint_depth": JOINT_DEPTH, "principals": PRINCIPALS, "authority": AUTHORITY}, data)
    print(verdict, {k: good[k] for k in ("states", "transitions", "violations", "contested_reached")}, "codes missing:", missing, good["per_scenario"])
    for n, c in ctl.items():
        print(" ", n, "viol", c["violations"], "expected", c["expected_invariant"], "found", c["expected_found"], "cex", c["shortest_counterexample"] and c["shortest_counterexample"]["trace"])


if __name__ == "__main__":
    main()
