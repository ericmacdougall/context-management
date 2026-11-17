"""Offline unit test (no stores): the key service (wp/keysvc.py, D2) serves only allowed peer uids, round-trips the
KeyStore interface, and never logs a key, a text or an HMAC.

A real unix socket in a temporary directory; the peer uid is injected (macOS has no SO_PEERCRED; on Linux the server
reads it from the kernel, which exp/h_d2_uids.py exercises with real uids). psycopg and cryptography are stubbed: the
fake store uses neither. usage (from writepath/): python3 -m unittest tests.test_keysvc -v"""
import importlib
import os
import pathlib
import socket
import sys
import tempfile
import threading
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
KEY = bytes(range(32))
SECRET_TEXT = "ident:zqsecretname"


def _stub(name, **attrs):
    if name in sys.modules:
        return
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m


class FakeStore:
    calls = []

    def __init__(self):
        self.conn = None
        self.destroyed = set()

    def keys(self, tenant, subjects, create=False):
        FakeStore.calls.append("keys")
        out = {}
        for s in sorted(set(subjects)):
            if create and (tenant, s) in self.destroyed:
                raise self.K.SubjectErased(s)
            out[s] = KEY if s != "nokey" else None
        return out

    def tenant_hmac_many(self, tenant, texts):
        FakeStore.calls.append("tenant_hmac_many")
        return ["h" + str(len(t)) for t in texts]

    def tenant_hmac(self, tenant, text):
        return "h" + str(len(text))

    def chain_key(self, tenant):
        return KEY

    def is_destroyed(self, tenant, subject):
        return (tenant, subject) in self.destroyed

    def key_exists(self, tenant, subject):
        return (tenant, subject) not in self.destroyed

    def destroy(self, tenant, subject):
        FakeStore.calls.append("destroy")
        self.destroyed.add((tenant, subject))

    def purge(self, timeout_s=60.0):
        return {"purged": True, "waited_s": 0.0}

    def physical_pending(self, tenant=None):
        return [("t", "X")]

    def describe(self):
        return {"kind": "fake", "stand_in": True, "deletion": "none"}


class KeyService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        home = pathlib.Path(cls.tmp) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        os.environ["WP_HOME"] = str(home)
        _stub("psycopg", connect=lambda *a, **k: None, Error=Exception, errors=types.SimpleNamespace(UniqueViolation=Exception))
        _stub("cryptography"); _stub("cryptography.hazmat"); _stub("cryptography.hazmat.primitives")
        _stub("cryptography.hazmat.primitives.ciphers"); _stub("cryptography.hazmat.primitives.ciphers.aead", AESGCM=object)
        sys.path.insert(0, str(HERE))
        cls.S = importlib.import_module("wp.keysvc")
        FakeStore.K = importlib.import_module("wp.keys")
        cls.path = os.path.join(cls.tmp, "k.sock")
        cls.uid = {"v": 20001}
        cls.log = []
        cls.store = FakeStore()
        cls.srv = cls.S.Server(cls.path, {20001}, make_store=lambda: cls.store, uid_of=lambda s: cls.uid["v"], log=cls.log.append)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_1_allowed_peer_round_trip(self):
        self.uid["v"] = 20001
        c = self.S.Client(self.path)
        self.assertEqual(c.keys("t", ["X", "nokey"]), {"X": KEY, "nokey": None})
        self.assertEqual(c.tenant_hmac_many("t", [SECRET_TEXT, "ab"]), ["h" + str(len(SECRET_TEXT)), "h2"])
        self.assertEqual(c.chain_key("t"), KEY)
        c.destroy("t", "Y")
        self.assertTrue(c.is_destroyed("t", "Y"))
        self.assertEqual(c.physical_pending(), [("t", "X")])
        self.assertTrue(c.purge(timeout_s=1)["purged"])
        self.assertIn("transport", c.describe())
        with self.assertRaises(FakeStore.K.SubjectErased):
            c.keys("t", ["Y"], create=True)
        c.close()

    def test_2_refused_peer_reaches_no_store(self):
        self.uid["v"] = 20002
        before = len(FakeStore.calls)
        c = self.S.Client(self.path)
        with self.assertRaises(self.S.ServiceError) as cm:
            c.keys("t", ["X"])
        self.assertIn("peer_refused", str(cm.exception))
        self.assertEqual(len(FakeStore.calls), before, "a refused peer reached the store")
        c.close()
        self.uid["v"] = 20001

    def test_3_unknown_op(self):
        c = self.S.Client(self.path)
        with self.assertRaises(self.S.ServiceError):
            c._call("drop_everything")
        c.close()

    def test_4_log_has_no_values(self):
        blob = "\n".join(self.log)
        self.assertTrue(self.log, "nothing logged")
        for v in (KEY.hex(), SECRET_TEXT, "zqsecretname"):
            self.assertNotIn(v, blob)

    def test_5_serve_fails_closed_without_allow_list(self):
        os.environ.pop("WP_KEYSVC_ALLOW_UIDS", None)
        with self.assertRaises(SystemExit):
            self.S.serve(os.path.join(self.tmp, "x.sock"))
        os.environ["WP_KEYSVC_ALLOW_UIDS"] = " , "
        with self.assertRaises(SystemExit):
            self.S.serve(os.path.join(self.tmp, "y.sock"))

    def test_6_peer_uid_from_kernel_on_linux(self):
        if not hasattr(socket, "SO_PEERCRED"):
            self.skipTest("SO_PEERCRED is Linux-only; exercised by exp/h_d2_uids.py with real uids")
        a, b = socket.socketpair(socket.AF_UNIX)
        self.assertEqual(self.S.peer_uid(a), os.getuid())
        a.close(); b.close()

    def test_7_malformed_requests_survive_and_log_no_caller_bytes(self):
        """review B3: invalid JSON first, then a valid request on the same connection; unknown ops (string, object,
        non-object request, bad args) are answered with a fixed label and never copied into the log."""
        import json as _j
        self.uid["v"] = 20001
        mark = "zqsecretop9917"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.path)
        f = s.makefile("rwb")

        def ask(raw):
            f.write(raw + b"\n")
            f.flush()
            return _j.loads(f.readline())
        n0 = len(self.log)
        self.assertEqual(ask(b"{not json"), {"error": "bad_request"})
        self.assertIn("ok", ask(_j.dumps({"op": "chain_key", "args": {"tenant": "t"}}).encode()))
        self.assertEqual(ask(_j.dumps({"op": mark}).encode()), {"error": "unknown_op"})
        self.assertEqual(ask(_j.dumps({"op": {"x": mark}}).encode()), {"error": "unknown_op"})
        self.assertEqual(ask(_j.dumps([mark]).encode()), {"error": "bad_request"})
        self.assertEqual(ask(_j.dumps({"op": "chain_key", "args": mark}).encode()), {"error": "bad_request"})
        f.close(); s.close()
        logged = "\n".join(self.log[n0:])
        self.assertNotIn(mark, logged)
        self.assertIn("op=bad_request", logged)
        self.assertIn("op=unknown_op", logged)


if __name__ == "__main__":
    unittest.main()
