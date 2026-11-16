# Composition with the write path (interface the decision core needs; GoodQA #16096)

Status: written down, NOT built. The write-path worktree (`feat/writepath-rescue`) is not touched. The earlier DESIGN.md sentence that
`wp.log.decide(item, based_on_version, ...)` has the same shape was wrong: the write path appends `kind='decision'` events through
`log.append`, binds them to `based_on_event_id`, and enforces everything in the `events_guard` trigger.

What the core needs from the write path, each as a testable requirement:

1. Sibling window inside the head transaction. `decision_heads` must keep, per (tenant, item): version, the last decision's (event, author kind,
   based_on, effect, verb), the prior value reference, status (current | contested | retracted) and the candidate list (bounded, humans only).
   The trigger implements `pol.core.apply` for decisions: stale unless sibling; contested opened only between two humans, never against a resolve;
   a candidate must adopt content that is open or closed by the competing decision (no rejected proposal can be installed); accept names the proposal
   by id and content hash; event ids unique per item (unique index).
2. Contested means no current value for every reader. Retrieval, injection, the projector and `execute requires current approve` must treat a contested
   item as having no current value (the previous value may be shown only as "last uncontested", labelled). Test: a reader through every adapter sees no
   value while contested.
3. Authority. The write path requires a user-typed source event in the same session, used once. The core lets listed agents accept or reject proposals
   authored by humans. One rule is needed: recommended, the write path's rule for anything that installs a value, and the core's agent rule only for
   reject and for accepting a human proposal when the authority map delegates it AND the write path's source rule is satisfied by a human-typed
   source on that proposal. Until then agent decisions are impossible on the write path.
4. Idempotency. The write path scopes keys per (tenant, session) and raises on reuse; the core scopes per principal, returns the ORIGINAL outcome on a
   retry (`duplicate` plus the first code), does not record refused events, and returns idem_conflict for the same key with another payload. Map the key
   to (tenant, principal, key) and keep the original outcome in the head row.
5. Tenant. Heads are per tenant (done there); the core now has a tenant in state, events and tables. The authority set and admins are per tenant.
   Revoke locks the tenant's authority row only (FOR UPDATE); heads are created lazily there, so a row list would miss new heads.
6. Lock order in the trigger: tenant authority FOR SHARE, then the head row FOR UPDATE; unique indexes on (tenant, idem) for applied events and
   (tenant, item, event id) as backstops; explicit isolation level; retry serialisation failures and deadlocks.
7. Erasure. The core keeps values, chains and candidates in plaintext JSON. For erasure to cover them they must be stored encrypted under the subject key
   or as references to write-path events whose payload is encrypted. Labels (including subjects and holds) stay in the clear and are what the erase
   decision reads. Erasure of a requirement value must also purge queued outbound items that reference it (the outbox re-checks existence at delivery).
8. Labels. Every installed value carries a COMPUTED label (join of the requirement label, the content it adopts and the event). `explain_for` and
   `table_for` are the only reader-facing views; the unscoped `explain` and `current_table` are administrative and must be audited.

Not proven until built: S1 to S6 against the real trigger, contested behaviour of `execute`, rebuild equals live for the write path's decision log under the
core reducer, and every invariant on the composed store.
