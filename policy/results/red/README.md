# policy/results/red: failing-first logs

Each log is the unittest output of a red run: the new tests against the code they were written to catch (or against a deliberately
broken variant), on local, never-pushed `red/*` branches. Home paths are redacted to `~`. Before commit the logs were scanned for
credential shapes (private-key blocks, AWS key ids, `sk-` keys, user:password URLs); none occur. The 64-hex strings are sha256 digests.

## Review of 6b77354 (N1-N4, N6, N7), LOCAL, 2026-11-18

DEVIATION: every log in this section ran on a throwaway PostgreSQL 17.11 (Homebrew) on the Mac at 127.0.0.1, not on t2-dev's
16.15; the t2-dev confirmation is a separate entry when it runs. Red tree: `git archive 6b77354` of `policy/` plus the new
`tests/test_rev6b77_pg.py`. Green tree: the working tree that became the fix commit (`pol/`, `ops/` and `tests/` byte-equal to it).

| Finding | Red log (6b77354) | Strict class of the red failures | Green log |
|---|---|---|---|
| N1 parked revocation ended | `rev6b77_red_6b77354_local.out` (Ran 17, FAILED, failures=42) | liveness 6 (`N1_ParkedRevocation`: in both parked states and for all three triggers the revoker was ended and the revocation lost); `N1_Sweep` lost 6 of 8 trials (supporting, timing-dependent, not counted) | `rev6b77_green_fix_local.out` (Ran 17, OK) |
| N2 refused shapes | same log | availability 15 (a refused change evicted bob's stalled submit), liveness 15 (it waited XWAIT for T and gave up 55P03 instead of answering its refusal): 15 of the 16 shapes; `grant_role_not_string` passes red and green | same |
| N3 innocent waiter | same log | availability 2 (a submit that queued behind a revoke and committed, and a pooled connection's later transaction, were ended) | same |
| N4 parked exclusive | same log | liveness 1 (an admin holding T(a) while parked on T(b) was spared, the revoke gave up 55P03) | same |
| N7 rollback mark | same log | integrity 1 (a tampered mark made the rollback grant `pg_read_file` to PUBLIC) | same |

Not red at 6b77354, by design: `N1_ModeRule` and `N4_ParkedExclusive.test_a_marker_for_another_key_spares_nothing` are mutant
killers (they fail on `mark_any_mode` and `mark_any_key` of the protocol fixtures); `N6_JudgmentAtTheGate` kills `gate_noop` and
`expiry_noop`; `N1_LeakBound` needs the marker (its positive control fails at 6b77354, counted above, not evidence of a defect).
Focused suites on the fix (`rev2262`, `s44`, `s1`, `final`, `delta1`, `review3`, `step2`, `pg`, `pg_boundary`):
`rev6b77_focused_green_fix_local.out`, Ran 156, OK.

## Review of 2262eaa (F1-F8, H1, H2), t2-dev, 2026-11-18

Trees are `git archive` exports (code paths only, `results/` excluded) of the fix worktree; the header of each log names the tree
object. 2262eaa is the code under review; the fix commits are 6361bd9 (H2), 4e1ba6b (F1-F3, F5, F8, H1) and the F6/F7 commit
that adds this paragraph. Trees b9cc544 and 20da971 have pol/ and ops/ identical to 4e1ba6b, and exp/ and verify/ identical to the
F6/F7 commit (the log headers that say "identical to the fix commits" mean these two; 20da971 adds the corrected
tests/test_rev2262_pg.py). Strict classes
as defined below (`kit/strict_classify.py`; rev2262 tests are classed by test name in its `BY_TEST`).

| Finding | Red log (2262eaa) | Strict class of the red failures | Green log |
|---|---|---|---|
| H2 in-memory reads of revoked principals | `rev2262_mem_red_2262eaa.out` (FAILED, failures=4) | safety 4: explain and read_table answered for a principal revoked by the core, by policy, or during the wait | `rev2262_mem_green_b9cc544.out` (in-memory and evidence suites, Ran 186, OK) |
| F1 cross-tenant key collisions | `rev2262_pg_red_2262eaa.out` (Ran 12, FAILED, failures=17, all assertions) | availability 3: a tenant-a revoke of a colliding existing or nonexistent target ended a tenant-b backend; a colliding subject stalled another tenant's submit (55P03) | `rev2262_pg_green_20da971.out` (Ran 12, OK) |
| F5 refused grant or revoke | same log | availability 4 (a refused change evicted bob's in-flight submit), liveness 4 (a refused change gave up with 55P03 on the tenant lock instead of answering bad_event) | same |
| F2a prepared transactions | same log | liveness 1: a prepared transaction held a call's advisory locks, so its principal could not be revoked | same (install refused, every call 55000 when max_prepared_transactions > 0) |
| F2b member login role | same log | liveness 1: a login role in dc_app was never evicted, the revoke gave up | same |
| H1 end_session vs a revoke in progress | same log | liveness 1: end_session evicted the revocation in progress (the mirror guard, a stalled applied revoke still evicted, passes red and green) | same |
| F3 fence before eviction | same log | mechanism 1: a reconnecting adversary was answered after the revoke began (not evidence of an unsafe outcome) | same |
| F8 advisory ACL | same log | mechanism 1 (install left explicit advisory grants), integrity 1 (rollback did not restore the recorded ACL) | same |

Other green logs: `rev2262_pg8_green_b9cc544.out` (the eight Postgres suites, Ran 145, OK). SUPERSEDED, kept for the record and NOT
evidence: `rev2262_pg_red_2262eaa_SUPERSEDED.out` (first red run; two test bugs, F2b's unrevoke waited on the holder's own pin and
F2a counted prepared locks with a wrong join) and `rev2262_pg_green_b9cc544_SUPERSEDED.out` (the same F2b bug, and an F5 test that
expected bad_event while a stalled submitter held the chain head, FINAL-JUDGMENT.md 4.4 residual 3; the test was split).

F6 and F7 (experiment controls; no failing-first unit test, disclosed: the evidence is the DEV run): `dev_b3_20da971.out` (DEV
seeds, not proof): b3_pg PASS in 54 s, nine scenarios 50 of 50 with overlap 50 of 50 where pinned; controls no_head_lock 50/50
(overlap 50), no_advisory_lock 50/50 (overlap 50), idem_unprotected caught 50 of 50 with the overlap witnessed in 49 (B seen
blocked on A's transaction id, wait_event transactionid); in the 50th round the monitor never saw B blocked (overlap False,
recorded as such; at 2262eaa the control reported overlap True unconditionally). The three earlier DEV b3 runs were VOID during
this control (see the S1 section). `dev_s2_20da971.out`: s2_sessions PASS, 1860 rows, 0 accepted attacks, 91 s; ctl_txn_clock now
patches only the proof-expiry clock (F7: the session-side patches changed nothing the arm measures, which runs proof trials only, and were removed; token expiry inside a
transaction is covered by tests/test_delta1_pg.py M1_WallClock, not by this control).

Complete DEV pg_diff at the final code, first attempt: VOID (`dev_pgdiff_20da971_VOID.out`, watcher record
`batch_b3_20da971.watch`). t2-dev free space fell from 19,133,423,616 bytes at the batch start to 16,099,225,600 at 19:13:55Z
(under 15 GiB), while my container's data volume grew from 113,788,149 to 398,262,013 bytes; the rest of the drop was not mine
(likely other jobs on the host; not attributed). My watcher stopped my container at that moment (1013 s into the run): b2 had
finished clean (300 logs, 45,206 events, 0 mismatches), B1 was cut. NOT a result.

## S1 (the final review's P1-P4 merged) and FINAL-JUDGMENT.md 4.4, t2-dev, 2026-11-18

The fixer's S1 runs (`s1_*`) were never committed before; the policy owner's reruns (`rerun_*`, `s44_*`, `r3_*`, `mem_*`, `dev_*`) ran in
exact-commit trees built by `git archive` (sha256 of each tar checked on both ends; the stamp JSON of the tree,
`tests_from` included, is in the header of at least one log per tree). A lab session token that one assertion printed (`s44_fence_red_8547b27.*`) is replaced by `<lab session token
redacted>`. Each log's first two lines say what ran, on which tree and commit, and with which command. A red log has a
`.classified.txt` beside it (kit/o-classify.py): every failure and error with its assertion text, split into behavioural assertions,
import or undefined-function errors, and liveness or vacuity failures; only behavioural assertions count as failing-first evidence.
`MANIFEST-policy-owner-reruns.txt` holds the sha256 of every log here and of the DEV result documents kept on t2-dev (not committed).
`batch_o*.guard` and `batch_o6.samples` are the disk watcher's records (OWNER-COMMON: no run may leave under 15 GB free, read as
15 GiB; my watcher stopped my own container the moment free space fell under it, and every later step of that batch is VOID).

Counting rule (strict, review of 2262eaa F4; it replaces the earlier "behavioural" counts of this section, which mixed classes): each
FAIL or ERROR block of a red log is put in ONE class by `kit/strict_classify.py` (its rules, in order, are the whole method; the
`.strict.txt` beside each log is its output, one line per block):
- failing-first evidence of a defect: `safety` (an unsafe outcome: answered, applied, opened, accepted or erased when it should have
  refused), `liveness` (a revocation gave up, waited past expiry or was starved), `availability` (another backend ended, stalled or
  fenced by a call that should not touch it), `integrity` (partial state, a raise instead of a refusal, inexact rows), `verifier` (a
  verifier said PASS on manipulated evidence);
- NOT evidence of a defect: `mechanism` (a step of the fix's protocol is absent, e.g. a fence did not refuse, a privilege was not
  refused, the mutation did not wait for the call, but the assertion shows no unsafe outcome), `vacuous` (the test's own
  precondition failed: no wait, no lock trace) and `missing-api` (the function, role or attribute the test calls does not exist).
`kit/o-classify.py` (the older three-way split behind the `.classified.txt` files) is committed beside it, verbatim from t2-dev
(sha256 79655326a98702db9e7b04a46521a2d5e747dde6a64b258f6486c91ad7da4a27). Not covered by the strict classifier, disclosed:
the evidence-identity logs (`evidence_*_red_ea4677a.out`; every block is UNCLASSIFIED there, their failures are verdict assertions
that the section below reads per manipulation by hand), and the SUPERSEDED and SPOILED logs (not evidence).

| What | Log | Code under test | Result |
|---|---|---|---|
| S1 P1/P4 (blocking points) | `rerun_s1_pg_red_a2d65fd.out` | a2d65fd + the S1 tests of 8547b27 | FAILED (failures=81, errors=5). Strict: 25 evidence (safety 13, liveness 12); not evidence: mechanism 28, vacuous 28, missing-api 5. (Was "28 behavioural".) |
| S1 at ea4677a (the fixer's red) | `s1_pg_red_ea4677a.out` (fixer run s1-red), `rerun_s1_pg_red_ea4677a.out` (my rerun, same counts) | ea4677a + the S1 tests of 8547b27 | FAILED (failures=70, errors=5). Strict: 11 evidence (liveness 11; no block classed safety); not evidence: mechanism 29, vacuous 30, missing-api 5. (Was "14 behavioural".) |
| P1, P4, P5 on Postgres, rerun | `rerun_final_pg_red_a2d65fd.out` | a2d65fd + test_final_pg of 8547b27 | FAILED (failures=19). Strict: safety 17; vacuous 2. At ea4677a the same module passes (`s1_final_pg_red_ea4677a.out`, `rerun_final_pg_ea4677a.out`: ea4677a already had the recheck fixes). |
| erase_values judged again after a wait | `rerun_delta1_pg_red_a2d65fd.out`, `rerun_delta1_pg_red_ea4677a.out`, `s1_delta1_pg_red_ea4677a.out` | a2d65fd / ea4677a | FAILED (failures=1) each: erased 1 after the wait (strict: safety 1 each). |
| P1, P3 in memory; P2 S2 verifier, reruns | `rerun_final_mem_red_a2d65fd.out`, `rerun_verify_s2_red_a2d65fd.out` | a2d65fd | FAILED (failures=16) each. Strict: memory safety 7, integrity 9; S2 verifier 16. |
| Green, S1 | `s1_pg_green_e816310.out` (Ran 126 OK), `s1_pg_green_8547b27.out` (test_s1_pg, Ran 51 OK), `rerun_pg7_green_8547b27.out` (Ran 126 OK), `rerun_mem_green_8547b27.out` (in-memory and evidence suites, Ran 183 OK) | e816310, 8547b27 | OK |
| 4.4 eviction | `s44_red_8547b27.out` | 8547b27 + the 4.4 tests of 6bed430 | FAILED (failures=16). Strict: liveness 9 (a revoke, end_session, revoke_factor, tenant revoke or erasure behind a stalled holder gave up with 55P03 instead of applying); mechanism 7, not evidence (6 InsufficientPrivilege not raised for dc_app/dc_idp advisory calls, 1 evictor role not scoped). (Was "16, all assertions".) |
| 4.4 fences | `s44_fence_red_8547b27.out` | 8547b27 + 0046ef6 tests | FAILED (failures=4): open_session, submit, explain and the tenant's submits were not refused (None != '55P03') while a revocation evicted. Strict: mechanism 4, not evidence of an unsafe outcome by themselves (the assertion is the missing refusal). |
| 4.4 migration | `s44_mig_tests_red_7464493.out`; `s44_mig_exp_red_7464493.out` | 7464493 + ae644df tests; 7464493 exp.mig_rehearsal | FAILED (failures=2): forward on a cluster without dc_evictor (42704), rollback leaves the old engine without its advisory lock; the rehearsal stops at "permission denied for function pg_advisory_xact_lock" (strict: liveness 1, missing-api 1). `s44_mig_tests_red_7464493_SPOILED.out` (0046ef6 tests) is NOT evidence: its forward test dropped dc_evictor and 6 later setUps errored; ae644df restores the role. |
| 4.4 broken variants | `s44_ctl_sharelock_7464493.out`, `s44_ctl_refused_088ab4c.out` | 7464493 without the evictor's ShareLock filter; 088ab4c with dc._xlock_caller's eviction unconditional | FAILED (1, "the queued revoker was evicted"; 2, "a refused revoker evicted the holder"): the tests catch both variants. |
| Green, 4.4 | `s44_pg8_green_7464493.out` (Ran 140 OK), `mem_green_7464493.out` (Ran 183 OK), `s44_pg8_green_088ab4c.out` (Ran 145 OK), `s44_pg8_green_ae644df.out` (Ran 145 OK), `r3_rollback_green_ae644df.out` (Ran 7 OK), `mem_green_ae644df.out` (in-memory and evidence suites, no database, Ran 183 OK) | 7464493, 088ab4c, ae644df | OK |

DEV seeds (development observations, not proof):
- e816310 (fixer run s1-green): `s1_dev_s2_e816310.out` PASS, `s1_dev_b3_e816310.out` PASS (1031.7 s), and a COMPLETE pg_diff,
  `s1_dev_pgdiff_e816310.out`: PASS, 2597.7 s, 1,454,803 rows, lowest free 17042204 KiB. At 8547b27: e2, e3, e5, e6 PASS.
- Every o6 log (`r3_rollback_green_ae644df.out`, `s44_pg8_green_ae644df.out`, `dev_*_ae644df.out` except the o5 VOID b3) ran after
  I capped my DEV container's WAL (ALTER SYSTEM max_wal_size 64MB, min_wal_size 32MB; pg_wal 352 MB -> 64 MB). ASSUMPTION: WAL
  sizing changes checkpoint frequency, not results. The log headers do not say so; this line does.
- 088ab4c and ae644df (code identical in pol/, exp/, ops/, verify/): mig_rehearsal PASS, s2_sessions PASS (1860 rows, 0 accepted
  attacks) at both. b3_pg is VOID three times (`dev_b3_088ab4c_VOID.out`, `dev_b3_ae644df_VOID_o5.out`, `dev_b3_ae644df_VOID_o6.out`):
  each time my watcher stopped the container during the idem_unprotected control; the nine scenarios and the other two controls had
  passed (50 of 50 each) in the 088ab4c and o6 runs. The verify logs show those VOID b3 rows.
- Complete pg_diff at 8547b27 and 7464493: VOID (`rerun_dev_pgdiff_*_VOID.out` with their `.guard`); at ae644df NOT STARTED: free
  space 15.5-15.8 GiB (16.6-17.0 GB) against the owner's start bar of 17 GiB, later 16.25 GiB (ASSUMPTION: the owner's choice).
  The fixer's complete run never had less than 17042204 KiB (16.25 GiB) free; mine at 7464493 grew my container's volume by 74 MB
  in 4 minutes before the watcher fired.

## Evidence identity (class B of the reviews: verifiers counted rows, not identities) at ea4677a

Each verifier's property tests (tests/test_evidence_<x>.py on tests/evidence_props.py: an honest synthetic set built from the declared
design, then drop, duplicate, relabel, renumber out of sequence, extra, overwrite and a design parameter shrunk in the result document)
ran first against that verifier at ea4677a (the log header shows `git diff --stat ea4677a -- verify/<x>.py` empty and both sha256),
then against the verifier on the shared layer verify/evidence.py. A test fails at ea4677a either because the manipulation returned
PASS ("verdict PASS" in the log: the defect) or because the verdict was already FAIL but no evidence reason was named (no defect, the
old verifier had no reasons). "Result" lists the manipulations that returned PASS.

| Verifier | Red log (ea4677a) | Green log | Result at ea4677a |
|---|---|---|---|
| mig | `evidence_mig_red_ea4677a.out` | `evidence_mig_green.out` (with tests.test_evidence_layer, test_verify_pgdiff_mig, test_verify_s2, test_verify_provenance: Ran 41, OK) | FAILED (failures=17). PASS on: a duplicated clean row, a duplicated control row, a failing row followed by its passing original (the audit's overwrite, clean and control), and two seeds declared and run. Drops, relabels and extras were already FAIL there. |
| e4 | `evidence_e4_red_ea4677a.out` | `evidence_e4_green.out` (Ran 10, OK) | FAILED (failures=15). PASS on: a duplicated run, a run relabelled with another run's identity or another seed, a run renumbered past the end, to -1, to a string or a float, an extra run (index far past the end, undeclared seed, undeclared arm), 3000 byte-identical copies of one positive run (the audit's probe 2), and four seeds declared with the runs rotated over them. Drops, overwrites and R = 100 were already FAIL (the 3000-run floor). The new verifier recomputes the stamped 8c03f02 and edb693f E4 files (results/superseded) PASS: their rows already carry the identity. |
| b1 | `evidence_b1_red_ea4677a.out` | `evidence_b1_green.out` (Ran 17 with tests.test_evidence_layer, OK) | FAILED (failures=19). PASS on: the enforced r2 run dropped, enforced r2 and r3 dropped with the other pools large enough (the audit's probe 1), a duplicated run, a duplicated control counterexample, a run relabelled with another run's identity, a run's depth renumbered past its pin, to -1, to a string or a float, an extra run (depth far past the pin, undeclared arm), an extra counterexample of an undeclared control, a broken control counterexample with its passing original before or after it, and enforced r1 run and declared at depth 3. B1 is seedless; the identity is (arm, scenario, depth) per run and the control per counterexample. The new verifier recomputes the stamped edb693f B1 file (results/superseded) PASS; the 8c03f02 and 41692f7 files FAIL on their older control sets and row format. |
| e6 | `evidence_e6_red_ea4677a.out` | `evidence_e6_green.out` (Ran 16 with tests.test_evidence_layer, OK) | FAILED (failures=55). PASS on 45 manipulations: a dropped empty, valid, hold_pdp or single row; a duplicate in every family; relabels (identity and seed); renumbering past the end, to -1, string, float or bool; extras (index, undeclared seed, undeclared arm) in every family; malformed_per_seed = 1000 declared and run; malformed rows with 2 probes instead of 5. exp/e6_policy_format.py now writes the index "i" on empty, malformed and valid rows, so the stamped files (no "i") recompute FAIL "unidentified row(s)"; the edb693f rows with the emission-order index added in memory recompute PASS. The next stamped E6 run must regenerate its rows. |
| e3 | `evidence_e3_red_ea4677a.out` | `evidence_e3_green.out` (Ran 15 with tests.test_evidence_layer, OK) | FAILED (failures=29). PASS on 25 manipulations: a dropped PDP case or forge row, a duplicate of either, relabels (identity and seed), renumbering a case or a forge round past the end, to -1, string or float, extras (index, undeclared seed, undeclared arm) in both families, and three shrunk designs declared and run (planted = 1100, forge_rounds = 2, surface_entries cut to four). exp/e3_clash.py now writes "case" on PDP rows and "seed" on every row, so the stamped files recompute FAIL "unidentified row(s)"; the 8c03f02 and edb693f rows with both fields added in memory (emission order) recompute PASS, which also checks the pinned forge kinds and entries against real rows. The next stamped E3 run must regenerate its rows. |
| s2 | `evidence_s2_red_ea4677a.out` | `evidence_s2_green.out` (Ran 26 with tests.test_verify_s2 and tests.test_evidence_layer, OK) | FAILED (failures=56, errors=1). The real arm was already judged by identity; the control arms were judged by any(). PASS on 21 manipulations: control proof rows dropped, duplicated, relabelled to another seed, renumbered past the end, to -1, string, float or bool, or added far past the end; control arms cut to one catching row each (the shape of the old fixture); a duplicated control mint row; control origin and body rows dropped, duplicated or relabelled to another seed; a silent control (ctl_no_mac, every proof refused) overwritten by one accepted copy; a catching replay copied from seed 1 into seed 3 (the nonce identity covered the real arm only); and the real arm's concurrent round renumbered to 1.0 or True (untyped compare; a string round crashed the old verifier with TypeError). tests/test_verify_s2.good_rows now writes the control arms as exp/s2_sessions.py does (full design), and each "control silent" break silences every row of the arm. No stamped S2 rows to recompute here (the d05d731 items file is not in the tree). |
| b3 | `evidence_b3_red_ea4677a.out` | `evidence_b3_green.out` (Ran 17 with tests.test_evidence_layer, OK) | FAILED (failures=33, errors=1). PASS on 18 manipulations: a failing round overwritten by its passing original (before or after it: the old verifier counted 50 passes among 51 rows); a dropped, duplicated or relabelled differential log; a whole log dropped with its events; a round or event renumbered past the end, to -1, string, float or bool; R = 25 and diff_logs = 10 declared and run. A round of an undeclared scenario crashed the old verifier (ValueError), not a false PASS. exp/b3_pg.py now writes the event index "i" on diff_event rows, so the stamped files recompute FAIL "unidentified row(s)"; the edb693f rows with "i" added in memory (emission order per log) recompute PASS. The next stamped B3 run must regenerate its rows. |
| e1 | `evidence_e1_red_ea4677a.out` | `evidence_e1_green.out` (Ran 15 with tests.test_evidence_layer, OK; 128 s CPU, 230,000 rows per call) | FAILED (failures=39, errors=2). PASS on 31 manipulations: a dropped law triple or round trip; a duplicate in every family; relabels (identity and seed) in every family; renumbering past the end, to -1, string or float in every family; extras (index far past the end, undeclared seed) in every family; n_per_seed = 2000 over ten seeds declared and run; law_triples = 1000 declared with 200 law rows. A law or round-trip row relabelled to an undeclared arm crashed the old verifier (KeyError), not a false PASS. No experiment change (E1 rows already carry arm, seed and n). The stamped edb693f and 8c03f02 E1 files (results/superseded) recompute PASS. |
| e2 | `evidence_e2_red_ea4677a.out` | `evidence_e2_green.out` (Ran 17 with tests.test_evidence_layer, OK) | FAILED (failures=28). PASS on 24 manipulations: a dropped, duplicated or relabelled scenario or child row (identity and seed); renumbering a scenario or a child past the end, to -1, string, float or bool; extras (index far past the end, undeclared seed) in both families; a fourth child in a 3-child scenario; a control scenario dropped with its leaking child; per_seed = 400 declared and run. exp/e2_egress.py now writes "k" (index in its scenario) on every child row and "items" (the child count) on every scenario row, so the stamped files recompute FAIL; the edb693f rows (510,578) with both fields added in memory recompute PASS. The next stamped E2 run must regenerate its rows. |
| b2 | `evidence_b2_red_ea4677a.out` | `evidence_b2_green.out` (Ran 16 with tests.test_evidence_layer, OK) | FAILED (failures=30). PASS on 16 manipulations: a dropped svc_audit or ctl_log row; a duplicated log, svc_audit or ctl_log row; a relabelled svc_audit or ctl_log row; an event renumbered past the end, to -1, string, float or bool; an extra log or event row with an undeclared arm; log 71/11 (with its events and control rows) moved to 75/600, a log the design never runs; 300 logs per seed over ten seeds declared and run. The honest set shuffled FAILED at ea4677a: the per-log code check concatenated event codes in row order, so the verdict depended on file order; the new verifier reads them in event order (i). No experiment change (the rows already carry arm, log and i, and the log row declares n); the stamped edb693f rows (476,869) recompute PASS unchanged; 8c03f02 recomputes FAIL on its older control set (params.controls is not the pinned list). |
| pgdiff | `evidence_pgdiff_red_ea4677a.out`; the reworked legacy fixture at ea4677a: `evidence_pgdiff_legacy_fixture_ea4677a.out` (Ran 8, OK: the honest set PASSES and every legacy break still FAILS there) | `evidence_pgdiff_green.out` (Ran 31 with tests.test_verify_pgdiff_mig and tests.test_evidence_layer, OK) | FAILED (failures=59). PASS on 34 manipulations: a duplicated B2 view row; a relabelled B2 log or event; a B2 event, B1 node or B1 transition renumbered past the end, to -1, string, float or bool; an extra row with an undeclared arm in every family; B2 log 65/59 moved to 65/60 (past per_seed) or 62/7 to seed 99; ctl:b2 log 61/29 moved to 63/0 (outside the first tenth); in a B1 tree, a node declared as the child of two transitions, a child cleared while its subtree stays, a node and its transitions dropped under a parent that still declares it (summary edited), an inner node given the depth's level with its transitions dropped (summary edited), a transition written at another level; and three shrunk designs (30 logs per seed over ten seeds, every B1 depth halved, scenario joint dropped). exp/pg_diff.py now appends i to B2 t rows, n to l rows, node to B1 v rows and node, ti, child to B1 t rows (emission unexecuted: no database runs here), so the stamped files recompute FAIL; the edb693f rows (1,453,450) with the fields rebuilt in memory from the depth-first row order recompute PASS, every family exact (B1: 136,810 nodes, 1,267,098 transitions, pool sizes 36/20/25/13/81 as pinned). The d05d731 items file is not in the repo. The next stamped pg_diff run must regenerate its rows. |
| e5 | `evidence_e5_red_ea4677a.out` | `evidence_e5_green.out` (Ran 22 with tests.test_evidence_layer, OK) | FAILED (failures=36, errors=24). PASS on 30 manipulations: a dropped view or dview; a duplicate in every family; a world or dworld overwritten by its broken copy when the passing original came later (the old verifier kept the LAST world row of an identity: order dependent); a view relabelled to another world, or to another seed (view and dview); a row renumbered to a float in every family; extras (index far past the end, undeclared seed, undeclared arm) of world and dworld rows; a view of an undeclared viewer (p2 in a 2-thread world), a dview of h4; the leaking control view of p0 relabelled pall; a world declaring no pt with its pt view gone; a world declaring a fourth thread viewer; the informational arm B_ctl_unscoped_value dropped; 50 B_ctl_flag_always worlds relabelled existence_only; 100 worlds per seed over ten seeds. The 24 errors are KeyError crashes of the old verifier on a view whose world is absent (not a PASS). exp/e5_threads.py now writes "viewers" (the viewer ids of TW.viewers) on every world row, so the stamped files recompute FAIL; the edb693f rows (44,503) with "viewers" rebuilt in memory from the regenerated worlds (their sources equal the stamped ones in all 3000 worlds) recompute PASS, every family exact. The next stamped E5 run must regenerate its rows. |

All twelve verifiers together at 1c13efb (clean tree): `evidence_all_green_1c13efb.out`, every tests.test_evidence_* module with
tests.test_verify_s2, test_verify_pgdiff_mig and test_verify_provenance: Ran 167, OK (1250 s wall on a loaded Mac; E1 is most of it).

## Final review (reviews/policy-final/review-1.md) at a2d65fd

| Finding | Log | Code under test | Result |
|---|---|---|---|
| P2 S2 trial independence | `final_p2_verifier_a2d65fd.out` | a2d65fd verify/s2.py + the new tests/test_verify_s2.py (good rows carry trial and nonce identities, which a2d65fd ignores) | FAILED (failures=16): a2d65fd returns PASS on each of the 11 duplication manipulations in `P2_MANIPULATIONS` (12 copies of one observation per class, the reviewer's probe; copies renumbered; one trial twice; a trial gap; a seed relabelled from another; replay_first unlinked or on another factor; revoked_principal copies; concurrent rounds copied; rows with no identity; one nonce in two classes) and on 5 non-integer trial indices. The pre-existing S2 verifier tests pass on the same run. |
| P1 and P3 in memory | `final_p1p3_memory_a2d65fd.out` | unmodified a2d65fd + the new tests/test_final.py (faults injected by wrapping existing callables) | FAILED (failures=16), each behavioural. P3: the reviewer's `"type": []` probe raised TypeError, left the value and mapping, and the exact retry of the next valid request got `idem_conflict`; an unhashable type on its own; a fault while tokenising the label, in the engine before it decides, inside the reduction and while applying the delta each left value and mapping; `_core.reduce` raised on 922 of 3000 fuzzed events; the random-mix vault invariant broke at step 39. P1 (memory): a session ended, a principal revoked by policy or removed from the directory while the submission waited for its key lock or for the engine lock was still applied (6 subtests); explain returned the head to a session ended during its wait; a lock wait could come between the erase judgment and the deletion. Regression guards in the same run pass: faults after acceptance (lost response, audit), the other malformed keys, self-revoke, table, erase after an ended session. |
| P1, P4, P5 on Postgres | `final_p1p4p5_pg_a2d65fd.out` | a2d65fd + the new tests/test_final_pg.py + the owner's READ UNCOMMITTED test in tests/test_delta1_pg.py; t2-dev PG 16.15 | FAILED (failures=56, errors=2). Every blocking-point test witnessed its wait (pg_blocking_pids). P1 submit: after the subject-token unique-index wait (held by a row or by bob's open submit), the chain-head wait and an events table lock, the call was applied (`proposed`) for an expired, ended or factor-revoked session (chain head: a revoked principal too); after the tenant row, advisory lock and head row only the factor mutation applied (the rest were judged again: regression guards). Revoke: after waiting on the target's principal row, its props rows, a SHARE lock on dc.sessions or a session row held by an open end_session, the revoke was applied (`revoked`) for an expired, ended or factor-revoked admin, and it waited 1.5 s on the held session row. explain, read_table and erase_candidates answered after a table wait for ended, expired, factor-revoked (and, for explain and erase_candidates, principal-revoked) sessions; explain answered for a principal revoked while its STABLE `_explain_raw` waited (pre-wait snapshot). erase_values erased after a value-row or token-row wait for every mutation (after the tenant row: factor only). P4: a factor or principal revoked while open_session waited on the replay-ledger twin, the prune or the factor row (foreign key) still opened a session. P5: end_session answered True (READ UNCOMMITTED) and False (REPEATABLE READ, SERIALIZABLE) and left the token live. The 2 errors were a test bug in P4_UseTimeFactorRevocation (an eagerly evaluated dict), fixed and rerun: `final_p4_usetime_pg_a2d65fd.out`, FAILED (failures=5): submit, explain, read_table, erase_candidates and erase_values all accepted a session whose factor was revoked. Regression guards passing in the same run: self-revoke, open_session expiry and positive control, the volatile open_session and dc._session reads after a table wait (P1_JudgmentNeverWaits sessions, open_session), the P3 Postgres challenges (statement timeout, deadlock victim, dropped client connection, lost response), the owner's READ UNCOMMITTED test and all of test_delta1_pg. "factors" subtests of P1_JudgmentNeverWaits fail as "did not wait": a2d65fd never read dc.factors on those calls. |
| Green for the above | `final_p1p4p5_pg_green_5814ea0.out`, `final_s2_dev_green_5814ea0.out` | 5814ea0 (run copy byte-identical) | tests.test_final_pg, test_delta1_pg, test_pg, test_pg_boundary, test_review3_pg, test_step2_pg: Ran 96, OK. exp.s2_sessions on DEV seeds: PASS, 1860 rows, 0 accepted attacks; `verify --dev` S2 row PASS, recomputed PASS, agree (other experiments not run in that copy). |
| P1 in PgDecisionService (the in-process authority) | `final_p1_pgservice_a2d65fd.out` | a2d65fd + tests/test_final_pg.py P1_PgServiceJudgment | FAILED (failures=4), each behavioural: with its dc.submit waiting on the chain head, a principal revoked by the policy document or removed from the directory was still applied (`proposed`); with its dc.erase_values waiting on the value row, the same admin mutations still erased. The live controls pass (waits witnessed). Green at the commit that adds `PgDecisionService._judged`: `final_p1p4p5_pg_green_service.out`, every Postgres module, Ran 98, OK. |
| P1 in PgDecisionService: each value's erase decision | `final_p1_erase_decision_68c6dcd.out` | 68c6dcd + tests/test_final_pg.py `test_erase_decision_changes_during_wait` | FAILED (failures=2), behavioural: with dc.erase_values waiting on the held value's row, recording its legal hold (`hold_recorded`) or withdrawing the tenant's erase grant (`erase_grant_withdrawn`) during the wait still erased both values (`erased=2`, result `erased`). The live subtest and the two earlier service tests pass (waits witnessed). Green at the commit that decides each value again after the call (run copy sha256-checked against the worktree): `final_p1p4p5_pg_green_erase.out`, every Postgres module, Ran 99, OK. |
| S2: nothing waits after the final judgment (FINAL-JUDGMENT.md) | `final_s2_postjudgment_3153059.out` | 3153059 + tests/test_final_pg.py `S2_NothingWaitsAfterTheJudgment`, with a deliberately broken variant made by the test (`dc._lock_authority` a no-op, restored after) | FAILED (failures=2), behavioural: the revoke's wait on its target's principal row moved after the judgment, the call waited there (`waited True`) and returned `revoked` after the admin's session was ended or expired. The guard that the caller's `lock_timeout` is restored passes. Green at ad2a9cb, the commit that sets `lock_timeout` to 1 ms after the judgment (run copy sha256-equal): `final_p1p4p5_pg_green_s2.out`, every Postgres module, Ran 101, OK (with the variant: lock_not_available, nothing applied, no wait). |

## Delta review 1 (reviews/policy-delta/review-1.md) at d05d731

| Finding | Log | Code under test | Result |
|---|---|---|---|
| M1 wall clock, every entry point | `delta1_m1_m3_n4_pg_d05d731.out` | d05d731 + red harness 7e8df79 | 14 FAIL, all behavioural assertions: a proof expiring inside a transaction opened a session; a token expiring inside a transaction, or ended after a REPEATABLE READ snapshot, was accepted by submit, explain, read_table, erase_candidates and erase_values; a factor revoked after the snapshot opened a session; submit and erase_values did not judge the session again after a lock wait. The M3 and N4 tests in the same run pass. |
| M2 S2 coverage pinned | `delta1_m2_verifier_d05d731.out` | d05d731 verifier + red harness 8e58460 | FAILED (failures=7, errors=1). The d05d731 verifier returns PASS on the reviewer's three manipulations (`_drop_classes`, `_one_mint_probe`, `_no_agent_origin`), on mine (`_small_replay`, `_fewer_trials`, `_one_seed`) and on duplicated or unexpected coverage; `_token_class_dropped` crashes it with KeyError instead of a verdict. |
| M3 in-memory overlap | `delta1_m3_memory_d05d731.out` | d05d731 | 1 FAIL: the winner's retry got `idem_conflict` instead of `duplicate` of its original ref, because the Vault published the idempotency mapping before the engine decided (delta review 1 M3). |
| M3 Postgres (challenge) | `delta1_m3_pg_challenge_d05d731_6b67f47.out` | unmodified d05d731 code, test as committed in 1ce569c | Ran 4, OK: the per-principal advisory lock already serializes allocation, acceptance and cleanup per (tenant, principal, key). |
| M3 Postgres, broken | `delta1_m3_pg_noadvisory_e9dd0af.out` | d05d731 without the advisory lock | FAIL: with B on another requirement, B gets `error:UniqueViolation` instead of `idem_conflict`. |
| M3 Postgres, first attempt | `delta1_m3_pg_noadvisory_r1only_02eec0e.out` | d05d731 without the advisory lock, r1-only test | OK: on one requirement the head row FOR UPDATE also serializes the pair, so this test did not isolate the advisory lock; the different-requirement cases were added for that. |
| M3 Postgres, no locks | `delta1_m3_pg_nolocks_9e99651.out` | d05d731 without advisory and head locks | ERROR: UniqueViolation on `events_idem` reaches the caller; the unique index is the last line, no value is stored twice. |
| N4 mid-rollback failure | `delta1_n4_split_variant_e494207.out` | rollback with the drops committed before the rename | 1 FAIL, behavioural ("dc_crypto is gone"); the other 14 rollback tests pass. |
| N4, first attempt | `delta1_n4_notxn_variant_573ddc2.out` | rollback with no transaction | 5 ERROR at LOCK TABLE outside a transaction, before the injection point: NOT meaningful red evidence for the injection test, kept for the record; superseded by the split variant above. |

## Review 3 at 0a20b64, step 2 at e15241c

- `n2_sync_guard_0a20b64.out`, `n5_value_refs_0a20b64.out`, `n6_provenance_0a20b64.out`, `n7_surface_ast_0a20b64.out`,
  `n8_n4_verifiers_0a20b64.out`, `n8_mig_probe_reproducer.out`, `review3_pg_0a20b64.out`: review-3 findings against 0a20b64.
- `step2_memory_e15241c.out`, `step2_pg_e15241c.out`: step-2 tests against e15241c.

**Delta review 1 M4: some step-2 red failures show only that an API was missing, not the refused behaviour.**
- `test_derivation_table` (tests/test_step2.py:44-49) failed with AttributeError: `_core.origin` did not exist at e15241c. This holds in both logs.
- The PG tests that go through the `_factor`/`_open`/`_token` helpers (tests/test_step2_pg.py:64-83) failed at the helper's positive control (`'refused' != 'ok'`): e15241c had no proof path, so no valid proof could open a session. Their own refusal assertions were never reached. This covers:
  - bad tokens on every call;
  - a body naming identity or origin;
  - enrolment refusals;
  - events carrying actor and origin;
  - expired and far-future proofs;
  - forged proofs;
  - the other-tenant token;
  - concurrent replay.
- The three bridge tests failed with AttributeError (no `PgIdentity`).

The failures that are behavioural at e15241c:
- memory:
  - agent and human events not stamped with actor and origin;
  - a body naming actor or origin refused as `bad_event`, not `identity_in_body`;
  - look-alike principals accepted;
- PG:
  - dc_idp minted a session from a bare principal id;
  - install accepted look-alike principals.
