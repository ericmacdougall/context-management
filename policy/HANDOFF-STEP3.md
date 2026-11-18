# Step-3 hand-off: the decision core, for its port into the write path (GoodQA #16096)

Status: hand-off package of the policy owner. The core is built and tested ALONE (Postgres `dc.*` schema plus the Python oracle); it is
NOT built or tested together with the write path. COMPOSITION.md is the design of the port (the write path owns the decision
transaction; the core's rules move into its `events_guard` trigger transaction).

Proof status (2026-11-18, after the delta review of 6b77354; a fixer's report, NOT a review pass: a delta review follows):
- The delta review of 6b77354 (BLOCK for client use) found 6b77354 unsafe: its in-progress exemption (H1) never worked, so a
  revocation in progress was ended by a later `end_session`, `dc.revoke_factor` or second revoke and lost (N1); six refused shapes
  of a grant or revoke still took T exclusively and evicted bystanders (N2); waiters and committed innocents were ended (N3); an
  admin parked on another tenant froze its own (N4); the rollback built SQL from the mark's text (N7). DO NOT port from 6b77354.
- Port from `fix/policy-final-review` at 81279dd (the fix of N1-N7) or a later commit of that branch; THRESHOLDS amendment 9 is its
  last commit (documentation only). Not merged to main. This branch, `docs/step3-handoff`, merges that branch (no rewrite) and adds
  the step-3 fixtures and the protocol fixtures (`tests/test_fixtures_step3_protocol.py`).
- Green at 81279dd, DEVIATION: PG 17.11 local on the Mac, not 16.15: `tests.test_rev6b77_pg` (red at 6b77354, results/red README
  section "Review of 6b77354") and the focused Postgres modules (`test_rev2262_pg`, `test_s44_pg`, `test_s1_pg`, `test_final_pg`,
  `test_delta1_pg`, `test_review3_pg`, `test_step2_pg`, `test_pg`, `test_pg_boundary`), Ran 156, OK. On t2-dev (16.15): see "NOT
  proven". Earlier, at 6b77354 on t2-dev: `tests.test_rev2262_pg` Ran 12, OK; the eight Postgres modules Ran 145, OK; in-memory
  and evidence suites Ran 186, OK; those runs did not detect N1-N4 (H1 passed on timing only).
- Protocol fixtures on this branch's merged tree (87e0843): NOT RUN anywhere. Every MUTANTS anchor was checked to occur exactly
  once in the 81279dd core; that each new mutant is caught by an assertion is NOT yet shown. Reasons: the Mac fell under 2 GB free
  (0.18 GB, not this work's files), so the local server was removed before the run; on t2-dev the 10 GB start bar was not reached
  from 22:30 to 23:02Z (8.8 to 9.7 GB free). Run first: `python -m unittest -v tests.test_fixtures_step3_protocol` on the lab
  environment of tests/test_pg.py.
- Failing-first, strict count (`results/red/README.md`, `kit/strict_classify.py`; only safety, liveness, availability, integrity and
  verifier failures count): the review of 2262eaa at 2262eaa, availability 7, liveness 7, integrity 1 (plus mechanism 2, not
  evidence) and in memory safety 4; S1 P1/P4 at a2d65fd, safety 13 and liveness 12 (the "28 behavioural" of the earlier hand-off
  mixed classes); 4.4 eviction at 8547b27, liveness 9 (was "16"); 4.4 fences at 8547b27, mechanism 4 only; 4.4 migration at
  7464493, liveness 1. Broken variants caught: the queued-revoker filter removed; the refused revoker's eviction made unconditional;
  the six lock-layer protocol mutants of 2129a5e on tree w4 (6b77354's code; its `in_progress` mutant changed dead code, see
  section 3), while the first `expiry_noop` ran and was NOT caught; the mutants of 87e0843 have NOT run (above). F3 (fence
  before eviction) and F8 (install grants) are red only as `mechanism` and F6, F7 have no red test: by the strict rule they have
  no failing-first evidence of a defect.
- DEV seeds at 6b77354's code (tree 20da971, NOT the port base; not re-run at 81279dd; development observations, not proof): b3_pg PASS (54 s; the idem_unprotected control
  caught 50 of 50 with the overlap witnessed in 49); s2_sessions PASS (1860 rows, 0 accepted attacks); the complete
  pg_diff VOID (t2-dev free space fell under 15 GiB, not by my files; b2 had finished clean, 300 logs, 45,206 events, 0
  mismatches; B1 was cut). The last complete DEV pg_diff is still the fixer's at e816310, before the 4.4 code.
- NOT proven:
  - no stamped run of this round (not started, by instruction);
  - NOT RUN on t2-dev (PostgreSQL 16.15) at 81279dd, because free space stayed under the lab rule's 10 GB start bar (8.8 to 9.7
    GB, 22:30 to 23:02Z; batch b6 waited 30 min without the lock and gave up): `tests.test_rev6b77_pg`, the focused Postgres
    modules, the protocol fixtures, DEV mig_rehearsal, the complete DEV pg_diff and `verify results --dev`. Every 81279dd result
    is local only (DEVIATION: PG 17.11). The last complete DEV pg_diff is still the fixer's at e816310, before the 4.4 code;
  - Correction: an earlier version of this list said the `expiry_noop` mutant of f9b4e63 "has NEVER run". It had run: the policy
    owner's t2-dev log `s3_green_f9b4e63.out` (Ran 25, FAILED, failures=1) and twice in the review of 6b77354, and it was NOT
    caught (`MUTANT expiry_noop ran=1 failures=0`): that version removed only `dc._session`'s expiry clause, which only the final
    judgment `dc._gate` reads, and the entry guards still refused the expired token. The reviewer's `gate_noop` was not caught
    either. Both are now caught by `N6_JudgmentAtTheGate` (a token that expires inside the call), see section 3;
  - nothing here is built or tested with the write path;
  - the eviction, the marker, the lock identities and the 2PC refusal are ASSUMPTIONS (the policy owner's and the fixers'), not the
    user's decisions;
  - the residuals of section 4 (a leaked marker, the unlocked pre-read, the re-check-to-signal window, S collisions within a tenant)
    are disclosed, not measured.

ASSUMPTIONS in force, not confirmed by the user (label them wherever the port relies on them): decision 8 (agents only propose and
remark), decision 9 (idempotency per tenant, principal and key), decision 10 (identifier declaration rule plus span redaction),
decision 11 (conflict notices default to none), the origin vocabulary (`user`/`user_typed`, `assistant`/`agent_generated`), the
eviction of stalled holders (FINAL-JUDGMENT.md 4.4: the policy owner's choice, not the user's), and, from the review of 2262eaa, the
exact lock identities and the 2PC refusal (the fixer's choices, not the user's).

## 1. What is handed over

| Part | Where | What it is |
|---|---|---|
| Decision core, SQL | `pol/_pgcore.sql`, installed by `pol/pgcore.py` `install()` | The rules in plpgsql (`dc._rules`), the state tables, the append-only event log with its hash chain, the identity tables and the session-keyed API. |
| Python oracle | `pol/_core.py` | `reduce(view, ev) -> (code, original_code, delta)`, `MemoryState(cfg)` with `apply_delta`, `comparable()` (CORE-RULES section 8) and `snapshot()`; `REFUSED`, `APPLIED`. The differential reference for the port. |
| Normative rules | `CORE-RULES.md` | Genesis, event format, outcome rules (first match wins; a refused code changes nothing), resolve, effects, bookkeeping, liveness bound, canonical forms and the comparable view. |
| Fixtures | `fixtures/step3/` (`b2.jsonl.gz`, `b1.jsonl.gz`, `MANIFEST.json`), `exp/fixtures_step3.py` | B2 logs (60, 9859 events) and the five B1 scenarios as breadth-first trees (3042 nodes, 16946 edges) with the oracle's expected `(code, original)`, applied lists and comparable-view hashes. `python -m exp.fixtures_step3 export <dir> --full` writes the set at the stamped depths (1.17 M B1 transitions; not committed). Development fixtures, not proof. |
| Acceptance tests | `tests/test_pg_boundary.py`, `tests/test_pg.py`, `tests/test_step2_pg.py`, `tests/test_delta1_pg.py`, `tests/test_review3_pg.py`, `tests/test_s1_pg.py`, `tests/test_final_pg.py`, `tests/test_s44_pg.py`, `tests/test_rev2262_pg.py`, `tests/test_rev6b77_pg.py`, `tests/test_rev2262.py`, `tests/test_fixtures_step3_set.py`,
`tests/test_fixtures_step3_protocol.py`; `exp/pg_diff.py`, `exp/b3_pg.py`, `exp/s2_sessions.py` | What the core proves today. The port is complete when the same checks run green against the write-path schema (COMPOSITION.md, "Recommendation"). |

Fixture formats are in the docstring of `exp/fixtures_step3.py`. A B2 log is submitted event by event on a fresh tenant
(`exp/core_gen.config()`); a B1 edge is applied ALONE from its node's state and rolled back, and the walk descends along the tree edges.
Scenario `caps` runs both cores with the B1 small caps (2 open per principal, 3 non-human per requirement). `check --pg` replays the set
through the dc core in Postgres; `tests/test_fixtures_step3_set.py` shows that every B1 control's mutated oracle, and pg_diff's mutated
SQL core, is caught by the set.

## 2. Interface contract (what callers see)

Roles (`pol/_pgcore.sql`, privileges at the end):
- `dc_owner` (NOLOGIN) owns every table, trigger and function. Logins use SCRAM; passwords are read from 0600 files.
- `dc_app` (the request handler) holds NO table, column or sequence privilege. It may EXECUTE only `dc.submit`, `dc.explain`,
  `dc.read_table`, `dc.erase_candidates` and `dc.erase_values`.
- `dc_idp` (the identity adapter) may EXECUTE only `dc.open_session` and `dc.end_session`.
- `dc_evictor` (NOLOGIN) is a member of `pg_signal_backend` and of nothing else, and owns only `dc._evict`, which only dc_owner may
  execute (FINAL-JUDGMENT.md 4.4; ASSUMPTION). The port needs the same privilege split, or another way to end a stalled holder.
  Eviction scope (review of 2262eaa F2b, review of 6b77354 N1, N3, N4; FINAL-JUDGMENT.md 4.4 "Whom" is normative): it ends a
  backend of this database whose session user is not a superuser and not a member of dc_owner, only if that backend HELD the key
  (or its fence) granted, in the same transaction, both when the evicting request began and just before the signal, and is not a
  call in progress marked for that key (the marker M(k), advisory class 1145241607, session-level SHARED, held together with k or
  its fence exclusively). Waiters are never ended. A login role that is a member of dc_app is evicted like dc_app. dc_evictor needs
  no statistics rights: everything it reads is in `pg_locks`.
- Two-phase commit (review of 2262eaa F2a; ASSUMPTION, fail closed): `install()` refuses a server with
  `max_prepared_transactions > 0`, and every API call raises SQLSTATE 55000 while it is > 0. The API cannot run on a cluster that
  uses two-phase commit; the port must either keep this refusal or end prepared holders some other way.
- The `pg_catalog` advisory-lock functions are NOT executable by PUBLIC, nor by any other explicit grantee except the owner (review
  of 2262eaa F8: `install()` now also removes explicit grants, e.g. to dc_app or a role it is a member of), in the policy database;
  dc_owner may call them. A login
  role cannot take a core lock with a raw call (`test_login_roles_cannot_take_advisory_locks`).
  For the write-path owner: this revoke is DATABASE-WIDE, not scoped to the `dc` schema (`pol/_pgcore.sql`, the DO block after
  the privilege section revokes EXECUTE on every `pg_catalog.pg_%advisory%` function from PUBLIC). If the write path shares the
  database, any of its login roles that calls an advisory-lock function directly gets "permission denied" after `install()`. Take
  such locks inside an owner-owned SECURITY DEFINER function. A grant to a named role does not survive: `install()` removes every
  explicit grant but the owner's (F8). A role granted them later could hold a core lock key; `dc._evict` then ends its backend
  (57P01) like any other holder that is not a superuser or a member of dc_owner (F2b) whenever it blocks a revocation past XWAIT. `ops/migrate_r3_7` forward records the advisory ACL it found and rollback restores exactly that ACL (an
  older mark without one falls back to the PUBLIC grant).
- `dc.enroll_factor` and `dc.revoke_factor` are owner or superuser only (the identity adapter's enrolment, out of scope;
  `dc.revoke_factor` is the queued factor revocation).
- The raw core (`dc._rules`, `dc._apply_delta`, `dc._lock_event`, `dc._reduce_apply`, `dc._apply_event`) is owner-only; no login role
  can execute it (`tests/test_pg_boundary.py`).

**Isolation: every `dc_idp` and `dc_app` call must run at READ COMMITTED** (the default). This is a behaviour change callers see
(delta review 1 M1, 8928870). Under any other level (READ UNCOMMITTED, which Postgres runs as READ COMMITTED but reports by its own
name, REPEATABLE READ or SERIALIZABLE):
- `dc.open_session` raises `proof refused: isolation`;
- `dc.submit` returns `unauthenticated:invalid_session`;
- `dc.explain` returns null; `dc.read_table` returns `[]`;
- `dc.erase_candidates` returns `{refused: unauthorized, values: []}`; `dc.erase_values` returns `{refused: unauthorized}`.

The reason: a fixed snapshot would outlive an `end_session`, a principal revoke or a factor revoke committed after it.
`tests/test_delta1_pg.py` shows each refusal after the same call works under READ COMMITTED.

`dc.end_session` raises `end_session refused: isolation` under any level but READ COMMITTED (final review P5): under REPEATABLE
READ or SERIALIZABLE its update read the transaction's snapshot, so a session opened after it was "not found" and stayed live.

Time: proof windows and session expiry are judged on the database host's WALL CLOCK (`clock_timestamp()`), never on the transaction
start. A token or proof that expires inside a long caller transaction is refused at its next call.

Acquire, judge, apply (S1, 078252d; FINAL-JUDGMENT.md sections 4-6). Every session-keyed call and `dc.open_session` takes EVERY lock
it needs before it judges, in one global order (section "Lock order" below). The caller's session, factor and principal rows are
pinned FOR SHARE by the statement that judges them, so a revoke, `end_session` or factor revocation waits for an in-flight call,
and the call commits first. Each wait is capped by the credential's remaining life (`dc._budget`) and by the caller's own
`lock_timeout`; an exclusive wait for A or T also by XWAIT = 2 s. After the last lock, `lock_timeout` is 1 ms: the staged writes,
the final judgment (`dc._gate`, the wall clock read last) and a revoke's authority writes never wait, and a wait there fails the call
with 55P03, nothing applied. This closes final review P1 (judgment after the last wait, including the `dc.subject_tokens` insert)
and P4 (`dc.open_session` judges factor and principal revocation after the replay-ledger wait: its L8 nonce lock comes after the
pins). Failing-first logs: results/red/ (README section "S1 (the final review's P1-P4 merged) and
FINAL-JUDGMENT.md 4.4").

New SQLSTATE 55P03 from any call: a wait ended by the caller's own `lock_timeout`, by XWAIT on an exclusive lock, by an eviction
fence, or a wait after the acquisition. Nothing was applied; the call is retryable.

Stalled holders (FINAL-JUDGMENT.md 4.4; ASSUMPTION). A caller that keeps a transaction open after a call holds that call's locks.
If that blocks a revoke, `end_session`, `dc.revoke_factor`, grant or erasure for more than XWAIT, the blocked call (once its own
caller is pinned and judged) ends the holder's backend (`pg_terminate_backend`) and retries, at most three rounds. Callers must
expect a terminated connection (57P01) in that case; nothing of the ended transaction is committed, except in the window between
eviction's last re-check and its signal (microseconds), where the holder's commit stands and its connection is ended before its
next statement (FINAL-JUDGMENT.md 4.4 residual 1). A call that only queued behind the evicting request, or whose connection began a
new transaction, is never ended (N3). Owner and superuser holders, and calls in progress marked for the key, are never ended. A
round that can signal nobody because every holder is spared gives up with 55P03 (retryable). Measured bound with one holder:
2.11-2.12 s (t2-dev, PG 16.15, at 6b77354; not re-measured at 81279dd).

`dc.open_session(proof text, ttl_s integer) -> jsonb` (dc_idp):
- proof = `<body>.<mac>`: body is a JSON object with exactly the keys `channel`, `expires` (epoch seconds, a number), `factor`, `nonce`
  (32 hex), `principal` and `tenant`; mac is the 64-hex HMAC-SHA256 of the body's UTF-8 bytes under the factor's key; at most 2048
  characters.
- Accepted only if the MAC is right, the factor is not revoked, the body names the factor's tenant, principal and channel, `expires` is
  in (now, now + 300 s], the principal is unrevoked with a channel its kind allows (human: `owner_ui` or `owner_cli`; agent and analyst:
  `agent_api`), and the nonce is unused with that factor. `ttl_s` must be 1 to 86400.
- Returns `{token, tenant, principal, channel, factor}`; the token is 64 hex and only its sha256 is stored.
- Refusals raise SQLSTATE P0001 `proof refused: <shape|isolation|signature|factor revoked|binding|time|principal|channel|replayed>`
  and write nothing.

`dc.end_session(token text) -> boolean` (dc_idp): true if a live session was ended; the token is refused from then on.

Origin is derived, never taken from the body: a human on `owner_ui` or `owner_cli` gives actor `user`, trust origin `user_typed`;
anything else gives `assistant`, `agent_generated`.

`dc.submit(token text, body jsonb) -> {code, original, id, value_ref}` (dc_app):
- body keys allowed: `type, client_id, idem, req, value, label, based_on, proposal, proposal_hash, pending, verb, choose, target, role`.
  A body naming `tenant`, `principal`, `channel`, `id`, `actor` or `trust_origin` is refused `identity_in_body`. `client_id` must match
  `^[A-Za-z0-9._-]{1,64}$`; the event id is `<principal>/<client_id>`.
- `value` (a string) needs `label`; subjects in the label must arrive canonical (`pol.labels.canon_subject`), else `bad_event`.
  Subjects are stored in the log only as random tokens; the value reference is random (`val-` + 40 hex), never derived from the value.
- `code` is one of `pol._core.OUTCOME_CODES`, or `unauthenticated:invalid_session`, `identity_in_body` or `bad_event` (raised before
  the core). `original` is set on `duplicate` (the original outcome). `value_ref` is set when a value was stored, and on a `duplicate`
  retry that reuses the stored value.
- Idempotency (ASSUMPTION decision 9): per (tenant, principal, idem). A retry with the same key and payload answers `duplicate` with
  the original outcome and reference; another payload under the same key answers `idem_conflict`. Overlapping submissions with one key
  are serialised by the per-principal advisory lock (delta review 1 M3, `tests/test_delta1_pg.py` M3_PgOverlapping).

`dc.explain(token text, req text) -> jsonb or null` (dc_app): for a participant of the requirement or an admin,
`{principal, tenant, req, head, open, values}`; null otherwise, and for an unknown requirement or a revoked principal. Label filtering
of the values is done by the in-process policy point, not the database.

`dc.read_table(token text) -> jsonb` (dc_app): the list of `dc.explain` objects of every requirement the caller may see, by req.

`dc.erase_candidates(token text, subject text) -> {refused, values: [{ref, label}]}` and
`dc.erase_values(token text, subject text, refs text[]) -> {refused, erased, remaining}` (dc_app, admin sessions only): the subject must
be canonical (`refused: bad_subject` otherwise). `erase_values` deletes only the listed values still about the subject, and the
subject's token mapping once none remains, under the tenant lock T exclusive (L3). The hold decision between the two calls is
in-process.

Lock order (READ COMMITTED; FINAL-JUDGMENT.md 4.2 is normative, `dc._at` enforces it at run time with SQLSTATE `DCORD`, and
`S1_LockOrder` checks every entry point's recorded sequence):
- L0 relation locks, one table per statement, alphabetical;
- L1 the authority lock A(tenant, principal), advisory class 1145241601, key = the principal row's `lockid` (`dc._ak`; review of
  2262eaa F1: no longer a 32-bit hash of the name): shared by every call of the principal, exclusive for a revoke of it (only when
  the target is a principal of the caller's tenant and the pre-read of L3 predicts that the revoke applies, review of 6b77354 N2),
  an `end_session` of its session, or `dc.revoke_factor`;
- L2 the authority rows: the caller's session, factor and principal FOR SHARE (the pins); a revoke's target principal and session
  rows FOR NO KEY UPDATE;
- L3 the tenant lock T(tenant), advisory class 1145241602, key = the tenant row's `lockid` (`dc._tk`): shared by ordinary
  submits, exclusive for erasure and for a revoke or grant that an UNLOCKED pre-read predicts will apply: the rules' facts (F5),
  the stage's body checks, `dc._shape`, the idempotency lookup and the caller's revocation (review of 6b77354 N2). A change the
  pre-read predicts will be refused takes T shared and evicts nobody (`N2_RefusedShapes`, every refusal reason of the code); one
  that applies without `xa` fails 55P03. Residual: a change refused only because owner SQL changed the state after the pre-read
  may have taken T exclusively and evicted (FINAL-JUDGMENT.md 4.4 residual 8);
- L4 the per-principal idempotency lock;
- L5 data rows (head, props, values, token row);
- L6 subject locks S(tenant, subject): class = -(the tenant's `lockid`), key = `hashtext` of the subject (32 bits within one
  tenant);
- L7 the tenant's chain-head row;
- L8 the nonce lock N(factor, nonce), advisory class 1145241604 (`dc.open_session`).

The tenant row is no longer locked; `dc._lock_event` remains only on the owner-only raw path (`dc._apply_event`). An evicting call
also takes the fence F_A (1145241605) or F_T (1145241606) of the key it evicts for, BEFORE its judgment and its first eviction
(review of 2262eaa F3; FINAL-JUDGMENT.md 4.4). Before it requests A or T exclusively, a call takes the marker M(k) (class
1145241607, session-level, SHARED only, outside the lock order: no one takes it exclusively, so nothing waits for it) and
releases it on every exit of `dc.submit` (revoke or grant), `dc.end_session`, `dc.revoke_factor` and `dc.erase_values`, from
their exception handlers too. The write path's trigger has its own locks; COMPOSITION.md item 5 gives the merged order, which must
be re-derived against this order.

**Reserved advisory key space (the port must not use it).** In the policy database, the two-key advisory classes 1145241601 to
1145241607 (A, T, 1145241603 unused and reserved, N, F_A, F_T, M) and every NEGATIVE class in [-1,000,000,000, -1] (S, one class
per tenant `lockid`) belong to the core; so does the one-key (bigint) space of L4, `hashtextextended` of (tenant, principal),
where a collision is a wait of probability 2^-64 per pair, never an eviction. A write-path
lock in these spaces would collide with the core: in A, T or a fence it would make its holder evictable, and in M it would spare a
holder that must be ended. The port that moves these locks into its own schema keeps the same reservation, or re-derives it.

## 3. What the port must keep

The acceptance list of COMPOSITION.md ("Recommendation"), plus this round's additions:
- the role boundary;
- identity only from an owner-written sessions row;
- append-only by grants and triggers;
- READ COMMITTED with the fixed lock order;
- the original outcome on retry;
- constant cost to 20,000 proposals;
- no subject names in the log;
- erase and propose serialised;
- the B3 overlap scenarios;
- the event-by-event differential against `pol/_core.py` with a mutated control (`exp/pg_diff.py`, and `fixtures/step3` with
  `check --pg` against the new schema);
- this round's additions: the wall-clock expiry and the READ COMMITTED refusal on every entry point, the session rechecks after lock
  waits, and the serialised idempotency key;
- the final review's P1-P4 (merged; S1 at 078252d): every lock acquired before the judgment, the session judged after the LAST
  wait with no staged value or token left on refusal; open_session judging factor and principal revocation after the replay-ledger
  wait; the exception-safe idempotency staging. The write path's trigger adds its own waits (lease, subject, identifier locks): the
  port must acquire them before its final judgment too, in one global order, or fail them in the apply phase;
- 4.4: a revocation is not blocked past XWAIT by a holder that keeps its transaction open (`tests/test_s44_pg.py`, with its
  broken-variant controls: eviction made a no-op, and the queued-revoker filter removed);
- the review of 2262eaa: no cross-tenant eviction, fencing or stalling through A or T (exact `lockid` keys); prepared
  transactions refused; member login roles evicted; the fence before the first eviction (F3); explicit advisory grants removed (F8)
  (`tests/test_rev2262_pg.py`);
- the review of 6b77354 (`tests/test_rev6b77_pg.py`), replacing two claims this section made before that were false at 6b77354:
  - a revocation in progress is never ended by a later exclusive request on its key, whatever it is doing (holding the key and
    parked on a row lock, or holding only the fence while it evicts), whether the trigger is `end_session`, `dc.revoke_factor` or a
    second admin's revoke (`N1_ParkedRevocation`, six cases); the claim before ("not evicted by a later exclusive request (H1)")
    was false, the exemption never worked;
  - a refused grant or revoke waits for no exclusive holder and evicts nobody for EVERY refusal reason the code can return
    (`N2_RefusedShapes`, `F5_RefusedAuthorityChange`); the claim before covered only four shapes. Except: residual 8 of
    FINAL-JUDGMENT.md 4.4 (state changed by owner SQL after the unlocked pre-read);
  - a backend that queued behind an evicting request, or a pooled connection's later transaction, is never ended (`N3_InnocentWaiter`);
  - an admin holding T exclusively while parked on another tenant's lock is evicted, so it cannot freeze its tenant (`N4_ParkedExclusive`);
  - a token that expires inside a call is refused by the final judgment (`N6_JudgmentAtTheGate`);
  - the rollback refuses a mark that names anything but a live advisory-lock function (`N7_RollbackMark`);
- the protocol fixtures (`tests/test_fixtures_step3_protocol.py`): `PROTOCOL` names, for the pins, the fence, the eviction, the
  in-progress marker, the eviction snapshot, the lock identity, the refused-change rule and authentication (session expiry and the
  final judgment), the cases the port must run green against its own schema. `MUTANTS` patches `pol/_pgcore.sql` to break exactly
  one layer each and requires an assertion failure (not an error) from that layer's cases: pins without FOR SHARE; the fence never
  refusing; `dc._evict` a no-op; the marker never taken, never released, sparing any key, or sparing a shared holder; eviction
  ignoring the snapshot, or matching it by backend instead of transaction; every principal on one A key; T always exclusive for a
  grant; the pre-read skipping the body checks; the final judgment's session check removed (`gate_noop`); expiry judged nowhere
  (`expiry_noop`). The B1/B2 replay of `fixtures/step3` replays the rules core only and sees none of these layers. History, so the
  port does not repeat it: the first `expiry_noop` (f9b4e63, `dc._session` only) RAN and was NOT caught, and `gate_noop` was not
  caught either; the H1 mutant of 2c56338 (`in_progress`) was reported caught although the clause it changed was dead code (dc_evictor cannot read
`wait_event`), so "caught" did not show that the exemption worked; it is replaced by the marker mutants. Layers these fixtures
  do NOT cover (disclosed, listed in the module docstring): S and N keys, L4, the lock order itself (`S1_LockOrder` covers it), deadlocks, the 2PC
  refusal, the member-role scope and the evictor's privileges, the advisory ACL and its rollback, the re-check-to-signal window,
  other N1 timings (`N1_Sweep`, supporting only) and a leaked marker (`N1_LeakBound`). Without the Postgres environment these tests
  and `CommittedSetPg` FAIL; `POL_STEP3_SKIP_PG=1` turns that into explicit, counted skips (it was a silent skip before).
- the reserved advisory key space (section 2, "Reserved advisory key space").

## 4. Limits (carried, not solved)

- N3: a raw-SQL `dc_app` caller holding a live token of a principal revoked ONLY in the policy document is stopped by the database
  only after a core revoke, a `dc.end_session` of that token, or the token's expiry: the TTL `dc_idp` chose (1 to 86400 s; PgIdentity
  default 3600 s), judged on the wall clock. The deployment must end that principal's sessions when it revokes it in the policy
  document, or accept that window.
- Factor keys are symmetric (pgcrypto has no signature verification), so the database owner can forge a proof. The proof window and
  session expiry trust the database host's clock.
- The external anchor (`FileAnchor`) is an append-only file, not immutable storage (R3-8): an owner who recomputes the whole chain
  passes `dc.verify_chain`, and is detected only against an anchor kept out of the owner's reach.
- Label filtering of reads and the hold decision of erasure are in-process (trusted), not enforced by the database.
- Enrolment is the identity adapter's and out of scope.
- Eviction residuals (FINAL-JUDGMENT.md 4.4): an evicted backend loses its whole connection, including unrelated work; for T that
  can be a legitimate submit of another principal, but only one that held T for at least XWAIT within one transaction (corrected:
  at 6b77354 a submit that merely queued behind the revoke was ended after it committed, N3). The fences refuse the target's or the tenant's new
  calls (55P03) while a revocation evicts. A stalled submitter holds the chain head (L7): the tenant's other submits are refused at
  their own expiry until a revoke, grant or erasure of that tenant (or a revoke of the staller) evicts it. Not measured.
- Revoke, grant and erasure serialise the tenant (T exclusive), and every submit of a tenant serialises from the chain-head lock
  (L7) to COMMIT: a scaling limit.
- Residuals of the reviews of 2262eaa and 6b77354 (FINAL-JUDGMENT.md 4.4, residuals 1 and 5-9):
  - Corrected: the "H1 window" stated here before (a revocation exposed only between two waits, microseconds) was false; at
    6b77354 a revocation was exposed whenever it evicted or was parked (N1). Now: a marker leaks only if an error escapes an API
    function's exception handler and the client keeps its transaction past a savepoint rollback; the leaked marker then spares
    only an admin's earlier applied exclusive lock on that key, and revoking that admin ends it (`N1_LeakBound`). Not observed.
  - Corrected: "a duplicate or idempotent retry of a VALID revoke or grant still takes the exclusive locks" is no longer true (the
    pre-read looks up idempotency, N2). What remains: a change refused only because owner SQL changed the state after the
    unlocked pre-read may have taken the exclusive locks and evicted (residual 8).
  - Between eviction's last re-check and its signal (microseconds), a holder may commit; its work stands and its connection is ended.
  - No migration path from an already deployed R3-7 schema of the 2262eaa era (no `lockid` columns): `install()` replaces schema
    dc and `ops/migrate_r3_7` refuses (residual 9). None is known to exist.
  - Within one tenant, two subjects collide on S with probability 2^-32 per pair (a wait under the caller's budget, never an
    eviction); N (32-bit hash of a random nonce) and L4 (`hashtextextended`, 64 bits) are unchanged; none is evictable. A and T
    have no collision (unique `lockid` from one sequence, 1 to 10^9, no cycle: once it is exhausted, inserting a principal or tenant fails).
  - The 2PC refusal makes the API unusable on a cluster with `max_prepared_transactions > 0`.
  - The database-wide advisory revoke breaks other tools in the same database that take advisory locks as a login role (Flyway,
    Liquibase, Rails); disclosed, not fixed.

## 5. Changes since 6b77354 (the delta review of 6b77354, GoodQA #16096)

All in 81279dd except where named; red logs at 6b77354 and green logs in `results/red/` (README section "Review of 6b77354"),
DEVIATION: PG 17.11 local, not 16.15.

| Item | Finding | Fix | Red at 6b77354 (local) | Green |
|---|---|---|---|---|
| N1 | The H1 exemption read `wait_event`, which dc_evictor cannot see: a revocation parked on a row lock or evicting was ended by a later `end_session`, `dc.revoke_factor` or second revoke | marker M(k) (class 1145241607) taken before the exclusive request, released on every exit; `dc._evict` spares a marked holder of k or F held exclusively | `N1_ParkedRevocation` 6 of 6 (revoker ended, revocation lost) | `N1_ParkedRevocation`, `N1_ModeRule`, `N1_LeakBound` |
| N2 | Six refused shapes (bad client_id, unknown key, label without value, grant role owner, exact retry, reused idem key) still took T and A exclusively and evicted | pre-read runs `dc._body_refusal`, `dc._shape`, the idempotency lookup and the caller's revocation before any lock | `N2_RefusedShapes` both tests, every shape | same |
| N3 | Eviction ended every holder row, waiters included: a submit that queued behind a revoke was ended after it committed | snapshot of `pid:virtualtransaction` before each request; only snapshotted holders still in that transaction are ended | `N3_InnocentWaiter` 2 of 2 | same |
| N4 | Any backend waiting for an A, T or fence lock was spared: an admin parked on T(b) froze tenant a | the exemption is the marker of the same key, nothing else | `N4_ParkedExclusive` parked case | both N4 cases |
| N6 | `expiry_noop` (f9b4e63) ran and was not caught; `gate_noop` not caught | `N6_JudgmentAtTheGate` (expiry inside the call); mutants `gate_noop`, `expiry_noop` (all checks) | green at 6b77354 as expected (a mutant-killer, not a defect test) | protocol fixtures: NOT RUN (section "Proof status") |
| N7 | Rollback built SQL from the mark's text: a tampered mark granted `pg_read_file` to PUBLIC | the mark is validated against the live advisory functions before any SQL | `N7_RollbackMark` | same |

## 5b. Changes since 2262eaa (the review of 2262eaa)

| Item | Finding | Fix | Commit | Red at 2262eaa, strict class | Green |
|---|---|---|---|---|---|
| F1 | A, T and S keys were `hashtext(kind, tenant, name)` in one 32-bit space: an admin could search names offline that collide with another tenant's principal, revoke one and evict or fence that tenant's backends; a colliding subject stalled another tenant | A and T keyed by a unique `lockid` per principal and tenant (`dc.lockid_seq`, frozen by triggers); S classed by tenant | 4e1ba6b | availability 3 (`F1_CrossTenant`) | `rev2262_pg_green_20da971.out` |
| F5 | A grant or revoke the rules refuse still took T (and A) exclusively, waited and evicted the tenant's in-flight submitters | pre-read sets `xa`; T exclusive only when the change would apply; 55P03 if it applies without it | 4e1ba6b | availability 4, liveness 4 (`F5_RefusedAuthorityChange`) | same |
| F2a | A prepared transaction keeps its advisory locks with no backend: its principal could never be revoked | fail closed: install refuses and every call raises 55000 while `max_prepared_transactions > 0` | 4e1ba6b | liveness 1 | same |
| F2b | Eviction matched the user NAMES dc_app and dc_idp: a member login role was never evicted | eviction by "not superuser, not a member of dc_owner" | 4e1ba6b | liveness 1 | same |
| H1 | `end_session` (exclusive on A) evicted a revocation in progress on the same key | in-progress exemption (waiting on BackendTermination or on an ungranted A/T/fence request) | 4e1ba6b | liveness 1 | same |
| F3 | The first eviction round of a revoke's A(target) ran without the fence; a reconnecting holder re-took the lock | fence taken before the judgment and the first eviction | 4e1ba6b | mechanism 1 (not an unsafe outcome) | same |
| F8 | Install left explicit advisory grants; rollback restored PUBLIC, not the prior ACL | revoke from every explicit grantee but the owner; migration records and restores the ACL | 4e1ba6b | mechanism 1, integrity 1 | same |
| H2 | In memory, explain and read_table answered for a principal revoked by the core, by policy or during the wait | refuse reads of revoked principals | 6361bd9 | safety 4 (`tests/test_rev2262.py`) | `rev2262_mem_green_b9cc544.out` |
| F4 | `kit/o-classify.py` not committed; "behavioural" counts mixed classes | classifiers committed, strict recount | 622def2 | n/a (documentation) | n/a |
| F6 | b3 idem_unprotected reported overlap True unconditionally; label and docstring untrue | monitor race, overlap only when B was seen blocked | bfe2a63 | none (control; DEV b3 shows 49 of 50 overlaps) | `dev_b3_20da971.out` |
| F7 | s2 ctl_txn_clock carried session-side patches its proof-only arm never measured | patches removed; token expiry in a transaction covered by `test_delta1_pg` M1_WallClock only | bfe2a63 | none (control) | `dev_s2_20da971.out` |

History of F1 and F2: 2262eaa (THRESHOLDS amendment 7) was handed over as the port base. The review of 2262eaa found that its
eviction (FINAL-JUDGMENT.md 4.4, added after the final review to close the stalled-holder residual) inherited the 32-bit name
hash of the lock keys: harmless while a collision only meant a wait, unsafe once a collision meant ending another tenant's
backend (F1, F5). It also found two holders eviction could not end: a prepared transaction (no backend) and a login role that is a
member of dc_app (matched by name only) (F2). Both made a principal unrevocable. The fixes above replace the hash with exact
identities and fail closed on 2PC; the red runs at 2262eaa and the green runs are in `results/red/` (README section "Review of
2262eaa"). The earlier hand-off text that named 2262eaa as the port base is superseded by this one.

