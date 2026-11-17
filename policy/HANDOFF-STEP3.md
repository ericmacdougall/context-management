# Step-3 hand-off: the decision core, for its port into the write path (GoodQA #16096)

Status: hand-off package of the policy owner. The core is built and tested ALONE (Postgres `dc.*` schema plus the Python oracle); it is
NOT built or tested together with the write path. COMPOSITION.md is the design of the port (the write path owns the decision
transaction; the core's rules move into its `events_guard` trigger transaction). Proof status of this commit's core: the stamped run of
THRESHOLDS.md amendment 7 is PENDING; results/README.md records the current stamped set and its verdict.

ASSUMPTIONS in force, not confirmed by the user (label them wherever the port relies on them): decision 8 (agents only propose and
remark), decision 9 (idempotency per tenant, principal and key), decision 10 (identifier declaration rule plus span redaction),
decision 11 (conflict notices default to none), and the origin vocabulary (`user`/`user_typed`, `assistant`/`agent_generated`).

## 1. What is handed over

| Part | Where | What it is |
|---|---|---|
| Decision core, SQL | `pol/_pgcore.sql`, installed by `pol/pgcore.py` `install()` | The rules in plpgsql (`dc._rules`), the state tables, the append-only event log with its hash chain, the identity tables and the session-keyed API. |
| Python oracle | `pol/_core.py` | `reduce(view, ev) -> (code, original_code, delta)`, `MemoryState(cfg)` with `apply_delta`, `comparable()` (CORE-RULES section 8) and `snapshot()`; `REFUSED`, `APPLIED`. The differential reference for the port. |
| Normative rules | `CORE-RULES.md` | Genesis, event format, outcome rules (first match wins; a refused code changes nothing), resolve, effects, bookkeeping, liveness bound, canonical forms and the comparable view. |
| Fixtures | `fixtures/step3/` (`b2.jsonl.gz`, `b1.jsonl.gz`, `MANIFEST.json`), `exp/fixtures_step3.py` | B2 logs (60, 9859 events) and the five B1 scenarios as breadth-first trees (3042 nodes, 16946 edges) with the oracle's expected `(code, original)`, applied lists and comparable-view hashes. `python -m exp.fixtures_step3 export <dir> --full` writes the set at the stamped depths (1.17 M B1 transitions; not committed). Development fixtures, not proof. |
| Acceptance tests | `tests/test_pg_boundary.py`, `tests/test_pg.py`, `tests/test_step2_pg.py`, `tests/test_delta1_pg.py`, `tests/test_review3_pg.py`, `tests/test_fixtures_step3_set.py`; `exp/pg_diff.py`, `exp/b3_pg.py`, `exp/s2_sessions.py` | What the core proves today. The port is complete when the same checks run green against the write-path schema (COMPOSITION.md, "Recommendation"). |

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
- `dc.enroll_factor` is owner or superuser only (the identity adapter's enrolment, out of scope).
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

`dc.end_session` has no isolation guard: it only ends a session, which every level may do.

Time: proof windows and session expiry are judged on the database host's WALL CLOCK (`clock_timestamp()`), never on the transaction
start. A token or proof that expires inside a long caller transaction is refused at its next call. `dc.erase_values` judges the
session again after its tenant lock. `dc.submit` judges it again after `dc._lock_event`; at a2d65fd a later wait (another transaction
holding a new subject's `dc.subject_tokens` entry) is NOT covered (final review P1). DRAFT: restate this from the fixer's merged code
before committing (P1 moves the last check after every blocking acquisition). Likewise `dc.open_session` re-reads factor and principal
revocation after its replay-ledger wait only once P4 lands.

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
subject's token mapping once none remains, under the tenant row FOR UPDATE. The hold decision between the two calls is in-process.

Lock order (READ COMMITTED, `dc._lock_event`):
1. the tenant row: FOR SHARE, or FOR UPDATE for revoke, grant and erasure;
2. the transaction advisory lock on (tenant, principal);
3. the requirement's head row FOR UPDATE;
4. the event insert, which takes the chain head.

The write path's trigger has its own locks; COMPOSITION.md item 5 gives the merged order.

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
- the final review's (P1-P4, once merged): the session judged after the LAST blocking acquisition of submit, with no staged value
  or token left on refusal; open_session re-reading factor and principal revocation after the replay-ledger wait; and the
  exception-safe idempotency staging. The write path's trigger adds its own waits (lease, subject, identifier locks): the port must
  place its final authentication check after all of them.

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
