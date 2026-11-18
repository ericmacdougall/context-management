"""S1, pinned authority (FINAL-JUDGMENT.md sections 4 and 5), on Postgres. Same environment as tests/test_pg.py (paths to 0600
files only; skipped without it). Written before the S1 code and run red against ea4677a, the recheck design (results/red/).

The oracle depends on where the call waits:
- BEFORE THE PINS (L0 relation locks, L1 authority lock): a change committed during the wait is judged by the pins that follow, so
  the call is refused (the oracle of tests/test_final_pg.py).
- AFTER THE PINS (L3-L8): no transaction can change the caller's authority until the call ends. The mutation waits FOR THE CALL
  (pg_blocking_pids of the mutation's backend contains the call's), the call applies, and only then does the mutation land, after
  which the token is refused. At ea4677a the mutation lands during the wait and the call is refused afterwards (red: "the mutation
  did not wait for the call").
- EXPIRY is a check with a ceiling. A call whose credential expires while it waits returns refused within 1 s of the expiry while
  the lock it waits for is STILL HELD. At ea4677a it waits until the holder lets go.
- THE APPLY PHASE never waits. A wait there fails the call (55P03) at once and writes nothing.

A docstring marked "S1 only" tests a mechanism that ea4677a does not have (its red is "did not wait" or a missing function, not a
wrong verdict). "Regression guard" means the test passes at ea4677a too."""
import os
import re
import secrets
import threading
import time
import unittest

try:
    import psycopg
    from psycopg.types.json import Jsonb  # noqa: F401
    from pol import pgcore
except ImportError:              # pragma: no cover
    psycopg = None

from tests.kit import enroll, pg_token, proof
from tests.test_delta1_pg import LOCK_KEY
from tests.test_final_pg import UNAUTH, _Wait
from tests.test_pg import T

A_CLS, T_CLS, S_CLS, N_CLS = 1145241601, 1145241602, 1145241603, 1145241604
ADV = "select pg_advisory_xact_lock(%s::int, hashtext(jsonb_build_array(%s::text, %s::text, %s::text)::text))"   # as dc._k
XWAIT = 2.0
POST = ("principal", "factor", "end", "revoke")


def adv(c, cls, kind, a, b=None):
    c.execute(ADV, (cls, kind, a, b))


class _S1(_Wait):
    def setUp(self):
        super().setUp()
        ap, ip, sp = os.environ["POL_PG_APP_PASSFILE"], os.environ["POL_PG_IDP_PASSFILE"], os.environ["POL_PG_SUPER_PASSFILE"]
        self.mut_o = pgcore._connect("postgres", sp, self.port, "s1-mut-owner")
        self.mut_idp = pgcore.connect_idp(self.port, ip, "s1-mut-idp")
        self.mut_app = pgcore.connect_app(self.port, ap, "s1-mut-app")
        for c in (self.mut_o, self.mut_idp, self.mut_app):
            self.addCleanup(c.close)
        self._threads = []

    def tearDown(self):
        """Before any cleanup closes a connection: a call that outlived its test (on the red tree a call can stay blocked past the
        test's join) is cancelled and its thread joined, so no connection is closed under a thread still inside libpq (that
        corrupted the process heap in a red run). Only this test database's active queries are cancelled; nothing else is signalled."""
        alive = [t for t in self._threads if t.is_alive()]
        if alive:
            self.o.execute("select pg_cancel_backend(pid) from pg_stat_activity where datname = current_database()"
                           " and pid <> pg_backend_pid() and state = 'active'")
            for t in alive:
                t.join(15)
        super().tearDown()

    def start(self, fn):
        out = {}

        def run():
            try:
                out["r"] = fn()
            except psycopg.Error as e:
                out["r"] = {"error": e.sqlstate, "message": e.diag.message_primary}
            out["t"] = time.time()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self._threads.append(t)
        return t, out

    def blocked_by(self, waiter, holder, timeout=5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.o.execute("select %s = any(pg_blocking_pids(%s))", (holder, waiter)).fetchone()[0]:
                return True
            time.sleep(0.02)
        return False

    def revoke_body(self, target):
        return {"type": "revoke", "client_id": "rv" + secrets.token_hex(3), "idem": "rv" + secrets.token_hex(3), "target": target}

    def mutation(self, how, tok, pid):
        """(connection, fn): a change of the authority of `tok` (principal `pid`) made on a connection of its own."""
        h = self.h(tok)
        if how == "principal":
            return self.mut_o, lambda: self.mut_o.execute("update dc.principal set revoked = true where tenant = %s and pid = %s", (T, pid))
        if how == "factor":
            return self.mut_o, lambda: self.mut_o.execute(
                "update dc.factors set revoked = true where factor = (select factor from dc.sessions where token_hash = %s)", (h,))
        if how == "end":
            return self.mut_idp, lambda: self.mut_idp.execute("select dc.end_session(%s)", (tok,)).fetchone()[0]
        if how == "revoke":
            adm = self.tok("admin")
            return self.mut_app, lambda: self.submit(self.mut_app, adm, self.revoke_body(pid))
        raise ValueError(how)

    def pinned(self, call_conn, fn, hold, how, tok, pid, hold_conn=None):
        """The S1 oracle after the pins: hold_conn holds what hold(conn) took; the call waits on it; the mutation starts and must be
        blocked BY THE CALL; then the hold is released. Returns (call result, mutation waited for the call, mutation result)."""
        hc = hold_conn or self.hold
        mc, mfn = self.mutation(how, tok, pid)
        hp, cp, mp = self.pid(hc), self.pid(call_conn), self.pid(mc)
        hc.execute("begin")
        hold(hc)
        ct, co = self.start(fn)
        mt = None
        try:
            self.assertTrue(self.blocked_by(cp, hp), "vacuous: the call did not wait on the held lock")
            mt, mo = self.start(mfn)
            waited = self.blocked_by(mp, cp, timeout=1.0)
        finally:
            hc.execute("commit")
        ct.join(30)
        if mt is not None:
            mt.join(30)
        self.assertFalse(ct.is_alive() or (mt is not None and mt.is_alive()), "stuck")
        return co.get("r"), waited, mo.get("r")

    def refused_after(self, how, tok, pid, call):
        """After the mutation landed, the same token must be refused by the same call."""
        return call(tok)

    def cleanup(self, how, pid):
        if how in ("principal", "revoke"):
            self.unrevoke(pid)

    def ceiling(self, call_conn, fn, hold, deadline, hold_conn=None):
        """The expiry ceiling: (result, returned while the lock was still held, seconds after the deadline it returned)."""
        hc = hold_conn or self.hold
        hp, cp = self.pid(hc), self.pid(call_conn)
        hc.execute("begin")
        hold(hc)
        ct, co = self.start(fn)
        try:
            self.assertTrue(self.blocked_by(cp, hp), "vacuous: the call did not wait on the held lock")
            ct.join(max(0.0, deadline - time.time()) + 1.0)
            held = not ct.is_alive()
        finally:
            hc.execute("rollback")
        ct.join(30)
        self.assertFalse(ct.is_alive(), "stuck")
        return co.get("r"), held, co["t"] - deadline

    def expires(self, tok):
        return float(self.o.execute("select extract(epoch from expires_at) from dc.sessions where token_hash = %s", (self.h(tok),)).fetchone()[0])

    def events(self, pid, cid):
        return self.count("select count(*) from dc.events where tenant = %s and evid = %s", T, pid + "/" + cid)


class S1_PinnedAuthority(_S1):
    """Points after the pins: the tenant lock, the idempotency lock, the head row, the subject lock (bob's open submit about the same
    new subject), the chain-head row; for erase_values the tenant lock, a value row and the token row. Four mutations each: the
    owner revokes the principal, the owner revokes the factor, dc_idp ends the session, an admin revokes the principal through
    dc.submit. Red at ea4677a for principal, factor and end (they land during the wait); the API revoke is a regression guard there
    (it waited for the call's tenant row FOR SHARE)."""

    def _submit_point(self, hold, req="r1", subject=False, hold_conn=None, hows=POST):
        for how in hows:
            with self.subTest(mutation=how):
                subj = "s" + secrets.token_hex(4) if subject else None
                tok, cid = self.tok("alice"), "c" + secrets.token_hex(4)
                body = self.body(cid, "text " + cid, subjects=[subj] if subj else (), req=req)
                r, waited, m = self.pinned(self.app, lambda: self.submit(self.app, tok, body), lambda c: hold(c, subj), how, tok, "alice",
                                           hold_conn=hold_conn)
                self.assertTrue(waited, "the mutation (%s) did not wait for the call" % how)
                self.assertEqual(r.get("code"), "proposed", "the call (authority pinned) after the wait, mutation %s: %r" % (how, r))
                self.assertFalse(isinstance(m, dict) and "error" in m, "the mutation failed: %r" % (m,))
                self.assertEqual(self.events("alice", cid), 1)
                again = self.submit(self.app, tok, self.body())
                self.assertIn(again["code"], (UNAUTH, "principal_revoked"), "the token after the mutation landed")
                self.cleanup(how, "alice")

    def test_tenant_lock(self):
        """S1 only: the tenant lock (L3), held exclusively by a revoke, grant or erasure in flight (was the tenant row)."""
        self._submit_point(lambda c, _s: adv(c, T_CLS, "tenant", T))

    def test_idempotency_lock(self):
        """The per-principal advisory lock (L4), which any login role can take."""
        self._submit_point(lambda c, _s: c.execute(LOCK_KEY, (T, "alice")))

    def test_head_row(self):
        """The requirement's head row FOR UPDATE (L5): a concurrent submission on the same requirement."""
        self._submit_point(lambda c, _s: c.execute("select 1 from dc.head where tenant = %s and req = 'r1' for update", (T,)))

    def test_subject_lock(self):
        """Bob's dc.submit about the same new subject (on r1) still open while alice submits about it on r2 (L6; was the
        subject-token unique index at ea4677a)."""
        bob = self.tok("bob")
        self._submit_point(lambda c, s: self.submit(c, bob, self.body("b" + secrets.token_hex(3), "bob on " + s, subjects=[s])), req="r2",
                           subject=True, hold_conn=self.app_t)

    def test_chain_head_row(self):
        """The tenant's chain-head row (L7), held by any concurrent event of the tenant until it commits."""
        self._submit_point(lambda c, _s: c.execute("select 1 from dc.chainhead where tenant = %s for update", (T,)), req="r2")

    def _erase_point(self, hold, hows=POST):
        for how in hows:
            with self.subTest(mutation=how):
                ref = self.submit(self.app, self.tok("alice"), self.body(None, "about dana", subjects=["dana"]))["value_ref"]
                tok = self.tok("admin")
                call = lambda t=tok: self.app.execute("select dc.erase_values(%s, 'dana', %s)", (t, [ref])).fetchone()[0]   # noqa: E731
                r, waited, m = self.pinned(self.app, call, hold(ref), how, tok, "admin")
                self.assertTrue(waited, "the mutation (%s) did not wait for erase_values" % how)
                self.assertEqual((r.get("refused"), r.get("erased")), (None, 1), "erase_values after the wait, mutation %s: %r" % (how, r))
                self.assertFalse(isinstance(m, dict) and "error" in m, "the mutation failed: %r" % (m,))
                self.assertEqual(self.count("select count(*) from dc.vals where tenant = %s and ref = %s", T, ref), 0)
                self.assertEqual(call(), {"refused": "unauthorized"}, "the token after the mutation landed")
                self.cleanup(how, "admin")

    def test_erase_tenant_lock(self):
        """S1 only: erase_values waits for the tenant lock (L3) held by a submit in flight."""
        self._erase_point(lambda ref: (lambda c: adv(c, T_CLS, "tenant", T)))

    def test_erase_value_row(self):
        """A value row held FOR UPDATE (L5.2)."""
        self._erase_point(lambda ref: (lambda c: c.execute("select 1 from dc.vals where tenant = %s and ref = %s for update", (T, ref))))

    def test_erase_token_row(self):
        """The subject's token row held FOR UPDATE (L5.3)."""
        self._erase_point(lambda ref: (lambda c: c.execute("select 1 from dc.subject_tokens where tenant = %s and subject = 'dana' for update", (T,))))


class S1_Control(_S1):
    def test_without_the_pins_a_mutation_lands_during_the_wait(self):
        """Control, for the green run: the pins, not the final judgment after them, make S1_PinnedAuthority pass. With FOR SHARE
        stripped from dc._pin, the owner's revoke of alice lands while her call waits on the head row, and the final judgment then
        refuses the call: the S1 oracle fails and the recheck oracle holds."""
        fn = "dc._pin(dc.sessions, bigint, boolean)"
        orig = self.o.execute("select pg_get_functiondef(%s::regprocedure)", (fn,)).fetchone()[0]
        self.assertEqual(orig.count(" for share"), 3)
        self.o.execute(orig.replace(" for share", ""))
        try:
            tok, cid = self.tok("alice"), "c" + secrets.token_hex(4)
            r, waited, _m = self.pinned(self.app, lambda: self.submit(self.app, tok, self.body(cid, "w")),
                                        lambda c: c.execute("select 1 from dc.head where tenant = %s and req = 'r1' for update", (T,)),
                                        "principal", tok, "alice")
            self.assertFalse(waited, "the mutation waited although nothing was pinned")
            self.assertEqual(r.get("code"), "principal_revoked")
            self.assertEqual(self.events("alice", cid), 0)
        finally:
            self.o.execute(orig)
            self.unrevoke("alice")


class S1_BeforeThePins(_S1):
    """Points before the pins (L0, L1) and the pin statements themselves. A change committed during the wait is judged by the pins
    that follow: the call is refused and writes nothing."""

    def _before(self, call, ok, hold, hows, pid="alice"):
        for how in hows:
            with self.subTest(mutation=how):
                tok = self.tok(pid)
                r, waited = self.blocked(self.app, lambda: call(tok), hold, how, tok, pid)
                self.assertTrue(waited, "vacuous: the call did not wait")
                self.assertFalse(ok(r), "answered after a change committed while it waited before its pins (%s): %r" % (how, r))
                self.cleanup(how, pid)

    def _submit(self, hold, hows=("principal", "factor", "end")):
        seen = []

        def call(tok):
            cid = "c" + secrets.token_hex(4)
            seen.append(cid)
            return self.submit(self.app, tok, self.body(cid, "text " + cid, req="r2"))
        self._before(call, lambda r: r.get("code") == "proposed", hold, hows)
        self.assertEqual(sum(self.events("alice", c) for c in seen), 0)

    def test_authority_lock(self):
        """S1 only: the caller's authority lock (L1) held exclusively (a revoke or end_session of the caller in flight)."""
        self._submit(lambda c: adv(c, A_CLS, "auth", T, "alice"), hows=("principal", "factor"))

    def test_events_table_lock(self):
        """Regression guard: a relation lock on dc.events (L0)."""
        self._submit(lambda c: c.execute("lock table dc.events in access exclusive mode"))

    def test_pin_waits_for_a_row_writer(self):
        """S1 only: a pin statement waits for a transaction that holds the row and writes it; it then locks and judges the newest
        version (PostgreSQL 13.2.1). At ea4677a nothing locks these rows, so the call does not wait."""
        for table, sql, want in (("sessions", "update dc.sessions set revoked = true where token_hash = %(h)s", UNAUTH),
                                 ("factors", "update dc.factors set revoked = true where factor = (select factor from dc.sessions where token_hash = %(h)s)", UNAUTH),
                                 ("principal", "update dc.principal set revoked = true where tenant = %(t)s and pid = 'alice'", "principal_revoked")):
            with self.subTest(table=table):
                tok, cid = self.tok("alice"), "c" + secrets.token_hex(4)
                r, waited = self.blocked(self.app, lambda: self.submit(self.app, tok, self.body(cid, "w", req="r2")),
                                         lambda c, sql=sql: c.execute(sql, {"h": self.h(tok), "t": T}), "live", tok, "alice")
                self.assertTrue(waited, "vacuous: the pin did not wait for the row's writer")
                self.assertEqual(r.get("code"), want)
                self.assertEqual(self.events("alice", cid), 0)
                self.unrevoke("alice")

    def _read(self, q, ok, table):
        self.assertEqual(self.submit(self.app, self.tok("alice"), self.body("seed", "about dana", subjects=["dana"]))["code"], "proposed")
        self._before(lambda tok: self.app.execute(q, (tok,)).fetchone()[0], ok,
                     lambda c: c.execute("lock table dc.%s in access exclusive mode" % table), ("principal", "factor", "end"), pid="admin")

    def test_explain_after_a_table_wait(self):
        """Regression guard: explain waits on a relation lock (L0), then pins."""
        self._read("select dc.explain(%s, 'r1')", lambda r: r is not None, "head")

    def test_read_table_after_a_table_wait(self):
        """Regression guard."""
        self._read("select dc.read_table(%s)", lambda r: bool(r), "head")

    def test_erase_candidates_after_a_table_wait(self):
        """Regression guard."""
        self._read("select dc.erase_candidates(%s, 'dana')", lambda r: r["refused"] is None and len(r["values"]) == 1, "vals")


class S1_RevokePath(_S1):
    """A revoke by admin. Its own pins come first (L2.0-2.2), then its target's rows (L2.3-2.4, L5): a wait on a target row comes
    after the admin's pins, so a change of the admin's authority waits for the revoke."""

    def _target_point(self, hold, target="bob", before=None):
        for how in POST:
            with self.subTest(mutation=how):
                self.unrevoke(target)
                if before:
                    before()
                tok = self.tok("admin")
                r, waited, m = self.pinned(self.app, lambda: self.submit(self.app, tok, self.revoke_body(target)), hold, how, tok, "admin")
                self.assertTrue(waited, "the mutation (%s) did not wait for the revoke" % how)
                self.assertEqual(r.get("code"), "revoked", "the revoke after the wait, mutation %s: %r" % (how, r))
                self.assertTrue(self.count("select revoked from dc.principal where tenant = %s and pid = %s", T, target))
                self.assertIn(self.submit(self.app, tok, self.revoke_body("x"))["code"], (UNAUTH, "principal_revoked"))
                self.cleanup(how, "admin")
        self.unrevoke(target)

    def test_target_principal_row(self):
        """The target's principal row held FOR UPDATE (L2.3)."""
        self._target_point(lambda c: c.execute("select 1 from dc.principal where tenant = %s and pid = 'bob' for update", (T,)))

    def test_target_props_rows(self):
        """The target's open proposal row held FOR UPDATE (L5.1). Each round x proposes afresh (the previous revoke invalidated it)."""
        def propose():
            self.assertEqual(self.submit(self.app, self.tok("x"), self.body(None, "x proposes"))["code"], "proposed")
        self._target_point(lambda c: c.execute("select 1 from dc.props where tenant = %s and author = 'x' and status = 'open' limit 1 for update", (T,)),
                           target="x", before=propose)

    def test_sessions_table_lock(self):
        """Regression guard: a SHARE lock on dc.sessions is waited for before the pins (L0); the change committed meanwhile refuses."""
        for how, want in (("principal", "principal_revoked"), ("factor", UNAUTH)):
            with self.subTest(how):
                self.unrevoke("bob")
                tok = self.tok("admin")
                r, waited = self.blocked(self.app, lambda: self.submit(self.app, tok, self.revoke_body("bob")),
                                         lambda c: c.execute("lock table dc.sessions in share mode"), how, tok, "admin")
                self.assertTrue(waited)
                self.assertEqual(r.get("code"), want)
                self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = 'bob'", T))
                self.cleanup(how, "admin")

    def test_revoke_waits_for_an_open_end_session(self):
        """S1: an open dc.end_session of the target holds the target's authority lock; the revoke waits for it (FIFO) and then
        revokes every session of the target. ea4677a skipped the held session row instead (red: it did not wait)."""
        for outcome in ("commit", "rollback"):
            with self.subTest(end_session=outcome):
                self.unrevoke("bob")
                bob1, bob2, adm = self.tok("bob"), self.tok("bob"), self.tok("admin")
                self.idp_t.execute("begin")
                self.assertIs(self.idp_t.execute("select dc.end_session(%s)", (bob1,)).fetchone()[0], True)
                t, out = self.start(lambda: self.submit(self.app, adm, self.revoke_body("bob")))
                waited = self.blocked_by(self.pid(self.app), self.pid(self.idp_t), timeout=1.5)
                self.idp_t.execute(outcome)
                t.join(20)
                self.assertTrue(waited, "the revoke did not wait for the open end_session")
                self.assertEqual(out["r"]["code"], "revoked")
                for tok in (bob1, bob2):
                    self.assertEqual(self.submit(self.app, tok, self.body())["code"], UNAUTH)

    def test_revoke_gives_up_after_xwait(self):
        """S1 only: an exclusive wait is capped at XWAIT (2 s). Past it the revoke fails with 55P03, applies nothing, and the
        queue behind it is released. ea4677a did not wait at all: it skipped the target's session row held by the open
        end_session and applied the revoke."""
        self.unrevoke("bob")
        adm = self.tok("admin")
        self.idp_t.execute("begin")
        self.idp_t.execute("select dc.end_session(%s)", (self.tok("bob"),))
        t0 = time.time()
        t, out = self.start(lambda: self.submit(self.app, adm, self.revoke_body("bob")))
        t.join(XWAIT + 2.5)
        alive = t.is_alive()
        self.idp_t.execute("rollback")
        t.join(20)
        self.assertFalse(alive, "the revoke was still waiting after XWAIT")
        self.assertEqual(out["r"].get("error"), "55P03", "the revoke after XWAIT: %r" % (out["r"],))
        self.assertGreater(out["t"] - t0, XWAIT - 0.3)
        self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = 'bob'", T))

    def test_self_revoke_is_applied(self):
        """Regression guard: an admin revoking itself (A exclusive directly) is applied, and its token is refused afterwards."""
        adm = self.tok("admin")
        self.assertEqual(self.submit(self.app, adm, self.revoke_body("admin"))["code"], "revoked")
        self.assertEqual(self.submit(self.app, adm, self.revoke_body("bob"))["code"], UNAUTH)
        self.unrevoke("admin")


class S1_ExpiryCeiling(_S1):
    """Expiry stays a check, read last; every acquisition wait is capped by the credential's remaining life. A call whose token or
    proof expires while it waits returns refused within 1 s of the expiry while the lock is STILL HELD, and writes nothing. ea4677a
    waits until the holder lets go (red for every point it has)."""

    def _submit(self, hold, req="r1", hold_conn=None):
        tok, cid = self.tok("alice", ttl=2), "c" + secrets.token_hex(4)
        r, held, late = self.ceiling(self.app, lambda: self.submit(self.app, tok, self.body(cid, "w", req=req)), hold, self.expires(tok),
                                     hold_conn=hold_conn)
        self.assertTrue(held, "the call was still waiting 1 s after its token expired")
        self.assertLess(late, 1.0)
        self.assertEqual(r.get("code"), UNAUTH, "%r" % (r,))
        self.assertEqual(self.events("alice", cid), 0)

    def test_idempotency_lock(self):
        self._submit(lambda c: c.execute(LOCK_KEY, (T, "alice")))

    def test_head_row(self):
        self._submit(lambda c: c.execute("select 1 from dc.head where tenant = %s and req = 'r1' for update", (T,)))

    def test_chain_head_row(self):
        self._submit(lambda c: c.execute("select 1 from dc.chainhead where tenant = %s for update", (T,)), req="r2")

    def test_events_table_lock(self):
        self._submit(lambda c: c.execute("lock table dc.events in access exclusive mode"), req="r2")

    def test_tenant_lock(self):
        """S1 only."""
        self._submit(lambda c: adv(c, T_CLS, "tenant", T))

    def test_authority_lock(self):
        """S1 only."""
        self._submit(lambda c: adv(c, A_CLS, "auth", T, "alice"))

    def test_erase_value_row(self):
        ref = self.submit(self.app, self.tok("alice"), self.body(None, "about dana", subjects=["dana"]))["value_ref"]
        tok = self.tok("admin", ttl=2)
        r, held, late = self.ceiling(self.app, lambda: self.app.execute("select dc.erase_values(%s, 'dana', %s)", (tok, [ref])).fetchone()[0],
                                     lambda c: c.execute("select 1 from dc.vals where tenant = %s and ref = %s for update", (T, ref)),
                                     self.expires(tok))
        self.assertTrue(held and late < 1.0, "erase_values still waiting after expiry (late %.2f s)" % late)
        self.assertEqual(r, {"refused": "unauthorized"})
        self.assertEqual(self.count("select count(*) from dc.vals where tenant = %s and ref = %s", T, ref), 1)

    def test_revoke_target_row(self):
        tok = self.tok("admin", ttl=2)
        r, held, late = self.ceiling(self.app, lambda: self.submit(self.app, tok, self.revoke_body("bob")),
                                     lambda c: c.execute("select 1 from dc.principal where tenant = %s and pid = 'bob' for update", (T,)),
                                     self.expires(tok))
        self.assertTrue(held and late < 1.0, "the revoke still waiting after expiry (late %.2f s)" % late)
        self.assertEqual(r.get("code"), UNAUTH)
        self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = 'bob'", T))

    def test_explain_table_lock(self):
        tok = self.tok("admin", ttl=2)
        r, held, late = self.ceiling(self.app, lambda: self.app.execute("select dc.explain(%s, 'r1')", (tok,)).fetchone()[0],
                                     lambda c: c.execute("lock table dc.head in access exclusive mode"), self.expires(tok))
        self.assertTrue(held and late < 1.0, "explain still waiting after expiry (late %.2f s)" % late)
        self.assertIsNone(r)

    def test_open_session_twin(self):
        """The proof expires while open_session waits for a concurrent presentation of the same proof (L8)."""
        f = enroll(self.o, T, "bob", "owner_ui")
        p = proof(f, expires_in=3.0)
        deadline = float(__import__("json").loads(p.rsplit(".", 1)[0])["expires"])
        r, held, late = self.ceiling(self.idp, lambda: self.idp.execute("select dc.open_session(%s, 60)", (p,)).fetchone()[0],
                                     lambda c: c.execute("select dc.open_session(%s, 60)", (p,)), deadline, hold_conn=self.idp_t)
        self.assertTrue(held and late < 1.0, "open_session still waiting after the proof expired (late %.2f s)" % late)
        self.assertEqual(r.get("message"), "proof refused: time", "%r" % (r,))
        self.assertEqual(self.count("select count(*) from dc.sessions where factor = %s", f.factor_id), 0)

    def test_the_callers_own_shorter_lock_timeout_still_applies(self):
        """Regression guard: the budget never lengthens the caller's own lock_timeout; its expiry is re-raised as 55P03."""
        tok = self.tok("alice")
        self.app_t.execute("begin")
        self.app_t.execute("set local lock_timeout = '300ms'")
        self.hold.execute("begin")
        self.hold.execute("select 1 from dc.head where tenant = %s and req = 'r1' for update", (T,))
        try:
            t0 = time.time()
            try:
                r = self.submit(self.app_t, tok, self.body())
            except psycopg.Error as e:
                r = {"error": e.sqlstate}
            took = time.time() - t0
        finally:
            self.hold.execute("rollback")
            self.app_t.execute("rollback")
        self.assertEqual(r.get("error"), "55P03")
        self.assertLess(took, 1.5)


class S1_ApplyNeverWaits(_S1):
    """The apply phase and everything after the final judgment run under lock_timeout 1 ms: a wait there fails the call (55P03)
    and writes nothing, instead of waiting while authority lapses."""

    def _variant(self, fn, old, new):
        """Recreate function `fn` (regprocedure text) with `old` replaced by `new`; returns the original definition."""
        orig = self.o.execute("select pg_get_functiondef(%s::regprocedure)", (fn,)).fetchone()[0]
        self.assertEqual(orig.count(old), 1, "variant anchor in %s" % fn)
        self.o.execute(orig.replace(old, new))
        return orig

    def test_a_wait_after_the_judgment_fails_the_call(self):
        """S1 only. Broken variant: the revoke's target pins (dc._lock_target) do nothing, so its authority writes after the
        judgment meet a held target principal row: 55P03 at once, nothing applied."""
        orig = self.o.execute("select pg_get_functiondef('dc._lock_target(text, text, timestamptz, bigint)'::regprocedure)").fetchone()[0]
        self.o.execute("create or replace function dc._lock_target(p_tenant text, tgt text, p_deadline timestamptz, p_lt bigint) returns void "
                       "language plpgsql as $$ begin end $$")
        try:
            tok = self.tok("admin")
            r, waited = self.blocked(self.app, lambda: self.submit(self.app, tok, self.revoke_body("bob")),
                                     lambda c: c.execute("select 1 from dc.principal where tenant = %s and pid = 'bob' for update", (T,)),
                                     lambda: None, tok, "admin")
            self.assertEqual(r.get("error"), "55P03", "%r" % (r,))
            self.assertFalse(waited, "the call waited after its judgment")
            self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = 'bob'", T))
        finally:
            self.o.execute(orig)

    def test_the_stage_never_waits(self):
        """S1 only. Broken variant: dc.submit's acquisition without the chain-head lock (L7). The event insert then meets the held
        chain head inside the stage: 55P03 at once, nothing written."""
        orig = self._variant("dc._acquire_submit(dc.sessions, jsonb, bigint)",
                             "perform 1 from dc.chainhead where tenant = s.tenant for update;", "null;")
        try:
            tok, cid = self.tok("alice"), "c" + secrets.token_hex(4)
            r, waited = self.blocked(self.app, lambda: self.submit(self.app, tok, self.body(cid, "w", req="r2")),
                                     lambda c: c.execute("select 1 from dc.chainhead where tenant = %s for update", (T,)), lambda: None, tok, "alice")
            self.assertEqual(r.get("error"), "55P03", "%r" % (r,))
            self.assertFalse(waited)
            self.assertEqual(self.events("alice", cid), 0)
        finally:
            self.o.execute(orig)

    def test_subject_token_unique_index(self):
        """Raw SQL holding an uncommitted subject token (no API call does: submits take the subject lock first). The insert would
        wait inside the stage; it fails the call at once instead and writes nothing. ea4677a waited there, past its judgment (P1a)."""
        tok, cid, subj = self.tok("alice"), "c" + secrets.token_hex(4), "s" + secrets.token_hex(4)
        r, waited = self.blocked(self.app, lambda: self.submit(self.app, tok, self.body(cid, "text " + cid, subjects=[subj])),
                                 lambda c: c.execute("insert into dc.subject_tokens values (%s, %s, %s)", (T, subj, "subj-held-" + subj)),
                                 lambda: time.sleep(0.5), tok, "alice", release="rollback")
        self.assertEqual(r.get("error"), "55P03", "%r" % (r,))
        self.assertFalse(waited)
        self.assertEqual(self.events("alice", cid), 0)
        self.assertEqual(self.count("select count(*) from dc.vals where tenant = %s and text = %s", T, "text " + cid), 0)

    def test_the_callers_lock_timeout_is_restored(self):
        """Regression guard: every exit path (applied, refused, read) restores the caller's lock_timeout."""
        tok, late = self.tok("admin"), self.tok("admin")
        self.o.execute("update dc.sessions set expires_at = clock_timestamp() - interval '1 second' where token_hash = %s", (self.h(late),))
        self.app.execute("begin")
        try:
            self.app.execute("set local lock_timeout = '7s'")
            self.assertEqual(self.submit(self.app, tok, self.revoke_body("bob"))["code"], "revoked")
            self.assertEqual(self.app.execute("show lock_timeout").fetchone()[0], "7s")
            self.assertEqual(self.submit(self.app, late, self.revoke_body("bob"))["code"], UNAUTH)
            self.assertEqual(self.app.execute("show lock_timeout").fetchone()[0], "7s")
            self.app.execute("select dc.explain(%s, 'r1')", (tok,))
            self.assertEqual(self.app.execute("show lock_timeout").fetchone()[0], "7s")
        finally:
            self.app.execute("rollback")


class S1_LockOrder(_S1):
    """The global order (FINAL-JUDGMENT.md 4.2): every call records the (level, key) of each lock it requests in dc.trace, and
    dc._at refuses (SQLSTATE DCORD) a request below one already made. S1 only (ea4677a has neither)."""
    EXPECTED = {"submit": [0, 1, 2, 3, 4, 5, 6, 7], "submit_revoke": [0, 1, 2, 3, 4, 5, 7], "submit_grant": [0, 1, 2, 3, 4, 5, 7],
                "explain": [0, 1, 2], "read_table": [0, 1, 2], "erase_candidates": [0, 1, 2], "erase_values": [0, 1, 2, 3, 5],
                "open_session": [0, 1, 2, 8], "end_session": [0, 1, 2]}

    def _trace(self, c, fn):
        c.execute("begin")
        try:
            fn()
            raw = c.execute("select current_setting('dc.trace', true)").fetchone()[0]
        finally:
            c.execute("rollback")
        self.assertTrue(raw, "no lock trace recorded")
        return [tuple(int(x) for x in e.split(":")) for e in raw.strip(",").split(",")]

    def test_every_entry_point_follows_the_declared_order(self):
        self.assertEqual(self.submit(self.app, self.tok("x"), self.body(None, "x proposes"))["code"], "proposed")
        ref = self.submit(self.app, self.tok("alice"), self.body(None, "about dana", subjects=["dana"]))["value_ref"]
        adm, al = self.tok("admin"), self.tok("alice")
        f = enroll(self.o, T, "bob", "owner_ui")
        a, i = self.app_t, self.idp_t
        runs = {"submit": (a, lambda: self.submit(a, al, self.body(None, "v", subjects=["dana", "erin"]))),
                "submit_revoke": (a, lambda: self.submit(a, adm, self.revoke_body("x"))),
                "submit_grant": (a, lambda: self.submit(a, adm, {"type": "grant", "client_id": "g" + secrets.token_hex(3),
                                                                  "idem": "g" + secrets.token_hex(3), "req": "r2", "target": "bob", "role": "decide"})),
                "explain": (a, lambda: a.execute("select dc.explain(%s, 'r1')", (adm,))),
                "read_table": (a, lambda: a.execute("select dc.read_table(%s)", (adm,))),
                "erase_candidates": (a, lambda: a.execute("select dc.erase_candidates(%s, 'dana')", (adm,))),
                "erase_values": (a, lambda: a.execute("select dc.erase_values(%s, 'dana', %s)", (adm, [ref]))),
                "open_session": (i, lambda: i.execute("select dc.open_session(%s, 60)", (proof(f),))),
                "end_session": (i, lambda: i.execute("select dc.end_session(%s)", (al,)))}
        for name, (c, fn) in runs.items():
            with self.subTest(name):
                tr = self._trace(c, fn)
                self.assertEqual(tr, sorted(tr), "%s took a lock below one it held: %r" % (name, tr))
                levels = [lv for k, lv in enumerate(x[0] for x in tr) if k == 0 or lv != tr[k - 1][0]]
                self.assertEqual(levels, self.EXPECTED[name], "%s: levels %r" % (name, tr))

    def test_an_out_of_order_lock_is_refused(self):
        for first, second in (((3, 0), (2, 0)), ((1, 50), (1, 7))):
            with self.subTest(first=first, second=second):
                self.o.execute("begin")
                try:
                    self.o.execute("select dc._at(%s, %s)", first)
                    with self.assertRaises(psycopg.Error) as cm:
                        self.o.execute("select dc._at(%s, %s)", second)
                    self.assertEqual(cm.exception.sqlstate, "DCORD")
                finally:
                    self.o.execute("rollback")

    def test_grant_state_is_add_only(self):
        """Enumerates every statement of the core that writes dc.role or a principal's kind or admin flag (regression guard)."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pol", "_pgcore.sql"), encoding="utf-8").read()
        body = src.split("set role dc_owner;", 1)[1]
        self.assertEqual(re.findall(r"(?i)\b(?:update|delete\s+from|truncate(?:\s+table)?)\s+dc\.role\b", body), [])
        ins = re.findall(r"(?i)insert\s+into\s+dc\.role\b[^;]*;", body)
        self.assertEqual(len(ins), 1)
        self.assertIn("on conflict do nothing", ins[0])
        sets = re.findall(r"(?i)update\s+dc\.principal\s+set\s+([^;]*?)\s+where", body)
        self.assertTrue(sets)
        self.assertEqual(len(re.findall(r"(?i)update\s+dc\.principal\b", body)), len(sets), "an aliased update of dc.principal")
        self.assertEqual(set(sets), {"revoked = true"}, "a principal column other than revoked is written: %r" % sets)


class S1_MutualWaits(_S1):
    """Pairs of entry points that wait for each other: none may deadlock (40P01), and each pair ends in a serial outcome."""

    def _two_admins(self):
        self.o.execute("insert into dc.principal(tenant, pid, kind, admin) values (%s, 'admin2', 'human', true) on conflict do nothing", (T,))
        return lambda: pg_token(self.o, self.idp, T, "admin2", "owner_ui", 60)

    def test_crossed_revokes(self):
        """Regression guard: admin revokes admin2 while admin2 revokes admin, eight rounds: exactly one applies, no deadlock."""
        adm2 = self._two_admins()
        ap = os.environ["POL_PG_APP_PASSFILE"]
        c1, c2 = pgcore.connect_app(self.port, ap, "s1-x1"), pgcore.connect_app(self.port, ap, "s1-x2")
        self.addCleanup(c1.close)
        self.addCleanup(c2.close)
        for i in range(8):
            with self.subTest(round=i):
                t1, t2 = self.tok("admin"), adm2()
                go = threading.Barrier(2)
                out = {}

                def run(k, c, tok, target):
                    go.wait()
                    try:
                        out[k] = self.submit(c, tok, self.revoke_body(target))["code"]
                    except psycopg.Error as e:
                        out[k] = "error:" + e.sqlstate
                ts = [threading.Thread(target=run, args=("a", c1, t1, "admin2")), threading.Thread(target=run, args=("b", c2, t2, "admin"))]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(20)
                codes = sorted(out.values())
                self.assertEqual(codes.count("revoked"), 1, "%r" % out)
                self.assertTrue(set(codes) - {"revoked"} <= {UNAUTH, "principal_revoked"}, "%r" % out)
                n = self.count("select count(*) from dc.principal where tenant = %s and pid in ('admin', 'admin2') and revoked", T)
                self.assertEqual(n, 1)
                self.unrevoke("admin")
                self.unrevoke("admin2")

    def test_revoke_waits_for_a_held_call(self):
        """Regression guard (ea4677a: the tenant row): alice's submit in a transaction she holds open; the revoke of alice waits
        for her COMMIT, then invalidates the proposal she just made. No open proposal of a revoked principal is left."""
        al, adm = self.tok("alice"), self.tok("admin")
        self.app_t.execute("begin")
        self.assertEqual(self.submit(self.app_t, al, self.body("held", "held"))["code"], "proposed")
        t, out = self.start(lambda: self.submit(self.app, adm, self.revoke_body("alice")))
        waited = self.blocked_by(self.pid(self.app), self.pid(self.app_t), timeout=1.0)
        self.app_t.execute("commit")
        t.join(20)
        self.assertTrue(waited)
        self.assertEqual(out["r"]["code"], "revoked")
        self.assertEqual(self.count("select count(*) from dc.props where tenant = %s and author = 'alice' and status = 'open'", T), 0)
        self.unrevoke("alice")

    def test_end_session_waits_for_the_call(self):
        """F1 P1b: an in-flight call (here explain in a transaction held open) has its session pinned; end_session waits for it (L1
        exclusive, FIFO) and then ends the session. Red at ea4677a: end_session did not wait."""
        tok = self.tok("alice")
        self.app_t.execute("begin")
        self.app_t.execute("select dc.explain(%s, 'r1')", (tok,))
        t, out = self.start(lambda: self.idp.execute("select dc.end_session(%s)", (tok,)).fetchone()[0])
        waited = self.blocked_by(self.pid(self.idp), self.pid(self.app_t), timeout=1.0)
        self.app_t.execute("commit")
        t.join(20)
        self.assertTrue(waited, "end_session did not wait for the in-flight call")
        self.assertIs(out["r"], True)
        self.assertEqual(self.submit(self.app, tok, self.body())["code"], UNAUTH)

    def _stream(self, change, seconds=6.0, start_after=1.0, within=2.5):
        """Three principals submit in overlapping transactions (each held 0.25 s) for `seconds`; `change` runs after
        `start_after` and must finish within `within` seconds (no starvation)."""
        ap = os.environ["POL_PG_APP_PASSFILE"]
        stop, errs = threading.Event(), []
        toks = {p: self.tok(p) for p in ("alice", "bob", "bot")}

        def loop(pid):
            c = pgcore.connect_app(self.port, ap, "s1-stream-" + pid)
            try:
                while not stop.is_set():
                    c.execute("begin")
                    try:
                        self.submit(c, toks[pid], self.body(None, "stream", req="r2" if pid == "alice" else "r1"))
                        time.sleep(0.25)
                        c.execute("commit")
                    except psycopg.Error as e:
                        errs.append(e.sqlstate)
                        c.execute("rollback")
            finally:
                c.close()
        ts = [threading.Thread(target=loop, args=(p,), daemon=True) for p in toks]
        for t in ts:
            t.start()
        time.sleep(start_after)
        t, out = self.start(change)
        t.join(within)
        done = not t.is_alive()
        t.join(seconds)
        stop.set()
        for x in ts:
            x.join(10)
        self.assertNotIn("40P01", errs)
        return done, out.get("r")

    def test_revoke_is_not_starved_by_a_stream_of_submits(self):
        """A revoke (tenant lock exclusive) while three principals keep the tenant busy. Red at ea4677a if the tenant row's share
        lockers starve it (they join the share set while the revoke waits)."""
        adm = self.tok("admin")
        done, r = self._stream(lambda: self.submit(self.app, adm, self.revoke_body("x")))
        self.assertTrue(done, "the revoke was starved by the stream")
        self.assertEqual(r.get("code"), "revoked")

    def test_erasure_is_not_starved_by_a_stream_of_submits(self):
        """The same for erase_values (the write path's known erasure-starvation shape)."""
        ref = self.submit(self.app, self.tok("alice"), self.body(None, "about dana", subjects=["dana"]))["value_ref"]
        adm = self.tok("admin")
        done, r = self._stream(lambda: self.app.execute("select dc.erase_values(%s, 'dana', %s)", (adm, [ref])).fetchone()[0])
        self.assertTrue(done, "erase_values was starved by the stream")
        self.assertEqual((r.get("refused"), r.get("erased")), (None, 1))

    def test_open_session_vs_revoke(self):
        """An open_session of bob held open; the revoke of bob waits for it (L1), then revokes the new session too. Red at
        ea4677a: the revoke did not wait (and did not see the new session)."""
        adm = self.tok("admin")
        self.idp_t.execute("begin")
        tok = self.idp_t.execute("select dc.open_session(%s, 60)", (proof(enroll(self.o, T, "bob", "owner_ui")),)).fetchone()[0]["token"]
        t, out = self.start(lambda: self.submit(self.app, adm, self.revoke_body("bob")))
        waited = self.blocked_by(self.pid(self.app), self.pid(self.idp_t), timeout=1.0)
        self.idp_t.execute("commit")
        t.join(20)
        self.assertTrue(waited, "the revoke did not wait for the open_session")
        self.assertEqual(out["r"]["code"], "revoked")
        self.assertTrue(self.count("select revoked from dc.sessions where token_hash = %s", self.h(tok)))
        self.unrevoke("bob")

    def test_grant_waits_for_a_submit(self):
        """Regression guard: a grant (tenant lock exclusive) waits for an in-flight submit and is then applied."""
        al, adm = self.tok("alice"), self.tok("admin")
        self.app_t.execute("begin")
        self.submit(self.app_t, al, self.body(None, "g", req="r2"))
        t, out = self.start(lambda: self.submit(self.app, adm, {"type": "grant", "client_id": "g" + secrets.token_hex(3),
                                                                 "idem": "g" + secrets.token_hex(3), "req": "r2", "target": "bob", "role": "decide"}))
        waited = self.blocked_by(self.pid(self.app), self.pid(self.app_t), timeout=1.0)
        self.app_t.execute("commit")
        t.join(20)
        self.assertTrue(waited)
        self.assertEqual(out["r"]["code"], "granted")

    def test_crossed_subjects(self):
        """Regression guard: two principals submit values about the same two new subjects concurrently, ten rounds; no deadlock."""
        ap = os.environ["POL_PG_APP_PASSFILE"]
        c1, c2 = pgcore.connect_app(self.port, ap, "s1-sub1"), pgcore.connect_app(self.port, ap, "s1-sub2")
        self.addCleanup(c1.close)
        self.addCleanup(c2.close)
        ta, tb = self.tok("alice"), self.tok("bob")
        for i in range(10):
            s1, s2 = "p" + secrets.token_hex(3), "q" + secrets.token_hex(3)
            out, go = {}, threading.Barrier(2)

            def run(k, c, tok, subs, req):
                go.wait()
                try:
                    out[k] = self.submit(c, tok, self.body(None, "x", subjects=subs, req=req))["code"]
                except psycopg.Error as e:
                    out[k] = "error:" + e.sqlstate
            ts = [threading.Thread(target=run, args=("a", c1, ta, [s1, s2], "r2")), threading.Thread(target=run, args=("b", c2, tb, [s2, s1], "r1"))]
            for t in ts:
                t.start()
            for t in ts:
                t.join(20)
            self.assertEqual(out, {"a": "proposed", "b": "proposed"}, "round %d" % i)


class S1_OpenSession(_S1):
    """dc.open_session: the factor and principal rows are pinned (L2) before the nonce lock (L8), so a revocation of either waits
    for the call. The prune skips rows another presentation holds. A writer holding the factor row makes the pin wait, and the pin
    then judges the newest version."""

    def _open(self, p, c=None):
        try:
            return (c or self.idp).execute("select dc.open_session(%s, 60)", (p,)).fetchone()[0]
        except psycopg.Error as e:
            return {"error": e.sqlstate, "message": e.diag.message_primary}

    def _blocked_by_any(self, waiter, holders, timeout=1.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.o.execute("select pg_blocking_pids(%s) && %s::int[]", (waiter, list(holders))).fetchone()[0]:
                return True
            time.sleep(0.02)
        return False

    def _twin(self, how, a_end):
        """A presents p in an open transaction; B presents p in a transaction of its own and waits for A (L8). The mutation starts
        while B waits and must be blocked (by A or B: both pinned the row FOR SHARE, and the writer waits for the lockers one at a
        time). A ends (`a_end`); B's call returns (opened when A rolled back, refused as a replay when A committed) with B's
        transaction still open, and the mutation must now be blocked by B; it lands only when B ends."""
        f = enroll(self.o, T, "bob", "owner_ui")
        p = proof(f)
        b = self.mut_idp
        ap, bp, mp = self.pid(self.idp_t), self.pid(b), self.pid(self.mut_o)
        self.idp_t.execute("begin")
        self.assertIn("token", self.idp_t.execute("select dc.open_session(%s, 60)", (p,)).fetchone()[0])
        b.execute("begin")
        bt, bo = self.start(lambda: self._open(p, b))
        self.assertTrue(self.blocked_by(bp, ap), "vacuous: B did not wait for A")
        mut = {"factor": ("update dc.factors set revoked = true where factor = %s", (f.factor_id,)),
               "principal": ("update dc.principal set revoked = true where tenant = %s and pid = 'bob'", (T,))}.get(how)
        mt, waited, a_ended = None, None, False
        try:
            if mut:
                mt, _mo = self.start(lambda: self.mut_o.execute(*mut))
                self.assertTrue(self._blocked_by_any(mp, (ap, bp)), "vacuous: the %s revocation did not wait while B waited" % how)
            self.idp_t.execute(a_end)
            a_ended = True
            bt.join(20)
            self.assertFalse(bt.is_alive(), "B stuck")
            if mt is not None:
                waited = self.blocked_by(mp, bp, timeout=1.0) and mt.is_alive()
        finally:
            # A first: B waits for A, and B's connection is busy until B's call returns (a red run fails above with A still open)
            if not a_ended:
                self.idp_t.execute("rollback")
            bt.join(20)
            if not bt.is_alive():
                b.execute("commit")   # a refused call left B's transaction aborted: COMMIT then rolls it back
        if mt is not None:
            mt.join(20)
            self.assertFalse(mt.is_alive(), "the mutation is stuck")
        return bo["r"], waited, f

    def test_twin_rolls_back(self):
        """B is opened once A rolls back. A factor or principal revocation started while B waited waits for B (red at ea4677a: it
        landed during the wait and B was refused). B's session is refused once the revocation lands."""
        for how in (None, "factor", "principal"):
            with self.subTest(mutation=how):
                r, waited, f = self._twin(how, "rollback")
                self.assertIn("token", r, "%r" % (r,))
                if how:
                    self.assertTrue(waited, "the %s revocation did not wait for B" % how)
                    self.assertIn(self.submit(self.app, r["token"], self.body())["code"], (UNAUTH, "principal_revoked"))
                    self.unrevoke("bob")

    def test_twin_commits(self):
        """Regression guard: A commits; B is refused as a replay and leaves no session."""
        r, _w, f = self._twin(None, "commit")
        self.assertEqual(r.get("message"), "proof refused: replayed")
        self.assertEqual(self.count("select count(*) from dc.sessions where factor = %s", f.factor_id), 1)

    def test_prune_never_waits(self):
        """A pruned an expired ledger row of the factor and holds its transaction open. B (same factor, another nonce) skips that
        row and opens without waiting. Red at ea4677a: B waited for A's prune."""
        f = enroll(self.o, T, "bob", "owner_ui")
        self.o.execute("insert into dc.used_proofs values (%s, %s, now() - interval '2 minutes')", (f.factor_id, secrets.token_hex(16)))
        self.idp_t.execute("begin")
        self.assertIn("token", self.idp_t.execute("select dc.open_session(%s, 60)", (proof(f),)).fetchone()[0])
        bt, bo = self.start(lambda: self._open(proof(f)))
        waited = self.blocked_by(self.pid(self.idp), self.pid(self.idp_t), timeout=1.0)
        bt.join(5)
        self.idp_t.execute("commit")
        bt.join(20)
        self.assertFalse(waited, "B waited for A's prune")
        self.assertIn("token", bo["r"])

    def test_factor_row_writer(self):
        """Regression guard: a transaction holds the factor row FOR UPDATE, revokes the factor and commits; B's pin waited for it
        and judges the newest version."""
        f = enroll(self.o, T, "bob", "owner_ui")
        self.hold.execute("begin")
        self.hold.execute("select 1 from dc.factors where factor = %s for update", (f.factor_id,))
        bt, bo = self.start(lambda: self._open(proof(f)))
        waited = self.blocked_by(self.pid(self.idp), self.pid(self.hold))
        self.hold.execute("update dc.factors set revoked = true where factor = %s", (f.factor_id,))
        self.hold.execute("commit")
        bt.join(20)
        self.assertTrue(waited)
        self.assertEqual(bo["r"].get("message"), "proof refused: factor revoked")


if __name__ == "__main__":
    unittest.main()
