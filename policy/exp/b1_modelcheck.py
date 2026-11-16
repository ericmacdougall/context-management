"""B1: exhaustive breadth-first exploration of every reachable state of small decision-core scenarios over pol._core.reduce on
MemoryState, with exp/b1_invariants asserted on every transition and in every new state, the contested-resolve liveness L1 in every
contested state, and the head-starvation bound L2 (an inductive per-transition check plus a played game witness, see b1_invariants).

Every pool contains, for EVERY binding event type (propose, remark, confirm, decide, supersede, retract), at least one event bound
to "HEAD", resolved to the requirement's version at delivery (asserted at import). Proposal hashes are filled from the state the
way a well-behaved client would, except for events that carry a wrong hash on purpose.

Controls are broken implementations: one-patch source variants of pol/_core.py (exp.patches). Each must yield a counterexample whose
violated invariant is the expected one; the shortest trace (BFS order) is recorded with the raw pre/post snapshots of its last step.
usage: python -m exp.b1_modelcheck   (B1_QUICK=1: shallow DEV depths)"""
import json
import os
import sys
import time
from collections import deque

from exp import common  # noqa: F401
from exp import b1_invariants as INV
from exp.common import write_items, write_result
from exp.patches import CONTROLS as PATCHES
import pol._core as CORE

T = "T1"
PRINCIPALS = {"u1": "human", "u2": "human", "a1": "agent", "n1": "analyst"}
AUTHORITY = {"r1": ["u1", "u2"], "r2": ["u1"], "r3": ["u1", "a1"]}
PROPOSERS = {"r1": ["n1"], "r3": ["n1"]}
ADMINS = ["u1"]
LAB = {"tenants": [T], "acl": [], "regions": None, "sensitivity": 0, "models": None, "banned": [], "local_only": False, "restricted": False,
       "holds": [], "subjects": []}
BINDING = ("propose", "remark", "confirm", "decide", "supersede", "retract")


def config():
    return {"tenant": T, "principals": dict(PRINCIPALS), "authority": {r: list(v) for r, v in AUTHORITY.items()},
            "proposers": {r: list(v) for r, v in PROPOSERS.items()}, "admins": list(ADMINS)}


def E(type, id, req, principal, **kw):
    kind = PRINCIPALS.get(principal)
    d = {"type": type, "id": id, "idem": kw.pop("idem", id), "tenant": kw.pop("tenant", T), "principal": principal,
         "channel": kw.pop("channel", "owner_ui" if kind == "human" else "agent_api")}
    if req is not None:
        d["req"] = req
    if type in ("propose", "remark", "supersede") or (type == "decide" and kw.get("verb") == "amend") or (
            type == "decide" and kw.get("verb") == "resolve" and "choose" not in kw):
        d["label"] = dict(LAB)
    d.update(kw)
    if "label" in d and d["label"] is None:
        del d["label"]                              # malformed on purpose: missing label
    return d


POOLS = {
    "r1": [
        E("propose", "P1", "r1", "n1", value_ref="vA", based_on=0),
        E("propose", "P2", "r1", "u2", value_ref="vB", based_on=0),
        E("decide", "D1", "r1", "u1", verb="accept", proposal="P1", based_on=0),
        E("decide", "D2", "r1", "u2", verb="accept", proposal="P2", based_on=0),
        E("decide", "D3", "r1", "u1", verb="reject", proposal="P1", based_on=0),
        E("decide", "D5", "r1", "a1", verb="accept", proposal="P1", based_on=0),
        E("supersede", "S1", "r1", "u1", value_ref="vZ", based_on=1),
        E("retract", "T1", "r1", "u1", based_on=1),
        E("decide", "RES1", "r1", "u2", verb="resolve", choose="D2", based_on=2),
        E("decide", "RES2", "r1", "u1", verb="resolve", choose="D1", based_on=2),
        E("decide", "RES4", "r1", "u1", verb="resolve", choose="NOPE", based_on=2),
        E("decide", "RESV", "r1", "u1", verb="resolve", value_ref="vR", based_on="HEAD"),
        E("decide", "D9", "r1", "u1", verb="amend", proposal="P2", value_ref="vB2", based_on=0),
        E("decide", "D10", "r1", "u2", verb="accept", proposal="P1", based_on=0),
        E("decide", "Dc", "r1", "u1", verb="accept", proposal="P2", channel="agent_api", based_on=0),
        E("decide", "Dx", "r9", "u1", verb="accept", proposal="P1", based_on=0),
        E("decide", "Dp", "r1", "u1", verb="accept", proposal="PX", based_on=0),
        E("decide", "Dbad", "r1", "u1", verb="bogus", based_on=0),
        E("decide", "D1x", "r1", "u1", idem="D1", verb="accept", proposal="P2", based_on=0),
        E("supersede", "S2", "r1", "u1", value_ref="vQ", based_on=2),
        E("revoke", "REV", None, "u1", target="u2"),
        E("grant", "GR1", "r1", "u1", target="a1", role="decide"),
        E("decide", "D2h", "r1", "u2", verb="accept", proposal="P2", based_on="HEAD"),
        E("decide", "D3h", "r1", "u1", verb="reject", proposal="P1", based_on="HEAD"),
        E("supersede", "S1h", "r1", "u1", value_ref="vZh", based_on="HEAD"),
        E("supersede", "S4h", "r1", "u2", value_ref="vZu2", channel="owner_cli", based_on="HEAD"),
        E("retract", "T1h", "r1", "u1", based_on="HEAD"),
        E("decide", "AGJ", "r1", "a1", verb="reject", proposal="P1", based_on="HEAD"),
        E("decide", "AGA", "r1", "a1", verb="accept", proposal="P2", based_on="HEAD"),
        E("propose", "P5h", "r1", "u2", value_ref="vP5", based_on="HEAD"),
        E("decide", "Xh", "r1", "u1", verb="accept", proposal="P1", based_on=0, proposal_hash="bad"),
        E("supersede", "Xt", "r1", "u1", value_ref="vT", based_on=0, tenant="T2"),
        E("supersede", "Xu", "r1", "u1", value_ref="vU", based_on=0, label=None),
        E("propose", "P1", "r1", "u2", idem="P1dup", value_ref="vA2", based_on=0),
        E("remark", "RMh", "r1", "u2", value_ref="vRMh", channel="owner_cli", based_on="HEAD"),
        E("confirm", "KMh", "r1", "u1", pending="RMh", based_on="HEAD"),
    ],
    "r2": [
        E("remark", "R1", "r2", "u1", value_ref="vX", channel="owner_cli", based_on=0),
        E("remark", "R3", "r2", "u1", value_ref="vW", based_on=0),
        E("confirm", "K2", "r2", "u1", pending="R3", based_on=0),
        E("confirm", "K3", "r2", "u1", pending="R1", based_on=0),
        E("confirm", "K3b", "r2", "u1", pending="R1", based_on=1),
        E("confirm", "K3h", "r2", "u1", pending="R1", based_on="HEAD"),
        E("confirm", "K4", "r2", "u1", pending="RX", based_on=0),
        E("confirm", "K5", "r2", "u1", pending="R1", channel="agent_api", based_on=0),
        E("decide", "RES3", "r2", "u1", verb="resolve", choose="K3", based_on=0),
        E("retract", "T2", "r2", "u1", based_on=0),
        E("retract", "T2h", "r2", "u1", based_on="HEAD"),
        E("supersede", "S3", "r2", "u2", value_ref="vV", based_on=0),
        E("supersede", "S3h", "r2", "u2", value_ref="vVh", based_on="HEAD"),
        E("revoke", "REV", None, "u1", target="u2"),
        E("revoke", "RVa", None, "a1", target="u1"),
        E("grant", "GR", "r2", "u1", target="u2", role="decide"),
        E("propose", "NP", "r2", "n1", value_ref="vN", based_on=0),
        E("propose", "P7h", "r2", "u1", value_ref="vP7", based_on="HEAD"),
        E("remark", "R5h", "r2", "u1", value_ref="vR5", channel="owner_cli", based_on="HEAD"),
        E("decide", "D7h", "r2", "u1", verb="accept", proposal="P7h", based_on="HEAD"),
    ],
    "r3": [
        E("propose", "P3", "r3", "n1", value_ref="vC", based_on=0),
        E("propose", "P4", "r3", "u1", value_ref="vH", based_on=0),
        E("decide", "D4", "r3", "a1", verb="accept", proposal="P3", based_on=0),
        E("decide", "D8", "r3", "u1", verb="accept", proposal="P3", based_on=0),
        E("decide", "D8b", "r3", "u1", verb="accept", proposal="P3", based_on=1),
        E("decide", "D8h", "r3", "u1", verb="accept", proposal="P3", based_on="HEAD"),
        E("remark", "R2", "r3", "u1", value_ref="vY", channel="owner_cli", based_on=0),
        E("confirm", "K1", "r3", "a1", pending="R2", based_on=0),
        E("confirm", "K6h", "r3", "u1", pending="R2", based_on="HEAD"),
        E("supersede", "AG1", "r3", "a1", value_ref="vAV", based_on=0),
        E("retract", "AG2", "r3", "a1", based_on=1),
        E("decide", "AG3", "r3", "a1", verb="amend", proposal="P4", value_ref="vAV2", based_on=0),
        E("decide", "AG4", "r3", "a1", verb="accept", proposal="P4", based_on=0),
        E("decide", "AG4h", "r3", "a1", verb="accept", proposal="P4", based_on="HEAD"),
        E("decide", "AR3", "r3", "a1", verb="reject", proposal="P3", based_on="HEAD"),
        E("decide", "AR4", "r3", "a1", verb="reject", proposal="P4", based_on=0),
        E("supersede", "N1", "r3", "n1", value_ref="vNV", based_on=0),
        E("decide", "N2", "r3", "n1", verb="accept", proposal="P3", based_on=0),
        E("grant", "GR3", "r3", "u1", target="u2", role="decide"),
        E("revoke", "REVn", None, "u1", target="n1"),
        E("supersede", "S5h", "r3", "u1", value_ref="vS5", based_on="HEAD"),
        E("retract", "T5h", "r3", "u1", based_on="HEAD"),
        E("propose", "P8h", "r3", "n1", value_ref="vP8", based_on="HEAD"),
        E("remark", "RM3h", "r3", "u1", value_ref="vRM", based_on="HEAD"),
        E("decide", "D11", "r3", "u1", verb="reject", proposal="P4", based_on=0),
    ],
    # small-scope caps scenario: run on a core whose caps are 2 per principal and 3 per requirement (SMALL_CAPS, constants only)
    "caps": [
        E("propose", "C1", "r1", "n1", value_ref="vC1", based_on=0),
        E("propose", "C2", "r1", "n1", value_ref="vC2", based_on=0),
        E("propose", "C3", "r1", "n1", value_ref="vC3", based_on="HEAD"),
        E("remark", "C4", "r1", "u2", value_ref="vC4", based_on="HEAD"),
        E("propose", "C5", "r1", "u1", value_ref="vC5", based_on=0),
        E("decide", "CR", "r1", "u1", verb="reject", proposal="C1", based_on="HEAD"),
        E("decide", "CA", "r1", "u2", verb="accept", proposal="C2", based_on="HEAD"),
        E("confirm", "CK", "r1", "u1", pending="C4", channel="owner_cli", based_on="HEAD"),
        E("supersede", "CS", "r1", "u1", value_ref="vCS", based_on="HEAD"),
        E("retract", "CT", "r1", "u1", based_on="HEAD"),
        E("revoke", "CREV", None, "u1", target="n1"),
        E("propose", "C6", "r1", "a1", value_ref="vC6", based_on=0),
        E("grant", "CGR", "r1", "u1", target="a1", role="propose"),    # round 3: a second non-human proposer, so non-human items can reach the per-req cap
    ],
}
for _name, _pool in POOLS.items():
    _missing = set(BINDING) - {e["type"] for e in _pool if e.get("based_on") == "HEAD"}
    assert not _missing, ("every pool needs a HEAD-bound event of every binding type", _name, _missing)

SMALL_CAPS = [("MAX_OPEN_PER_PRINCIPAL = 16\n", "MAX_OPEN_PER_PRINCIPAL = 2\n"), ("MAX_OPEN_PER_REQ = 256 ", "MAX_OPEN_PER_REQ = 3 ")]
DEPTH = {"r1": 6, "r2": 8, "r3": 8, "caps": 8, "joint": 4}
CTL_DEPTH = {"r1": 6, "r2": 6, "r3": 6, "caps": 6}
if os.environ.get("B1_QUICK"):
    DEPTH = {"r1": 3, "r2": 4, "r3": 4, "caps": 5, "joint": 2}
    CTL_DEPTH = {"r1": 3, "r2": 4, "r3": 4, "caps": 5}

OWN = {
    "idempotency_removed": ("pol._core", "    old = view.idem(pid, ev[\"idem\"])\n", "    old = None\n"),
    "authority_removed": ("pol._core", "    # ---- head-moving types\n    if not view.has_authority(req, pid):\n        return \"unauthorized\", []\n",
                          "    # ---- head-moving types\n"),
}
# (name, (module, old, new), expected invariant, scenarios)
# Round 3 (amendment 5): agent_may_decide replaces the two N3 controls and self_approval_removed (the rules they broke are gone: a
# non-human never decides); cap_blocks_humans is R3-4 reintroduced.
CONTROLS = [
    ("agent_may_decide", PATCHES["agent_may_decide"], "I2_agent_head_move", ["r3"]),
    ("cap_blocks_humans", PATCHES["cap_blocks_humans"], "I18_human_locked_out_by_nonhumans", ["caps"]),
    ("n2_no_role", PATCHES["n2_no_role"], "I15_proposal_without_role", ["r2"]),
    ("n2_no_cap", PATCHES["n2_no_cap"], "I17_open_cap", ["caps"]),
    ("stale_unchecked", PATCHES["stale_unchecked"], "I4_decision_not_bound_to_head", ["r1", "r2"]),
    ("agent_may_supersede", PATCHES["agent_may_supersede"], "I2_agent_head_move", ["r3"]),
    ("m2_no_original", PATCHES["m2_no_original"], "I6_redelivery_changed_state_or_lost_outcome", ["r2"]),
    ("idempotency_removed", OWN["idempotency_removed"], "I6_redelivery_changed_state_or_lost_outcome", ["r2"]),
    ("authority_removed", OWN["authority_removed"], "I1_authority", ["r2"]),
]
CTL_BY_NAME = {c[0]: c for c in CONTROLS}


def multi_variant(module_name, patches, tag):
    """Replacements each asserted to apply exactly once, executed as a fresh module in package pol (production file untouched)."""
    import importlib
    import types
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *module_name.split(".")) + ".py"
    src = open(path, encoding="utf-8").read()
    for old, new in patches:
        assert src.count(old) == 1, (tag, old)
        src = src.replace(old, new)
    importlib.import_module(module_name)
    mod = types.ModuleType(module_name + "__b1_" + tag)
    mod.__file__, mod.__package__ = path + "#" + tag, "pol"
    sys.modules[mod.__name__] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


def core_for(arm, scen):
    patches = list(SMALL_CAPS) if scen == "caps" else []
    if arm != "enforced":
        m, old, new = CTL_BY_NAME[arm][1]
        assert m == "pol._core"
        patches.append((old, new))
    return CORE if not patches else multi_variant("pol._core", patches, "%s_%s" % (arm, scen))


def deliver(st, tpl, core):
    ev = dict(tpl)
    if ev.get("based_on") == "HEAD":
        ev["based_on"] = st.heads[ev["req"]]["version"] if ev.get("req") in st.heads else 0
    if "proposal" in ev and "proposal_hash" not in ev:
        p = st.props[ev["req"]].get(ev["proposal"]) if ev.get("req") in st.props else None
        ev["proposal_hash"] = core.proposal_hash(p["id"], p["value_ref"], p["author"]) if p else "0" * 64
    return ev


def step(core, st, ev):
    code, orig, delta = core.reduce(st, ev)
    if not delta:
        return st, code, orig
    post = st.copy()
    post.apply_delta(delta)
    return post, code, orig


def snap(st):
    s = getattr(st, "_b1snap", None)
    if s is None:
        s = INV.canon(st.snapshot())
        st._b1snap = s
    return s


def human_for(st, req):
    return next((p for p in sorted(st.authority[req]) if st.principals.get(p) == "human" and p not in st.revoked), None)


def liveness_l1(core, st, pool):
    """L1: in every contested requirement, after any single non-human pool event, an authorised human resolve succeeds."""
    bad = []
    for r, h in st.heads.items():
        if h["status"] != "contested":
            continue
        for ae in [None] + [e for e in pool if PRINCIPALS.get(e["principal"]) != "human"]:
            s2 = st if ae is None else step(core, st, deliver(st, ae, core))[0]
            h2 = s2.heads[r]
            who = human_for(s2, r)
            if h2["status"] != "contested" or who is None:
                continue
            res = {"type": "decide", "id": "LIVE-R", "idem": "LIVE-R", "tenant": T, "principal": who, "channel": "owner_ui", "req": r, "verb": "resolve",
                   "choose": h2["contested"]["candidates"][0]["event"], "based_on": h2["version"]}
            if step(core, s2, res)[1] != "resolved":
                bad.append("L1_contested_not_resolvable")
    return bad


def l2_game(core, pre, post, req):
    """Played witness of an L2 violation: the human read pre's version; the adversary's event moved the head; the human's probe bound to
    the read version is stale; it re-reads and retries (applied). Attempts vs the bound (open human-authored proposals at the read + 1)."""
    who = human_for(post, req)
    if who is None or post.heads[req]["status"] == "contested":
        return None
    mk = lambda n, v: {"type": "supersede", "id": "LIVE%d" % n, "idem": "LIVE%d" % n, "tenant": T, "principal": who, "channel": "owner_ui",  # noqa: E731
                       "req": req, "value_ref": "vLIVE", "label": dict(LAB), "based_on": v}
    v = pre.heads[req]["version"]
    c1 = step(core, post, mk(1, v))[1]
    c2 = step(core, post, mk(2, post.heads[req]["version"]))[1]
    return {"read_version": v, "probe1_code": c1, "probe2_code": c2, "attempts": 1 + (c1 == "stale"),
            "bound": INV.open_human(json.loads(snap(pre)), req) + 1}


def explore(arm, scen, depth, max_cex=2):
    core = core_for(arm, scen)
    pool = POOLS[scen] if scen != "joint" else POOLS["r1"] + POOLS["r2"] + POOLS["r3"]
    st0 = core.MemoryState(config())
    k0 = INV.sha(snap(st0))
    seen = {k0}
    parent = {k0: None}
    frontier = deque([(k0, st0, 0)])
    stats = {"arm": arm, "scenario": scen, "depth": depth, "pool": len(pool), "states": 1, "transitions": 0, "codes": {}, "violations": 0,
             "violated": {}, "cex": [], "contested_states": 0, "l1_checks": 0}
    restore = lambda s: core.MemoryState.restore(s).snapshot()                       # noqa: E731
    caps = (core.MAX_CANDIDATES, core.MAX_OPEN_PER_PRINCIPAL, core.MAX_OPEN_PER_REQ)
    refused = set(core.REFUSED)
    t0 = time.time()
    while frontier:
        key, st, d = frontier.popleft()
        if d >= depth:
            continue
        pre = json.loads(snap(st))
        for tpl in pool:
            ev = deliver(st, tpl, core)
            post, code, orig = step(core, st, ev)
            stats["transitions"] += 1
            stats["codes"][code] = stats["codes"].get(code, 0) + 1
            ps = json.loads(snap(post)) if post is not st else pre
            bad = INV.transition(pre, ev, ps, code, orig, refused, core.MAX_OPEN_PER_PRINCIPAL)
            k2 = INV.sha(snap(post))
            if k2 not in seen:
                seen.add(k2)
                parent[k2] = (key, ev)
                bad = bad + INV.state(ps, restore, *caps)
                stats["states"] += 1
                if any(h["status"] == "contested" for h in post.heads.values()):
                    stats["contested_states"] += 1
                    stats["l1_checks"] += 1
                    bad = bad + liveness_l1(core, post, pool)
                if d + 1 < depth:
                    frontier.append((k2, post, d + 1))
            if bad:
                stats["violations"] += 1
                for b in set(bad):
                    stats["violated"][b] = stats["violated"].get(b, 0) + 1
                if any(sum(1 for c in stats["cex"] if b in c["invariants"]) < max_cex for b in set(bad)):
                    trace, k = [ev], key
                    while parent[k] is not None:
                        k, e = parent[k]
                        trace.append(e)
                    trace.reverse()
                    cx = {"invariants": sorted(set(bad)), "trace": trace, "code": code, "orig": orig, "pre": pre, "post": ps, "ev": ev}
                    if "L2_head_starvation" in bad and isinstance(ev.get("req"), str):
                        cx["game"] = l2_game(core, st, post, ev["req"])
                    stats["cex"].append(cx)
    stats["seconds"] = round(time.time() - t0, 1)
    return stats


def task(args):
    return explore(*args)


def main():
    import multiprocessing as mp
    tasks = [("enforced", s, d) for s, d in DEPTH.items()] + [(n, s, CTL_DEPTH[s]) for n, _p, _e, scs in CONTROLS for s in scs]
    t0 = time.time()
    with mp.Pool(min(len(tasks), mp.cpu_count())) as p:
        res = p.map(task, tasks, chunksize=1)
    enf = [r for r in res if r["arm"] == "enforced"]
    codes = {}
    for r in enf:
        for k, v in r["codes"].items():
            codes[k] = codes.get(k, 0) + v
    UNREACHABLE = {"candidates_full": "needs 8 joined candidates (depth >= 10); covered by the B2 targeted mode"}
    missing = sorted(set(CORE.OUTCOME_CODES) - set(codes) - set(UNREACHABLE))
    states = sum(r["states"] for r in enf)
    trans = sum(r["transitions"] for r in enf)
    contested = sum(r["contested_states"] for r in enf)
    ok = (all(r["violations"] == 0 for r in enf) and states >= 1000 and trans >= 20000 and not missing and contested > 0 and codes.get("resolved", 0) > 0)
    ctl, rows = {}, []
    for name, _p, expect, _scs in CONTROLS:
        rs = [r for r in res if r["arm"] == name]
        cex = sorted([c for r in rs for c in r["cex"] if expect in c["invariants"]], key=lambda c: len(c["trace"]))
        viol = {}
        for r in rs:
            for k, v in r["violated"].items():
                viol[k] = viol.get(k, 0) + v
        ctl[name] = {"expected_invariant": expect, "violated": viol, "expected_found": bool(cex),
                     "shortest_trace_len": len(cex[0]["trace"]) if cex else None,
                     "shortest_trace_ids": [e.get("id") for e in cex[0]["trace"]] if cex else None, "game": cex[0].get("game") if cex else None}
        if cex:
            rows.append(dict(cex[0], kind="cex", control=name, expected=expect))
    ctl_ok = all(c["expected_found"] for c in ctl.values())
    for r in res:
        rows.append({"kind": "run", **{k: r[k] for k in ("arm", "scenario", "depth", "pool", "states", "transitions", "codes", "violations", "violated",
                                                          "contested_states", "l1_checks", "seconds")}})
    for r in enf:
        for c in r["cex"]:
            rows.append(dict(c, kind="cex", control="enforced", expected=None))
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    data = {"verdict": verdict, "enforced": {"states": states, "transitions": trans, "codes": codes, "contested_states": contested,
                                             "violations": sum(r["violations"] for r in enf),
                                             "per_scenario": {r["scenario"]: {k: r[k] for k in ("depth", "pool", "states", "transitions", "violations", "seconds")} for r in enf}},
            "outcome_codes_not_reached": missing, "unreachable_by_design": UNREACHABLE, "controls": ctl, "pass_conditions_met": ok, "controls_found_expected": ctl_ok,
            "liveness_bound": "L2: attempts <= (open human-authored proposals on the requirement at the read) + 1, CORE-RULES section 7; checked inductively on every transition",
            "seconds_total": round(time.time() - t0, 1),
            "not_proven": "bounded exhaustive search (depths in params; the joint pool is the shallowest); the caps scenario runs a variant whose caps are 2 and 3 "
                          "(constants only); candidates_full not reached by BFS; four principals (two humans, one agent, one analyst)"}
    data["items"] = dict(zip(("file", "sha256"), write_items("b1_modelcheck", rows)))
    write_result("b1_modelcheck", {"depth": DEPTH, "ctl_depth": CTL_DEPTH, "principals": PRINCIPALS, "authority": AUTHORITY, "proposers": PROPOSERS,
                                   "pools": {k: [e["id"] for e in v] for k, v in POOLS.items()}, "small_caps": SMALL_CAPS}, data)
    print(verdict, "states", states, "transitions", trans, "contested", contested, "missing", missing)
    for r in enf:
        print("  enforced", r["scenario"], "depth", r["depth"], "states", r["states"], "trans", r["transitions"], "viol", r["violations"], r["violated"],
              r["seconds"], "s")
    for n, c in ctl.items():
        print("  control", n, "expected", c["expected_invariant"], "found", c["expected_found"], "len", c["shortest_trace_len"], c["shortest_trace_ids"],
              c["game"] or "")


if __name__ == "__main__":
    main()
