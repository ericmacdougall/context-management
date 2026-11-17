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

## Amendment 15a (2026-11-17, before any held-out item was scored by the real scanner; thresholds of 15 unchanged)
At the coordinator's request one comment line was added to exp/heldout_secrets_gen.py naming the public source of a
documentation-example value in the negative set. The file's sha256 is now
d67e76929102a65130d0ab6db3e279300273688b6bd677f565757ebee58abeba (was bc731ed0...). The generated corpus is unchanged:
sha256 of json.dumps([positives(16096), negatives(16096)], sort_keys=True) =
2392990636495a3acd0d635e0917e337a5d8ac860b338a6e26d53edbb9d704a5 at both file versions. The harness now pins the new
file hash AND this corpus digest.

## Amendment 16 (2026-11-17, written before the run; new owner): two erasures of subjects who mention each other
Low item from the 7f6536c round (exp/mutual_mentions.py). Per run, a fresh tenant: identifiers declared for A and B
(>= 6 characters, accepted by the D3 rule); events A1 (A's own, mentions B), A2 (A's own), B1 (B's own, mentions A),
B2 (B's own), C1 (C's own, mentions nobody), C2 (C's own, mentions A and B); projected before erasure. Both erasures run
with physical="wait".
Property (per run): both jobs done with error_type None; A1, A2, B1, B2 and C2 have content NULL in Postgres and no
non-placeholder Elasticsearch document; C1 still decrypts to its original text and its Elasticsearch document is not a
placeholder.
Modes: SEQ (erase A, then B; R=5) and CONC (two real processes, each erasing one subject, held at a barrier on the
named hook after_mark until both have marked, so both cascades run at once; R=10). Overlap witness (CONC): the two
cascade intervals [barrier release, after_cascade] intersect on the database clock.
Control (one patch, reverted): wp_erasure_cascade with its mention branch removed (erasure_mentions never read), run in
SEQ mode, R=5.
PASS = SEQ holds 5/5 AND CONC holds 10/10 with >= 5 overlapping runs AND 0 errors in both AND the control violates 5/5.
CONC with < 5 overlapping runs and no violation = INCONCLUSIVE. Any error or violation in SEQ or CONC = FAIL.
Amendment 16a (2026-11-17, harness only, thresholds unchanged): a cascade that fails has no after_cascade instant; its
interval then ends when its erase() call returns (database clock), so a failed concurrent run still gets an overlap
witness. The 4f403dd shake-down scored a deadlocked concurrent run as non-overlapping for this reason.

## Amendment 17 (2026-11-17, written before the reproducer runs): lock order of erasure, holds and identifiers
Finding (shake-down at 4f403dd, PG log): two concurrent erasures of subjects who mention each other deadlock. Each
cascade batch first runs wp_hold_check (FOR UPDATE on its own subject row), then wp_erasure_apply share-locks every
subject the batch touches; A holds A and waits for B, B holds B and waits for A. The failed job was left retryable
(state marked, claim expired, last_error_type DeadlockDetected) and a later sweep finished it (done).
Suspected by reading the schema, same class: wp_identifier_declare(Y) locks subject Y FOR UPDATE then identifier_epochs
FOR UPDATE, while an append by X that mentions Y locks identifier_epochs FOR SHARE then Y FOR SHARE.
Reproducer exp/lock_order.py, deterministic: two transactions on separate connections, the interleaving forced by
waiting (pg_stat_activity, wait_event_type Lock) until the first blocked statement is really waiting. Cases:
  MM (R=10) both jobs marked; T1 runs wp_hold_check(A) then T2 wp_hold_check(B), then T1 and T2 each run one
     wp_erasure_cascade batch. Holds iff both batches commit and no DeadlockDetected.
  MK (R=5) T1 holds one open cascade batch of A (share on B); T2 claims and marks B. Holds iff T2 commits after T1
     commits, no deadlock.
  HP (R=5) T1 holds one open cascade batch of A; the legal role places a hold on B. Holds iff the placement waits for
     T1, then succeeds, and wp_hold_check(B) afterwards raises WPS16 (the hold is queued, not lost).
  HR (R=5) a hold on B exists; T1 holds one open cascade batch of A; the legal role releases the hold. Holds iff the
     release waits for T1, then succeeds, no deadlock.
  DA (R=5) T1 takes subject Y FOR UPDATE (the first lock of wp_identifier_declare) and waits; T2 appends an event by X
     that mentions Y; T1 then runs wp_identifier_declare(Y). Holds iff no DeadlockDetected (one of them may be refused
     WPS17 mention_scan_stale or succeed; both outcomes are retryable by design).
PASS (at the fixed code) = every case holds in all R runs with 0 deadlocks and 0 other errors, AND controls (one patch
each, reverted): MM with the pre-fix wp_hold_check (FOR UPDATE) deadlocks in >= 9/10; DA with the pre-fix
wp_identifier_declare lock order deadlocks in >= 4/5. Before the fix the same module is expected to FAIL (MM, and DA
if the reading is right); that run is the failing-first evidence, reported, not a PASS.
Not covered here: the policy core's revoke and tenant-authority row (DESIGN.md lock-order row); they enter the write
path with build step 3 and are checked there.
Amendment 17a (2026-11-17, before ANY lock_order run; none exists yet): two corrections to 17.
(1) DA interleaving. Pre-taking Y FOR UPDATE by hand cannot test a fix that changes that lock, so DA instead runs the
real wp_identifier_declare and stops it between its locks with a third holder: T0 (admin) share-locks the tenant's
identifier_epochs row; T1 calls wp_identifier_declare(Y) and is observed waiting; T2 appends an event by X that mentions
Y and is observed waiting or finishes; T0 commits. Holds iff no deadlock and both T1 and T2 succeed (the append's own
WPS17 retry is part of success).
(2) Fix shape and control. The fix keeps the DESIGN.md order (subjects before identifier epochs) and removes the
exclusive subject locks that formed the cycles: wp_hold_check and wp_identifier_declare take the subject row FOR SHARE
(still exclusive against wp_hold_place, wp_hold_release and wp_erasure_mark, which take it FOR UPDATE), and
wp_erasure_mark takes its own subject FOR UPDATE before the hold check (no share-to-exclusive upgrade). The DA control
is wp_identifier_declare with its pre-fix FOR UPDATE on the subject row; the MM control is wp_hold_check with its pre-fix
FOR UPDATE. Thresholds unchanged (MM control >= 9/10, DA control >= 4/5 deadlocked).
Harness rules: holder transactions stay idle well under the role's 5 s idle-in-transaction timeout; a holder that is
terminated, or any error other than the case's stated outcomes, scores the run as an error (never as holds). Deadlocks
are counted by sqlstate 40P01 on the test connections; the database deadlock counter delta is recorded as a second
witness. Waits are observed from pg_stat_activity (wait_event_type Lock) on the admin connection.
Not shown by this fixture: row-level contention inside wp_erasure_apply when both cascades touch the same events,
facts or tombstones (MM's two subjects share no event rows); stated as not verified.
Amendment 17b (2026-11-17, written after the failing-first run at eaed478 and before any run at the fixed code; main's
review questions on share locks). Failing-first record at eaed478 (pre-fix): MM deadlocked 10/10, DA deadlocked 5/5,
MK, HP, HR held 5/5 each; controls unavailable (as stated). Changes for the run at the fixed code:
Counts: MM R=30, DA R=30, controls R=10 each (MM control and DA control must each deadlock in >= 9/10). MK, HP, HR
stay R=5. Rationale: each pre-fix run of the forced interleaving deadlocked (15/15), so 30 passes of each at the fixed
code are not luck (a fix that left even a 10 % deadlock rate passes 30/30 with probability under 0.05).
New case M2 (R=10, gating): mark of B behind TWO share holders at once, an open wp_identifier_declare(B) (new
identifier) and an open wp_hold_check(B). Holds iff the mark waits, is still waiting after the first holder commits,
completes after the second commits with 0 deadlocks, the job is marked, and the job's ident_xmax includes the identifier
declared by the first holder (declare-before-mark is still seen by the mark). Not run at the pre-fix code: there the two
holders exclude each other, so the interleaving cannot exist.
New case MS (R=3, a measurement, not gating): a relay of share holders on B (wp_hold_check in open transactions, a new
one started before the previous commits, every 0.8 s for 12 s) while B's mark waits. Recorded: whether each new share
holder was granted while the FOR UPDATE waited (PostgreSQL does not queue a FOR SHARE behind a waiting FOR UPDATE when
only share locks are held), whether the mark was starved until its lock_timeout (10 s, sqlstate 55P03), and whether a
later sweep still finishes the job (retryable, not lost). Any share path behaves the same (appends, other subjects'
cascades, declares), so a starvation result applies to the pre-fix code's appends too; it is reported as a liveness
limit with its bound, not hidden.
Static part of the answer (recorded in the step-1 report, not a run): the only UPDATE of subjects is in wp_erasure_mark,
which now takes the row FOR UPDATE first in the same statement; no transaction that share-locks a subject row later
updates or exclusively locks a subject row (audit of every lock site in wp/schema.sql and every transaction in wp/).
Amendment 17c (2026-11-17, before the stamped run; written after a disclosed shake-down at 5ab1e2e with tiny counts,
whose results are not reported). The shake-down's MS run showed the mark waiting through the whole 12 s relay (24 share
grants passed it) without lock_timeout firing: each wait is on a different holder and lasts about one holder's life,
so the role lock_timeout (10 s) does not bound it. Change to MS (still a measurement, not gating): the relay runs 35 s,
longer than the role statement_timeout (30 s); recorded per run: the mark attempt's outcome (completed, or cut by 57014
statement_timeout / 55P03 lock_timeout = "starved"), its wait, the share grants that passed it, the job state right after
(an attempt cut short must leave the job requested, not lost) and after a sweep run once the relay ends.
Run validity (main, 2026-11-17): the stamped run_all starts only with >= 17 GB free on the host; this stream's own
usage (Postgres and ES data directories and WP_HOME, du) may grow by at most 2 GB, checked before every experiment
(exp/disk_gate.py); over the cap the run stops itself and is VOID. If the host falls under 15 GB free from other work
while own usage is within the cap, the run continues, but any ES flood-stage event during the run (ES then refuses
writes) makes it VOID. Postgres max_wal_size is capped at 256MB for this stream's node (min_wal_size 64MB); that is a
deviation from the earlier lab's default (1GB), stated, and does not change any threshold.

## Amendment 18 (2026-11-17, written before the reproducer runs): plan stability of the erasure path (exp/plan_stability.py)
Finding that motivates it (main's F1 request): the voided 1f059f4 run's F1 cascade took 1,410 s for 100,000 events
(63.8 s at 7e8f28f). Its batches cost 2.8 ms/row for the first 5 calls of wp_erasure_cascade, then about 30 ms/row from
the 6th, falling with the subject's remaining jobs (batch time halved exactly with batch size: 15.2/7.4/3.75/1.88 s at
500/250/125/62 rows). auto_explain on a run_all-like multi-tenant state (unstamped, ab/autoexplain-mt-cur.txt) showed
plpgsql switching wp_erasure_apply's analyst_jobs delete to a generic plan at the 6th execution: the probe reads every job
of the tenant (tenant index, materialised) and filters it per event, so a batch costs batch x tenant jobs. The
same run showed wp_erasure_pg_residue (language sql: PG16 plans it with the parameters unknown) estimating 1 tombstone
row for the subject (10,000 actual) and doing a nested-loop anti join with 49,995,000 rows removed by join filter
(17.5 s at 10,000 events), and its analyst_jobs probe intersecting the whole tenant index on every loop. The same-hour
A/B on a FRESH single-tenant database (setup --reset before each arm) did not show it (100k: 7e8f28f 58.6 s, 5fc4bd6
46.0 s, 8c7d841 36.9 s), because with one tenant the generic plan keeps the GIN probe. Code origin: 7f6536c (per-event
GIN probes), the residue form of the 4960f50 round; 7e8f28f deleted by array overlap.
Property: on the erasure path no statement's work grows with the tenant's size or quadratically with the subject's
footprint, whatever the planner's statistics and plan-cache state. Measured as WORK, not time: rows processed per
statement = sum over plan nodes of actual rows x loops, plus rows removed by filter and by join filter, read from
auto_explain (log_analyze on, log_timing off, nested statements, sent to the client as notices) on the session that runs
the cascade (one session, so plpgsql's plan cache behaves as in erasure.run). Deterministic in the plan, not in host load.
Fixture per run: 30 small tenants (20 message events each, so 20 analyst jobs each) and one tenant with subject X of M
message events (M analyst jobs) and subject Y of 5. Two statistics states:
  S1 analysed: ANALYZE of the whole database after all appends.
  S2 stale: ANALYZE after the small tenants only, before X's appends; autovacuum off on events, analyst_jobs, facts,
     tombstones and erasure_mentions for the run (restored afterwards).
Procedure: request + mark X (app role); then one admin session runs wp_erasure_cascade(job, 200) until it returns 0
(fixed batch 200, so M=3000 gives 15 data batches); then wp_erasure_pg_residue(tenant, 'X') once.
Gates (each must hold in every run of both states):
  PS-C (cascade): work per event of the analyst_jobs delete and of the facts delete in batches 6..10 <= 3x the same
       in batches 1..5 and <= 50 rows per event. (A generic plan that reads the tenant gives about the subject's
       remaining jobs per event, 1,800+ at M=3000.)
  PS-R (residue): residue work at M=3000 <= 3x the work at M=1500 (linear gives about 2x, quadratic about 4x), and
       no plan node in the residue removes more than 10 x M rows by join filter.
Sizes: M=1500 and M=3000 in each state, R=3 runs per (state, size). Controls (after a fix only): the shipped function
bodies of the pre-fix code restored by variant() must violate PS-C or PS-R in >= 2 of 3 runs per state, else the fix is
not shown. Failing-first: the same harness at 8c7d841 (pre-fix) is run first and committed as failing-first evidence.
Afterwards (measurement, not gating): one unstamped F1 at 100,000 on a multi-tenant state at the fixed code, reported
with per-batch times beside the voided run's.

## Amendment 18a (2026-11-17, harness repair before the stamped run; the property of F3 and F4 is unchanged)
At 5fc4bd6 and at 8c7d841 (reproduced in the inherited tree and the current tree, unstamped): review_fix F3 has 12/14
controls accepted and F4 crashes with done_missing_stores keys:physical_pending, which also stops F5, F7, F1S and F6
from running. Causes: (1) the zero counts the tests hand to wp_erasure_complete predate the registered store
keys:physical_pending (71b3ecc), so a correct completion is refused for a
missing store (F3 app_calls_complete control; F4 case a). (2) F3 app_sets_done's control removes only the
"only wp_erasure_complete may complete" check, but the later transition rule ("state % is written by the store only")
refuses the same update, so the control was no longer a broken implementation. Repair: the zero counts are built from
wp/registry.py (every verifiable store not of kind pg, as wp_erasure_complete requires); F4 case b still omits es:facts
on purpose; the app_sets_done control removes both owner checks of erasure_guard (one patch, reverted). Pass rules
unchanged: F3 needs 14/14 attacks refused and 14/14 controls accepted; F4 as before.

## Amendment 18b (2026-11-17, after the failing-first run at fdbecb4 and before any run at the fixed code)
Failing-first record (fdbecb4, pre-fix schema; result plan_stability.e1cf9f86e3a5c08b): PS-C violated in 6/6 runs
(S1 3/3, S2 3/3). S1: 5 rows of work per event in batches 1..5, then 1,022 to 4,022 per event from batch 6 (generic plan
reading the tenant through analyst_jobs_tenant_id_idem_key_key). S2: the custom plans of batches 1..5 already read the
tenant (3,027 to 6,042 per event), so forcing custom plans would not fix it. PS-R held 6/6 as written, but in S1 run 0
(the first run on a fresh database) the residue at M=1500 did 2,286,481 rows (about M^2, the tenant index intersected
per tombstone) while that run's M=3000 residue got a linear plan, so the ratio gate passed a quadratic plan; and the
residue's facts probe removed 5 x M rows by join filter in every run (M x the tenant's facts).
Changes for the run at the fixed code (no threshold relaxed):
(1) PS-R adds an absolute bound: residue work <= 100 rows per event (<= 100 x M) in every run; linear residue plans
    measured 25 to 35 per event at the pre-fix code.
(2) Every run starts from a fresh database (wp.setup --reset inside the run, then the control's patch, if any), so
    every run has the same statistics history, the one that produced the quadratic residue plan.
(3) Fix shape: each per-event GIN probe (cascade deletes, residue facts and analyst_jobs counts) behind an optimisation
    fence (lateral subquery with OFFSET 0, tenant filtered outside; event ids are uuids) and the residue's untombstoned
    count as an EXCEPT. Controls: CTL_APPLY restores the pre-fix wp_erasure_apply probes, CTL_RESIDUE the pre-fix
    residue (both checked to equal the pre-fix bodies apart from comments). CTL_APPLY must violate a gate in >= 2 of 3
    runs in each state; CTL_RESIDUE in >= 2 of 3 runs in S1, the state where the pre-fix residue misplanned (its S2
    count is recorded, not required).

## Amendment 18c (2026-11-17, after the run at ba71567 and before any further run; that run's PASS is void)
The run at ba71567 reported PASS (6/6, controls 3/3 in each state; result plan_stability.98a94c31b9c01856, not
reported as a result). It is void: the work metric added "Rows Removed by Filter" without multiplying by the node's
loops, and EXPLAIN prints removed rows as an average per loop. The printed plans of a fixed-code run (ab/ps_plans.py)
show the fenced probe as a Seq Scan on analyst_jobs per event (2,404 to 3,604 rows removed x 200 loops per batch):
with the tenant predicate fenced out, the planner's remaining alternative to the GIN index is a sequential scan of the
whole table, and it picked it on these small fresh tables; the residue's 0.7 s at M=3000 (against 0.02 s for the pre-fix
linear plan) was the same per-tombstone scan. The failing-first verdict at fdbecb4 is unchanged (the corrected metric
only raises those counts).
Changes (no threshold relaxed): (1) work counts each node's actual rows and removed rows x that node's loops; join
filter rows likewise. (2) Fix shape: wp_erasure_apply and wp_erasure_pg_residue run with enable_seqscan off (function
SET clause); inside the fence the only other path is the GIN index on input_event_ids, and nothing else in the two
functions needs a sequential scan of a large table (each other statement has an index on its predicate). The pre-fix
code's reason to drop enable_seqscan (7f6536c: it pushed the planner onto the tenant primary key) does not apply once
the tenant predicate is outside the fence. (3) The controls also restore the pre-fix function headers (checked equal
to the pre-fix bodies apart from comments). Gates, sizes, states, counts and control requirements as in 18 and 18b.

## Amendment 14a (2026-11-17, after the E1 run inside the VOID run_all at fd4ed53, before any further E1 run): E1 judging
This amendment changes what counts as an E1 violation. It is not a harness-only repair.

Observed at fd4ed53 (VOID run; results/run_all-fd4ed53-VOID/e2e_E1.bf4bd8db3b372a49.json):
- Fenced arm: 40 runs, 28 overlapping, 28/28 overlapping held. Seven non-overlapping runs were judged violated, and 5
  runs were errors.
- Control arm: 23/23 overlapping runs violated, and 6 runs were errors.
- That run's verdict, FAIL, stands for that run.

Cause 1, the oracle. P1 tags every document it sends. In the 7 runs, P1's bulk went out while P1 still held the lease:
P1's pre-bulk instant came before P2's fenced_at, P1 wrote 6 documents and none was refused. Its legitimate documents
were counted as stale.

Cause 2, the product. All 11 errors (5 fenced, 6 control) are P1 processes that died without reporting. Each raised
StaleEpoch (4, in families.fence_es) or StaleWrite (7, in the cursor insert) from Projector.start(), which step() calls
outside its stale-error handler. This is fixed in the product (amendment 19), not excused by the harness. A P1 process
that dies without reporting stays a harness error.

New instants, on the database clock:
- P1 bulk_done, taken after bulk_fenced returns.
- P2 cas_start, taken before start(force=True).

Each run falls in exactly one window:
- **before_takeover:** P1 bulk_done < P2 cas_start. P1 was the live writer and its documents are legitimate. Property:
  every event of the tenant is in Elasticsearch exactly once; P1-tagged documents are allowed.
- **in_flight:** P1 sent a bulk, the run is not before_takeover, and P1's pre-bulk instant < P2 fenced_at (the bulk and
  CAS intervals intersect). Judged strictly: no P1-tagged document, and every event exactly once. A P1 document here is
  a violation, never excused. These runs are counted and reported separately.
- **after_takeover:** every other run (P1's bulk sent after fenced_at, or P1 never sent one). Judged strictly.

Unchanged:
- The overlap definition: P1 read < P2 fenced_at < P1 pre-bulk instant. An overlapping run is never before_takeover.
- The thresholds of amendment 14.

Plan: one stamped E1 run at the fixed code from a pinned tree, with window counts reported per arm.

## Amendment 19 (2026-11-17, written before the test's first run): projector start race (exp/projector_start_race.py)
Property: a projector that loses the start-up race to a forced takeover drops its lease and returns -1 from step(), as
step()'s docstring states. The test is deterministic: one process, two connections, and the takeover injected by
wrapping families.fence_es. Each case runs on a fresh tenant with 6 events, R=5 times.

| Case | Takeover | Holds when |
|---|---|---|
| A | Between acquire and fence_es; P1's CAS then sees w<e+1> | P1.step() returns -1 without raising; P1 holds no lease; the lease row's holder is the taker; the taker then projects all 6 events exactly once |
| B | Between fence_es and the cursor insert; P1's insert is fenced | As for A |
| C | A fresh takeover before each of P1's fence_es calls | P1.run(max_rounds=5) returns False without raising after exactly 5 starts; P1 holds no lease; the holder is the last taker |

Control (one mechanism): Projector.step replaced by the fd4ed53 body verbatim, with start() outside the handler.

PASS = the fixed arm holds R/R in each of A, B and C with 0 errors, AND the control is violated R/R in each case.
Otherwise FAIL.

Order:
1. Failing-first: the test runs once at its own commit, before the fix. Its fixed arm must FAIL there, because the code
   under test is still the fd4ed53 step().
2. One stamped run at the fix commit.

Fix shape: step() moves start() inside its try, so StaleWrite, StaleEpoch and LeaseLost raised by start() drop the
lease and return -1. Every other exception still propagates; for example, the RuntimeError for a family with other than
one index.

Residual, reported and not fixed: a non-stale failure inside start() leaves the lease it just took held until its TTL
expires (30 s).

## Amendment 19a (2026-11-17, after the stamped run at 5d26746 and before any further run; thresholds of 19 unchanged)
Runs so far:

| Run | Commit | Fixed arm | Control | Verdict |
|---|---|---|---|---|
| Failing-first (projector_start_race.da5c9773ae7c5b9b.json) | 3c229fc | A, B, C violated 5/5 each (raised StaleEpoch, StaleWrite, StaleEpoch) | violated 5/5 | FAIL, as required |
| Stamped (projector_start_race.322d05b6f7ce312e.json) | 5d26746 | A and B held 5/5; C held 0/5 | violated 5/5 in A, B and C | FAIL, which stands for that run |

Cause, a harness defect in case C. Case C's precondition ("every start of P1 loses") was never created:
- After P1's first loss, the taker still held a live lease.
- So P1's next start() made a non-forced acquire, found that lease, and returned False without reaching fence_es.
- step() returned 0, and after two idle rounds run() returned True.
- P1 had started once, held no lease, and the holder was the taker. That is correct back-off, not the case under test.

Repair, harness only: in case C, before each of P1's own lease acquires, the harness releases the previous taker's lease
(Projector.stop). Every round P1 then really acquires and loses at the CAS.

C's property and the PASS rule are unchanged: P1.run(max_rounds=5) returns False without raising after exactly 5 starts,
P1 holds no lease, and the holder is the last taker.

Next: one stamped run at the repaired harness commit.

## Amendment 15b (2026-11-17, after the VOID run_all at fd4ed53, before any further held-out run; thresholds of 15 unchanged)
Amendment 15 says the held-out scan runs once, stamped, inside run_all. The only scoring so far was inside the fd4ed53
run_all, which is VOID (ES flood stage at 10:23Z). The held-out step itself ran at 09:41Z and needs no stores. That
scoring is recorded as an observation, not a result (results/run_all-fd4ed53-VOID/heldout_secrets.2e854942e9884360.json):

| Measure | Caught | Wilson 95% bound |
|---|---|---|
| H1 in-scope | 483/1,169 | lower 0.3853 |
| H2 in-scope | 150/750 | lower 0.1729 |
| H1 false blocks | 37/1,200 | upper 0.0422 |
| Control gap | 0.3247 | |

That run's verdict: FAIL.

The stamped held-out result is one run of exp/heldout_secrets.py outside run_all, from a pinned tree at 62c6be5, under the
same validity gates (exp/disk_gate.py start, check and end, plus the flood watcher). Nothing else changes:
- the scanner blob, 58b025fc;
- the generator hash and corpus digest of 15a;
- the H2 vendored transforms;
- the scope split, the judge, the control and the thresholds.

The scanner was not edited after the VOID-run scoring, and the harness refuses any other blob. The corpus is
deterministic, so the run re-scores the same items.
