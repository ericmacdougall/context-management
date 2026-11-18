"""Offline: review J M5 and M6 (registry gaps, narrow static privilege checks).
  ST1  exp/static_check.py on the real source: every check clean, S7 (privileges on wp_seal, wp_owner membership) exists
  ST2  S5 sees unlogged, if-not-exists, uppercase and quoted tables; S7 sees an uppercase GRANT on wp_seal, alter default
       privileges in schema wp_seal and grant wp_owner to a role (each is a mutation of the real source)
  RG1  wp/registry.py unregistered() lists large objects (events and keys databases), data streams under the prefix and
       their .ds- backing indices; a clean fake system lists nothing
  RG2  setup revokes TEMPORARY on both databases from public (static; the live grant is checked by the P0 setup run)
Psycopg is stubbed when absent; nothing connects.
usage (from writepath/): python3 -m unittest tests.test_m5m6_statics -v"""
import importlib
import os
import pathlib
import re
import sys
import tempfile
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
_SET = []


def setUpModule():
    if not os.environ.get("WP_HOME"):
        home = pathlib.Path(tempfile.mkdtemp()) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        os.environ["WP_HOME"] = str(home)
        _SET.append(1)
    try:
        import psycopg  # noqa: F401
    except ImportError:
        m = types.ModuleType("psycopg")
        m.connect, m.Error = (lambda *a, **k: None), Exception
        m.errors = types.SimpleNamespace(UniqueViolation=Exception)
        sys.modules["psycopg"] = m
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))


def tearDownModule():
    if _SET:
        os.environ.pop("WP_HOME", None)


MUTS = {"S5": [("schema.sql", "\ncreate unlogged table ghost_u (x text);\n"),
               ("schema.sql", "\nCREATE TABLE IF NOT EXISTS Ghost_Up (x text);\n"),
               ("schema.sql", '\n  create temp table "ghost_q" (x text);\n')],
        "S7": [("schema.sql", "\nGRANT SELECT ON wp_seal.seal_keys TO wp_app;\n"),
               ("schema.sql", "\nalter default privileges in schema wp_seal grant select on tables to wp_app;\n"),
               ("setup.py", "\nX = 'grant wp_owner to wp_app'\n")]}


class Statics(unittest.TestCase):
    def test_st1_real_source_clean(self):
        S = importlib.import_module("exp.static_check")
        out, _, _ = S.checks(S.load())
        self.assertIn("S7", out, "no static check on privileges over wp_seal or wp_owner")
        self.assertEqual({k: v for k, v in out.items() if v}, {})

    def test_st2_mutations_flagged(self):
        S = importlib.import_module("exp.static_check")
        src = S.load()
        for k, ms in MUTS.items():
            for f, add in ms:
                m = dict(src)
                m[f] = m[f] + add
                out, _, _ = S.checks(m)
                self.assertTrue(out.get(k), f"{k} misses {add.strip()!r}")

    def test_rg2_temporary_revoked(self):
        s = (HERE / "wp" / "setup.py").read_text()
        self.assertTrue(re.search(r"revoke temporary on database", s), "TEMPORARY is not revoked from public")


class Conn:
    def __init__(self, rels, lo):
        self.rels, self.lo = rels, lo

    def execute(self, sql, args=None):
        rows = [(self.lo,)] if "pg_largeobject_metadata" in sql else [(r,) for r in self.rels]
        return types.SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0])


class FakeES:
    def __init__(self, P, dirty):
        self.P, self.dirty = P, dirty

    def indices(self, pattern):
        if self.dirty and pattern.startswith(".ds-"):
            return [f".ds-{self.P}zzds1-2026.11.18-000001"]
        return []

    def alias_map(self, pattern):
        return {}

    def req(self, method, path, body=None, ok=(200, 201), **params):
        if self.dirty and path.startswith("/_data_stream/"):
            return 200, {"data_streams": [{"name": f"{self.P}zzds1"}]}
        return 404, {}


class Registry(unittest.TestCase):
    def test_rg1_large_objects_and_data_streams(self):
        R = importlib.import_module("wp.registry")
        P = R.P
        pg = sorted(s.where for s in R.STORES if s.kind == "pg")
        kk = sorted(s.where for s in R.STORES if s.kind == "keys")
        self.assertEqual(R.unregistered(Conn(pg, 0), Conn(kk, 0), FakeES(P, False)), [], "a clean system is not clean")
        got = R.unregistered(Conn(pg, 2), Conn(kk, 1), FakeES(P, True))
        for want in ("pg:large_objects(2)", "keys:large_objects(1)", f"es-datastream:{P}zzds1",
                     f"es-index:.ds-{P}zzds1-2026.11.18-000001"):
            self.assertIn(want, got)


if __name__ == "__main__":
    unittest.main()
