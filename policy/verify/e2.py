"""Independent recomputation of the E2 verdict from its raw rows (stdlib only; imports nothing from pol or exp).

Rows: payload (channel, destination attributes, tenant, markers with classes), attempt (destination attributes, marker classes of
the inputs, outcome), forge (forge kind, path, outcome, payloads produced), inject, scenario (registered stub actions, evaluated
actions from the audit log). Thresholds: THRESHOLDS.md E2 with amendments 1-3."""

CLASSES = ["eu_only", "local_only", "model_banned", "restricted_channel", "other_tenant", "plain"]
OUTBOUND = ["embed", "export", "send_inbox", "send_model"]          # ACTIONS minus append, read, inject, erase


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


def verify(doc, rows):
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
    return {"verdict": verdict, "checks": checks, "agrees_with_experiment": verdict == doc["result"]["verdict"],
            "enforced": {"scenarios": len(e["scen"]), "violations": e["viol"], "forbidden": e["forb"], "permitted": e["perm"], "forges": e["forges"]},
            "control_leak_rates": {a: len(A["leak"]) / max(1, len(A["scen"])) for a, A in arms.items() if a.startswith("ctl_")}}
