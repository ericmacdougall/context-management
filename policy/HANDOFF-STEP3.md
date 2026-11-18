# Step-3 hand-off: the decision core, for its port into the write path (GoodQA #16096)

Status: hand-off package of the policy owner. The core is built and tested ALONE (Postgres `dc.*` schema plus the Python oracle); it is
NOT built or tested together with the write path. COMPOSITION.md is the design of the port (the write path owns the decision
transaction; the core's rules move into its `events_guard` trigger transaction).

Proof status (2026-11-18, policy owner; an owner report, NOT a review pass: an independent review follows):
- Port from commit `2262eaa` of `fix/policy-final-review` (the Amendment 7 commit; its `pol/`, `exp/`, `ops/` and `verify/` are
  identical to ae644df). Not merged to main. The step-3 fixture set (`exp/fixtures_step3.py`, `fixtures/step3/`,
  `tests/test_fixtures_step3.py`, `tests/test_fixtures_step3_set.py`) is NOT on `fix/policy-final-review`: it is on this branch,
  `docs/step3-handoff` (cherry-picked from the draft branch: 3ef0fe4, 75a5799, 0b887b5). At this branch's f98768c on t2-dev,
  `tests.test_fixtures_step3` and `tests.test_fixtures_step3_set` Ran 11, OK (151 s), including the Postgres class: the set replays
  clean through the dc core of 2262eaa and pg_diff's no-stale core is caught.
- Green on t2-dev (Postgres 16.15) at ae644df, exact-commit tree: `tests.test_s44_pg` plus the seven Postgres modules, Ran 145, OK
  (238 s). The in-memory and evidence suites at ae644df (no database): Ran 183, OK.
- Failing-first (behavioural assertions only; logs and the counting rule in `results/red/README.md`): S1 P1/P4 at a2d65fd (28),
  4.4 eviction at 8547b27 (16), 4.4 fences at 8547b27 (4), 4.4 migration at 7464493 (2, and `exp.mig_rehearsal` stops at
  "permission denied"). Broken variants caught: the queued-revoker filter removed; the refused revoker's eviction made unconditional.
- DEV seeds at ae644df: mig_rehearsal PASS; s2_sessions PASS (1860 rows, 0 accepted attacks); b3_pg VOID three times (the disk
  watcher stopped the database; not passed). Development observations, not proof.
- NOT proven:
  - no stamped run of this round (it needs 20 GB free; t2-dev had 16.6-17.0 GB);
  - no complete DEV pg_diff at the final code (the last complete one PASSED at e816310, before the 4.4 code);
  - the eviction is the policy owner's ASSUMPTION, not the user's decision;
  - nothing here is built or tested with the write path.

ASSUMPTIONS in force, not confirmed by the user (label them wherever the port relies on them): decision 8 (agents only propose and
remark), decision 9 (idempotency per tenant, principal and key), decision 10 (identifier declaration rule plus span redaction),
decision 11 (conflict notices default to none), the origin vocabulary (`user`/`user_typed`, `assistant`/`agent_generated`), and the
eviction of stalled holders (FINAL-JUDGMENT.md 4.4: the policy owner's choice, not the user's).

## 1. What is handed over

| Part | Where | What it is |
|---|---|---|
| Decision core, SQL | `pol/_pgcore.sql`, installed by `pol/pgcore.py` `install()` | The rules in plpgsql (`dc._rules`), the state tables, the append-only event log with its hash chain, the identity tables and the session-keyed API. |
| Python oracle | `pol/_core.py` | `reduce(view, ev) -> (code, original_code, delta)`, `MemoryState(cfg)` with `apply_delta`, `comparable()` (CORE-RULES section 8) and `snapshot()`; `REFUSED`, `APPLIED`. The differential reference for the port. |
| Normative rules | `CORE-RULES.md` | Genesis, event format, outcome rules (first match wins; a refused code changes nothing), resolve, effects, bookkeeping, liveness bound, canonical forms and the comparable view. |
| Fixtures | `fixtures/step3/` (`b2.jsonl.gz`, `b1.jsonl.gz`, `MANIFEST.json`), `exp/fixtures_step3.py` | B2 logs (60, 9859 events) and the five B1 scenarios as breadth-first trees (3042 nodes, 16946 edges) with the oracle's expected `(code, original)`, applied lists and comparable-view hashes. `python -m exp.fixtures_step3 export <dir> --full` writes the set at the stamped depths (1.17 M B1 transitions; not committed). Development fixtures, not proof. |
| Acceptance tests | `tests/test_pg_boundary.py`, `tests/test_pg.py`, `tests/test_step2_pg.py`, `tests/test_delta1_pg.py`, `tests/test_review3_pg.py`, `tests/test_s1_pg.py`, `tests/test_final_pg.py`, `tests/test_s44_pg.py`, `tests/test_fixtures_step3_set.py`; `exp/pg_diff.py`, `exp/b3_pg.py`, `exp/s2_sessions.py` | What the core proves today. The port is complete when the same checks run green against the write-path schema (COMPOSITION.md, "Recommendation"). |

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
- The `pg_catalog` advisory-lock functions are NOT executable by PUBLIC in the policy database; dc_owner may call them. A login
  role cannot take a core lock with a raw call (`test_login_roles_cannot_take_advisory_locks`).
  For the write-path owner: this revoke is DATABASE-WIDE, not scoped to the `dc` schema (`pol/_pgcore.sql`, the DO block after
  the privilege section revokes EXECUTE on every `pg_catalog.pg_%advisory%` function from PUBLIC). If the write path shares the
  database, any of its login roles that calls an advisory-lock function directly gets "permission denied" after `install()`. Take
  such locks inside an owner-owned SECURITY DEFINER function, or grant the functions to a named role and accept that this role can
  then hold a core lock key that `dc._evict` will not end (it ends only `dc_app` and `dc_idp` backends), so it can stall a
  revocation past XWAIT. `ops/migrate_r3_7` rollback restores the PUBLIC grant.
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
expect a terminated connection (57P01) in that case; nothing of the ended transaction is committed. Owner and superuser holders are
never ended. Measured bound with one holder: 2.11-2.12 s (t2-dev, PG 16.15).

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
- L1 the authority lock A(tenant, principal), advisory class 1145241601: shared by every call of the principal, exclusive for a
  revoke of it, an `end_session` of its session, or `dc.revoke_factor`;
- L2 the authority rows: the caller's session, factor and principal FOR SHARE (the pins); a revoke's target principal and session
  rows FOR NO KEY UPDATE;
- L3 the tenant lock T(tenant), advisory class 1145241602: shared by ordinary submits, exclusive for revoke, grant and erasure;
- L4 the per-principal idempotency lock;
- L5 data rows (head, props, values, token row);
- L6 subject locks S(tenant, subject), advisory class 1145241603;
- L7 the tenant's chain-head row;
- L8 the nonce lock N(factor, nonce), advisory class 1145241604 (`dc.open_session`).

The tenant row is no longer locked; `dc._lock_event` remains only on the owner-only raw path (`dc._apply_event`). An evicting call
also takes the fence F_A (1145241605) or F_T (1145241606) of the key it evicts for (FINAL-JUDGMENT.md 4.4). The write path's trigger
has its own locks; COMPOSITION.md item 5 gives the merged order, which must be re-derived against this order.

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
  broken-variant controls: eviction made a no-op, and the queued-revoker filter removed).

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
  can be a legitimate submit of another principal that held T longer than XWAIT. The fences refuse the target's or the tenant's new
  calls (55P03) while a revocation evicts. A stalled submitter holds the chain head (L7): the tenant's other submits are refused at
  their own expiry until a revoke, grant or erasure of that tenant (or a revoke of the staller) evicts it. Not measured.
- Revoke, grant and erasure serialise the tenant (T exclusive), and every submit of a tenant serialises from the chain-head lock
  (L7) to COMMIT: a scaling limit.
