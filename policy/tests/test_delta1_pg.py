"""Delta review 1 at d05d731 (reviews/policy-delta/review-1.md, run ad27751f), Postgres findings, reproduced first. Same environment as
tests/test_pg.py (paths to 0600 files only; skipped without it). Each test was run against d05d731 before its fix; red logs in
policy/results/red/.

M1  proof and session expiry used now(), fixed when the caller's transaction began. Every test opens an explicit transaction while the
    proof or token is valid, reads once (fixing now()), lets it expire by the wall clock, then calls: open_session and all five dc_app
    calls must refuse. The same family: a call that waits for a lock must judge the session again after the wait, and a REPEATABLE READ
    snapshot taken before an end_session or a factor revoke must not authenticate. Each refusal follows the same call succeeding
    (positive control), so none can pass vacuously.
M3  dc.submit under two overlapping submissions with one (principal, idempotency key) and different payloads: the accepted one stays
    retryable as `duplicate` with its original reference. dc.submit already serialises this with its per-principal transaction
    advisory lock, so this test is expected to PASS on d05d731 too: it is the reproducer for the claim that the Postgres path does not
    have M3's shape."""
import json
import os
import secrets
import threading
import time
import unittest

try:
    import psycopg
    from psycopg.types.json import Jsonb
    from pol import pgcore
except ImportError:              # pragma: no cover
    psycopg = None

from tests.kit import enroll, pg_token, proof
from tests.test_pg import CFG, ENV, LAB, T

CALLS = ("submit", "explain", "read_table", "erase_candidates", "erase_values")
LOCK_KEY = "select pg_advisory_xact_lock(hashtextextended(jsonb_build_array(%s::text, %s::text)::text, 0))"   # as dc._lock_event


@unittest.skipUnless(psycopg is not None and all(os.environ.get(k) for k in ENV), "Postgres lab environment not configured")
class _Base(unittest.TestCase):
    port = int(os.environ.get("POL_PG_PORT") or 0)

    def setUp(self):
        pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG},
                       idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])
        ap, ip = os.environ["POL_PG_APP_PASSFILE"], os.environ["POL_PG_IDP_PASSFILE"]
        self.app, self.app_t = pgcore.connect_app(self.port, ap, "d1-app"), pgcore.connect_app(self.port, ap, "d1-app-txn")
        self.idp, self.idp_t = pgcore.connect_idp(self.port, ip, "d1-idp"), pgcore.connect_idp(self.port, ip, "d1-idp-txn")
        self.o = pgcore._connect("postgres", os.environ["POL_PG_SUPER_PASSFILE"], self.port, "d1-super")
        self.hold = pgcore._connect("postgres", os.environ["POL_PG_SUPER_PASSFILE"], self.port, "d1-lock-holder")
        for c in (self.app, self.app_t, self.idp, self.idp_t, self.o, self.hold):
            self.addCleanup(c.close)

    def tok(self, pid="admin", ttl=60):
        return pg_token(self.o, self.idp, T, pid, "owner_ui" if CFG["principals"][pid] == "human" else "agent_api", ttl)

    def body(self, cid=None, value="w", idem=None, subjects=(), req="r1"):
        cid = cid or "c" + secrets.token_hex(4)
        return {"type": "propose", "client_id": cid, "idem": idem or "k-" + cid, "req": req, "value": value,
                "label": dict(LAB, subjects=list(subjects)), "based_on": 0}

    def submit(self, c, tok, body):
        return c.execute("select dc.submit(%s, %s)", (tok, Jsonb(body))).fetchone()[0]


class M1_WallClock(_Base):
    def setUp(self):
        super().setUp()
        out = self.submit(self.app, self.tok("alice"), self.body("seed", "about dana", subjects=["dana"]))
        self.assertEqual(out["code"], "proposed")
        self.ref = out["value_ref"]

    def raw(self, c, name, tok):
        if name == "submit":
            return self.submit(c, tok, self.body())
        q = {"explain": ("select dc.explain(%s, 'r1')", (tok,)), "read_table": ("select dc.read_table(%s)", (tok,)),
             "erase_candidates": ("select dc.erase_candidates(%s, 'dana')", (tok,)),
             "erase_values": ("select dc.erase_values(%s, 'dana', %s)", (tok, ["nothing"]))}[name]
        return c.execute(*q).fetchone()[0]

    @staticmethod
    def ok(name, r):
        """True when the call treated the token as a live session."""
        return {"submit": lambda: not r["code"].startswith("unauthenticated"), "explain": lambda: r is not None,
                "read_table": lambda: len(r) > 0, "erase_candidates": lambda: r["refused"] is None,
                "erase_values": lambda: r["refused"] is None}[name]()

    def dana_values(self):
        return self.o.execute("select count(*) from dc.vals where tenant = %s and 'dana' = any(subjects)", (T,)).fetchone()[0]

    def test_proof_that_expires_inside_a_transaction_is_refused(self):
        """d05d731: open_session compared the proof's expiry with now(); a dc_idp caller that began its transaction while the proof was
        valid could present it after expiry and get a session."""
        f = enroll(self.o, T, "admin", "owner_ui")
        c = self.idp_t
        c.execute("begin")
        c.execute("select now()")
        tok0 = c.execute("select dc.open_session(%s, 60)", (proof(f, expires_in=30),)).fetchone()[0]["token"]
        c.execute("commit")
        self.assertTrue(self.ok("submit", self.raw(self.app, "submit", tok0)), "positive control: a proof inside a transaction works")
        p = proof(f, expires_in=1.2)
        x = json.loads(p.rpartition(".")[0])["expires"]
        c.execute("begin")
        c.execute("select now()")
        time.sleep(max(0.0, x + 0.3 - time.time()))
        try:
            c.execute("select dc.open_session(%s, 60)", (p,))
            opened = True
        except psycopg.Error as e:
            opened, msg = False, e.diag.message_primary or ""
        c.execute("rollback")
        self.assertFalse(opened, "a proof that expired inside the transaction opened a session")
        self.assertEqual(msg, "proof refused: time")

    def test_token_that_expires_inside_a_transaction_is_refused_on_every_call(self):
        """d05d731: dc._session compared expires_at with now(); a dc_app caller that began its transaction while the token was valid
        kept writing, reading and erasing after expiry."""
        for name in CALLS:
            with self.subTest(name):
                tok = self.tok("admin", ttl=1)
                c = self.app_t
                c.execute("begin")
                c.execute("select now()")
                before = self.ok(name, self.raw(c, name, tok))
                time.sleep(1.3)
                after = self.ok(name, self.raw(c, name, tok))
                c.execute("rollback")
                self.assertTrue(before, "positive control: %s works before expiry" % name)
                self.assertFalse(after, "%s accepted a token that expired inside the transaction" % name)

    def _blocked_call(self, fn, hold_sql, hold_args, release_after):
        self.hold.execute("begin")
        self.hold.execute(hold_sql, hold_args)
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("r", fn()))
        t.start()
        time.sleep(release_after)
        self.hold.execute("commit")
        t.join(20)
        self.assertFalse(t.is_alive())
        return out["r"]

    def test_submit_judges_the_session_again_after_waiting_for_a_lock(self):
        """d05d731: dc.submit checked the session once, before _lock_event; a call that waited on the principal's advisory lock past
        its token's expiry was then applied. Any login role can take that advisory lock."""
        live = self._blocked_call(lambda: self.submit(self.app, self.tok("alice"), self.body("live")), LOCK_KEY, (T, "alice"), 0.3)
        self.assertEqual(live["code"], "proposed", "positive control: a short wait with a live token is applied")
        tok = self.tok("alice", ttl=2)
        late = self._blocked_call(lambda: self.submit(self.app, tok, self.body("late")), LOCK_KEY, (T, "alice"), 2.6)
        self.assertEqual(late["code"], "unauthenticated:invalid_session", "applied after the token expired during the lock wait")
        self.assertEqual(self.o.execute("select count(*) from dc.events where tenant = %s and evid = 'alice/late'", (T,)).fetchone()[0], 0)

    def test_erase_values_judges_the_session_again_after_waiting_for_a_lock(self):
        """d05d731: dc.erase_values checked the session before taking the tenant row FOR UPDATE; a call that waited there past its
        token's expiry then erased."""
        q = "select 1 from dc.tenant where tenant = %s for update"
        tok = self.tok("admin", ttl=2)
        late = self._blocked_call(lambda: self.app.execute("select dc.erase_values(%s, 'dana', %s)", (tok, [self.ref])).fetchone()[0],
                                  q, (T,), 2.6)
        self.assertEqual(late, {"refused": "unauthorized"}, "erased after the token expired during the lock wait")
        self.assertEqual(self.dana_values(), 1)
        live_tok = self.tok("admin")
        live = self._blocked_call(lambda: self.app.execute("select dc.erase_values(%s, 'dana', %s)", (live_tok, [self.ref])).fetchone()[0],
                                  q, (T,), 0.3)
        self.assertEqual((live["refused"], live["erased"]), (None, 1), "positive control: a short wait with a live token erases")

    def test_repeatable_read_snapshot_does_not_outlive_end_session(self):
        """d05d731: in a REPEATABLE READ transaction dc._session read the snapshot taken at the transaction's first read, so a token
        ended (dc.end_session, committed) after that kept working on every call."""
        for name in CALLS:
            with self.subTest(name):
                tok = self.tok("admin")
                self.assertTrue(self.ok(name, self.raw(self.app, name, tok)), "positive control: %s works with the live token" % name)
                c = self.app_t
                c.execute("begin isolation level repeatable read")
                c.execute("select 1")
                self.assertIs(self.idp.execute("select dc.end_session(%s)", (tok,)).fetchone()[0], True)
                after = self.ok(name, self.raw(c, name, tok))
                c.execute("rollback")
                self.assertFalse(after, "%s accepted a token ended after the transaction's snapshot" % name)

    def test_repeatable_read_snapshot_does_not_outlive_a_factor_revoke(self):
        """d05d731: open_session in a REPEATABLE READ transaction read the factor row from a snapshot taken before the factor was
        revoked, and opened a session from it."""
        g = enroll(self.o, T, "admin", "owner_ui")
        self.idp.execute("select dc.open_session(%s, 60)", (proof(g),))             # positive control (raises if refused)
        f = enroll(self.o, T, "admin", "owner_ui")
        c = self.idp_t
        c.execute("begin isolation level repeatable read")
        c.execute("select 1")
        self.o.execute("update dc.factors set revoked = true where factor = %s", (f.factor_id,))
        try:
            c.execute("select dc.open_session(%s, 60)", (proof(f),))
            opened = True
        except psycopg.Error:
            opened = False
        c.execute("rollback")
        self.assertFalse(opened, "a factor revoked after the snapshot opened a session")


class M3_PgOverlapping(_Base):
    """Two submissions with one (tenant, principal, idempotency key) overlap: A is accepted inside an open transaction, B arrives
    before A commits or rolls back. The per-principal advisory lock in dc._lock_event serializes them, so B decides after A's outcome.
    The *_different_requirements cases put B on another requirement, so no head row lock is shared and only that advisory lock
    serializes the pair (on the same requirement the head row FOR UPDATE serializes them as well)."""

    def _overlap(self, first_commits, req_b="r1"):
        tok = self.tok("alice")
        a, b = self.body("cA", "text A", idem="k-same"), self.body("cB", "text B", idem="k-same", req=req_b)
        self.app_t.execute("begin")
        ra = self.submit(self.app_t, tok, a)                     # holds the (tenant, alice) advisory lock until commit or rollback
        out = {}

        def run_b():
            try:
                out["B"] = self.submit(self.app, tok, b)
            except psycopg.Error as e:                          # a caller must get a decision, never a raw constraint error
                out["B"] = {"code": "error:" + type(e).__name__}
        t = threading.Thread(target=run_b)
        t.start()
        time.sleep(0.5)
        self.app_t.execute("commit" if first_commits else "rollback")
        t.join(20)
        self.assertFalse(t.is_alive())
        return tok, a, b, ra, out["B"]

    def _stored(self):
        return self.o.execute("select count(*) from dc.vals where tenant = %s and text in ('text A', 'text B')", (T,)).fetchone()[0]

    def _first_committed(self, req_b):
        tok, a, _b, ra, rb = self._overlap(True, req_b)
        self.assertEqual((ra["code"], rb["code"]), ("proposed", "idem_conflict"))
        retry = self.submit(self.app, tok, a)
        self.assertEqual((retry["code"], retry["value_ref"]), ("duplicate", ra["value_ref"]))
        self.assertEqual(self._stored(), 1)

    def _first_rolled_back(self, req_b):
        tok, _a, b, _ra, rb = self._overlap(False, req_b)
        self.assertEqual(rb["code"], "proposed")
        retry = self.submit(self.app, tok, b)
        self.assertEqual((retry["code"], retry["value_ref"]), ("duplicate", rb["value_ref"]))
        self.assertEqual(self._stored(), 1)

    def test_first_committed_stays_retryable(self):
        self._first_committed("r1")

    def test_first_rolled_back_leaves_the_second_retryable(self):
        self._first_rolled_back("r1")

    def test_first_committed_stays_retryable_different_requirements(self):
        self._first_committed("r2")

    def test_first_rolled_back_leaves_the_second_retryable_different_requirements(self):
        self._first_rolled_back("r2")


if __name__ == "__main__":
    unittest.main()
