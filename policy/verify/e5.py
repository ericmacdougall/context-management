"""Independent recomputation of the E5 verdict from its raw rows (stdlib only; imports nothing from pol or exp).

Family A rows: world (thread pairs of edges, findings, token sources with their label dicts), view (viewer id/tenant/groups, the
serialised view, its SHA-256, the SHA-256 of the view of the mutated world). Family B rows: dworld (notice mode, authority, ground
truth per requirement: role, text, label dict), dview (viewer attributes, serialised view, SHA-256s). Thresholds: THRESHOLDS.md E5 with
amendments 2 and 3."""
import hashlib
import json
import re


def can_read_a(label, v):
    return label["tenants"] == [v["tenant"]] and all(set(c) & set(v["groups"]) for c in label["acl"])


def can_read_b(label, v):
    return (label["tenants"] == ["t1"] and all(set(c) & set(v["groups"]) for c in label["acl"]) and v["clearance"] >= label["sensitivity"]
            and (not label["restricted"] or v["restricted_ok"]) and (label["regions"] is None or v["region"] in label["regions"]))


def compute(rows):
    worlds, arms = {}, {}
    for r in rows:
        if r["t"] in ("world", "dworld"):
            worlds[(r["arm"], r["seed"], r["i"])] = r
    for r in rows:
        if r["t"] not in ("view", "dview"):
            continue
        k = (r["arm"], r["seed"], r["i"])
        w = worlds[k]
        A = arms.setdefault(r["arm"], {"worlds": set(), "leak_worlds": set(), "interference_worlds": set(), "views": 0, "leaks": 0, "bad_sha": 0,
                                       "key_rule_worlds": set(), "flag_wrong": 0, "flags_true": 0, "hidden_elements": 0, "cross_conflict": set(),
                                       "findings": 0, "finding_hidden": 0, "finding_visible": 0, "xrefs": 0})
        A["worlds"].add(k)
        A["views"] += 1
        blob = r["blob"]
        A["bad_sha"] += hashlib.sha256(blob.encode()).hexdigest() != r["blob_sha256"]
        if r["mutated_sha256"] != r["blob_sha256"]:
            A["interference_worlds"].add(k)
        v = r["viewer"]
        if r["t"] == "view":
            forb = [t for s in w["sources"] if not all(can_read_a(l, v) for l in s["labels"]) for t in s["tokens"]]
            for s in w["sources"]:
                if s["tokens"][0].startswith("FIND-"):
                    if all(can_read_a(l, v) for l in s["labels"]):
                        A["finding_visible"] += 1
                    else:
                        A["finding_hidden"] += 1
            if any(t in blob for t in forb):
                A["leaks"] += 1
                A["leak_worlds"].add(k)
            A["xrefs"] += len(json.loads(blob)["xrefs"])
            if any(a != b and t == "conflicts_with" for a, b, t in w["edges"] if a != "t2thr" and b != "t2thr"):
                A["cross_conflict"].add(k)
        else:
            view = json.loads(blob)
            opted = w["notice"] == "existence_only"
            leaked = False
            for req, els in w["truth"].items():
                hidden = [e for e in els if e["label"] is not None and not can_read_b(e["label"], v)]
                A["hidden_elements"] += len(hidden)
                for e in hidden:
                    m = re.search(r"MK-[0-9a-z-]+", e["text"] or "")
                    if m and m.group(0) in blob:
                        leaked = True
                ex = view["explain"].get(req)
                if ex is None:
                    continue
                if ("hidden_candidates" in ex) != opted:
                    A["key_rule_worlds"].add(k)
                if opted:
                    A["flags_true"] += ex.get("hidden_candidates") is True
                    A["flag_wrong"] += ex.get("hidden_candidates") != bool(hidden)
            for t in view["table"]:
                if ("hidden_candidates" in t) != opted:
                    A["key_rule_worlds"].add(k)
            if leaked:
                A["leaks"] += 1
                A["leak_worlds"].add(k)
    for r in rows:
        if r["t"] == "world" and r["arm"] in arms:
            arms[r["arm"]]["findings"] += r["findings"]
    out = {a: {"worlds": len(A["worlds"]), "views": A["views"], "leak_worlds": len(A["leak_worlds"]), "leaks": A["leaks"],
               "interference_worlds": len(A["interference_worlds"]), "bad_sha": A["bad_sha"], "key_rule_worlds": len(A["key_rule_worlds"]),
               "flag_wrong": A["flag_wrong"], "flags_true": A["flags_true"], "hidden_elements": A["hidden_elements"],
               "cross_conflict_worlds": len(A["cross_conflict"]), "findings": A["findings"], "finding_hidden": A["finding_hidden"],
               "finding_visible": A["finding_visible"], "xrefs": A["xrefs"]} for a, A in arms.items()}
    e, n0, n1 = out["A_enforced"], out["B_notice_none"], out["B_notice_existence_only"]
    checks = {
        "A_no_leak": e["leaks"] == 0, "A_no_interference": e["interference_worlds"] == 0, "A_worlds_ge_1000": e["worlds"] >= 1000,
        "A_cross_conflict_ge_90pct": e["cross_conflict_worlds"] >= 0.9 * e["worlds"], "A_findings_ge_1_per_world": e["findings"] >= e["worlds"],
        "A_findings_hidden_and_visible": e["finding_hidden"] > 0 and e["finding_visible"] > 0, "A_xrefs_visible": e["xrefs"] > 0,
        "B_none_no_leak_no_key_no_interference": n0["leaks"] == 0 and n0["key_rule_worlds"] == 0 and n0["interference_worlds"] == 0 and n0["worlds"] >= 1000,
        "B_optin_no_leak_key_and_flag_exact": n1["leaks"] == 0 and n1["key_rule_worlds"] == 0 and n1["flag_wrong"] == 0 and n1["interference_worlds"] == 0,
        "B_optin_notices_ge_1000": n1["flags_true"] >= 1000,
        "sha_bound": all(x["bad_sha"] == 0 for x in out.values()),
    }
    for a, x in out.items():
        if "_ctl_" in a and a != "B_ctl_unscoped_value":            # informational (see exp/e5_threads.no_read_check)
            bad = max(x["leak_worlds"], x["key_rule_worlds"])
            checks["control_floor:" + a] = bad >= 0.10 * max(1, x["worlds"])
    return {"verdict": "PASS" if all(checks.values()) else "FAIL", "checks": checks, "arms": out}


def verify(doc, rows):
    out = compute(rows)
    out["agrees_with_experiment"] = out["verdict"] == doc["result"]["verdict"]
    return out
