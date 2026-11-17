"""Offline unit test (review B6 at 76c9276): H2's verifier-side key-service probe and its judge distinguish the
protocol's peer-uid refusal from an outage (missing socket, refused connection, crashed service, other errors).
usage (from writepath/): python3 -m unittest tests.test_h2_probe -v"""
import contextlib
import importlib
import io
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "tests"))
from test_broker import GOOD, _psycopg_stub  # noqa: E402


class H2Probe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = pathlib.Path(tempfile.mkdtemp()) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        os.environ["WP_HOME"] = str(home)
        _psycopg_stub()
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        cls.KS = importlib.import_module("wp.keysvc")
        cls.H2 = importlib.import_module("exp.h_d2_uids")

    def probe_label(self, exc):
        KS = self.KS

        class C:
            def __init__(self, path):
                if isinstance(exc, (FileNotFoundError, ConnectionRefusedError)):
                    raise exc

            def keys(self, *a):
                raise exc
        real, KS.Client = KS.Client, C
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.H2.keysvc_probe()
        finally:
            KS.Client = real
        out = buf.getvalue().strip()
        return out.split(" ")[1]          # what once() records as verifier_uid_keysvc

    def judged(self, label):
        x = {"files": {}, "connect": {}, "complete_as_app": "42501", "dishonest_done": False, "honest_external_done": True,
             "key_store": "separate key-service process", "verifier_uid_keysvc": label, "job_state": "done"}
        return [w for w in self.H2.judge("fenced", x, {}) if "keysvc" in w]

    def test_only_protocol_refusal_counts(self):
        cases = {"peer_refused": self.KS.ServiceError("peer_refused"),
                 "missing socket": FileNotFoundError(2, "No such file"),
                 "connection refused": ConnectionRefusedError(111, "refused"),
                 "service closed": self.KS.ServiceError("key service closed the connection"),
                 "other error": RuntimeError("boom")}
        for name, exc in cases.items():
            with self.subTest(name):
                label = self.probe_label(exc)
                if name == "peer_refused":
                    self.assertEqual(label, "peer_refused")
                    self.assertEqual(self.judged(label), [])
                else:
                    self.assertNotEqual(label, "peer_refused")
                    self.assertTrue(self.judged(label), f"{name}: an outage must not satisfy the verifier denial check")


if __name__ == "__main__":
    unittest.main(verbosity=2)
