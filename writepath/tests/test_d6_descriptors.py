"""D6 C8's descriptor check after the key-id fingerprints of review J M8 (offline; a fake cluster).

  DC1  the child records [tenant, epoch, fingerprint]; the operator finds each key among the broker user's keys by
       fingerprint and checks its role descriptors: a key scoped to its tenant and epoch counts as checked, not bad.
       Live origin: 2026-11-19 window A at 1ffbfa1, D6 fenced 0/5 with "C8 errors 0 bad 40 checked 0" for every run:
       the check still looked keys up by id, and since bd2fd17 it was handed fingerprints.
  DC2  a key whose descriptors name another tenant's alias is bad (the check is not vacuous).
  DC3  a fingerprint no key of the broker user has is bad (not readable)."""
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

if not os.environ.get("WP_HOME"):                 # an isolated lab home, as tests/test_sealer.py does
    _home = pathlib.Path(tempfile.mkdtemp()) / "home"
    (_home / "conf").mkdir(parents=True)
    for _k, _v in {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
                   "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}.items():
        (_home / "conf" / _k).write_text(_v + "\n")
    os.environ["WP_HOME"] = str(_home)

from exp import h_d6_broker as H  # noqa: E402
from wp import broker as BR, es_keys, families as F  # noqa: E402


def _desc(t, e):
    names = [F.write_alias(t, f, e) for f in F.FAMILIES] + [F.read_alias(t, f) for f in F.FAMILIES]
    return {"w": {"indices": [{"names": names, "privileges": ["write"]}]}}


class FakeES:
    def __init__(self, keys):
        self.keys = keys                                   # id -> role descriptors

    def req(self, method, path, body=None, ok=None, **q):
        if method == "GET" and path == "/_security/api_key":
            if q.get("id") is not None:
                k = self.keys.get(q["id"])
                return (200, {"api_keys": [{"id": q["id"], "role_descriptors": k}]}) if k else (404, {})
            if q.get("username") == BR.BROKER_USER:
                return 200, {"api_keys": [{"id": i, "role_descriptors": d} for i, d in self.keys.items()]}
        return 404, {}


class Descriptors(unittest.TestCase):
    def test_dc1_scoped_keys_checked_by_fingerprint(self):
        es = FakeES({"idA1": _desc("ta", 3), "idB1": _desc("tb", 5)})
        r = H._descriptor_check(es, [["ta", 3, es_keys.fingerprint("idA1")], ["tb", 5, es_keys.fingerprint("idB1")]])
        self.assertEqual((r["checked"], r["n_bad"]), (2, 0), r)

    def test_dc2_other_tenant_alias_is_bad(self):
        es = FakeES({"idA1": _desc("tb", 3)})
        r = H._descriptor_check(es, [["ta", 3, es_keys.fingerprint("idA1")]])
        self.assertEqual((r["checked"], r["n_bad"]), (1, 1), r)

    def test_dc3_unknown_fingerprint_is_bad(self):
        es = FakeES({"idA1": _desc("ta", 3)})
        r = H._descriptor_check(es, [["ta", 3, es_keys.fingerprint("other")]])
        self.assertEqual((r["checked"], r["n_bad"]), (0, 1), r)


if __name__ == "__main__":
    unittest.main()
