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
    commit stays retryable as duplicate with its reference: dc.submit is one statement. Expected to PASS at a2d65fd (challenge).

S1 (FINAL-JUDGMENT.md): the recheck design above was replaced by pinned authority. The classes whose oracle was "the mutation commits
during the wait and the call is refused" for points that now come AFTER the pins were moved to tests/test_s1_pg.py with the S1 oracle:
the mutation waits for the call. These were P1_SubmitBlockingPoints, P1_RevokePath, P1_ReadAndEraseCalls, S2_NothingWaitsAfterTheJudgment
and P4_OpenSessionBlockingPoints; their red logs at a2d65fd and 3153059 stay in results/red as history. The classes kept here hold
under S1 as written: their waits come before the pins (relation locks), or their authority is in process."""
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

from tests.kit import enroll, proof
from tests.test_delta1_pg import CALLS, LOCK_KEY, _Base
from tests.test_pg import CFG, T

UNAUTH = "unauthenticated:invalid_session"


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


class P1_JudgmentNeverWaits(_Wait):
    """The judgment must read after every wait, including a wait inside the judgment's own reads. A STABLE function reads with its
    caller's snapshot, and a plpgsql simple expression takes its snapshot before the functions it calls run, so a read there that
    waits for a table lock sees the rows as they were BEFORE the wait (red at a2d65fd: dc.explain's STABLE dc._explain_raw read the
    principal that way). A plain statement of a VOLATILE function reads after its own lock wait on PG 16 (its plan's locks are taken
    before its snapshot; the regression guards here show it). Here the holder takes ACCESS EXCLUSIVE on the table the judgment reads,
    changes the caller's row (session ended, factor revoked, principal revoked) and commits while the call waits. Fixed then: the
    judgment was the VOLATILE dc._gate, which locked its tables first. Under S1 these waits are on relation locks: the token lookup
    (dc._peek) waits for dc.sessions, the L0 relation locks wait for factors and principal. All of them come before the pins, which
    lock and judge the newest version of each row, so the oracle stands."""
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
        return (("live", None), ("policy_revoked", lambda w, pid, s: setattr(w, "pol", parse_policy(policy_doc(revoked=[pid])))),
                ("removed", lambda w, pid, s: w.dir.remove(pid)), ("session_ended", lambda w, pid, s: w.sessions.end(s.session)))

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
                                         lambda: mutate and mutate(lab.w, "alice", s), None, "alice")
                self.assertTrue(waited, "vacuous: the service's dc.submit did not wait")
                want = {"live": "proposed", "policy_revoked": "principal_revoked", "removed": UNAUTH, "session_ended": UNAUTH}[name]
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
                                         lambda: mutate and mutate(lab.w, "admin", adm), None, "admin")
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
