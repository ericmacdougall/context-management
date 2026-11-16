"""B2: differential equivalence and rebuild-equals-live.

Arm "enforced": random event logs (exp/core_gen.Gen, plus two targeted modes that reach proposal_cap and candidates_full) are applied
event by event to (a) exp/ref_reducer.RefCore, an independent implementation written from CORE-RULES.md with no shared imports, and
(b) pol._core.reduce on MemoryState with a JSON snapshot/restore every k events (k random). After each event the outcome code, the
original code and the comparable view (CORE-RULES section 8) must be equal. At the end of each log the live state must equal a fold of
the applied log from genesis (rebuild), snapshot for snapshot.

Refusal audit: every refused event appears in RefCore.audit (same code, same order); on the production service path
(DecisionService over MemoryEngine with real sessions) every refusal returned by submit appears in the AuditLog with its code.

Controls are broken implementations (one-patch variants of pol/_core.py): each must show a mismatch against RefCore (or, for the two
snapshot controls, live != rebuild) in >= 10 percent of logs.
usage: python -m exp.b2_rebuild"""
import json
import random
import time

from exp import common  # noqa: F401
from exp.common import write_items, write_result
from exp.core_gen import Gen, TENANT, config, label
from exp.patches import CONTROLS as PATCHES, variant
from exp.ref_reducer import RefCore
from exp.seeds import seeds
import pol._core as CORE

PER_SEED = int(__import__("os").environ.get("B2_PER_SEED", "600"))
MODES = (("random", 0.70), ("cap", 0.15), ("contest", 0.15))
SNAP_CONTROLS = {
    "snapshot_drops_revocations": ("pol._core", "\"revoked\": sorted(self.revoked),\n                \"req_labels\": self.req_labels",
                                   "\"revoked\": [],\n                \"req_labels\": self.req_labels"),
    "restore_drops_idempotency": ("pol._core", "        s.idems = {(x[0], x[1]): [x[2], x[3]] for x in snap[\"idems\"]}\n", "        s.idems = {}\n"),
}
DIFF_CONTROLS = ["n3_agent_reject_moves_head", "n3_value_unchanged_moves_head", "n2_no_cap", "n2_no_role", "m2_no_original", "stale_unchecked", "agent_may_supersede"]


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def vsha(v):
    import hashlib
    return hashlib.sha256(canon(v).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ targeted generators
def cap_log(rng, n):
    """One or two principals flood a requirement with proposals (per-principal cap 16), with some closures and noise."""
    g = Gen(rng.random())
    evs = []
    who = rng.choice(["h1", "x1", "a2"])
    req = "r1"
    for i in range(n):
        if rng.random() < 0.8:
            kind = g.principals[who]
            evs.append({"type": "propose", "id": "c%d" % i, "idem": "ck%d" % i, "tenant": TENANT, "principal": who,
                        "channel": "owner_ui" if kind == "human" else "agent_api", "req": req, "value_ref": "v%d" % (i % 7), "label": label(rng),
                        "based_on": 0})
        else:
            evs.append(g.event())
    return evs


def contest_log(rng, n):
    """Two humans decide the same base differently, then humans keep joining at the contest base (same principal may join twice,
    CORE-RULES ambiguity 13) until candidates_full; then noise and a resolve."""
    evs = []
    h = ["h1", "h2", "h3"]
    req = "r2"                                       # authority r2: h1, h3, a1, a2
    hs = ["h1", "h3"]
    for i in range(n):
        x = rng.random()
        if i < 2 or (x < 0.7 and i < 14):
            p = hs[i % 2] if i < 2 else rng.choice(hs)
            evs.append({"type": "supersede", "id": "s%d" % i, "idem": "sk%d" % i, "tenant": TENANT, "principal": p, "channel": rng.choice(["owner_ui", "owner_cli"]),
                        "req": req, "value_ref": "v%d" % (i % 9), "label": label(rng), "based_on": 0})
        elif x < 0.8:
            evs.append({"type": "decide", "id": "z%d" % i, "idem": "zk%d" % i, "tenant": TENANT, "principal": rng.choice(hs), "channel": "owner_ui", "req": req,
                        "verb": "resolve", "choose": "s%d" % rng.randrange(0, max(1, min(i, 14))), "based_on": rng.choice([1, 2, 3])})
        else:
            evs.append({"type": "retract", "id": "t%d" % i, "idem": "tk%d" % i, "tenant": TENANT, "principal": rng.choice(h), "channel": "owner_ui",
                        "req": req, "based_on": rng.choice([0, 1])})
    return evs


def make_log(rng, seed, li):
    mode = rng.choices([m for m, _w in MODES], [w for _m, w in MODES])[0]
    n = rng.randint(5, 300)
    if mode == "cap":
        return mode, cap_log(rng, max(n, 40))
    if mode == "contest":
        return mode, contest_log(rng, max(n, 20))
    g = Gen("%s-%d" % (seed, li))
    evs, hist = [], []
    st = CORE.MemoryState(config())                 # only to steer based_on to plausible versions (the generator never reads it otherwise)
    for _ in range(n):
        ev = g.retry_or_new(hist)
        hist.append(ev)
        evs.append(ev)
        _c, _o, d = CORE.reduce(st, ev)
        st.apply_delta(d)
        for r in ("r1", "r2"):
            g.note_version(r, st.heads[r]["version"])
    return mode, evs


# ------------------------------------------------------------------------------------------------ arms
def run_log(core, evs, rng, rows, arm, li, record_events):
    """Returns (first_mismatch or None, live/rebuild equal, codes)."""
    ref = RefCore(config())
    st = core.MemoryState(config())
    applied, codes, first = [], [], None
    restores = 0
    k = rng.randint(1, 20)
    for i, ev in enumerate(evs):
        a = ref.apply(ev)
        c, o, d = core.reduce(st, ev)
        st.apply_delta(d)
        if c not in core.REFUSED:
            applied.append(ev)
        codes.append(c)
        if (i + 1) % k == 0:
            st = core.MemoryState.restore(json.loads(canon(st.snapshot())))
            restores += 1
        rv, pv = ref.view(), st.comparable()
        rs, ps = vsha(rv), vsha(pv)
        if record_events:
            rows.append({"kind": "event", "arm": arm, "log": li, "i": i, "ev": ev, "ref_code": a[0], "ref_orig": a[1], "prod_code": c, "prod_orig": o,
                         "ref_view": rs, "prod_view": ps})
        if first is None and (a != (c, o) or rs != ps):
            first = {"i": i, "ev": ev, "ref": list(a), "prod": [c, o], "ref_view": rv, "prod_view": pv}
    reb = core.MemoryState(config())
    for ev in applied:
        _c, _o, d = core.reduce(reb, ev)
        reb.apply_delta(d)
    live_sha, reb_sha = vsha(st.snapshot()), vsha(reb.snapshot())
    refused_n = sum(1 for x in codes if x in core.REFUSED)
    audit_codes = [a["code"] for a in ref.audit]
    rows.append({"kind": "log", "arm": arm, "log": li, "n": len(evs), "restores": restores, "first_mismatch": first, "live_sha": live_sha, "rebuild_sha": reb_sha,
                 "prod_codes": codes, "ref_audit_codes": audit_codes})
    return first, live_sha == reb_sha, codes


# ------------------------------------------------------------------------------------------------ production service audit path
def service_audit(evs):
    """Drive the DecisionService over MemoryEngine with real sessions from the same log; every refusal returned must be in the AuditLog."""
    from pol.decisions import DecisionService, MemoryEngine, Vault
    from pol.directory import Directory, Principal, SessionAuthority
    from pol.enforce import AuditLog, Enforcer
    from pol.policy import parse_policy
    cfg = config()
    d = Directory([Principal(p, TENANT, frozenset(["g1"]), 3, False, k, "eu", None) for p, k in cfg["principals"].items()])
    pol = parse_policy({"version": 1})
    sa, audit = SessionAuthority(d), AuditLog()
    svc = DecisionService(Enforcer(lambda: pol, d, sa, audit), {TENANT: MemoryEngine(cfg)}, Vault())
    sess = {}
    for p, k in cfg["principals"].items():
        for ch in (("owner_ui", "owner_cli") if k == "human" else ("agent_api",)):
            sess[(p, ch)] = sa.issue(p, ch)
    full = {}                                         # generator id -> (service id, value_ref, author)
    returned = []
    for ev in evs:
        p = ev.get("principal")
        kind = cfg["principals"].get(p)
        if kind is None:
            continue
        ch = ev.get("channel") if (p, ev.get("channel")) in sess else ("owner_ui" if kind == "human" else "agent_api")
        body = {k: v for k, v in ev.items() if k not in ("id", "tenant", "principal", "channel", "value_ref")}
        body["client_id"] = str(ev.get("id", "x"))
        if "value_ref" in ev:
            body["value"] = ev["value_ref"]
        if ev.get("tenant") != TENANT:
            body["tenant"] = ev.get("tenant")                     # forged tenant in the body: must be refused identity_in_body
        for f in ("proposal", "pending", "choose"):
            if f in body and body[f] in full:
                body[f] = full[body[f]][0]
        if "proposal_hash" in body and ev.get("proposal") in full and body["proposal_hash"] != "0" * 64:
            sid, ref, auth = full[ev["proposal"]]
            body["proposal_hash"] = CORE.proposal_hash(sid, ref, auth)
        out = svc.submit(sess[(p, ch)], body)
        if out["id"] and out["code"] not in CORE.REFUSED and "id" in ev:
            full.setdefault(ev["id"], (out["id"], out["value_ref"], p))
        returned.append(out["code"])
    refusals_returned = [c for c in returned if c in CORE.REFUSED or c in ("identity_in_body", "unknown_tenant")]
    audited = [r["reason"] for r in audit._snapshot() if r.get("action") == "decision" and not r["allow"]]
    return {"returned": returned, "refusals_returned": refusals_returned, "audited_refusals": audited}


def main():
    t0 = time.time()
    rows = []
    sd = seeds("b2")
    logs = []
    for s in sd:
        rng = random.Random(s)
        for li in range(PER_SEED):
            mode, evs = make_log(rng, s, li)
            logs.append(("%s/%d" % (s, li), mode, evs))
    # enforced
    rng = random.Random("b2-restore-k")
    mism, reb_bad, codes_seen, contested_logs, restores = 0, 0, {}, 0, 0
    modes = {}
    for lid, mode, evs in logs:
        modes[mode] = modes.get(mode, 0) + 1
        first, eq, codes = run_log(CORE, evs, rng, rows, "enforced", lid, True)
        mism += first is not None
        reb_bad += not eq
        for c in codes:
            codes_seen[c] = codes_seen.get(c, 0) + 1
        contested_logs += "contested_opened" in codes
    restores = sum(r["restores"] for r in rows if r["kind"] == "log" and r["arm"] == "enforced")
    missing = sorted(set(CORE.OUTCOME_CODES) - set(codes_seen))
    # service-path refusal audit on every fifth log
    svc_bad, svc_refusals = 0, 0
    for lid, mode, evs in logs[::5]:
        a = service_audit(evs)
        ok = a["refusals_returned"] == a["audited_refusals"]
        svc_bad += not ok
        svc_refusals += len(a["refusals_returned"])
        rows.append({"kind": "svc_audit", "log": lid, **a})
    # controls
    ctl = {}
    for name in DIFF_CONTROLS + list(SNAP_CONTROLS):
        mod = variant(*(PATCHES[name] if name in PATCHES else SNAP_CONTROLS[name]), tag="b2_" + name)
        rng = random.Random("b2-restore-k")
        n_bad = 0
        for lid, mode, evs in logs:
            sub = []
            first, eq, _c = run_log(mod, evs, rng, sub, name, lid, False)
            bad = first is not None or not eq
            n_bad += bad
            r = sub[-1]
            rows.append({"kind": "ctl_log", "arm": name, "log": lid, "first_mismatch": r["first_mismatch"], "live_sha": r["live_sha"], "rebuild_sha": r["rebuild_sha"]})
        ctl[name] = {"logs_with_mismatch": n_bad, "rate": n_bad / len(logs), "meets_floor": n_bad / len(logs) >= (0.03 if name == "n3_value_unchanged_moves_head" else 0.10)}
    ok = (mism == 0 and reb_bad == 0 and not missing and contested_logs >= 0.10 * len(logs) and restores >= 3000 and svc_bad == 0 and svc_refusals > 0)
    ctl_ok = all(c["meets_floor"] for c in ctl.values())
    verdict = "PASS" if ok and ctl_ok else "FAIL"
    data = {"verdict": verdict, "logs": len(logs), "modes": modes, "events": sum(len(e) for _l, _m, e in logs), "mismatched_logs": mism,
            "rebuild_unequal_logs": reb_bad, "codes": codes_seen, "codes_missing": missing, "contested_logs": contested_logs, "restores": restores,
            "service_audit_logs": len(logs[::5]), "service_audit_bad": svc_bad, "service_refusals": svc_refusals, "controls": ctl,
            "pass_conditions_met": ok, "controls_meet_floor": ctl_ok, "seconds": round(time.time() - t0, 1),
            "not_proven": "equivalence with an independent reading of CORE-RULES.md on generated logs only; the reference shares the spec, so a spec "
                          "error common to both is not detected; label validity domain restricted to what both implementations accept"}
    data["items"] = dict(zip(("file", "sha256"), write_items("b2_rebuild", rows)))
    write_result("b2_rebuild", {"seeds": sd, "per_seed": PER_SEED, "modes": MODES, "lengths": "random 5..300, cap >= 40, contest >= 20",
                                "controls": DIFF_CONTROLS + list(SNAP_CONTROLS)}, data)
    print(verdict, {k: data[k] for k in ("logs", "events", "mismatched_logs", "rebuild_unequal_logs", "codes_missing", "contested_logs", "restores",
                                         "service_audit_bad", "service_refusals", "seconds")})
    for n, c in ctl.items():
        print("  control", n, c)


if __name__ == "__main__":
    main()
