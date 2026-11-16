"""E4: an item queued under a permissive policy is refused at delivery after the policy (or the directory, or the source record)
tightens. Production path: pol.enforce.Outbox for send_model, send_inbox and export, with sessions issued by SessionAuthority.
The per-item artifact holds RAW observations per run: the enqueue decision (allow, policy version), every audit row written after the
tightening (action, allow, reason, policy version), deliveries, refusal reasons, how many recorded payloads carry the run's marker,
and, per delivery, the identity of the recipient object the sink received next to the identity of the recipient object the decision
evaluated (the recipient-snapshot check). verify/e4.py recomputes the verdict from these rows.
Control: an enqueue-time-only Outbox (one-fragment variant of pol/enforce.py: no decision at delivery).
usage: python -m exp.e4_tighten"""
import random
import time
from dataclasses import replace

from exp import common  # noqa: F401
from exp import e245_kit as K
from exp.common import write_items, write_result, lab_authority, lab_issue
from exp.seeds import seeds
from pol.directory import Directory, Principal
from pol.labels import Label
from pol.policy import ACTIONS, parse_policy
import pol.enforce as PE

R = 200
COMBOS = [("model", "model_banned"), ("model", "region_removed"), ("model", "sender_revoked"), ("model", "tenant_killed"),
          ("inbox", "model_banned"), ("inbox", "region_removed"), ("inbox", "recipient_revoked"), ("inbox", "tenant_killed"), ("inbox", "group_removed"),
          ("export", "region_removed"), ("export", "sender_revoked"), ("export", "tenant_killed"),
          ("inbox", "label_narrowed"), ("inbox", "source_erased"), ("inbox", "legal_hold_recorded")]


def policy1():
    return parse_policy({"version": 1, "grants": {"t1": list(ACTIONS)}, "models": {"m-eu-1": {"family": "m-eu", "version": "1"}},
                         "destinations": {"d_model_eu": {"kind": "model", "region": "eu", "local": True, "max_sensitivity": 3, "model": "m-eu-1",
                                                         "tenants": ["t1"]},
                                          "d_export_eu": {"kind": "export", "region": "eu", "local": False, "max_sensitivity": 3, "model": None,
                                                          "tenants": ["t1"]}}})


def one(E, seed, ci, path, tighten, i):
    rng = random.Random("%s/%d/%d" % (seed, ci, i))
    cur = [policy1()]
    rgroups = rng.choice([["g1"], ["g1", "g2"]])
    dr = Directory([Principal("s", "t1", frozenset(["g1", "g2"]), 3, True, "human", "eu", None),
                    Principal("r", "t1", frozenset(rgroups), rng.choice([2, 3]), False, "agent", "eu", "d_model_eu")])
    sessions = lab_authority(dr)
    audit = E.AuditLog()
    enf = E.Enforcer(lambda: cur[0], dr, sessions, audit)
    store = E.Store(enf)
    s = lab_issue(sessions, "s", "owner_ui")
    marker = "MK%08x" % rng.getrandbits(32)
    lab = Label(["t1"], acl=[["g1"]] + ([["g2", "g1"]] if rng.random() < 0.5 else []), regions=["eu"], sensitivity=rng.choice([0, 1, 2]),
                subjects=rng.choice([[], ["s1"]]))
    refs = [store.append(s, "payload %d %s" % (k, marker), lab) for k in range(rng.randint(1, 3))]
    sent, checked = [], []
    real = E._evaluate

    def spy(principal, action, labels, request, policy, now, recipient):
        checked.append(id(recipient) if recipient is not None else None)
        return real(principal, action, labels, request, policy, now, recipient)
    E._evaluate = spy
    try:
        def sink(sender, rcp, texts, req):
            for t in texts:
                sent.append({"text_has_marker": marker in t, "rcp_obj": id(rcp) if rcp is not None else None,
                             "checked_rcp_obj": checked[-1] if checked else None})
        action = {"model": "send_model", "export": "export", "inbox": "send_inbox"}[path]
        ob = E.Outbox(enf, store, action, sink)
        rq = {"model": {"destination": "d_model_eu"}, "export": {"destination": "d_export_eu"}, "inbox": {"recipient_id": "r"}}[path]
        ob.enqueue(s, refs, rq)
        enq = audit._snapshot()[-1]
        n0 = len(audit._snapshot())
        p1 = cur[0]
        if tighten == "model_banned":
            cur[0] = p1.with_changes({"banned_models": frozenset(["m-eu"])})
        elif tighten == "region_removed":
            dd = dict(p1.destinations)
            for k in ("d_model_eu", "d_export_eu"):
                dd[k] = replace(dd[k], region="us")
            cur[0] = p1.with_changes({"destinations": dd})
        elif tighten == "sender_revoked":
            cur[0] = p1.with_changes({"revoked": frozenset(["s"])})
        elif tighten == "recipient_revoked":
            cur[0] = p1.with_changes({"revoked": frozenset(["r"])})
        elif tighten == "tenant_killed":
            cur[0] = p1.with_changes({"deny_tenants": frozenset(["t1"])})
        elif tighten == "group_removed":
            dr.put(Principal("r", "t1", frozenset(["g9"]), 3, False, "agent", "eu", "d_model_eu"))
        elif tighten == "label_narrowed":
            r0 = store._get(refs[0])
            store._recs[refs[0]] = E.Record(r0.id, r0.tenant, r0.text, Label(["t1"], acl=[["hr"]], regions=["eu"]), r0.lineage)
        elif tighten == "source_erased":
            store.erase(s, refs[0])
        elif tighten == "legal_hold_recorded":
            r0 = store._get(refs[0])
            store._recs[refs[0]] = E.Record(r0.id, r0.tenant, r0.text, Label(["t1"], acl=[["hr"]], holds=["h1"]), r0.lineage)
        delivered, refused = ob.deliver_all()
    finally:
        E._evaluate = real
    after = [{"action": a["action"], "allow": a["allow"], "reason": a["reason"], "policy_version": a["policy_version"]} for a in audit._snapshot()[n0:]]
    return {"enqueue": {"action": enq["action"], "allow": enq["allow"], "policy_version": enq["policy_version"]}, "audit_after": after,
            "delivered": len(delivered), "refused": [x.get("refusal") for x in refused], "sent": sent}


ARMS = [("fenced", lambda: PE, True), ("positive", lambda: PE, False), ("control_enqueue_only", K.enqueue_only, True)]


def summarise(rows):
    out = {}
    for arm, _mk, _t in ARMS:
        rs = [r for r in rows if r["arm"] == arm]
        out[arm] = {"runs": len(rs), "runs_delivered": sum(1 for r in rs if r["delivered"] > 0),
                    "marker_payloads": sum(1 for r in rs for x in r["sent"] if x["text_has_marker"]),
                    "enqueue_allowed_v1": sum(1 for r in rs if r["enqueue"]["allow"] and r["enqueue"]["policy_version"] == 1),
                    "snapshot_mismatch": sum(1 for r in rs for x in r["sent"] if r["combo"].startswith("inbox") and x["rcp_obj"] != x["checked_rcp_obj"]),
                    "reasons": {}}
        for r in rs:
            for x in r["refused"]:
                out[arm]["reasons"][x] = out[arm]["reasons"].get(x, 0) + 1
    return out


def main():
    t0 = time.time()
    rows = []
    sd = seeds("e4")
    for arm, mk, tight in ARMS:
        E = mk()
        for ci, (path, t) in enumerate(COMBOS):
            for i in range(R):
                seed = sd[i % len(sd)]
                r = one(E, seed, ci, path, t if tight else None, i)
                rows.append(dict(r, arm=arm, combo="%s/%s" % (path, t), seed=seed, i=i))
    s = summarise(rows)
    f, p, c = s["fenced"], s["positive"], s["control_enqueue_only"]
    n_items_pos = sum(len(r["sent"]) for r in rows if r["arm"] == "positive")
    ok = (f["runs_delivered"] == 0 and f["marker_payloads"] == 0 and f["enqueue_allowed_v1"] == f["runs"] and p["runs_delivered"] == p["runs"]
          and p["snapshot_mismatch"] == 0 and n_items_pos == p["marker_payloads"])
    ctl = c["runs_delivered"] >= 0.9 * c["runs"]
    verdict = "PASS" if ok and ctl else "FAIL"
    data = {"verdict": verdict, "arms": s, "pass_conditions_met": ok, "control_meets_floor": ctl, "runtime_s": round(time.time() - t0, 1),
            "note": "directory, record and erasure changes (group_removed, label_narrowed, legal_hold_recorded) are logged at the unchanged policy version 1; "
                    "source_erased is refused by the purge (refusal 'source_erased') without a delivery decision",
            "not_proven": "an item already handed to a sink cannot be recalled; legal_hold_recorded is exercised as a narrowing of the source label "
                          "(a hold itself only affects erasure)"}
    data["items"] = dict(zip(("file", "sha256"), write_items("e4_tighten", rows)))
    write_result("e4_tighten", {"R": R, "combos": COMBOS, "seeds": sd}, data)
    print(verdict, s, "runtime", data["runtime_s"])


if __name__ == "__main__":
    main()
