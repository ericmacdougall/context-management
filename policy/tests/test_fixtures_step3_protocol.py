"""Step-3 protocol fixtures: the authorisation and lock layer (S1 pins, FINAL-JUDGMENT.md 4.4 eviction and fences, the exact lock
identities of the review of 2262eaa), which the B1/B2 replay of tests/test_fixtures_step3_set.py cannot see (it replays the rules core
only). Same environment as tests/test_pg.py; it FAILS, never skips, when that environment is missing, unless POL_STEP3_SKIP_PG=1 is set
explicitly (then each test is SKIPPED with that reason, and counted as a skip).

PROTOCOL names, for each protocol property, the test cases the port must run green against its own schema (the same cases, with the
port's install). MUTANTS are literal patches of pol/_pgcore.sql that break exactly one layer; each must make at least one of its
protocol cases FAIL (an assertion, not an error), so a green protocol run is not vacuous:
  pins_noop      dc._pin takes no FOR SHARE: a revocation lands while an in-flight call waits (S1).
  fence_noop     dc._fenced never refuses: the target's calls are answered while its revocation evicts (4.4 fence).
  evict_noop     dc._evict signals nobody: a stalled holder blocks every revocation (4.4).
  mark_noop      the marker M(k) of a call in progress is never taken: a later end_session ends a parked revocation (review of
                 6b77354 N1). It replaces the in_progress mutant of 2c56338, whose wait_event clause was dead code (dc_evictor
                 cannot read wait_event), and the reviewer's bt_clause_only, whose anchor no longer exists.
  unmark_noop    the marker is never released: an applied revoke left open stays spared and blocks the next exclusive request.
  mark_any_key   the marker spares a holder whatever key it names: an admin parked on another tenant freezes its own T (N4).
  mark_any_mode  the marker spares a holder that has k only SHARED (N1 mode rule).
  snap_noop      eviction ignores the snapshot taken before the request: a submit that queued behind a revoke is ended (N3).
  snap_pid_only  the snapshot is matched by backend, not by transaction: a pooled connection's later transaction is ended (N3).
  collapsed_key  every principal shares one authority key (dc._ak = 1): a revoke in one tenant evicts and fences another (F1).
  refused_xlock  a grant takes the tenant lock exclusively whatever the rules will say (F5).
  body_preread   the pre-read skips the stage's body checks: a malformed grant or revoke takes T exclusively (N2).
  gate_noop      the final judgment dc._gate's session check removed: a token that expires inside the call is accepted (N6).
                 Caught by no case before the review of 6b77354 (disclosed then).
  expiry_noop    the authentication layer: session expiry is judged nowhere (dc._session and the entry guards of dc._submit,
                 dc._explain, dc._erase_candidates and dc._erase_values), so an expired token is accepted. History: the first
                 version (f9b4e63) patched dc._session alone; it RAN and was NOT caught (the entry guards still refused the expired
                 token); this version removes every check.
Layers these cases do NOT cover (disclosed): the S (subject) and N (nonce) lock keys; the L4 idempotency lock; the lock order
(tests.test_s1_pg.S1_LockOrder checks it, not this file); deadlocks; the 2PC refusal (F2a); the member-role eviction scope (F2b) and
the evictor's privileges; the advisory ACL and its rollback (F8, N7); the window between eviction's re-check and its signal; N1 timing
variants other than the two parked states (tests.test_rev6b77_pg.N1_Sweep is supporting evidence only); a leaked marker (residual 5,
tests.test_rev6b77_pg.N1_LeakBound)."""
import os
import tempfile
import unittest

try:
    import psycopg  # noqa: F401
    from pol import pgcore
except ImportError:              # pragma: no cover
    psycopg = None

from tests.test_pg import ENV

PROTOCOL = {
    "pins": ["tests.test_s1_pg.S1_PinnedAuthority.test_head_row"],
    "fence": ["tests.test_s44_pg.S44_Fence.test_the_authority_fence_refuses_the_target_only"],
    "evict": ["tests.test_s44_pg.S44_Eviction.test_revoke_evicts_the_target_holder"],
    "in_progress": ["tests.test_rev6b77_pg.N1_ParkedRevocation.test_holds_key_end_session",
                    "tests.test_rev6b77_pg.N1_ParkedRevocation.test_fence_only_end_session",
                    "tests.test_rev2262_pg.H1_RevocationInProgress.test_a_stalled_applied_revoke_is_still_evicted",
                    "tests.test_rev6b77_pg.N1_ModeRule.test_a_marked_shared_holder_is_evicted",
                    "tests.test_rev6b77_pg.N4_ParkedExclusive.test_a_holder_parked_on_another_tenant_is_evicted",
                    "tests.test_rev6b77_pg.N4_ParkedExclusive.test_a_marker_for_another_key_spares_nothing"],
    "snapshot": ["tests.test_rev6b77_pg.N3_InnocentWaiter.test_a_submit_queued_behind_a_revoke_survives",
                 "tests.test_rev6b77_pg.N3_InnocentWaiter.test_a_pooled_connection_is_judged_by_its_transaction"],
    "identity": ["tests.test_rev2262_pg.F1_CrossTenant.test_colliding_principals_never_cross_evict_or_cross_fence",
                 "tests.test_rev2262_pg.F1_CrossTenant.test_a_nonexistent_colliding_target_evicts_nobody"],
    "refused": ["tests.test_rev2262_pg.F5_RefusedAuthorityChange.test_refused_changes_never_wait_for_the_tenant_lock",
                "tests.test_rev6b77_pg.N2_RefusedShapes.test_refused_shapes_never_wait_for_the_tenant_lock"],
    "auth": ["tests.test_step2_pg.Step2Pg.test_bad_tokens_are_refused_on_every_call",
             "tests.test_rev6b77_pg.N6_JudgmentAtTheGate.test_expiry_inside_the_call_is_refused_at_the_gate"],
}
MUTANTS = {
    "pins_noop": ("pins", [
        ("  select x.revoked into sv from dc.sessions x where x.token_hash = s.token_hash for share;\n",
         "  select x.revoked into sv from dc.sessions x where x.token_hash = s.token_hash;\n"),
        ("  select f.revoked into fv from dc.factors f where f.factor = s.factor for share;\n",
         "  select f.revoked into fv from dc.factors f where f.factor = s.factor;\n"),
        ("  select p.* into pr from dc.principal p where p.tenant = s.tenant and p.pid = s.principal for share;\n"
         "  if sv is distinct from false",
         "  select p.* into pr from dc.principal p where p.tenant = s.tenant and p.pid = s.principal;\n"
         "  if sv is distinct from false")]),
    "fence_noop": ("fence", [("    raise exception 'a revocation of this authority is in progress' using errcode = 'lock_not_available';\n",
                              "    null;\n")]),
    "evict_noop": ("evict", [("    perform pg_terminate_backend(r.pid, 1000);\n", "    null;\n")]),
    "mark_noop": ("in_progress", [("    perform pg_advisory_lock_shared(1145241607, p_key);\n", "    null;\n")]),
    "unmark_noop": ("in_progress", [("    perform pg_advisory_unlock_shared(1145241607, r.objid::text::bigint::int);\n", "    null;\n")]),
    "mark_any_key": ("in_progress", [("m.database = db\n                           and m.classid = 1145241607::oid and m.objid = ko)\n",
                                      "m.database = db\n                           and m.classid = 1145241607::oid)\n")]),
    "mark_any_mode": ("in_progress", [("                               and x.mode = 'ExclusiveLock' and x.objid = ko\n",
                                       "                               and x.objid = ko\n")]),
    "snap_noop": ("snapshot", [("              and (l.pid::text || ':' || l.virtualtransaction) = any(coalesce(p_snap, array[]::text[]))\n",
                                "")]),
    "snap_pid_only": ("snapshot", [
        ("              and (l.pid::text || ':' || l.virtualtransaction) = any(coalesce(p_snap, array[]::text[]))\n",
         "              and l.pid::text = any(array(select split_part(x, ':', 1) from unnest(coalesce(p_snap, array[]::text[])) x))\n")]),
    "collapsed_key": ("identity", [("  select p.lockid from dc.principal p where p.tenant = p_tenant and p.pid = p_pid\n",
                                    "  select 1 from dc.principal p where p.tenant = p_tenant and p.pid = p_pid\n")]),
    "refused_xlock": ("refused", [
        ("    xa := exists (select 1 from dc.head x where x.tenant = s.tenant and x.req = p_body->>'req')\n"
         "          and exists (select 1 from dc.principal p where p.tenant = s.tenant and p.pid = p_body->>'target' and not p.revoked);\n",
         "    xa := true;\n")]),
    "body_preread": ("refused", [
        ("  if pre and dc._body_refusal(p_body) is not null then pre := false; end if;       -- (in steps: SQL does not promise an order)\n",
         "")]),
    "gate_noop": ("auth", [
        ("  if s.token_hash is null or cur.token_hash is distinct from s.token_hash then return 'unauthenticated:invalid_session'; end if;\n",
         "")]),
    "expiry_noop": ("auth", [
        ("     and not x.revoked and x.expires_at > clock_timestamp();\n", "     and not x.revoked;\n"),
        ("  if s.token_hash is null or s.revoked or s.expires_at <= clock_timestamp() then\n",
         "  if s.token_hash is null or s.revoked then\n"),
        ("  if s.token_hash is null or s.revoked or s.expires_at <= clock_timestamp() then return null; end if;\n",
         "  if s.token_hash is null or s.revoked then return null; end if;\n"),
        ("  if s.token_hash is null or s.revoked or s.expires_at <= clock_timestamp() then return deny; end if;\n",
         "  if s.token_hash is null or s.revoked then return deny; end if;\n"),
        ("  if s.token_hash is null or s.revoked or s.expires_at <= clock_timestamp() then return jsonb_build_object('refused', "
         "'unauthorized'); end if;\n",
         "  if s.token_hash is null or s.revoked then return jsonb_build_object('refused', 'unauthorized'); end if;\n")]),
}


def _configured():
    return psycopg is not None and all(os.environ.get(k) for k in ENV)


def _run(names):
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    res = unittest.TestResult()
    suite.run(res)
    return res


class StepThreeProtocol(unittest.TestCase):
    def setUp(self):
        if not _configured():
            if os.environ.get("POL_STEP3_SKIP_PG") == "1":
                self.skipTest("POL_STEP3_SKIP_PG=1: the Postgres protocol fixtures were NOT run")
            self.fail("Postgres lab environment not configured (tests/test_pg.py ENV); set POL_STEP3_SKIP_PG=1 to skip explicitly")
        self._sql = pgcore._SQL_PATH
        self.addCleanup(setattr, pgcore, "_SQL_PATH", self._sql)

    def _variant(self, patches):
        src = open(self._sql, encoding="utf-8").read()
        for old, new in patches:
            self.assertEqual(src.count(old), 1, "mutant anchor: %r" % old[:80])
            src = src.replace(old, new)
        fd, p = tempfile.mkstemp(suffix=".sql")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        self.addCleanup(os.unlink, p)
        return p

    def test_the_real_core_passes_every_protocol_case(self):
        names = [n for ns in PROTOCOL.values() for n in ns]
        res = _run(names)
        self.assertEqual((res.testsRun, len(res.failures), len(res.errors), len(res.skipped)), (len(names), 0, 0, 0),
                         [str(t) for t, _ in res.failures + res.errors + res.skipped])

    def test_each_mutant_is_caught_by_an_assertion(self):
        for name, (prop, patches) in MUTANTS.items():
            with self.subTest(mutant=name):
                pgcore._SQL_PATH = self._variant(patches)
                try:
                    res = _run(PROTOCOL[prop])
                finally:
                    pgcore._SQL_PATH = self._sql
                print("MUTANT %s ran=%d failures=%d errors=%d" % (name, res.testsRun, len(res.failures), len(res.errors)), flush=True)
                self.assertGreater(len(res.failures), 0, "mutant %s was not caught by an assertion of %s" % (name, PROTOCOL[prop]))
        # every protocol case installs the core in its own setUp, so the next run starts from the real core again


if __name__ == "__main__":
    unittest.main()
