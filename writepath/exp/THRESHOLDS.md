# Pre-registered thresholds (committed before any proof run)

Applies to every experiment under exp/ in this branch. Proof rules: gotchas/PROOF-RULES.md on lab main.

Common
- Interleavings are forced by named in-process hooks (the wrapped function runs the competing operation at the exact
  point), not by sleeps. Each arm runs R = 10 times on a fresh tenant.
- PASS for a scenario: the fenced arm satisfies the property in 10/10 runs AND the control arm violates it in at least
  9/10 runs, with the violation reason recorded per run, AND every witness count named below is > 0 in every run.
- A run that errors (exception other than the expected refusal) scores as a failure of that arm.
- Results: content-addressed JSON with environment fingerprint under WP_HOME/results.

N1 read-before-pin: witness = rows read containing the subject before the erasure; overlap witness = DB clock times
  read < erasure phase-1 commit < write attempt. Control = projector that resolves its epoch after the read.
N2 every family: witness = families enumerated from wp.families.FAMILIES (>= 2) each with > 0 subject docs before.
  Control = a writer for an unregistered family; it must leave residue AND be flagged by registry.unregistered().
N3 stale rebuild: witness = A's survivor read contains Y; overlap = A read < B done < A write. Control = A writes by
  concrete index name.
N4 completion: witness = residue count > 0 at completion time. Control = the same run on wp_ctl (no guard trigger),
  where 'done' is accepted with residue, plus a reader that filters only while a job is open.
N5 pending reads: witness = raw ES count of the subject > 0 while pending. Control = facts read without the filter.
N6 no plaintext in Postgres: marker hits in every table of wp_events and wp_keys = 0 (SQL scan of all columns cast to
  text, and grep of the database's files on disk after CHECKPOINT). Positive control: the same grep finds the old
  code's plaintext marker in database 'emlab' on the same cluster.
N7 decisions: (a) stale acceptance refused, (b) replay refused or duplicate, (c) same-id retry is one event.
  Control = wp_ctl (no head compare-and-set, no idempotency constraint): must accept (a) and duplicate (c).
Row 1 zombie session: 20 zombie appends + 1 confirm per run. Pass = 0 accepted, 20 quarantined, chain verifies,
  0 analyst jobs and 0 ES docs from the zombie, inbox item shown once and confirmed by the new holder only.
  Control = wp_ctl: at least 1 zombie event accepted or the item confirmed by the zombie.
Row 23 late commit: hold one transaction open while 10 later ones commit. Naive cursor control must skip >= 1 row;
  the xmin cursor must skip 0 and project all 11.
Registry: unregistered() == [] on a clean system; controls: one rogue table and one rogue index must each be listed.
ES on-disk marker grep: if the pre-erasure grep finds 0 files, the on-disk claim is NOT made (API level only).

## Amendment 1 (committed before the second, reported run; the a482a22 run is superseded)
Added after an advisor review found two unexercised paths:
N3S_projector stale swap: eraser A has bulk-written its staging index; a projector takes over (force) before A's swap.
  Pass = A's swap refused, read alias on exactly 1 index, the new projector's write alias present and a new event W
  projected, survivor Z live. Control = A swaps without the compare-and-set: must leave W unprojected or the alias gone.
N3S_eraser stale swap with an eraser B as taker: fenced arm only. No sensitive control exists in this harness because
  B's swap deletes A's staging index; reported as SUPPORTING evidence, never as PASS.
N8 writers racing phase 1: a derive that read X's fact before phase 1, an inbox enqueue for X and a stale append for X
  are released while phase 1 is open. Overlap witness = backends waiting on a lock during phase 1 (>= 1).
  Pass = job done and zero X residue in facts, inbox, quarantine and ES. Control = wp_ctl: residue > 0 after done.
ES probe P8: remove_index combined with a failing must_exist remove must leave the index in place.
Row 27 (stale-approval half): amended -> execution refused 10/10; not amended -> accepted 10/10; control accepts >= 9/10.

## Amendment 2 (2026-11-16, committed before any run that uses it)
N3S_eraser now has a control: eraser B's fence and cleanup see only indices carrying the read or a write alias (blind to
  staging indices). Pass = fenced 10/10 (Y_live 0, Y_all_after_sweep 0, read alias on 1 index, Z live, A's staging
  index never carries the read alias) AND control violated >= 9/10, where the violation must include
  stale_swap_took_effect (A's staging index, holding Y, became the live index). Supporting arm ablate_cas (A swaps without its CAS, B correct) is reported, never decides PASS.
N6 + registry (registry_marker) now R = 10 per arm. Fenced pass per run = the original conditions. Control = erasure with
  an in-place delete-by-query purge, the subject key kept, no force merge: violated when the marker (ES data dir) or the
  key hex (PG data dir) is still found on disk after the job. PASS = fenced 10/10 AND control violated >= 9/10.
Independent replications: where a race is claimed, a "jitter" mode (competing thread released after a random delay drawn
  from a seeded RNG, seeds recorded) is reported next to the hook-forced mode; the jitter mode reports how many runs
  actually overlapped (DB clock witness) and is judged only on runs that overlapped (>= 5 of 10 must overlap, else the
  jitter result is reported INCONCLUSIVE, not PASS).

## Amendment 3 (2026-11-16, after the BLOCK review of aaa0fec and the user decisions relayed the same day; committed before any reported run that uses it)
Superseded: N3, N3S_projector, N3S_eraser and N8's wp_ctl control (the rebuild and the all-triggers-dropped wp_ctl
database are removed); the "5 of 10 overlap" jitter rule (replaced by exp/races.py below).
Controls: every control is the shipped system with ONE named patch (exp/patches.py SQL variant cut from wp/schema.sql,
  or a named in-process substitution), the patch text is recorded in the result, and exp/guard_check.py runs before and
  after every reported run and must PASS both times (a patch that failed to revert fails the run). Where a property is
  enforced in two places, the control removes both and says so.
Attacks are run inside a rolled-back transaction and counted with rows affected; an attack that touches 0 rows is
  neither a refusal nor a breach (it makes the case FAIL as vacuous).
Deterministic cases, one run each (exp/review_fix.py F3, F4, F5, F7): PASS = every fenced case refused/holds AND every
  control violated; F5 positive cases (current approval executes; first decision; same key in another session) accepted.
F1 one subject with 100,000 events (plus subject Y with 2): job done, X docs 0 in every index, Y docs 2.
F1S sweep isolation, R=10 per arm: the first job fails (injected); PASS = fenced 10/10 (B done, A not done and its
  claim released) AND control (aaa0fec loop that aborts on the first failure) violated >= 9/10.
F6 takeover with a zombie stalled 20 s before COMMIT, R=3 per arm: fenced takeover <= 8 s; control (idle-in-transaction
  timeout reset) > 8 s in 3/3. Quarantine: 1,500 stale attempts, kept <= 1,000 in 3/3; control (cap 10^9) keeps 1,500.
  xmin: MEASUREMENT only (another database's transaction held 20 s, unmitigated vs cluster-wide
  idle_in_transaction_session_timeout=5s); no PASS.
Races (exp/races.py R1, R2, R3): real processes, random delays from seeded RNGs (seeds recorded), instants on the
  database clock. N = 40 per arm. A run counts toward the verdict only if it OVERLAPPED (definition per race in the
  module docstring). PASS = fenced: 0 violations in all 40 runs and 0 errors; control: >= 1 violation among overlapping
  runs; both arms >= 20 overlapping runs, else INCONCLUSIVE. Reported: Wilson 95% interval of the hold rate over
  overlapping runs (fenced 20/20 gives lower bound 0.839, 40/40 gives 0.912), and of the control's violation rate.
Mentions and legal hold (exp/scope_hold.py, user decisions 2026-11-16), R=3 per arm:
  M fenced: done; every planted in-scope marker (own, 4 write-time hits, 2 rescan hits) absent from Postgres plaintext
    (oracle decrypts every remaining ciphertext) and from ES; every out-of-scope marker (4 documented matcher misses,
    1 other subject) still present; receipt counts found_at_write = 4, found_by_rescan = 2, declared_identifiers = 3,
    not_covered listed. norescan_fenced (runner skips the rescan): the store refuses done with pg:mentions_unscanned.
    Controls violated 3/3: ctl_write_matcher (matcher returns nothing), ctl_norescan (store rescan gate removed).
  H fenced 3/3 over three cases: hold before request (X held and readable; concurrent Y erasure done during the hold;
    X done after release), request before hold (held; final receipt lists the released hold and delay_s >= 1),
    hold placed after mark (cascade stopped, key kept; done after release). Control (wp_hold_check no-op) violated 3/3.
Every result carries env.key_store: the lab key store is a STAND-IN for the client's KMS (keys.KeyStore interface).
Erasure cost (exp/erasure_cost.py): measurement only.

## Amendment 4 (2026-11-16, after the reported run at 3944749; applies to the rerun of the cases below only)
F1: the pre-registered "Y docs 2" was an arithmetic error in the threshold: subject Y writes 1 message and 1 fact,
  which project to 2 event documents and 1 fact document. Corrected to Y docs = 3. The 3944749 F1 result stays
  reported as FAIL against the original text.
F3 delete_tombstones: the attack ran against a tenant with no tombstones (0 rows: vacuous control). The attack now
  runs after the app's own mark + cascade of X created tombstones. Rerun of F3 only.
row23: the 5 s idle-in-transaction timeout (F6 mitigation) ended H's held transaction; H's session now raises it to
  120 s for this experiment (stated in the module). Rerun of row23 only.
registry_marker: the key hex survived in the subject_keys heap after a plain VACUUM in 3/10 fenced runs (a real
  defect: VACUUM does not zero freed tuple space). Fixed with VACUUM FULL in the lab key store; rerun R=10 per arm.

## Amendment 5 (2026-11-16, written before any of these runs; delta review at 963e597 and rows 19, 6)
D1 (exp/d1_hold_mentions.py), R=5 per case and per control: PASS = fenced S1, S2, S3 hold 5/5 each with 0 errors AND
  ctl_apply_ignores_holds violates S1 and S2 5/5 AND ctl_app_can_release violates S3 5/5 (definitions in the module).
D5 cascade (exp/review_fix.py F1 at 100,000 events, and exp/erasure_cost.py): MEASUREMENT of per-batch time against the
  batch index; the claim "linear" is accepted only if the last 5 batches take <= 2x the first 5 (old code: grows with
  the remaining set). No PASS for the cost measurement itself.
Row 19 (exp/row19_secrets.py): planted corpus = 11 detector families x 8 in-scope encodings x 10 random instances
  (880 items); clean corpus = >= 3,000 chunks of 40 lines from the Python standard library (/usr/lib/python3.12/*.py,
  top level only) plus 500 synthetic look-alikes (UUIDs, git SHAs, sha256 hex, base64 of prose, URLs without
  credentials, placeholder assignments, the AWS documentation example keys). PASS = catch-rate Wilson 95% lower bound
  >= 0.98 on the in-scope planted corpus AND false-block Wilson 95% upper bound <= 0.01 on the clean corpus AND the
  documented out-of-scope variants (reversed, ROT13, split across two writes) are reported, not counted. Controls:
  ctl_nodecode (scanner without the decoding layers) must miss >= 1 in-scope encoded family (catch lower bound < 0.98);
  ctl_noscan (append path without the scanner, store check removed) must store a planted secret; fenced append of a
  planted secret stores nothing (events and quarantine unchanged) and the store refuses an event without the current
  scanner version (WPS21); ctl_store_check_removed accepts it.
Row 6 (exp/row6_chain.py), R=10 per case: forged append (admin, correctly SHA-chained, no chain key) detected by the
  keyed verify 10/10, while the unkeyed control verifies it 10/10; app role cannot disable or drop a trigger, alter a
  guard function or set session_replication_role (all refused) while the control (wp_app made a member of wp_owner)
  succeeds; truncated tail and regressed head (tail removed then new events appended) are flagged by the anchor check
  10/10 while the chain-only control reports ok 10/10.
D4 (exp/d4_rescan_es.py), R=5 per arm, written before the run: the runner skips the placeholders of rescan hits;
  fenced (verifier checks every tombstoned id is a placeholder) not done with es:tombstoned_not_placeholder in the
  residue 5/5; control (verifier without that check) reports done 5/5 while the hit's ES document keeps its content.

## Amendment 6 (2026-11-16, written before the run): row 3 checkpoint seal (exp/row3_seal.py), R=10 per case and arm
PASS = P_prefix holds 10/10 (verify ok after later appends); E_edited_seal fenced 10/10 mac_invalid, control (unkeyed
seal) violated 10/10; T_truncated_log fenced 10/10 head_missing, control (seal checked only against itself) violated
10/10. The seal interface (wp/seal.py seal/verify) is offered to the context-runtime stream; its consumption there is not
tested by this stream.

## Amendment 7 (2026-11-16, after the reported run at 7e8f28f; thresholds unchanged)
M failed at 7e8f28f on counts only (done, no over- or under-erasure): the receipt's found_by_rescan counted the
write-time mentions that 47a932a materialises into erasure_mentions at mark. A real receipt defect (overclaims the
rescan), fixed by counting source = 'rescan'. The 7e8f28f M result stays reported as FAIL; M is rerun alone, R=3.
R1 at 7e8f28f was INCONCLUSIVE (fenced arm 18 overlapping runs < 20; 0 violations in 40). Reported as INCONCLUSIVE; one
  rerun of R1 alone at N=40 with the same rule follows (written before the rerun). If it is again below 20 overlaps it
  is reported INCONCLUSIVE, not combined with this run.

## Amendment 8 (2026-11-16, written before the reported run): cross-tenant projection delay (exp/xmin_stall.py)
Confirmation already observed at 4960f50 on the pre-round role settings (one arm, 60 s holders, tenant B
commit-to-visible max): read-only verifier snapshot 0.149 s; app idle xid 4.67 s (ended by its 5 s idle timeout); app
active xid 59.6 s; verifier idle xid 59.8 s; key-database idle xid 59.75 s; baseline 0.21 s. Mechanism: the
projector's late-commit fence is the cluster-wide oldest running xid, so ANY xid holder in any tenant or database
delays every tenant's projection for as long as it is open.
Fix under test: every login role gets statement_timeout 30 s and idle_in_transaction_session_timeout 5 s
(wp/setup.py ROLE_TIMEOUTS); erasure batches are sized to a 2 s target. Hold 60 s; fenced arm R=3, control arm R=1
(control = timeouts reset on every role except the app's pre-round 5 s / 300 s).
PASS = in every fenced repetition: ro_verifier max <= 5 s; idle_xid_app, idle_xid_verifier, idle_xid_keysdb max <= 10 s
  each; active_xid_app max <= 35 s; every xid holder ended by the server; AND in the control active_xid_app,
  idle_xid_verifier and idle_xid_keysdb each >= 55 s (the stall reproduces without the fix).
Stated residual, not a PASS claim: a role without these settings (the admin/superuser, a migration or an operator
session) is not bounded by them; with one shared Postgres the bound is the longest statement or idle period any role
allows. Deployment requirement: the timeouts on every role, a monitor and alert on the age of the oldest xid
(pg_stat_activity backend_xid/backend_xmin, pg_prepared_xacts, replication slots), or per-tenant databases.

## Amendment 8a (2026-11-16, before any amendment-8 run; supersedes amendment 8's PASS rule and residual)
Correction: statement_timeout and idle_in_transaction_session_timeout do NOT bound a transaction on PostgreSQL 16 (the
lab runs 16.15): a session issuing a short statement every few seconds keeps its xid indefinitely (transaction_timeout
arrives in 17). Amendment 8's residual ("the longest statement or idle period any role allows") was wrong.
Added: holder chatty_xid_app (txid_current(), then select 1 every 2 s); the transaction-age watchdog
wp/xact_watchdog.py (role wp_watchdog: pg_signal_backend + pg_read_all_stats, no data access; terminates managed-role
backends whose transaction holding an xid or snapshot is older than 30 s, polled every 1 s).
Arms: fenced = role timeouts + watchdog, R=3; timeouts_only = role timeouts, no watchdog, R=1; control = pre-round
settings, no watchdog, R=1.
PASS = every fenced repetition: ro_verifier <= 5 s; idle_xid_app, idle_xid_verifier, idle_xid_keysdb <= 10 s each;
  active_xid_app and chatty_xid_app <= 35 s each; every xid holder ended by the server; AND timeouts_only
  chatty_xid_app >= 55 s; AND control active_xid_app, idle_xid_verifier, idle_xid_keysdb, chatty_xid_app >= 55 s each.
Residual (stated, not tested as a PASS): superuser/admin sessions (pg_signal_backend cannot end them), prepared
transactions and replication slots are not bounded by the watchdog; with one shared Postgres any of these delays every
tenant's projection for its duration. Deployment requirement: the role timeouts AND (PostgreSQL >= 17
transaction_timeout on every role, or this watchdog), an alert on the age of the oldest xid / xmin, no
long-running superuser sessions, prepared transactions monitored; or one database cluster per tenant.

## Amendment 9 (2026-11-16, written before the runs): erasure at tenant scale and the physical-purge receipt
Diagnosis already observed (exp/explain_cascade.py, 200k tenant, 100k subject, not a PASS): 95% of a cascade batch was
a sequential scan of analyst_jobs for `input_event_ids && ids` (3.7-4 ms per event at 200k, proportional to tenant
rows; unchanged by ANALYZE; the generic plan from the sixth call used the GIN index at 0.16 ms per event).
exp/scale_1m.py at N = 100,000 and 1,000,000 (S100000 only at 1M). PASS =
  every S-subject done; no cascade batch > 5 s; heartbeat_transient == 0 in every real erasure;
  at 1M the per-event cascade time of S100000 <= 2 x that of S10000 (linear in the subject);
  S10000 cascade time at 1M <= 2 x at 100k (independent of tenant size);
  wp_erasure_pg_residue for S10 <= 1 s at each N, and at 1M the 4960f50 control body >= 10 x the shipped one;
  heartbeat (35 s job-row lock during a job): fenced 3/3 done, control (no transient tolerance) 3/3 ClaimLost.
  EXPLAIN, verify step timings and the time to done are reported, not thresholded.
exp/phys_purge.py, R=5 per arm. PASS = fenced 5/5: default run returns receipt_state
  logically_erased_physical_purge_pending, done False, within 10 s, with docs.deleted > 0 or a key purge pending at
  that moment; then the follow-up sweep reaches done with docs.deleted == 0 within the store deadline (300 s);
  AND control (wp_erasure_complete ignoring es:soft_deleted and keys:physical_pending) 5/5 claims done while
  docs.deleted > 0. Not tested here: the overdue path (a job past its deadline), which is code-reviewed only.

## Amendment 10 (2026-11-16, written before the run): D4 store-set rescan-done mark (exp/d4_store_mark.py)
R=5. PASS = honest runner (every candidate reported page by page) accepted 5/5 with 12 examined; lazy runner (first
page only) refused WPS24 5/5 with the covered version unset; control (store check removed) accepts the lazy runner 5/5.
Limit stated in the receipt: the store verifies that every candidate was EXAMINED; whether an examined event matched is
still the runner's word (the store cannot decrypt).

## Amendment 11 (2026-11-16, written before the run): D3 identifier rule (exp/d3_identifier.py)
ASSUMPTION (safe default, not a user decision): single-token identifiers without a digit need >= 6 characters, and
identifiers made only of stop-list words are refused (wp/mentions.py identifier_problem). R=5. PASS = fenced 5/5:
"alex" refused, X's erasure done, Y's "Alex from accounting" event keeps its content and is not tombstoned; AND control
(rule bypassed) 5/5: Y's event tombstoned with content nulled. Span redaction is not implemented and not claimed.

## Amendment 9a (2026-11-16, after the void 481afae scale_1m run; thresholds of amendment 9 unchanged)
The scale_1m run at 481afae is VOID and not reported as a result: it erased with the default physical="followup", so
no subject could reach done, and its EXPLAIN showed that enable_seqscan = off left the overlap delete scanning the
tenant through the primary key (9.4 s per 5,000 at 100k). Fix: per-event GIN probes for the deletes and the residue
counts (measured on that tenant: 55 ms per 5,000 events; residue 5 ms against 405 ms for the 4960f50 body). The run is
repeated in full at the fixing commit with physical="wait".

## Amendment 12 (2026-11-16, written before the run): D2 verifier as a separate process (exp/d2_verifier_process.py)
R=5. PASS = fenced 5/5 (application process with only its own credential directory): cannot connect as wp_verifier;
wp_erasure_complete as wp_app refused; a dishonest runner (placeholders skipped, fabricated zero ES counts) does not
reach done; the honest runner with verifier="external" reaches done through the separate verifier process; AND control
5/5 (one credential directory for every role): the dishonest runner reaches done while X's content is still in ES.
Limits: one OS user (file permissions do not separate the processes); the runner holds the key-service credential.

## Amendment 13 (2026-11-16, written before the run): D6 per-role Elasticsearch API keys (exp/d6_es_keys.py)
Security on for this stream's node only (wp/es_security.py). R=3. PASS = fenced 3/3: P_alias, E_erase, V_merge allowed;
P_concrete, P_erase, E_concrete, E_walias, V_write refused with 403; AND control (one shared key) 3/3: every refused
case succeeds. Reported only: the projector's `manage` privilege lets it delete its family index (data loss, rebuilt
from Postgres; not a content plant).

## Amendment 14 (2026-11-16, written before the run): restored end-to-end tests (exp/e2e_restored.py)
E1 (alias fence between two projectors) and E2 (concurrent same-idempotency-key submissions, K=4 processes): N=40 per
arm, judged as amendment 3 (>= 20 overlapping runs per arm, else INCONCLUSIVE). PASS = fenced: 0 violations and 0
errors over all runs; control: >= 1 violation among overlapping runs. E1 control: alias CAS without removing the old
write alias. E2 control: the client drops the idempotency key. E2 also has a store_only arm (client pre-check removed),
N=40: >= 20 overlapping runs, 0 violations (exactly one event carries the key) and 0 harness errors, else FAIL.
E3 (replay of a decision under a new key after a lost response), R=5, sequential: PASS = fenced 5/5 (first accepted,
same-key retry returns the same event as duplicate, new-key replay refused WPS13, one decision, head version 1) AND
control (single-use source removed, F5 c1 patch) 5/5 violated. Not claimed: that a concurrent new-key replay isolates
the single-use rule (the head compare-and-set also stops it).

## Amendment 15 (2026-11-17, written before the held-out corpora were scored; new owner): held-out secret scan
Why: row 19's 880 of 880 is on a corpus the scanner's author planted; a tuned scanner elsewhere in the lab fell to 350
of 800 on held-out data. Test (exp/heldout_secrets.py), one stamped run inside run_all, no stores needed.
Scanner under test: wp/secrets.py, git blob 58b025fcb5b6266077c586436e00acf247d97854 (last changed 47a932a,
2026-11-15 23:12 -0700). The harness refuses to run against any other blob. The scanner is NOT edited after this
run; any later edit burns both sets, and a later claim needs a new held-out set.
Judge: an item is caught iff wp.secrets.scan(text) is non-empty (the same judge as row 19's planted corpus; check()
raises on exactly those texts). Negatives: a block is a scan() hit on a negative item.
Sets:
- H1, independent: exp/heldout_secrets_gen.py (sha256 bc731ed07cab94d700303a473c9a758180c85c8c1abfb775e6e00af054bf65c1,
  VERSION heldout-secrets-gen-1.0.0-2026-11-17), written on 2026-11-17 by an agent that read neither the scanner nor any
  earlier corpus, seed 16096: 1,200 positives (24 families x 50, 38 encodings) and 1,200 negatives (20 families x 60).
- H2, the ingest stream's held-out variants: the 16 HELDOUT transforms of ingest/heldout.py (feat/port-ingest blob
  3e4f3dc057d2f32480b1a8a73dd58b3f6243315f, committed a7f0529 2026-11-16 12:25 -0700, after the scanner's last change;
  the previous write-path owner's transcript never refers to it), vendored verbatim with their own generator and seed
  (n=50 per variant, seed0=9, the same text frame), 800 positives.
Scope split, fixed now from row 19's own docstring at 4960f50 ("Out-of-scope variants (reversed, ROT13, split across
two writes)"): items whose encoding is reversed (H1 reversed, H2 reversed) are DECLARED OUT OF SCOPE; everything else,
including splits inside one text (H1 split_concat_literal, zero_width_inserted; H2 hyphen_split, space_split), is in
scope. Both splits and the overall figures are reported.
PASS (the scanner generalises to held-out data) = H1 in-scope positives Wilson 95% lower bound >= 0.95 AND H2 in-scope
positives Wilson 95% lower bound >= 0.95 AND H1 negatives false-block Wilson 95% upper bound <= 0.02. Otherwise FAIL.
Control (sensitivity of the harness): the scanner with decoding off (scan(text, decode=False), row 19's ctl_nodecode)
on the H1 in-scope positives whose encoding is a value transform other than plain (base64*, hex*, url_*, json_*,
quoted_printable, html_entity_*, double_base64, base64_of_assignment): its catch rate must be at least 0.10 below the
shipped scanner's on the same items; if not, the verdict is INCONCLUSIVE unless it is already FAIL.
Reported, not thresholded: catch rate per family and per encoding (cells under 100% listed), the comparison with
row 19's 880/880, negative blocks per negative family.
Rows: set, family, encoding, context, index, caught, detector names, sha256 of the text (16 hex). Never the text or the
planted value; the harness asserts before writing that no planted value or text occurs in the result document.
