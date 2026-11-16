# Policy decision point and decision core (GoodQA #16096): design after the rescue rebuild

Code is in `pol/`, experiments in `exp/`, independent verifiers in `verify/`, pre-registered thresholds in `THRESHOLDS.md`, core rules in
`CORE-RULES.md`, and the write-path composition in `COMPOSITION.md`. No concrete policy ships. The format, the validator (`parse_policy`),
the examples and default deny are what ships.

## Trust boundary (N1, H1, M1)
- `SessionAuthority.issue` (the authentication path) creates a `Session`. A session is valid only if it is the very object the authority
  issued, by identity. A hand-built Session, a copy of one, a Principal or a string is refused.
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
  per principal and 256 per requirement. Cost per event is constant: a deterministic count, plus timing to 20,000 proposals in memory
  and in Postgres.
- Head moves need a human on an owner channel. A non-human may only reject, which never moves the head, or accept an open
  human-authored proposal whose value differs from the current one (`no_value_change`, amendment 3).
- Liveness: a human who reads the head is applied within (open human-authored proposals at the read) + 1 attempts.
- Concurrency: the one-step sibling window gives `contested`, bounded at 8 human candidates, ended by `resolve`.
- Event ids are `<principal>/<client_id>`, so no id can be squatted. Idempotency is per principal and returns the ORIGINAL outcome code
  on retry.
- Values are referenced, not held. The vault keeps the text under a keyed reference, tagged with the label's subjects.
  `erase_subject` deletes every value ABOUT the subject, whoever wrote it. A value under a recorded legal hold is queued instead.
- Reads are viewer-scoped (`explain(session, req)`, `table(session)`): only values, proposals and candidates the viewer may read.
  `hidden_candidates` exists only when the policy opts in (`conflict_notice: existence_only`). The default is NONE.
- Every refusal is written to the audit log with its code. The audit log grows by one row per checked request. Bounding it (rotation,
  retention) is outside this module and not done.
- Value encryption is an interface only (`_ValueCipher`): in the composed system the write path's KeyStore owns the per-subject keys.

## Postgres (`pol/pgcore.py`)
- Roles:
  - `dc_owner` (NOLOGIN) owns everything.
  - `dc_app` logs in with SCRAM. The password is read from a 0600 file, and only its verifier, computed client-side, is sent.
  - Passwordless connections are refused on TCP and on the socket.
- `dc.events` is append-only by grants plus triggers: UPDATE, DELETE and TRUNCATE are refused for the app and for the owner.
  `DISABLE TRIGGER` needs ownership.
- The hash chain is computed in a trigger under a per-tenant chain-head lock and verified in the database (`dc.verify_chain`). A
  superuser rewrite with triggers bypassed is detected.
- Authority tables are SELECT-only for the app. They change through `dc.apply_authority(seq)`, a SECURITY DEFINER function that applies
  a logged revoke or grant once.
- Isolation is READ COMMITTED, set explicitly. Lock order:
  1. tenant row (FOR SHARE, or FOR UPDATE for revoke and grant);
  2. advisory (tenant, principal);
  3. head FOR UPDATE;
  4. event insert, which takes the chain head.
- Revoke serialises the tenant, which is a scaling limit.

## Not proven / limits (see also each result's `not_proven`)
- Python cannot stop in-process code from reaching private names. The surface test enforces what the package exports and imports.
- The skeleton table is partial. Unicode confusables are refused by the ASCII rule, but ASCII look-alikes outside the table are not.
- Audit growth is unbounded in this module.
- The pin is checked after the call. Local endpoints are not attested.
- The composed system (COMPOSITION.md) is not built.
