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
  in_progress    dc._evict's "inside the protocol" exemption removed: end_session evicts a revocation in progress (H1).
  collapsed_key  every principal shares one authority key (dc._ak = 1): a revoke in one tenant evicts and fences another (F1).
  refused_xlock  a grant takes the tenant lock exclusively whatever the rules will say (F5).
  expiry_noop    the authentication layer: session expiry is judged nowhere (dc._session and the entry guards of dc._submit,
                 dc._explain, dc._erase_candidates and dc._erase_values), so an expired token is accepted. A first version patched
                 dc._session alone and was NOT caught (the entry guards still refused the expired token): each expiry check alone is
                 redundant, like dc._gate below.
Not covered, disclosed: removing the final judgment dc._gate's session check is caught by no case here (the pins make it redundant
on every path these cases drive); the port keeps it as defence in depth."""
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
    "in_progress": ["tests.test_rev2262_pg.H1_RevocationInProgress.test_end_session_does_not_cancel_a_revoke_in_progress"],
    "identity": ["tests.test_rev2262_pg.F1_CrossTenant.test_colliding_principals_never_cross_evict_or_cross_fence",
                 "tests.test_rev2262_pg.F1_CrossTenant.test_a_nonexistent_colliding_target_evicts_nobody"],
    "refused": ["tests.test_rev2262_pg.F5_RefusedAuthorityChange.test_refused_changes_never_wait_for_the_tenant_lock"],
    "auth": ["tests.test_step2_pg.Step2Pg.test_bad_tokens_are_refused_on_every_call"],
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
    "in_progress": ("in_progress", [("                   or (l.granted and a.wait_event is distinct from 'BackendTermination'\n",
                                     "                   or (l.granted or a.wait_event is distinct from 'BackendTermination'\n")]),
    "collapsed_key": ("identity", [("  select p.lockid from dc.principal p where p.tenant = p_tenant and p.pid = p_pid\n",
                                    "  select 1 from dc.principal p where p.tenant = p_tenant and p.pid = p_pid\n")]),
    "refused_xlock": ("refused", [
        ("    xa := exists (select 1 from dc.head x where x.tenant = s.tenant and x.req = p_body->>'req')\n"
         "          and exists (select 1 from dc.principal p where p.tenant = s.tenant and p.pid = p_body->>'target' and not p.revoked);\n",
         "    xa := true;\n")]),
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
