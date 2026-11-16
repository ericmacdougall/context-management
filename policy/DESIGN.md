# Policy decision point and decision core (GoodQA #16096)

Code in `pol/`, experiments in `exp/`, pre-registered thresholds in `THRESHOLDS.md`, results in `results/` (stamped).

## A. Policy decision point (`pol/pdp.py`, `pol/labels.py`, `pol/enforce.py`)

`decide(principal, action, resource_labels, context, *, policy) -> Decision(allow, reason, policy_version)`; pure, default deny.
Actions (the choke points): append, read, inject, send_model, embed, send_inbox, export.

Label on every record: tenants, ACL, regions, sensitivity, allowed models, banned models, local-inference flag, restricted-channel flag.
- ACL is CNF (a principal needs one group from every clause); join = union of clauses, so readers of a derived record are the
  intersection of its inputs' readers. Region and allowed models join by intersection (None = any), bans by union, flags by OR,
  sensitivity by max, tenants by union (a set of more than one tenant is readable by nobody: no cross-tenant derivation).
- `decide` joins every input label at the moment of use. `derive()` is the only way to build a derived Block; `Store.append`
  refuses a record whose label is below the join of its declared lineage.
- Identity (tenant, id, groups, clearance, restricted flag) comes only from the Principal. Credential and derived attributes can
  only narrow (`cap_clearance`, `cap_actions`, `cap_restricted`); a key that normalises (NFKC, case, whitespace, zero-width,
  separators) to an identity key, or a non-string key, denies with `attr_clash`.
- Any exception, malformed label or principal, or an extra rule that does not return exactly `True` denies.
- Destination attributes (region, local, model id, programme, sensitivity ceiling) come from the Policy, never from the request.
- Restricted-channel data needs a destination that belongs to a programme and an unexpired authorisation for that programme and tenant.
- `Enforcer.check` reads the policy source at every call (policy re-evaluated at use) and logs every decision with the policy version.
  `Outbox` re-checks at delivery with the current directory entries of sender and recipient. A pinned model that answers with another
  model id is a `pin_drift` denial and the answer is dropped. `PolicyDenied` is not a `PortError`, so no failover may retry it elsewhere.

### Conflict notice (decision made here, needs the user's confirmation)
Item L visible, item X (conflicts_with L) not readable by the viewer: the viewer gets exactly
`{kind: "conflict", item: L, version: L.version, state: "contested_by_restricted_item"}`, one per L. Not revealed: X's id, thread, group,
content, value, decider, version, how many X there are. Policy option `conflict_notice: none` removes even this. A generic
"context withheld" placeholder in injected prompts still tells the viewer that something was withheld.

## B. Decision core (`pol/core.py`, `pol/invariants.py`, `pol/pgcore.py`)

Pure `apply(state, event) -> (state, outcome)`. Events: propose, remark (plain text, creates a pending change), confirm, decide
(accept, reject, amend, resolve), supersede, retract, revoke, grant. Contested is derived state.
- Authority is a map in the state (data), changed only by logged grant and revoke events. Revoked principals do nothing and their open
  pending changes are invalidated.
- Decisions need a human on an owner channel (`owner_ui`, `owner_cli`) or an agent on `agent_api` that the authority map explicitly lists;
  an agent never accepts or amends a proposal authored by an agent (own or peer). A remark is never a decision: a human confirms it
  on an owner channel different from the remark's.
- Every decision is bound to the head version. A decision bound to another version is `stale`, except the one-step sibling window:
  bound to the version the head moved from, by a different principal, with a different effect than the decision that moved it,
  the requirement becomes `contested` (version +1, value withheld, both candidates kept; same effect is `concurrent_agree`).
  A third sibling joins the candidates. `resolve` against the contested head ends it.
- Idempotency: one key one effect; same payload `duplicate`, other payload `idem_conflict`.
- `current_table`, `manifest_hash`, `explain` derive from state/log only. Postgres: per-requirement `FOR UPDATE`, authority `FOR SHARE`
  (revoke and grant `FOR UPDATE` on authority and every requirement), idem row read after the locks, unique idem index as backstop.

### Composition with the write path
`feat/writepath-rescue` `wp.log.decide(item, based_on_version, action, payload, source_event_id, idem_key)` is the same shape:
core event -> (item=req, based_on_version=based_on, action=verb, idem_key=idem). It compares-and-sets the head and raises
`StaleDecision`; it does not have the sibling window, so two concurrent decisions on one version end as one winner and one
refusal there. To get a contested state on that store, the sibling rule has to move into its `decision_heads` transaction.
That integration is NOT built or tested.

### Alternatives rejected
- Refuse the second concurrent decision as stale only: loses the second principal's intent silently (what the user asked not to have).
- Contest every stale decision: turns a late click into a conflict; the window is one head step only.
- Credential attributes win over identity (Ockam order reversed): the attack the key-clash rule exists for.
- Label as str subclass: Python string operations drop attributes, so labels live on `Block`.
