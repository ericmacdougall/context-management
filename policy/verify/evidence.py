"""Shared evidence layer of the verifiers (GoodQA #16096, class B of the reviews: a verifier counts IDENTITIES, not rows). Stdlib only;
imports nothing from pol, exp or tests (verify/__main__.py checks this file like a verifier).

Every row an experiment writes has a declared identity: the experiment (bound at file level: the items file is hash-checked against
that experiment's result document), the arm or class, the seed, the trial or case index, and the nonce or case id where the
experiment has one. A verifier declares, from its pinned design (THRESHOLDS.md, written into the verifier) and the declared seeds:
  - which FAMILY each row belongs to (a row that belongs to none is unclassified);
  - each family's IDENTITY function (the row's identity fields, read without defaults: a missing field leaves the row unidentified);
  - each family's EXPECTED identity set, derived from the pins and the seeds, never from the rows. For a few families the number of
    children is data dependent (events per generated log, rows per scenario, nodes of an explored tree, views per world); there the
    parent row, itself an exact member of its own expected set, declares the count, and the children must be exactly 0..count-1
    (category 2, "parent-declared": it cannot see a parent and its children shortened consistently).
It then judges ONLY a row set whose identities are exactly the expected set: a duplicate (two rows, one identity: a copy or an
overwrite), a missing identity (an omission), an extra identity (an undeclared arm, seed, class or an index outside the sequence), an
unidentified or unclassified row, a design parameter that differs from the pin, or a bad seed list, is a FAIL of the experiment with
the reason named in checks["evidence"].

Identities are TYPED: 1, True, 1.0 and "1" are four different identities (Python's 1 == True == 1.0 would otherwise let a bool or a
float stand in for a trial index). The verdict does not depend on row order; an identity names a slot of the design, so a bijective
relabelling inside the declared sequence is a reordering, not a defect (only S2's nonce binds an identity to an observation)."""
import json

SAMPLES = 8            # identities shown per reason


_EXACT = {bool: "bool", int: "int", float: "float", str: "str"}        # exact types first (type(True) is bool, never int)


def _t(x):
    tag = _EXACT.get(type(x))
    if tag is not None:
        return (tag, x)
    if isinstance(x, bool):
        return ("bool", x)
    if isinstance(x, int):
        return ("int", x)
    if isinstance(x, float):
        return ("float", x)
    if isinstance(x, str):
        return ("str", x)
    if x is None:
        return ("none",)
    if isinstance(x, (list, tuple)):
        return ("list", tuple(_t(y) for y in x))
    if isinstance(x, dict):
        return ("dict", tuple(sorted((str(k), _t(v)) for k, v in x.items())))
    raise TypeError("identity part of type %s" % type(x).__name__)


def key(*parts):
    """The typed identity of a row (or of an expected slot) from its parts."""
    return tuple(map(_t, parts))


def _show(k):
    def back(t):
        if t[0] == "none":
            return None
        if t[0] == "list":
            return [back(y) for y in t[1]]
        if t[0] == "dict":
            return {a: back(b) for a, b in t[1]}
        return t[1]
    return repr(tuple(back(t) for t in k))


def index(x):
    """A trial, case or event index: a non-negative int (never a bool, float or string). None when it is not one."""
    return x if isinstance(x, int) and not isinstance(x, bool) and x >= 0 else None


def canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"), default=repr)


class Gate:
    """Collects the evidence problems of one result document and its rows."""

    def __init__(self, experiment, doc):
        self.experiment = experiment
        self.doc = doc if isinstance(doc, dict) else {}
        self.params = self.doc.get("params") if isinstance(self.doc.get("params"), dict) else {}
        self.problems = []
        self.families = {}
        name = self.doc.get("name")
        if name is not None and name != experiment:
            self.problems.append("result document names experiment %r, not %r" % (name, experiment))

    # ------------------------------------------------------------------------------------------------ design
    def pinned(self, pins):
        """Each design parameter recorded in params must EQUAL its pin (THRESHOLDS.md); a missing one is a difference."""
        for k, v in pins.items():
            got = self.params.get(k, "<missing>")
            if canon(got) != canon(v):
                self.problems.append("params.%s = %s, pinned %s" % (k, canon(got)[:200], canon(v)[:200]))

    def seeds(self, minimum, exact=None):
        """The declared seeds (params.seeds): a list of distinct ints (not bools) or strings, at least `minimum` of them (exactly
        `exact` when given). Returns [] when the list is not acceptable (every expected set is then empty and every row extra)."""
        sd = self.params.get("seeds")
        if not isinstance(sd, list) or not sd:
            self.problems.append("params.seeds is not a non-empty list: %r" % (sd,))
            return []
        if any(isinstance(s, bool) or not isinstance(s, (int, str)) for s in sd):
            self.problems.append("params.seeds holds a value that is not an int or a string: %r" % (sd,))
            return []
        if len({key(s) for s in sd}) != len(sd):
            self.problems.append("params.seeds repeats a seed: %r" % (sd,))
            return []
        if len(sd) < minimum or (exact is not None and len(sd) != exact):
            self.problems.append("params.seeds has %d seeds, want %s %d" % (len(sd), "exactly" if exact is not None else "at least",
                                                                        exact if exact is not None else minimum))
            return []
        return list(sd)

    # ------------------------------------------------------------------------------------------------ rows
    def split(self, rows, classify):
        """{family: [rows]} by classify(row) -> family name or None. A row classify cannot place (None, or an exception) is unclassified."""
        out, bad = {}, []
        for r in rows:
            try:
                f = classify(r)
            except Exception:           # noqa: BLE001  a malformed row is unclassified, never a crash
                f = None
            if f is None:
                bad.append(r)
            else:
                out.setdefault(f, []).append(r)
        if bad:
            self.problems.append("%d unclassified row(s), e.g. %s" % (len(bad), canon(bad[0])[:160]))
        return out

    def family(self, name, rows, ident, expected):
        """Compare the identities of `rows` (ident(row) -> tuple of parts) with `expected` (a set of key(...)). Returns the rows by
        identity ({key: row}) when the family is exact, else None."""
        got, unident = {}, 0
        for r in rows:
            try:
                k = key(*ident(r))
            except Exception:           # noqa: BLE001  a missing or untyped identity field: unidentified
                unident += 1
                continue
            got.setdefault(k, []).append(r)
        dup = sorted((k for k, v in got.items() if len(v) > 1), key=_show)
        missing = sorted(expected - set(got), key=_show)
        extra = sorted(set(got) - expected, key=_show)
        rep = {"rows": len(rows), "expected": len(expected), "distinct": len(got), "duplicates": len(dup), "missing": len(missing),
               "extra": len(extra), "unidentified": unident}
        why = []
        if dup:
            why.append("duplicate identity (%d): %s" % (len(dup), ", ".join(_show(k) for k in dup[:SAMPLES])))
        if missing:
            why.append("missing identity (%d): %s" % (len(missing), ", ".join(_show(k) for k in missing[:SAMPLES])))
        if extra:
            why.append("extra identity (%d): %s" % (len(extra), ", ".join(_show(k) for k in extra[:SAMPLES])))
        if unident:
            why.append("unidentified row(s): %d" % unident)
        if not expected:
            why.append("empty expected set")
        rep["why"] = why
        self.families[name] = rep
        self.problems += ["%s: %s" % (name, w) for w in why]
        return None if why else {k: v[0] for k, v in got.items()}

    def problem(self, text):
        """A design problem found outside the identity sets (e.g. a per-row list shorter than its pinned length)."""
        self.problems.append(text)

    def not_checked(self, name, because):
        self.families[name] = {"why": ["not checked: %s" % because]}
        self.problems.append("%s: not checked (%s)" % (name, because))

    def declared_count(self, name, parent_key, n):
        """A parent's declared number of children: a non-negative int, else a problem (and 0)."""
        if index(n) is None:
            self.problems.append("%s: parent %s declares a child count that is not a non-negative int: %r" % (name, _show(parent_key), n))
            return 0
        return n

    # ------------------------------------------------------------------------------------------------ outcome
    @property
    def ok(self):
        return not self.problems

    def report(self):
        return {"ok": self.ok, "problems": self.problems[:20], "problem_count": len(self.problems), "families": self.families}

    def fail(self, **extra):
        """The verdict when the row set is not exactly the expected set: FAIL, judged no further."""
        return dict({"verdict": "FAIL", "checks": {"evidence": self.report()}}, **extra)
