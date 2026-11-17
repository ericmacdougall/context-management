"""Offline unit test (no stores): the alias generation invariant under interleavings (review B2 at 76c9276).

One SHARED simulated cluster (index creation is create-once; an _aliases request whose `remove` has must_exist=true
fails as a whole when the alias is absent: probe P3 on 9.5.6) and several Broker objects with their own clients, as
concurrent broker connections have. Request A (tenant acme, epoch 1, holder hA) is paused at one point while other
requests run to completion, then resumed:
  pause points   before_create (A saw no index), before_cas (A observed and built its CAS), before_recheck (A's CAS
                 applied, before its second lease check)
  sequences      B: epoch 1 again (same lease, a retry); C: takeover to epoch 2; D: takeover to epoch 3
Invalidation of older keys answers 500 throughout (the design does not depend on it). After each scenario:
  I1  every family has exactly ONE write alias (-w<n>)
  I2  that alias is the newest epoch whose CAS completed (no older epoch's alias is ever reinstated)
  I3  A obtained a key only if its lease was current at its re-check
At 76c9276 / 0110981 (families.py adds -w<E> unconditionally to a family with no write alias) the before_cas
scenarios reinstate -w1 beside -w2: the code-level counterexample in review B2.
The wp package under test is this tree's, or the directory WP_PKG_UNDER_TEST (failing-first: an older wp/).
usage (from writepath/): python3 -m unittest tests.test_alias_generation -v"""
import fnmatch
import importlib
import itertools
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "tests"))
from test_broker import GOOD, _psycopg_stub  # noqa: E402

T = "acme"


class Cluster:
    """index -> {alias: is_write_index}; API keys are counted, never stored."""

    def __init__(self, ESError):
        self.ESError, self.idx, self.minted = ESError, {}, []

    def alias_map(self, pattern):
        return {i: set(a) for i, a in self.idx.items() if fnmatch.fnmatch(i, pattern)}

    def alias_meta(self, index=None, name=None):
        return {i: {a: w for a, w in als.items() if name is None or fnmatch.fnmatch(a, name)}
                for i, als in self.idx.items() if (index is None or fnmatch.fnmatch(i, index))
                and (name is None or any(fnmatch.fnmatch(a, name) for a in als))}

    def create(self, name, aliases):
        if name in self.idx:
            raise self.ESError(400, "resource_already_exists_exception", "PUT")
        self.idx[name] = {a: bool(m.get("is_write_index")) for a, m in aliases.items()}

    def aliases(self, actions):
        for act in actions:                       # P3: must_exist failures reject the WHOLE request, nothing applied
            (op, m), = act.items()
            if op == "remove" and m.get("must_exist") and m["alias"] not in self.idx.get(m["index"], {}):
                raise self.ESError(404, "aliases_not_found_exception", "_aliases")
        for act in actions:
            (op, m), = act.items()
            if op == "remove":
                self.idx[m["index"]].pop(m["alias"], None)
            else:
                self.idx[m["index"]][m["alias"]] = bool(m.get("is_write_index"))


class Client:
    """One broker connection's ES client over the shared cluster, with one optional pause hook."""

    def __init__(self, cluster, pause=None, at=None):
        self.c, self.pause, self.at = cluster, pause, at

    def _maybe(self, point):
        if self.pause and self.at == point:
            p, self.pause = self.pause, None
            p()

    def alias_map(self, pattern):
        return self.c.alias_map(pattern)

    def alias_meta(self, index=None, name=None):
        return self.c.alias_meta(index, name)

    def aliases(self, actions):
        self._maybe("before_cas")
        self.c.aliases(actions)

    def req(self, method, path, body=None, ok=(200, 201), **params):
        if method == "PUT" and path.count("/") == 1:
            self._maybe("before_create")
            self.c.create(path[1:], body.get("aliases", {}))
            return 200, {}
        if (method, path) == ("POST", "/_security/api_key"):
            self.c.minted.append(body["metadata"]["epoch"])
            return 200, {"id": f"k{len(self.c.minted)}", "encoded": "x" * 24}
        if (method, path) in (("GET", "/_security/api_key"), ("DELETE", "/_security/api_key")):
            return 500, {}                                      # hygiene unavailable for the whole scenario
        raise AssertionError(f"unexpected {method} {path}")


class AliasGeneration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        home = pathlib.Path(cls.tmp) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        os.environ["WP_HOME"] = str(home)
        os.environ.pop("WP_BROKER", None)
        _psycopg_stub()
        under = os.environ.get("WP_PKG_UNDER_TEST")
        if under:                                   # failing-first: a whole older wp package (e.g. at 0110981)
            pkg = pathlib.Path(cls.tmp) / "pkg"
            shutil.copytree(under, pkg / "wp", ignore=shutil.ignore_patterns("__pycache__"))
            for k in [k for k in sys.modules if k == "wp" or k.startswith("wp.")]:
                del sys.modules[k]
            sys.path.insert(0, str(pkg))
        elif str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        cls.B = importlib.import_module("wp.broker")
        cls.F = importlib.import_module("wp.families")
        cls.ESError = importlib.import_module("wp.es").ESError

    def scenario(self, at, seq):
        cl = Cluster(self.ESError)
        lease = {"cur": (1, "hA")}
        live = lambda t, sc, e, who: (e, who) == lease["cur"]       # noqa: E731
        completed = []

        def other(epoch, holder):
            if (epoch, holder) != lease["cur"]:
                lease["cur"] = (epoch, holder)           # a takeover (PG) precedes the new holder's request
            self.B.Broker(Client(cl), live).writer_key(T, epoch, holder)
            completed.append(epoch)

        def run_others():
            for epoch, holder in seq:
                other(epoch, holder)

        a_lease_checks = {"n": 0}

        def live_a(t, sc, e, who):
            a_lease_checks["n"] += 1
            if at == "before_recheck" and a_lease_checks["n"] == 2:
                run_others()
            return live(t, sc, e, who)
        a = self.B.Broker(Client(cl, pause=None if at == "before_recheck" else run_others, at=at), live_a)
        try:
            a.writer_key(T, 1, "hA")
            outcome = "issued"
        except (self.B.Refused, self.F.StaleEpoch, self.F.ProjectionError) as e:
            outcome = type(e).__name__
        if outcome == "issued":
            completed.append(1)
        return cl, outcome, completed

    def check(self, cl, outcome, completed, label):
        newest = max(completed) if completed else None
        for f in self.F.FAMILIES:
            als = cl.idx.get(self.F.base_index(T, f), {})
            rx = re.compile(rf"^{re.escape(self.F.P + T)}-{f}-w(\d+)$")
            ws = sorted(int(m.group(1)) for a in als for m in [rx.match(a)] if m)
            self.assertEqual(len(ws), 1, f"{label}: I1 {f} write aliases {ws}")
            self.assertEqual(ws[0], newest if newest is not None else 0, f"{label}: I2 {f} {ws}")

    def test_interleavings(self):
        seqs = {"B": [(1, "hA")], "C": [(2, "hC")], "BC": [(1, "hA"), (2, "hC")], "CD": [(2, "hC"), (3, "hD")],
                "BCD": [(1, "hA"), (2, "hC"), (3, "hD")]}
        results = {}
        for at, (name, seq) in itertools.product(("before_create", "before_cas", "before_recheck"), seqs.items()):
            label = f"{at}/{name}"
            with self.subTest(label):
                cl, outcome, completed = self.scenario(at, seq)
                results[label] = outcome
                self.check(cl, outcome, completed, label)
                takeover = any(e > 1 for e, _ in seq)
                if takeover:
                    self.assertNotEqual(outcome, "issued", f"{label}: I3 A got a key after a completed takeover")

    def test_no_epoch_zero_key(self):
        cl = Cluster(self.ESError)
        b = self.B.Broker(Client(cl), lambda *a: True)
        with self.assertRaises((ValueError, self.B.Refused)):
            b.writer_key(T, 0, "h0")
        self.assertEqual(cl.minted, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
