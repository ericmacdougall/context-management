"""Independent recomputation of E6 from its raw rows (stdlib only; imports nothing from pol or exp).
Probe observations are [allow, reason, action].

Evidence identity (GoodQA #16096 class B, verify/evidence.py): params must equal PINS (THRESHOLDS E6) and params.seeds is the seed list.
The rows must be EXACTLY, per seed: empty, malformed and valid rows i = 0..<arm>_per_seed-1 (identity (arm, seed, i)); one hold_pdp row
per case of HOLD_CASES (identity (arm, seed, case)); one example, one hold_join and one erase_service row (identity (arm, seed)). Every
malformed row holds exactly probes_per_doc safe and lenient_ctl probes. Anything else FAILs before any counter is read."""
import importlib.util
import os


def _evidence():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence.py")
    spec = importlib.util.spec_from_file_location("verify_evidence", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EV = _evidence()
# the design (THRESHOLDS E6; exp/e6_policy_format.py): pinned here, never read from the judged document
PINS = {"malformed_per_seed": 3000, "probes_per_doc": 5, "empty_per_seed": 3000, "valid_per_seed": 3000}
MIN_SEEDS = 1
INDEXED = {"empty": "empty_per_seed", "malformed": "malformed_per_seed", "valid": "valid_per_seed"}
HOLD_CASES = ("recorded_hold", "unrecorded_hold", "no_hold", "hold_event_removed", "other_tenant")
SINGLE = ("example", "hold_join", "erase_service")


def _family(r):
    a = r["arm"]
    return "indexed" if a in INDEXED else "hold_pdp" if a == "hold_pdp" else "single"


def _gate(result_doc, rows):
    g = EV.Gate("e6_policy_format", result_doc)
    g.pinned(PINS)
    sd = g.seeds(MIN_SEEDS)
    fam = g.split(rows, _family)
    g.family("indexed", fam.get("indexed", []), lambda r: (r["arm"], r["seed"], r["i"]),
             {EV.key(a, s, i) for a, p in INDEXED.items() for s in sd for i in range(PINS[p])})
    g.family("hold_pdp", fam.get("hold_pdp", []), lambda r: (r["arm"], r["seed"], r["case"]), {EV.key("hold_pdp", s, c) for s in sd for c in HOLD_CASES})
    g.family("single", fam.get("single", []), lambda r: (r["arm"], r["seed"]), {EV.key(a, s) for a in SINGLE for s in sd})
    if g.ok:
        n = PINS["probes_per_doc"]
        short = sum(1 for r in fam["indexed"] if r["arm"] == "malformed"
                    and not (isinstance(r.get("safe"), list) and isinstance(r.get("lenient_ctl"), list) and len(r["safe"]) == len(r["lenient_ctl"]) == n))
        if short:
            g.problem("malformed: %d row(s) whose safe or lenient_ctl list is not %d probes long" % (short, n))
    return g


def verify(result_doc, rows):
    g = _gate(result_doc, rows)
    if not g.ok:
        return g.fail()
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
            "checks": {"evidence": g.report(), "empty_allows": empty_allows, "empty_probes": empty_probes, "malformed": len(mal), "malformed_per_seed": per_seed,
                       "parse_errors": parse_ok, "escaped": escaped, "safe_allows": safe_allows, "valid_allow_rate": round(valid_rate, 4),
                       "valid_allowed_actions": valid_actions, "holds_ok": hold_ok, "erase_service_ok": erase_ok, "example_ok": example_ok,
                       "lenient_control_allow_rate": round(ctl_rate, 4)}}
