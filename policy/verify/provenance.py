"""Provenance gate of a STAMPED result set (review-3 N6). Stdlib only; imports nothing from the policy tree (exp/seeds.py is READ and
parsed, never imported). Called by `python -m verify` for a stamped set; its outcome is reported separately from the per-experiment
verdicts, and the overall verdict needs both.

A stamped set is accepted only when:
  - every experiment has exactly ONE non-DEV result file;
  - every stamp says dirty is False and was made AT the thresholds commit (stamp.commit == stamp.thresholds_commit: the thresholds
    were committed alone and the run used that commit, nothing in between);
  - all stamps name ONE commit and ONE thresholds digest (one run, one rule set), and that digest is the sha256 of the THRESHOLDS file
    given (default: THRESHOLDS.md of this tree; pass the file as it was at the run commit to verify an older set);
  - each experiment's params.seeds equals the STAMPED seeds of exp/seeds.py (default: this tree's; --seeds-file for an older set),
    except SEEDLESS experiments, which must record no seeds at all (b1 is exhaustive and deterministic: it never draws seeds, and
    amendment 5 leaves its reserved seed unused).
It does NOT authenticate the host or the time of the run: a stamp is written by the experiment, so the gate proves consistency of
what was recorded, not that the record is genuine."""
import ast
import hashlib

_MISSING = object()
SEEDLESS = frozenset(["b1"])


def stamped_seeds(seeds_path):
    """The STAMPED dict literal of exp/seeds.py, parsed (never imported)."""
    with open(seeds_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), seeds_path)
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "STAMPED" for t in n.targets):
            return ast.literal_eval(n.value)
    raise ValueError("no STAMPED literal in %s" % seeds_path)


def check(docs_by_name, exps, seeds_path, thresholds_path):
    """docs_by_name: {experiment name: [result doc, ...]} (non-DEV only); exps: {key: experiment name}. Returns a report dict."""
    problems = []
    try:
        stamped = stamped_seeds(seeds_path)
    except (OSError, SyntaxError, ValueError) as e:
        return {"ok": False, "problems": ["seeds file: %s: %s" % (type(e).__name__, e)]}
    with open(thresholds_path, "rb") as f:
        thr_file = hashlib.sha256(f.read()).hexdigest()
    commits, digests = set(), set()
    for key, name in exps.items():
        docs = docs_by_name.get(name, [])
        if len(docs) != 1:
            problems.append("%s: %d stamped result files (want 1)" % (name, len(docs)))
            continue
        st, par = docs[0].get("stamp") or {}, docs[0].get("params") or {}
        if st.get("dirty") is not False:
            problems.append("%s: stamp dirty=%r" % (name, st.get("dirty")))
        if not st.get("commit") or st.get("thresholds_commit") != st.get("commit"):
            problems.append("%s: run commit %s is not the thresholds commit %s" % (name, st.get("commit"), st.get("thresholds_commit")))
        commits.add(st.get("commit"))
        digests.add(st.get("thresholds_sha256"))
        want = stamped.get(key, _MISSING)
        if key in SEEDLESS:
            if "seeds" in par:
                problems.append("%s: records seeds %r but never draws any" % (name, par.get("seeds")))
        elif want is _MISSING:
            problems.append("%s: no STAMPED seeds for key %s" % (name, key))
        elif par.get("seeds") != want:
            problems.append("%s: seeds %r != stamped %r" % (name, par.get("seeds"), want))
    if len(commits) != 1:
        problems.append("results from %d commits: %s" % (len(commits), sorted(str(c) for c in commits)))
    if len(digests) != 1:
        problems.append("results under %d thresholds digests" % len(digests))
    elif thr_file not in digests:
        problems.append("thresholds digest of the results differs from %s" % thresholds_path)
    return {"ok": not problems, "problems": problems, "commit": sorted(str(c) for c in commits),
            "thresholds_sha256": sorted(str(d) for d in digests), "thresholds_file_sha256": thr_file}
