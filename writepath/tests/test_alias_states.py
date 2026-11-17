"""Offline unit test (no stores): the projector alias CAS converges to exactly ONE write alias from ANY starting state,
or refuses (review C F5).

Target for tenant T, epoch E, every family f: across ALL indices, the only alias named <prefix>T-f-w<n> is
<prefix>T-f-wE, on the family's base index, with is_write_index true. fence_es must reach that in one atomic request
(every observed -w alias removed with must_exist, then -wE added) and re-read it before returning; a newer epoch
anywhere is StaleEpoch; a family with no -w alias at all is refused (ProjectionError). The starting states are
enumerated below (STATES); at 4fe3ac1 a family that already had -wE skipped its cleanup, so a stale -w(E-1) kept a
second writer, and a -w alias on another index was not seen at all.

The fake applies an alias request all or nothing, in order: a must_exist removal of an absent alias fails the whole
request (the Elasticsearch 9.5.6 behaviour the P3/P8 logs record). The families module under test is this tree's, or
the file named in FAMILIES_UNDER_TEST (failing-first: 4fe3ac1's), loaded as wp.families in a temporary package.
usage (from writepath/): python3 -m unittest tests.test_alias_states -v"""
import copy
import fnmatch
import importlib
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
T, E = "acme", 5
B, O = "BASE", "OTHER"          # placeholders in STATES: the family's base index, an index outside the family


class MetaES:
    """{index: {alias: is_write_index (True, False or None when unset)}}. `after` scripts run after each applied
    request (a concurrent actor); `apply=False` acknowledges requests without applying them."""

    def __init__(self, state, after=(), apply=True):
        self.state, self.after, self.apply, self.requests = state, list(after), apply, []

    def alias_map(self, pattern):                     # 4fe3ac1's read: names only, by index pattern
        return {i: set(a) for i, a in self.state.items() if fnmatch.fnmatch(i, pattern)}

    def alias_meta(self, index=None, name=None):
        out = {}
        for i, als in self.state.items():
            if index is not None and not fnmatch.fnmatch(i, index):
                continue
            m = {a: w for a, w in als.items() if name is None or fnmatch.fnmatch(a, name)}
            if m or name is None:
                out[i] = m
        return out

    def req(self, *a, **k):
        raise AssertionError("ensure_family should find the index")

    def aliases(self, actions):
        self.requests.append(copy.deepcopy(actions))
        new = copy.deepcopy(self.state)
        for act in actions:
            (op, m), = act.items()
            als = new.setdefault(m["index"], {})
            if op == "remove":
                if m["alias"] not in als:
                    if m.get("must_exist"):
                        raise self.ESError(404, "aliases_not_found_exception", "stub")
                    continue
                del als[m["alias"]]
            elif op == "add":
                als[m["alias"]] = m.get("is_write_index")
            else:
                raise AssertionError(op)
        if self.apply:
            self.state = new
        if self.after:
            self.after.pop(0)(self.state)
        return {"acknowledged": True}


# one family's -w aliases: [(index placeholder, epoch, is_write_index)]; expected outcome
STATES = {
    "target_already":            ([(B, E, True)], "applied"),
    "only_generation_w0":        ([(B, 0, False)], "applied"),
    "older_epoch":               ([(B, E - 1, True)], "applied"),
    "older_and_current":         ([(B, E - 1, True), (B, E, True)], "applied"),
    "w0_older_and_current":      ([(B, 0, False), (B, E - 1, True), (B, E, True)], "applied"),
    "current_not_write_index":   ([(B, E, False)], "applied"),
    "current_write_index_unset": ([(B, E, None)], "applied"),
    "current_on_other_index":    ([(O, E, True)], "applied"),
    "current_on_both_indices":   ([(B, E, True), (O, E, True)], "applied"),
    "older_on_other_index":      ([(B, E, True), (O, E - 1, True)], "applied"),
    "no_write_alias":            ([], "ProjectionError"),
    "newer_on_base":             ([(B, E - 1, True), (B, E + 1, True)], "StaleEpoch"),
    "newer_on_other_index":      ([(B, E, True), (O, E + 1, True)], "StaleEpoch"),
}


class AliasStates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        home = pathlib.Path(cls.tmp) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        cls._env = os.environ.get("WP_HOME")
        os.environ["WP_HOME"] = str(home)
        pkg = pathlib.Path(cls.tmp) / "pkg" / "wp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        for name in ("config.py", "es.py"):
            shutil.copy(HERE / "wp" / name, pkg / name)
        shutil.copy(os.environ.get("FAMILIES_UNDER_TEST") or HERE / "wp" / "families.py", pkg / "families.py")
        cls._saved = {k: v for k, v in sys.modules.items() if k == "wp" or k.startswith("wp.")}
        for k in cls._saved:
            del sys.modules[k]
        sys.path.insert(0, str(pkg.parent))
        cls.F = importlib.import_module("wp.families")
        MetaES.ESError = importlib.import_module("wp.es").ESError
        cls.FAMS = list(cls.F.FAMILIES)
        cls.OTHER = cls.F.P + T + "-legacy-copy"
        assert not any(cls.F.family_index_re(T, f).match(cls.OTHER) for f in cls.FAMS)

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(pathlib.Path(cls.tmp) / "pkg"))
        for k in [k for k in sys.modules if k == "wp" or k.startswith("wp.")]:
            del sys.modules[k]
        sys.modules.update(cls._saved)
        if cls._env is None:
            os.environ.pop("WP_HOME", None)
        else:
            os.environ["WP_HOME"] = cls._env
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # state helpers
    def world(self, per_family):
        """per_family: {family: [(B|O, epoch, is_write_index)]}; families not named are at the target."""
        F = self.F
        st = {self.OTHER: {}}
        for f in self.FAMS:
            st[F.base_index(T, f)] = {F.read_alias(T, f): False, F.erase_alias(T, f): True}
        for f in self.FAMS:
            for where, e, w in per_family.get(f, [(B, E, True)]):
                st[F.base_index(T, f) if where == B else self.OTHER][F.write_alias(T, f, e)] = w
        return st

    def walias(self, st, f):
        rx = re.compile(rf"^{re.escape(self.F.P + T)}-{re.escape(f)}-w\d+$")
        return sorted((i, a, w) for i, als in st.items() for a, w in als.items() if rx.match(a))

    def target(self, f):
        return [(self.F.base_index(T, f), self.F.write_alias(T, f, E), True)]

    def outcome(self, es):
        try:
            self.F.fence_es(es, T, E)
            return "applied"
        except Exception as ex:  # noqa: BLE001
            return type(ex).__name__

    def assert_converged(self, es):
        for f in self.FAMS:
            self.assertEqual(self.walias(es.state, f), self.target(f), f"family {f}: not exactly one -w{E} write alias")

    def assert_cas_shape(self, start, request):
        """Every -w alias of the starting state is removed with must_exist before the one add per changed family."""
        removes = {(m["index"], m["alias"]) for act in request for op, m in act.items() if op == "remove" and m.get("must_exist")}
        adds = [m for act in request for op, m in act.items() if op == "add"]
        for m in adds:
            f = next(f for f in self.FAMS if m["alias"] == self.F.write_alias(T, f, E))
            for i, a, _ in self.walias(start, f):
                self.assertIn((i, a), removes, f"{a} on {i} survives the CAS of family {f}")
            self.assertIs(m["is_write_index"], True)
            self.assertEqual(m["index"], self.F.base_index(T, f))

    def run_state(self, name):
        spec, want = STATES[name]
        start = self.world({self.FAMS[0]: spec})
        es = MetaES(copy.deepcopy(start))
        got = self.outcome(es)
        self.assertEqual(got, want, name)
        if want == "applied":
            self.assert_converged(es)
            self.assertLessEqual(len(es.requests), 1, "one atomic request")
            if es.requests:
                self.assert_cas_shape(start, es.requests[0])
            else:
                self.assertEqual(start, es.state)
        else:
            self.assertEqual(es.requests, [], "a refused or stale state sends no request")
            self.assertEqual(es.state, start)

    # mixed states across families, converged in ONE request
    def test_mixed_families_converge_in_one_request(self):
        if len(self.FAMS) < 2:
            self.skipTest("one family")
        start = self.world({self.FAMS[0]: STATES["older_and_current"][0], self.FAMS[1]: STATES["current_on_other_index"][0]})
        es = MetaES(copy.deepcopy(start))
        self.assertEqual(self.outcome(es), "applied")
        self.assert_converged(es)
        self.assertEqual(len(es.requests), 1)
        self.assert_cas_shape(start, es.requests[0])

    def test_every_family_polluted_converges(self):
        spec = STATES["w0_older_and_current"][0] + [(O, E - 2, True)]
        start = self.world({f: spec for f in self.FAMS})
        es = MetaES(copy.deepcopy(start))
        self.assertEqual(self.outcome(es), "applied")
        self.assert_converged(es)
        self.assert_cas_shape(start, es.requests[0])

    # the re-read after a successful request
    def test_stray_alias_after_success_is_converged_again(self):
        f0 = self.FAMS[0]
        stray = lambda st: st[self.OTHER].__setitem__(self.F.write_alias(T, f0, E - 1), True)  # noqa: E731
        es = MetaES(self.world({f0: [(B, E - 1, True)]}), after=[stray])
        self.assertEqual(self.outcome(es), "applied")
        self.assert_converged(es)
        self.assertEqual(len(es.requests), 2)

    def test_newer_alias_after_success_is_stale(self):
        f0 = self.FAMS[0]
        newer = lambda st: st[self.F.base_index(T, f0)].__setitem__(self.F.write_alias(T, f0, E + 1), True)  # noqa: E731
        es = MetaES(self.world({f0: [(B, E - 1, True)]}), after=[newer])
        self.assertEqual(self.outcome(es), "StaleEpoch")

    def test_acknowledged_but_not_applied_is_refused(self):
        es = MetaES(self.world({self.FAMS[0]: [(B, E - 1, True)]}), apply=False)
        self.assertEqual(self.outcome(es), "ProjectionError")
        self.assertEqual(len(es.requests), self.F.CAS_ATTEMPTS)


for _n in STATES:
    setattr(AliasStates, f"test_state_{_n}", lambda self, n=_n: self.run_state(n))


if __name__ == "__main__":
    unittest.main(verbosity=2)
