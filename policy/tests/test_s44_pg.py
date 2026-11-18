"""FINAL-JUDGMENT.md 4.4: a holder of a principal's authority lock, or of the tenant lock, that never ends its transaction must not be
able to block a revocation (or an erasure) for longer than a stated bound. Same environment as tests/test_pg.py (paths to 0600 files
only; skipped without it). Written before the eviction code and run red against 8547b27 (results/red/).

At 8547b27 a revoke, end_session, dc.revoke_factor or erase_values that meets such a holder fails retryably (55P03) after XWAIT,
for as long as the holder keeps its transaction open: by resetting idle_in_transaction_session_timeout (user-settable), by a long
statement (pg_sleep, statement_timeout reset), or by a stream of statements (never idle). The fix (ASSUMPTION, the owner's choice, not
the user's): after XWAIT the call evicts (pg_terminate_backend) the dc_app and dc_idp backends of THIS database that hold or wait for
the blocking lock, only after its own caller is pinned and judged, through one SECURITY DEFINER function owned by the NOLOGIN role
dc_evictor (the only role granted pg_signal_backend), behind a gate that refuses the target's new calls while the revocation runs.

Each test prints "MEASURE <name> <seconds>" for the bounds in FINAL-JUDGMENT.md 4.4. Docstrings marked "regression guard" pass at
8547b27 too (negative cases: nothing must be evicted)."""
import os
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

from tests.kit import enroll, proof
from tests.test_final_pg import UNAUTH
from tests.test_pg import T
from tests.test_s1_pg import A_CLS, XWAIT, _S1

EVICT_WAIT = 1.0                  # dc._evict waits up to 1 s for each terminated backend to exit
ROUNDS = 3                        # dc._xlock: at most three wait-evict rounds per exclusive lock
BOUND = XWAIT + EVICT_WAIT + 1.5  # one holder: one XWAIT, one eviction, then the lock (1.5 s slack for the test host)
ADV_BOUND = ROUNDS * (XWAIT + EVICT_WAIT) + XWAIT + 1.5


def measure(name, secs):
    print("MEASURE %s %.3f" % (name, secs), flush=True)


class _S44(_S1):
    def setUp(self):
        super().setUp()
        self._stop = threading.Event()
        self.addCleanup(self._stop.set)

    def conn(self, name, idp=False):
        c = (pgcore.connect_idp(self.port, os.environ["POL_PG_IDP_PASSFILE"], name) if idp
             else pgcore.connect_app(self.port, os.environ["POL_PG_APP_PASSFILE"], name))
        self.addCleanup(self._close, c)
        return c

    @staticmethod
    def _close(c):
        try:
            c.close()
        except Exception:
            pass

    def alive(self, backend_pid):
        return bool(self.count("select count(*) from pg_stat_activity where pid = %s", backend_pid))

    def end(self, *backend_pids):
        """End this test's OWN holder connections that are still there (on the red tree nothing evicts them, and a holder's pins would
        block the next step's owner update). Only pids of connections this test opened are passed here."""
        for bp in backend_pids:
            if self.alive(bp):
                self.o.execute("select pg_terminate_backend(%s, 5000)", (bp,))

    def call(self, fn):
        t0 = time.time()
        try:
            r = fn()
        except psycopg.Error as e:
            r = {"error": e.sqlstate, "message": e.diag.message_primary}
        return r, time.time() - t0

    def holder(self, pid, how, tok=None):
        """A dc_app connection of `pid` that keeps its transaction open after one call (explain, or submit for how='submit'),
        in the way `how` names. Returns (connection, backend pid, token)."""
        tok = tok or self.tok(pid)
        c = self.conn("s44-holder-%s-%s" % (pid, how))
        bp = self.pid(c)
        c.execute("begin")
        c.execute("set local idle_in_transaction_session_timeout = 0")
        c.execute("set local statement_timeout = 0")
        if how == "submit":
            self.assertEqual(self.submit(c, tok, self.body(None, "held", req="r1" if pid != "alice" else "r2"))["code"], "proposed")
        else:
            c.execute("select dc.explain(%s, 'r1')", (tok,))
        if how == "sleep":
            threading.Thread(target=self._swallow, args=(lambda: c.execute("select pg_sleep(60)"),), daemon=True).start()
        elif how == "stream":
            def loop():
                while not self._stop.is_set():
                    c.execute("select 1")
                    time.sleep(0.05)
            threading.Thread(target=self._swallow, args=(loop,), daemon=True).start()
        time.sleep(0.2)
        self.assertTrue(self.alive(bp))
        return c, bp, tok

    @staticmethod
    def _swallow(fn):
        try:
            fn()
        except Exception:
            pass

    def revoke(self, target, adm=None, conn=None):
        adm = adm or self.tok("admin")
        return self.call(lambda: self.submit(conn or self.app, adm, self.revoke_body(target)))


class S44_Eviction(_S44):
    """The target's own connection keeps its transaction open; the revocation must still apply within the bound."""

    def test_revoke_evicts_the_target_holder(self):
        """Red at 8547b27: 55P03 after XWAIT and the holder still connected, for every way of holding."""
        for how in ("idle_reset", "sleep", "stream", "submit"):
            with self.subTest(holder=how):
                self.unrevoke("alice")
                c, bp, tok = self.holder("alice", how)
                r, dt = self.revoke("alice")
                measure("revoke_target_holder_" + how, dt)
                self._stop.set()
                was = self.alive(bp)
                self.end(bp)
                self._stop.clear()
                self.assertEqual(r.get("code"), "revoked", "%r" % (r,))
                self.assertLess(dt, BOUND)
                self.assertFalse(was, "the holder is still connected")
                self.assertEqual(self.submit(self.app, tok, self.body())["code"], UNAUTH)
                self.unrevoke("alice")

    def test_end_session_evicts_a_holder(self):
        """end_session (A exclusive, by dc_idp) of a token whose principal's connection holds the authority lock."""
        c, bp, tok = self.holder("alice", "idle_reset")
        r, dt = self.call(lambda: self.idp.execute("select dc.end_session(%s)", (tok,)).fetchone()[0])
        measure("end_session_holder", dt)
        was = self.alive(bp)
        self.end(bp)
        self.assertIs(r, True, "%r" % (r,))
        self.assertLess(dt, BOUND)
        self.assertFalse(was)
        self.assertEqual(self.submit(self.app, tok, self.body())["code"], UNAUTH)

    def test_revoke_factor_evicts_a_holder(self):
        """dc.revoke_factor (owner-only, A exclusive) of the holder's factor."""
        c, bp, tok = self.holder("alice", "stream")
        f = self.count("select factor from dc.sessions where token_hash = %s", self.h(tok))
        r, dt = self.call(lambda: self.o.execute("select dc.revoke_factor(%s)", (f,)).fetchone()[0])
        measure("revoke_factor_holder", dt)
        self._stop.set()
        was = self.alive(bp)
        self.end(bp)
        self.assertIs(r, True, "%r" % (r,))
        self.assertLess(dt, BOUND)
        self.assertFalse(was)

    def test_tenant_lock_holder_is_evicted(self):
        """Any submitter of the tenant holds the tenant lock (L3) shared until COMMIT; a revoke of ANOTHER principal and an erasure
        (tenant lock exclusive) must still apply. Red at 8547b27: 55P03 after XWAIT."""
        self.unrevoke("alice")
        c, bp, _ = self.holder("bob", "submit")
        r, dt = self.revoke("alice")
        measure("revoke_tenant_holder", dt)
        was = self.alive(bp)
        self.end(bp)
        self.assertEqual(r.get("code"), "revoked", "%r" % (r,))
        self.assertLess(dt, BOUND)
        self.assertFalse(was)
        self.unrevoke("alice")
        ref = self.submit(self.app, self.tok("alice"), self.body(None, "about dana", subjects=["dana"], req="r2"))["value_ref"]
        c2, bp2, _ = self.holder("bob", "submit")
        adm = self.tok("admin")
        r, dt = self.call(lambda: self.app.execute("select dc.erase_values(%s, 'dana', %s)", (adm, [ref])).fetchone()[0])
        measure("erase_tenant_holder", dt)
        was = self.alive(bp2)
        self.end(bp2)
        self.assertEqual((r.get("refused"), r.get("erased")), (None, 1), "%r" % (r,))
        self.assertLess(dt, BOUND)
        self.assertFalse(was)

    def test_a_reconnecting_adversary(self):
        """Three connections of alice keep re-taking her authority lock: each opens a transaction, makes a call, holds it 1 s with
        the idle timeout reset, and reconnects at once when it is evicted. The gate refuses alice's new calls while the revocation
        runs, so each round can only meet holders that started before it. Regression guard, NOT failing-first: it passes at 8547b27
        too (o2 red run, 0.53 s), because each hold ends after 1 s and the revoke's exclusive request queues ahead of later shared
        ones (FIFO). Commit 174d837's message called it red; that was wrong. It stays as the measured bound for this adversary."""
        self.unrevoke("alice")
        tok = self.tok("alice")
        ap = os.environ["POL_PG_APP_PASSFILE"]
        stats = {"evicted": 0, "calls": 0, "gated": 0}

        def adversary(i):
            c = None
            while not self._stop.is_set():
                try:
                    if c is None or c.closed:
                        c = pgcore.connect_app(self.port, ap, "s44-adv-%d" % i)
                    c.execute("begin")
                    c.execute("set local idle_in_transaction_session_timeout = 0")
                    c.execute("select dc.explain(%s, 'r1')", (tok,))
                    stats["calls"] += 1
                    time.sleep(1.0)
                    c.execute("commit")
                except psycopg.Error as e:
                    if getattr(e, "sqlstate", None) == "55P03" and c is not None and not c.closed:
                        stats["gated"] += 1             # the gate (or a lock wait) refused the call; the connection is fine
                        try:
                            c.execute("rollback")
                            continue
                        except psycopg.Error:
                            pass
                    stats["evicted"] += 1               # terminated (57P01) or the connection is gone: reconnect at once
                    if c is not None:
                        self._close(c)
                    c = None
            if c is not None:
                self._close(c)
        ts = [threading.Thread(target=adversary, args=(i,), daemon=True) for i in range(3)]
        for t in ts:
            t.start()
        time.sleep(1.5)
        r, dt = self.revoke("alice")
        measure("revoke_reconnecting_adversary", dt)
        self._stop.set()
        for t in ts:
            t.join(10)
        self.o.execute("select pg_terminate_backend(pid, 5000) from pg_stat_activity where datname = current_database()"
                       " and application_name like 's44-adv-%%'")       # this test's own adversary connections, if any are left
        print("ADVERSARY %r" % (stats,), flush=True)
        self.assertEqual(r.get("code"), "revoked", "%r" % (r,))
        self.assertLess(dt, ADV_BOUND)
        self.assertEqual(self.submit(self.app, tok, self.body())["code"], UNAUTH)
        self.unrevoke("alice")


class S44_Scope(_S44):
    """What eviction must never touch."""

    def test_a_refused_revoker_evicts_nobody(self):
        """Regression guard: the admin's own authority changes while its revoke waits for the holder; the revoke is refused and the
        holder is still connected. Eviction comes only after the caller's pinned judgment."""
        for how in ("expire", "factor", "principal"):
            with self.subTest(mutation=how):
                self.unrevoke("alice")
                self.unrevoke("admin")
                c, bp, _ = self.holder("alice", "idle_reset")
                adm = self.tok("admin", ttl=1) if how == "expire" else self.tok("admin")
                t, out = self.start(lambda: self.submit(self.app, adm, self.revoke_body("alice")))
                time.sleep(0.5)
                if how == "factor":
                    self.sup2.execute("update dc.factors set revoked = true where factor = (select factor from dc.sessions where token_hash = %s)",
                                      (self.h(adm),))
                elif how == "principal":
                    self.sup2.execute("update dc.principal set revoked = true where tenant = %s and pid = 'admin'", (T,))
                t.join(ADV_BOUND)
                self.assertFalse(t.is_alive())
                r = out.get("r")
                self.assertNotEqual(r.get("code"), "revoked", "%r" % (r,))
                self.assertTrue(self.alive(bp), "a refused revoker evicted the holder")
                self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = 'alice'", T))
                c.execute("rollback")
                self.unrevoke("admin")

    def test_an_owner_holder_is_never_evicted(self):
        """Regression guard: a superuser connection holding alice's authority lock is not a dc_app or dc_idp backend. The revoke gives
        up retryably (55P03) after its rounds, applies nothing, and the holder is still connected."""
        self.unrevoke("alice")
        self.hold.execute("begin")
        self.hold.execute("select pg_advisory_xact_lock_shared(%s::int, dc._ak(%s::text, 'alice'))", (A_CLS, T))
        r, dt = self.revoke("alice")
        measure("revoke_owner_holder_gives_up", dt)
        self.hold.execute("rollback")
        self.assertEqual(r.get("error"), "55P03", "%r" % (r,))
        self.assertLess(dt, ROUNDS * (XWAIT + EVICT_WAIT) + 2.0)
        self.assertTrue(self.alive(self.pid(self.hold)))
        self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = 'alice'", T))

    def test_other_principals_and_databases_are_not_evicted(self):
        """Revoking alice evicts alice's holder only: bob's connection holding his own authority lock (no tenant lock), an idle dc_idp
        connection, and a dc_app connection holding THE SAME advisory key in another database all stay connected."""
        self.unrevoke("alice")
        other = "pol_s44_other"
        self.o.execute("drop database if exists %s" % other)
        self.o.execute("create database %s" % other)
        self.addCleanup(lambda: self.o.execute("drop database if exists %s with (force)" % other))
        x = psycopg.connect(host=pgcore.HOST, port=self.port, dbname=other, user="dc_app", passfile=os.environ["POL_PG_APP_PASSFILE"],
                            autocommit=True, application_name="s44-other-db")
        self.addCleanup(self._close, x)
        x.execute("begin")
        ak = self.count("select dc._ak(%s, 'alice')", T)      # alice's exact key, taken in the OTHER database (no dc schema there)
        self.assertIsNotNone(ak)
        x.execute("select pg_advisory_xact_lock_shared(%s::int, %s::int)", (A_CLS, ak))
        xp = x.info.backend_pid
        cb, bpb, _ = self.holder("bob", "idle_reset")
        ci = self.conn("s44-idle-idp", idp=True)
        ip = self.pid(ci)
        ca, bpa, _ = self.holder("alice", "idle_reset")
        r, dt = self.revoke("alice")
        measure("revoke_with_bystanders", dt)
        self.assertEqual(r.get("code"), "revoked", "%r" % (r,))
        self.assertFalse(self.alive(bpa))
        for name, p in (("bob's holder", bpb), ("the idle dc_idp connection", ip), ("the other database's holder", xp)):
            self.assertTrue(self.alive(p), name + " was evicted")
        x.execute("rollback")
        self.unrevoke("alice")

    def test_the_evictor_role_is_scoped(self):
        """4.4 only (no such role at 8547b27): dc_evictor cannot log in, is not a superuser, is a member of pg_signal_backend and of
        nothing else, owns dc._evict and nothing else; only dc_owner (and superusers) may execute dc._evict, not dc_app or dc_idp."""
        self.assertEqual(self.o.execute("select rolcanlogin, rolsuper, rolcreaterole, rolcreatedb from pg_roles where rolname = 'dc_evictor'")
                         .fetchone(), (False, False, False, False))
        member_of = {r for (r,) in self.o.execute("""select b.rolname from pg_auth_members m join pg_roles a on a.oid = m.member
                                                      join pg_roles b on b.oid = m.roleid where a.rolname = 'dc_evictor'""").fetchall()}
        self.assertEqual(member_of, {"pg_signal_backend"})
        owned = {f for (f,) in self.o.execute("select oid::regprocedure::text from pg_proc where proowner = 'dc_evictor'::regrole").fetchall()}
        self.assertEqual(owned, {"dc._evict(integer,integer,text[])"})
        can = {r: self.o.execute("select has_function_privilege(%s, 'dc._evict(integer,integer,text[])', 'EXECUTE')", (r,)).fetchone()[0]
               for r in ("dc_app", "dc_idp", "dc_owner")}
        self.assertEqual(can, {"dc_app": False, "dc_idp": False, "dc_owner": True})
        d = self.o.execute("select prosecdef, proconfig from pg_proc where oid = 'dc._evict(integer,integer,text[])'::regprocedure").fetchone()
        self.assertTrue(d[0])
        self.assertIn("search_path=pg_catalog, pg_temp", d[1] or [])

    def test_login_roles_cannot_take_advisory_locks(self):
        """4.4: a raw advisory call is a way to hold A, T, a fence or an idempotency lock outside any API call, where no judgment or
        eviction scope applies. In this database only dc_owner may call the advisory functions. Red at 8547b27 (they are PUBLIC)."""
        for role, c in (("dc_app", self.conn("s44-raw-app")), ("dc_idp", self.conn("s44-raw-idp", idp=True))):
            for q in ("select pg_advisory_xact_lock_shared(%s::int, 1)" % A_CLS, "select pg_try_advisory_lock(1)",
                      "select pg_advisory_lock_shared(1145241606, 1)"):
                with self.subTest(role=role, q=q):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        c.execute(q)
        self.assertTrue(self.o.execute("select has_function_privilege('dc_owner', 'pg_advisory_xact_lock(int4,int4)', 'EXECUTE')")
                        .fetchone()[0])

    def test_a_queued_revoker_is_not_evicted(self):
        """4.4: two admins revoke alice while her holder stalls. The one that evicts must not end the other, which waits for the same
        lock EXCLUSIVELY (a queued revocation, not a stall). Red at 8547b27 (both give up, 55P03); red too for an evictor that ends
        waiters of either mode."""
        self.unrevoke("alice")
        c, bp, _ = self.holder("alice", "idle_reset")
        r2c = self.conn("s44-second-revoker")
        r2p = self.pid(r2c)
        adm = self.tok("admin")
        t1, o1 = self.start(lambda: self.submit(self.app, adm, self.revoke_body("alice")))
        time.sleep(0.5)
        t2, o2 = self.start(lambda: self.submit(r2c, adm, self.revoke_body("alice")))
        t1.join(ADV_BOUND)
        t2.join(ADV_BOUND)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertFalse(self.alive(bp), "the stalled holder survived")
        self.assertTrue(self.alive(r2p), "the queued revoker was evicted: %r" % (o2,))
        self.assertTrue(self.count("select revoked from dc.principal where tenant = %s and pid = 'alice'", T))
        self.assertTrue(any((o.get("r") or {}).get("code") == "revoked" for o in (o1, o2)), "%r %r" % (o1, o2))
        self.unrevoke("alice")

    def test_no_holder_evicts_nothing(self):
        """Regression guard: a revoke with nothing in its way ends no backend."""
        self.unrevoke("alice")
        q = "select array_agg(pid order by pid) from pg_stat_activity where datname = current_database() and usename in ('dc_app', 'dc_idp')"
        before = self.count(q)
        r, dt = self.revoke("alice")
        self.assertEqual(r.get("code"), "revoked")
        self.assertEqual(self.count(q), before)
        self.unrevoke("alice")


FENCE_A = "select pg_advisory_xact_lock(1145241605, dc._ak(%s::text, %s::text))"        # exact keys (review of 2262eaa F1)
FENCE_T = "select pg_advisory_xact_lock(1145241606, dc._tk(%s::text))"


class _Fence(_S44):
    def calls(self, pid, which=("explain", "submit", "open_session")):
        """Each named call by `pid`, prepared (token, factor) BEFORE the fence is taken; returns ({call: result}, {call: seconds})."""
        tok = self.tok(pid)
        f = enroll(self.o, T, pid, "owner_ui")
        fns = {"explain": lambda: self.app.execute("select dc.explain(%s, 'r1')", (tok,)).fetchone()[0],
               "submit": lambda: self.submit(self.app, tok, self.body(None, "fenced")),
               "open_session": lambda: self.idp.execute("select dc.open_session(%s, 60)", (proof(f),)).fetchone()[0]}
        return lambda: {n: self.call(fns[n]) for n in which}

    def under(self, fence_sql, args, *preps):
        """Run each prepared call set while the superuser `hold` connection holds the fence lock (as an evicting revocation would)."""
        self.hold.execute("begin")
        try:
            self.hold.execute(fence_sql, args)
            return [p() for p in preps]
        finally:
            self.hold.execute("rollback")

    @staticmethod
    def err(r):
        return r.get("error") if isinstance(r, dict) else None


class S44_Fence(_Fence):
    """While a revocation evicts it holds the fence of the lock it evicts for; a new call that would take that lock shared refuses at
    once (55P03), nothing done, instead of re-taking it behind the revocation's back. Held here directly by a superuser connection, so
    the test does not depend on eviction timing (the reconnecting adversary never reaches the fence: its holds end within XWAIT).
    Red at 8547b27 (no fence: every call answers)."""

    def test_the_authority_fence_refuses_the_target_only(self):
        self.unrevoke("alice")
        a, b = self.under(FENCE_A, (T, "alice"), self.calls("alice"), self.calls("bob"))
        for name, (r, dt) in a.items():
            with self.subTest(principal="alice", call=name):
                self.assertEqual(self.err(r), "55P03", "%r" % (r,))
                self.assertLess(dt, 0.5, "the fenced call waited instead of refusing")
        for name, (r, dt) in b.items():
            with self.subTest(principal="bob", call=name):
                self.assertIsNone(self.err(r), "%r" % (r,))
        after = self.submit(self.app, self.tok("alice"), self.body(None, "after", req="r2"))
        self.assertNotEqual(after.get("code"), UNAUTH, "%r" % (after,))         # the fence ends with the revocation's transaction

    def test_the_tenant_fence_refuses_submits_only(self):
        self.unrevoke("alice")
        (a,) = self.under(FENCE_T, (T,), self.calls("alice"))
        self.assertEqual(self.err(a["submit"][0]), "55P03", "%r" % (a["submit"],))
        self.assertLess(a["submit"][1], 0.5, "the fenced submit waited instead of refusing")
        for name in ("explain", "open_session"):
            with self.subTest(call=name):
                self.assertIsNone(self.err(a[name][0]), "%r" % (a[name],))


class S44_FenceControl(_Fence):
    def test_without_the_fence_check_the_target_is_not_refused(self):
        """Control for S44_Fence: dc._fenced made a no-op; the target's calls answer under a held fence."""
        self.o.execute("create or replace function dc._fenced(p_fcls int, p_key int) returns void language plpgsql as $$ begin end $$")
        self.unrevoke("alice")
        (a,) = self.under(FENCE_A, (T, "alice"), self.calls("alice"))
        for name, (r, dt) in a.items():
            with self.subTest(call=name):
                self.assertIsNone(self.err(r), "%r" % (r,))

    """The control: the same build with dc._evict made a no-op must show the defect (the holder blocks the revoke). It passes at
    8547b27 too, where nothing evicts."""

    def test_without_eviction_the_holder_blocks_the_revoke(self):
        self.o.execute("create or replace function dc._evict(p_cls int, p_key int, p_snap text[]) returns int language sql as $$ select 0 $$")
        self.unrevoke("alice")
        c, bp, _ = self.holder("alice", "idle_reset")
        r, dt = self.revoke("alice")
        measure("control_no_eviction", dt)
        self.assertEqual(r.get("error"), "55P03", "%r" % (r,))
        self.assertTrue(self.alive(bp))
        self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = 'alice'", T))


class S44_Options(_S44):
    """Measurements for the options in FINAL-JUDGMENT.md 4.4. Regression guards (they hold at 8547b27 too)."""

    def test_the_idle_timeout_bounds_only_a_cooperating_holder(self):
        """Option 'timeouts': with the role default lowered to 3 s, a holder that leaves its setting alone is ended by the server
        after about 3 s; a holder that sets it to 0 for its transaction is still connected 8 s later. statement_timeout is
        user-settable in the same way. So the timeouts bound only cooperating clients."""
        self.o.execute("alter role dc_app set idle_in_transaction_session_timeout = '3s'")
        try:
            coop = self.conn("s44-coop")
            cp = self.pid(coop)
            coop.execute("begin")
            coop.execute("select dc.explain(%s, 'r1')", (self.tok("alice"),))
            hard, hp, _ = self.holder("bob", "idle_reset")
            t0 = time.time()
            while self.alive(cp) and time.time() - t0 < 10:
                time.sleep(0.1)
            measure("idle_timeout_cooperating_holder_ended_after", time.time() - t0)
            self.assertFalse(self.alive(cp))
            time.sleep(max(0.0, 8.0 - (time.time() - t0)))
            measure("idle_timeout_noncooperating_holder_still_connected_after", time.time() - t0)
            self.assertTrue(self.alive(hp))
        finally:
            self.o.execute("alter role dc_app set idle_in_transaction_session_timeout = '60s'")


if __name__ == "__main__":
    unittest.main()
