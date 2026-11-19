"""Offline unit test (no stores) for the seal authority (wp/sealer.py; DESIGN-NOTES "Seal authority" A1 to A8, main's
option (b)): the authority admits, secret-scans, matches, encrypts and attests; nothing else holds the seal key.

  U1  the encoding is length-prefixed in UTF-8 octets: no two field lists encode alike; a null field is refused
  U2  the attestation binds every field: changing any one changes the MAC
  U3  the store's formula (RFC 2104 with precomputed pads) equals HMAC-SHA256 under the key
  U4  the MAC is HMAC under the authority's own key, unchanged when the key service's tenant and chain keys change
  U5  seal output: mentions found by the authority, speaker removed, version and matcher and secret-scan version as the
      authority ran them, batch fields, digest of the ciphertext, a MAC that verifies (non-ASCII ids included)
  U6  an over-budget, oversize or secret-bearing batch is refused before the key service is used; nothing is sealed
  U7  over the socket: an allowed peer seals, a refused peer gets nothing, refusals keep their codes, and the log holds
      no content, ciphertext, key, MAC, subject id or event id
  U8  log.append without a reachable authority raises SealUnavailable: no subject key, no insert; WP_SEALER set never
      falls back to an in-process authority even with a readable key file
  U9  the key file must be mode 0600; the key service's operations are unchanged (it never serves the seal key)
  U10 schema statics: wp_seal private, its key table referenced only by the guards and wp_seal's own functions, the
      guards refuse unattested (WPS40) and invalid (WPS41) rows before the fence, the columns exist, the cascade's
      quarantine version rule; setup and the live fixtures create and drop wp_seal

psycopg is stubbed when absent; K.seal is replaced by a deterministic stand-in (another module may have stubbed
cryptography process-wide). Needs WP_UCD_DIR (the matcher fails closed without its tables).
usage (from writepath/): WP_UCD_DIR=<dir> python3 -m unittest tests.test_sealer -v"""
import hashlib
import hmac
import importlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import threading
import types
import unittest
import uuid

HERE = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = (HERE / "wp" / "schema.sql").read_text()
SEAL_KEY = bytes(range(100, 132))            # opaque test bytes, not a secret
KID = "test-k1"
SUBJ_KEY = bytes(range(32))
NAME = "Rosalind Okafor"
CRAFTED = " ".join("1I1I1I" for _ in range(300))          # over the matcher key budget (b1, WPM02)
KEYSVC_OPS = ("keys", "tenant_hmac", "tenant_hmac_many", "chain_key", "is_destroyed", "key_exists", "destroy", "purge",
              "physical_pending", "describe")


def _stub(name, **attrs):
    if name in sys.modules:
        return
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m


GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}


def _ensure_imports():
    if not os.environ.get("WP_HOME"):
        home = pathlib.Path(tempfile.mkdtemp()) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        os.environ["WP_HOME"] = str(home)
    try:
        import psycopg  # noqa: F401
    except ImportError:
        class _Errors:
            def __getattr__(self, name):
                return type(name, (Exception,), {})
        _stub("psycopg", connect=lambda *a, **k: None, Error=Exception, errors=_Errors(), sql=types.SimpleNamespace(),
              __getattr__=lambda name: type(name, (Exception,), {}))
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError:
        for n in ("cryptography", "cryptography.hazmat", "cryptography.hazmat.primitives", "cryptography.hazmat.primitives.ciphers"):
            _stub(n)
        _stub("cryptography.hazmat.primitives.ciphers.aead", AESGCM=object)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))


def fake_seal(key, tenant, subject, eid, text):
    """Deterministic stand-in for K.seal: 12-byte nonce, the text, 16-byte tag bound to the key and the AAD."""
    body = text.encode()
    tag = hmac.new(key, ("%s|%s|%s" % (tenant, subject, eid)).encode() + body, hashlib.sha256).digest()[:16]
    return b"N" * 12 + body + tag


class FakeKS:
    """Records every call. Tenant HMAC under `hkey`; chain key `ckey` (both changeable for U4)."""

    def __init__(self, hkey=b"tenant-hmac-key", ckey=b"c" * 32):
        self.calls, self.hkey, self.ckey = [], hkey, ckey
        self.conn = None

    def keys(self, tenant, subjects, create=False):
        self.calls.append(("keys", create))
        return {s: SUBJ_KEY for s in subjects}

    def tenant_hmac_many(self, tenant, texts):
        self.calls.append(("tenant_hmac_many", len(texts)))
        return [hmac.new(self.hkey, x.encode(), hashlib.sha256).hexdigest() for x in texts]

    def tenant_hmac(self, tenant, text):
        self.calls.append(("tenant_hmac", 1))
        return hmac.new(self.hkey, text.encode(), hashlib.sha256).hexdigest()

    def chain_key(self, tenant):
        self.calls.append(("chain_key", 0))
        return self.ckey


class FakeConn:
    """identifier_epochs and subject_identifiers as the authority reads them; anything else is recorded."""

    def __init__(self, version=0, matcher=None, idents=None):
        self.version, self.matcher, self.idents, self.sql, self.inserted = version, matcher, dict(idents or {}), [], []

    def execute(self, sql, args=()):
        self.sql.append(sql)
        conn = self

        class R:
            rowcount = 0

            def fetchone(self):
                if "identifier_epochs" in sql:
                    return (conn.version, conn.matcher)
                return None

            def fetchall(self):
                if "subject_identifiers" in sql:
                    return [(h, conn.idents[h]) for h in args[1] if h in conn.idents]
                return []
        if "insert into events" in sql or "insert into quarantine" in sql:
            self.inserted.append(sql.split("(")[0])
            r = R()
            r.rowcount = 1
            return r
        return R()

    def transaction(self):
        import contextlib
        return contextlib.nullcontext()

    def close(self):
        pass


def _fn_body(sql, name):
    m = re.search(r"create (?:or replace )?function " + re.escape(name) + r"\(.*?\$\$(.*?)\$\$", sql, re.S)
    return m.group(1) if m else ""


def _functions(sql):
    return {m.group(1): m.group(2) for m in re.finditer(r"create (?:or replace )?function ([\w.]+)\(.*?\$\$(.*?)\$\$", sql, re.S)}


def _table(sql, name):
    m = re.search(r"create table " + re.escape(name) + r" \((.*?)\n\);", sql, re.S)
    return m.group(1) if m else ""


class SealerOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_imports()
        cls.M = importlib.import_module("wp.mentions")
        cls.K = importlib.import_module("wp.keys")
        cls.SEC = importlib.import_module("wp.secrets")
        cls.LOG = importlib.import_module("wp.log")
        cls.KSVC = importlib.import_module("wp.keysvc")
        try:
            cls.S = importlib.import_module("wp.sealer")
        except ImportError:
            cls.S = None

    def setUp(self):
        self._seal = self.K.seal
        self.K.seal = fake_seal

    def tearDown(self):
        self.K.seal = self._seal

    def sealer(self):
        if self.S is None:
            self.fail("wp.sealer is missing: no authority seals, so every write is the writer's word (review H F14)")
        return self.S

    def fields(self, **over):
        f = dict(key_id=KID, tenant="t1", event_id="e1", subject_id="S1", content_digest="ab" * 32, scan=3,
                 matcher="m5:x", secret_scan="s1", mentions=["S2", "S3"], lease_scope="session:s1", lease_epoch=7,
                 batch_id="b1", index=0, count=2)
        f.update(over)
        return f

    def tenant_conn(self, ks, idents):
        """A tenant on this process's matcher, version 1, with `idents` {identifier: subject}."""
        hm = {}
        for name, subj in idents.items():
            for h in self.M.subject_hmacs(ks, "t1", [name]):
                hm[h] = subj
        ks.calls.clear()
        return FakeConn(version=1, matcher=self.M.matcher_id(), idents=hm)

    def authority(self, conn=None, ks=None):
        S = self.sealer()
        ks = ks or FakeKS()
        return S.Authority(conn or FakeConn(), ks, KID, SEAL_KEY), ks

    # ------------------------------------------------------------------ U1-U4: the attestation
    def test_u1_encoding_is_length_prefixed_in_octets(self):
        S = self.sealer()
        self.assertNotEqual(S.encode(["ab", "c"]), S.encode(["a", "bc"]))
        self.assertNotEqual(S.encode(["a:b"]), S.encode(["a", "b"]))
        self.assertEqual(S.encode(["é", "7"]), b"2:\xc3\xa91:7")
        self.assertNotEqual(S.message(**self.fields(mentions=["a,b"])), S.message(**self.fields(mentions=["a", "b"])))
        self.assertNotEqual(S.message(**self.fields(mentions=[])), S.message(**self.fields(mentions=[""])))
        with self.assertRaises((TypeError, ValueError)):
            S.encode(["a", None])
        with self.assertRaises((TypeError, ValueError)):
            S.message(**self.fields(matcher=None))

    def test_u2_every_field_is_bound(self):
        S = self.sealer()
        base = S.mac(SEAL_KEY, S.message(**self.fields()))
        changes = dict(key_id="k2", tenant="t2", event_id="e2", subject_id="S9", content_digest="cd" * 32, scan=4,
                       matcher="m5:y", secret_scan="s2", mentions=["S2"], lease_scope="session:s2", lease_epoch=8,
                       batch_id="b2", index=1, count=3)
        self.assertEqual(set(changes), set(self.fields()), "a field of the attestation is not exercised")
        seen = {base}
        for k, v in changes.items():
            m = S.mac(SEAL_KEY, S.message(**self.fields(**{k: v})))
            self.assertNotIn(m, seen, f"changing {k} did not change the MAC")
            seen.add(m)
        self.assertNotEqual(S.mac(SEAL_KEY, S.message(**self.fields(mentions=["S3", "S2"]))), base, "mention order is not bound")

    def test_u3_store_formula_is_hmac(self):
        S = self.sealer()
        ipad, opad = S.pads(SEAL_KEY)
        self.assertEqual((len(ipad), len(opad)), (64, 64))
        for msg in (b"", b"x", S.message(**self.fields()), "Ü".encode() * 500):
            want = hmac.new(SEAL_KEY, msg, hashlib.sha256).hexdigest()
            self.assertEqual(hashlib.sha256(opad + hashlib.sha256(ipad + msg).digest()).hexdigest(), want)
            self.assertEqual(S.mac(SEAL_KEY, msg), want)

    def test_u4_mac_key_is_the_authoritys_own(self):
        S = self.sealer()
        out = []
        for hk, ck in ((b"tenant-A", b"a" * 32), (b"tenant-B", b"b" * 32)):
            ks = FakeKS(hkey=hk, ckey=ck)
            a = S.Authority(FakeConn(), ks, KID, SEAL_KEY)
            r = a.seal("t1", "session:s1", 7, "b1", [{"event_id": "e1", "subject_id": "S1", "content": "plain words"}])[0]
            want = hmac.new(SEAL_KEY, S.message(KID, "t1", "e1", "S1", r["content_digest"], r["scan"], r["matcher"], r["secret_scan"],
                                                r["mentions"], "session:s1", 7, "b1", 0, 1), hashlib.sha256).hexdigest()
            self.assertEqual(r["seal_mac"], want)
            self.assertNotIn("chain_key", [c[0] for c in ks.calls], "the authority used the key service's chain key")
            out.append(r["seal_mac"])
        self.assertEqual(out[0], out[1], "the MAC depends on a key the key service holds")

    # ------------------------------------------------------------------ U5-U6: the authority
    def test_u5_seal_output(self):
        S = self.sealer()
        ks = FakeKS()
        conn = self.tenant_conn(ks, {NAME: "SX", "Zoë Ngüyen": "ÜnïS"})
        a = S.Authority(conn, ks, KID, SEAL_KEY)
        evs = [{"event_id": "e1", "subject_id": "SY", "content": "I met " + NAME + " and Zoë Ngüyen."},
               {"event_id": "e2", "subject_id": "SX", "content": "this is " + NAME + " speaking"}]
        rows = a.seal("t1", "session:s1", 7, "b1", evs)
        self.assertEqual([r["event_id"] for r in rows], ["e1", "e2"])
        self.assertEqual(rows[0]["mentions"], sorted(["SX", "ÜnïS"]))
        self.assertEqual(rows[1]["mentions"], [], "the speaker was listed as mentioning itself")
        for i, r in enumerate(rows):
            self.assertEqual((r["scan"], r["matcher"], r["secret_scan"]), (1, self.M.matcher_id(), self.SEC.VERSION))
            self.assertEqual((r["seal_key_id"], r["seal_batch"], r["seal_index"], r["seal_count"]), (KID, "b1", i, 2))
            self.assertEqual(r["content_digest"], hashlib.sha256(bytes(r["content_enc"])).hexdigest())
            self.assertEqual(bytes(r["content_enc"]), fake_seal(SUBJ_KEY, "t1", evs[i]["subject_id"], evs[i]["event_id"], evs[i]["content"]))
            m = S.message(KID, "t1", r["event_id"], evs[i]["subject_id"], r["content_digest"], r["scan"], r["matcher"], r["secret_scan"],
                          r["mentions"], "session:s1", 7, "b1", i, 2)
            self.assertEqual(r["seal_mac"], hmac.new(SEAL_KEY, m, hashlib.sha256).hexdigest())
        self.assertIn(("keys", True), ks.calls)
        self.assertLess(conn.sql.index(next(s for s in conn.sql if "identifier_epochs" in s)),
                        conn.sql.index(next(s for s in conn.sql if "subject_identifiers" in s)), "identifiers read before the version")

    def test_u6_refused_batches_seal_nothing(self):
        a, ks = self.authority()
        ev = [{"event_id": "e1", "subject_id": "S1", "content": "fine"}, {"event_id": "e2", "subject_id": "S1", "content": CRAFTED}]
        for evs, code in ((ev, "WPM02"), ([{"event_id": "e3", "subject_id": "S1", "content": "a" * 65537}], "WPM01"),
                          ([{"event_id": "e%d" % i, "subject_id": "S1", "content": "x"} for i in range(1001)], "WPM03")):
            with self.assertRaises(self.M.WriteRefused) as cm:
                a.seal("t1", "session:s1", 7, "b1", evs)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(self.SEC.SecretDetected):
            a.seal("t1", "session:s1", 7, "b1", [{"event_id": "e4", "subject_id": "S1", "content": "key AKIA" + "ABCDEFGHIJKLMNOP"}])
        self.assertEqual(ks.calls, [], "a refused batch reached the key service")

    # ------------------------------------------------------------------ U11: one scan binds both tables (review 42cfd11 R1)
    def test_u11_one_scan_both_purposes_and_prior_mentions(self):
        S = self.sealer()
        ks = FakeKS()
        conn = self.tenant_conn(ks, {NAME: "SX"})
        a = S.Authority(conn, ks, KID, SEAL_KEY)
        evs = [{"event_id": "e1", "subject_id": "SY", "content": "I met " + NAME},
               {"event_id": "e2", "subject_id": "SY", "content": "nobody named"}]
        req = {"tenant": "t1", "lease_scope": "session:s1", "lease_epoch": 7, "batch_id": "b1", "events": evs}
        n_scans = sum(1 for s in conn.sql if "subject_identifiers" in s)
        out = S.dispatch(a, "seal", dict(req, prior_mentions=[["SQ"], ["SY", "SP"]]))
        self.assertEqual(sum(1 for s in conn.sql if "subject_identifiers" in s) - n_scans, 1, "more than one scan for one seal")
        rows = out.get("ok") or []
        self.assertEqual([r["mentions"] for r in rows], [["SQ", "SX"], ["SP"]],
                         "an earlier scan's mention was dropped (the WPS17 retry), or the speaker listed")
        for i, r in enumerate(rows):
            macs = r.get("seal_macs") or {}
            self.assertEqual(set(macs), {"events", "quarantine"}, "the authority did not return both purposes from the one scan")
            for p in ("events", "quarantine"):
                m = S.message(KID, "t1", r["event_id"], "SY", r["content_digest"], r["scan"], r["matcher"], r["secret_scan"],
                              r["mentions"], "session:s1", 7, "b1", i, 2, p)
                self.assertEqual(macs[p], hmac.new(SEAL_KEY, m, hashlib.sha256).hexdigest(), p)
            self.assertEqual(r["seal_mac"], macs["events"])
            self.assertNotEqual(macs["events"], macs["quarantine"])
        for bad in ([["SQ"]], [["SQ"], "SP"], [["SQ"], [""]], [["SQ"], [None]], "SQ"):
            self.assertEqual(S.dispatch(a, "seal", dict(req, prior_mentions=bad)), {"error": "bad_request"}, repr(bad))
            with self.assertRaises(ValueError):
                a.seal("t1", "session:s1", 7, "b1", evs, prior_mentions=bad)
        r = a.seal("t1", "session:s1", 7, "b1", evs, purpose="quarantine", prior_mentions=[["SQ"], []])[0]
        self.assertEqual((r["mentions"], r["seal_mac"]), (["SQ", "SX"], r["seal_macs"]["quarantine"]))

    # ------------------------------------------------------------------ U12: append's binding scan (review 42cfd11 R1)
    def _r1_append(self, fail_states, strip_macs=False):
        """append() of one event by SY naming NAME (subject SX), against an in-process authority whose identifier rows
        for SX vanish after its first scan (an erasure's final pass). The events insert raises `fail_states` in turn
        (then succeeds). Returns (result or exception, seal calls [(purpose, prior)], inserts [(table, args)])."""
        import datetime
        S = self.sealer()
        ks = FakeKS()
        conn = self.tenant_conn(ks, {NAME: "SX"})
        real = S.Authority(conn, ks, KID, SEAL_KEY)
        calls, inserts, fails = [], [], list(fail_states)

        class Rec:
            def seal(self, *a, purpose="events", prior_mentions=None):
                calls.append((purpose, prior_mentions))
                kw = {"purpose": purpose} if prior_mentions is None else {"purpose": purpose, "prior_mentions": prior_mentions}
                rows = real.seal(*a, **kw)
                conn.idents.clear()                      # the cascade's final pass: SX can no longer be found
                return [{k: v for k, v in r.items() if k != "seal_macs"} for r in rows] if strip_macs else rows

        base_exec = conn.execute

        def execute(sql, args=()):
            if "insert into events" in sql or "insert into quarantine" in sql:
                table = "events" if "insert into events" in sql else "quarantine"
                if table == "events" and fails:
                    err = type("E", (self.LOG.psycopg.Error,), {"sqlstate": fails.pop(0)})
                    raise err("refused")
                inserts.append((table, args))
            return base_exec(sql, args)
        conn.execute = execute
        lease = types.SimpleNamespace(tenant="t1", scope="session:s1", epoch=1)
        ev = dict(subject_id="SY", kind="message", actor="user", trust_origin="user_typed",
                  ts_event=datetime.datetime.now(datetime.timezone.utc), content="I met " + NAME)
        try:
            res = self.LOG.append(conn, ks, lease, [ev], sealer=Rec())
        except Exception as e:                           # noqa: BLE001  (the type is asserted by each caller)
            res = e
        return res, calls, inserts

    def test_u12a_fenced_batch_is_quarantined_under_its_own_scan(self):
        S = self.sealer()
        res, calls, inserts = self._r1_append(["WPS01"])
        self.assertEqual([c[0] for c in calls], ["events"], "the stale path called the authority again (a rescan after the cascade)")
        self.assertEqual([t for t, _ in inserts], ["quarantine"], res)
        a = inserts[0][1]
        self.assertEqual(a[7], ["SX"], "the quarantine row lost the mention its first scan found")
        self.assertEqual(a[13], S.mac(SEAL_KEY, S.message(KID, "t1", a[8], "SY", hashlib.sha256(bytes(a[6])).hexdigest(), a[9], a[10],
                                                           a[11], ["SX"], "session:s1", 1, a[14], 0, 1, "quarantine")),
                         "the quarantine row is not attested by the scan that bound it")

    def test_u12b_retry_after_wps17_keeps_the_earlier_mention(self):
        res, calls, inserts = self._r1_append(["WPS17"])
        self.assertEqual([c[0] for c in calls], ["events", "events"], res)
        self.assertEqual(calls[1][1], [["SX"]], "the retry did not hand the authority the first scan's mentions")
        self.assertEqual([(t, a[13]) for t, a in inserts], [("events", ["SX"])], "the retried row dropped SX")

    def test_u12c_authority_without_both_attestations_is_refused(self):
        res, calls, inserts = self._r1_append([], strip_macs=True)
        self.assertEqual(type(res).__name__, "SealError", f"append ended with {res!r}")
        self.assertEqual(inserts, [], "a row was stored from an authority that returned one attestation")

    # ------------------------------------------------------------------ U7: the socket
    def test_u7_socket_peer_codes_and_log(self):
        S = self.sealer()
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.sock")
        uid = {"v": 20004}
        log = []
        ks = FakeKS()
        conn = self.tenant_conn(ks, {NAME: "SX"})
        srv = self.KSVC.Server(path, {20004}, make_store=lambda: S.Authority(conn, ks, KID, SEAL_KEY), uid_of=lambda s: uid["v"],
                               log=log.append, ops=S.OPS, dispatch_fn=S.dispatch)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            eid = uuid.uuid4().hex
            content = "I met " + NAME + " zqsealtext"
            c = S.Client(path)
            rows = c.seal("t1", "session:s1", 7, "batch-u7", [{"event_id": eid, "subject_id": "SY", "content": content}])
            self.assertEqual(rows[0]["mentions"], ["SX"])
            self.assertIsInstance(rows[0]["content_enc"], bytes)
            self.assertEqual(set(rows[0].get("seal_macs") or {}), {"events", "quarantine"}, "the socket lost an attestation (42cfd11 R1)")
            r2 = c.seal("t1", "session:s1", 7, "batch-u7p", [{"event_id": eid + "p", "subject_id": "SY", "content": "no name"}],
                        prior_mentions=[["SQ"]])[0]
            self.assertEqual(r2["mentions"], ["SQ"], "the socket dropped prior_mentions (42cfd11 R1)")
            with self.assertRaises(self.M.MatcherBudgetExceeded) as cm:
                c.seal("t1", "session:s1", 7, "batch-u7b", [{"event_id": "e9", "subject_id": "SY", "content": CRAFTED}])
            self.assertEqual(cm.exception.code, "WPM02")
            with self.assertRaises(self.SEC.SecretDetected):
                c.seal("t1", "session:s1", 7, "batch-u7c", [{"event_id": "e8", "subject_id": "SY", "content": "AKIA" + "ABCDEFGHIJKLMNOP"}])
            c.close()
            uid["v"] = 20001
            n = len(ks.calls)
            c = S.Client(path)
            with self.assertRaises(S.SealUnavailable):
                c.seal("t1", "session:s1", 7, "batch-u7d", [{"event_id": "e7", "subject_id": "SY", "content": "x"}])
            c.close()
            self.assertEqual(len(ks.calls), n, "a refused peer reached the authority")
        finally:
            srv.shutdown()
            srv.server_close()
        blob = "\n".join(log)
        self.assertTrue(log, "nothing logged")
        for v in (content, "zqsealtext", NAME, eid, "SY", "SX", SEAL_KEY.hex(), SUBJ_KEY.hex(), rows[0]["seal_mac"],
                  rows[0]["content_enc"].hex()[:32], rows[0]["content_digest"]):
            self.assertNotIn(v, blob, "the authority logged a value")

    # ------------------------------------------------------------------ U8: no authority, no write
    def test_u8_append_without_an_authority(self):
        import datetime
        tmp = tempfile.mkdtemp()
        kf = pathlib.Path(tmp) / "seal_key"
        fd = os.open(kf, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, json.dumps({"key_id": KID, "key": SEAL_KEY.hex()}).encode())
        os.close(fd)
        lease = types.SimpleNamespace(tenant="t1", scope="session:s1", epoch=1)
        ev = dict(subject_id="S1", kind="message", actor="user", trust_origin="user_typed",
                  ts_event=datetime.datetime.now(datetime.timezone.utc), content="hello")
        saved = {k: os.environ.get(k) for k in ("WP_SEALER", "WP_SECRETS_DIR")}
        try:
            for env in ({"WP_SEALER": os.path.join(tmp, "absent.sock"), "WP_SECRETS_DIR": tmp},      # socket named: never in-process
                        {"WP_SEALER": None, "WP_SECRETS_DIR": os.path.join(tmp, "empty")}):        # no socket, no key file
                for k, v in env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                ks, conn = FakeKS(), FakeConn()
                err = None
                try:
                    self.LOG.append(conn, ks, lease, [ev])
                except Exception as e:                   # noqa: BLE001  (the type is the contract, checked below)
                    err = e
                self.assertNotIn(("keys", True), ks.calls, "a subject key was created without an authority")
                self.assertEqual(conn.inserted, [], "an event was inserted without an authority")
                self.assertEqual(type(err).__name__, "SealUnavailable", f"append ended with {err!r}")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    # ------------------------------------------------------------------ U9: key handling
    def test_u9_key_file_and_key_service(self):
        S = self.sealer()
        tmp = tempfile.mkdtemp()
        kf = pathlib.Path(tmp) / "seal_key"
        kf.write_text(json.dumps({"key_id": KID, "key": SEAL_KEY.hex()}))
        os.chmod(kf, 0o644)
        with self.assertRaises((PermissionError, ValueError)):
            S.load_key(kf)
        os.chmod(kf, 0o600)
        self.assertEqual(S.load_key(kf), (KID, SEAL_KEY))
        self.assertEqual(stat.S_IMODE(kf.stat().st_mode), 0o600)
        self.assertEqual(tuple(self.KSVC.OPS), KEYSVC_OPS, "the key service's operations changed")
        self.assertEqual(tuple(S.OPS), ("seal",))

    # ------------------------------------------------------------------ U10: the store
    def test_u10_schema_statics(self):
        self.assertTrue(re.search(r"create table wp_seal\.seal_keys \(", SCHEMA), "no private key table")
        keys_table = _table(SCHEMA, "wp_seal.seal_keys")
        for col in ("key_id text primary key", "ipad bytea not null", "opad bytea not null", "active boolean not null"):
            self.assertTrue(col in keys_table, col)
        self.assertFalse(re.search(r"grant [^;]*wp_seal(?!er)", SCHEMA), "a privilege on wp_seal is granted")
        self.assertTrue("revoke all on schema wp_seal from public" in SCHEMA, "revoke all on schema wp_seal from public")
        self.assertTrue("revoke all on all functions in schema wp_seal from public" in SCHEMA, "revoke all on all functions in schema wp_seal from public")
        fns = _functions(SCHEMA)
        users = {n for n, body in fns.items() if "wp_seal." in body and not n.startswith("wp_seal.")}
        self.assertEqual(users, {"events_guard", "quarantine_guard"}, "wp_seal is reachable from another function")
        for g in ("events_guard", "quarantine_guard"):
            body = fns.get(g, "")
            self.assertTrue("WPS40" in body, f"{g} does not refuse a missing attestation")
            self.assertTrue("WPS41" in body, f"{g} does not refuse an invalid attestation")
        eg = fns.get("events_guard", "")
        self.assertTrue(0 <= eg.find("WPS40") < eg.find("wp_fence"), "the attestation is not checked before the fence")
        verify = "".join(b for n, b in fns.items() if n.startswith("wp_seal."))
        self.assertTrue("sha256(" in verify, "sha256(")
        self.assertTrue("is distinct from" in verify, "the MAC comparison can pass on a null")
        ev = _table(SCHEMA, "events")
        for col in ("seal_key_id text", "seal_mac text", "seal_batch text", "seal_index int", "seal_count int"):
            self.assertTrue(col in ev, f"events lacks {col}")
        q = _table(SCHEMA, "quarantine")
        for col in ("event_id text", "mention_scan_upto bigint", "mention_matcher text", "secret_scan text", "seal_key_id text",
                    "seal_mac text", "seal_batch text", "seal_index int", "seal_count int", "unique (tenant_id, event_id)"):
            self.assertTrue(col in q, f"quarantine lacks {col}")
        casc = fns.get("wp_erasure_cascade", "")
        self.assertTrue(re.search(r"delete from quarantine[^;]*mention_scan_upto <", casc), "the final pass keeps stale-scanned quarantine rows")
        setup = (HERE / "wp" / "setup.py").read_text()
        self.assertTrue("create schema wp_seal authorization wp_owner" in setup, "create schema wp_seal authorization wp_owner")
        self.assertTrue("drop schema if exists wp_seal cascade" in setup, "drop schema if exists wp_seal cascade")
        for f in ("test_d3_pg.py", "test_schema_broker_pg.py"):
            self.assertTrue("create schema wp_seal authorization wp_owner" in (HERE / "tests" / f).read_text(), f"{f} builds no wp_seal")


if __name__ == "__main__":
    unittest.main()
