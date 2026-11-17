"""Offline unit test (no stores): families.fence_es classifies a failed alias CAS (THRESHOLDS amendment 21, review-2 N1).

  newer write alias visible after the failure     -> StaleEpoch (a confirmed loss)
  aliases unchanged after the failure             -> ProjectionError (infrastructure; at 1fcf3f7: StaleEpoch)
  aliases changed with no newer epoch             -> observe again and retry; at most 3 requests, then ProjectionError
The ES client is a stub holding {index: aliases}; WP_HOME is a temporary home with this stream's conf. The families
module under test is this tree's wp/families.py, or the file named in FAMILIES_UNDER_TEST (failing-first: 1fcf3f7's),
loaded as wp.families inside a temporary package next to this tree's config.py and es.py.
usage (from writepath/): python3 -m unittest discover -s tests -v"""
import fnmatch
import importlib
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-writepath", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-writepath-pg", "es_container": "emlab2-writepath-es"}
T, E = "acme", 5


class FakeES:
    """{index: set(aliases)}; `on_aliases` scripts each alias request: a callable(state, actions) that may change the
    state and returns an exception to raise, or None to apply the actions."""

    def __init__(self, F, state, on_aliases):
        self.F, self.state, self.on, self.calls = F, state, list(on_aliases), 0

    def alias_map(self, pattern):
        return {i: set(a) for i, a in self.state.items() if fnmatch.fnmatch(i, pattern)}

    def req(self, *a, **k):
        raise AssertionError("ensure_family should find the index")

    def aliases(self, actions):
        self.calls += 1
        script = self.on.pop(0) if self.on else (lambda s, a: None)
        err = script(self.state, actions)
        if err is not None:
            raise err
        for act in actions:
            (op, m), = act.items()
            (self.state[m["index"]].discard if op == "remove" else self.state[m["index"]].add)(m["alias"])


class FenceClassification(unittest.TestCase):
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
        cls.ESError = importlib.import_module("wp.es").ESError

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

    def state(self, epoch):
        F = self.F
        return {F.base_index(T, f): {F.read_alias(T, f), F.erase_alias(T, f), F.write_alias(T, f, epoch)} for f in F.FAMILIES}

    def move(self, state, new_epoch):
        F = self.F
        for f in F.FAMILIES:
            idx = F.base_index(T, f)
            state[idx] = {a for a in state[idx] if not a.startswith(F.write_alias(T, f, ""))} | {F.write_alias(T, f, new_epoch)}

    def outcome(self, es):
        try:
            self.F.fence_es(es, T, E)
            return "applied"
        except Exception as ex:  # noqa: BLE001
            return type(ex).__name__

    def err(self):
        return self.ESError(503, "unavailable_shards_exception", "stub")

    # both versions
    def test_success_applies_the_cas(self):
        es = FakeES(self.F, self.state(E - 1), [])
        self.assertEqual(self.outcome(es), "applied")
        self.assertEqual(es.state, self.state(E))

    def test_newer_alias_before_the_cas_is_stale(self):
        es = FakeES(self.F, self.state(E + 1), [])
        self.assertEqual(self.outcome(es), "StaleEpoch")
        self.assertEqual(es.calls, 0)

    def test_newer_alias_after_a_failed_cas_is_stale(self):
        es = FakeES(self.F, self.state(E - 1), [lambda s, a: (self.move(s, E + 1), self.err())[1]])
        self.assertEqual(self.outcome(es), "StaleEpoch")

    # review-2 N1: fail on 1fcf3f7
    def test_unchanged_aliases_after_a_failed_cas_is_infrastructure(self):
        es = FakeES(self.F, self.state(E - 1), [lambda s, a: self.err()])
        self.assertEqual(self.outcome(es), "ProjectionError")
        self.assertEqual(es.state, self.state(E - 1))

    def test_changed_aliases_without_a_newer_epoch_are_retried(self):
        es = FakeES(self.F, self.state(E - 2), [lambda s, a: (self.move(s, E - 1), self.err())[1]])
        self.assertEqual(self.outcome(es), "applied")
        self.assertEqual(es.calls, 2)
        self.assertEqual(es.state, self.state(E))

    def test_retries_are_bounded(self):
        es = FakeES(self.F, self.state(E - 4), [lambda s, a, n=n: (self.move(s, n), self.err())[1] for n in (E - 3, E - 2, E - 1)])
        self.assertEqual(self.outcome(es), "ProjectionError")
        self.assertEqual(es.calls, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
