"""Independent recomputation of E6 from its raw rows (stdlib only; imports nothing from pol or exp).
Probe observations are [allow, reason, action]."""


def verify(result_doc, rows):
    by = {}
    for r in rows:
        by.setdefault(r["arm"], []).append(r)
    empty_allows = sum(1 for r in by.get("empty", []) for v in r["returned"].values() if v[0] is True)
    empty_probes = sum(len(r["returned"]) for r in by.get("empty", []))
    mal = by.get("malformed", [])
    per_seed = {}
    for r in mal:
        per_seed[r["seed"]] = per_seed.get(r["seed"], 0) + 1
    parse_ok = sum(r["parse"] == "PolicyError" for r in mal)
    escaped = sorted({r["parse"] for r in mal if r["parse"].startswith("escaped")})
    safe_allows = sum(1 for r in mal for x in r["safe"] if x[0] is True)
    len_n = sum(len(r["lenient_ctl"]) for r in mal)
    len_allow = sum(1 for r in mal for x in r["lenient_ctl"] if x[0] is True)
    valid = by.get("valid", [])
    valid_rate = sum(r["returned"][0] is True for r in valid) / max(1, len(valid))
    valid_actions = sorted({r["returned"][2] for r in valid if r["returned"][0] is True})
    hold_ok = True
    for r in by.get("hold_pdp", []):
        a, reason = r["returned"][0], r["returned"][1]
        want = {"recorded_hold": (False, "erasure_queued"), "unrecorded_hold": (True, None), "no_hold": (True, None),
                "hold_event_removed": (True, None), "other_tenant": (False, None)}[r["case"]]
        hold_ok = hold_ok and a is want[0] and (want[1] is None or reason == want[1])
    for r in by.get("hold_join", []):
        holds = set().union(*[set(i["holds"]) for i in r["inputs"]])
        subj = set().union(*[set(i["subjects"]) for i in r["inputs"]])
        hold_ok = hold_ok and holds <= set(r["derived"]["holds"]) and subj <= set(r["derived"]["subjects"])
    erase_ok = True
    for r in by.get("erase_service", []):
        about = [t for t in r["before"] if "dana" in t]
        held = [t for t in about if t.endswith("-held")]
        # every value about dana is gone except the held one, whoever said it; values about others stay; a non-admin is refused
        erase_ok = erase_ok and sorted(r["after"]) == sorted(set(r["before"]) - set(about) | set(held)) and r["admin"]["queued"] == len(held) \
            and r["admin"]["erased"] == len(about) - len(held) and r["non_admin"].get("refused") == "unauthorized" and len(about) >= 3
    example_ok = all(r["example_flag"] is True for r in by.get("example", [])) and bool(by.get("example"))
    ok = (empty_allows == 0 and empty_probes > 0 and safe_allows == 0 and parse_ok == len(mal) and not escaped
          and min(per_seed.values() or [0]) >= 3000 and valid_rate > 0.05 and hold_ok and erase_ok and example_ok and bool(by.get("hold_pdp")))
    ctl_rate = len_allow / max(1, len_n)
    return {"verdict": "PASS" if ok and ctl_rate >= 0.10 else "FAIL",
            "checks": {"empty_allows": empty_allows, "empty_probes": empty_probes, "malformed": len(mal), "malformed_per_seed": per_seed,
                       "parse_errors": parse_ok, "escaped": escaped, "safe_allows": safe_allows, "valid_allow_rate": round(valid_rate, 4),
                       "valid_allowed_actions": valid_actions, "holds_ok": hold_ok, "erase_service_ok": erase_ok, "example_ok": example_ok,
                       "lenient_control_allow_rate": round(ctl_rate, 4)}}
