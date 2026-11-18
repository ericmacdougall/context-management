"""Review of 6b77354 (research/policy-review-6b77354.md), Postgres findings N1-N4 and N6, reproduced first. Same environment as
tests/test_pg.py (paths to 0600 files only). Unless its docstring says "regression guard", "mutant killer" or "fix-only control",
each test runs UNMODIFIED against 6b77354 and fails there by a behavioural assertion. No sleep decides an outcome in the red tests:
every wait is a poll for an observed state (pg_blocking_pids, pg_locks). Red and green logs: policy/results/red/.

Invariant under test (FINAL-JUDGMENT.md 4.4 "Whom", as fixed): an exclusive request E on key k (A or T, or its fence) may end a backend
B only if B held that lock, granted, in the same transaction when E's request began and still holds it just before the signal, and B
is not a call in progress on k: B does not hold the marker M(k) (advisory class 1145241607, key k, taken by an API call before it
requests k exclusively and released on every exit of the call) together with k or its fence EXCLUSIVELY.

N1  A revocation in progress was evicted by a later exclusive request on the same key: the exemption read wait_event, which dc_evictor
    cannot see. States of the revoker driven here: parked holding A(target) exclusive (on the target's principal row, which the owner
    holds) and parked holding only the target's fence F_A (in its judgment's pins, on its own principal row, which the owner holds),
    with three triggers each: dc_idp's end_session, the owner's revoke_factor, a second admin's revoke. The sweeps (one holder at
    short offsets, the target timing her own logout) are supporting evidence only: they depend on timing.
N2  A refused revoke or grant whose refusal the pre-read did not predict took A and T exclusively and evicted a bystander. Every
    refusal the code can return for an admin's revoke or grant is listed in N2_RefusedShapes.CASES.
N3  An innocent submit that queued behind a revoke's tenant-lock request was granted the lock when that request timed out, committed,
    and its backend was then terminated.
N4  An admin parked an exclusive tenant lock in a wait on another tenant's lock and was exempt from eviction.
N6  No protocol case caught a core without the final judgment's session check (gate_noop) or without expiry (expiry_noop)."""
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

from tests.test_final_pg import UNAUTH
from tests.test_pg import CFG, LAB, T
from tests.test_rev2262_pg import TB, T_CLS, _Rev
from tests.test_s1_pg import XWAIT
from tests.test_s44_pg import BOUND, measure

A_CLS, FA_CLS, FT_CLS, MARK_CLS = 1145241601, 1145241605, 1145241606, 1145241607
PARK = XWAIT + 8.0               # how long a poll waits for a call to reach its parked state


class _N(_Rev):
    def reinstall(self):
        """A fresh install of the one-tenant configuration (the sweep's trials each start from it)."""
        pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG},
                       idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])

    def second_admin(self):
        """bob becomes an admin too (owner SQL, before any token is minted): the second admin of the N1 triggers."""
        self.o.execute("update dc.principal set admin = true where tenant = %s and pid = 'bob'", (T,))

    def own_row(self, tenant, pid):
        """The owner (superuser connection `hold`, never evicted) holds a principal row FOR UPDATE in an open transaction."""
        self.hold.execute("begin")
        self.hold.execute("select 1 from dc.principal where tenant = %s and pid = %s for update", (tenant, pid))
        self.addCleanup(self._swallow, lambda: self.hold.execute("rollback"))

    def holds(self, backend_pid, cls, key, mode=None):
        q = ("select count(*) from pg_locks where pid = %s and locktype = 'advisory' and objsubid = 2 and granted"
             " and classid = %s::bigint::oid and objid = (%s::bigint & 4294967295)::oid")
        a = [backend_pid, cls, key]
        if mode:
            q += " and mode = %s"
            a.append(mode)
        return self.count(q, *a) > 0

    def factor_of(self, tok):
        return self.count("select factor from dc.sessions where token_hash = %s", self.h(tok))

    def explain_holder(self, tok, name):
        c = self.conn(name)
        bp = self.pid(c)
        c.execute("begin")
        c.execute("set local idle_in_transaction_session_timeout = 0")
        c.execute("select dc.explain(%s, 'r1')", (tok,))
        return c, bp

    @staticmethod
    def code(out):
        r = out.get("r") if isinstance(out, dict) and "r" in out else out
        if isinstance(r, dict):
            return r.get("code") or r.get("error")
        return r


class N1_ParkedRevocation(_N):
    def _case(self, state, trigger):
        self.second_admin()
        atok, adm = self.tok("alice"), self.tok("admin")
        btok = self.tok("bob")
        fac = self.factor_of(atok)
        akey = self.key("auth", T, "alice")
        h = None
        if state == "holds_key":
            self.own_row(T, "alice")                      # the revoke parks in _lock_target, holding A(alice) exclusively
        else:
            _, h = self.explain_holder(atok, "n1-alice-holder")   # the revoke must evict her: it times out, takes F_A(alice)...
            self.own_row(T, "admin")                      # ...and parks in its judgment's pins, on its own principal row
        rc = self.conn("n1-revoker")
        rp = self.pid(rc)
        rt, ro = self.start(lambda: self.submit(rc, adm, self.revoke_body("alice")))
        self.assertTrue(self.blocked_by(rp, self.pid(self.hold), timeout=PARK), "the revoke did not park on the owner's row")
        if state == "holds_key":
            self.assertTrue(self.holds(rp, A_CLS, akey, "ExclusiveLock"), "positive control: the parked revoke holds A(alice) exclusively")
        else:
            self.assertTrue(self.holds(rp, FA_CLS, akey, "ExclusiveLock"), "positive control: the parked revoke holds F_A(alice)")
            self.assertFalse(self.holds(rp, A_CLS, akey), "positive control: the parked revoke does not hold A(alice) yet")
        if trigger == "end_session":
            ic = self.conn("n1-idp", idp=True)
            fn = lambda: ic.execute("select dc.end_session(%s)", (atok,)).fetchone()[0]          # noqa: E731
        elif trigger == "revoke_factor":
            fn = lambda: self.mut_o.execute("select dc.revoke_factor(%s)", (fac,)).fetchone()[0]  # noqa: E731
        else:
            sc = self.conn("n1-second-admin")
            fn = lambda: self.submit(sc, btok, self.revoke_body("alice"))                       # noqa: E731
        t0 = time.time()
        tt, to = self.start(fn)
        tt.join(4 * XWAIT + 10)
        measure("n1_%s_%s_trigger" % (state, trigger), time.time() - t0)
        was = self.alive(rp)
        self.hold.execute("rollback")         # (on 6b77354 a second admin's revoke, having evicted the revoker, waits for this row)
        rt.join(4 * XWAIT + 10)
        tt.join(4 * XWAIT + 10)
        self.assertFalse(tt.is_alive() or rt.is_alive(), "stuck")
        print("N1 %s/%s revoke=%r trigger=%r revoker_alive=%s" % (state, trigger, self.code(ro), self.code(to), was), flush=True)
        self.assertTrue(was, "the %s ended the revocation in progress (%s)" % (trigger, state))
        self.assertEqual(self.code(ro), "revoked", "%r" % (ro,))
        self.assertTrue(self.count("select revoked from dc.principal where tenant = %s and pid = 'alice'", T))
        self.assertEqual(self.code(to), "55P03", "the trigger must give up (retryable), not evict: %r" % (to,))
        if h is not None:
            self.assertFalse(self.alive(h), "positive control: the revoke evicted alice's stalled holder")

    def test_holds_key_end_session(self):
        self._case("holds_key", "end_session")

    def test_holds_key_revoke_factor(self):
        self._case("holds_key", "revoke_factor")

    def test_holds_key_second_admin_revoke(self):
        self._case("holds_key", "second_revoke")

    def test_fence_only_end_session(self):
        self._case("fence_only", "end_session")

    def test_fence_only_revoke_factor(self):
        self._case("fence_only", "revoke_factor")

    def test_fence_only_second_admin_revoke(self):
        self._case("fence_only", "second_revoke")


class N1_Sweep(_N):
    """Supporting evidence (timing decides whether 6b77354 fails; the fix must never lose a revocation): the review's probe H with one
    stalled tenant-lock holder at short offsets, with six, and the target-driven variant (alice keeps four holders of her own authority
    lock, which the revoke must evict first, and logs out at an offset of her choosing while bob's six submits stall the tenant lock)."""

    def _trial(self, k, d, alice_holders=0):
        self.reinstall()
        if True:
            atok, btok, adm = self.tok("alice"), self.tok("bob"), self.tok("admin")
            hs = []
            for i in range(k):
                c = self.conn("n1s-bob-%d" % i)
                c.execute("begin")
                c.execute("set local idle_in_transaction_session_timeout = 0")
                hs.append(self.pid(c))
                if i == 0:
                    self.assertEqual(self.submit(c, btok, self.body(None, "held"))["code"], "proposed")
                else:
                    self.start(lambda c=c: self.submit(c, btok, self.body(None, "queued")))
            for i in range(alice_holders):
                hs.append(self.explain_holder(atok, "n1s-alice-%d" % i)[1])
            for p in hs[1:k]:
                self.assertTrue(self.blocked_by(p, hs[0], timeout=5), "a queued holder did not queue")
            rc = self.conn("n1s-revoker")
            rp = self.pid(rc)
            rt, ro = self.start(lambda: self.submit(rc, adm, self.revoke_body("alice")))
            time.sleep(d)
            ic = self.conn("n1s-idp", idp=True)
            et, eo = self.start(lambda: ic.execute("select dc.end_session(%s)", (atok,)).fetchone()[0])
            rt.join(30)
            et.join(30)
            out = (k, d, alice_holders, self.code(ro), self.code(eo), self.alive(rp),
                   bool(self.count("select revoked from dc.principal where tenant = %s and pid = 'alice'", T)))
            print("N1S k=%d d=%.2f alice_holders=%d revoke=%r end_session=%r revoker_alive=%s alice_revoked=%s" % out, flush=True)
            self.end(*[p for p in hs + [rp] if self.alive(p)])
            return out

    def test_sweep(self):
        outs = [self._trial(1, d) for d in (0.0, 0.03, 0.06, 0.1)]
        outs += [self._trial(6, d) for d in (0.05, 0.2)]
        outs += [self._trial(6, d, alice_holders=4) for d in (0.4, 0.7)]
        lost = [o for o in outs if not (o[5] and o[6] and o[3] == "revoked")]
        print("N1S SUMMARY trials=%d lost=%d" % (len(outs), len(lost)), flush=True)
        self.assertEqual(lost, [], "a revocation in progress was evicted")


class N1_ModeRule(_N):
    """Regression guard and mutant killer (green at 6b77354, whose eviction never spared a shared holder): only an EXCLUSIVE holder of
    the key is spared by its marker. The admin opens a transaction, reads (A(admin) shared), and then loops a self-revoke under a
    savepoint with lock_timeout just under XWAIT, which never reaches its eviction, while an owner connection holds A(admin) shared
    (never evicted, so the self-revoke never gets the lock): the looping backend is marked almost all the time but holds A(admin)
    only shared. dc_idp's end_session of its token must still evict it (a core that spared marked shared holders would leave it
    holding the admin's authority lock; end_session itself then gives up because of the owner's hold)."""

    def test_a_marked_shared_holder_is_evicted(self):
        adm = self.tok("admin")
        akey = self.key("auth", T, "admin")
        self.hold.execute("begin")                     # an owner holder of A(admin) shared: never evicted, so the self-revoke never
        self.hold.execute("select pg_advisory_xact_lock_shared(%s::int, %s::int)", (A_CLS, akey))    # gets A(admin) exclusively
        self.addCleanup(self._swallow, lambda: self.hold.execute("rollback"))
        lc = self.conn("n1m-looper")
        lp = self.pid(lc)
        lc.execute("begin")
        lc.execute("set local idle_in_transaction_session_timeout = 0")
        lc.execute("set local lock_timeout = '1900ms'")
        lc.execute("select dc.explain(%s, 'r1')", (adm,))
        stop = threading.Event()
        n = [0]
        errs = []

        def loop():
            while not stop.is_set():
                try:
                    lc.execute("savepoint s")
                    errs.append(self.code(self.submit(lc, adm, self.revoke_body("admin"))))
                except psycopg.Error as e:
                    errs.append((e.sqlstate, e.diag.message_primary))
                try:
                    lc.execute("rollback to savepoint s")
                except psycopg.Error:
                    return
                n[0] += 1
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self._threads.append(t)
        waiting = False
        for _ in range(100):
            if self.count("select count(*) from pg_locks where pid = %s and locktype = 'advisory' and not granted and mode = 'ExclusiveLock'"
                          " and classid = %s::bigint::oid and objid = %s::bigint::oid", lp, A_CLS, akey):
                waiting = True
                break
            time.sleep(0.02)
        self.assertTrue(waiting, "positive control: the looping self-revoke waits for A(admin) exclusively: %r" % (errs[:3],))
        r, dt = self.call(lambda: self.idp.execute("select dc.end_session(%s)", (adm,)).fetchone()[0])
        stop.set()
        measure("n1_mode_rule", dt)
        was = self.alive(lp)
        t.join(10)
        self.end(lp)
        print("N1M end_session=%r in %.2fs looper_alive=%s loops=%d first=%r" % (r, dt, was, n[0], errs[:2]), flush=True)
        self.assertNotIn("revoked", errs, "the self-revoke applied: the control is void")
        self.assertFalse(was, "a marked backend that holds the key only SHARED was spared")


class N1_LeakBound(_N):
    """Fix-only control (needs dc._unmark; at 6b77354 it errors): with dc._unmark patched to a no-op, an admin's applied grant leaves
    its marker on T, so in an open transaction it is a marked exclusive holder of T: another admin's revoke of bob gives up (55P03,
    the tenant's authority changes are frozen), and the revoke of that admin still evicts it (its call held A(admin) shared, which no
    marker spares). This is the bound FINAL-JUDGMENT.md 4.4 states for a leaked marker."""

    def test_a_leaked_marker_is_bounded_by_revoking_its_holder(self):
        self.second_admin()
        adm, btok = self.tok("admin"), self.tok("bob")
        self.o.execute("create or replace function dc._unmark() returns void language sql as $$ select $$")
        lc = self.conn("n1l-leaker")
        lp = self.pid(lc)
        lc.execute("begin")
        lc.execute("set local idle_in_transaction_session_timeout = 0")
        cid = "g" + secrets.token_hex(3)
        g = self.submit(lc, adm, {"type": "grant", "req": "r1", "target": "alice", "role": "propose", "client_id": cid, "idem": cid})
        self.assertEqual(g.get("code"), "granted")
        self.assertTrue(self.holds(lp, MARK_CLS, self.key("tenant", T)), "positive control: the patched core leaked the marker")
        r1, d1 = self.call(lambda: self.submit(self.app, btok, self.revoke_body("x")))
        self.assertTrue(self.alive(lp))
        r2, d2 = self.call(lambda: self.submit(self.app, btok, self.revoke_body("admin")))
        was = self.alive(lp)
        self.end(lp)
        print("N1L revoke_x=%r (%.2fs) revoke_admin=%r (%.2fs) leaker_alive=%s" % (r1, d1, r2, d2, was), flush=True)
        self.assertEqual(r1.get("error"), "55P03", "%r" % (r1,))
        self.assertEqual(r2.get("code"), "revoked", "%r" % (r2,))
        self.assertFalse(was, "revoking the leaking admin did not evict it")


class N2_RefusedShapes(_N):
    """N2. Every refusal dc.submit can return for an admin's revoke or grant whose rules facts the 6b77354 pre-read accepted (target
    x exists; requirement r1 exists and target alice is unrevoked). Enumerated from the code, not from the review:
      dc._submit_stage: identity_in_body; bad_event for an unknown key, a missing or malformed client_id, a value that is not a string
        or has no label, a label _label_norm refuses, a label without a value; bad_subject (answered bad_event) for a non-canonical
        subject. (A body that is not an object has no type, so it never was an authority change.)
      dc._reduce_stage: bad_event when dc._shape refuses (missing or empty idem, malformed based_on, grant role not decide or propose,
        role not a string); duplicate and idem_conflict (an events row with the same idem); tenant_mismatch cannot happen (the stage
        sets the tenant).
      dc._rules: unauthorized (not an admin: the pre-read never asked for an exclusive lock), principal_revoked (refused by the pins,
        which come before any eviction: tests.test_s44_pg.S44_Scope.test_a_refused_revoker_evicts_nobody), bad_event for a missing
        revoke target or a missing requirement or missing/revoked grant target (tests.test_rev2262_pg.F5_RefusedAuthorityChange).
    Red at 6b77354: each case took T (and, for a revoke, A(x)) exclusively, so it waited XWAIT behind the owner's shared hold and gave
    up (55P03), and evicted bob's stalled submit."""

    CASES = {
        "identity_in_body": ("revoke", {"tenant": T}, (), "identity_in_body"),
        "unknown_key": ("revoke", {"foo": 1}, (), "bad_event"),
        "missing_client_id": ("revoke", {}, ("client_id",), "bad_event"),
        "bad_client_id": ("revoke", {"client_id": "bad id!"}, (), "bad_event"),
        "value_not_string": ("revoke", {"value": 1, "label": LAB}, (), "bad_event"),
        "value_without_label": ("revoke", {"value": "v"}, (), "bad_event"),
        "label_refused": ("revoke", {"value": "v", "label": {"tenants": [T]}}, (), "bad_event"),
        "non_canonical_subject": ("revoke", {"value": "v", "label": dict(LAB, subjects=["Dana"])}, (), "bad_event"),
        "label_without_value": ("revoke", {"label": LAB}, (), "bad_event"),
        "missing_idem": ("revoke", {}, ("idem",), "bad_event"),
        "empty_idem": ("revoke", {"idem": ""}, (), "bad_event"),
        "bad_based_on": ("revoke", {"based_on": "x"}, (), "bad_event"),
        "grant_bad_role": ("grant", {"role": "owner"}, (), "bad_event"),
        "grant_role_not_string": ("grant", {"role": 1}, (), "bad_event"),
        "grant_duplicate": ("grant", "dup", (), "duplicate"),
        "grant_idem_conflict": ("grant", "conflict", (), "idem_conflict"),
    }

    def _bodies(self, adm):
        cid = "g" + secrets.token_hex(3)
        applied = {"type": "grant", "req": "r1", "target": "alice", "role": "propose", "client_id": cid, "idem": cid}
        self.assertEqual(self.submit(self.app, adm, dict(applied))["code"], "granted", "positive control: the valid grant applies")
        out = {}
        for name, (typ, extra, drop, want) in self.CASES.items():
            c = "c" + secrets.token_hex(3)
            if extra == "dup":
                b = dict(applied)
            elif extra == "conflict":
                b = dict(applied, role="decide")
            else:
                base = ({"type": "revoke", "target": "x"} if typ == "revoke"
                        else {"type": "grant", "req": "r1", "target": "alice", "role": "propose"})
                b = dict(base, client_id=c, idem=c)
                b.update(extra)
                for k in drop:
                    b.pop(k)
            out[name] = (b, want)
        return out

    def test_refused_shapes_never_wait_for_the_tenant_lock(self):
        adm = self.tok("admin", ttl=120)
        bodies = self._bodies(adm)
        self.hold.execute("begin")
        self.hold.execute("select pg_advisory_xact_lock_shared(%s::int, %s::int)", (T_CLS, self.key("tenant", T)))
        try:
            for name, (b, want) in bodies.items():
                with self.subTest(name):
                    r, dt = self.call(lambda: self.submit(self.app, adm, b))
                    measure("n2_owner_t_" + name, dt)
                    self.assertEqual(r.get("code"), want, "%r" % (r,))
                    self.assertLess(dt, XWAIT, "a refused authority change waited for the tenant lock")
        finally:
            self.hold.execute("rollback")
        self.assertFalse(self.count("select revoked from dc.principal where tenant = %s and pid = 'x'", T), "x revoked by a refused body")

    def test_refused_shapes_evict_nobody(self):
        """bob's submit holds T shared and the chain head; each refused change (a 4 s admin token) answers its refusal or waits at the
        chain head and is refused at expiry, and bob must survive every one."""
        bodies = self._bodies(self.tok("admin", ttl=120))
        for name, (b, want) in bodies.items():
            with self.subTest(name):
                c, bp, _ = self.holder("bob", "submit")
                tok = self.tok("admin", ttl=4)
                r, dt = self.call(lambda: self.submit(self.app, tok, b))
                measure("n2_stalled_submitter_" + name, dt)
                was = self.alive(bp)
                self.end(bp)
                self.assertTrue(was, "a refused %s evicted bob's in-flight submit: %r" % (name, r))
                self.assertIn(r.get("code"), (want, UNAUTH), "%r" % (r,))


class N3_InnocentWaiter(_N):
    def test_a_submit_queued_behind_a_revoke_survives(self):
        """N3. bob's submit is stalled (T shared, chain head). The admin revokes x; once that revoke waits for T, alice submits on r2
        and queues behind it. When the revoke's request times out alice is granted T, waits for the chain head, and commits after
        bob is evicted. Red at 6b77354: the eviction query took every ShareLock row, alice's too, and her backend was terminated."""
        c, bp, _ = self.holder("bob", "submit")
        adm, atok = self.tok("admin", ttl=120), self.tok("alice", ttl=120)
        rc, ac = self.conn("n3-revoker"), self.conn("n3-innocent")
        rp, ap = self.pid(rc), self.pid(ac)
        rt, ro = self.start(lambda: self.submit(rc, adm, self.revoke_body("x")))
        self.assertTrue(self.blocked_by(rp, bp, timeout=PARK), "the revoke did not wait for T")
        it, io = self.start(lambda: self.submit(ac, atok, self.body(None, "innocent", req="r2")))
        self.assertTrue(self.blocked_by(ap, rp, timeout=PARK), "alice did not queue behind the revoke")
        rt.join(4 * XWAIT + 10)
        it.join(4 * XWAIT + 10)
        was = self.alive(ap)
        print("N3 revoke=%r innocent=%r innocent_alive=%s bob_alive=%s" % (self.code(ro), self.code(io), was, self.alive(bp)), flush=True)
        self.assertEqual(self.code(ro), "revoked", "%r" % (ro,))
        self.assertEqual(self.code(io), "proposed", "%r" % (io,))
        self.assertTrue(was, "the innocent submit's backend was terminated")
        self.assertFalse(self.alive(bp), "positive control: bob's stalled submit was evicted")

    def test_a_pooled_connection_is_judged_by_its_transaction(self):
        """N3, the snapshot is per transaction, not per backend. The owner holds T shared (never evicted); a pooled connection P holds
        T in transaction 1 when the admin's revoke of x begins to wait, then commits and makes a second, short submit, which queues
        behind the revoke and runs when the revoke's request times out. P must not be ended: it never held T for XWAIT. The revoke
        gives up (the owner never lets go). Red at 6b77354 (P's ShareLock row was taken; timing-dependent but seen every run)."""
        self.hold.execute("begin")
        self.hold.execute("select pg_advisory_xact_lock_shared(%s::int, %s::int)", (T_CLS, self.key("tenant", T)))
        self.addCleanup(self._swallow, lambda: self.hold.execute("rollback"))
        atok, adm = self.tok("alice", ttl=120), self.tok("admin", ttl=120)
        pc = self.conn("n3-pooled")
        pp = self.pid(pc)
        pc.execute("begin")
        pc.execute("set local idle_in_transaction_session_timeout = 0")
        self.assertEqual(self.submit(pc, atok, self.body(None, "tx1", req="r2"))["code"], "proposed")
        rc = self.conn("n3b-revoker")
        rp = self.pid(rc)
        rt, ro = self.start(lambda: self.submit(rc, adm, self.revoke_body("x")))
        self.assertTrue(self.blocked_by(rp, pp, timeout=PARK), "the revoke did not wait for T")
        pc.execute("commit")
        pt, po = self.start(lambda: self.submit(pc, atok, self.body(None, "tx2", req="r2")))
        self.assertTrue(self.blocked_by(pp, rp, timeout=PARK), "the second submit did not queue behind the revoke")
        rt.join(4 * XWAIT + 10)
        pt.join(4 * XWAIT + 10)
        was = self.alive(pp)
        print("N3B revoke=%r second=%r pooled_alive=%s" % (self.code(ro), self.code(po), was), flush=True)
        self.assertEqual(self.code(po), "proposed", "%r" % (po,))
        self.assertTrue(was, "a pooled connection that never held T for XWAIT was ended")
        self.assertEqual(self.code(ro), "55P03", "%r" % (ro,))


class N4_ParkedExclusive(_N):
    def test_a_holder_parked_on_another_tenant_is_evicted(self):
        """N4 (the review's probe Z, with polls). B0: tenant-b's admin grant applied, transaction open (T(b) exclusive). B1, one
        transaction: tenant-a's admin grant applied (T(a) exclusive), then a tenant-b submit that waits for T(b). Tenant-a's revoke of
        bob must evict B1. Red at 6b77354: B1 was 'waiting on an ungranted T lock', exempt; the revoke gave up (55P03)."""
        self.tenants()
        adm_a, adm_b, bob_b = self.token(T, "admin"), self.token(TB, "admin"), self.token(TB, "bob")

        def g():
            cid = "g" + secrets.token_hex(3)
            return {"type": "grant", "req": "r1", "target": "alice", "role": "propose", "client_id": cid, "idem": cid}
        b0 = self.conn("n4-b0")
        b0p = self.pid(b0)
        b0.execute("begin")
        b0.execute("set local idle_in_transaction_session_timeout = 0")
        self.assertEqual(self.submit(b0, adm_b, g())["code"], "granted")
        b1 = self.conn("n4-b1")
        b1p = self.pid(b1)
        b1.execute("begin")
        b1.execute("set local idle_in_transaction_session_timeout = 0")
        self.assertEqual(self.submit(b1, adm_a, g())["code"], "granted")
        pt, _ = self.start(lambda: self.submit(b1, bob_b, self.body_b("parked", [])))
        self.assertTrue(self.blocked_by(b1p, b0p, timeout=PARK), "B1 did not park on T(b)")
        r, dt = self.call(lambda: self.submit(self.app, self.token(T, "admin"), self.revoke_body("bob")))
        measure("n4_parked_on_other_tenant", dt)
        was = self.alive(b1p)
        b0.execute("rollback")
        pt.join(15)
        self.end(b1p)
        print("N4 revoke=%r in %.2fs b1_alive=%s" % (r, dt, was), flush=True)
        self.assertEqual(r.get("code"), "revoked", "%r" % (r,))
        self.assertFalse(was, "the parked exclusive holder was not evicted")
        self.assertLess(dt, BOUND)

    def test_a_marker_for_another_key_spares_nothing(self):
        """Mutant killer (green at 6b77354, which did not see a row-lock wait as 'inside'): B1 holds T(a) exclusively from an applied
        grant, then in the same transaction runs tenant-b's admin revoke of bob, parked on bob's tenant-b principal row (the owner
        holds it), so B1 is marked for A(bob in b) and holds A(bob in b) exclusively, but not for T(a). Tenant-a's revoke of bob must
        evict B1: the marker spares only the key it names."""
        self.tenants()
        adm_a, adm_b = self.token(T, "admin"), self.token(TB, "admin")
        b1 = self.conn("n4b-b1")
        b1p = self.pid(b1)
        b1.execute("begin")
        b1.execute("set local idle_in_transaction_session_timeout = 0")
        cid = "g" + secrets.token_hex(3)
        self.assertEqual(self.submit(b1, adm_a, {"type": "grant", "req": "r1", "target": "alice", "role": "propose", "client_id": cid,
                                                  "idem": cid})["code"], "granted")
        self.own_row(TB, "bob")
        pt, _ = self.start(lambda: self.submit(b1, adm_b, self.revoke_body("bob")))
        self.assertTrue(self.blocked_by(b1p, self.pid(self.hold), timeout=PARK), "B1 did not park on bob's tenant-b row")
        r, dt = self.call(lambda: self.submit(self.app, self.token(T, "admin"), self.revoke_body("bob")))
        was = self.alive(b1p)
        self.hold.execute("rollback")
        pt.join(15)
        self.end(b1p)
        print("N4B revoke=%r in %.2fs b1_alive=%s" % (r, dt, was), flush=True)
        self.assertEqual(r.get("code"), "revoked", "%r" % (r,))
        self.assertFalse(was, "a backend marked for another key was spared")


class N6_JudgmentAtTheGate(_N):
    """Regression guard and mutant killer (green at 6b77354): the token expires between the call's last lock and its final judgment.
    A test-only trigger on dc.events sleeps past the token's expiry inside the stage (the value event with idem n6-sleep), so only the
    final judgment dc._gate (dc._session, wall clock) can refuse it. The submit must answer unauthenticated and leave no event. Kills
    gate_noop (the session check of dc._gate removed) and expiry_noop (expiry judged nowhere)."""

    def test_expiry_inside_the_call_is_refused_at_the_gate(self):
        self.o.execute("create function dc.n6_sleep() returns trigger language plpgsql as $$ begin"
                       " if new.idem = 'n6-sleep' then perform pg_sleep(3); end if; return new; end $$")
        self.o.execute("create trigger n6_sleep before insert on dc.events for each row execute function dc.n6_sleep()")
        tok = self.tok("alice", ttl=2)
        r, dt = self.call(lambda: self.submit(self.app, tok, self.body(None, "late", idem="n6-sleep", req="r2")))
        n = self.count("select count(*) from dc.events where tenant = %s and idem = 'n6-sleep'", T)
        print("N6 submit=%r in %.2fs events=%d" % (r, dt, n), flush=True)
        self.assertGreater(dt, 2.0, "positive control: the call outlived its token")
        self.assertEqual(r.get("code"), UNAUTH, "%r" % (r,))
        self.assertEqual(n, 0, "an event was applied after the token expired")


class N7_RollbackMark(_N):
    """N7. ops/migrate_r3_7.rollback restores the advisory ACL from dc.migration_mark, a table of schema dc that dc_owner can rewrite,
    and ran each function signature from it as raw SQL text (sql.SQL(f)) as superuser: an owner-to-superuser injection. A signature
    list "pg_advisory_lock(bigint), pg_read_file(text)" turned the restore's grant into a grant of pg_read_file to PUBLIC. Red at
    6b77354: the rollback ran and PUBLIC could read server files. Fixed: a mark that names anything but a live advisory-lock function
    is refused (MigrationRefused) and nothing is changed."""
    EVIL = "pg_catalog.pg_advisory_lock(bigint), pg_catalog.pg_read_file(text)"

    def _public_can_read_files(self):
        return self.count("select has_function_privilege('public', 'pg_catalog.pg_read_file(text)', 'EXECUTE')")

    def test_a_tampered_mark_is_refused(self):
        import sys
        from ops import migrate_r3_7 as MIG
        from tests.test_review3_pg import _old_module
        self.addCleanup(self._swallow, lambda: self.o.execute("drop schema if exists %s cascade" % MIG.PRE))
        self.addCleanup(self._swallow, lambda: self.o.execute("revoke execute on function pg_catalog.pg_read_file(text) from public"))
        self.assertFalse(self._public_can_read_files(), "precondition: PUBLIC cannot execute pg_read_file")
        sp = os.environ["POL_PG_SUPER_PASSFILE"]
        self.o.execute("drop schema if exists %s cascade" % MIG.PRE)
        self.o.execute("drop schema if exists dc_crypto cascade")
        _old_module().install(self.port, sp, os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG})
        self.addCleanup(sys.modules.pop, "pol.pgcore_5a80e52_t", None)
        MIG.forward(self.o, {T: CFG}, os.environ["POL_PG_IDP_PASSWORD_FILE"])
        self.o.execute("update dc.migration_mark set advisory_acl = advisory_acl || jsonb_build_object(%s::text, '[[\"public\", false]]'::jsonb)",
                       (self.EVIL,))
        refused = None
        try:
            MIG.rollback(self.o)
        except MIG.MigrationRefused as e:
            refused = str(e)
        except psycopg.Error as e:
            refused = "error %s" % e.sqlstate
        leaked = self._public_can_read_files()
        print("N7 rollback refused=%r public_can_read_files=%s" % (refused, leaked), flush=True)
        self.assertFalse(leaked, "the rollback granted a superuser-only function to PUBLIC from the mark's text")
        self.assertIsNotNone(refused, "the rollback accepted a mark that names a non-advisory function")


if __name__ == "__main__":
    unittest.main()
