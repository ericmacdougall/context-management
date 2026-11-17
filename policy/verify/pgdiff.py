"""Independent recomputation of the PG-DIFF verdict (exp/pg_diff.py) from its per-item rows. Stdlib only.

Rows: ["t", arm, unit, oracle_code, oracle_orig, pg_code, pg_orig], ["v", arm, unit, oracle_view_sha, pg_view_sha],
["l", arm, unit, oracle_applied, pg_applied, chain_ok]. Enforced arms: "b2" and "b1:<scenario>"; control arms "ctl:b2", "ctl:b1:r2".

PASS (THRESHOLDS amendment 5): in every enforced arm no event/transition differs in (code, original code) and none is an error, no
view hash differs, every B2 log's applied events (id, code, chain order) are equal and its chain verifies; the B2 arm holds exactly
per_seed x seeds logs, each with a view row; every B1 scenario of params.b1_depth is present with transitions and nodes, and the
counts equal the experiment's own summary; the run covered all scenarios. Controls (amendment 6, review-3 N8): EACH control arm has
transitions and at least one SEMANTIC difference (codes, view hash or applied log differ with no error on either side). An `error:*`
outcome is never counted as a catch; control-arm errors are reported (`errors`) so a reader sees them (the edb693f run has one: a view
of ctl:b2 log 41/6 raised TypeError on the broken variant's state)."""


def _bad_t(r):
    return (r[3], r[4]) != (r[5], r[6]) or str(r[5]).startswith("error:")


def _bad_v(r):
    return r[3] != r[4] or str(r[4]).startswith("error:")


def _bad_l(r):
    return r[3] != r[4] or not r[5]


def _err(*xs):
    return any(str(x).startswith("error:") for x in xs)


def _sem(r):
    """A control row's semantic difference: the broken rule produced another outcome, not an error."""
    if r[0] == "t":
        return not _err(r[3], r[5]) and (r[3], r[4]) != (r[5], r[6])
    if r[0] == "v":
        return not _err(r[3], r[4]) and r[3] != r[4]
    return r[3] != r[4]


def _errors(r):
    return _err(r[3], r[5]) if r[0] == "t" else _err(r[3], r[4]) if r[0] == "v" else False


def verify(doc, rows):
    res, par = doc["result"], doc["params"]
    by = {}
    for r in rows:
        by.setdefault(r[1], {"t": [], "v": [], "l": []})[r[0]].append(r)
    scen = list(par["b1_depth"])
    enforced = ["b2"] + ["b1:" + s for s in scen]
    checks, ok = {}, True
    for arm in enforced:
        a = by.get(arm, {"t": [], "v": [], "l": []})
        c = {"transitions": len(a["t"]), "views": len(a["v"]), "logs": len(a["l"]),
             "code_mismatches": sum(_bad_t(r) for r in a["t"]), "view_mismatches": sum(_bad_v(r) for r in a["v"]),
             "log_mismatches": sum(_bad_l(r) for r in a["l"]), "codes_reached": sorted({r[3] for r in a["t"]})}
        checks[arm] = c
        ok = ok and c["transitions"] > 0 and c["views"] > 0 and c["code_mismatches"] == c["view_mismatches"] == c["log_mismatches"] == 0
    b2 = checks["b2"]
    want_logs = int(par["per_seed"]) * len(par["seeds"])
    logs_ok = b2["logs"] == want_logs and len({r[2] for r in by.get("b2", {}).get("v", [])}) == want_logs
    summ = {x["scenario"]: x for x in res.get("b1", [])}
    counts_ok = (b2["transitions"] == res["b2"]["events"] and b2["logs"] == res["b2"]["logs"]
                 and all(s in summ and summ[s]["transitions"] == checks["b1:" + s]["transitions"] and summ[s]["states"] == checks["b1:" + s]["views"]
                         for s in scen))
    ctl = {}
    for arm in ("ctl:b2", "ctl:b1:r2"):
        a = by.get(arm, {"t": [], "v": [], "l": []})
        rs = a["t"] + a["v"] + a["l"]
        ctl[arm] = {"transitions": len(a["t"]), "differences": sum(map(_sem, rs)), "errors": sum(map(_errors, rs))}
    ctl_ok = all(v["transitions"] > 0 and v["differences"] > 0 for v in ctl.values())
    all_scen = par.get("scenarios_only") == "all"
    verdict = "PASS" if ok and logs_ok and counts_ok and ctl_ok and all_scen else "FAIL"
    return {"verdict": verdict, "agrees": verdict == res.get("verdict"),
            "checks": {"arms": checks, "b2_logs_expected": want_logs, "b2_logs_ok": logs_ok, "counts_equal_summary": counts_ok,
                       "all_scenarios": all_scen, "control": ctl, "control_caught": ctl_ok}}
