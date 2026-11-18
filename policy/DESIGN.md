# Policy decision point and decision core (GoodQA #16096): design after the rescue rebuild

Code is in `pol/`, experiments in `exp/`, independent verifiers in `verify/`, pre-registered thresholds in `THRESHOLDS.md`, core rules in
`CORE-RULES.md`, and the write-path composition in `COMPOSITION.md`. No concrete policy ships. The format, the validator (`parse_policy`),
the examples and default deny are what ships.

## Trust boundary (N1, H1, M1)
- Where the boundary is ENFORCED. In a Postgres deployment the enforced boundary is the database role boundary (`pol/_pgcore.sql`, see
  Postgres below): the request handler's role `dc_app` holds no table privilege and can only call the session-keyed API, so nothing
  the Python process does can forge identity or decision state in the database. Everything in-process (`pol.enforce`, `pol.decisions`,
  the private `_` names) is TRUSTED code: Python cannot stop code in the same process from reaching private names, and the surface
  tests only enforce what the package exports and imports. Label filtering of reads and the legal-hold check of erasure are in that
  trusted part.
- `SessionAuthority.issue(credential)` (the authentication path) creates a `Session` only from a credential the identity adapter
  (`pol.identity.IdentityAdapter.authenticate`) verified; principal and channel come from the adapter's Assertion, and the channel must
  match the principal's kind (R3-1). A session is valid only if it is the very object the authority issued, by identity. A hand-built
  Session, a copy of one, a Principal or a string is refused. `tests/kit.LabIdP` is a test double, never shipped.
- Every attribute a decision uses comes from the `Directory` by the session's principal id at the moment of use: tenant, groups,
  clearance, restricted flag, kind, region and model. An absent entry denies `unknown_principal`. A directory that raises denies
  `directory_error:*`.
- `Enforcer(policy_source, directory, sessions, audit)`: every argument is required and type-checked. The clock is the runtime's.
  There is no clock parameter anywhere; tests change it only through `tests/kit.FixedClock`.
- The PDP (`pol/_pdp.py`) is private. A request may only NAME a destination, an authorisation, a recipient id, or narrowing credential
  claims. Any other request key denies `forged_request`.
- Credential keys are an exact ASCII allow-list (`cap_clearance`, `cap_actions`, `cap_restricted`). Anything else denies
  `attr_unrecognised_key` (H2).
- The decision service stamps tenant, principal and channel from the session. A body naming any of them, or an `id`, is refused
  `identity_in_body` (M1).
- The public surface is enforced mechanically (`tests/surface.py`, `tests/test_surface.py`). There is:
  - no optional safety dependency;
  - no parameter through which a caller supplies identity, tenant, clock, labels or recipient, or switches a check off;
  - no `**kwargs`;
  - no unscoped read;
  - no production import of tests-only code.
  A planted package shows the scanner catches each rule. Six value-type or wiring parameters are allow-listed, each with a justification.

## Labels and egress (H7, H8, E1, E2, E4, E5)
- The label lattice is unchanged (`pol/labels.py`): ten dimensions; derived data gets the join.
- Record ids are store-assigned. A new record's label must be at least the join of its lineage. Derived records (`append_derived`,
  model answers) get the join, never a caller label.
- Ports (`GuardedChat`, `GuardedEmbedder`, `GuardedExport`) and the `Outbox` take record ids, never text or labels.
- The Outbox queues references. At delivery it re-reads existence and CURRENT labels, which is mandatory. It resolves the recipient from
  the directory ONCE and hands that same object to the decision and to the sink.
  - A real-thread race test shows 0 wrong sends with production code, against 200 of 200 for the control that looks the recipient up
    again. Overlap counts are reported.
- Pinned-model drift is checked on the answer, after the request has reached the model. That is the most a pin can do.
- `Dest.local` is declared, not attested.
- `GuardedChat` takes no authorisation, so restricted content reaches a model only through `Outbox(send_model)` with an authorisation.

## Model ids and bans (M3)
- Ids are canonical: NFKC, casefold, strip, ASCII `[a-z0-9._-]`. Non-ASCII (confusables, combining marks) is refused.
- A partial skeleton (rn to m, vv to w, 0 to o, 1 or i to l, 5 to s, separators dropped) refuses a look-alike directory entry and makes
  revocation over-match. ASCII look-alikes outside that table are NOT covered.
- A model is declared under a family: its id is the family, or the family followed by a separator (`- _ . : @ /`).
  - A ban `f` covers every declared model of family f, with all versions, suffixes and tags.
  - A ban `f@v` covers version v.
  - Family comes from the declaration only. `gpt-x` does not cover family `gpt-xl`.
  - An undeclared pinned model denies.
- Aliases are canonicalised and followed to a fixed point. A cycle or a dangling alias is a PolicyError. This holds wherever a Policy is
  built, since validation is in `Policy.__post_init__`.

## Decision core (N2, N3, H6, M2; CORE-RULES.md)
- `pol/_core.reduce(view, event) -> (code, original_code, delta)`. The view gives point reads only; the delta changes a bounded number
  of rows. The memory engine (`MemoryState`) and the Postgres engine (`pgcore._View`) implement the same view.
- Proposals and pending changes are their own rows. Proposing needs a role (decide authority or proposer). Open items are capped at 16
  per principal per requirement, and at 256 NON-HUMAN-authored open items per requirement (the per-requirement cap never locks a human
  out, R3-4). Cost per event is constant: a deterministic count, plus timing to 20,000 proposals in memory and in Postgres.
- ASSUMPTION (round 3, CORE-RULES.md; the reviewer's safe default, not yet confirmed by the user): non-humans only propose and remark.
  Every head-moving type (confirm, decide, supersede, retract) by a non-human is refused `agent_not_permitted` (confirm keeps
  `agent_cannot_confirm`), so an agent can neither move the head nor veto a proposal (R3-5). Head moves need a human on an owner
  channel. `self_approval` and `no_value_change` are gone with the rule they guarded.
- Liveness: a human who reads the head is applied within (open human-authored proposals at the read) + 1 attempts.
- Concurrency: the one-step sibling window gives `contested`, bounded at 8 human candidates, ended by `resolve`.
- Event ids are `<principal>/<client_id>`, so no id can be squatted. Idempotency is per principal and returns the ORIGINAL outcome code
  on retry.
- Values are referenced, not held. The vault keeps the text under a keyed reference, tagged with the label's subjects.
  `erase_subject` deletes every value ABOUT the subject, whoever wrote it. A value under a recorded legal hold is queued instead.
- The append-only event log and the core state never hold subject names. The service puts a label into the event only after each
  subject is replaced by a per-tenant token. The token is RANDOM in both engines (`subj-<random>`; memory `MemoryEngine._event_label`,
  Postgres minted under the tenant lock and the subject's lock), never computed from the name or from any key, and the name-to-token mapping is kept only
  by the engine (Postgres: the owner-only `dc.subject_tokens`). Erasure deletes the mapping once no value about the subject remains;
  after that no row links the token to the name, no party (a key holder included) can confirm a guessed name against a token left in
  the log, and a re-appearing subject gets a fresh token (`tests/test_round3.py::test_tokens_are_unconfirmable_after_erasure`,
  `tests/test_pg.py::test_tokens_unconfirmable_after_erasure_in_the_database`). Value refs are RANDOM too (`val-<random>`, review-3
  N5), never derived from the text, the label or a subject
  (`tests/test_review3.py::test_value_ref_cannot_be_confirmed_by_guessing_the_subject_after_erasure`).
- Reads are viewer-scoped (`explain(session, req)`, `table(session)`): only values, proposals and candidates the viewer may read.
  `hidden_candidates` exists only when the policy opts in (`conflict_notice: existence_only`). The default is NONE.
- Every refusal is written to the audit log with its code. The audit log grows by one row per checked request. Bounding it (rotation,
  retention) is outside this module and not done.
- Value encryption is an interface only (`_ValueCipher`): in the composed system the write path's KeyStore owns the per-subject keys.

## Postgres (`pol/pgcore.py`, `pol/_pgcore.sql`): the enforced boundary
- Roles:
  - `dc_owner` (NOLOGIN) owns every table, trigger and function.
  - `dc_app` (the request handler) holds NO table, column or sequence privilege. It may only EXECUTE `dc.submit`, `dc.explain`,
    `dc.read_table`, `dc.erase_candidates` and `dc.erase_values`: SECURITY DEFINER, `search_path` pinned, each keyed by a session token.
  - `dc_idp` (the identity adapter) may only EXECUTE `dc.open_session`, which stores sha256 of a fresh random token for the
    principal and channel of the factor that made the proof (channel must match the principal's kind), and `dc.end_session`. Python:
    `PgSessionBridge` (dc_idp) and `PgDecisionService` (dc_app).
  - Logins use SCRAM. Passwords are read from 0600 files; only verifiers computed client-side are sent. Passwordless connections are
    refused on TCP and on the socket.
- Identity comes only from the sessions row; a body naming tenant, principal, channel or id is refused `identity_in_body`. Revoke and
  grant are applied inside the same function from the event it judged (`dc.apply_authority` is gone, R3-7).
- The decision rules run in plpgsql (`dc._rules`, a port of `pol/_core.py`). `pol/_core.py` stays as the oracle: `exp/pg_diff.py`
  replays the B2 logs and every B1 transition through both and compares codes and views, with a mutated-SQL control.
- `dc._apply_event`, `dc._lock_event` and `dc._reduce_apply` are the raw core: OWNER-ONLY, for the differential and B3; no login role
  can execute them (`tests/test_pg_boundary.py`). The same holds for the internals of S1 (`dc._peek`, `dc._pin`, `dc._gate`,
  `dc._judge_proof`, `dc._acquire_submit`, `dc._submit_stage`, `dc._reduce_stage`, `dc._apply_authority`, the inner functions behind
  each API function) and for `dc.revoke_factor`, which is owner-only.
- `dc.events` is append-only by grants plus triggers: UPDATE, DELETE and TRUNCATE are refused for the owner too. `DISABLE TRIGGER`
  needs ownership.
- The hash chain is computed in a trigger under a per-tenant chain-head lock and verified in the database (`dc.verify_chain`). A rewrite
  by a superuser with triggers bypassed is detected only against an EXTERNAL anchor (`anchor_export` / `anchor_check`, `FileAnchor`):
  an owner who recomputes the whole chain passes `dc.verify_chain` (R3-8).
- Erasure is two calls: `dc.erase_candidates` lists the values about a subject; the in-process policy point applies the hold decision;
  `dc.erase_values` deletes the chosen ones under the tenant lock T held exclusively and deletes the subject mapping only if no value
  about the subject remains. A submit holds T shared from its acquisition to commit, so erasure and minting cannot interleave.
- Isolation is READ COMMITTED, set explicitly. Lock order (S1, FINAL-JUDGMENT.md 4.2; levels in increasing order, keys sorted inside
  a level, checked at run time by `dc._at`, SQLSTATE `DCORD`):
  - L0 relation locks, one table per statement, in a fixed table order;
  - L1 the authority lock A(tenant, principal) (advisory): shared by every call of the principal, exclusive for a revoke of it, an
    `dc.end_session` of one of its sessions and `dc.revoke_factor`;
  - L2 the authority rows: the caller's session, factor and principal FOR SHARE (the pins); a revoke's target principal row and
    session rows FOR NO KEY UPDATE;
  - L3 the tenant lock T (advisory): shared by submits, exclusive for an admin's revoke or grant and for erasure;
  - L4 the per-principal idempotency lock; L5 data rows (head, a revoke's props, erasure's value and token rows); L6 subject locks;
    L7 the chain head; L8 the nonce lock of `dc.open_session`.
  The owner-only raw path (`dc._lock_event`) takes L1-L5 without a budget and leaves the chain head to the event insert.
- Acquire, judge, apply (S1; final review P1 and P4, and the coordinator's structural brief). The invariant, for every entry point
  (`dc.submit`, `dc.explain`, `dc.read_table`, `dc.erase_candidates`, `dc.erase_values`, `dc.open_session`):
  - J1. Every lock of the call is taken BEFORE the judgment, the authority rows among them (`dc._pin`; `dc._judge_proof` for a
    proof). A locking read that waited locks and returns the newest version (PostgreSQL 13.2.1), and no transaction can change a
    pinned row until the call ends: a revoke, an `end_session`, a factor revoke or direct owner SQL waits for the call. A change
    committed during a wait BEFORE the pins is judged by the pins (the call is refused); one attempted after them waits.
  - J2. Each acquisition statement waits for one lock, with `lock_timeout` set by `dc._budget` to the smaller of the caller's own and
    the credential's remaining life (session `expires_at`, proof `expires`), and XWAIT = 2 s for an exclusive A or T. A timeout after
    the expiry is a refusal; one before it is re-raised (55P03, retryable). So no call waits past its expiry by more than the timer
    latency, except the token lookup itself (`dc._peek`, which can wait only for owner DDL on `dc.sessions`, under the caller's own
    `lock_timeout`; a call that waited there is refused if the token expired meanwhile).
  - J3. After the acquisition `lock_timeout` is 1 ms: the staged writes, the final judgment (`dc._gate`, the wall clock read last
    inside `dc._session`) and a revoke's authority writes never wait. A wait there fails the call (55P03) and writes nothing
    (`tests/test_s1_pg.py::S1_ApplyNeverWaits`). Each inner function carries a SET clause, so the caller's `lock_timeout` is restored
    on every exit.
  - J4. What is judged: the session row is not ended or revoked, unexpired on the wall clock, its factor exists and is unrevoked
    (use-time factor revocation), its principal is unrevoked, and for erasure the principal is an admin. `dc.open_session` judges the
    whole proof on the pinned rows, and again after the prune, the replay-ledger insert and the session insert.
  - J5. Writes before the judgment are staged in a subtransaction that a refusal rolls back (SQLSTATE `DCGAT`); after it a call
    writes only a revoke's own authority writes, on rows locked at L2.3-2.4 (every session of the target, none skipped), and a
    refusal's audit row. A revoke's authority writes never refuse their own caller (a self-revoke is applied).
  - J6. Only READ COMMITTED, by exact comparison (below), checked before the first lock.
  The blocking points and their tests are in `tests/test_s1_pg.py` (points after the pins: the mutation waits for the call, which
  applies first; the expiry ceiling; the lock order; mutual waits) and `tests/test_final_pg.py` (points before the pins; each test
  witnesses the wait with `pg_blocking_pids`, so none passes because nothing waited). The memory `DecisionService` follows the same invariant: `MemoryEngine._submit` runs the service's
  final judgment inside the engine lock, after the key-lock and engine-lock waits and before the reduction; `explain` and `table`
  judge after their reads; erasure judges inside the vault lock before each deletion (`tests/test_final.py`). `PgDecisionService`
  judges the in-process authority the database cannot see (the in-process session, the directory, the policy document's
  revocations) again AFTER its database call: `dc.submit` and `dc.erase_values` run in their own transaction, rolled back when that
  judgment refuses (`principal_revoked`, or `unauthenticated:invalid_session`; erasure `unauthorized`); reads are rendered by the
  in-process service, which judges after its reads (`tests/test_final_pg.py::P1_PgServiceJudgment`). Erasure also makes each
  value's in-process erase decision (a recorded legal hold queues it; the tenant's `erase` grant) again after `dc.erase_values`
  and before COMMIT, as the memory engine decides each value at its deletion: when a decision changed during the wait the call is
  rolled back, those values are counted as queued or denied and the rest are erased in a fresh call that is judged again (each
  round drops at least one value). A value denied before the call and allowed after it is left (`incomplete`).
- Caller contract (restated from the code at the final review; it replaces the delta review 1 M1 text of 8928870):
  - Every `dc_idp` and `dc_app` call must run at READ COMMITTED, the default. READ UNCOMMITTED is refused like REPEATABLE READ and
    SERIALIZABLE: Postgres runs it as READ COMMITTED, but the guard compares the level name exactly and does not rely on that. Under
    any other level `dc.open_session` raises `proof refused: isolation`, `dc.end_session` raises `end_session refused: isolation`, and
    every session-keyed call answers as for an invalid session:
    - `dc.submit` returns `unauthenticated:invalid_session`;
    - `dc.explain` returns null, and `dc.read_table` returns `[]`;
    - `dc.erase_candidates` returns `{refused: unauthorized, values: []}`, and `dc.erase_values` returns `{refused: unauthorized}`.
    The reason: such a snapshot would outlive `end_session`, a principal revoke or a factor revoke.
  - Every session-keyed call pins its authority before it judges (J1). A session that ended, lost its factor or lost its principal
    during a wait before the pins, or expired before the final judgment, gets the answers above (`dc.submit`:
    `unauthenticated:invalid_session`, or `principal_revoked` with an audit row for a revoked principal), and nothing the call staged
    is kept. An end, revoke or factor revoke attempted after the pins waits for the call. This replaces the earlier sentence
    "`dc.submit` and `dc.erase_values` judge the session again after their lock waits", which was false for `dc.submit` (its
    subject-token, value, event and revoke writes waited after that judgment) and did not cover the read calls.
  - `dc.end_session` isolation guard (decision, final review P5): it raises unless READ COMMITTED. Under REPEATABLE READ or
    SERIALIZABLE its update reads the transaction's snapshot, so a session opened after that snapshot was "not found": it answered
    false and the session stayed live. A logout that fails loudly is better than one that reports "no such session" for a live one,
    and the identity adapter (`PgSessionBridge`) already runs at READ COMMITTED in autocommit, so no correct caller changes.
  - A revoked factor ends the sessions it opened at their next call (J4). Revoking a principal ends every session of it: the revoke
    waits (FIFO, XWAIT per attempt) for an open `dc.end_session` or `dc.open_session` of that principal instead of skipping its row.
  - Proof and session expiry are judged on `clock_timestamp()` (the wall clock), not on the transaction start, so a token or proof
    that expires inside a long caller transaction is refused at its next call.
  - The judgment is a point inside the call. A caller that keeps its own transaction open after a call commits that call's effects
    later; an end, revoke or factor revoke waits for that COMMIT (the pins and the authority lock are held until then), XWAIT per
    attempt. `PgDecisionService` and `PgSessionBridge` use autocommit. `pol.pgcore.install` sets
    `idle_in_transaction_session_timeout` = 60 s as the role default of `dc_app` and `dc_idp`; a caller's own `lock_timeout` is
    honoured and never lengthened.
  - Stalled holders (FINAL-JUDGMENT.md 4.4; ASSUMPTION, the policy owner's choice, not the user's). The idle timeout bounds only a
    caller that leaves it alone. When a revoke, `end_session`, `dc.revoke_factor`, grant or erasure has waited XWAIT for A or T
    exclusively, and its own caller is pinned and judged, it takes the fence of that lock and then ends (`pg_terminate_backend`)
    the backends of this database, other than superusers and members of dc_owner, that already held that lock (granted) when its
    request began and still hold it in the same transaction, unless they are a call in progress on that key (they hold its marker,
    advisory class 1145241607, and the key or its fence exclusively; review of 6b77354 N1, N3, N4); then it tries again, at most
    three rounds, never after its caller's expiry. Waiters are never ended. Only `dc._evict` signals, as the NOLOGIN role dc_evictor
    (member of `pg_signal_backend` only; EXECUTE for dc_owner only). While it evicts it holds the fence, and new shared callers of
    that key refuse with 55P03. Owner and superuser holders are never evicted; the revocation then gives up with 55P03. A caller
    that keeps a transaction open after a call should expect its connection to be ended if that transaction blocks a revocation
    for more than XWAIT (2 s). The advisory-lock functions are not executable by any role but the owner and dc_owner in the policy
    database (install removes PUBLIC and every explicit grant).
  - Lock identities (review of 2262eaa; ASSUMPTION, the fixer's choice): A and T, and their fences, use the `lockid` of the
    principal or tenant row (one sequence, unique, fixed at insert), so tenants never share an A or T key; the subject lock is
    classed by tenant. A revoke or grant takes its exclusive locks only when an unlocked pre-read predicts it applies: the stage's
    body checks, the event's shape, its idempotency key and the rules' facts (review of 6b77354 N2).
  - Two-phase commit (review of 2262eaa F2a; ASSUMPTION, the fixer's choice): install and every API call refuse while
    `max_prepared_transactions > 0` (SQLSTATE 55000), because a prepared transaction would keep a call's locks where no
    eviction can reach them.
  - New SQLSTATE 55P03 from a call: a wait before the expiry ended by the caller's own `lock_timeout` or by XWAIT on an exclusive
    lock, or a wait after the acquisition. Nothing was applied; the call is retryable.
- Revoke, grant and erasure serialise the tenant (T exclusive), which is a scaling limit; so do all submits of a tenant from the
  chain-head lock (L7) to COMMIT.

## Not proven / limits (see also each result's `not_proven`)
- Python cannot stop in-process code from reaching private names. The surface test enforces what the package exports and imports; the
  enforced boundary is the database role boundary, and the in-process internals are trusted code.
- Label filtering of reads and the hold decision of erasure are in-process (trusted), not enforced by the database.
- The database canonicalises subjects with lower() and NFKC, which can differ from Python's casefold on rare characters; Python
  canonicalises first and the database refuses non-canonical input (best effort).
- The skeleton table is partial. Unicode confusables are refused by the ASCII rule, but ASCII look-alikes outside the table are not.
- Audit growth is unbounded in this module.
- Residues of the final judgment (by design): a refused `dc.submit` leaves sequence gaps (its staged inserts are rolled back); a
  subject-token mapping minted by a call that the CORE refused (not the final judgment) stays until that subject is erased (it links
  to no value); a login role can hold the locks and rows of its calls in an open transaction (the advisory-lock functions
  themselves are no longer PUBLIC in the policy database), and so delay other callers, at most until each caller's expiry (J2).
  A held A or T no longer blocks revocations past XWAIT: the holder is evicted (FINAL-JUDGMENT.md 4.4, ASSUMPTION). Disclosed
  residuals of eviction: an evicted backend loses its whole connection, including unrelated work, and for T that can be a
  legitimate submit of another principal that held T longer than XWAIT (retryable, nothing of it committed); the fence refuses
  the target's or tenant's new calls (55P03) while a revocation evicts; a stalled submitter also holds the chain head (L7), so the
  tenant's other submits are refused at their expiry until a revoke, grant or erasure of the tenant (or a revoke of the staller)
  evicts it. That is a denial of service ended by an operator's action, never a wrong decision; not measured.
- The pin is checked after the call. Local endpoints are not attested.
- The composed system (COMPOSITION.md) is not built.
