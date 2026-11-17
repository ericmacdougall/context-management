"""Step 2 (authenticated sessions as the single root of identity), Postgres side. Same environment as tests/test_pg.py (paths to 0600
files only; skipped without it). Run first against e15241c (review-3 fixes, before step 2); red output in the policy owner's work dir
(red-artifacts/step2_pg_e15241c.out).

Acceptance (BRIEF-policy.md step 2):
  - a dc.sessions row is written only by dc.open_session, executable by dc_idp alone, and only from an authentication PROOF: a device's
    HMAC over (tenant, principal, channel, factor, nonce, expires) under the key of a factor enrolled by the owner. dc_idp cannot read
    factor keys, so it relays proofs and cannot mint a session from a bare principal id (the pre-step-2 signature is gone);
  - as dc_app (and as dc_idp) with raw SQL, minting, altering or reading sessions and factors is refused;
  - forged, expired, far-future, replayed (also concurrently: exactly one wins), revoked-principal, revoked-factor, wrong-channel,
    other-tenant and look-alike proofs are refused, and refused proofs write no session;
  - a forged, expired, revoked, ENDED or other-tenant session token is refused on every call (submit, explain, read_table,
    erase_candidates, erase_values), each next to the same call with a valid token that succeeds;
  - tenant, principal and channel are stamped from the session row; actor and trust_origin are derived from it and stamped on the event;
    a body naming any of them is refused identity_in_body;
  - look-alike principal ids cannot be installed in one tenant.
Every refusal test starts with a valid proof that works (positive control), so at e15241c, which has no proof path, it fails on that
assertion and can never pass vacuously. Proof keys are held in memory only and never printed."""
import hashlib
import json
import os
import secrets
import threading
import time
import unittest

try:
    import psycopg
    from pol import pgcore
except ImportError:              # pragma: no cover
    psycopg = None

from tests.test_pg import CFG, ENV, LAB, T

TB = "tenant-b"
CFG_B = dict(CFG, tenant=TB)
NO_KEY = bytes(32)


def _principals(tenant):
    from tests.kit import P
    return [P("alice", tenant=tenant), P("bob", tenant=tenant), P("bot", kind="agent", model="local", tenant=tenant),
            P("x", kind="analyst", tenant=tenant), P("admin", tenant=tenant)]


def _chan(pid):
    return "owner_ui" if CFG["principals"][pid] == "human" else "agent_api"


@unittest.skipUnless(psycopg is not None and all(os.environ.get(k) for k in ENV), "Postgres lab environment not configured")
class Step2Pg(unittest.TestCase):
    port = int(os.environ.get("POL_PG_PORT") or 0)

    def setUp(self):
        pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG, TB: CFG_B},
                       idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])
        self.raw = pgcore.connect_app(self.port, os.environ["POL_PG_APP_PASSFILE"])
        self.o = pgcore._connect("postgres", os.environ["POL_PG_SUPER_PASSFILE"], self.port, "pol-test-super")
        self.idp = pgcore.connect_idp(self.port, os.environ["POL_PG_IDP_PASSFILE"])
        for c in (self.raw, self.o, self.idp):
            self.addCleanup(c.close)

    # ---------------------------------------------------------------- helpers
    def _factor(self, tenant=T, pid="alice", channel=None):
        """Enrol through dc.enroll_factor as the owner; where that does not exist (e15241c) a placeholder factor, so the test reaches
        its positive control and fails THERE, on the missing proof path."""
        from tests.kit import LabFactor, enroll
        try:
            return enroll(self.o, tenant, pid, channel or _chan(pid))
        except psycopg.Error:
            return LabFactor("none", tenant, pid, channel or _chan(pid), NO_KEY)

    def _open(self, proof_text, ttl=60, conn=None):
        """('ok', {token, tenant, principal, channel, factor}) or ('refused', sqlstate)."""
        try:
            return "ok", (conn or self.idp).execute("select dc.open_session(%s, %s)", (proof_text, ttl)).fetchone()[0]
        except psycopg.Error as e:
            return "refused", e.sqlstate

    def _token(self, tenant=T, pid="alice", channel=None):
        from tests.kit import proof
        st, d = self._open(proof(self._factor(tenant, pid, channel)))
        self.assertEqual(st, "ok", "positive control: a valid proof must open a session (got %r)" % (d,))
        return d["token"]

    def _sessions(self):
        return self.o.execute("select count(*) from dc.sessions").fetchone()[0]

    def _refused_sql(self, conn, q, args=()):
        try:
            conn.execute(q, args)
        except psycopg.Error:
            return True
        return False

    def _propose(self, tok, cid, value="v", req="r1", subjects=(), conn=None, **extra):
        body = dict({"type": "propose", "client_id": cid, "idem": "k-" + cid, "req": req, "value": value,
                     "label": dict(LAB, subjects=list(subjects)), "based_on": 0}, **extra)
        from psycopg.types.json import Jsonb
        return (conn or self.raw).execute("select dc.submit(%s, %s)", (tok, Jsonb(body))).fetchone()[0]

    # ---------------------------------------------------------------- minting
    def test_bare_principal_mint_is_refused_for_every_role(self):
        for name, conn in (("dc_idp", self.idp), ("dc_app", self.raw)):
            self.assertTrue(self._refused_sql(conn, "select dc.open_session('tenant-a','alice','owner_ui','f',60)"),
                            "%s minted a session from a bare principal id" % name)
        self.assertEqual(self._sessions(), 0)

    def test_roles_cannot_mint_alter_or_read_sessions_and_factors(self):
        tok = self._token()
        from tests.kit import proof
        p = proof(self._factor(pid="bob"))
        for name, conn in (("dc_app", self.raw), ("dc_idp", self.idp)):
            for q in ("insert into dc.sessions values ('h','tenant-a','admin','owner_ui','f', now() + interval '1 hour', false)",
                      "update dc.sessions set revoked = false", "update dc.sessions set principal = 'admin'", "delete from dc.sessions",
                      "select * from dc.sessions", "select * from dc.factors", "select * from dc.used_proofs",
                      "update dc.factors set revoked = false", "select dc.enroll_factor('tenant-a','admin','owner_ui')"):
                self.assertTrue(self._refused_sql(conn, q), "%s: %s" % (name, q))
        self.assertTrue(self._refused_sql(self.raw, "select dc.open_session(%s, 60)", (p,)), "dc_app opened a session")
        self.assertTrue(self._refused_sql(self.raw, "select dc.end_session(%s)", (tok,)), "dc_app may not end sessions (dc_idp only)")

    # ---------------------------------------------------------------- proofs
    def test_valid_proof_opens_a_session_stamped_from_the_factor(self):
        from tests.kit import proof
        f = self._factor()
        st, d = self._open(proof(f))
        self.assertEqual(st, "ok", d)
        self.assertEqual((d["tenant"], d["principal"], d["channel"], d["factor"]), (T, "alice", "owner_ui", f.factor_id))
        row = self.o.execute("select tenant, principal, channel, factor from dc.sessions").fetchall()
        self.assertEqual(row, [(T, "alice", "owner_ui", f.factor_id)])
        self.assertEqual(self._propose(d["token"], "ok1")["code"], "proposed")

    def test_forged_proofs_are_refused_and_write_no_session(self):
        from tests.kit import proof
        a, b = self._factor(), self._factor(pid="bob")
        self.assertEqual(self._open(proof(a))[0], "ok", "positive control")
        n = self._sessions()
        good = proof(a)
        body, _, mac = good.rpartition(".")
        flip = mac[:-1] + ("0" if mac[-1] != "0" else "1")
        forged = {
            "flipped mac": body + "." + flip,
            "body edited, mac kept": body.replace('"alice"', '"bob"') + "." + mac,
            "signed with another factor's key": proof(a, key=b.key),
            "bob's factor id, alice's key": proof(a, factor=b.factor_id),
            "look-alike principal (a1ice)": proof(a, principal="a1ice"),
            "look-alike principal (aiice)": proof(a, principal="aiice"),
            "other principal on own factor": proof(a, principal="bob"),
            "channel not enrolled": proof(a, channel="owner_cli"),
            "agent channel for a human": proof(a, channel="agent_api"),
            "other tenant": proof(a, tenant=TB),
            "extra field": proof(a, admin=True),
            "nonce not hex": proof(a, nonce="zz" * 16),
            "expires not a number": proof(a, expires="soon"),
            "no mac": body,
            "not json": "x." + mac,
            "empty": "",
            "oversized": proof(a, pad="p" * 5000),
            "null": None,
        }
        for name, p in forged.items():
            st, d = self._open(p)
            self.assertEqual(st, "refused", "%s: accepted" % name)
        self.assertEqual(self._sessions(), n)

    def test_expired_and_far_future_proofs_are_refused(self):
        from tests.kit import proof
        f = self._factor()
        self.assertEqual(self._open(proof(f))[0], "ok", "positive control")
        for name, p in (("expired", proof(f, expires_in=-1)), ("beyond the 300 s window", proof(f, expires_in=3600))):
            self.assertEqual(self._open(p)[0], "refused", name)

    def test_proof_replay_is_refused(self):
        from tests.kit import proof
        p = proof(self._factor())
        self.assertEqual(self._open(p)[0], "ok", "positive control")
        self.assertEqual(self._open(p)[0], "refused", "replayed proof accepted")
        self.assertEqual(self._sessions(), 1)

    def test_concurrent_replay_exactly_one_wins(self):
        from tests.kit import proof
        p, n = proof(self._factor()), 8
        conns = [pgcore.connect_idp(self.port, os.environ["POL_PG_IDP_PASSFILE"]) for _ in range(n)]
        for c in conns:
            self.addCleanup(c.close)
        bar, out = threading.Barrier(n), [None] * n

        def run(i):
            bar.wait()
            out[i] = self._open(p, conn=conns[i])[0]
        ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(out), ["ok"] + ["refused"] * (n - 1))
        self.assertEqual(self._sessions(), 1)

    def test_revoked_principal_and_revoked_factor_are_refused(self):
        from tests.kit import proof
        a, b = self._factor(), self._factor(pid="bob")
        self.assertEqual(self._open(proof(a))[0], "ok", "positive control")
        self.o.execute("update dc.factors set revoked = true where factor = %s", (a.factor_id,))
        self.assertEqual(self._open(proof(a))[0], "refused", "revoked factor")
        self.assertEqual(self._open(proof(b))[0], "ok", "positive control (bob)")
        self.o.execute("update dc.principal set revoked = true where tenant = %s and pid = 'bob'", (T,))
        self.assertEqual(self._open(proof(b))[0], "refused", "revoked principal")

    def test_enrolment_refuses_unknown_principal_and_wrong_channel(self):
        from tests.kit import enroll, proof
        self.assertEqual(self._open(proof(self._factor()))[0], "ok", "positive control")
        for args in ((T, "mallory", "owner_ui"), (T, "alice", "agent_api"), (T, "bot", "owner_ui"), (T, "a1ice", "owner_ui"),
                     ("tenant-z", "alice", "owner_ui")):
            with self.assertRaises(psycopg.Error, msg=str(args)):
                enroll(self.o, *args)

    # ---------------------------------------------------------------- tokens on every call
    def _calls(self, tok):
        """The five dc_app entry points with `tok`: what each returns."""
        from psycopg.types.json import Jsonb
        q = lambda s, a: self.raw.execute(s, a).fetchone()[0]      # noqa: E731
        return {"submit": q("select dc.submit(%s, %s)", (tok, Jsonb({"type": "propose", "client_id": "c" + secrets.token_hex(4),
                                                                      "idem": "i" + secrets.token_hex(4), "req": "r1", "value": "w",
                                                                      "label": LAB, "based_on": 0})))["code"],
                "explain": q("select dc.explain(%s, %s)", (tok, "r1")),
                "read_table": q("select dc.read_table(%s)", (tok,)),
                "erase_candidates": q("select dc.erase_candidates(%s, %s)", (tok, "dana")),
                "erase_values": q("select dc.erase_values(%s, %s, %s)", (tok, "dana", ["nothing"]))}

    def test_bad_tokens_are_refused_on_every_call(self):
        alice = self._token()
        self.assertEqual(self._propose(alice, "seed", value="about dana", subjects=["dana"])["code"], "proposed")
        good = self._calls(self._token(pid="admin"))
        self.assertEqual(good["submit"], "unauthorized")                         # admin decides nothing on r1: a core decision, so authenticated
        self.assertIsNotNone(good["explain"])
        self.assertNotEqual(good["read_table"], [])
        self.assertIsNone(good["erase_candidates"]["refused"])
        self.assertNotEqual(good["erase_candidates"]["values"], [])
        self.assertIsNone(good["erase_values"]["refused"])
        expired, revoked, ended = self._token(pid="admin"), self._token(pid="admin"), self._token(pid="admin")
        h = lambda t: hashlib.sha256(t.encode()).hexdigest()                  # noqa: E731
        self.o.execute("update dc.sessions set expires_at = now() - interval '1 second' where token_hash = %s", (h(expired),))
        self.o.execute("update dc.sessions set revoked = true where token_hash = %s", (h(revoked),))
        self.assertIs(self.idp.execute("select dc.end_session(%s)", (ended,)).fetchone()[0], True)
        bad = {"forged": secrets.token_hex(32), "expired": expired, "revoked": revoked, "ended": ended, "empty": "", "null": None}
        for name, tok in bad.items():
            got = self._calls(tok)
            self.assertEqual(got["submit"], "unauthenticated:invalid_session", name)
            self.assertIsNone(got["explain"], name)
            self.assertEqual(got["read_table"], [], name)
            self.assertEqual(got["erase_candidates"], {"refused": "unauthorized", "values": []}, name)
            self.assertEqual(got["erase_values"], {"refused": "unauthorized"}, name)
        self.assertEqual(self.o.execute("select count(*) from dc.vals where tenant = %s and 'dana' = any(subjects)", (T,)).fetchone()[0], 1)

    def test_other_tenant_token_sees_and_changes_nothing_of_this_tenant(self):
        alice = self._token()
        self.assertEqual(self._propose(alice, "seed", value="about dana", subjects=["dana"])["code"], "proposed")
        mine = self._calls(self._token(pid="admin"))
        self.assertNotEqual(mine["erase_candidates"]["values"], [], "positive control")
        other = self._token(TB, "admin")
        got = self._calls(other)
        self.assertEqual(got["erase_candidates"], {"refused": None, "values": []})
        self.assertEqual(got["erase_values"]["erased"], 0)
        self.assertEqual(got["explain"]["tenant"], TB)
        self.assertNotIn("alice/seed", json.dumps([got["explain"], got["read_table"]]))
        self.assertIn("alice/seed", json.dumps(mine["explain"]), "positive control")
        bob_b = self._token(TB, "bob")
        self.assertEqual(self._propose(bob_b, "t1", tenant=T)["code"], "identity_in_body")
        n = self.o.execute("select count(*) from dc.events where tenant = %s", (T,)).fetchone()[0]
        self.assertEqual(self._propose(bob_b, "t2")["code"], "proposed")
        self.assertEqual(self.o.execute("select count(*) from dc.events where tenant = %s", (T,)).fetchone()[0], n)
        self.assertEqual(self.o.execute("select count(*) from dc.vals where tenant = %s and 'dana' = any(subjects)", (T,)).fetchone()[0], 1)

    # ---------------------------------------------------------------- stamping
    def test_events_carry_actor_and_trust_origin_derived_from_the_session(self):
        for pid, want in (("alice", ("user", "user_typed")), ("bot", ("assistant", "agent_generated"))):
            tok = self._token(pid=pid)
            self.assertEqual(self._propose(tok, "s-" + pid)["code"], "proposed")
            got = self.o.execute("select ev->>'actor', ev->>'trust_origin' from dc.events where ev->>'id' = %s", (pid + "/s-" + pid,)).fetchone()
            self.assertEqual(tuple(got), want, pid)

    def test_body_naming_identity_or_origin_is_refused(self):
        tok = self._token(pid="bot")
        for k, v in (("tenant", TB), ("principal", "alice"), ("channel", "owner_ui"), ("id", "alice/x"), ("actor", "user"),
                     ("trust_origin", "user_typed")):
            self.assertEqual(self._propose(tok, "f-" + k, **{k: v})["code"], "identity_in_body", k)
        self.assertEqual(self.o.execute("select count(*) from dc.events").fetchone()[0], 0)

    # ---------------------------------------------------------------- the composed bridge
    def _world(self):
        from tests.kit import World
        idp = pgcore.PgIdentity(self.idp, ttl_s=60)
        w = World(principals=_principals(T), core_config=CFG, identity=idp)
        return w, pgcore.PgSessionBridge(w.sessions), pgcore.PgDecisionService(w.enf, self.raw)

    def test_bridge_issue_and_end(self):
        from tests.kit import proof
        w, bridge, svc = self._world()
        ps = bridge.issue(proof(self._factor()))
        self.assertEqual(svc.submit(ps, {"type": "propose", "client_id": "b1", "idem": "k-b1", "req": "r1", "value": "v", "label": LAB,
                                         "based_on": 0})["code"], "proposed")
        bridge.end(ps)
        self.assertFalse(w.sessions.verify(ps.session))
        self.assertEqual(self._calls(ps.db_token)["submit"], "unauthenticated:invalid_session", "the database session outlived end()")

    def test_bridge_refuses_replay_and_leaves_nothing_live(self):
        from tests.kit import proof
        from pol.identity import AuthenticationFailed
        w, bridge, svc = self._world()
        p = proof(self._factor())
        bridge.issue(p)
        with self.assertRaises(AuthenticationFailed):
            bridge.issue(p)
        self.assertEqual(len(w.sessions._live), 1)

    def test_bridge_ends_the_database_session_when_the_directory_refuses(self):
        """The core knows `x` (analyst) but this Directory does not: the database session the proof opened must not stay usable."""
        from tests.kit import World, proof, P
        from pol.identity import AuthenticationFailed
        idp = pgcore.PgIdentity(self.idp, ttl_s=60)
        w = World(principals=[P("alice")], core_config=CFG, identity=idp)
        bridge = pgcore.PgSessionBridge(w.sessions)
        with self.assertRaises(AuthenticationFailed):
            bridge.issue(proof(self._factor(pid="x")))
        self.assertEqual(self.o.execute("select count(*) from dc.sessions where not revoked").fetchone()[0], 0)
        self.assertEqual(w.sessions._live, {})

    # ---------------------------------------------------------------- install
    def test_install_refuses_look_alike_principals(self):
        cfg = dict(CFG, principals=dict(CFG["principals"], a1ice="human"))
        with self.assertRaises(Exception):
            pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {T: cfg},
                           idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])
        self.assertEqual(self.o.execute("select count(*) from dc.principal where pid = 'a1ice'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
