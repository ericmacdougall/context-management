"""Independent recomputation of the E2 verdict from its raw rows (stdlib only; imports nothing from pol or exp).

Rows: payload (channel, destination attributes, tenant, markers with classes), attempt (destination attributes, marker classes of
the inputs, outcome), forge (forge kind, path, outcome, payloads produced), inject, scenario (registered stub actions, evaluated
actions from the audit log). Thresholds: THRESHOLDS.md E2 with amendments 1-3.

Evidence identity (GoodQA #16096 class B, verify/evidence.py): params.per_seed, classes and forges must equal the pins and params.seeds
(at least MIN_SEEDS) is the seed list. The scenario rows must be EXACTLY one per (arm, seed, i) for the arms of ARMS and i = 0..per_seed-1.
A scenario's child rows (attempt, inject, payload, forge) are data dependent: the scenario row declares their number in "items" and the
children must be exactly k = 0..items-1 of that (arm, seed, i) (category 2, parent-declared: a scenario and its children shortened
together are not seen). Anything else FAILs before any count is read."""
import importlib.util
import os


def _evidence():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence.py")
    spec = importlib.util.spec_from_file_location("verify_evidence", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EV = _evidence()
CLASSES = ["eu_only", "local_only", "model_banned", "restricted_channel", "other_tenant", "plain"]
OUTBOUND = ["embed", "export", "send_inbox", "send_model"]          # ACTIONS minus append, read, inject, erase
# the design (THRESHOLDS E2; exp/e2_egress.py ARMS, PER_SEED, FORGES): pinned here, never read from the judged rows
ARMS = ("enforced", "ctl_model_port_disabled", "ctl_embed_disabled", "ctl_inbox_disabled", "ctl_export_disabled", "ctl_derive_join_dropped",
        "ctl_id_pattern_guard", "info_inject_only_disabled")
FORGES = ["extra_tenant", "extra_principal", "extra_now", "extra_recipient_object", "credential_unknown_key", "foreign_recipient", "unknown_recipient",
          "other_principals_authorization", "forged_session", "other_authority_session", "ended_session", "principal_as_session"]
PER_SEED, MIN_SEEDS = 1000, 5
CHILDREN = ("attempt", "inject", "payload", "forge")


def forbidden(cls, a, tenant):
    if cls == "plain":
        return False
    if cls == "other_tenant":
        return tenant != "t2"
    if a["kind"] == "inbox":
        md = a["model_dest"]
        return {"eu_only": a["rcp_region"] != "eu" or (md is not None and md["region"] != "eu"),
                "local_only": md is not None and not md["local"],
                "model_banned": md is not None and md["family"] == "m3",
                "restricted_channel": not (a["rcp_restricted_ok"] and (md is None or md["programme"] == "cyber"))}[cls]
    return {"eu_only": a["region"] != "eu", "local_only": not a["local"], "model_banned": a["family"] == "m3",
            "restricted_channel": a["programme"] != "cyber"}[cls]


def _gate(doc, rows):
    g = EV.Gate("e2_egress", doc)
    g.pinned({"per_seed": PER_SEED, "classes": CLASSES, "forges": FORGES})
    sd = g.seeds(MIN_SEEDS)
    fam = g.split(rows, lambda r: "scenario" if r["t"] == "scenario" else "child" if r["t"] in CHILDREN else None)
    scen = g.family("scenario", fam.get("scenario", []), lambda r: (r["arm"], r["seed"], r["i"]),
                    {EV.key(a, s, i) for a in ARMS for s in sd for i in range(PER_SEED)})
    if scen is None:
        g.not_checked("child", "the scenario rows are not exact")
    else:
        g.family("child", fam.get("child", []), lambda r: (r["arm"], r["seed"], r["i"], r["k"]),
                 {EV.key(r["arm"], r["seed"], r["i"], k) for key, r in scen.items() for k in range(g.declared_count("child", key, r.get("items")))})
    return g


def verify(doc, rows):
    g = _gate(doc, rows)
    if not g.ok:
        return g.fail(agrees_with_experiment=doc["result"]["verdict"] == "FAIL")
    arms = {}
    for r in rows:
        A = arms.setdefault(r["arm"], {"scen": set(), "leak": set(), "viol": 0, "perm": dict.fromkeys(CLASSES, 0), "forb": dict.fromkeys(CLASSES, 0),
                                       "chans": set(), "forges": 0, "forge_bad": 0, "forge_payloads": 0, "idblocks": 0, "evald": set(), "reg": set()})
        k = (r["seed"], r["i"])
        if r["t"] == "scenario":
            A["scen"].add(k)
            A["evald"] |= set(r["evaluated_actions"])
            A["reg"] |= set(r["registered"])
        elif r["t"] == "payload":
            A["chans"].add(r["channel"])
            for _m, c in r["markers"]:
                if forbidden(c, r["attrs"], r["tenant"]):
                    A["viol"] += 1
                    A["leak"].add(k)
                else:
                    A["perm"][c] += 1
        elif r["t"] == "attempt":
            if r["attrs"] is not None:
                for c in r["classes"]:
                    A["forb"][c] += forbidden(c, r["attrs"], "t1")
            A["idblocks"] += r["outcome"] == "denied:id_pattern"
        elif r["t"] == "forge":
            A["forges"] += 1
            A["forge_bad"] += not r["outcome"].startswith("denied")
            A["forge_payloads"] += r["payloads"]
    e = arms["enforced"]
    checks = {
        "evidence": g.ok,
        "zero_violations": e["viol"] == 0,
        "scenarios_ge_2000": len(e["scen"]) >= 2000,
        "forbidden_attempts_ge_1000_per_class": all(e["forb"][c] >= 1000 for c in CLASSES if c != "plain"),
        "permitted_ge_1000_per_class": all(e["perm"][c] >= 1000 for c in CLASSES if c != "other_tenant"),
        "every_channel_recorded": e["chans"] == {"model", "embed", "inbox", "export"},
        "inventory_registered_equals_outbound": sorted(e["reg"]) == OUTBOUND,
        "inventory_evaluated_covers_outbound_and_inject": set(OUTBOUND) | {"inject"} <= e["evald"],
        "forges_ge_1000_all_denied_nothing_sent": e["forges"] >= 1000 and e["forge_bad"] == 0 and e["forge_payloads"] == 0,
    }
    for arm, A in arms.items():
        if arm.startswith("ctl_"):
            n = max(1, len(A["scen"]))
            ok = len(A["leak"]) >= 0.10 * n and A["viol"] >= 1
            if arm == "ctl_id_pattern_guard":
                ok = ok and A["idblocks"] >= 1
            checks["control_floor:" + arm] = ok
    verdict = "PASS" if all(checks.values()) else "FAIL"
    return {"verdict": verdict, "checks": dict(checks, evidence=g.report()), "agrees_with_experiment": verdict == doc["result"]["verdict"],
            "enforced": {"scenarios": len(e["scen"]), "violations": e["viol"], "forbidden": e["forb"], "permitted": e["perm"], "forges": e["forges"]},
            "control_leak_rates": {a: len(A["leak"]) / max(1, len(A["scen"])) for a, A in arms.items() if a.startswith("ctl_")}}
