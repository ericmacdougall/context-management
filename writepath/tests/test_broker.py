"""Offline unit test (no stores): the key broker (wp/broker.py, D6 on the H1/H1a basis: alias-name scoping plus the
broker-held alias CAS; key invalidation is hygiene only).

  descriptors        a writer key names exactly the tenant's -w<E> aliases (create_doc, index) and read aliases (read,
                     view_index_metadata, maintenance); no manage, no wildcard, no base index, no -x, no other epoch or
                     tenant; an eraser key names exactly the tenant's -x aliases (index, view_index_metadata)
  lease              no key (and no CAS) for a holder whose lease is not current; no key when the lease is lost
                     between the check and the CAS
  order              the alias CAS happens before the key is minted; StaleEpoch (a newer epoch visible) mints nothing
  hygiene            older-epoch writer keys of the SAME tenant are invalidated with owner: true; other tenants' and
                     newer keys are not; a failed invalidation is recorded, never fatal
  secrets            the broker's log carries key id fingerprints, never a key id or an encoded key
  transport          an unlisted peer uid is refused; an allowed one gets the key; an error crosses as its type
  projector          in broker mode the projector ignores any ES client it is given, takes its key from the broker,
                     maps a broker lease refusal to LeaseLost, and classifies a refused bulk item by the lease (never by
                     reading aliases)
The ES client is a stub; psycopg and cryptography are stubbed. usage (from writepath/): python3 -m unittest
tests.test_broker -v"""
import fnmatch
import importlib
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
T, E, HOLDER = "acme", 5, "h-live"
ENC = "ZW5jb2RlZC1rZXktdmFsdWU="          # a stand-in encoded key; the test asserts it never reaches the log


def _psycopg_stub():
    """A psycopg stub with every name the imported modules touch; extends a stub another test module installed."""
    m = sys.modules.get("psycopg")
    if m is None:
        m = types.ModuleType("psycopg")
        sys.modules["psycopg"] = m
    if getattr(m, "__file__", None):          # the real package: nothing to do
        return
    for n in ("Error", "OperationalError"):
        if not hasattr(m, n):
            setattr(m, n, type(n, (Exception,), {}))
    if not hasattr(m, "connect"):
        m.connect = lambda *a, **k: None
    if not hasattr(m, "errors"):
        m.errors = types.SimpleNamespace()
    for n in ("UniqueViolation", "LockNotAvailable", "QueryCanceled", "SerializationFailure", "DeadlockDetected"):
        if not hasattr(m.errors, n):
            setattr(m.errors, n, type(n, (Exception,), {}))
    for name, attrs in (("cryptography", {}), ("cryptography.hazmat", {}), ("cryptography.hazmat.primitives", {}),
                        ("cryptography.hazmat.primitives.ciphers", {}),
                        ("cryptography.hazmat.primitives.ciphers.aead", {"AESGCM": object})):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__dict__.update(attrs)
            sys.modules[name] = mod


class FakeES:
    """{index: set(aliases)} for the alias CAS, plus the API-key endpoints. Records every call in order."""

    def __init__(self, F, keys=(), stale=False, list_status=200, delete_status=200):
        self.F = F
        self.state = {F.base_index(T, f): {F.read_alias(T, f), F.erase_alias(T, f), F.write_alias(T, f, E - 1)}
                      for f in F.FAMILIES}
        if stale:
            for f in F.FAMILIES:
                self.state[F.base_index(T, f)].add(F.write_alias(T, f, E + 1))
        self.keys, self.list_status, self.delete_status = list(keys), list_status, delete_status
        self.calls, self.minted, self.deleted = [], [], []

    def alias_map(self, pattern):
        return {i: set(a) for i, a in self.state.items() if fnmatch.fnmatch(i, pattern)}

    def alias_meta(self, index=None, name=None):
        """The is_write_index view (review C F5): a -w alias is the write index except the generation alias -w0."""
        return {i: {a: not a.endswith("-w0") for a in als if name is None or fnmatch.fnmatch(a, name)}
                for i, als in self.state.items() if (index is None or fnmatch.fnmatch(i, index))
                and (name is None or any(fnmatch.fnmatch(a, name) for a in als))}

    def aliases(self, actions):
        self.calls.append("cas")
        for act in actions:
            (op, m), = act.items()
            (self.state[m["index"]].discard if op == "remove" else self.state[m["index"]].add)(m["alias"])

    def req(self, method, path, body=None, ok=(200, 201), **params):
        if (method, path) == ("POST", "/_security/api_key"):
            self.calls.append("mint")
            self.minted.append(body)
            return 200, {"id": f"kid{len(self.minted)}", "encoded": ENC, "expiration": 1}
        if (method, path) == ("GET", "/_security/api_key"):
            self.calls.append("list")
            assert params.get("owner") == "true", params
            return self.list_status, {"api_keys": self.keys}
        if (method, path) == ("DELETE", "/_security/api_key"):
            self.calls.append("invalidate")
            self.deleted.append(body)
            return self.delete_status, {"invalidated_api_keys": body["ids"]}
        if method == "PUT":
            raise AssertionError("the family indices exist in this stub")
        raise AssertionError(f"unexpected request {method} {path}")


class BrokerTest(unittest.TestCase):
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
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        cls.B = importlib.import_module("wp.broker")
        cls.F = importlib.import_module("wp.families")
        cls.L = importlib.import_module("wp.lease")
        cls.P = importlib.import_module("wp.projector")
        cls.KS = importlib.import_module("wp.keysvc")

    def broker(self, es, live=lambda t, sc, e, who: (t, sc, e, who) == (T, "projector", E, HOLDER)):
        log = []
        return self.B.Broker(es, live, log=log.append), log

    # ------------------------------------------------------------ descriptors
    def test_writer_descriptors_exact(self):
        F = self.F
        (role, rd), = self.B.writer_descriptors(T, E).items()
        self.assertEqual(rd["cluster"], [])
        w, r = rd["indices"]
        self.assertEqual(sorted(w["names"]), sorted(F.write_alias(T, f, E) for f in F.FAMILIES))
        self.assertEqual(sorted(w["privileges"]), ["create_doc", "index"])
        self.assertEqual(sorted(r["names"]), sorted(F.read_alias(T, f) for f in F.FAMILIES))
        self.assertEqual(sorted(r["privileges"]), ["maintenance", "read", "view_index_metadata"])
        names = w["names"] + r["names"]
        self.assertFalse([n for n in names if "*" in n or "?" in n or n.startswith("/")], "no wildcard or pattern")
        self.assertFalse([n for n in names if n.endswith("-base") or n.endswith("-x")], "no base index, no -x")
        self.assertFalse([n for n in names if f"-w{E}" not in n and n not in r["names"]], "no other epoch")
        self.assertTrue(all(n.startswith(f"wp16096-{T}-") for n in names), "one tenant only")
        self.assertNotIn("manage", w["privileges"] + r["privileges"])

    def test_eraser_descriptors_exact(self):
        F = self.F
        (_, rd), = self.B.eraser_descriptors(T).items()
        self.assertEqual(rd["cluster"], [])
        (x,) = rd["indices"]
        self.assertEqual(sorted(x["names"]), sorted(F.erase_alias(T, f) for f in F.FAMILIES))
        self.assertEqual(sorted(x["privileges"]), ["index", "view_index_metadata"])

    # ------------------------------------------------------------ lease and order
    def test_stale_holder_refused_before_any_es_call(self):
        es = FakeES(self.F)
        b, _ = self.broker(es)
        for epoch, holder in ((E, "h-old"), (E - 1, HOLDER), (E, "")):
            with self.assertRaises(self.B.Refused):
                b.writer_key(T, epoch, holder)
        self.assertEqual(es.calls, [], "no CAS and no mint for a holder that is not current")

    def test_lease_lost_between_check_and_cas_mints_nothing(self):
        es = FakeES(self.F)
        seen = []

        def live(*a):
            seen.append(a)
            return len(seen) == 1                       # current at the first check, gone after the CAS
        b, _ = self.broker(es, live)
        with self.assertRaises(self.B.Refused):
            b.writer_key(T, E, HOLDER)
        self.assertEqual(es.calls, ["cas"])
        self.assertEqual(es.minted, [])

    def test_cas_before_mint_and_alias_moved(self):
        es = FakeES(self.F)
        b, _ = self.broker(es)
        k = b.writer_key(T, E, HOLDER)
        self.assertEqual(es.calls[:2], ["cas", "mint"])
        for f in self.F.FAMILIES:
            als = es.state[self.F.base_index(T, f)]
            self.assertIn(self.F.write_alias(T, f, E), als)
            self.assertNotIn(self.F.write_alias(T, f, E - 1), als)
        m = es.minted[0]
        self.assertEqual(m["role_descriptors"], self.B.writer_descriptors(T, E))
        self.assertEqual(m["expiration"], self.B.KEY_TTL)
        self.assertEqual((m["metadata"]["tenant"], m["metadata"]["epoch"]), (T, E))
        self.assertEqual(k["id"], "kid1")

    def test_newer_epoch_visible_mints_nothing(self):
        es = FakeES(self.F, stale=True)
        b, _ = self.broker(es)
        with self.assertRaises(self.F.StaleEpoch):
            b.writer_key(T, E, HOLDER)
        self.assertNotIn("mint", es.calls)
        self.assertEqual(self.B.dispatch(b, "writer_key", {"tenant": T, "epoch": E, "holder": HOLDER}),
                         {"error": "StaleEpoch"})

    def test_same_epoch_again_is_idempotent(self):
        es = FakeES(self.F)
        b, _ = self.broker(es)
        b.writer_key(T, E, HOLDER)
        b.writer_key(T, E, HOLDER)
        self.assertEqual(es.calls.count("cas"), 1, "the second call finds -w<E> and moves nothing")
        self.assertEqual(es.calls.count("mint"), 2)

    # ------------------------------------------------------------ hygiene
    def test_invalidates_only_older_same_tenant_with_owner(self):
        keys = [{"id": "old3", "metadata": {"tenant": T, "epoch": 3}},
                {"id": "old4", "metadata": {"tenant": T, "epoch": E - 1}},
                {"id": "same", "metadata": {"tenant": T, "epoch": E}},
                {"id": "newer", "metadata": {"tenant": T, "epoch": E + 1}},
                {"id": "other", "metadata": {"tenant": "zeta", "epoch": 1}}]
        es = FakeES(self.F, keys=keys)
        b, _ = self.broker(es)
        k = b.writer_key(T, E, HOLDER)
        self.assertEqual(es.deleted, [{"ids": ["old3", "old4"], "owner": True}])
        self.assertEqual((k["invalidated_older"], k["invalidate_error"]), (2, None))

    def test_failed_invalidation_is_recorded_not_fatal(self):
        es = FakeES(self.F, keys=[{"id": "old4", "metadata": {"tenant": T, "epoch": E - 1}}], delete_status=403)
        b, _ = self.broker(es)
        k = b.writer_key(T, E, HOLDER)
        self.assertEqual((k["invalidated_older"], k["invalidate_error"]), (0, "invalidate 403"))
        self.assertEqual(k["encoded"], ENC)

    def test_hygiene_transport_failure_returns_the_key(self):
        """review B5: a transport exception in listing or invalidation is recorded; the minted key is returned."""
        for fail_on in ("GET", "DELETE"):
            es = FakeES(self.F, keys=[{"id": "old4", "metadata": {"tenant": T, "epoch": E - 1}}])
            real = es.req

            def req(method, path, *a, _real=real, _m=fail_on, **k):
                if method == _m and path == "/_security/api_key":
                    raise ConnectionResetError("simulated")
                return _real(method, path, *a, **k)
            es.req = req
            b, log = self.broker(es)
            k = b.writer_key(T, E, HOLDER)
            self.assertEqual(k["encoded"], ENC, fail_on)
            self.assertEqual(k["invalidate_error"], "hygiene ConnectionResetError", fail_on)
            self.assertIn("err=hygiene ConnectionResetError", "\n".join(log))
            self.assertNotIn("simulated", "\n".join(log))

    def test_mint_failure_stays_fatal(self):
        es = FakeES(self.F)
        real = es.req
        es.req = lambda m, p, *a, **k: (500, {"error": {"type": "x"}}) if (m, p) == ("POST", "/_security/api_key") else real(m, p, *a, **k)
        b, _ = self.broker(es)
        with self.assertRaises(self.B.Refused):
            b.writer_key(T, E, HOLDER)

    # ------------------------------------------------------------ secrets
    def test_log_has_key_fingerprint_never_id_or_value(self):
        """Review J M8: the stream's leak scan counts key ids as secret values, so the broker logs a fingerprint."""
        es = FakeES(self.F)
        b, log = self.broker(es)
        b.writer_key(T, E, HOLDER)
        b.eraser_key(T)
        text = "\n".join(log)
        EK = importlib.import_module("wp.es_keys")
        self.assertIn("key=" + EK.fingerprint("kid1"), text)
        self.assertIn("key=" + EK.fingerprint("kid2"), text)
        self.assertNotRegex(text, r"\bkid[12]\b", "the broker logged a key id")
        self.assertNotIn(ENC, text)

    # ------------------------------------------------------------ transport
    def test_socket_peer_refused_and_allowed(self):
        es = FakeES(self.F)
        b, _ = self.broker(es)
        path = os.path.join(self.tmp, "b.sock")
        uid, srvlog = {"v": 30001}, []
        srv = self.KS.Server(path, {30001}, make_store=lambda: b, uid_of=lambda s: uid["v"], log=srvlog.append,
                             ops=self.B.OPS, dispatch_fn=self.B.dispatch)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = self.B.Client(path)
            k = c.writer_key(T, E, HOLDER)
            self.assertEqual((k["id"], k["encoded"]), ("kid1", ENC))
            with self.assertRaises(self.B.Refused):
                c.writer_key(T, E, "h-old")
            with self.assertRaises(self.KS.ServiceError):
                c._req("keys", tenant=T, subjects=["x"])          # a key-service op is not a broker op
            c.close()
            uid["v"] = 30002
            c = self.B.Client(path)
            with self.assertRaises(self.KS.ServiceError) as cm:
                c.writer_key(T, E, HOLDER)
            self.assertIn("peer_refused", str(cm.exception))
            c.close()
            self.assertEqual(es.calls.count("mint"), 1, "the refused peer got nothing minted")
            self.assertNotIn(ENC, "\n".join(srvlog))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_concurrent_clients_get_their_own_broker(self):
        made = []

        class PG:
            def __init__(self):
                self.closed = False

            def execute(self, sql, args):
                return types.SimpleNamespace(fetchone=lambda: (args == (T, "projector", E, HOLDER),))

            def close(self):
                self.closed = True

        def es_factory():
            es = FakeES(self.F)
            made.append(es)
            return es
        pgs = []

        def pg_factory():
            pgs.append(PG())
            return pgs[-1]
        path = os.path.join(self.tmp, "c.sock")
        srv = self.KS.Server(path, {30001}, make_store=self.B.broker_factory(es_factory, pg_factory),
                             uid_of=lambda s: 30001, ops=self.B.OPS, dispatch_fn=self.B.dispatch)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            gate, out = threading.Barrier(2), []

            def one():
                c = self.B.Client(path)
                gate.wait()
                out.append(c.writer_key(T, E, HOLDER)["id"])
                c.close()
            ts = [threading.Thread(target=one) for _ in range(2)]
            [t.start() for t in ts]
            [t.join(10) for t in ts]
            self.assertEqual(len(out), 2)
            self.assertEqual(len(made), 2, "one ES client per connection")
            self.assertEqual(len({id(e) for e in made}), 2)
            self.assertEqual([e.calls.count("mint") for e in made], [1, 1], "no connection saw another's requests")
            for _ in range(100):
                if all(p.closed for p in pgs):
                    break
                threading.Event().wait(0.02)
            self.assertEqual([p.closed for p in pgs], [True, True], "each connection's PG session closed on disconnect")
        finally:
            srv.shutdown()
            srv.server_close()

    # ------------------------------------------------------------ receipt wording
    def test_fence_statement_per_mode(self):
        os.environ["WP_BROKER"] = os.path.join(self.tmp, "none.sock")
        try:
            s = self.B.fence_statement()
        finally:
            os.environ.pop("WP_BROKER", None)
        self.assertEqual(s["mode"], "broker")
        self.assertIn("not key revocation", s["basis"])
        self.assertIn("0.055 s", s["revocation"])
        self.assertIn("one node", s["revocation"])
        self.assertIn("broker is down", s["issued_key_window"])
        self.assertIn("none claimed", s["time_bound"])
        s = self.B.fence_statement()
        self.assertNotEqual(s["mode"], "broker")
        self.assertIn("re-add", s["basis"])
        self.assertIn("none claimed", s["time_bound"])

    # ------------------------------------------------------------ projector broker mode
    class _FakeBrokerClient:
        def __init__(self, B, refuse=None):
            self.B, self.refuse, self.asked = B, refuse, []

        def writer_key(self, t, epoch, holder):
            self.asked.append((t, epoch, holder))
            if self.refuse:
                raise self.B.Refused(self.refuse)
            return {"id": "kidP", "encoded": ENC}

    def _projector(self, fake):
        sentinel = object()
        p = self.P.Projector(None, None, sentinel, T, broker=fake)
        self.assertIsNone(p.es, "broker mode ignores the ES client it was given")
        p.lease = self.L.Lease(T, "projector", E, HOLDER, 0.0)
        return p

    def test_projector_takes_key_from_broker(self):
        fake = self._FakeBrokerClient(self.B)
        p = self._projector(fake)
        p._broker_key()
        self.assertEqual(fake.asked, [(T, E, HOLDER)])
        self.assertEqual(p.es.auth, "ApiKey " + ENC)
        self.assertEqual(p.key_id, "kidP")

    def test_projector_broker_lease_refusal_is_lease_lost(self):
        p = self._projector(self._FakeBrokerClient(self.B, refuse="lease_not_current"))
        with self.assertRaises(self.L.LeaseLost):
            p._broker_key()

    def test_projector_bulk_refusal_classified_by_lease(self):
        p = self._projector(self._FakeBrokerClient(self.B))
        p._broker_key()

        class BulkES:
            auth = "x"

            def bulk_fenced(self, actions):
                return 0, [("id1", 403, "security_exception")]

            def alias_map(self, pattern):
                raise AssertionError("broker mode never reads aliases")

            def alias_meta(self, index=None, name=None):
                raise AssertionError("broker mode never reads aliases")
        p.es = BulkES()
        orig = self.L.renew
        try:
            self.P.L.renew = lambda conn, lease, ttl: None          # the lease is still ours
            with self.assertRaises(self.F.ProjectionError):
                p._bulk("events", [({"index": {}}, {})])

            def lost(conn, lease, ttl):
                raise self.L.LeaseLost("gone")
            self.P.L.renew = lost
            with self.assertRaises(self.L.LeaseLost):
                p._bulk("events", [({"index": {}}, {})])
            self.assertEqual(p.stats["stale_bulk_items"], 1)
        finally:
            self.P.L.renew = orig


if __name__ == "__main__":
    unittest.main()
