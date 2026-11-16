# Decision core: normative rules (rescue, GoodQA #16096)

This file is the specification. `pol/_core.py` implements it and `exp/ref_reducer.py` is an independent implementation written from
this file alone. Where they disagree, this file decides which one is wrong.

## 1. Genesis (per tenant)

```
config = {
  "tenant": str,
  "principals": {principal_id: kind},          # kind in {"human", "agent", "analyst"}; anything not "human" is NON-HUMAN
  "authority": {req: [principal_id, ...]},      # may make head-moving decisions on req
  "proposers": {req: [principal_id, ...]},      # may propose and remark on req (authority implies proposer)
  "admins": [principal_id, ...],                # may grant and revoke
  "req_labels": {req: label_dict},              # optional per req; default {"tenants":[tenant], every other dimension empty/None/0/false}
}
```
Requirements are exactly the keys of `authority` (a req missing from `proposers` has no extra proposers). Initial per-requirement head:
`version 0, status "unset", value_ref None, value_label None, value_event None, value_by None, value_version 0, last None, contested None`.

Constants: `MAX_CANDIDATES = 8`, `MAX_OPEN_PER_PRINCIPAL = 16` (open proposals plus open pending changes of one principal on one
requirement), `MAX_OPEN_PER_REQ = 256` (all open proposals plus open pending changes on one requirement).
Channels: owner channels `owner_ui`, `owner_cli`; agent channel `agent_api`.

## 2. Event format (as logged; stamped by the service from the authenticated session)

Every event has `type`, `id`, `idem`, `tenant`, `principal`, `channel`. `id` is `principal + "/" + client_id` (the service builds it;
the reducer only needs it to be a non-empty string). Required fields per type (all must be present):

| type | required besides the six common fields |
|---|---|
| propose | req, value_ref, label, based_on |
| remark | req, value_ref, label, based_on |
| confirm | req, pending, based_on |
| decide | req, verb, based_on (verb accept/reject: + proposal, proposal_hash; amend: + proposal, proposal_hash, value_ref, label; resolve: + either choose, or value_ref and label) |
| supersede | req, value_ref, label, based_on |
| retract | req, based_on |
| revoke | target |
| grant | req, target, role (role in {"decide", "propose"}) |

Types: `id`, `idem`, `tenant`, `principal`, `channel`, `req`, `value_ref`, `proposal`, `proposal_hash`, `pending`, `choose`, `target`,
`verb`, `role` are non-empty str; `based_on` is int (not bool) >= 0; `label` is a label dict with exactly the ten keys
(tenants, acl, regions, sensitivity, models, banned, local_only, restricted, holds, subjects) and valid values. Anything else in
those fields is `bad_event`.

## 3. Outcome of an event (first matching rule wins; a REFUSED code changes nothing at all)

1. Shape violation of section 2 (unknown type, missing field, wrong type, invalid label) -> `bad_event`.
2. `tenant` differs from the state's tenant -> `tenant_mismatch`.
3. Idempotency: key = (principal, idem). If the key was recorded by an earlier APPLIED event: identical event (same canonical JSON)
   -> `duplicate` (the original code is also reported); otherwise -> `idem_conflict`. Refused events never record their key.
4. Principal not in `principals` -> `unauthorized`. Principal revoked -> `principal_revoked`.
5. `revoke`: principal must be an admin (`unauthorized`); target must be a known principal (`bad_event`). Applied: target joins
   `revoked`; every OPEN proposal and OPEN pending change authored by target on any requirement becomes `invalidated` (and stops
   counting toward the open caps). Contested candidates are not touched. Code `revoked`.
6. `grant`: principal must be an admin (`unauthorized`); req must exist, target must be known and not revoked (`bad_event`).
   Applied: role `decide` adds target to authority[req]; role `propose` adds target to proposers[req]. Code `granted`.
7. req not a requirement -> `unknown_req`.
8. `id` already used by an applied event on this req -> `duplicate_id`.
9. `propose` / `remark`: principal must be in authority[req] or proposers[req] (`unauthorized`); the principal's open count on req
   must be < MAX_OPEN_PER_PRINCIPAL and the req's open count < MAX_OPEN_PER_REQ (`proposal_cap`). Applied: a proposal (propose) or
   a pending change (remark, which also records `channel`) with status `open`, author, author kind, value_ref, label, base = based_on.
   Codes `proposed` / `pending_created`. The head does not change.
10. Head-moving types (confirm, decide, supersede, retract), in order:
    a. principal not in authority[req] -> `unauthorized`;
    b. human on a channel that is not an owner channel, or non-human on a channel that is not `agent_api` -> `channel_mismatch`;
    c. non-human and (type is supersede, retract, or decide with verb amend or resolve) -> `agent_not_permitted`;
    d. confirm: non-human -> `agent_cannot_confirm`; pending unknown -> `unknown_pending`; pending not open -> `pending_closed`;
       pending's channel equals this event's channel -> `same_channel_confirmation`;
    e. decide: verb not in {accept, reject, amend, resolve} -> `bad_event`; for accept, reject, amend: proposal unknown on this req ->
       `unknown_proposal`; accept by a non-human of a proposal whose author is non-human -> `self_approval`; proposal_hash must equal
       sha256 of the canonical JSON of `[proposal id, proposal value_ref, proposal author]` (`proposal_mismatch`).
11. NON-HUMAN REJECT (changes no value, never moves the head): requires based_on == head version (`stale`) and the proposal open
    (`proposal_closed`). Applied: the proposal becomes `rejected` with closed_by = this event. Version, value, last and contested are
    unchanged. Code `decided_reject`.
12. Requirement contested:
    - decide resolve: based_on != version -> `stale`; otherwise RESOLVE (section 4).
    - based_on == contested base: the candidate's effect is computed (section 5); if the principal is non-human, or any existing
      candidate's author is non-human -> `stale`; candidates already MAX_CANDIDATES -> `candidates_full`; the adopted content is not
      open and was not closed by one of the existing candidates' events -> `proposal_closed`. Applied: appended as a candidate
      (event, by, effect, verb, computed label). Code `contested_joined`. Version unchanged.
    - based_on == version -> `contested_needs_resolve`; otherwise `stale`.
13. Not contested and verb resolve: based_on != version -> `stale`, else `not_contested`.
14. based_on != version (stale binding): it is a SIBLING when all hold: last exists; based_on == last.based_on; version == based_on + 1;
    last.by != principal; last.verb != "resolve"; principal is human and last.by is human. Not a sibling -> `stale`. A sibling whose
    adopted content is not open and was not closed by last.event -> `proposal_closed`. Effect equal to last.effect ->
    `concurrent_agree` (nothing changes, but the idempotency key and the id ARE recorded). Otherwise CONTEST: contested = {base:
    based_on, prior: last.prev, candidates: [last's (event, by, effect, verb, label), this event's]}; status `contested`; value_ref
    None; version + 1; last None. Code `contested_opened`.
15. Normal application (based_on == version):
    - decide accept: proposal must be open (`proposal_closed`); a NON-HUMAN accept whose proposal value_ref equals the head's current
      value_ref while status is `current` is refused `no_value_change` (N3: a non-human event that changes no value never moves the
      head; amendment 3, after B1 found an agent re-accepting a value a resolve had already installed); proposal becomes `accepted`
      (closed_by this event); value set. `decided_accept`.
    - decide reject (human): proposal must be open (`proposal_closed`); proposal becomes `rejected`; value unchanged.
      `decided_reject`.
    - decide amend: proposal must be open (`proposal_closed`); proposal becomes `accepted`; value set to the event's value_ref.
      `decided_amend`.
    - supersede: value set to the event's value_ref. `superseded`.
    - retract: status unset or retracted -> `nothing_to_retract`; otherwise the current value is pushed to the chain (if value_ref is
      not None), status becomes `retracted`, value_ref None, value_label None, value_event = this event, value_by = principal,
      value_version = version + 1, contested None. `retracted`.
    - confirm: the pending change becomes `confirmed`; value set to its value_ref. `confirmed_applied`.
    For every applied code in this rule: version + 1; last = {event, by, based_on, effect, verb (the decide verb, else the type),
    prev: the head's (value_ref, status, value_event, value_by, value_version, value_label) BEFORE this event, label: computed}.
    "value set" means: push the current value (if not None) to the chain; value_ref, status `current`, value_event = this event,
    value_by = principal, value_label = computed label, value_version = version + 1; contested None.

## 4. Resolve

The head returns to contested.prior, then: `choose` names a candidate event (`unknown_candidate` if none) whose effect is applied;
otherwise `value_ref` with `label` is a set effect. A set effect sets the value (as in 15, with value_event = the resolve). A retract
effect retracts exactly as in 15 (value_event = the resolve; no nothing_to_retract check). A reject effect marks that proposal `rejected`. version + 1; last = {event, by, based_on, effect, verb "resolve",
prev: prior}. Code `resolved`.

Clarified 2026-11-16 after the reference author's ambiguity list (retract fields). Other ambiguities were resolved as the reference
author chose them (listed in exp/ref_reducer.py) and the production reducer follows the same reading.

## 5. Effects and computed labels

Effect of: accept -> ["set", proposal.value_ref]; amend -> ["set", event.value_ref]; reject -> ["reject", proposal id];
supersede -> ["set", event.value_ref]; retract -> ["retract"]; confirm -> ["set", pending.value_ref].
Computed label = join of the requirement label and the content label: accept -> proposal label; amend -> proposal label and event label;
confirm -> pending label; supersede or resolve-by-value -> event label; reject and retract carry none (value_label None after retract).
"Adopted content is open": for accept, reject, amend the proposal; for confirm the pending change; other types always pass.

## 6. Bookkeeping of every APPLIED event (codes not in the refused list)

The idempotency key is recorded with (fingerprint, code); the event id is recorded as used on its req (types with a req); the event is
appended to the log. REFUSED codes: bad_event, tenant_mismatch, unauthorized, principal_revoked, unknown_req, duplicate_id,
proposal_cap, channel_mismatch, agent_not_permitted, agent_cannot_confirm, unknown_pending, pending_closed, same_channel_confirmation,
unknown_proposal, self_approval, proposal_mismatch, stale, proposal_closed, candidates_full, contested_needs_resolve, not_contested,
nothing_to_retract, unknown_candidate, idem_conflict, no_value_change, plus `duplicate` (not applied again).
Every refused event is written to the audit log with its code (not to the event log).

## 7. Liveness bound (what N3 needs)

A non-human can move the head only by accepting an OPEN HUMAN-authored proposal whose value differs from the current one (rules 10c,
10e, 11, 15). So between two reads of the head
by a human, non-humans can move it at most once per open human proposal, and new human proposals come only from humans. A human who
re-reads the head and resubmits is therefore applied within (open human-authored proposals on that req) + 1 attempts, whatever
non-humans do.

## 8. Canonical forms and the comparable view (what B2 compares, event by event)

Canonical JSON: `json.dumps(o, sort_keys=True, separators=(",", ":"))`; sha256 over its UTF-8 bytes, hex. The event fingerprint is the
sha256 of the whole event. Label join: tenants union; acl union of clauses then drop any clause that is a strict superset of another;
regions and models intersection (None = any); sensitivity max; banned, holds, subjects union; local_only and restricted OR. A label
dict is written with every collection sorted (acl: each clause sorted, clauses sorted).

The comparable view after every event:
```
{"revoked": sorted list,
 "authority": {req: sorted list}, "proposers": {req: sorted list},
 "reqs": {req: {"version", "status", "value_ref", "value_event", "value_by", "value_version", "value_label",
                "contested": None or {"base": int, "candidates": [[event, by, effect, verb], ...] in arrival order}}},
 "proposals": {req: {proposal id: status}}, "pending": {req: {pending id: status}}}
```
