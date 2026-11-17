"""Property tests of the evidence identity rules (GoodQA #16096, class B of the reviews: verifiers counted rows, not identities), shared
by tests/test_evidence_<x>.py. Stdlib only. Each test calls ONLY verify(doc, rows) of the verifier under test, loaded BY PATH as
verify/__main__.py loads it, so the same tests run unchanged against the verifiers at ea4677a (red) and after the shared evidence layer
(green). They never import verify/evidence.py.

The honest synthetic row set of each module is built from the experiment's declared design (the THRESHOLDS.md sizes and the identity
fields the experiment writes) and must PASS. Then, per row family and on one row of it:
  drop       the row removed                                                        -> FAIL, "missing identity"
  duplicate  a byte-identical copy of the row added                                 -> FAIL, "duplicate identity"
  relabel    the row given the identity of another row of its family                -> FAIL, "duplicate identity"
             (and, with a seed field, the row's seed replaced by another declared seed: a seed copied from another)
  renumber   the row's index moved OUT of the declared sequence: one past the end, -1, and the same number as a string, a float
             or a bool                                                              -> FAIL, "missing identity"
  extra      a copy added with an identity outside the design: an index far past the end, an undeclared seed, an undeclared arm
                                                                                    -> FAIL, "extra identity"
  overwrite  (where the module gives break_row) the row broken, then its passing original added after it, or before it
                                                                                    -> FAIL, "duplicate identity"
  shrink     (where the module gives shrunk) the design parameter edited in the result document with the rows cut to match
                                                                                    -> FAIL (the target comes from THRESHOLDS, not from the
                                                                                       judged document)
Order: a permutation of the rows (seeded shuffle, and the reversed list) gives the SAME verdict as the honest set. The brief's word
"reorder" is read as RENUMBERING identities (above); a bijective swap of two in-range indices names the same design slots and is not a
defect (only S2's nonce binds an identity to an observation), so no test claims it fails."""
import importlib.util
import json
import os
import random

POLICY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE = {}


def load(name):
    spec = importlib.util.spec_from_file_location("evidence_props_verify_" + name, os.path.join(POLICY, "verify", name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Fam:
    """A row family: pick(row) selects it; ident lists the identity fields (dict keys, or list positions for list rows); index is the
    field holding the trial/case/event index (None when the family has none); seed and arm name those fields when present."""

    def __init__(self, name, pick, ident, index=None, seed=None, arm=None, end=None):
        self.name, self.pick, self.ident, self.index, self.seed, self.arm, self.end = name, pick, ident, index, seed, arm, end


def _copy(r):
    return dict(r) if isinstance(r, dict) else list(r)


def _get(r, f):
    try:
        return r[f]
    except (KeyError, IndexError, TypeError):
        return None


def _set(r, f, v):
    c = _copy(r)
    c[f] = v
    return c


def _ev(out):
    return json.dumps((out or {}).get("checks", {}).get("evidence"), sort_keys=True, default=repr)


class Props:
    """Mixin for unittest.TestCase. A module sets V (the loaded verifier), FAMILIES, and defines doc() and build() (the honest rows);
    optionally break_row(row) -> a failing copy, and shrunk() -> [(label, doc, rows)]."""
    V = None
    FAMILIES = ()

    def doc(self):
        raise NotImplementedError

    def build(self):
        raise NotImplementedError

    def break_row(self, fam, row):
        return None

    def shrunk(self):
        return []

    # ------------------------------------------------------------------------------------------------ helpers
    def rows(self):
        k = type(self).__name__
        if k not in _CACHE:
            _CACHE[k] = self.build()
        return _CACHE[k]

    def v(self, rows, doc=None):
        return self.V.verify(doc if doc is not None else self.doc(), rows)

    def bad(self, rows, what, reasons=(), doc=None):
        out = self.v(rows, doc)
        self.assertNotEqual(out.get("verdict"), "PASS", "%s: verdict PASS" % what)
        ev = _ev(out)
        for word in reasons:
            self.assertIn(word, ev, "%s: verdict %s, reason %r not named" % (what, out.get("verdict"), word))

    def positions(self, fam):
        rows = self.rows()
        return [i for i, r in enumerate(rows) if fam.pick(r)]

    def sample(self, fam):
        """(position of the sampled row, position of another row of the family with a different identity or None)."""
        pos = self.positions(fam)
        self.assertTrue(pos, "family %s is empty in the honest set" % fam.name)
        rows = self.rows()
        i = pos[len(pos) // 2]
        me = [_get(rows[i], f) for f in fam.ident]
        other = next((j for j in pos if [_get(rows[j], f) for f in fam.ident] != me), None)
        return i, other

    # ------------------------------------------------------------------------------------------------ tests
    def test_honest_set_passes(self):
        out = self.v(self.rows())
        self.assertEqual(out.get("verdict"), "PASS", json.dumps(out.get("checks"), default=repr, sort_keys=True)[:3000])

    def test_permutation_keeps_the_verdict(self):
        rows = list(self.rows())
        want = self.v(rows).get("verdict")
        random.Random(16096).shuffle(rows)
        self.assertEqual(self.v(rows).get("verdict"), want, "shuffled")
        self.assertEqual(self.v(list(reversed(self.rows()))).get("verdict"), want, "reversed")

    def test_drop(self):
        rows = self.rows()
        for fam in self.FAMILIES:
            with self.subTest(family=fam.name):
                i, _o = self.sample(fam)
                self.bad(rows[:i] + rows[i + 1:], "drop %s" % fam.name, ("missing identity",))

    def test_duplicate(self):
        rows = self.rows()
        for fam in self.FAMILIES:
            with self.subTest(family=fam.name):
                i, _o = self.sample(fam)
                self.bad(rows + [_copy(rows[i])], "duplicate %s" % fam.name, ("duplicate identity",))

    def test_relabel(self):
        rows = self.rows()
        sd = self.doc().get("params", {}).get("seeds") or []
        for fam in self.FAMILIES:
            i, o = self.sample(fam)
            if o is not None:
                with self.subTest(family=fam.name, relabel="identity of another row"):
                    c = _copy(rows[i])
                    for f in fam.ident:
                        c[f] = rows[o][f]
                    self.bad(rows[:i] + [c] + rows[i + 1:], "relabel %s" % fam.name, ("duplicate identity", "missing identity"))
            if fam.seed is not None and len(sd) > 1:
                with self.subTest(family=fam.name, relabel="seed"):
                    s = _get(rows[i], fam.seed)
                    other = next(x for x in sd if x != s)
                    self.bad(rows[:i] + [_set(rows[i], fam.seed, other)] + rows[i + 1:], "seed relabel %s" % fam.name, ("missing identity",))

    def test_renumber_out_of_sequence(self):
        rows = self.rows()
        for fam in self.FAMILIES:
            if fam.index is None:
                continue
            i, _o = self.sample(fam)
            n = _get(rows[i], fam.index)
            end = fam.end(self, rows[i]) if fam.end else None
            cases = [("past the end", end), ("-1", -1), ("string", str(n)), ("float", float(n))]
            if n in (0, 1):
                cases.append(("bool", bool(n)))
            for label, val in cases:
                if val is None:
                    continue
                with self.subTest(family=fam.name, renumber=label):
                    self.bad(rows[:i] + [_set(rows[i], fam.index, val)] + rows[i + 1:], "renumber %s %s" % (fam.name, label), ("missing identity",))

    def test_extra(self):
        rows = self.rows()
        for fam in self.FAMILIES:
            i, _o = self.sample(fam)
            cases = []
            if fam.index is not None:
                cases.append(("index far past the end", fam.index, 10 ** 7))
            if fam.seed is not None:
                cases.append(("undeclared seed", fam.seed, "seed-not-declared"))
            if fam.arm is not None:
                cases.append(("undeclared arm", fam.arm, "ctl_made_up"))
            for label, f, val in cases:
                with self.subTest(family=fam.name, extra=label):
                    self.bad(rows + [_set(rows[i], f, val)], "extra %s %s" % (fam.name, label), ("extra identity",))

    def test_overwrite(self):
        rows = self.rows()
        for fam in self.FAMILIES:
            i, _o = self.sample(fam)
            b = self.break_row(fam, rows[i])
            if b is None:
                continue
            with self.subTest(family=fam.name, overwrite="broken row alone"):
                out = self.v(rows[:i] + [b] + rows[i + 1:])
                self.assertEqual(out.get("verdict"), "FAIL", "the broken row must fail on its own (test precondition)")
            with self.subTest(family=fam.name, overwrite="broken first, passing original later"):
                self.bad(rows[:i] + [b] + rows[i + 1:] + [_copy(rows[i])], "overwrite %s (later)" % fam.name, ("duplicate identity",))
            with self.subTest(family=fam.name, overwrite="passing original first, broken later"):
                self.bad(rows + [b], "overwrite %s (earlier)" % fam.name, ("duplicate identity",))

    def test_design_comes_from_the_pins(self):
        for label, doc, rows in self.shrunk():
            with self.subTest(shrink=label):
                self.bad(rows, "shrunk design: %s" % label, (), doc=doc)
