# Composing the decision core with the write path (GoodQA #16096)

Status: design, NOT built and NOT tested together. This document was written against the write path at commit 3944749
(`writepath/wp/schema.sql`), which was read and not modified. The rescue worktree's HEAD has moved since (963e597), and that commit was
not re-read. The decision core is at this branch's head (`pol/_core.py`, `pol/pgcore.py`). The core's rules are normative in
CORE-RULES.md.

## Recommendation: the write path owns the decision transaction

The decision should commit inside the write path's `events_guard` trigger transaction. The core's rules (CORE-RULES rules 1 to 15 and
section 4) are ported to plpgsql there. The Python reducer (`pol._core.reduce`) stays as the differential ORACLE, in the way B3 compares
`PgEngine` with `MemoryState`:
- the same event stream goes to both;
- the outcome codes must be equal;
- the comparable views must be equal (CORE-RULES section 8).

Why the write path, not a separate `dc.*` transaction:
- One log, one chain. The write path already has the append-only `events` table, with a hash chain per session, owner-only triggers
  (WPS10/WPS11), lease fencing (WPS01/WPS12), subject erasure (WPS03) and legal holds (WPS16). `pol.pgcore` re-implements a weaker copy
  of each (`dc.events` triggers, `dc.chainhead`, `dc.apply_authority`). Two logs would need two-phase agreement on every decision, and
  a crash between them leaves a decision in one log and not the other.
- The write path's guard already decides at the same moment the core needs: after the lease, subject and identifier locks, and inside
  `decision_heads ... FOR UPDATE`. Moving the core's head-moving rules there removes the second compare-and-set.
- What `pol.pgcore` proves (append-only by grants and triggers, chain verified in the database, definer-only authority writes,
  READ COMMITTED with a fixed lock order, original outcome on retry, linear cost to 20,000 proposals) is the acceptance list for the port.
  The port is complete when those tests run green against the write-path schema.

The rejected alternative is for the decision service to own the transaction (`dc.*` tables in the same database) and call the write path
for the event. It is simpler to build, because the Python reducer runs unchanged. But it keeps two authorities for the same facts: holds,
erasure, leases and the chain. It also needs a cross-module lock order, which the next section shows is already tight.

## The nine items the composition must carry

### 1. Lease and session binding of tenant, principal and channel
Write-path rows carry `tenant_id`, `session_id` and `(lease_scope, lease_epoch)`. `wp_fence` takes `leases ... FOR SHARE` and raises WPS01
on a stale epoch. Events carry `actor` and `trust_origin`, but no principal and no channel.

The composition adds `sessions(tenant_id, session_id, principal_id, channel, kind)`. It is written only by an owner function on the
authentication path, which is `SessionAuthority.issue` here. `events_guard` reads that row FOR SHARE right after the lease row.

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
In the core, a head move needs a human on an owner channel. The only non-human head moves are an agent accepting an open
human-authored proposal whose value differs (amendment 3, `no_value_change`) and an agent reject, which never moves the head.

The write path requires every decision to cite a `source_event_id` that:
- has `trust_origin = 'user_typed'`;
- is in the same session;
- is used once (WPS04/WPS13, unique index `decision_source_once`).

The mapping:
- A human decision cites the user_typed event that carried the typed confirmation.
- A remark (agent-created pending change) is confirmed by a user_typed event on a DIFFERENT owner channel (the core's
  `same_channel_confirmation`). The guard compares the session's channel with the pending row's channel.
- An agent accept or reject has no user_typed source. The guard must allow `actor = 'assistant'` decisions only for `accept` of a
  human-authored open proposal and for `reject`, which is exactly rule 10c/10e/11. Otherwise WPS04 has to be relaxed for those two
  cases, and only those.

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
`events_guard` today takes, in order:
1. `leases` FOR SHARE (`wp_fence`);
2. `subjects` (event subject) FOR SHARE;
3. `identifier_epochs` FOR SHARE;
4. mentioned `subjects` FOR SHARE;
5. the idempotency read;
6. `chains` (session) FOR UPDATE;
7. the source event check;
8. `decision_heads` FOR UPDATE.

Holds (`wp_hold_place`) and identifier declaration lock `subjects` FOR UPDATE, then `identifier_epochs` FOR UPDATE. `pol.pgcore` takes the
tenant row FOR SHARE (FOR UPDATE for revoke and grant), then the (tenant, principal) advisory lock, then the head FOR UPDATE, then the
chain head.

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

### 7. Role separation for the authority tables
The write path's model is `wp_owner` (owner, NOLOGIN in effect), `wp_app` (no DELETE or TRUNCATE anywhere, no write on leases, subjects,
chains or decision_heads) and `wp_verifier`. The authority tables (`principal`, `role`, and the `sessions` table of item 1) follow
`pol.pgcore`'s pattern:
- they are owned by `wp_owner`;
- they are SELECT-only for `wp_app`;
- they are written only by SECURITY DEFINER functions that apply an already-logged `revoke` or `grant` event once, after re-checking
  that its author is an unrevoked admin (`dc.apply_authority`, `tests/test_pg.py::test_revoke_through_definer_function_invalidates_open_items`).

The write path's `wp_owner_only` triggers (WPS10) cover them.

### 8. One source of truth for legal hold
The write path's `legal_holds` (`wp_hold_place`, `wp_hold_release`, `wp_hold_check`, WPS16) is the only record of holds. The policy's
`hold_events` and the label `holds` dimension are DERIVED from it:
- at decision time the PDP's erase rule asks `wp_hold_check(tenant, subject)` for every subject in the label;
- a held subject makes the decision `erasure_queued`, never a separate list in the policy document;
- the write path's erasure job then waits on the hold, as it does today.

In this branch the hold list is the policy's `hold_events`, which is the lab stand-in. The user's decisions are in force here: a recorded
legal hold beats erasure, the erasure is queued, and erasure covers what others said about the subject.

### 9. N2 and N3 are fixed before porting
On this branch:
- proposals are rows with per-principal (16) and per-requirement (256) open caps. Cost is flat to 20,000 proposals in memory and in
  Postgres (`tests/test_pg.py::test_proposal_cost_linear_to_20000`; 13.1 to 14.3 s per 2,000 proposals on t2-dev);
- a non-human event that changes no value never moves the head (rules 11 and 15, B1 invariant I16);
- a human who reads the head succeeds within (open human-authored proposals at the read) + 1 attempts (CORE-RULES section 7, B1 L2 with
  the `n3_agent_reject_moves_head` control found).

The port must carry these rules and their tests. The write path's own `decision_heads` has neither proposals nor a non-human rule today.

## Not proven by this document
- No composed build exists. Every statement about the write path is read from its schema at 3944749.
- Whether one principal may hold several live sessions decides the idempotency scope (item 5).
- WPS04 relaxation for agent accept and reject (item 3) needs the write-path owner's review.
