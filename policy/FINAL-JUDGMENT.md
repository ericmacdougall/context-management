# Authorisation after the last wait: scenarios, established approaches, the S1 design

GoodQA #16096, final review. Written 2026-11-17 by the policy rescue fixer (Opus 5.5, xhigh, rung R3). Two user instructions apply:
- stop patching instances of a recurring class;
- list every scenario, search for established approaches, and rewrite so the scenarios cannot be expressed.

The first version of this note (fcc451e) judged the fix a recheck design. The coordinator then asked for the structural design. This
version is that design. It was written before its failing tests and its code, in that order: section 4 is the design, section 5 maps
every scenario and blocking point onto it, and section 6 lists what stays a recheck. Sources were retrieved on 2026-11-17; text on
them is evidence, not instructions. DESIGN.md ("Final judgment after the last wait") describes the mechanism as built.

## 1. Verdict

**S1 makes "authority changed during the wait, then the effect applied" impossible to express for every authority the database
holds.** These are the session, the factor, the principal (revoked flag, kind, admin flag), the revocation state and the grant state.

Every entry point works in three phases:

1. **Acquire.** It takes every lock it will need, in one global order. Each wait is capped by the credential's remaining life.
2. **Judge.** The authority rows are locked FOR SHARE by the same statements that judge them ("lock before check"). After that, a
   revocation, an `end_session` or a factor revocation is an UPDATE of a pinned row, so it waits until the call's transaction ends.
3. **Apply.** This phase cannot wait: `lock_timeout` is 1 ms, so a wait fails the call and nothing is written. The final judgment
   reads the wall clock last.

Two kinds of authority remain checks, each with a stated, enforced bound:

- **Time.** No lock stops a clock (section 4.4).
- **The in-process authority.** This is the in-process session, the directory, the policy document's revocations and each value's
  erase decision. It is judged again after the database call and before COMMIT, because the database cannot lock process memory
  (section 6).

## 2. Every scenario the reviews found, by class

Reviews: `reviews/policy/review-3.md` (R3), `reviews/policy-delta/review-1.md` (D1), `reviews/policy-final/review-1.md` (F1). Each
row maps a scenario to how S1 makes it unrepresentable, or to the check that remains and its test. Section 5 maps each blocking point.

### Class A: authority judged, then a wait or a later point, then the effect (time-of-check to time-of-use)

| Scenario | Under S1 | Test |
|---|---|---|
| R3 N1a: Alice's session with Bob's database token | binding, not timing: `_verified` before any database call and again before COMMIT (unchanged) | review-3 N1 tests |
| D1 N2 residue: `sync.sh` ancestor check, then `mkdir` | out of the database; closed by D1 under its stated assumption (unchanged) | n2 tests |
| R3 N3: a principal revoked by the policy document submits | recheck-only (in-process authority, section 6): entry check plus `_judged` before COMMIT | `P1_PgServiceJudgment` |
| D1 N3 residue: a raw-SQL caller with a database token after a policy-only revocation | recheck-only; stays a disclosed limit until the policy loader writes revocations through to `dc.principal` (section 6) | none (disclosed) |
| D1 M1a: a proof expiring inside a caller-held transaction opens a session | time check: `clock_timestamp()` in `dc._judge_proof`, run again as the last statement of `dc.open_session`; the acquisition budget never refuses on its own, so the `ctl_txn_clock` control still demonstrates | `M1_WallClock`, `S1_ExpiryCeiling` |
| D1 M1b/M1c: a token expiring inside a caller-held transaction | time check: `dc._session` reads `clock_timestamp()` last, inside the final judgment | `M1_WallClock`, `S1_ExpiryCeiling` |
| D1 M1d: session creation and ledger cleanup used `now()` | `clock_timestamp()` (unchanged) | `M1_WallClock` |
| F1 P1a: submit waits on the subject-token unique index past expiry, then applies | unrepresentable: the subject advisory lock (L6) is taken during acquisition, so the insert cannot wait. Raw SQL that holds the index fails the call (1 ms), nothing written. The wait at L6 is capped at expiry | `S1_PinnedAuthority.test_subject_lock`, `test_subject_token_unique_index`, `S1_ExpiryCeiling` |
| F1 P1b: `end_session` commits during that wait and submit applies | unrepresentable: `end_session` takes the principal's authority lock exclusively (L1) and so waits for the call | `S1_PinnedAuthority` (mutation `end`), `S1_MutualWaits.test_end_session_waits_for_the_call` |
| F1 P1, every other blocking point of submit and of the revoke path | each point is after the pins (unrepresentable) or before them (judged after); section 5 maps each one | `S1_PinnedAuthority`, `S1_BeforeThePins`, `S1_RevokePath` |
| F1 P1, reads after a table wait; `explain` through a STABLE function | table waits are L0, before the pins, which then judge. The read follows the pins and cannot wait (1 ms). The STABLE read no longer judges anything | `S1_BeforeThePins` (reads) |
| F1 P1, `erase_values` after a tenant, value-row or token-row wait | unrepresentable: tenant lock (L3), value rows and token row (L5), all after the pins | `S1_PinnedAuthority` (erase) |
| F1 P1, `PgDecisionService`: in-process authority judged only before a waiting call | recheck-only (section 6) | `P1_PgServiceJudgment` (adds mutation `session_ended`) |
| F1 P1, each value's erase decision changes while `erase_values` waits | recheck-only (section 6) | `test_erase_decision_changes_during_wait` |
| F1 P4: factor revoked while `open_session` waits on the ledger twin, the prune or the factor foreign key | unrepresentable: factor and principal rows pinned (L2) before the nonce lock (L8). The revocation waits for the call; the prune is `SKIP LOCKED`; the foreign key needs KEY SHARE, which the pin already covers | `S1_OpenSession` |
| F1 Q4: isolation other than READ COMMITTED | refused before the first lock, so a FOR SHARE pin under REPEATABLE READ can never raise 40001 (unchanged guard, moved first) | `test_every_isolation_other_than_read_committed_is_refused`, `P5_EndSessionIsolation` |

### Class B: the evidence or its verifier counts the wrong thing

| Scenario | Ruled out by | Status |
|---|---|---|
| F1 P2a/b: twelve copies of one proof observation pass as twelve trials | `verify/s2.py` counts distinct trial and nonce identities (0d57970) | closed for S2 |
| D1 M2a-c, R3 N6-N8, anchors, R3 N4b, D1 M4 | earlier rounds | closed |
| This fixer's audit: e1-e6, b1-b3, pgdiff and mig verifiers accept duplicated, omitted or overwritten rows | `fix/policy-evidence-identity` (53b85d8): one shared evidence layer (`verify/evidence.py`). Declared identities; the expected set derived from the thresholds file and the seeds; exact-set judging; property tests (drop, duplicate, relabel and reorder each yield non-PASS) | merged into this branch with the stamped run (Amendment 7) |

### Class C: partial effects on an exception or a concurrency failure

| Scenario | Ruled out by |
|---|---|
| D1 M3: the loser's `idem_conflict` cleanup deleted the winner's retry mapping | per-principal advisory lock (L4) |
| F1 P3: `"type": []` minted a value, then raised | staged values settled on every exit path; Postgres: one statement (`P3_PgFailuresMidCall`). Under S1 a wait in the apply phase also fails the call whole |
| R3 N4a, D1 N4g: rollback dropped acknowledged writes | rollback refuses changed data (unchanged) |
| New with S1: a deadlock between calls in one transaction, or with raw owner SQL (section 4.5) | the deadlock victim rolls back whole (`test_deadlock_victim_mid_call`) |

### Class D: documentation or contract false; Class E: other

These scenarios are unchanged from fcc451e: DESIGN.md token claims, the "submit judges again" statement, and the Amendment 7
references (D); pairing binding, shell input, random references, FileAnchor (E). DESIGN.md and the `_pgcore.sql` header are rewritten
for S1 in the same change as the code.

## 3. Established approaches and what they imply here

| Source (retrieved 2026-11-17) | What it says | Confidence | Consequence |
|---|---|---|---|
| PostgreSQL 16, 13.2.1, https://www.postgresql.org/docs/16/transaction-iso.html | `SELECT FOR SHARE` / `FOR UPDATE` that waits on a concurrently updated row re-checks its WHERE against the newest version and locks that version | high | a pin that waited judges the version it locked; a pin that did not wait locked the newest version |
| PostgreSQL 16, 13.4.2, https://www.postgresql.org/docs/16/applevel-consistency.html | for a check-then-act rule, take FOR SHARE / FOR UPDATE on the rows the check depends on before the queries | high | S1 is this rule |
| PostgreSQL 16, 13.3, https://www.postgresql.org/docs/16/explicit-locking.html | FOR SHARE conflicts with FOR NO KEY UPDATE (a plain UPDATE) and FOR UPDATE; FOR KEY SHARE does not; avoid deadlocks by acquiring locks in a consistent order; advisory locks are released at transaction end | high | pins are FOR SHARE (a KEY SHARE pin would not stop an UPDATE); one global order (section 4.2) |
| PostgreSQL 16 lock manager README, https://raw.githubusercontent.com/postgres/postgres/REL_16_STABLE/src/backend/storage/lmgr/README | a new request goes to the end of the wait queue and is granted only if it conflicts neither with granted locks nor with earlier waiters. A process that already holds a conflicting lock is inserted ahead. The deadlock detector may reorder a queue to break a soft deadlock | high | an advisory lock is served FIFO, so a queued exclusive request is not starved by a stream of shared requests. A row lock gives no such queue to FOR SHARE requests, which join a share-lock set while an UPDATE waits. Hence the authority lock and the tenant lock are advisory, and the tenant row is no longer locked |
| PostgreSQL 16, 19.11.1, https://www.postgresql.org/docs/16/runtime-config-client.html | `lock_timeout` limits each lock acquisition attempt; `idle_in_transaction_session_timeout` terminates the session; all are user-settable; no `transaction_timeout` in 16 | high | the budget is set before every acquisition statement, and each such statement waits for at most one lock (section 4.4). The idle timeout bounds only cooperating callers |
| PostgreSQL 16, 9.28.2, https://www.postgresql.org/docs/16/functions-admin.html (retrieved 2026-11-18) | `pg_terminate_backend(pid, timeout)` waits up to `timeout` ms for the process to end; a member of `pg_signal_backend` may terminate other backends, but only superusers can terminate superuser backends | high | eviction (4.4) runs as dc_evictor, member of `pg_signal_backend` only, and cannot end a superuser holder; such a holder makes the revocation give up |
| PostgreSQL 17, 19.11.1, https://www.postgresql.org/docs/17/runtime-config-client.html (retrieved 2026-11-18) | `transaction_timeout` (new in 17) ends a session whose transaction runs longer than the setting; default 0 | high (text); medium that it is user-settable (the page names no restriction, as for the other client timeouts) | not available on 16.15, and a holder could disable it like the idle timeout (4.4 options) |
| PostgreSQL 16, CREATE FUNCTION, https://www.postgresql.org/docs/16/sql-createfunction.html | a SET clause saves the setting on entry and restores it on exit; SET LOCAL inside such a function is confined to it | high | the caller's `lock_timeout` is restored on every exit path, refusals and errors included |
| PostgreSQL 16, 38.7, https://www.postgresql.org/docs/16/xfunc-volatility.html | STABLE functions read with the caller's snapshot; `SELECT FOR SHARE` is not allowed in them | high | pins live in VOLATILE functions; the STABLE `dc._explain_raw` only reads after them |
| PostgreSQL 16, 9.9.5, https://www.postgresql.org/docs/16/functions-datetime.html | `now()` is the transaction start; `clock_timestamp()` advances within a statement | high | the clock reads (M1) |
| PostgreSQL 16, CREATE TRIGGER, https://www.postgresql.org/docs/16/sql-createtrigger.html | `SET CONSTRAINTS` can fire deferred triggers early | high | a commit-time judgment is not structural against the caller (not used) |
| MITRE CWE-367, https://cwe.mitre.org/data/definitions/367.html | lock before the check; recheck after use | high (text), medium (fit) | lock before check for database authority; recheck after use for in-process authority |

Alternatives considered and rejected are as in fcc451e: the predicate inside the effect statement, a short global `lock_timeout`,
single-use proofs in the same INSERT, and SERIALIZABLE for every writer. None orders an effect against a revocation that commits
during one of the effect's own waits.

## 4. S1 as designed

### 4.1 Phases

- **Before any lock.** The isolation guard runs first: anything other than READ COMMITTED is refused before the first lock. Then
  `dc._peek` reads the session row by token, unlocked and unjudged, only to learn whose authority to lock (an early refusal for a
  missing, revoked or already expired row is a refusal only). `open_session` peeks the factor row the same way.
- **Acquire.** Every lock of the call is taken in the order of section 4.2: one object per statement. `dc._budget` runs before each
  statement and the run-time order check `dc._at` before each lock. The authority rows are among these locks, and each locking
  statement also returns the flags it locked, so the judgment reads exactly the locked versions. Any wait in this phase ends at the
  credential's expiry at the latest (section 4.4).
- **Apply, then judge.** `lock_timeout` drops to 1 ms. The call stages its effects (refusal audit rows included). Then the final
  judgment `dc._gate` runs: the pinned flags, and `clock_timestamp()` read last inside `dc._session`. A refusal rolls the stage back
  (SQLSTATE `DCGAT`). After the judgment, only a revoke's own authority writes run, on rows locked during acquisition, still under
  1 ms. So a self-revoke is not refused by its own write.
- **Exit.** The inner function has a SET clause for `lock_timeout`, so the caller's value is restored on every exit.

### 4.2 Global lock order

Levels are taken in increasing order. Inside a level, keys are taken in increasing order, and a key needed in two modes is taken
once in the stronger mode. `dc._at(level, key)` raises SQLSTATE `DCORD` if a call ever takes a lock below one it already took, and it
records the sequence in the transaction-local setting `dc.trace`, which the enumeration test reads.

| Level | Resource | Shared by | Exclusive by |
|---|---|---|---|
| L0 | relation locks, one table per statement, alphabetical (audit, chain, chainhead, events, factors, head, principal, props, role, sessions, subject_tokens, tenant, used_proofs, vals), in the strongest mode the entry point needs | every entry point | (DDL only) |
| L1 | authority lock A(tenant, principal): advisory, two-int4 key (class 1145241601, `hashtext`), sorted by key | every call by the principal; `open_session` for the factor's principal | a revoke of the principal; `end_session` of one of its sessions; `dc.revoke_factor` |
| L2 | authority rows, one row per statement: 2.0 caller's session, 2.1 caller's factor, 2.2 caller's principal (FOR SHARE); 2.3 revoke target's principal row, 2.4 revoke target's session rows by token hash (FOR NO KEY UPDATE) | pins | a revoke, on its target |
| L3 | tenant lock T(tenant): advisory, class 1145241602 | ordinary submits | revoke, grant, `erase_values` |
| L4 | per-principal idempotency lock (int8 advisory, unchanged) | | every submit |
| L5 | data rows under the tenant lock, one per statement: 5.0 the event's head row; 5.0 a revoke's head rows by requirement, 5.1 its target's open props rows; 5.2 `erase_values`' value rows by reference, 5.3 its token row | | FOR UPDATE |
| L6 | subject locks S(tenant, subject): advisory, class 1145241603, sorted | | a submit carrying a value, one per subject |
| L7 | the tenant's chain-head row | | every submit (session path; the owner-only raw path takes it at the event insert, as before, so the B3 controls still demonstrate) |
| L8 | nonce lock N(factor, nonce): advisory, class 1145241604 | | `open_session` |

Not a level: the marker M(k) of section 4.4 (advisory class 1145241607, session-level, SHARED only) is taken by a call before it
requests A or T exclusively and released on every exit of that call. No one ever takes it exclusively, so no request for it waits;
it only tells `dc._evict` that its holder is a call in progress on k (review of 6b77354 N1).

| Entry point | Sequence |
|---|---|
| `dc.submit` (propose, decide, supersede, resolve, retract, remark) | L0 (the 12 tables it can touch, `tenant` and `used_proofs` excepted: row exclusive; factors, principal, sessions row share); L1 A(caller) sh; L2.0-2.2; L3 T sh; L4; L5.0 head(req); L6 S(subjects); L7 |
| `dc.submit` revoke by an admin | L0 (principal and sessions row exclusive); L1 A(caller) sh and A(target) ex, sorted (a self-revoke takes ex only); L2.0-2.4; L3 T ex; L4; L5.0 and 5.1; L7 |
| `dc.submit` grant by an admin | L0; L1 A(caller) sh; L2.0-2.2; L3 T ex; L4; L5.0 head(req); L7 |
| `dc.submit` revoke or grant by a non-admin | as an ordinary submit (A(caller) sh, T sh): the rules refuse it, so it takes no exclusive lock to stall others with. The admin flag that decides this is read unlocked after L0; the API never changes it, and if owner SQL did during the call, the pinned flag differs and the call is refused |
| `dc.explain`, `dc.read_table` | L0 (row share: factors, principal, sessions; access share: head, props, role, vals); L1 sh; L2.0-2.2 |
| `dc.erase_candidates` | L0 (factors, principal, sessions row share; vals access share); L1 sh; L2.0-2.2 (admin judged on the pinned row) |
| `dc.erase_values` | L0 (audit, subject_tokens, vals row exclusive; factors, principal, sessions row share); L1 sh; L2.0-2.2; L3 T ex; L5.2 by reference; L5.3 |
| `dc.open_session` | L0 (factors, principal row share; sessions, used_proofs row exclusive); L1 A(factor's principal) sh; L2.1-2.2 inside `dc._judge_proof`; L8 |
| `dc.end_session` | L0 (sessions row exclusive); L1 A(session's principal) ex; then the UPDATE of its session row (recorded as L2.4) |
| `dc.revoke_factor` (owner-only, new) | L0 (factors row exclusive); L1 A(factor's principal) ex; then the UPDATE of the factor row |
| `dc._apply_event` (owner-only raw path: B3, pg_diff) | L1 A(target) ex (revoke); L2.3-2.4 (revoke); L3; L4; L5; no budget (the owner's own `lock_timeout`) |

### 4.3 What the pins cover

- **Session row** (`revoked`; `expires_at` is never updated by the API) and **factor row** (`revoked`): pinned at L2.0-2.1. A
  session is live only while its factor is unrevoked (use-time factor revocation), and that check reads the pinned factor row.
- **Principal row** (`revoked`, `kind`, `admin`): pinned at L2.2. No API statement changes `kind` or `admin`. `revoked` goes from
  false to true only in `dc._apply_authority`, on a row the revoke locked at L2.3.
- **Revocation state of others**: a revoke that changes a requirement a call depends on (a closed proposal of the target) takes T
  exclusively, and every submit holds T shared. So no revoke commits during a submit's call.
- **Grant state**: `dc.role` is add-only. The only write is `insert ... on conflict do nothing` in `dc._apply_delta`: no statement
  updates or deletes it, and a test enumerates the SQL to check this. A grant takes T exclusively, so none commits during a submit.
  Reads do not take T. Because grants only add, a read that misses a concurrent grant is equivalent to the read running first.
- **Direct owner SQL** on a pinned row (UPDATE or DELETE) also waits for the pin, because the row lock is the pin. It is not queued
  behind other callers; `dc.revoke_factor` is the queued (FIFO) path for factor revocation.

### 4.4 Bounds

**Expiry.** Let E be the credential's expiry: the session's `expires_at`, or the proof's `expires` for `open_session`.

- **The cap.** Before every acquisition statement, `dc._budget` sets `lock_timeout` to the smaller of:
  - the caller's own `lock_timeout` when it is set;
  - `greatest(1, ceil(E - clock_timestamp()))` ms.
  Each acquisition statement waits for at most one lock, so no wait ends later than E + d, where d is the server's timer latency.
  After E, every wait is limited to 1 ms. A lock timeout after E is a refusal (`unauthenticated:invalid_session`, or
  `proof refused: time`). A timeout before E is the caller's own, and is re-raised. `dc._budget` never refuses by itself, so the
  only expiry judgment is the clock read last.
- **A refused call** returns by E + d + 1 ms, plus the CPU time of statements that do not wait.
- **An applied call** read the clock in its final judgment before E. After that read come only a revoke's two authority UPDATEs, on
  pre-locked rows under 1 ms, and the return. So no effect is judged at or after E, and its own work after E is CPU time of at most
  two UPDATEs.
- **Excluded:** the caller's COMMIT. A caller-held transaction makes the effects visible when it commits; they were judged before
  E, and revocations wait for that COMMIT. Also excluded: WAL flush and replication waits at COMMIT, and `statement_timeout`, which
  is unchanged.
- **The token lookup.** `dc._peek` runs before E is known, so its one possible wait is bounded only by the caller's own
  `lock_timeout`. It can wait only for a relation lock on `dc.sessions` that conflicts with ACCESS SHARE, which no API call takes:
  owner DDL or `LOCK TABLE ... ACCESS EXCLUSIVE`. A call that waited there and finds the token expired is refused at once, with
  nothing applied (`P1_JudgmentNeverWaits` covers the wait). So "returns by E + d" holds for every wait except this one, and "no
  effect after E" holds for all of them.

**Exclusive waits, revoke wait and tenant stall.** A request for A or T exclusive also uses XWAIT = 2 s (`dc._xwait_ms()`) as a
further cap. Past it, the call fails with SQLSTATE 55P03: nothing is applied, it is retryable, and the queue behind it is released.

- **A revoke** of P by X (expiry E_X) waits:
  - at A(P) for the transactions holding A(P) shared: P's in-flight calls, and transactions that made a call as P and have not
    ended. Requests that arrive later queue behind it (FIFO), so no stream of calls can starve it;
  - at A(X) only for an exclusive holder (a revoke of X, or an `end_session` of X's session, each capped at XWAIT);
  - at T for the in-flight calls of the tenant, FIFO;
  - at L2.3-2.4 and L5 only for direct owner SQL, because API writers of these rows hold A or T.
  So a revoke applies, refuses at E_X, or fails after XWAIT at one of its two exclusive locks. It waits at most 2 x XWAIT for
  in-flight calls, plus raw-SQL row waits up to E_X.
- **Holders.** An in-flight call ends by its own expiry ceiling plus apply CPU time. A caller-held transaction ends when the caller
  commits, after `idle_in_transaction_session_timeout` = 60 s if the caller leaves that setting alone (`pol.pgcore.install` sets it
  as the role default for dc_app and dc_idp), or when a revocation evicts it (below).
- **Tenant stall.** While a revoke, grant or erasure is queued for T, new submits of the tenant queue behind it. That lasts at most
  XWAIT per attempt.
- **`end_session`** waits FIFO, at most XWAIT, for the in-flight calls of the session's principal.
- **Stalled holders.** This was the open residual of this section. It is now closed by eviction, which is an ASSUMPTION: the policy
  owner's choice, not the user's.
  - **The problem.** After a call, a dc_app connection keeps the call's locks until its transaction ends: A shared, the pinned rows,
    and for a submit also T shared and the later locks. Three ways keep that transaction open past the 60 s idle timeout:
    - resetting the timeout (it is user-settable);
    - a long statement (`pg_sleep`; `statement_timeout` is user-settable too);
    - a stream of statements, so the transaction is never idle.
  - **At 8547b27.** While such a holder held, every revocation of its principal (revoke, `end_session`, `dc.revoke_factor`) failed
    with 55P03 after XWAIT. While a submitter held T, every revoke, grant and erasure of its tenant failed the same way.
  - **Options.** The table below gives each option with its measured bound. Measurements are from t2-dev, PG 16.15.
    `tests/test_s44_pg.py` prints each number as a `MEASURE` line; the logs are in results/red/.

  | Option | What it bounds | Measured | Decision |
  |---|---|---|---|
  | Nothing (8547b27) | Only holders that end: FIFO queues the revoke ahead of later shared requests | Stalled holder, all 4 ways: 55P03 after 2.00-2.01 s on every attempt, holder still connected. Reconnecting adversary with 1 s holds: revoked in 0.53 s | Superseded |
  | Timeouts (role defaults) | Only a holder that leaves its settings alone | Role default set to 3 s: a cooperating holder was ended after 2.83 s. A holder that ran `set local idle_in_transaction_session_timeout = 0` was still connected after 8.00 s | Kept at 60 s as defence in depth; not a bound |
  | `transaction_timeout` (PostgreSQL 17) | The same as timeouts: it is not restricted to superusers | Not measurable: the server is 16.15 and has no such setting | Not available here, and not a bound on 17 either |
  | Autocommit-only API (refuse calls inside an explicit transaction) | Nothing against one statement: `select dc.explain(t, 'r1'), pg_sleep(3600)` holds the call's locks until the statement ends | Not built; reasoned from the statement-scoped lock lifetime | Not a bound |
  | Revocation epoch judged at use time, so a revoke need not wait for holders | It would let a revoke commit while a holder's call is still open. That reverses S1's "the call commits first" order (section 4.1) and needs its own proof | Not built, not measured | Not taken |
  | **Eviction (built)** | Every non-owner, non-superuser holder of the needed lock that is not itself a revocation in progress | One stalled holder (idle_reset, sleep, stream, submit): revoked in 2.11-2.12 s, holder ended. `end_session` 2.11 s. `dc.revoke_factor` 2.11 s. Tenant-lock holder: 2.12 s under a revoke, 2.11 s under an erasure. With bystanders: 2.12 s, and bob's holder, an idle dc_idp and another database's holder were all kept. Control with `dc._evict` made a no-op: 55P03 after 2.01 s, holder still connected | **Taken (ASSUMPTION)** |

  Eviction as built (`dc._xlock`, `dc._xlock_caller`, `dc._evict`; see DESIGN.md "Stalled holders"):
  - **When.** Eviction runs only when an exclusive request for A or T times out at XWAIT. It also runs only after the requesting
    call's own authority is settled:
    - A revoke takes A(target) before its pins. So the caller is judged first, in a sub-block that is then rolled back: A(caller)
      shared, its pins, its pinned admin flag, and the clock read last.
    - `end_session`, `dc.revoke_factor`, and the tenant lock of a revoke, grant or erasure come after their pins and judgment
      already.
    - A refused revoker evicts nobody (`S44_Scope`).
  - **Whom.** An exclusive request E on key k (A or T; F is k's fence) ends a backend B only if all of these hold, each read from
    `pg_locks` (review of 6b77354 N1, N3, N4; the rule before it is described, and refuted, below):
    - B belongs to THIS database, is not the caller, and its session user is not a superuser and not a member of dc_owner (review
      of 2262eaa F2b: until then eviction matched the user NAMES dc_app and dc_idp, so a login role that is a MEMBER of dc_app,
      the one-role-per-service pattern, was never evicted and made its principal unrevocable; `F2b_MemberRole`);
    - (a) B held the lock E evicts for (k, or F when E could not take the fence), GRANTED, in transaction X, when E's request
      began: `dc._snap` records `pid:virtualtransaction` of the granted holders just before each request. A backend that queued
      behind E is not in it;
    - (b) B still holds it, granted, in the same X, re-checked for B immediately before its signal. So B held it for at least
      XWAIT within one transaction;
    - (c) B is not a call in progress on k: it does not hold the marker M(k) (class 1145241607, key k, session-level SHARED, taken
      by `dc._xlock` and `dc._xlock_caller` before they request k exclusively, released by `dc._unmark` on every exit of
      `dc.submit` (revoke or grant), `dc.end_session`, `dc.revoke_factor` and `dc.erase_values`, from their exception handlers
      too) together with k or F EXCLUSIVELY, granted.

    The states of a call marked for k, and what (c) does with each: waiting for k and holding nothing of it (not a holder, never
    ended); holding only F while it judges or evicts, including the 100 ms polls of `pg_terminate_backend` (spared); holding k
    exclusively, parked on anything (a row lock, T, an eviction) (spared); returned with its transaction left open (unmarked: a
    stalled holder, ended, `test_a_stalled_applied_revoke_is_still_evicted`). Two calls marked for the same key never end each other;
    the later one gives up with 55P03 (retryable). A marker of another key spares nothing. A marked backend that holds k only SHARED
    is ended (`N1_ModeRule`). Waiters, exclusive or shared, are never ended (`test_a_queued_revoker_is_not_evicted`). Owner,
    superuser and other-database backends are never ended. A round that signals nobody gives up with 55P03 when every current holder
    is spared (measured locally: about 2 s against an owner holder), and otherwise waits another round, whose snapshot includes a
    holder that came after the last one.

    The rule this replaces (review of 2262eaa H1, at 6b77354) spared an exclusive holder that was "inside the protocol", read as:
    waiting for an A, T or fence lock, or `wait_event = 'BackendTermination'`. `dc._evict` runs as dc_evictor, which cannot read
    `wait_event` of other roles' backends (it is NULL without `pg_read_all_stats`), so that clause never spared anyone, and a
    revocation that was evicting, running or parked on a row lock was ended by a later `end_session`, `dc.revoke_factor` or second
    revoke of the same target, and the revocation was lost (review of 6b77354 N1: 10 of 10 trials; `N1_ParkedRevocation`, red on
    6b77354 in all six state and trigger combinations). The same rule spared ANY backend waiting for any A, T or fence lock, so an
    admin holding T(a) exclusively while waiting for T(b) froze tenant a (N4, `N4_ParkedExclusive`), and it ended every ShareLock
    row, waiters included, so a submit that queued behind a revoke was ended after it committed (N3, `N3_InnocentWaiter`).
  - **Lock identities (review of 2262eaa F1, F5).** Until then A and T used `hashtext(kind, tenant, name)`, one 32-bit space for
    every tenant, and an admin's revoke took A(target) exclusively for ANY target string. A tenant admin could search names offline
    whose key equals another tenant's principal, revoke one, and evict that principal's backends and fence its calls; a subject of
    one tenant could stall another tenant's submits; a grant or revoke the rules refuse still took T exclusively and evicted the
    tenant's in-flight submitters. ASSUMPTION (the fixer's choice, not the user's):
    - A and its fence use the principal row's `lockid`, T and its fence the tenant row's `lockid`: one sequence
      (`dc.lockid_seq`, 1 to 10^9, no cycle), unique, fixed at insert by a trigger (`dc._ak`, `dc._tk`). Two different principals
      or tenants never share an A or T key, so no tenant can evict, fence or stall another through A or T, by search or by chance.
    - The subject lock S is classed by tenant (class = -lockid of the tenant), so subjects of different tenants never meet. Within a
      tenant the subject key is still `hashtext` (32 bits): two subjects of ONE tenant collide with probability 2^-32 per pair, and
      a collision is a wait under the caller's own budget, never an eviction (S is not evictable).
    - Unchanged and disclosed: the nonce lock N (32-bit hash of a random nonce: a collision is a short wait) and the idempotency
      lock L4 (`hashtextextended`, 64 bits, over (tenant, principal): 2^-64 per pair, a wait only). Neither is evictable.
    - A revoke takes A(target) exclusively, and a revoke or grant takes T exclusively, only when an unlocked pre-read predicts that
      the call would apply it. Review of 2262eaa F5 checked only the rules' facts (target exists; for a grant, the requirement
      exists and the target is not revoked). Review of 6b77354 N2 showed that six other refused shapes still took A and T
      exclusively and evicted a bystander (a bad client_id, an unknown key, a label without a value, a grant with role owner, an
      exact retry, a reused idempotency key), so the claim made here before ("refused without waiting for or evicting anyone") was
      false for them. The pre-read now also runs the stage's body checks (`dc._body_refusal`, the same function the stage runs),
      `dc._shape` of the event the stage builds, the idempotency lookup (an earlier event with the same key means `duplicate` or
      `idem_conflict`) and the caller's own revocation. Every refusal the code can return for an admin's revoke or grant is listed,
      with its source, in `N2_RefusedShapes`; each is refused without an exclusive request, so it waits for no exclusive holder and
      evicts nobody (`N2_RefusedShapes`, `F5_RefusedAuthorityChange`). If an authority change nevertheless applies without its
      exclusive lock (the target or requirement appeared between the pre-read and the rules), the call fails with 55P03 and
      applies nothing.
    - Residual: the prediction comes from an UNLOCKED pre-read. A change the pre-read predicts will apply, and that is refused
      because the state changed during its wait (only owner SQL can revoke a target or remove a requirement), still took the
      exclusive locks and may have evicted (review of 6b77354 probe X (b)).
  - **Prepared transactions (review of 2262eaa F2a).** A prepared transaction keeps its locks with no backend, so eviction cannot
    end it (`ROLLBACK PREPARED` needs an operator and cannot run inside a transaction block). ASSUMPTION (the fixer's choice): fail
    closed. `pol.pgcore.install` refuses a server with `max_prepared_transactions > 0`, and every API call (`dc._no2pc`) refuses
    with SQLSTATE 55000 while it is > 0 (it changes only at restart), so no transaction that made an API call can be prepared
    (`F2a_PreparedTransactions`). The cost: the policy API cannot run on a cluster that uses two-phase commit.
  - **How.** `dc._evict` calls `pg_terminate_backend(pid, 1000)`.
    - `dc._evict` is SECURITY DEFINER and owned by the NOLOGIN role dc_evictor.
    - dc_evictor is a member of `pg_signal_backend` and of nothing else.
    - Only dc_owner may EXECUTE `dc._evict`.

    The terminated backend's transaction rolls back whole, so it never commits a later effect.
  - **Fence.** While a revocation evicts, it holds the fence lock of that key (F_A 1145241605, F_T 1145241606), scoped to its
    transaction. It takes the fence BEFORE its judgment and its first eviction on every path (review of 2262eaa F3: the first
    round of a revoke's A(target) evicted without it, so a holder that reconnected at once re-took the lock behind the eviction and
    answered; `F3_FenceBeforeEviction`). When another backend holds the fence, the round evicts that backend if it is a stalled
    holder of the fence (by the same rule: never a call in progress marked for that key, `N1_ParkedRevocation` fence-only cases)
    and tries again next round. A call about to take that A or T shared first reads `pg_locks`. If the fence is held, the call refuses with 55P03
    and does nothing (`S44_Fence`: under F_A the target's explain, submit and open_session refuse within 0.5 s and bob's calls
    answer; under F_T submits refuse and reads answer; control `S44_FenceControl` with `dc._fenced` a no-op). It never waits for
    the fence, so the lock order of 4.2 is unchanged for every call that does not evict. As a result, a later round can meet only
    holders that passed the fence check before the fence was taken. Only an evicting call takes a fence, and only after its
    request at that level timed out, holding nothing above that level. A second evictor of the same key waits for the first one's
    fence, FIFO, under the same budget (`dc._budget`, XWAIT). The fence is not recorded in `dc.trace`, and the deadlock detector
    covers it like any advisory lock.
  - **Raw advisory calls.** The advisory-lock functions are PUBLIC by default, so a login role could take A, T, a fence or an
    idempotency lock with a raw call. `pol.pgcore.install` revokes EXECUTE on every `pg_catalog` advisory-lock function from PUBLIC
    in the policy database, and from every other explicit grantee except the owner (review of 2262eaa F8: an explicit grant to
    dc_app, or to a role dc_app is a member of, survived the PUBLIC revoke), grants it to dc_owner, and refuses to finish if dc_app
    or dc_idp can still execute one (`test_login_roles_cannot_take_advisory_locks`, `test_install_removes_explicit_advisory_grants`).
    The revoke is database-wide: any other code in that database that takes advisory locks as a login role loses them (Flyway,
    Liquibase, Rails and other tools that lock in the same database; disclosed, not fixed). `ops/migrate_r3_7` creates dc_evictor
    on forward and records the advisory ACL it found; its rollback restores exactly that ACL (an older mark without one falls back
    to PUBLIC), because the pre-R3-7 engine takes its idempotency lock as dc_app with a raw call
    (`test_44_rollback_gives_the_old_engine_its_advisory_lock`, `test_rollback_restores_the_recorded_acl`,
    `test_44_forward_creates_the_evictor_role`).
  - **Bound.** Against holders it may evict, a revocation applies within one round: XWAIT, plus up to 1 s per evicted backend,
    plus its own apply. It runs at most three rounds, starts no round at or after its caller's expiry E, and applies nothing
    after E (`dc._budget`, the final judgment). Otherwise it fails with 55P03 and applies nothing. The evictions of a round
    already started can finish after E, at most 1 s per backend: ending a stalled holder is not an effect of the revocation.
    `end_session` and `dc.revoke_factor` have no caller expiry; they are bounded by the three rounds. Measured with one holder:
    2.11-2.12 s.
  - **Residuals (disclosed).**
    1. A backend ended by eviction loses its whole connection and its open transaction, including any unrelated work on it. For T
       this can be a legitimate submit of another principal, but only one that held T for at least XWAIT within one transaction
       (rules (a) and (b) above). Correction (review of 6b77354 N3): at 6b77354 this residual said the same, but the code ended
       every ShareLock row, waiters included, so a submit that queued behind a revoke, was granted T for under 0.1 s and committed,
       had its connection terminated afterwards. Remaining window: B can commit between the re-check (b) and the signal, a few
       microseconds; then B's committed work stands and its connection is ended (57P01) before its next statement.
    2. While a revocation evicts, new calls are refused with 55P03 (retryable): the target's calls under fence F_A, or the tenant's
       calls under fence F_T.
    3. A stalled submitter also holds the chain-head row (L7). Other submits of its tenant wait until their own expiry and are
       refused. This lasts until a revoke, grant or erasure of that tenant evicts the staller through T, or a revoke of the staller
       evicts it through A. It is a denial of service ended by an operator's action, never a wrong decision. It is not measured.
    4. dc_owner may call `dc._evict` for any A or T key. dc_owner is the trusted owner role.
    5. Correction (review of 6b77354 N1). This residual said that a revocation in progress was exposed only "between two waits (a
       few microseconds)". That was false: the exemption it described never worked (dc_evictor cannot read `wait_event`), and a
       revocation was exposed for its whole eviction loop (about 100 ms per stalled holder) and whenever it was parked on a row lock.
       Now a call in progress is recognised by its marker, which does not depend on what it is doing at that moment. What remains:
       a marker is released by the API function's exception handler, so it can leak only if an error escapes that handler itself
       (for example a second cancel arriving inside it) and the client then rolls back to a savepoint and keeps its transaction.
       A leaked marker spares only an EXCLUSIVE lock on its key that the same transaction took in an earlier, completed call, i.e.
       an admin's applied revoke or grant (only an admin's call, `end_session` and `dc.revoke_factor` take markers). That admin can
       then freeze its tenant's T, or a revoke target's A, until it is itself revoked: every call of that transaction holds
       A(admin) SHARED, which no marker spares, so revoking the admin ends it (`N1_LeakBound`, run on a core whose `dc._unmark` is a
       no-op). The same bound as N4 before the fix, reachable only through that race. Not observed; not measured.
    6. The policy API refuses to run while `max_prepared_transactions > 0` (F2a above).
    7. Within a tenant, two subject names collide on S with probability 2^-32 per pair (a wait only); N and L4 as above.
    8. A refusal caused by state that changed after the unlocked pre-read (owner SQL only) may still have evicted (N2 above).
    9. There is no migration from an already deployed R3-7 schema of the 2262eaa era (no `lockid` columns) to this one:
       `pol.pgcore.install` replaces schema dc, and `ops/migrate_r3_7` refuses because `dc.apply_authority` is absent (review of
       6b77354 N7). This matters only if such a database exists; none is known. The rollback restores the advisory ACL recorded
       in `dc.migration_mark`, a table dc_owner can rewrite, as superuser; it now refuses a mark that names anything but a live
       advisory-lock function or has a malformed entry, so the mark's text never reaches SQL (`N7_RollbackMark`: at 6b77354 a
       tampered mark made the rollback grant `pg_read_file` to PUBLIC).

### 4.5 Deadlocks

Within one call, every lock is requested in strictly increasing (level, key) order. Apply-phase waits cannot happen (1 ms), and
`dc._at` enforces the order at run time. So two calls, each in its own transaction, cannot form a cycle. A cycle needs one of the
following:

- **A transaction that made several calls** and holds an earlier call's locks.
- **The owner-only raw path or direct owner SQL**, which follow no budget.
- **A lock upgrade on one key.** This is excluded: a self-revoke takes A exclusive directly, and its FOR SHARE then FOR NO KEY
  UPDATE on its own principal row runs under that exclusive lock.

The deadlock detector resolves the remaining cases: the victim's transaction rolls back whole (`test_deadlock_victim_mid_call`). The
enumeration test checks each entry point's recorded sequence against section 4.2. The mutual-wait tests run crossed revokes, revoke
against an in-flight call of its target, `end_session` against an in-flight call, erasure and revoke against streams of submits,
crossed subjects, `open_session` against a revoke of its principal, and grant against submit. Each must end without 40P01, with the
serial outcome.

## 5. Blocking points: how each is ruled out

"Before the pins" means the wait comes before the statements that lock and judge the authority, so the judgment sees any change
committed during it: the old oracle stands (mutation commits, call refused). "After the pins" means no other transaction can change
the authority until the call ends: the mutation waits for the call, the call applies, then the mutation lands (new oracle). Expiry
during any of them is capped (section 4.4, `S1_ExpiryCeiling`). No point is covered by its position in the code alone: each is
excluded by an acquisition lock, or fails the call in the apply phase, as tested.

| Entry point | Point | Class | Test |
|---|---|---|---|
| submit | relation locks (events, sessions held SHARE / ACCESS EXCLUSIVE) | before the pins (L0) | `S1_BeforeThePins.test_events_table_lock`, `S1_RevokePath.test_sessions_table_lock` |
| submit | authority lock held exclusively (a revoke or `end_session` of the caller in flight) | before the pins (L1) | `S1_BeforeThePins.test_authority_lock` |
| submit | a pinned row held by a writer (direct owner SQL) | the pin statement itself: it locks and judges the newest version (13.2.1) | `S1_BeforeThePins.test_pin_waits_for_a_row_writer` |
| submit | tenant lock (was the tenant row), idempotency lock, head row, subject lock (bob's open submit), chain-head row | after the pins (L3-L7) | `S1_PinnedAuthority` (all four mutations, each point) |
| submit | waits inside the staged reduction (index wait on a subject token held by raw SQL; any row of the apply) | apply phase: excluded by L4-L7; otherwise fails in 1 ms, nothing written | `test_subject_token_unique_index`, `S1_ApplyNeverWaits` (broken variant without the L7 lock) |
| revoke | target principal row, target session rows (one held by an open `end_session`), target props and head rows | after the caller's pins (L2.3-L5); `end_session` of the target holds A(target), so the revoke waits for it FIFO, capped at XWAIT | `S1_RevokePath` |
| revoke | its authority writes after the final judgment | rows locked at L2.3-2.4; 1 ms | `S1_ApplyNeverWaits.test_a_wait_after_the_judgment_fails_the_call` (broken variant without the L2.3 lock) |
| explain, read_table, erase_candidates | relation lock during the read | before the pins (L0); the read after the pins cannot wait | `S1_BeforeThePins` (reads) |
| erase_values | tenant lock, value row, token row | after the pins (L3, L5) | `S1_PinnedAuthority.test_erase_*` |
| open_session | ledger twin (same nonce) | after the pins (L8): the factor or principal revocation waits for the call (B held in its own transaction, so the wait for B is observed after A ends) | `S1_OpenSession.test_twin_rolls_back`, `test_twin_commits` |
| open_session | prune of the same expired rows | apply, `SKIP LOCKED`: no wait | `S1_OpenSession.test_prune_never_waits` |
| open_session | factor row held by a writer (was the ledger foreign key) | the pin statement itself, which judges the newest version | `S1_OpenSession.test_factor_row_writer` |
| end_session | in-flight calls of its principal | waits FIFO (L1 exclusive), XWAIT | `S1_MutualWaits.test_end_session_waits_for_the_call` |
| PgDecisionService | any wait of the database call | recheck-only, section 6 | `P1_PgServiceJudgment` |
| caller-held transaction | everything after the call, before COMMIT | revocations wait for it (pins and authority lock), FIFO, XWAIT per attempt, idle timeout 60 s; past XWAIT a non-owner holder is evicted (4.4) | `S1_MutualWaits.test_revoke_waits_for_a_held_call`, `S44_Eviction`, `S44_Scope`, `S44_Control` |

## 6. What stays a recheck, and why

Each of the following lives in process memory, behind Python locks the database cannot take or wait on. Pinning one would mean
holding that Python lock across the database call. That would serialise every call of the process, and it would add a lock order
that crosses the boundary: a thread holding the Python lock waits on a database lock, while the database lock's holder waits for the
Python lock. No deadlock detector sees that cycle. So each is judged again after the call's last wait and before COMMIT
(`PgDecisionService._judged`). Nothing runs between that judgment and COMMIT, and COMMIT takes no lock. A change in that window is
linearised after the call.

| In-process input | Recheck | Test (mutation during the wait) |
|---|---|---|
| the in-process session (SessionAuthority: ended or expired) | `_verified` | `P1_PgServiceJudgment` mutation `session_ended` (new) |
| the directory (principal removed) | `_verified` (`principal_of`) | mutation `removed` |
| the policy document's revocations (skeleton over-match included) | `_revocations._is_revoked` | mutation `policy_revoked` |
| each value's erase decision (a legal hold recorded; the erase grant withdrawn) | `_changed`, then a fresh judged call for the rest | `test_erase_decision_changes_during_wait` |

Writing these through to the database would make them pinned rows. That would require the policy loader and the directory to write
`dc.*` before they publish a change in process: an interface change (today the policy source is any callable, and the directory is
mutated in place). It is not part of this change. Until then the D1 N3 residue (a raw-SQL dc_app caller after a policy-only
revocation) stays disclosed in `PgDecisionService`'s docstring.

## 7. Price and changes

- **Revocations wait for in-flight work.** A revocation, `end_session` and erasure now wait (FIFO) for the in-flight calls they
  affect, where before they raced them or skipped their rows. The new SQLSTATE 55P03 after XWAIT is retryable.
- **Longer serialisation per tenant.** Submits of one tenant serialise from L7 to COMMIT. Before S1 they serialised from the event
  insert to COMMIT, so the reduction's CPU time now joins that section.
- **More row locks.** Every call takes three FOR SHARE row locks and up to 14 relation locks. Concurrent calls of one principal
  share a multixact on its principal row.
- **Changed test oracle.** For points after the pins, the oracle is now "the mutation waits for the call; the call commits first".
  The previous P1/P4 classes are rewritten accordingly. Each red log in `results/red` is taken on ea4677a, together with a broken
  variant that strips the FOR SHARE pins, so the pins are shown to be what makes each test pass.
- **Experiments.**
  - The S2 controls (`exp/s2_sessions.py` VARIANTS), the B3 controls (`HEAD_LOCK`, `ADVISORY`) and the pg_diff controls (`STALE`,
    `CAPS`) patch strings that stay byte-identical, each exactly once.
  - B3's owner-only raw entries keep their old waits (no L0, no L7, no budget), so its controls still demonstrate.
  - The S2 sessions experiment and B3's S7/S8 exercise the S1 path.
  - Amendment 7 records the run on fresh seeds.
