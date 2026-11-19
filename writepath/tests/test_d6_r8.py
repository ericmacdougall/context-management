"""D6 cases that review 42cfd11 R8 found vacuous or misstated (offline; the judge and the request helpers only).

  D6R1  C14c is judged only when it ran with a key that was still valid: the 42cfd11 shape (5 writes after C5's CAS,
        each refused 401 because the key was already invalidated) no longer holds.
  D6R2  C14c holds only when the operator found no index of that name afterwards (exists_after False); an index that
        exists, or a HEAD that could not tell, fails.
  D6R3  C1 B_read is a search through B's read alias (POST /<read alias>/_search), in both children; before, it was a
        write through a read alias, refused 400 in both arms whatever the key could read.
  D6R4  a B_read of 404 (B has no read alias) is INVALID-REQUEST in the fenced arm, not a refusal.
  D6R5  the stated limit no longer claims that the projector role key re-points any tenant's alias: the receipts of
        dc08269a and e8ebd3c7 show that request refused (403, control C4 and the fence CAS).
  D6R6  the key's validity is observed (an own write right after the five): a refused own write fails C14c, and any
        401 among the five makes C14c INVALID-REQUEST.
  D6R7  the operator's existence check: 404 False, 200 True, any other status or an exception None (never raises, so
        one failed HEAD cannot lose the run's rows)."""
import inspect
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
from wp import families as F  # noqa: E402

GOOD_C14 = {"a": {"own": [201, None], "B": [403, "security_exception"], "request": 200},
            "b": [403, "security_exception", None], "d": {"B_mapping": [403, "security_exception"]}}


def _c14c_why(c):
    why, _ = H.judge("fenced", {"own_write": [201, None], "C14": dict(GOOD_C14, c=c)}, {})
    return [w for w in why if w.startswith("C14c")]


class FakeES:
    def __init__(self):
        self.calls = []

    def req(self, method, path, body=None, ok=None, **q):
        self.calls.append((method, path, body))
        return 403, {"error": {"type": "security_exception"}}


class HeadES:
    def __init__(self, status=None, exc=None):
        self.status, self.exc = status, exc

    def req(self, method, path, body=None, ok=None, **q):
        if self.exc:
            raise self.exc
        return self.status, {}


class D6R8(unittest.TestCase):
    def test_d6r1_c14c_after_the_cas_does_not_hold(self):
        stale = {"writes": [[401, "security_exception"]] * 5, "accepted": 0}     # the 42cfd11 shape
        self.assertTrue(_c14c_why(stale), "C14c held with writes made by an already invalidated key")

    def test_d6r2_c14c_needs_no_index_afterwards(self):
        base = {"name": "x", "writes": [[403, "security_exception"]] * 5, "accepted": 0, "key_valid": True}
        self.assertEqual(_c14c_why(dict(base, exists_after=False)), [])
        self.assertTrue(_c14c_why(dict(base, exists_after=True)), "an index of that name exists and C14c held")
        self.assertTrue(_c14c_why(dict(base, exists_after=None)), "the HEAD could not tell and C14c held")
        self.assertTrue(_c14c_why(dict(base, exists_after=False, accepted=1)), "an accepted write and C14c held")

    def test_d6r3_b_read_is_a_search_in_both_children(self):
        s = getattr(H, "_s", None)
        self.assertIsNotNone(s, "no search helper: B_read is not a read")
        es = FakeES()
        out = s(es, F.read_alias("tB", "events"))
        self.assertEqual(es.calls, [("POST", f"/{F.read_alias('tB', 'events')}/_search", {"size": 0})])
        self.assertEqual(out, [403, "security_exception"])
        for child in (H.child_fenced, H.child_control):
            src = inspect.getsource(child)
            line = next((ln for ln in src.splitlines() if '"B_read":' in ln), "")
            self.assertIn("_s(", line, f"{child.__name__}: B_read is not a search: {line.strip()}")

    def test_d6r4_b_read_404_is_invalid_request(self):
        _, inv = H.judge("fenced", {"own_write": [201, None], "C1": {"B_read": [404, "index_not_found_exception"]}}, {})
        self.assertIn("C1 B_read", inv)

    def test_d6r5_limit_does_not_claim_re_pointing(self):
        src = inspect.getsource(H)
        self.assertNotIn("can re-point any tenant's alias", src)
        self.assertIn("re-pointing is not demonstrated", src)


    def test_d6r6_key_validity_is_observed(self):
        base = {"name": "x", "writes": [[403, "security_exception"]] * 5, "accepted": 0, "exists_after": False}
        self.assertTrue(_c14c_why(dict(base, own_after=[401, "security_exception"], key_valid=False)))
        _, inv = H.judge("fenced", {"own_write": [201, None], "C14": dict(
            GOOD_C14, c=dict(base, writes=[[401, "security_exception"]] * 5, key_valid=True))}, {})
        self.assertIn("C14c", inv)

    def test_d6r7_exists_after(self):
        f = getattr(H, "exists_after", None)
        self.assertIsNotNone(f, "no operator existence check for the C14c name")
        self.assertIs(f("n", HeadES(404)), False)
        self.assertIs(f("n", HeadES(200)), True)
        self.assertIsNone(f("n", HeadES(500)))
        self.assertIsNone(f("n", HeadES(exc=OSError("refused"))))


if __name__ == "__main__":
    unittest.main()
