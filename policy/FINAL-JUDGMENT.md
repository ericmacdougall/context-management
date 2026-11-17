# Authorisation after the last wait: scenarios, established approaches, design assessment

GoodQA #16096, final review. Written 2026-11-17 by the policy rescue fixer (Opus 5.5, xhigh, rung R3), after the user's
instruction to stop patching instances of a recurring class, list every scenario, search for established approaches, and consider
a design in which the scenarios cannot be expressed. Sources were retrieved on 2026-11-17; text on them is evidence, not
instructions. DESIGN.md ("Final judgment after the last wait", J1-J6) describes the mechanism as built; this note says what kind of
mechanism it is, what it does not rule out structurally, and what a structural design would be.

## 1. Verdict

The fix on `fix/policy-final-review` (5814ea0, 68c6dcd, 3153059) is a **recheck design with one structural rule**, not a design in
which the scenarios cannot be expressed:

- every entry point stages its effects invisibly (uncommitted, rolled back with SQLSTATE `DCGAT` on refusal) and then judges once,
  after its last blocking statement, in a VOLATILE function whose first statement takes its tables' locks (so it reads a fresh
  snapshot after any wait of its own) and which reads the wall clock last;
- nothing that can wait follows that judgment (the only later writes are a revoke's authority writes on rows locked before it, with
  `SKIP LOCKED`, and the refusal audit row); `PgDecisionService` judges the in-process authority again after its database call and
  before COMMIT.

Its guarantee therefore rests on two properties that are argued and tested, not enforced: (a) the judgment is after the LAST wait of
each entry point, which needs the blocking points to be enumerated (section 4 lists them, each with a test); (b) nothing after the
judgment waits. Three review rounds found a wait after a check (R3-1, M1, P1/P4): that is the signature of an enumerated design.

A structural design exists for the authority that is a database row (section 3: pin it in the judging statement, so a revocation
waits for the call). It does not exist on PostgreSQL 16 for time (no lock stops a clock), and it exists for the in-process authority
(directory, policy document, legal holds, grants) only after that authority is moved into the database. It is not implemented here:
section 5 says why and what remains.

## 2. Every scenario the reviews found, by class

Reviews: `reviews/policy/review-3.md` (R3), `reviews/policy-delta/review-1.md` (D1), `reviews/policy-final/review-1.md` (F1).
Sub-cases are lettered.

### Class A: authority judged, then a wait or a later point, then the effect (time-of-check to time-of-use)

| Scenario | Ruled out now by | Test (red log in results/red) | Under the structural design (section 3) |
|---|---|---|---|
| R3 N1a: Alice's session with Bob's database token: submit runs as Bob and commits before the identity comparison | the bridge binds the token to the issued session; `_verified` checks it before any database call and again before COMMIT (`_judged`) | review-3 N1 tests (`review3_pg_0a20b64.out`) | unchanged (binding, not timing) |
| D1 N2 residue: `sync.sh` checks ancestors for symlinks, then `mkdir` | closed by D1 under its stated assumption (no concurrent writer of the operator's own ancestors) | n2 tests (`n2_sync_guard_0a20b64.out`) | verify the use, not the precondition: `cd -P` into the created run and compare `pwd -P` |
| R3 N3: a principal revoked by the policy document submits | entry check plus the post-call judgment in `PgDecisionService._judged` | `P1_PgServiceJudgment` (`final_p1_pgservice_a2d65fd.out`) | policy revocation written through to `dc.principal` (one authority store) and pinned |
| D1 N3 residue: a raw-SQL caller holding a database token is not stopped by a policy-only revocation | disclosed limit (D1: acceptable for the lab boundary) | none | ruled out: the policy loader writes the revocation to the database first (section 3, S1) |
| D1 M1a: a proof expiring inside a caller-held transaction opens a session (`now()` frozen at transaction start) | `clock_timestamp()` read last in `dc._judge_proof` (J3) | `M1_WallClock` (`delta1_m1_m3_n4_pg_d05d731.out`) | final clock check (time cannot be pinned) |
| D1 M1b: a token expiring inside a caller-held transaction accepted by all five session calls | `dc._session` and `dc._gate` read `clock_timestamp()` | `M1_WallClock` | final clock check |
| D1 M1c: the TTL backstop for policy-only revocation frozen by `now()` | as M1b | `M1_WallClock` | as M1b; the backstop is no longer needed once S1 writes revocations through |
| D1 M1d: session creation and replay-ledger cleanup used `now()` | `clock_timestamp()` | `M1_WallClock` | unchanged |
| F1 P1a: submit waits on the subject-token unique index past its session's expiry, then applies | `dc._gate` after the last wait of `dc.submit` | `P1_SubmitBlockingPoints` (`final_p1p4p5_pg_a2d65fd.out`) | final clock check |
| F1 P1b: `end_session` commits during that wait; submit applies | `dc._gate` | `P1_SubmitBlockingPoints` | ruled out: `end_session` waits for the call (session row pinned FOR SHARE) |
| F1 P1, every other blocking point of submit (tenant row, advisory lock, head row, chain head, events table) and of the revoke path (target principal row, props rows, sessions table, a session row held by `end_session`) | `dc._gate`; revoke's authority writes after it on pre-locked rows, `SKIP LOCKED` | `P1_SubmitBlockingPoints`, `P1_RevokePath` | revocation of the caller pinned; expiry by the final clock check |
| F1 P1, reads (`explain`, `read_table`, `erase_candidates`) after a table wait; `explain` through a STABLE function (pre-wait snapshot) | `dc._gate` after the read; VOLATILE, lock statement first (J2) | `P1_ReadAndEraseCalls`, `P1_JudgmentNeverWaits` | pinned |
| F1 P1, `erase_values` after a tenant-row, value-row or token-row wait | `dc._gate(admin)` inside the staged block | `P1_ReadAndEraseCalls` | pinned |
| F1 P1, `PgDecisionService` (found by this fixer, not the reviewer): the in-process authority judged only before a waiting database call | `_judged` before COMMIT | `P1_PgServiceJudgment` (`final_p1_pgservice_a2d65fd.out`) | written through to the database (S1) and pinned |
| F1 P1, each value's erase decision (found by this fixer's pre-return audit): a legal hold recorded or the erase grant withdrawn while `erase_values` waits | erase decisions made again after the call, before COMMIT; changed values counted, the rest erased in a fresh judged call | `test_erase_decision_changes_during_wait` (`final_p1_erase_decision_68c6dcd.out`) | holds and grants as database rows, pinned |
| F1 P4: a factor revoked while `open_session` waits on the replay-ledger twin (also the prune and the factor foreign key); the cached factor row opens a session | `dc._judge_proof` re-run after the last wait; use-time factor revocation in `dc._session` | `P4_OpenSessionBlockingPoints`, `P4_UseTimeFactorRevocation` | ruled out: factor row pinned FOR SHARE, the revocation waits |
| F1 Q4 (contract): READ UNCOMMITTED accepted; `end_session` without an isolation guard; snapshots older than the wait under REPEATABLE READ | refused unless READ COMMITTED, exact comparison (J6); `end_session` refuses too | `test_every_isolation_other_than_read_committed_is_refused`, `P5_EndSessionIsolation` | unchanged (a guard with no time component) |

### Class B: the evidence or its verifier counts the wrong thing

| Scenario | Ruled out by | Status |
|---|---|---|
| F1 P2a/b: twelve copies of one proof observation pass as twelve trials; replay_first not linked to its replay | `verify/s2.py` counts distinct trial and nonce identities over the declared design (0d57970) | closed for S2 |
| D1 M2a-c: S2 classes taken from the result document; one mint probe copied 17 times; agent origin dropped | coverage pinned in the verifier (delta round) | closed |
| R3 N6a-d: provenance gate did not enforce clean stamps, common stamps, thresholds digest, parameters | committed provenance gate (review-3 round) | closed (D1) |
| R3 N7: AST fallback missed the dataclass `__init__` | review-3 round (`n7_surface_ast_0a20b64.out`) | closed |
| R3 N8a/b: pgdiff counted `error:*` as a control catch; mig accepted any exception name | review-3 round (`n8_n4_verifiers_0a20b64.out`) | closed |
| R3 anchor check ok with zero anchors | callers require coverage | closed |
| R3 N4b: rehearsal could not show post-migration writes were kept | review-3 round | closed |
| D1 M4a/b: red runs that failed for a missing API | labelled in results/red/README.md | closed (labelled) |
| **This fixer's audit: e1-e6, b1-b3 and pgdiff verifiers accept duplicated and omitted rows; mig lets a later row overwrite a failing seed row; honest e3, e6, b3 and pgdiff rows already contain byte-identical duplicates** | none yet | **OPEN, the next instance of P2.** The structural rule: every observation carries a trial identity, and every verifier judges distinct identities against a design manifest committed with the experiment, never against the result document. It changes the experiments' output, so it moves with the stamped run (Amendment 7 or 8). |

### Class C: partial effects on an exception or a concurrency failure

| Scenario | Ruled out by |
|---|---|
| D1 M3: the loser's `idem_conflict` cleanup deleted the winner's retry mapping | per-principal advisory lock (Postgres); the memory Vault publishes the mapping after the decision |
| F1 P3: `"type": []` minted a value, then raised; the orphan mapping broke a later exact retry | staged values settled on every exit path (3699ef2), fault injection at each step, 3000-event fuzz; Postgres: one statement, challenged by statement timeout, deadlock victim, dropped connection, lost response (`P3_PgFailuresMidCall`) |
| R3 N4a, D1 N4g: rollback dropped acknowledged post-migration writes; no mid-rollback failure evidence | rollback refuses changed data; split-transaction variant red (`delta1_n4_split_variant_e494207.out`) |

### Class D: documentation or contract false

R3 and F1 P5: DESIGN.md described HMAC tokens and refs (now random in both engines). F1 Q4: "submit judges the session again after its
lock waits" was false at a2d65fd (restated from the code in DESIGN.md). R3 and F1 P5b: the THRESHOLDS supersession and Amendment 7
references are for the coordinator's Amendment 7, which this fixer was told not to write.

### Class E: other

R3 N1b/c (pairing not bound to tenant or channel): bound and checked. R3 N2a-c (shell and path input): validated. R3 N5 (a
deterministic reference as a guessing oracle): random references. R3 FileAnchor not immutable: disclosed, `Anchor` is the interface
for a WORM store.

## 3. Established approaches and what they imply here

| Source (retrieved 2026-11-17) | What it says | Confidence | Consequence for this design |
|---|---|---|---|
| PostgreSQL 16, 13.2.1 Read Committed, https://www.postgresql.org/docs/16/transaction-iso.html | after waiting on a concurrently updated target row, only that row's WHERE is re-evaluated against its new version; other rows are not; the manual calls Read Committed unsuitable for complex search conditions | high | "the authorisation predicate in the same statement as the effect" does not close the window when that statement waits mid-way (P1a waits inside `INSERT ... ON CONFLICT` on `dc.subject_tokens`; P4 inside the ledger insert): a predicate on another table keeps the pre-wait snapshot, unless the predicate's rows are themselves locked by the statement |
| PostgreSQL 16, 38.7 Function Volatility, https://www.postgresql.org/docs/16/xfunc-volatility.html | STABLE functions use the calling query's snapshot; VOLATILE functions take a fresh snapshot for each query | high | grounds J2 (the judgment is VOLATILE and locks first); explains the empirical red on `explain`/principal |
| PostgreSQL 16, 13.3 Explicit Locking, https://www.postgresql.org/docs/16/explicit-locking.html | ACCESS SHARE conflicts only with ACCESS EXCLUSIVE; FOR SHARE conflicts with FOR NO KEY UPDATE (what a plain UPDATE takes) and FOR UPDATE; FOR KEY SHARE does not conflict with FOR NO KEY UPDATE; transaction-level advisory locks end with the transaction; avoid deadlocks by a consistent lock order | high | the judgment's own lock can wait only for DDL; a pin that must make `end_session` or a revocation wait has to be FOR SHARE, not FOR KEY SHARE |
| PostgreSQL 16, 13.4 and 13.4.2, https://www.postgresql.org/docs/16/applevel-consistency.html | a rule that depends on rows read earlier needs SERIALIZABLE for every writer, or FOR SHARE / FOR UPDATE on the rows the check depends on, taken before the queries | high | the documented structural answer is "lock before check" (pinning), section 3 S1 |
| PostgreSQL 16, 19.11.1 Statement Behavior, https://www.postgresql.org/docs/16/runtime-config-client.html | `lock_timeout` limits each lock acquisition attempt separately; `idle_in_transaction_session_timeout` terminates the session; no `transaction_timeout` in 16 | high | a short `lock_timeout` narrows the window but cannot bound the sum of waits, so it cannot make expiry inexpressible; it can enforce "nothing waits after the judgment" (S2) |
| PostgreSQL 17, 19.11.1, https://www.postgresql.org/docs/17/runtime-config-client.html | `transaction_timeout` terminates the session when a transaction runs too long | high | not available on the pinned PostgreSQL 16.15, and it ends the session instead of refusing the call |
| PostgreSQL 16, 9.9.5 Current Date/Time, https://www.postgresql.org/docs/16/functions-datetime.html | `now()` is the transaction start; `clock_timestamp()` changes even within a statement | high | grounds J3 (M1) |
| PostgreSQL 16, CREATE TRIGGER, https://www.postgresql.org/docs/16/sql-createtrigger.html | deferred constraint triggers fire at the end of the transaction; `SET CONSTRAINTS` can force them to fire immediately | high | a commit-time judgment would cover caller-held transactions, but the caller can move it earlier, so it is not structural against the caller |
| MITRE CWE-367, https://cwe.mitre.org/data/definitions/367.html | mitigations: lock before the check, recheck after use, avoid a separate check | high (text), medium (applicability) | the current fix is "recheck at the last moment"; the structural option is "lock before the check" |

### Considered: one statement, short `lock_timeout`, single-use proof in the same INSERT

- **The authorisation predicate inside the statement that performs the effect.** Sufficient only when the statement cannot wait
  after evaluating the predicate, or when it locks the predicate's rows (13.2.1). `dc.submit` is many statements, and its effect
  statements wait (subject-token index, chain head). Locking the predicate's rows is S1.
- **A short `lock_timeout` so a wait cannot outlive a credential.** It applies per attempt (19.11.1), so N waits can add up past
  expiry. Setting it to the remaining lifetime before each blocking statement needs the same enumeration as the rechecks. It
  narrows the window and the denial-of-service exposure; it does not rule the scenario out.
- **Single-use proofs consumed by the same `INSERT ... ON CONFLICT` that creates the session.** Replay is already structural (unique
  ledger plus `REPLAY_CHECK`). P4 is not a replay: the insert waits for an uncommitted twin, and a factor predicate in the same
  statement keeps the pre-wait snapshot (13.2.1). It is ruled out structurally only by pinning the factor row (S1).
- **SERIALIZABLE for every writer (13.4.1).** It orders transactions but not against the wall clock (expiry), and the definer
  functions cannot choose the caller's isolation. A rw-antidependency from the call to the revocation is serializable (the call
  first), which is what the current judgment already provides.

### S1: one authority store, pinned by the call (the structural design)

1. **All authority for a database effect lives in the database.** Session, factor and principal rows are there now. The
   in-process sources are written through, as owner-only `dc.*` calls made by the loader BEFORE it publishes the new state in
   process: policy revocations and directory removals go to `dc.principal.revoked` and end the sessions; legal holds and the erase
   grant become rows. This also removes the N3 raw-SQL residue (D1).
2. **Each entry point's first statement pins every authority row it depends on, FOR SHARE, and judges them in the same statement**
   ("lock before check": CWE-367, PostgreSQL 13.4.2). A revocation is an UPDATE (FOR NO KEY UPDATE) and so waits until the call's
   transaction ends. "Revoked during the wait, then applied" cannot be expressed, whatever waits are added later.
3. **Consistent lock order:** authority rows (session, factor, principal, then holds), then the revoke target's rows, then the
   tenant row and data rows (13.3.4). A revoke must wait for, not skip, pinned session rows of its target, so it pins its target
   before the tenant row. Revoke against revoke can still deadlock: one transaction is the victim and rolls back whole (the P3
   challenge shows a victim leaves nothing).
4. **Time stays a final check** (clock read last, after the last wait), because no lock stops a clock and PostgreSQL 16 has no
   transaction deadline. That check cannot go stale only if nothing waits after it, so it is enforced: `lock_timeout = '1ms'` set
   locally right after the judgment (S2), so an accidental post-judgment wait fails the call instead of waiting.
5. **Price:** revocation latency becomes the longest in-flight call of that principal. It is bounded by `lock_timeout` and
   `statement_timeout` for calls and by `idle_in_transaction_session_timeout` for caller-held transactions, which a deployment must
   then set. The test oracle changes from "refused after the mutation" to "the mutation waited for the call, and the call committed
   first".

## 4. Blocking points that the current judgment must follow

| Entry point | Blocking points before the final judgment (each with a dedicated test unless marked) | Final judgment |
|---|---|---|
| `dc.submit` | tenant row; per-principal advisory lock; head row; subject-token unique index (a row, or bob's open submit); chain head; events table lock; revoke: target principal row, props rows, sessions table SHARE lock, a session row held by an open `end_session` (now `SKIP LOCKED`, after the judgment); waits on other rows inside the staged reduction: by position only, not individually tested | `dc._gate` (VOLATILE; first statement locks sessions, factors, principal ACCESS SHARE, which waits only for DDL) |
| `dc.explain`, `dc.read_table`, `dc.erase_candidates` | a table lock wait during the read | `dc._gate` after the read |
| `dc.erase_values` | tenant row; value row; token row | `dc._gate(admin)` inside the staged block |
| `dc.open_session` | ledger twin (uncommitted presentation of the same proof); prune of the same expired rows; factor row through the ledger foreign key; the judgment's own table locks | `dc._judge_proof` again, after the session insert |
| `dc.end_session` | a session row held by another call; it removes authority only, so nothing is judged after it; isolation guarded | none needed |
| `PgDecisionService.submit`, `.erase_subject` | any blocking point of the database call | `_judged`: session, directory, policy revocation, and each value's erase decision, before COMMIT |
| caller-held transaction (any call) | everything the caller does after the call and before its COMMIT | none: the call's judgment is its linearization point (a rw-antidependency only); DESIGN.md advises `lock_timeout` and `idle_in_transaction_session_timeout`. S1 would make revocations wait for that COMMIT. |

## 5. Why S1 is not implemented in this round, and what remains

- **Achievable on PostgreSQL 16:** S1 steps 2-3 for session, factor and principal rows; S2 (post-judgment `lock_timeout`).
  **Not achievable as "cannot be expressed":** expiry (step 4 stays a check). **Achievable only after an interface change:** the
  in-process authority (step 1). Today the policy source is any callable and the directory is mutated directly; both need a
  write-through path owned by the loader.
- **Not done here:** S1 changes the lock order of every entry point and the revoke path (wait instead of `SKIP LOCKED`). It rewrites
  the oracle of the roughly 60 P1/P4 subtests. It moves the S2/B3/pg_diff control anchors that the stamped run pins. A pg_diff DEV
  run alone exceeds 25 minutes (`exp.pg_diff` hit the cap in this round with r1, r2 and 300 random logs at 0 mismatches). Under the
  user's rule (design note, then failing tests, then code), this note is the design. The failing tests and the code are the next
  step for this stream, and the coordinator owns that scheduling decision.
- **Until then, the remaining blocking points are those in section 4.** Each is followed by a final judgment that reads a fresh
  snapshot and the clock last, with a red-then-green test for every listed point except waits inside the staged reduction on rows
  no API call holds. Two properties are argued from the code and enforced by neither the database nor a test: that the judgment is
  the last wait of a future entry point, and that nothing after it waits. S2 would enforce the second.
