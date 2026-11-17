"""Final policy review (reviews/policy-final/review-1.md, run 1356898b, at a2d65fd), Postgres findings, reproduced first. Same environment as
tests/test_pg.py (paths to 0600 files only; skipped without it). Every test was run against unmodified a2d65fd before the fix (red logs in
policy/results/red/); a docstring that says "regression guard" passed there too.

The invariant under test (DESIGN.md, "Final judgment after the last wait"): every session-keyed call judges its authorization AFTER the
last statement that can wait and BEFORE anything it does can be committed. Each test makes ONE blocking point wait: a superuser
connection (`hold`) takes exactly the lock that statement needs, in an open transaction, and the test checks that the call really waits
on it (pg_blocking_pids, so no test passes because nothing waited). While the call waits, the authorization changes: the token expires
on the wall clock, dc_idp ends it, the owner revokes its factor, or the owner revokes its principal. The call must then refuse and
write nothing. Each blocking point also runs once with nothing changed (positive control: the same wait, applied).

P1  submit judged the session before the subject-token insert (unique-index wait on a concurrent first use of a subject), the event
    insert (the chain head row, held by any concurrent event of the tenant), the revoke's updates (target's principal row, props, head
    rows, sessions) and every table lock; explain, read_table and erase_candidates judged before their reads; erase_values before its
    deletes. Fixed: staged writes, then one final judgment (dc._gate), refusal rolls the stage back; a revoke's own authority writes
    come after the judgment on rows and tables locked before it (and never wait: sessions held by an open end_session are skipped).
P4  open_session judged the factor and principal before the replay-ledger insert, which waits for a concurrent presentation of the same
    proof; if that one rolled back after the factor was revoked, the proof opened a session. Fixed: the whole proof judgment
    (dc._judge_proof) runs again after the ledger insert, the prune and the session insert; and a session is live only while its factor
    is unrevoked (use-time factor revocation).
P5  end_session had no isolation guard: under REPEATABLE READ it reports "not found" (false) for a session opened after its snapshot,
    which stays live. Fixed: refused unless READ COMMITTED.
P3  (Postgres) a failure mid-call (statement timeout, deadlock victim, terminated backend) leaves nothing, and a lost response after
    commit stays retryable as duplicate with its reference: dc.submit is one statement. Expected to PASS at a2d65fd (challenge)."""
import os
import random
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

from pol import _core
from tests.kit import enroll, pg_token, proof
from tests.test_delta1_pg import CALLS, LOCK_KEY, _Base
from tests.test_pg import CFG, LAB, T

UNAUTH = "unauthenticated:invalid_session"
MUTATIONS = ("live", "expire", "end", "factor", "principal")


class _Wait(_Base):
    def setUp(self):
        super().setUp()
        self.sup2 = pgcore._connect("postgres", os.environ["POL_PG_SUPER_PASSFILE"], self.port, "final-mutator")
        self.addCleanup(self.sup2.close)

    @staticmethod
    def pid(c):
        return c.execute("select pg_backend_pid()").fetchone()[0]

    def h(self, tok):
        return __import__("hashlib").sha256(tok.encode()).hexdigest()

    def mutate(self, how, tok, pid):
        """Change the authorization of `tok` (principal `pid`) while its call waits (a callable: run it instead)."""
        if callable(how):
            return how()
        if how == "live":
            return
        if how == "expire":
            exp = self.sup2.execute("select extract(epoch from expires_at) from dc.sessions where token_hash = %s", (self.h(tok),)).fetchone()[0]
            time.sleep(max(0.0, float(exp) - time.time() + 0.4))
        elif how == "end":
            self.assertIs(self.idp.execute("select dc.end_session(%s)", (tok,)).fetchone()[0], True)
        elif how == "factor":
            self.sup2.execute("update dc.factors set revoked = true where factor = (select factor from dc.sessions where token_hash = %s)",
                              (self.h(tok),))
        elif how == "principal":
            self.sup2.execute("update dc.principal set revoked = true where tenant = %s and pid = %s", (T, pid))
        else:
            raise ValueError(how)

    def unrevoke(self, pid):
        self.sup2.execute("update dc.principal set revoked = false where tenant = %s and pid = %s", (T, pid))

    def blocked(self, call_conn, fn, hold, how, tok, pid, hold_conn=None, release="commit"):
        """Run fn() on call_conn while hold_conn (default: the superuser `hold`) holds what hold(conn) took in an open transaction.
        Returns (result, waited): waited is True when the call was seen blocked by hold_conn (pg_blocking_pids)."""
        hc = hold_conn or self.hold
        hp, cp = self.pid(hc), self.pid(call_conn)
        hc.execute("begin")
        hold(hc)
        out = {}

        def run():
            try:
                out["r"] = fn()
            except psycopg.Error as e:
                out["r"] = {"error": e.sqlstate, "message": e.diag.message_primary}
        t = threading.Thread(target=run)
        t.start()
        waited = False
        try:
            for _ in range(100):
                if self.o.execute("select %s = any(pg_blocking_pids(%s))", (hp, cp)).fetchone()[0]:
                    waited = True
                    break
                time.sleep(0.05)
            self.mutate(how, tok, pid)
        finally:
            hc.execute(release)
        t.join(30)
        self.assertFalse(t.is_alive(), "the call is stuck")
        return out.get("r"), waited

    def count(self, q, *a):
        return self.o.execute(q, a).fetchone()[0]


class P1_SubmitBlockingPoints(_Wait):
    """dc.submit: one test per blocking point; each runs the five MUTATIONS."""

    def _point(self, hold, req="r1", subject=False, hold_conn=None):
        """hold(conn, subject): take the lock in conn's open transaction; subject: a fresh subject name per run (or None)."""
        for how in MUTATIONS:
            with self.subTest(mutation=how):
                pid = "alice"
                subj = "".join(random.choice("abcdefghjkmnpqrstuvwxyz") for _ in range(8)) if subject else None
                tok = self.tok(pid, ttl=2 if how == "expire" else 60)
                cid = "c" + secrets.token_hex(4)
                body = self.body(cid, "text " + cid, subjects=[subj] if subj else (), req=req)
                r, waited = self.blocked(self.app, lambda: self.submit(self.app, tok, body), lambda c: hold(c, subj), how, tok, pid,
                                         hold_conn=hold_conn)
                self.assertTrue(waited, "vacuous: the call did not wait on the held lock")
                want = {"live": "proposed", "principal": "principal_revoked"}.get(how, UNAUTH)
                self.assertEqual(r.get("code") if isinstance(r, dict) else r, want, "after a wait with mutation %s" % how)
                n = self.count("select count(*) from dc.events where tenant = %s and evid = %s", T, pid + "/" + cid)
                v = self.count("select count(*) from dc.vals where tenant = %s and text = %s", T, "text " + cid)
                self.assertEqual((n, v), (1, 1) if how == "live" else (0, 0), "event and value rows after mutation %s" % how)
                if how == "principal":
                    self.unrevoke(pid)

    def test_tenant_row(self):
        """Regression guard for expire, end and principal (a2d65fd judged again after _lock_event); red for factor (use-time factor
        revocation is new): the tenant row held FOR UPDATE (a revoke, grant or erasure)."""
        self._point(lambda c, _s: c.execute("select 1 from dc.tenant where tenant = %s for update", (T,)))

    def test_principal_advisory_lock(self):
        """As test_tenant_row: the (tenant, principal) advisory lock, which any login role can take."""
        self._point(lambda c, _s: c.execute(LOCK_KEY, (T, "alice")))

    def test_head_row(self):
        """As test_tenant_row: the requirement's head row FOR UPDATE (a concurrent submission on the same requirement)."""
        self._point(lambda c, _s: c.execute("select 1 from dc.head where tenant = %s and req = 'r1' for update", (T,)))

    def test_subject_token_unique_index(self):
        """a2d65fd (P1, _pgcore.sql:712): the subject-token insert waits for a concurrent uncommitted first use of the same subject,
        after the last judgment; the event was applied for an expired, ended or factor-revoked session (a revoked principal was still
        refused there, by the core's own revoked check, which reads after this wait)."""
        self._point(lambda c, s: c.execute("insert into dc.subject_tokens values (%s, %s, %s)", (T, s, "subj-held-" + s)), subject=True)

    def test_subject_token_held_by_another_principals_submit(self):
        """a2d65fd: the same wait, made the realistic way: bob's dc.submit about the same new subject (on r1) is still open while alice
        submits about it on r2 (no head row or advisory lock in common). Red for expire, end and factor, as above."""
        bob = self.tok("bob")
        self._point(lambda c, s: self.submit(c, bob, self.body("b" + secrets.token_hex(3), "bob on " + s, subjects=[s])), req="r2",
                    subject=True, hold_conn=self.app_t)

    def test_chain_head_row(self):
        """a2d65fd (P1, _pgcore.sql:731): the event insert's trigger takes the tenant's chain head row FOR UPDATE, held by ANY concurrent
        event of the tenant until it commits; the wait came after the last judgment (and after the core's checks): red for all four."""
        self._point(lambda c, _s: c.execute("select 1 from dc.chainhead where tenant = %s for update", (T,)), req="r2")

    def test_events_table_lock(self):
        """a2d65fd: a table lock on dc.events (the value-reference lookup and the core's reads) is waited for after the last judgment:
        red for expire, end and factor (the core's revoked check reads after this wait)."""
        self._point(lambda c, _s: c.execute("lock table dc.events in access exclusive mode"), req="r2")


class P1_RevokePath(_Wait):
    """A revoke by admin: its own blocking points, and the writes that change the authority the final judgment reads."""

    def _revoke(self, tok, target, cid=None):
        return self.submit(self.app, tok, {"type": "revoke", "client_id": cid or "rv" + secrets.token_hex(3), "idem": "rv" + secrets.token_hex(3),
                                           "target": target})

    def _expire_during(self, hold, target="bob", how="expire"):
        tok = self.tok("admin", ttl=2 if how == "expire" else 60)
        r, waited = self.blocked(self.app, lambda: self._revoke(tok, target), hold, how, tok, "admin")
        self.assertTrue(waited, "vacuous: the revoke did not wait")
        self.assertEqual(r.get("code"), UNAUTH if how != "principal" else "principal_revoked", "the revoke was applied after the wait")
        self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = %s", T, target), "target revoked")
        self.assertEqual(self.count("select count(*) from dc.events where tenant = %s and ev->>'type' = 'revoke'", T), 0)

    def test_target_principal_row(self):
        """a2d65fd: the revoke's update of the target's principal row waited (row held by the owner) after the last judgment."""
        for how in ("expire", "end", "factor"):
            with self.subTest(how):
                self._expire_during(lambda c: c.execute("select 1 from dc.principal where tenant = %s and pid = 'bob' for update", (T,)), how=how)

    def test_target_props_rows(self):
        """a2d65fd: invalidating the target's open proposals waited on a held props row after the last judgment."""
        xt = self.tok("x")
        self.assertEqual(self.submit(self.app, xt, self.body("xp", "x proposes"))["code"], "proposed")
        self._expire_during(lambda c: c.execute("select 1 from dc.props where tenant = %s and author = 'x' for update", (T,)), target="x")
        self.assertEqual(self.count("select count(*) from dc.props where tenant = %s and author = 'x' and status = 'open'", T), 1)

    def test_sessions_table_lock(self):
        """a2d65fd: the revoke's update of dc.sessions waited for a table lock (SHARE) after the last judgment. Fixed: the table lock is
        taken before the final judgment, which then refuses the expired session."""
        self._expire_during(lambda c: c.execute("lock table dc.sessions in share mode"))

    def test_revoke_never_waits_for_an_open_end_session(self):
        """a2d65fd: the revoke's sessions update waited for the target's session row held by an uncommitted dc.end_session (an
        identity adapter mid-logout), after the last judgment, so the admin's own session could expire meanwhile. Fixed: the authority
        writes come after the judgment and skip rows another transaction holds (that end_session is revoking the row anyway); the
        principal revoke alone already refuses the target's tokens."""
        for outcome in ("commit", "rollback"):
            with self.subTest(end_session=outcome):
                self.unrevoke("bob")
                bob1, bob2 = self.tok("bob"), self.tok("bob")
                adm = self.tok("admin")
                self.idp_t.execute("begin")
                self.assertIs(self.idp_t.execute("select dc.end_session(%s)", (bob1,)).fetchone()[0], True)
                t0 = time.time()
                out = {}
                t = threading.Thread(target=lambda: out.__setitem__("r", self._revoke(adm, "bob")))
                t.start()
                t.join(1.5)
                waited = t.is_alive()
                self.idp_t.execute(outcome)
                t.join(20)
                self.assertFalse(waited, "the revoke waited %.1fs for the held session row" % (time.time() - t0))
                self.assertEqual(out["r"]["code"], "revoked")
                want = {bob1: UNAUTH if outcome == "commit" else "principal_revoked", bob2: UNAUTH}     # bob1's row was skipped, not revoked
                for tok in (bob1, bob2):
                    self.assertEqual(self.submit(self.app, tok, self.body())["code"], want[tok])
                    self.assertIsNone(self.app.execute("select dc.explain(%s, 'r1')", (tok,)).fetchone()[0])

    def test_self_revoke_is_applied(self):
        """Regression guard: the call's own authority writes never refuse it: an admin revoking itself is applied, and its token is
        refused afterwards."""
        adm = self.tok("admin")
        self.assertEqual(self._revoke(adm, "admin")["code"], "revoked")
        self.assertEqual(self._revoke(adm, "bob")["code"], UNAUTH)


class P1_ReadAndEraseCalls(_Wait):
    """explain, read_table, erase_candidates (reads that can wait for a table lock) and erase_values (deletes that can wait for rows)."""

    def setUp(self):
        super().setUp()
        out = self.submit(self.app, self.tok("alice"), self.body("seed", "about dana", subjects=["dana"]))
        self.assertEqual(out["code"], "proposed")
        self.ref = out["value_ref"]

    def _read(self, name, hold, pid="admin"):
        q = {"explain": "select dc.explain(%s, 'r1')", "read_table": "select dc.read_table(%s)",
             "erase_candidates": "select dc.erase_candidates(%s, 'dana')"}[name]
        for how in MUTATIONS:
            with self.subTest(call=name, mutation=how):
                tok = self.tok(pid, ttl=2 if how == "expire" else 60)
                r, waited = self.blocked(self.app, lambda: self.app.execute(q, (tok,)).fetchone()[0], hold, how, tok, pid)
                self.assertTrue(waited, "vacuous: %s did not wait" % name)
                got = {"explain": lambda: r is not None and "seed" in str(r), "read_table": lambda: "seed" in str(r),
                       "erase_candidates": lambda: r["refused"] is None and len(r["values"]) == 1}[name]()
                self.assertEqual(got, how == "live", "%s answered after the wait with mutation %s" % (name, how))
                if how == "principal":
                    self.unrevoke(pid)

    def test_explain_after_a_table_wait(self):
        """a2d65fd: explain judged the session, then waited for dc.head, then returned the head to an ended or expired session."""
        self._read("explain", lambda c: c.execute("lock table dc.head in access exclusive mode"))

    def test_read_table_after_a_table_wait(self):
        """a2d65fd: the same for read_table (red for expire, end and factor; its query reads the principal after the wait)."""
        self._read("read_table", lambda c: c.execute("lock table dc.head in access exclusive mode"))

    def test_erase_candidates_after_a_table_wait(self):
        """a2d65fd: erase_candidates listed the values about a subject (and their labels) after its admin session ended."""
        self._read("erase_candidates", lambda c: c.execute("lock table dc.vals in access exclusive mode"))

    def _erase(self, hold):
        for how in MUTATIONS:
            with self.subTest(mutation=how):
                if how != "live":
                    self.sup2.execute("delete from dc.vals where tenant = %s and text = 'about dana'", (T,))
                    self.ref = self.submit(self.app, self.tok("alice"), self.body(None, "about dana", subjects=["dana"]))["value_ref"]
                tok = self.tok("admin", ttl=2 if how == "expire" else 60)
                r, waited = self.blocked(self.app, lambda: self.app.execute("select dc.erase_values(%s, 'dana', %s)", (tok, [self.ref])).fetchone()[0],
                                         hold(self.ref), how, tok, "admin")
                self.assertTrue(waited, "vacuous: erase_values did not wait")
                left = self.count("select count(*) from dc.vals where tenant = %s and ref = %s", T, self.ref)
                if how == "live":
                    self.assertEqual((r["refused"], r["erased"], left), (None, 1, 0))
                else:
                    self.assertEqual((r, left), ({"refused": "unauthorized"}, 1), "erased after the wait with mutation %s" % how)
                if how == "principal":
                    self.unrevoke("admin")

    def test_erase_values_after_a_value_row_wait(self):
        """a2d65fd: erase_values judged again after the tenant row, then its delete waited for the value's row: erased for an ended
        session."""
        self._erase(lambda ref: (lambda c: c.execute("select 1 from dc.vals where tenant = %s and ref = %s for update", (T, ref))))

    def test_erase_values_after_a_subject_token_wait(self):
        """a2d65fd: the delete of the subject's token mapping (last value gone) waited for the mapping's row."""
        self._erase(lambda ref: (lambda c: c.execute("select 1 from dc.subject_tokens where tenant = %s and subject = 'dana' for update", (T,))))

    def test_erase_values_after_the_tenant_row(self):
        """Regression guard for expire, end and principal (a2d65fd judged again after the tenant row); red for factor."""
        self._erase(lambda ref: (lambda c: c.execute("select 1 from dc.tenant where tenant = %s for update", (T,))))


class P1_JudgmentNeverWaits(_Wait):
    """The judgment must read after every wait, including a wait inside the judgment's own reads. A STABLE function reads with its
    caller's snapshot, and a plpgsql simple expression takes its snapshot before the functions it calls run, so a read there that
    waits for a table lock sees the rows as they were BEFORE the wait (red at a2d65fd: dc.explain's STABLE dc._explain_raw read the
    principal that way). A plain statement of a VOLATILE function reads after its own lock wait on PG 16 (its plan's locks are taken
    before its snapshot; the regression guards here show it). Here the holder takes ACCESS EXCLUSIVE on the table the judgment reads,
    changes the caller's row (session ended, factor revoked, principal revoked) and commits while the call waits. Fixed: the judgment
    is the VOLATILE dc._gate (dc._judge_proof for open_session), which first locks its tables in a statement of its own, so its reads
    never wait and each takes its snapshot after every wait, whatever the server's order of snapshot and plan locks."""
    CHANGE = {"sessions": "update dc.sessions set revoked = true where token_hash = %(h)s",
              "factors": "update dc.factors set revoked = true where factor = (select factor from dc.sessions where token_hash = %(h)s)",
              "principal": "update dc.principal set revoked = true where tenant = %(t)s and pid = %(p)s"}

    def _stale(self, table, fn, tok, pid, conn=None):
        def hold(c):
            c.execute("lock table dc.%s in access exclusive mode" % table)
            c.execute(self.CHANGE[table], {"h": self.h(tok) if tok else None, "t": T, "p": pid})
        return self.blocked(conn or self.app, fn, hold, "live", tok, pid)

    def test_submit(self):
        """Regression guard for sessions and principal (a2d65fd judged again after _lock_event, in plain statements). The factors
        subtest fails at a2d65fd as "did not wait": no call read dc.factors there (no use-time factor check; the behaviour is shown by
        P4_UseTimeFactorRevocation)."""
        for table, want in (("sessions", UNAUTH), ("factors", UNAUTH), ("principal", "principal_revoked")):
            with self.subTest(table=table):
                tok, cid = self.tok("alice"), "c" + secrets.token_hex(4)
                r, waited = self._stale(table, lambda: self.submit(self.app, tok, self.body(cid, "text " + cid)), tok, "alice")
                self.assertTrue(waited, "vacuous: submit did not wait for dc.%s" % table)
                self.assertEqual(r.get("code"), want, "submit after a wait for dc.%s" % table)
                self.assertEqual(self.count("select count(*) from dc.events where tenant = %s and evid = %s", T, "alice/" + cid), 0)
                self.unrevoke("alice")

    def test_explain(self):
        """a2d65fd: explain's principal check WAS a read that waited, inside the STABLE dc._explain_raw called from a simple
        expression: it answered from the snapshot taken before the wait (red for principal; factors as in test_submit; sessions is a
        regression guard: dc._session is VOLATILE)."""
        self.assertEqual(self.submit(self.app, self.tok("alice"), self.body("seed", "about dana"))["code"], "proposed")
        for table in ("sessions", "factors", "principal"):
            with self.subTest(table=table):
                tok = self.tok("admin")
                r, waited = self._stale(table, lambda: self.app.execute("select dc.explain(%s, 'r1')", (tok,)).fetchone()[0], tok, "admin")
                self.assertTrue(waited, "vacuous: explain did not wait for dc.%s" % table)
                self.assertIsNone(r, "explain answered after dc.%s changed while it waited" % table)
                self.unrevoke("admin")

    def test_open_session(self):
        """Regression guard (passed at a2d65fd: the VOLATILE open_session's factor and principal statements read after their own
        table-lock waits on PG 16). Kept because the fix moves these reads into dc._judge_proof, which must stay VOLATILE."""
        for table, msg in (("factors", "proof refused: factor revoked"), ("principal", "proof refused: principal")):
            with self.subTest(table=table):
                f = enroll(self.o, T, "bob", "owner_ui")
                p = proof(f)
                change = {"factors": ("update dc.factors set revoked = true where factor = %s", (f.factor_id,)),
                          "principal": ("update dc.principal set revoked = true where tenant = %s and pid = 'bob'", (T,))}[table]

                def hold(c, table=table, change=change):
                    c.execute("lock table dc.%s in access exclusive mode" % table)
                    c.execute(*change)
                r, waited = self.blocked(self.idp, lambda: self.idp.execute("select dc.open_session(%s, 60)", (p,)).fetchone()[0], hold,
                                         "live", None, "bob")
                self.assertTrue(waited, "vacuous: open_session did not wait for dc.%s" % table)
                self.assertEqual(r.get("message") if "error" in r else "opened", msg)
                self.assertEqual(self.count("select count(*) from dc.sessions where factor = %s", f.factor_id), 0, "a session row was left")
                self.unrevoke("bob")


class P1_PgServiceJudgment(_Wait):
    """PgDecisionService (the request handler over dc_app) judges the IN-PROCESS authority (the in-process session, the directory, the
    policy document's revocations), which the database's own judgment cannot see, before its database call, and that call can wait.
    a2d65fd: a principal revoked by the policy document or removed from the directory while dc.submit or dc.erase_values waited was
    applied. Fixed: the writing call runs in its own transaction and the in-process judgment runs again after it, before COMMIT."""

    @staticmethod
    def _mutations():
        from pol.policy import parse_policy
        from tests.kit import policy_doc
        return (("live", None), ("policy_revoked", lambda w, pid: setattr(w, "pol", parse_policy(policy_doc(revoked=[pid])))),
                ("removed", lambda w, pid: w.dir.remove(pid)))

    def _lab(self):
        from pol.policy import parse_policy
        from tests.kit import P, PgLab, policy_doc
        lab = PgLab(self.o, self.idp, principals=[P("alice"), P("bob"), P("admin")], policy=parse_policy(policy_doc()), core_config=dict(CFG))
        return lab, pgcore.PgDecisionService(lab.w.enf, self.app)

    def test_submit(self):
        for name, mutate in self._mutations():
            with self.subTest(mutation=name):
                lab, svc = self._lab()
                s, cid = lab.s("alice"), "c" + secrets.token_hex(4)
                body = self.body(cid, "text " + cid, req="r2")
                r, waited = self.blocked(self.app, lambda: svc.submit(s, body),
                                         lambda c: c.execute("select 1 from dc.chainhead where tenant = %s for update", (T,)),
                                         lambda: mutate and mutate(lab.w, "alice"), None, "alice")
                self.assertTrue(waited, "vacuous: the service's dc.submit did not wait")
                want = {"live": "proposed", "policy_revoked": "principal_revoked", "removed": "unauthenticated:invalid_session"}[name]
                self.assertEqual(r.get("code"), want, "service submit after a wait with mutation %s" % name)
                self.assertEqual(self.count("select count(*) from dc.events where tenant = %s and evid = %s", T, "alice/" + cid),
                                 1 if name == "live" else 0)

    def test_erase_subject(self):
        for name, mutate in self._mutations():
            with self.subTest(mutation=name):
                lab, svc = self._lab()
                ref = self.submit(self.app, self.tok("alice"), self.body(None, "about dana " + name, subjects=["dana"]))["value_ref"]
                adm = lab.s("admin")
                r, waited = self.blocked(self.app, lambda: svc.erase_subject(adm, "dana"),
                                         lambda c: c.execute("select 1 from dc.vals where tenant = %s and ref = %s for update", (T, ref)),
                                         lambda: mutate and mutate(lab.w, "admin"), None, "admin")
                self.assertTrue(waited, "vacuous: the service's dc.erase_values did not wait")
                left = self.count("select count(*) from dc.vals where tenant = %s and ref = %s", T, ref)
                if name == "live":
                    self.assertEqual((r["refused"], left), (None, 0))
                else:
                    self.assertEqual((r["refused"], left), ("unauthorized", 1), "service erased after a wait with mutation %s" % name)

    def test_erase_decision_changes_during_wait(self):
        """Each value's erase decision (a recorded legal hold queues it; a tenant without the erase grant is denied) is the in-process
        policy point's. a2d65fd and 68c6dcd made it only before dc.erase_values, which waits on a held value row, so a hold recorded or
        the grant withdrawn during that wait was not seen and the value was erased. Fixed: decided again after the call, before COMMIT
        (the memory engine decides each value inside its deletion's vault lock). Two values about one subject: `held` carries hold-9
        (not recorded when erasure starts), `plain` none; the call waits on held's row."""
        from pol.policy import parse_policy
        from tests.kit import policy_doc
        doc = policy_doc()
        no_erase = dict(doc["grants"], **{T: [g for g in doc["grants"][T] if g != "erase"]})
        cases = (("live", None, dict(erased=2, queued=0, denied=0, result="erased"), (0, 0)),
                 ("hold_recorded", lambda w: setattr(w, "pol", parse_policy(policy_doc(hold_events=["hold-9"]))),
                  dict(erased=1, queued=1, denied=0, result="incomplete"), (1, 0)),
                 ("erase_grant_withdrawn", lambda w: setattr(w, "pol", parse_policy(policy_doc(grants=no_erase))),
                  dict(erased=0, queued=0, denied=2, result="incomplete"), (1, 1)))
        for name, mutate, want, left_want in cases:
            with self.subTest(mutation=name):
                lab, svc = self._lab()
                subj = "dana" + secrets.token_hex(3)
                hb = self.body(None, "held about " + subj, subjects=[subj])
                hb["label"] = dict(hb["label"], holds=["hold-9"])
                held = self.submit(self.app, self.tok("alice"), hb)["value_ref"]
                plain = self.submit(self.app, self.tok("alice"), self.body(None, "plain about " + subj, subjects=[subj]))["value_ref"]
                adm = lab.s("admin")
                r, waited = self.blocked(self.app, lambda: svc.erase_subject(adm, subj),
                                         lambda c: c.execute("select 1 from dc.vals where tenant = %s and ref = %s for update", (T, held)),
                                         lambda: mutate and mutate(lab.w), None, "admin")
                self.assertTrue(waited, "vacuous: the service's dc.erase_values did not wait")
                left = tuple(self.count("select count(*) from dc.vals where tenant = %s and ref = %s", T, x) for x in (held, plain))
                got = {k: r[k] for k in want}
                self.assertEqual((got, left), (want, left_want), "service erasure after a wait with mutation %s" % name)
                self.assertIsNone(r["refused"])


class S2_NothingWaitsAfterTheJudgment(_Wait):
    """FINAL-JUDGMENT.md S2: "nothing after the final judgment waits" enforced by the database instead of argued. dc.submit sets
    lock_timeout to 1 ms right after its judgment, so a lock wait there fails the call (lock_not_available, everything rolled back)
    instead of waiting while the caller's authority lapses. The broken variant replaces dc._lock_authority with a no-op, which moves a
    revoke's wait on its target's principal row after the judgment (the a2d65fd shape). 3153059: the call waits there and applies the
    revoke after the admin's session was ended or expired during that wait. Fixed: 55P03, nothing applied, no wait."""

    def _revoke(self, tok, target):
        return self.submit(self.app, tok, {"type": "revoke", "client_id": "rv" + secrets.token_hex(3), "idem": "rv" + secrets.token_hex(3),
                                           "target": target})

    def test_a_wait_after_the_judgment_fails_the_call(self):
        orig = self.o.execute("select pg_get_functiondef('dc._lock_authority(text, text)'::regprocedure)").fetchone()[0]
        self.o.execute("create or replace function dc._lock_authority(p_tenant text, tgt text) returns void language plpgsql as $$ begin end $$")
        try:
            for how in ("end", "expire"):
                with self.subTest(how):
                    self.unrevoke("bob")
                    tok = self.tok("admin", ttl=2 if how == "expire" else 60)
                    r, waited = self.blocked(self.app, lambda: self._revoke(tok, "bob"),
                                             lambda c: c.execute("select 1 from dc.principal where tenant = %s and pid = 'bob' for update", (T,)),
                                             how, tok, "admin")
                    bob = self.count("select revoked from dc.principal where tenant = %s and pid = 'bob'", T)
                    self.assertEqual((r.get("error"), bob), ("55P03", False), "a wait after the judgment: %r (waited %s)" % (r, waited))
                    self.assertFalse(waited, "the call waited after its judgment")
        finally:
            self.o.execute(orig)
            self.unrevoke("bob")

    def test_the_callers_lock_timeout_is_restored(self):
        """Regression guard for S2: the 1 ms applies only after the judgment inside the call, never to the caller's transaction."""
        tok = self.tok("admin")
        self.app.execute("begin")
        try:
            self.app.execute("set local lock_timeout = '7s'")
            self.assertEqual(self._revoke(tok, "bob")["code"], "revoked")
            self.assertEqual(self.app.execute("show lock_timeout").fetchone()[0], "7s")
        finally:
            self.app.execute("rollback")
            self.unrevoke("bob")


class P4_OpenSessionBlockingPoints(_Wait):
    """dc.open_session: the replay-ledger insert waits for a concurrent presentation of the same proof (an uncommitted ledger row);
    the prune waits for a concurrent prune of the same rows; the ledger's foreign key waits for a factor row held FOR UPDATE."""

    def _open(self, p, c=None):
        try:
            return (c or self.idp).execute("select dc.open_session(%s, 60)", (p,)).fetchone()[0]
        except psycopg.Error as e:
            return {"error": e.sqlstate, "message": e.diag.message_primary}

    def _twin(self, change, want, expires_in=60.0):
        """A presents proof p in an open transaction (its ledger row uncommitted); B presents p and waits for A; `change` runs; A rolls
        back; B's insert then succeeds and B must be judged again."""
        f = enroll(self.o, T, "bob", "owner_ui")
        p = proof(f, expires_in=expires_in)
        self.idp_t.execute("begin")
        self.assertIn("token", self.idp_t.execute("select dc.open_session(%s, 60)", (p,)).fetchone()[0])
        hp, cp = self.pid(self.idp_t), self.pid(self.idp)
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("r", self._open(p)))
        t.start()
        waited = False
        for _ in range(100):
            if self.o.execute("select %s = any(pg_blocking_pids(%s))", (hp, cp)).fetchone()[0]:
                waited = True
                break
            time.sleep(0.05)
        change(f)
        self.idp_t.execute("rollback")
        t.join(20)
        self.assertTrue(waited, "vacuous: B did not wait for A's ledger row")
        r = out["r"]
        self.assertEqual(r.get("message") if "error" in r else "opened", want)
        if want != "opened":
            self.assertEqual(self.count("select count(*) from dc.sessions where factor = %s", f.factor_id), 0, "a session row was left")
        return r

    def test_positive_control(self):
        """Regression guard: with nothing changed, B opens the session after A rolls back."""
        self._twin(lambda f: None, "opened")

    def test_factor_revoked_while_waiting_on_the_ledger(self):
        """a2d65fd (P4, _pgcore.sql:601-607,622-629): the factor was judged before the ledger insert; revoked while B waited, A rolled
        back, and B opened a session from a revoked factor."""
        self._twin(lambda f: self.sup2.execute("update dc.factors set revoked = true where factor = %s", (f.factor_id,)), "proof refused: factor revoked")

    def test_principal_revoked_while_waiting_on_the_ledger(self):
        """a2d65fd: the same with the principal revoked."""
        self._twin(lambda f: self.sup2.execute("update dc.principal set revoked = true where tenant = %s and pid = 'bob'", (T,)), "proof refused: principal")

    def test_proof_expired_while_waiting_on_the_ledger(self):
        """Regression guard (a2d65fd re-judged the expiry after the insert)."""
        self._twin(lambda f: time.sleep(1.6), "proof refused: time", expires_in=1.2)

    def test_factor_revoked_while_waiting_on_the_prune(self):
        """a2d65fd: the prune of the factor's expired ledger rows waits for a concurrent prune of the same rows; the factor revoked
        meanwhile was not judged again."""
        f = enroll(self.o, T, "bob", "owner_ui")
        self.o.execute("insert into dc.used_proofs values (%s, %s, now() - interval '2 minutes')", (f.factor_id, secrets.token_hex(16)))
        self.idp_t.execute("begin")
        self.assertIn("token", self.idp_t.execute("select dc.open_session(%s, 60)", (proof(f),)).fetchone()[0])   # pruned, uncommitted
        hp, cp = self.pid(self.idp_t), self.pid(self.idp)
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("r", self._open(proof(f))))
        t.start()
        waited = any(self.o.execute("select %s = any(pg_blocking_pids(%s))", (hp, cp)).fetchone()[0] or time.sleep(0.05) for _ in range(100))
        self.sup2.execute("update dc.factors set revoked = true where factor = %s", (f.factor_id,))
        self.idp_t.execute("commit")
        t.join(20)
        self.assertTrue(waited, "vacuous: B did not wait on the prune")
        self.assertEqual(out["r"].get("message"), "proof refused: factor revoked")

    def test_factor_revoked_while_waiting_on_the_ledger_foreign_key(self):
        """a2d65fd: the ledger insert's foreign-key check waits for the factor row held FOR UPDATE; the holder revokes the factor and
        commits; B was not judged again."""
        f = enroll(self.o, T, "bob", "owner_ui")
        hp, cp = self.pid(self.hold), self.pid(self.idp)
        self.hold.execute("begin")
        self.hold.execute("select 1 from dc.factors where factor = %s for update", (f.factor_id,))
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("r", self._open(proof(f))))
        t.start()
        waited = any(self.o.execute("select %s = any(pg_blocking_pids(%s))", (hp, cp)).fetchone()[0] or time.sleep(0.05) for _ in range(100))
        self.hold.execute("update dc.factors set revoked = true where factor = %s", (f.factor_id,))
        self.hold.execute("commit")
        t.join(20)
        self.assertTrue(waited, "vacuous: B did not wait on the factor row")
        self.assertEqual(out["r"].get("message"), "proof refused: factor revoked")


class P4_UseTimeFactorRevocation(_Wait):
    def test_a_revoked_factor_ends_its_sessions_on_every_call(self):
        """a2d65fd: revoking a factor stopped new proofs only; a session it had opened (for instance one that won the race above just
        before the revoke committed) kept working on all five calls. Fixed: a session is live only while its factor is unrevoked."""
        self.assertEqual(self.submit(self.app, self.tok("alice"), self.body("seed", "about dana", subjects=["dana"]))["code"], "proposed")
        for name in CALLS:
            with self.subTest(name):
                tok = self.tok("admin")
                self.assertTrue(self._ok(name, tok), "positive control: %s works" % name)
                self.mutate("factor", tok, "admin")
                self.assertFalse(self._ok(name, tok), "%s accepted a session of a revoked factor" % name)

    def _ok(self, name, tok):
        if name == "submit":
            return not self.submit(self.app, tok, self.body())["code"].startswith("unauthenticated")
        r = self.app.execute({"explain": "select dc.explain(%s, 'r1')", "read_table": "select dc.read_table(%s)",
                              "erase_candidates": "select dc.erase_candidates(%s, 'dana')",
                              "erase_values": "select dc.erase_values(%s, 'dana', array['nothing'])"}[name], (tok,)).fetchone()[0]
        return {"explain": lambda: r is not None, "read_table": lambda: bool(r), "erase_candidates": lambda: r["refused"] is None,
                "erase_values": lambda: r["refused"] is None}[name]()


class P5_EndSessionIsolation(_Wait):
    def test_end_session_refuses_other_isolation_levels(self):
        """a2d65fd: end_session had no isolation guard. Under REPEATABLE READ its update reads the transaction's snapshot: a session
        opened after that snapshot is not found, end_session answers false, and the token stays live. Fixed: refused (raises) unless
        READ COMMITTED, so a logout never reports a wrong 'not found'."""
        for level in ("read uncommitted", "repeatable read", "serializable"):
            with self.subTest(level=level):
                c = self.idp_t
                c.execute("begin isolation level " + level)
                c.execute("select 1")
                tok = self.tok("alice")                                     # opened (committed) after c's snapshot
                try:
                    got = c.execute("select dc.end_session(%s)", (tok,)).fetchone()[0]
                except psycopg.Error as e:
                    got = "refused:" + (e.diag.message_primary or "")
                c.execute("rollback")
                live = not self.submit(self.app, tok, self.body())["code"].startswith("unauthenticated")
                self.assertEqual(got, "refused:end_session refused: isolation", "%s: end_session answered %r (token live: %s)" % (level, got, live))
                self.assertTrue(self.idp.execute("select dc.end_session(%s)", (tok,)).fetchone()[0], "positive control: READ COMMITTED ends it")


class P3_PgFailuresMidCall(_Wait):
    """Challenge tests for P3 on Postgres (expected to PASS at a2d65fd too): dc.submit is one statement, so a failure part-way leaves
    nothing, and a lost response after commit stays retryable."""

    def _state(self, cid, text):
        return (self.count("select count(*) from dc.events where tenant = %s and evid = %s", T, "alice/" + cid),
                self.count("select count(*) from dc.vals where tenant = %s and text = %s", T, text),
                self.count("select count(*) from dc.subject_tokens where tenant = %s and subject = 'fred'", T))

    def _retry_ok(self, tok, body):
        r1 = self.submit(self.app, tok, body)
        r2 = self.submit(self.app, tok, dict(body))
        self.assertEqual((r1["code"], r2["code"], r2["value_ref"] == r1["value_ref"]), ("proposed", "duplicate", True))

    def test_statement_timeout_mid_call(self):
        tok, cid = self.tok("alice"), "f" + secrets.token_hex(3)
        body = self.body(cid, "text " + cid, subjects=["fred"])
        c = pgcore.connect_app(self.port, os.environ["POL_PG_APP_PASSFILE"], "final-timeout")
        self.addCleanup(c.close)
        c.execute("set statement_timeout = '700ms'")
        r, waited = self.blocked(c, lambda: self.submit(c, tok, body), lambda h: h.execute("select 1 from dc.chainhead where tenant = %s for update", (T,)),
                                 lambda: time.sleep(1.2), tok, "alice")
        self.assertTrue(waited)
        self.assertEqual(r.get("error"), "57014", "the statement was not cancelled")
        self.assertEqual(self._state(cid, "text " + cid), (0, 0, 0), "a cancelled call left rows")
        self._retry_ok(tok, body)

    def test_client_connection_dropped_mid_call(self):
        """The client's socket is shut down while its call waits (a dropped connection; no server process is signalled). The server
        either finishes and commits the statement (the response is lost) or aborts it; either way nothing half-done is left and the
        exact retry converges on one event and one value."""
        import socket
        tok, cid = self.tok("alice"), "f" + secrets.token_hex(3)
        body = self.body(cid, "text " + cid, subjects=["fred"])
        c = pgcore.connect_app(self.port, os.environ["POL_PG_APP_PASSFILE"], "final-dropped")
        self.addCleanup(lambda: c.closed or c.close())

        def drop():
            socket.socket(fileno=os.dup(c.fileno())).shutdown(socket.SHUT_RDWR)
            time.sleep(0.3)
        r, waited = self.blocked(c, lambda: self.submit(c, tok, body), lambda h: h.execute("select 1 from dc.chainhead where tenant = %s for update", (T,)),
                                 drop, tok, "alice")
        self.assertTrue(waited)
        self.assertIn("error", r, "the call answered over a dropped connection")
        for _ in range(50):                          # the server-side statement may still be finishing
            if not self.o.execute("select count(*) from pg_stat_activity where application_name = 'final-dropped' and state = 'active'").fetchone()[0]:
                break
            time.sleep(0.1)
        st = self._state(cid, "text " + cid)
        self.assertIn(st, ((0, 0, 0), (1, 1, 1)), "a dropped call left part of its writes")
        r2 = self.submit(self.app, tok, dict(body))
        self.assertEqual(r2["code"], "duplicate" if st[0] else "proposed")
        self.assertEqual(self._state(cid, "text " + cid), (1, 1, 1))

    def test_deadlock_victim_mid_call(self):
        """The owner holds the chain head and then asks for alice's advisory lock, which alice's submit holds while it waits for the
        chain head: one of the two is the deadlock victim. Whichever it is, nothing half-done is left and the retry converges."""
        tok, cid = self.tok("alice"), "f" + secrets.token_hex(3)
        body = self.body(cid, "text " + cid, subjects=["fred"])
        self.hold.execute("begin")
        self.hold.execute("select 1 from dc.chainhead where tenant = %s for update", (T,))
        out = {}

        def run():
            try:
                out["r"] = self.submit(self.app, tok, body)
            except psycopg.Error as e:
                out["r"] = {"error": e.sqlstate}
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)
        try:
            self.hold.execute(LOCK_KEY, (T, "alice"))
            self.hold.execute("commit")
            owner = "ok"
        except psycopg.Error as e:
            owner = e.sqlstate
            self.hold.execute("rollback")
        t.join(20)
        self.assertIn("40P01", (owner, out["r"].get("error")), "no deadlock was detected (vacuous)")
        st = self._state(cid, "text " + cid)
        if out["r"].get("error"):
            self.assertEqual(st, (0, 0, 0), "the deadlock victim left rows")
            self._retry_ok(tok, body)
        else:
            self.assertEqual((out["r"]["code"], st[:2]), ("proposed", (1, 1)))
            r2 = self.submit(self.app, tok, dict(body))
            self.assertEqual((r2["code"], r2["value_ref"]), ("duplicate", out["r"]["value_ref"]))

    def test_lost_response_after_commit(self):
        tok, cid = self.tok("alice"), "f" + secrets.token_hex(3)
        body = self.body(cid, "text " + cid, subjects=["fred"])
        self.app_t.execute("begin")
        first = self.submit(self.app_t, tok, body)
        self.app_t.execute("commit")                 # the caller never saw `first`
        r = self.submit(self.app, tok, dict(body))
        self.assertEqual((first["code"], r["code"], r["value_ref"]), ("proposed", "duplicate", first["value_ref"]))
        self.assertEqual(self._state(cid, "text " + cid)[:2], (1, 1))


if __name__ == "__main__":
    unittest.main()
