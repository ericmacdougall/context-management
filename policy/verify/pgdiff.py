"""Independent recomputation of the PG-DIFF verdict (exp/pg_diff.py) from its per-item rows. Stdlib only.

Rows: ["t", arm, unit, oracle_code, oracle_orig, pg_code, pg_orig], ["v", arm, unit, oracle_view_sha, pg_view_sha],
["l", arm, unit, oracle_applied, pg_applied, chain_ok]. Enforced arms: "b2" and "b1:<scenario>"; control arms "ctl:b2", "ctl:b1:r2".

PASS (THRESHOLDS amendment 5): in every enforced arm no event/transition differs in (code, original code) and none is an error, no
view hash differs, every B2 log's applied events (id, code, chain order) are equal and its chain verifies; the B2 arm holds exactly
per_seed x seeds logs, each with a view row; every B1 scenario of params.b1_depth is present with transitions and nodes, and the
counts equal the experiment's own summary; the run covered all scenarios. Controls (amendment 6, review-3 N8): EACH control arm has
transitions and at least one SEMANTIC difference (codes, view hash or applied log differ with no error on either side). An `error:*`
outcome is never counted as a catch; control-arm errors are reported (`errors`) so a reader sees them (the edb693f run has one: a view
of ctl:b2 log 41/6 raised TypeError on the broken variant's state).

Evidence identity (GoodQA #16096 class B, verify/evidence.py). The rows carry identity fields appended by exp/pg_diff.py:
  ["t", arm, log, ..., i] and ["l", arm, log, ..., n] in arms b2 and ctl:b2 (i the event's index in its log, n the log's event count);
  ["v", arm, level, ..., node] and ["t", arm, level, ..., node, ti, child] in arms b1:<s> and ctl:b1:r2 (node the tree node's id, root
  0; ti the template's index in the scenario's pool; child the id of the node the transition discovered, or null).
params.per_seed, b1_depth and scenarios_only must equal the pins (THRESHOLDS amendment 5: 60 logs per seed, every scenario at its B1
depth, all scenarios), with at least 5 seeds. The B2 logs are "<seed>/<li>", li = 0..per_seed-1, in arm b2, and the first
max(10, logs // 10) of them in arm ctl:b2 (the experiment's own rule); each has exactly one v and one l row and t rows i = 0..n-1, n
declared by its l row. A B1 tree (arms b1:<s> for every pinned scenario, ctl:b1:r2 the r2 tree) is checked by closure from its root:
its v rows are exactly node 0 and the children its t rows declare; its t rows are exactly ti = 0..P-1 for every node above the depth,
P the scenario's template pool (len(exp/b1_modelcheck.POOLS[s]), pinned below); each child is declared once and lies one level below
its transition, whose level is its node's, the root at level 0. Both counts are category 2 (parent-declared): a tree pruned
consistently is not detected. Anything else FAILs before any count is read."""
import importlib.util
import os


def _evidence():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence.py")
    spec = importlib.util.spec_from_file_location("verify_evidence", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EV = _evidence()
# the design (THRESHOLDS amendment 5; exp/pg_diff.py PER_SEED, exp/b1_modelcheck.py DEPTH and POOLS): pinned here, never read from the rows
PER_SEED, MIN_SEEDS = 60, 5
B1_DEPTH = {"r1": 6, "r2": 8, "r3": 8, "caps": 8, "joint": 4}
POOL = {"r1": 36, "r2": 20, "r3": 25, "caps": 13, "joint": 81}          # joint = r1 + r2 + r3
B2_ARMS = ("b2", "ctl:b2")
B1_ARMS = dict([("b1:" + s, s) for s in B1_DEPTH] + [("ctl:b1:r2", "r2")])


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


def _family(r):
    b1 = isinstance(r[1], str) and r[1].startswith(("b1:", "ctl:b1:"))
    return ({"t": "b1 transition", "v": "b1 node"} if b1 else {"t": "b2 event", "v": "b2 view", "l": "b2 log"}).get(r[0])


def _tail(r, n):
    """The identity field n of a list row; a row too short to have it is unidentified (KeyError)."""
    if not isinstance(r, list) or len(r) <= n:
        raise KeyError(n)
    return r[n]


def _b1_gate(g, fam):
    """Closure from the root: expected nodes from the transitions' children, expected transitions from the nodes' levels."""
    tr, nd = fam.get("b1 transition", []), fam.get("b1 node", [])
    roots = {EV.key(a, 0) for a in B1_ARMS}
    declared = {}
    for r in tr:
        try:
            if r[1] in B1_ARMS and _tail(r, 9) is not None:
                c = EV.key(r[1], r[9])
                declared[c] = declared.get(c, 0) + 1
        except Exception:               # noqa: BLE001  unidentified: the transition family names it
            pass
    for c, m in sorted(declared.items(), key=lambda x: EV._show(x[0])):
        if m > 1 or c in roots:
            g.problem("b1 tree: node %s is the child of %d transitions%s" % (EV._show(c), m, " (it is the root)" if c in roots else ""))
    want_nodes = roots | set(declared)
    want_tr = set()
    for r in nd:
        try:
            a, lv, n = r[1], r[2], _tail(r, 5)
            d = B1_DEPTH[B1_ARMS[a]]
        except Exception:               # noqa: BLE001  an undeclared arm or a short row: the node family names it
            continue
        if EV.index(lv) is not None and lv < d:
            want_tr |= {EV.key(a, n, ti) for ti in range(POOL[B1_ARMS[a]])}
    nodes = g.family("b1 node", nd, lambda r: (r[1], _tail(r, 5)), want_nodes)
    edges = g.family("b1 transition", tr, lambda r: (r[1], _tail(r, 7), _tail(r, 8)), want_tr)
    if nodes is None or edges is None:
        g.not_checked("b1 tree", "the node or transition rows are not exact")
        return
    level = {k: r[2] for k, r in nodes.items()}
    for k, lv in level.items():
        d = B1_DEPTH[B1_ARMS[k[0][1]]]
        if EV.index(lv) is None or lv > d:
            g.problem("b1 tree: node %s at level %r, outside 0..%d" % (EV._show(k), lv, d))
    for a in B1_ARMS:
        if EV.key(level.get(EV.key(a, 0))) != EV.key(0):
            g.problem("b1 tree: the root of %s is at level %r, not 0" % (a, level.get(EV.key(a, 0))))
    for k, r in edges.items():
        own = EV.key(r[1], r[7])
        if EV.key(r[2]) != EV.key(level.get(own)):
            g.problem("b1 tree: transition %s at level %r, its node at level %r" % (EV._show(k), r[2], level.get(own)))
        c = None if r[9] is None else EV.key(r[1], r[9])
        if c is not None and EV.index(level[c]) is not None and EV.index(r[2]) is not None and level[c] != r[2] + 1:
            g.problem("b1 tree: node %s at level %r is the child of transition %s at level %r" % (EV._show(c), level[c], EV._show(k), r[2]))


def _gate(doc, rows):
    g = EV.Gate("pg_diff", doc)
    g.pinned({"per_seed": PER_SEED, "b1_depth": B1_DEPTH, "scenarios_only": "all"})
    sd = g.seeds(MIN_SEEDS)
    ids = ["%s/%d" % (s, li) for s in sd for li in range(PER_SEED)]
    want_logs = {EV.key("b2", lid) for lid in ids} | {EV.key("ctl:b2", lid) for lid in ids[:max(10, len(ids) // 10)]}
    fam = g.split(rows, _family)
    logs = g.family("b2 log", fam.get("b2 log", []), lambda r: (r[1], r[2]), want_logs)
    g.family("b2 view", fam.get("b2 view", []), lambda r: (r[1], r[2]), want_logs)
    if logs is None:
        g.not_checked("b2 event", "the log rows are not exact")
    else:
        g.family("b2 event", fam.get("b2 event", []), lambda r: (r[1], r[2], _tail(r, 7)),
                 {EV.key(r[1], r[2], i) for k, r in logs.items() for i in range(g.declared_count("b2 event", k, r[6] if len(r) > 6 else None))})
    _b1_gate(g, fam)
    return g


def verify(doc, rows):
    res, par = doc["result"], doc["params"]
    g = _gate(doc, rows)
    if not g.ok:
        return g.fail(agrees=res.get("verdict") == "FAIL")
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
            "checks": {"evidence": g.report(), "arms": checks, "b2_logs_expected": want_logs, "b2_logs_ok": logs_ok, "counts_equal_summary": counts_ok,
                       "all_scenarios": all_scen, "control": ctl, "control_caught": ctl_ok}}
