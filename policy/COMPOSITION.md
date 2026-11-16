# Composing the decision core with the write path (GoodQA #16096)

Status: design, NOT built and NOT tested together. Revised 2026-11-16 against the write path at e687fb1 (head of
feat/writepath-rescue: `writepath/wp/schema.sql`, `wp/anchor.py`, `wp/seal.py`, read and not modified; the earlier revision read
3944749). The decision core is at this branch's head: the rules run in plpgsql behind a session-keyed definer API (`pol/_pgcore.sql`,
`pol/pgcore.py`, R3-7), with `pol/_core.py` as the oracle. The core's rules are normative in CORE-RULES.md; the round-3 rules are an
ASSUMPTION until the user confirms them (non-humans only propose and remark; the per-requirement cap binds non-humans).

What changed in the write path since 3944749 and matters here:
- still no sessions table: `events.actor` and `events.trust_origin` are columns the APPLICATION writes (`wp_app` has INSERT on `events`;
  a check constraint limits the combinations, nothing ties them to an authenticated principal);
- chain links are HMACs under a per-tenant chain key computed by the application; the database checks linkage only, and truncation,
  rewrite and regression are detected against an external anchor (`wp/anchor.py`, AnchorStore with a file stand-in) and the checkpoint
  seal (`wp/seal.py`, 06f5f4c);
- every event carries the secret-scanner version (`secret_scan`, WPS21); the tenant's `identifier_epochs` row is pre-created at lease
  acquire;
- holds: only the legal role (`wp_legal`) places and releases holds; an erasure withholds every event another subject's active hold
  covers (`erasure_withheld`, D1) and the job stays open; the cascade is linear in the subject's events (D5).

## Recommendation: the write path owns the decision transaction

The decision should commit inside the write path's `events_guard` trigger transaction. The core's rules (CORE-RULES rules 1 to 15 and
section 4) are ported to plpgsql there. The Python reducer (`pol._core.reduce`) stays as the differential ORACLE, in the way B3 compares
`PgEngine` with `MemoryState`:
- the same event stream goes to both;
- the outcome codes must be equal;
- the comparable views must be equal (CORE-RULES section 8).

Why the write path, not a separate `dc.*` transaction:
- One log, one chain. The write path already has the append-only `events` table, with a keyed hash chain per session and an external
  anchor, owner-only triggers (WPS10/WPS11), lease fencing (WPS01/WPS12), subject erasure (WPS03) and legal holds (WPS16). `pol.pgcore`
  carries its own copy of each (`dc.events` triggers, `dc.chainhead` with an external anchor, `dc.erase_values`). Two logs would need
  two-phase agreement on every decision, and a crash between them leaves a decision in one log and not the other.
- The write path's guard already decides at the same moment the core needs: after the lease, subject and identifier locks, and inside
  `decision_heads ... FOR UPDATE`. Moving the core's head-moving rules there removes the second compare-and-set.
- The core is already plpgsql (`dc._rules`, `dc._reduce_apply`), so the port moves functions, not a Python reducer. What `pol.pgcore`
  proves is the acceptance list for the port: the login role holds no table privilege and reaches decision state only through
  session-keyed definer functions (`tests/test_pg_boundary.py`, R3-7), identity from an owner-written sessions row, append-only by
  grants and triggers, READ COMMITTED with a fixed lock order, original outcome on retry, constant cost to 20,000 proposals, no subject
  names in the log, erase/propose serialised (`tests/test_pg.py`), the overlap scenarios (B3) and the event-by-event differential
  against `pol/_core.py` with a mutated control (`exp/pg_diff.py`). The port is complete when those run green against the write-path
  schema.

The rejected alternative is for the decision service to own the transaction (`dc.*` tables in the same database) and call the write path
for the event. It is simpler to build, because the Python reducer runs unchanged. But it keeps two authorities for the same facts: holds,
erasure, leases and the chain. It also needs a cross-module lock order, which the next section shows is already tight.

## The nine items the composition must carry

### 1. Lease and session binding of tenant, principal and channel
Write-path rows carry `tenant_id`, `session_id` and `(lease_scope, lease_epoch)`. `wp_fence` takes `leases ... FOR SHARE` and raises WPS01
on a stale epoch. Events carry `actor` and `trust_origin`, but no principal and no channel, and at e687fb1 both are written by the
application role. For decisions that is the R3-7 defect in another place: whoever holds `wp_app` can claim `actor = 'user'`,
`trust_origin = 'user_typed'`.

The composition adds `sessions(token_hash, tenant_id, session_id, principal_id, channel, kind, expires_at, revoked)`, exactly as
`dc.sessions`: written only by an owner function executable by the identity adapter's role alone (`dc.open_session`, role `dc_idp`;
here `SessionAuthority.issue` after `IdentityAdapter.authenticate`), storing sha256 of a random token. `events_guard` reads that row
FOR SHARE right after the lease row and STAMPS principal and channel, and derives `actor`/`trust_origin` for decision-relevant
events from the session's kind and channel; an application-supplied value that disagrees is refused. Decision events reach `events`
only through a definer function keyed by the session token (the `dc.submit` pattern), never through a direct INSERT by `wp_app`.

The decision service never takes tenant, principal or channel from the request body: a body naming one is refused `identity_in_body`
(M1, `tests/test_findings.py::M1_IdentityFromSession`, E3 forge arm). The trigger therefore stamps them from `sessions`, not from the
event row.

### 2. Version to event-id mapping, including contests
The core binds to an integer `version`. The write path binds to `based_on_event_id` and compare-and-sets `decision_heads.last_event_id`
(WPS05). The mapping:
- version v is the event id that produced it (`head.last.event`, or the resolve or contest event);
- `decision_heads` keeps version, last_event_id and the core's head document;
- the head document holds status, value_ref, value_label, value_version, `last` (event, by, based_on, effect, verb, prev) and
  `contested` (base and the candidate list, at most MAX_CANDIDATES = 8, humans only).

Contests:
- A sibling bound to the version the head moved from is in the one-step window. That is CORE-RULES rule 14: the same based_on as
  `last`, a different principal, both human, and the last move not a resolve.
- The guard needs the previous event id to recognise a sibling. It is `last.based_on`, mapped to an event id. Today WPS05 refuses
  every sibling.
- `contested_joined` appends a candidate.
- `resolve` must bind to the contested version's event id.
- The write path's `decision_action` set (approve, amend, reject, execute) needs `supersede`, `retract`, `resolve`, `confirm` and
  `accept`, with `execute` kept as the write path's own action: it requires the current head action to be approve or accept.

### 3. "Humans only" maps to the write path's user_typed trust origin
In the core (round 3, ASSUMPTION), every head-moving event needs a human on an owner channel; a non-human may only propose and remark
(`agent_not_permitted`, `agent_cannot_confirm`).

The write path requires every decision to cite a `source_event_id` that:
- has `trust_origin = 'user_typed'`;
- is in the same session;
- is used once (WPS04/WPS13, unique index `decision_source_once`).

The mapping:
- A human decision cites the user_typed event that carried the typed confirmation.
- A remark (agent-created pending change) is confirmed by a user_typed event on a DIFFERENT owner channel (the core's
  `same_channel_confirmation`). The guard compares the session's channel with the pending row's channel.
- Agents no longer decide, so WPS04 needs NO relaxation (the earlier revision's agent accept/reject exception is gone with rule 11).
  An agent's proposal or remark is an ordinary event with `actor = 'assistant'`; only a human decision cites a user_typed source.
- `user_typed` must come from the session (item 1), not from the application, or the mapping proves nothing.

### 4. Labels are a schema change and a projector change
`events` has no label column, so labels are not in the hash chain. The composition needs three changes:
- add `label jsonb not null` to `events`, covered by `content_digest` or hashed alongside it, so a label cannot be changed without
  breaking the chain;
- add a label to `facts` and `inbox`;
- teach the projector to write each fact with the JOIN of the labels of the events it was derived from. The projector is the write
  path's equivalent of `Store.append_derived`. E1 shows the join is monotone and E5 shows derived records leak nothing when the join is
  applied.

The core's `value_label` (CORE-RULES section 5) is computed by the trigger, never taken from the caller. The value itself stays a
reference: the write path's encrypted content plays the role of the vault (item 8 and H7).

### 5. A global lock order that includes the trigger's existing locks
`events_guard` at e687fb1 takes, in order:
1. `leases` FOR SHARE (`wp_fence`); then the secret-scan version check (no lock);
2. `subjects` (event subject) FOR SHARE;
3. `identifier_epochs` FOR SHARE (the row is pre-created at lease acquire since 963e597, so no appender waits on another's insert);
4. mentioned `subjects` FOR SHARE;
5. the idempotency read;
6. `chains` (session) FOR UPDATE;
7. the source event check;
8. `decision_heads` FOR UPDATE.
The erasure cascade share-locks every subject a batch touches (`wp_erasure_apply`), and holds lock the subject row FOR UPDATE.

Holds (`wp_hold_place`) and identifier declaration lock `subjects` FOR UPDATE, then `identifier_epochs` FOR UPDATE. `pol.pgcore` takes the
tenant row FOR SHARE (FOR UPDATE for revoke, grant and erasure), then the (tenant, principal) advisory lock, then the head FOR UPDATE,
then the chain head. Propose holds the tenant row FOR SHARE from subject-token minting to commit so erasure cannot interleave; in the
composition that role is played by the subject rows (item 8).

The composed order, which every path must follow:
1. lease;
2. the `sessions` row (item 1);
3. subjects;
4. identifier_epochs;
5. mentioned subjects;
6. session chain FOR UPDATE;
7. the tenant authority row (FOR SHARE for decisions, FOR UPDATE for revoke and grant);
8. `decision_heads` FOR UPDATE;
9. the proposal rows of that requirement.

Consequences:
- revoke and grant, which lock every requirement's open proposals of the target, must take the authority row FOR UPDATE AFTER their own
  session chain and never hold a `decision_heads` row first;
- a hold or erasure (subjects FOR UPDATE) never takes the authority row or a head;
- the advisory lock disappears, because idempotency becomes the write path's per-session unique key (item 1). The core's per-principal
  idempotency scope becomes per-session, with a principal-scoped unique index added if one principal may hold several live sessions.
  This is a semantic change, recorded here so it is decided explicitly.

### 6. A stale lease is quarantined, not refused
WPS01 raises on a stale epoch, so a zombie writer's decision would be dropped. The composition catches WPS01 in the decision service and
writes the event to the write path's bounded `quarantine` table (`quarantine_guard`, per-session quota). It returns
`quarantined_stale_lease` to the caller, and the head does not move. A human (or the session's new lease holder) re-submits from
quarantine, bound to the current head. Nothing a user typed is lost, and nothing stale is applied.

### 7. Role separation: the database role boundary is the enforced one
The write path's model is `wp_owner` (owner, NOLOGIN in effect), `wp_app` (no DELETE or TRUNCATE anywhere, no write on leases, subjects,
chains or decision_heads, but INSERT on `events`), `wp_verifier` and `wp_legal` (holds only). The decision tables follow `pol.pgcore`'s
round-3 pattern, which replaced the earlier "SELECT-only plus `dc.apply_authority`" design after the R3-7 forgery (an app-role event
row fed to the authority function granted admin):
- `principal`, `role`, `sessions`, heads, proposals, values and subject mappings are owned by `wp_owner` and carry NO privilege for any
  login role, not even SELECT;
- decision events are written only by session-keyed SECURITY DEFINER functions (`search_path` pinned) that build the event from the
  session row and apply revoke and grant in the same transaction from the event they judged; no function applies an event row someone
  else wrote;
- reads go through definer functions that return only what the session's principal participates in; label-level filtering stays in
  the in-process policy point, which is trusted code, not an enforced boundary.

The write path's `wp_owner_only` triggers (WPS10) cover the tables. Consequence for the write path: `wp_app`'s INSERT on `events` must
be narrowed so a decision event cannot be inserted directly (a definer function, or a guard that refuses `kind = 'decision'` from
`wp_app`).

### 8. One source of truth for legal hold
The write path's `legal_holds` (`wp_hold_place`, `wp_hold_release`, `wp_hold_check`, WPS16) is the only record of holds. The policy's
`hold_events` and the label `holds` dimension are DERIVED from it:
- at decision time the PDP's erase rule asks `wp_hold_check(tenant, subject)` for every subject in the label;
- a held subject makes the decision `erasure_queued`, never a separate list in the policy document;
- the write path's erasure job then waits on the hold, as it does today.

In this branch the hold list is the policy's `hold_events`, which is the lab stand-in. The user's decisions are in force here: a recorded
legal hold beats erasure, the erasure is queued, and erasure covers what others said about the subject.

At e687fb1 the write path's erasure already withholds events another subject's hold covers (`erasure_withheld`) and keeps the job open,
which is the user's rule (a hold beats erasure; the erasure is queued) and covers mentions (what others said about the subject).

Erasure also constrains the ported core: `events_guard` writes to an append-only table, so the event label must carry opaque subject
ids, never names. In `pol.pgcore` the token is random and the name-to-token mapping is an owner-only table deleted at erasure once no
value about the subject remains. In the composition the write path's `subject_id` (names live only as HMACs in `subject_identifiers`,
deleted after the rescan) is that token, so the core needs no mapping of its own.

### 9. N2, N3 and the round-3 rules are fixed before porting
On this branch:
- proposals are rows with per-principal (16) and per-requirement non-human (256) open caps; a human is never locked out by non-human
  items (R3-4, B1 I18 with control `cap_blocks_humans`). Cost is flat to 20,000 proposals in memory and in Postgres
  (`tests/test_pg.py::test_proposal_cost_linear_to_20000`; 6.21 to 7.32 s per 2,000 proposals on t2-dev with the plpgsql core);
- a non-human never moves the head or vetoes (round 3, ASSUMPTION; B1 I2 with control `agent_may_decide`);
- a human who reads the head succeeds within (open human-authored proposals at the read) + 1 attempts (CORE-RULES section 7, B1 L2).

The port must carry these rules and their tests. The write path's own `decision_heads` has neither proposals nor a non-human rule today.

## Not proven by this document
- No composed build exists. Every statement about the write path is read from its code at e687fb1.
- Whether one principal may hold several live sessions decides the idempotency scope (item 5). `pol.pgcore` keeps idempotency per
  (tenant, principal, idem) with the channel in the fingerprint (B3 S8: two sessions of one principal on one channel get `duplicate`).
- Narrowing `wp_app`'s INSERT on `events` (item 7) and stamping actor/trust_origin from a sessions row (item 1) are write-path changes
  that need the write-path owner's review.
- The keyed chain and anchor of the write path and the decision core's chain and `FileAnchor` are two designs for the same property;
  the composition keeps the write path's.
