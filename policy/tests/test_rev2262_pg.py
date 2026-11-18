"""Review of 2262eaa (research/policy-review-2262eaa.md), Postgres findings, reproduced first. Same environment as tests/test_pg.py
(paths to 0600 files only). Each test runs UNMODIFIED against 2262eaa (it imports only helpers that exist there and computes no key
with a function the fix introduces, except where a docstring says so) and fails there by a behavioural assertion unless its
docstring says it is a regression guard. Red and green logs: policy/results/red/.

F1  One 32-bit advisory key space for all tenants: dc._k = hashtext(kind, tenant, name). A tenant's admin revoking a target whose key
    collides with another tenant's principal evicted that principal's backends and fenced its calls (the fence and the eviction match
    the key, not the tenant); a revoke took the exclusive lock for ANY target string, existing or not. A subject of one tenant could
    stall another tenant's submits the same way. Fixed: exact lock identities (the lockid of the principal and tenant rows), the
    subject lock classed by tenant, and an exclusive request only when the rules would apply the change.
F5  A grant (or revoke) that the rules refuse still took T exclusively and evicted the tenant's in-flight submitter.
F2  (a) a prepared transaction kept a call's authority lock with no backend, so its principal could not be revoked; (b) a login role
    that is a MEMBER of dc_app was never evicted (eviction matched the user name). Fixed: fail closed while max_prepared_transactions
    > 0 (install and every API call refuse); eviction by role class, not name.
H1  A revocation in progress (holding the target's A exclusively, waiting for the tenant lock) was evicted by a later exclusive
    request on the same key (here dc_idp's end_session), cancelling it.
F3  The first eviction round of a revoke ran without the fence, so a holder that reconnected at once re-took the target's authority
    lock behind the eviction and the revoke needed another round.
F8  Install revoked only PUBLIC's EXECUTE on the advisory-lock functions (an explicit grant to dc_app, or to a role dc_app is a member
    of, survived); the migration's rollback granted PUBLIC instead of restoring the ACL recorded before forward.

Environment for F2a (never skipped silently: the test FAILS when it is missing): POL_PG_PREP_PORT, a second server of the same
cluster settings except max_prepared_transactions > 0, reachable with the same 0600 password files."""
import os
import secrets
import tempfile
import threading
import time
import unittest

try:
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb
    from pol import pgcore
except ImportError:              # pragma: no cover
    psycopg = None

from tests.kit import pg_token
from tests.test_final_pg import UNAUTH
from tests.test_pg import CFG, ENV, T
from tests.test_s1_pg import XWAIT
from tests.test_s44_pg import BOUND, EVICT_WAIT, _S44, measure

TB = "tenant-b"
A_CLS, T_CLS = 1145241601, 1145241602
_COLL = {}


class _Rev(_S44):
    # ------------------------------------------------------------------ helpers that work on 2262eaa and on the fix
    def collide(self, kind, ta, pa, tb, pb):
        """(name in tenant ta, name in tenant tb) whose keys under 2262eaa's formula hashtext(jsonb_build_array(kind, tenant, name)) are
        equal: a 200k x 200k hash join (about 9 expected pairs; the first in name order is used, so the pair is deterministic)."""
        k = (kind, ta, pa, tb, pb)
        if k not in _COLL:
            r = self.o.execute(
                """with a as (select %s::text || i::text x, hashtext(jsonb_build_array(%s::text, %s::text, %s::text || i::text)::text) h
                                from generate_series(1, 200000) i),
                        b as (select %s::text || i::text x, hashtext(jsonb_build_array(%s::text, %s::text, %s::text || i::text)::text) h
                                from generate_series(1, 200000) i)
                   select a.x, b.x from a join b using (h) order by a.x, b.x limit 1""", (pa, kind, ta, pa, pb, kind, tb, pb)).fetchone()
            self.assertIsNotNone(r, "no collision found")
            _COLL[k] = r
        return _COLL[k]

    def tenants(self, a=(), b=()):
        """Reinstall with two tenants (the install's own genesis): tenant-a as CFG plus the human principals `a`, tenant-b as CFG plus
        `b`, each extra principal with role decide on r1. Called before any token is minted (the install replaces schema dc)."""
        def cfg(tenant, extra):
            return dict(CFG, tenant=tenant, principals=dict(CFG["principals"], **{p: "human" for p in extra}),
                        authority=dict(CFG["authority"], r1=list(CFG["authority"]["r1"]) + list(extra)))
        pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"],
                       {T: cfg(T, a), TB: cfg(TB, b)}, idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])

    def body_b(self, value, subjects):
        b = self.body(None, value, subjects=subjects)
        b["label"] = dict(b["label"], tenants=[TB])
        return b

    def token(self, tenant, pid):
        return pg_token(self.o, self.idp, tenant, pid, "owner_ui")

    def hold_explain(self, tok, name):
        """A connection that makes one explain and keeps its transaction open (idle timeout reset): a stalled holder of the caller's
        authority lock. Returns (connection, backend pid, the explain result)."""
        c = self.conn(name)
        bp = self.pid(c)
        c.execute("begin")
        c.execute("set local idle_in_transaction_session_timeout = 0")
        out = c.execute("select dc.explain(%s, 'r1')", (tok,)).fetchone()[0]
        return c, bp, out

    def key(self, kind, tenant, pid=None):
        """A's or T's key: the exact lockid on the fix (dc._ak, dc._tk), the 32-bit hash at 2262eaa (only for holds taken by the test)."""
        if kind == "auth" and self.count("select to_regprocedure('dc._ak(text,text)') is not null"):
            return self.count("select dc._ak(%s, %s)", tenant, pid)
        if kind == "tenant" and self.count("select to_regprocedure('dc._tk(text)') is not null"):
            return self.count("select dc._tk(%s)", tenant)
        return self.count("select hashtext(jsonb_build_array(%s::text, %s::text, %s::text)::text)", kind, tenant, pid)

    def looper(self, tok, stop, name):
        """Calls explain(tok) on its own connection every 50 ms until stop is set; returns the list of (seconds, result or error)."""
        out = []

        def run():
            c = pgcore.connect_app(self.port, os.environ["POL_PG_APP_PASSFILE"], name)
            try:
                while not stop.is_set():
                    t0 = time.time()
                    try:
                        r = c.execute("select dc.explain(%s, 'r1')", (tok,)).fetchone()[0]
                    except psycopg.Error as e:
                        r = {"error": e.sqlstate}
                    out.append((time.time() - t0, r))
                    time.sleep(0.05)
            finally:
                c.close()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self._threads.append(t)
        return out, t


class F1_CrossTenant(_Rev):
    def test_a_nonexistent_colliding_target_evicts_nobody(self):
        """NEG-1. tenant-a's admin revokes a name that is no principal of tenant-a and whose 2262eaa key equals tenant-b principal v's.
        Red at 2262eaa: after XWAIT the revoke evicted v's backend (then answered bad_event)."""
        v, t = self.collide("auth", TB, "v", T, "t")
        self.tenants(b=[v])
        c, bp, seen = self.hold_explain(self.token(TB, v), "rev-victim")
        self.assertIsNotNone(seen, "positive control: v reads r1 of tenant-b")
        r, dt = self.revoke(t)
        measure("f1_nonexistent_colliding_target", dt)
        self.assertTrue(self.alive(bp), "a tenant-a revoke of a nonexistent target ended a tenant-b backend")
        self.assertEqual(r.get("code"), "bad_event", "%r" % (r,))
        self.assertLess(dt, XWAIT, "the refused revoke waited for another tenant's holder")

    def test_colliding_principals_never_cross_evict_or_cross_fence(self):
        """NEG-2. Both names exist: t in tenant-a (stalled holder, must be evicted: positive control) and v in tenant-b (stalled holder
        and a stream of calls). Red at 2262eaa: v's holder was evicted with t's, and v's calls stalled or were refused (fence, 55P03)."""
        v, t = self.collide("auth", TB, "v", T, "t")
        self.tenants(a=[t], b=[v])
        vtok = self.token(TB, v)
        tc, tbp, _ = self.hold_explain(self.token(T, t), "rev-target")
        vc, vbp, _ = self.hold_explain(vtok, "rev-victim")
        stop = threading.Event()
        calls, lt = self.looper(vtok, stop, "rev-victim-loop")
        time.sleep(0.3)
        r, dt = self.revoke(t)
        time.sleep(0.3)
        stop.set()
        lt.join(10)
        measure("f1_colliding_existing_target", dt)
        self.assertEqual(r.get("code"), "revoked", "%r" % (r,))
        self.assertFalse(self.alive(tbp), "positive control: the target's own stalled holder was not evicted")
        self.assertTrue(self.alive(vbp), "a tenant-a revoke ended tenant-b's backend through a key collision")
        bad = [(round(s, 3), x) for s, x in calls if x is None or (isinstance(x, dict) and "error" in x) or s > 0.5]
        self.assertGreaterEqual(len(calls), 5, "vacuous: too few victim calls")
        self.assertEqual(bad, [], "tenant-b's calls were refused, emptied or stalled while tenant-a revoked")

    def test_a_colliding_subject_never_stalls_another_tenant(self):
        """NEG-4. alice (tenant-a) submits a value about subject sa and keeps her transaction open; tenant-b's v submits about sb, whose
        2262eaa subject key equals sa's. Red at 2262eaa: v's submit waited on alice's subject lock (55P03 at its 1 s lock_timeout)."""
        sa, sb = self.collide("subject", T, "sa", TB, "sb")
        self.tenants(b=["vs"])
        vtok = self.token(TB, "vs")
        h = self.conn("rev-subject-holder")
        h.execute("begin")
        h.execute("set local idle_in_transaction_session_timeout = 0")
        self.assertEqual(self.submit(h, self.tok("alice"), self.body(None, "about sa", subjects=[sa]))["code"], "proposed")
        v = self.conn("rev-subject-victim")
        v.execute("set lock_timeout = '1s'")
        t0 = time.time()
        th, out = self.start(lambda: self.submit(v, vtok, self.body_b("about sb", [sb])))
        th.join(3.0)                                             # the holder lets go after 3 s at the latest, so a red run ends
        h.execute("rollback")
        th.join(15)
        self.assertFalse(th.is_alive(), "stuck")
        r, dt = out.get("r") or {}, out.get("t", time.time()) - t0
        measure("f1_colliding_subject", dt)
        self.assertEqual(r.get("code"), "proposed", "%r" % (r,))
        self.assertLess(dt, 0.5, "tenant-b's submit waited for tenant-a's subject lock")


class F5_RefusedAuthorityChange(_Rev):
    BODIES = {"grant_unknown_req": {"type": "grant", "req": "no-such-req", "target": "alice", "role": "propose"},
              "grant_unknown_target": {"type": "grant", "req": "r1", "target": "no-such-principal", "role": "propose"},
              "grant_revoked_target": {"type": "grant", "req": "r1", "target": "x", "role": "propose"},
              "revoke_unknown_target": {"type": "revoke", "target": "no-such-principal"}}

    def _refused(self, adm_tok):
        cid = "g" + secrets.token_hex(3)
        return lambda b: self.call(lambda: self.submit(self.app, adm_tok(), dict(b, client_id=cid, idem=cid)))

    def test_refused_changes_never_wait_for_the_tenant_lock(self):
        """NEG-3a. An owner connection holds the tenant lock T shared (never evicted, and it holds no chain head). A grant to a
        nonexistent requirement, to a nonexistent or revoked target, and a revoke of a nonexistent target are refused by the rules
        (bad_event) at once. Red at 2262eaa: each took T exclusively, waited XWAIT, found nobody to evict and failed (55P03)."""
        self.o.execute("update dc.principal set revoked = true where tenant = %s and pid = 'x'", (T,))
        adm = self.tok("admin")
        self.hold.execute("begin")
        self.hold.execute("select pg_advisory_xact_lock_shared(%s::int, %s::int)", (T_CLS, self.key("tenant", T)))
        try:
            for name, b in self.BODIES.items():
                with self.subTest(name):
                    r, dt = self._refused(lambda: adm)(b)
                    measure("f5_owner_t_" + name, dt)
                    self.assertEqual(r.get("code"), "bad_event", "%r" % (r,))
                    self.assertLess(dt, XWAIT, "a refused authority change waited for the tenant lock")
        finally:
            self.hold.execute("rollback")

    def test_refused_changes_evict_nobody(self):
        """NEG-3b. bob's submit holds T shared AND the tenant's chain head row (L7) in an open transaction. The same refused changes,
        each with a 4 s admin token, must not evict bob. On the fix they wait at the chain head like any submit of the tenant and are
        refused at the token's expiry (FINAL-JUDGMENT 4.4 residual 3), or answer bad_event. Red at 2262eaa: each took T exclusively
        and evicted bob after XWAIT. The valid grant at the end is the positive control (it evicts bob's holder)."""
        self.o.execute("update dc.principal set revoked = true where tenant = %s and pid = 'x'", (T,))
        c, bp, _ = self.holder("bob", "submit")
        for name, b in self.BODIES.items():
            with self.subTest(name):
                tok = self.tok("admin", ttl=4)
                r, dt = self._refused(lambda: tok)(b)
                measure("f5_stalled_submitter_" + name, dt)
                self.assertTrue(self.alive(bp), "a refused %s evicted bob's in-flight submit: %r" % (name, r))
                self.assertIn(r.get("code"), ("bad_event", UNAUTH), "%r" % (r,))
        cid = "g" + secrets.token_hex(3)
        r, dt = self.call(lambda: self.submit(self.app, self.tok("admin"), {"type": "grant", "req": "r1", "target": "alice",
                                                                             "role": "propose", "client_id": cid, "idem": cid}))
        self.assertEqual(r.get("code"), "granted", "%r" % (r,))
        self.assertFalse(self.alive(bp), "positive control: a valid grant evicts the stalled tenant-lock holder")


class F2b_MemberRole(_Rev):
    ROLE = "polfix_member_login"

    def _drop(self):
        self.o.execute("select pg_terminate_backend(pid, 5000) from pg_stat_activity where usename = %s", (self.ROLE,))
        self.o.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(self.ROLE)))

    def test_a_member_login_role_holder_is_evicted(self):
        """NEG-6. A login role that is a member of dc_app (the one-role-per-service pattern) calls explain as alice and keeps its
        transaction open. Red at 2262eaa: the revoke gave up (55P03) and the holder stayed connected (eviction matched usename)."""
        self._drop()
        self.addCleanup(self._drop)
        pw = secrets.token_hex(24)                               # in memory only; the server receives its SCRAM verifier
        self.o.execute(sql.SQL("create role {} login password {} in role dc_app").format(sql.Identifier(self.ROLE),
                                                                                         sql.Literal(pgcore._scram_verifier(pw))))
        m = psycopg.connect(host=pgcore.HOST, port=self.port, dbname=pgcore._dbname(), user=self.ROLE, password=pw, autocommit=True,
                            application_name="rev-member")
        del pw
        self.addCleanup(self._close, m)
        mp = m.info.backend_pid
        m.execute("begin")
        m.execute("set local idle_in_transaction_session_timeout = 0")
        self.assertIsNotNone(m.execute("select dc.explain(%s, 'r1')", (self.tok("alice"),)).fetchone()[0], "positive control")
        r, dt = self.revoke("alice")
        measure("f2b_member_role_holder", dt)
        was = self.alive(mp)
        self.end(mp)
        self.assertEqual(r.get("code"), "revoked", "%r" % (r,))
        self.assertFalse(was, "the member-role holder was not evicted")
        self.assertLess(dt, BOUND)


class H1_RevocationInProgress(_Rev):
    def test_end_session_does_not_cancel_a_revoke_in_progress(self):
        """H1. The admin's revoke of alice holds A(alice) exclusively and waits for the tenant lock: bob's stalled submit (evicted in its
        first round) and an owner connection that holds T shared until 3.5 s (never evicted, so the revoke waits a second round).
        Meanwhile dc_idp ends alice's session: it waits XWAIT for A(alice), then evicts. Red at 2262eaa: it evicted the revoker
        (granted holder), and the revocation was lost."""
        atok = self.tok("alice")
        bc, bbp, _ = self.holder("bob", "submit")
        self.hold.execute("begin")
        self.hold.execute("select pg_advisory_xact_lock_shared(%s::int, %s::int)", (T_CLS, self.key("tenant", T)))
        rc = self.conn("rev-revoker")
        rp = self.pid(rc)
        adm = self.tok("admin")
        t0 = time.time()
        rt, ro = self.start(lambda: self.submit(rc, adm, self.revoke_body("alice")))
        time.sleep(0.3)
        et, eo = self.start(lambda: self.idp.execute("select dc.end_session(%s)", (atok,)).fetchone()[0])
        time.sleep(max(0.0, t0 + 3.5 - time.time()))
        self.hold.execute("rollback")
        rt.join(15)
        et.join(15)
        self.assertFalse(rt.is_alive() or et.is_alive(), "stuck")
        print("H1 revoke %r end_session %r" % (ro.get("r"), eo.get("r")), flush=True)
        self.assertTrue(self.alive(rp), "end_session evicted the revocation in progress")
        self.assertEqual((ro.get("r") or {}).get("code"), "revoked", "%r" % (ro,))
        self.assertTrue(self.count("select revoked from dc.principal where tenant = %s and pid = 'alice'", T))
        self.assertFalse(self.alive(bbp), "positive control: bob's stalled submit was evicted by the revoke")

    def test_a_stalled_applied_revoke_is_still_evicted(self):
        """H1 mirror, regression guard (passes at 2262eaa too): an admin transaction that applied a revoke and stays open holds A(bob)
        and T exclusively but waits for nothing. It is a stalled holder, not a revocation in progress: a later grant (T exclusive) must
        evict it and apply."""
        sc = self.conn("rev-stalled-admin")
        sp = self.pid(sc)
        sc.execute("begin")
        sc.execute("set local idle_in_transaction_session_timeout = 0")
        self.assertEqual(self.submit(sc, self.tok("admin"), self.revoke_body("bob"))["code"], "revoked")
        cid = "g" + secrets.token_hex(3)
        r, dt = self.call(lambda: self.submit(self.app, self.tok("admin"), {"type": "grant", "req": "r1", "target": "alice",
                                                                              "role": "propose", "client_id": cid, "idem": cid}))
        measure("h1_mirror_stalled_admin", dt)
        was = self.alive(sp)
        self.end(sp)
        self.assertEqual(r.get("code"), "granted", "%r" % (r,))
        self.assertFalse(was, "the stalled admin transaction was not evicted")
        self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = 'bob'", T),
                         "the stalled revoke was evicted, so it never committed")


class F3_FenceBeforeEviction(_Rev):
    def test_a_reconnecting_adversary_never_re_takes_the_lock(self):
        """Six connections of alice each hold her authority lock 10 s (past XWAIT, so the revoke must evict them) with a SPARE
        connection ready: the moment one is ended it calls explain on the spare. The fence must refuse those calls while the
        revocation evicts, so no call of alice answers with data after the first eviction, and the revoke applies within one round.
        Red at 2262eaa: the first round evicted without the fence, a spare re-took A(alice) and answered, and the revoke needed a
        second round."""
        tok = self.tok("alice")
        ap = os.environ["POL_PG_APP_PASSFILE"]
        ev = {"evicted_at": [], "answered_at": [], "gated": 0, "after": 0}
        ready = threading.Barrier(7)
        stop = threading.Event()

        def adversary(i):
            c = pgcore.connect_app(self.port, ap, "rev-adv-%d" % i)
            spare = pgcore.connect_app(self.port, ap, "rev-adv-spare-%d" % i)
            try:
                c.execute("begin")
                c.execute("set local idle_in_transaction_session_timeout = 0")
                c.execute("select dc.explain(%s, 'r1')", (tok,))
                ready.wait(10)
                try:
                    c.execute("select pg_sleep(10)")
                except psycopg.Error:
                    ev["evicted_at"].append(time.time())
                while not stop.is_set():
                    try:
                        r = spare.execute("select dc.explain(%s, 'r1')", (tok,)).fetchone()[0]
                        if r is not None:
                            ev["answered_at"].append(time.time())
                        else:
                            ev["after"] += 1
                        spare.execute("rollback") if spare.info.transaction_status else None
                    except psycopg.Error as e:
                        if e.sqlstate == "55P03":
                            ev["gated"] += 1
                        else:
                            break
                    time.sleep(0.002)
            finally:
                self._close(c)
                self._close(spare)
        ts = [threading.Thread(target=adversary, args=(i,), daemon=True) for i in range(6)]
        for t in ts:
            t.start()
        ready.wait(10)
        r, dt = self.revoke("alice")
        t_end = time.time()
        time.sleep(0.3)
        stop.set()
        for t in ts:
            t.join(15)
        self.o.execute("select pg_terminate_backend(pid, 5000) from pg_stat_activity where datname = current_database()"
                       " and application_name like 'rev-adv-%%'")
        measure("f3_reconnecting_adversary", dt)
        first = min(ev["evicted_at"]) if ev["evicted_at"] else None
        late = [round(a - first, 3) for a in ev["answered_at"] if first is not None and first <= a <= t_end]
        print("F3 adversary evicted=%d gated=%d answered_after_first_eviction=%r refused_after=%d dt=%.3f"
              % (len(ev["evicted_at"]), ev["gated"], late, ev["after"], dt), flush=True)
        self.assertEqual(r.get("code"), "revoked", "%r" % (r,))
        self.assertGreaterEqual(len(ev["evicted_at"]), 1, "vacuous: no adversary was evicted")
        self.assertEqual(late, [], "an adversary re-took alice's authority lock and answered after the first eviction")
        self.assertGreaterEqual(ev["gated"], 1, "vacuous: no call met the fence")
        self.assertLess(dt, XWAIT + 1.0 + EVICT_WAIT, "the revoke needed more than one round")
        self.assertEqual(self.submit(self.app, tok, self.body())["code"], UNAUTH)


class F8_AdvisoryAcl(_Rev):
    ROLE = "polfix_adv_tool"
    ADV = ("select p.oid::regprocedure::text f from pg_proc p where p.pronamespace = 'pg_catalog'::regnamespace"
           " and p.proname like 'pg\\_%%advisory%%' order by 1")

    def _acl(self):
        rows = self.o.execute(
            "select p.oid::regprocedure::text, coalesce(r.rolname, 'public'), a.is_grantable from pg_proc p"
            " cross join lateral aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a left join pg_roles r on r.oid = a.grantee"
            " where p.pronamespace = 'pg_catalog'::regnamespace and p.proname like 'pg\\_%%advisory%%' and a.privilege_type = 'EXECUTE'"
            " and a.grantee <> p.proowner").fetchall()
        return sorted(rows)

    def _drop(self):
        if self.o.execute("select 1 from pg_roles where rolname = %s", (self.ROLE,)).fetchone():
            self.o.execute(sql.SQL("drop owned by {}").format(sql.Identifier(self.ROLE)))
            self.o.execute(sql.SQL("drop role {}").format(sql.Identifier(self.ROLE)))

    def test_install_removes_explicit_advisory_grants(self):
        """Explicit EXECUTE on advisory-lock functions to dc_app, and to a role dc_app is a member of, before an install. Red at
        2262eaa: install revoked PUBLIC only, so dc_app could still take a raw advisory lock (hold A, T or a fence outside a call)."""
        self._drop()
        self.addCleanup(self._drop)
        self.addCleanup(self.o.execute, "revoke execute on function pg_advisory_xact_lock(int, int) from dc_app")   # red: install kept it
        self.o.execute("grant execute on function pg_advisory_xact_lock(int, int) to dc_app")
        self.o.execute(sql.SQL("create role {} nologin").format(sql.Identifier(self.ROLE)))
        self.o.execute(sql.SQL("grant execute on function pg_advisory_lock_shared(int, int) to {}").format(sql.Identifier(self.ROLE)))
        self.o.execute(sql.SQL("grant {} to dc_app").format(sql.Identifier(self.ROLE)))
        pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG},
                       idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])
        can = [f for (f,) in self.o.execute(self.ADV).fetchall()
               if self.o.execute("select has_function_privilege('dc_app', %s::regprocedure, 'EXECUTE')"
                                 " or has_function_privilege('dc_idp', %s::regprocedure, 'EXECUTE')", (f, f)).fetchone()[0]]
        self.assertEqual(can, [], "a login role of the API can still execute advisory-lock functions after install")
        c = self.conn("rev-raw-adv")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            c.execute("select pg_advisory_lock_shared(1145241601, 1)")

    def test_rollback_restores_the_recorded_acl(self):
        """A pre-R3-7 database whose advisory ACL is NOT PostgreSQL's default (one function granted to a tool role instead of PUBLIC).
        forward, then rollback, must leave the ACL exactly as it was. Red at 2262eaa: rollback granted PUBLIC everything and left
        dc_owner's grant."""
        import sys
        from ops import migrate_r3_7 as MIG
        from tests.test_review3_pg import _old_module
        self._drop()
        self.addCleanup(self._drop)
        sp = os.environ["POL_PG_SUPER_PASSFILE"]
        self.o.execute("drop schema if exists %s cascade" % MIG.PRE)
        self.o.execute("drop schema if exists dc_crypto cascade")
        OLD = _old_module()
        OLD.install(self.port, sp, os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG})
        self.addCleanup(sys.modules.pop, "pol.pgcore_5a80e52_t", None)
        self.o.execute(sql.SQL("create role {} nologin").format(sql.Identifier(self.ROLE)))
        for (f,) in self.o.execute(self.ADV).fetchall():          # PostgreSQL's default, then one deliberate exception
            self.o.execute(sql.SQL("revoke all on function {} from dc_owner").format(sql.SQL(f)))
            self.o.execute(sql.SQL("grant execute on function {} to public").format(sql.SQL(f)))
        self.o.execute("revoke execute on function pg_try_advisory_lock(int, int) from public")
        self.o.execute(sql.SQL("grant execute on function pg_try_advisory_lock(int, int) to {}").format(sql.Identifier(self.ROLE)))
        before = self._acl()
        MIG.forward(self.o, {T: CFG}, os.environ["POL_PG_IDP_PASSWORD_FILE"])
        self.assertNotEqual(self._acl(), before, "vacuous: forward did not change the ACL")
        MIG.rollback(self.o)
        self.assertEqual(self._acl(), before, "rollback did not restore the advisory ACL recorded before forward")


@unittest.skipUnless(psycopg is not None and all(os.environ.get(k) for k in ENV), "Postgres lab environment not configured")
class F2a_PreparedTransactions(unittest.TestCase):
    """NEG-5. Needs POL_PG_PREP_PORT (see the module docstring); FAILS, never skips, when it is missing."""

    def setUp(self):
        self.port = int(os.environ.get("POL_PG_PREP_PORT") or 0)
        if not self.port:
            self.fail("POL_PG_PREP_PORT is not set: F2a needs a server with max_prepared_transactions > 0")
        self.sp = os.environ["POL_PG_SUPER_PASSFILE"]
        self.o = pgcore._connect("postgres", self.sp, self.port, "rev-prep-owner")
        self.addCleanup(self.o.close)
        self.assertGreater(int(self.o.execute("show max_prepared_transactions").fetchone()[0]), 0, "vacuous: 2PC is off on this server")
        for (g,) in self.o.execute("select gid from pg_prepared_xacts where gid like 'polfix%%'").fetchall():
            self.o.execute("rollback prepared %s" % sql.Literal(g).as_string(self.o))

    def _install(self, path=None):
        old = pgcore._SQL_PATH
        pgcore._SQL_PATH = path or old
        try:
            pgcore.install(self.port, self.sp, os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG},
                           idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])
        finally:
            pgcore._SQL_PATH = old

    def test_a_prepared_transaction_cannot_hold_authority(self):
        """dc_app: begin; explain as alice; PREPARE TRANSACTION. Red at 2262eaa: the prepared transaction kept A(alice) shared with no
        backend, the revoke of alice gave up (55P03) and nothing could evict it. Fixed: the install refuses on this server; with the
        install's own check removed (as if 2PC were enabled after install), every call refuses, so no call's locks can be prepared."""
        refused = None
        try:
            self._install()
        except psycopg.Error as e:
            refused = e.diag.message_primary or str(e)
            src = open(pgcore._SQL_PATH, encoding="utf-8").read()
            start = src.index("-- Review of 2262eaa F2a: refuse to install")
            end = src.index("drop schema if exists dc cascade;")
            fd, tmp = tempfile.mkstemp(suffix=".sql")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(src[:start] + src[end:])
            self.addCleanup(os.unlink, tmp)
            self._install(tmp)
        ip, ap = os.environ["POL_PG_IDP_PASSFILE"], os.environ["POL_PG_APP_PASSFILE"]
        idp = pgcore.connect_idp(self.port, ip, "rev-prep-idp")
        app = pgcore.connect_app(self.port, ap, "rev-prep-app")
        att = pgcore.connect_app(self.port, ap, "rev-prep-attacker")
        for c in (idp, app, att):
            self.addCleanup(c.close)
        try:
            atok = pg_token(self.o, idp, T, "alice", "owner_ui")
            adm = pg_token(self.o, idp, T, "admin", "owner_ui")
            minted = True
        except psycopg.Error:
            minted = False                                       # every API call refuses, open_session included
            atok = adm = None
        prepared, call_err = False, None
        att.execute("begin")
        try:
            att.execute("select dc.explain(%s, 'r1')", (atok if minted else "not-a-token",))
            att.execute("prepare transaction 'polfix_pt'")
            prepared = True
        except psycopg.Error as e:
            call_err = e.sqlstate
            att.execute("rollback")
        held = self.o.execute("select count(*) from pg_locks l where l.locktype = 'advisory' and l.pid is null"     # no backend: prepared
                              " and l.database = (select oid from pg_database where datname = current_database())").fetchone()[0]
        rv = None
        if minted:
            try:
                rv = app.execute("select dc.submit(%s, %s)", (adm, Jsonb(
                    {"type": "revoke", "client_id": "rvp", "idem": "rvp", "target": "alice"}))).fetchone()[0]
            except psycopg.Error as e:
                rv = {"error": e.sqlstate}
        if prepared:
            self.o.execute("rollback prepared 'polfix_pt'")
        print("F2a install_refused=%r minted=%r call_error=%r prepared=%r advisory_locks_held_by_prepared=%d revoke=%r"
              % (refused, minted, call_err, prepared, held, rv), flush=True)
        self.assertEqual(held, 0, "a prepared transaction holds a call's advisory locks (its principal cannot be revoked): revoke %r"
                         % (rv,))
        self.assertFalse(prepared, "a transaction that made an API call was prepared")
        self.assertEqual(call_err, "55000", "the API call was not refused for max_prepared_transactions > 0")
        self.assertIsNotNone(refused, "install accepted a server with max_prepared_transactions > 0")
        self.assertIn("max_prepared_transactions", refused)


if __name__ == "__main__":
    unittest.main()
