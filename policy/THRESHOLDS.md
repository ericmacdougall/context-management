# Pre-registered thresholds for the policy decision point and the decision core (GoodQA #16096)

Committed before any proof run. Proof rules: gotchas/PROOF-RULES.md. Every experiment prints a result file stamped with the
code commit (the run refuses a dirty tree), the commit of this file, parameters, time, host and an environment fingerprint.
A run that errors scores as FAIL. No partial reruns are combined into one result: a rerun is a new result file.
Controls are broken variants run by the same harness; each must reach its sensitivity floor AND fail for the stated reason
(the violated check is named in the result), or the experiment is FAIL.
No model call of any kind is made: all model, embedding, inbox and export endpoints are recording stubs.

## E1 label lattice (exp/e1_lattice.py)
- N = 20000 random derivations (1 to 5 inputs, depth 1 to 3), seeds 1..5 (4000 per seed, reported per seed).
- Structural check: derived label is at least as restrictive (leq) as every input, in every dimension.
- Semantic check (independent of leq): for each derivation 10 random (principal, action, destination) probes; whenever the
  derived label is allowed by decide(), every input label must be allowed too.
- Lattice laws on 5000 triples: join commutative, associative, idempotent.
- PASS: 0 violations on every check; witnesses: derivations >= 20000, probes >= 200000, allowed probes >= 5 percent of probes
  and denied probes >= 5 percent.
- Controls: one naive merge per dimension (tenant, acl, region, sensitivity, allowed models, banned models, local flag,
  restricted flag), eight in all. Floor for each: structural violation in >= 10 percent of derivations with >= 2 inputs and
  semantic counterexamples in >= 5 percent of derivations. The reason recorded must name the dropped dimension.

## E2 egress canary (exp/e2_egress.py)
- S = 2000 random scenarios (seeds 1..5, 400 each), 4 to 10 operations each, over the outbound paths enumerated from the
  registry of stub channels: model port, embedding call, inbox delivery, export, plus prompt injection (a context-building
  step whose product is later sent). Each scenario plants >= 12 records with unique marker strings in the classes
  eu_only, local_only, model_banned, restricted_channel, other_tenant, and plain.
- Oracle: written separately from decide(), from per-marker class and destination attributes. A violation is a marker
  appearing in any recorded payload at a destination the oracle forbids, directly or through derived (summarised) records.
- PASS: 0 violations. Witnesses: forbidden-opportunity attempts (a driver attempt the oracle forbids) >= 1000 per class;
  permitted marker deliveries >= 1000 per class (so a deny-everything policy cannot pass); every path in the registry
  records >= 1 payload in the positive run; the set of registered stubs equals the set of actions the decision point
  evaluates (minus append and read, which are store paths, covered in E5).
- Controls (each over the same 2000 scenarios): (a) enforcement disabled at the model port, (b) at the embedding call,
  (c) at inbox delivery, (d) at export, (e) derived records labelled with the first input only (derivation join dropped),
  (f) a canary-blind id-pattern guard instead of the decision point. Floor for each: markers escape in >= 10 percent of
  scenarios and the canary scanner flags each (detected >= 1). Disabling only injection is reported separately as a
  defence-in-depth measurement and is not a control.

## E3 key clash and error-deny (exp/e3_clash.py)
- N = 6000 cases: >= 2000 planted identity or tenant overrides carried in credential or derived attributes (keys tenant,
  id, groups, subject.identifier, case and whitespace and NFKC variants, bytes keys, non-string keys, nested values),
  >= 2000 evaluation faults (rule returns a string, 1, None, an object with a raising or truthy __bool__, NaN; rule raises;
  malformed label; malformed principal), and >= 1000 legitimate requests with benign credential attributes.
- PASS: 0 allows where an override was planted or the result was non-boolean or errored; legitimate requests allowed in
  >= 99 percent (so deny-all cannot pass; any miss is listed).
- Control: a naive evaluator (dict merge with credential last, truthiness test, exceptions swallowed to continue). Floor:
  allows >= 10 percent of the planted override cases and >= 10 percent of the fault cases.

## E4 tighten after enqueue (exp/e4_tighten.py)
- R = 200 runs per path in {model queue, inbox, export} and per tightening in {model banned, region removed, recipient
  revoked, tenant killed, group removed from recipient} where the tightening applies to the path. Items are enqueued under
  policy version 1 (allowed), the policy is tightened to version 2, then delivery is attempted.
- PASS: delivered after tightening = 0 of all runs; refusal logged with policy version 2 and the enqueue decision with
  version 1 in 100 percent; positive control without tightening delivers 100 percent (no over-denial).
- Control: enqueue-time-only check delivers >= 90 percent of tightened runs.

## E5 parallel related threads (exp/e5_threads.py)
- S = 1000 random worlds (seeds 1..5): 2 or 3 threads with different access groups (some overlapping), items with typed
  edges (conflicts_with, depends_on, references) across threads, shared decisions, analyst findings, 2 tenants.
- Conflict notice contract (decision, see DESIGN.md): a viewer who can read item L but not item X that conflicts with it
  receives exactly: kind "conflict", the id and version of L, state "contested_by_restricted_item", and nothing else.
  It does not reveal the id, thread, group, content, value, decider, count, timing or version of X.
- PASS: 0 markers from unreadable records anywhere in any principal's view (records, injection blocks, inbox deliveries,
  analyst findings, cross-reference expansions, notices); 100 percent of conflicts touching a visible item produce exactly
  one notice for each viewer who cannot read the other side; non-interference: mutating the hidden item's content, id,
  group name, decider leaves the viewer's whole serialised view byte-identical in 100 percent of worlds. Witnesses: worlds
  with >= 1 cross-thread conflict >= 90 percent; findings joining two threads >= 1 per world on average; notices >= 1000.
- Controls: (a) derived records labelled with the first input only, (b) a notice carrying the other item's content and
  id, (c) cross-reference expansion without a per-reference decision. Floor: >= 10 percent of worlds leak for each.

## B1 decision core model check (exp/b1_modelcheck.py)
- Scenario: principals u1, u2 (human), a1 (agent); requirements r1, r2, r3; authority map r1 {u1,u2}, r2 {u1}, r3 {u1,a1};
  an event pool of proposals, a plain-text remark, a typed confirmation on the owner channel, an agent confirmation, a
  same-channel confirmation, accept, reject, amend, supersede, retract and resolve decisions with fixed binding versions
  and idempotency keys; any pool event may be delivered at any point, including redelivery. Pool and depth L are fixed in
  the parameter block of the run script and recorded; L = 6 events minimum.
- Exhaustive breadth-first exploration of every reachable state; invariants checked in every state and on every transition:
  I1 authority, I2 no self approval, I3 a remark or proposal never changes the current value, I4 decisions bound to the
  head version (or the contested sibling window), I5 stale refused with state unchanged, I6 redelivery is a no-op,
  I7 no two accepted decisions on the same base version (conflict surfaces as contested), I8 versions monotonic and equal
  to the count of state changes, I9 rebuild of the log prefix equals the state.
- PASS: 0 violations; witnesses: states >= 1000, transitions >= 20000, every outcome code in the reducer's code list reached
  at least once (coverage), contested state reached and resolved.
- Controls, each removed in turn: self-approval check, staleness check, idempotency key; plus authority check, remark-is-
  decision, last-writer-wins. Each must yield >= 1 counterexample whose violated invariant is the expected one
  (I2, I4/I5, I6, I1, I3, I7 respectively), with the shortest trace printed.

## B2 rebuild equals live (exp/b2_rebuild.py)
- 3000 random logs (lengths 5 to 80, seeds 1..5) over the same principals and requirements plus revocations, duplicates,
  stale and unauthorised events. Live path: incremental apply with JSON snapshot and restore every k events (k random);
  rebuild: fold from empty. PASS: state equal, manifest hash equal, recorded outcomes equal in 100 percent; witnesses:
  contested in >= 10 percent of logs, refusals of each reason, snapshots restored >= 3000.
- Control: live engine with an out-of-band mutable authority map (changes not in the log) and a variant that drops refused
  events from its log. Floor: mismatch in >= 10 percent of logs each.

## B3 Postgres concurrency (exp/b3_pg.py, container emlab-pg-pol-16096 on t2-dev)
- R = 50 rounds per scenario, fresh requirement per round, real overlapping transactions on separate connections:
  S1 two authorised principals decide the same base version differently: both candidates surface as contested (never one
  silent winner); S2 stale token: decision bound to an old version while the head has moved twice: refused, state unchanged;
  S3 zombie: principal revoked after preparing a decision, revoke commits, then the decision is submitted: refused;
  S4 two connections submit the same idempotency key concurrently: exactly one event row, both callers get the same outcome;
  S5 resolve race: two resolves of a contested head: one wins, the other is refused as stale.
- Overlap witness per round for S1, S3, S5: a third connection observes the second transaction blocked on a lock
  (pg_stat_activity wait_event_type = Lock) while the first holds its transaction open, before the first commits.
- PASS: 50 of 50 per scenario; overlap witnesses 50 of 50 for S1, S3, S5; rebuild from the event table equals the live head
  rows (manifest hash) in 100 percent of rounds.
- Controls: (a) blind last-writer-wins update with no lock and no version check: silent loss of one decision in >= 45 of 50
  rounds of S1; (b) read-then-write without lock: lost update in >= 45 of 50 rounds under forced interleaving.

## Amendment 1 (written after DEV dry runs, before any stamped run; the DEV runs used seeds 1..5 and tuned generators)
Disclosed deviations and additions. Stamped runs use FRESH seeds (E1, E2, E5, B2: 11..15; E3: 77; E6: 606).
- E1: 40 probes per derivation (was 10; the semantic floor was marginal at 10), so probes >= 800000; the semantic floor is measured over
  derivations with >= 2 inputs; controls run on all five seeds and the WORST seed must meet the floors. Dimensions are now ten:
  holds and subjects added (their naive merges: intersection of holds; subjects of the first input only). Subject semantic check: every
  subject mentioned by any input is mentioned by the derived record.
- E2: "permitted deliveries >= 1000 per class" excludes other_tenant (it has no permitted destination by design). Control (f) now scans text for
  id-shaped strings (it must block >= 1 planted id-shaped string and still let markers escape). The oracle does not see the restricted authorisation
  (only the destination programme): listed as not proven.
- E4: group_removed is a directory change, so its refusal is logged with policy version 1; the other combinations log version 2. Runs vary
  sensitivity, ACL, recipient groups and clearance, item count; the structural distinct cases remain 12 combinations x 200 randomised runs.
- B1: minimum depth L = 6 applies PER REQUIREMENT scenario (r1 depth 6, r2 and r3 depth 8); the joint three-requirement pool is explored to depth 4
  (controls: depth 3). Decomposition is argued, not proven (see DESIGN.md). Added agent rule (an agent only accepts human-authored proposals or
  rejects; it never supersedes, retracts, amends or resolves), idempotency keys scoped per principal, invariant I2 generalised to I2_agent_head_move.
- B2: adds a second agent and runs every B1 invariant over every step of the random logs.
- B3: control (a) is now the engine with the last-writer-wins flag under the S1 overlap (log bases [0,0], no contested state); the blind UPDATE
  is a further control. Rebuild is compared once at the end of the scenario set, per requirement (each round's requirement) and as a manifest.
- New E6 (exp/e6_policy_format.py): empty, absent and 3000 malformed policy documents deny every probe (zero allows over >= 15000 probes) and the
  validator raises on 3000 of 3000; a lenient loader (control) allows >= 10 percent; a valid document allows > 5 percent (not deny-all); legal hold
  beats erasure only with a recorded hold event; hold and subjects labels are inherited by derived records.
- Not built: per-item result artifacts with an independent recomputation script (rule 5), viewer-scoped explain for contested candidates (E5b),
  a password for the Postgres container (loopback trust auth on the host).

## Amendment 2 (2026-11-16, after the independent review of 515e594; committed alone, before the fix-round runs)
The first stamped runs (515e594 and earlier) are CLAIMED (tuned then confirmed), superseded by the runs described here. Seeds for the new stamped runs
are fresh again: E1, E2, E5, B2 use 21..25; E3 uses 88; E6 uses 707. Thresholds below are additions or disclosed corrections.
- E2: the inventory condition compares the registered outbound stubs with ACTIONS minus append, read, inject and erase (store and context-building paths).
  This exclusion was previously only in code and caused the stamped FAIL at 47c39df.
- E3: a new arm fuzzes the REQUEST context (3000 cases): forged region, forged clock, forged recipient object, every extra top-level key, misspelt
  narrowing keys (case, separators, zero-width, trailing space, missing separator). PASS: 0 allows. Control: the old evaluator's semantics allow >= 10 percent.
- E4: three more combinations on the inbox path (source record narrowed after enqueue, source erased, hold label added), 15 combinations x 200 randomised
  runs; the policy is unchanged for these; refusal is logged at the unchanged policy version.
- E5: the default conflict notice is NONE. Arms: notice none (0 notices, 0 leaks, 0 interference) and existence_only (as before, opt-in); conflict edges to
  another tenant's item must never produce a notice (3 of 10 worlds carry one).
- B1: pool adds symbolic bindings ("HEAD" = the requirement's version at delivery), a third principal kind (analyst) and events for id reuse, wrong content
  hash, other tenant and missing label; invariants I12 (a value never comes from a closed proposal), I13 (event ids unique), I14 (a contest is never held open or
  grown by a non-human, candidates bounded); I2 covers every non-human kind; liveness L1: in every contested state, after any single non-human pool event, an
  authorised human resolve succeeds. Controls: ten (adds: agents may contest, duplicate ids allowed, sibling ignores proposal status). The candidate cap is
  covered by tests/test_review_cap.py (nine humans), not by the pool.
- B2: hostile events (wrong proposal hash, other tenant, unlabelled install, reused id), a third kind; invariants checked at every step (the rebuild comparison
  still uses the same reducer: B2 shows determinism and serialisability, the invariants carry correctness).
- B3: S4 asserts the first caller applies and the twin returns duplicate (same payload) or idem_conflict (other payload), exactly one event row. S4b (new):
  one principal reuses an idempotency key on two different requirements concurrently; the twin must block on the unique index, hit UniqueViolation, retry,
  and return idem_conflict; at least one backstop retry per round. S6 (new): another tenant's admin cannot revoke or grant in this tenant. Isolation level is
  set explicitly (READ COMMITTED); serialisation failures and deadlocks are retried. R = 50 per scenario; S4b overlap witness: the twin seen blocked while the
  first transaction holds its uncommitted insert.
- Rule 5: every experiment writes a per-item artifact (results/*.items.*.jsonl.gz, hash recorded in the result); exp/verify.py recomputes each verdict from the
  rows with its own code (B1: replays every control counterexample through the reducer).

## Amendment 3 (2026-11-16, written after DEV runs at the rescue rebuild; committed ALONE, before any stamped run of this round)

Why: the rescue rebuilt the decision point and the decision core (commits 402df5e and 0e2a8d6, plus the experiment port committed just
before this amendment). Every earlier stamped result (515e594, 77ea895 and before) is superseded and stays CLAIMED.

Rules for this round (they override earlier text where they conflict):
- Seeds. Development used ONLY the string seeds "dev-<exp>-1" to "dev-<exp>-5" (exp/seeds.py DEV). The stamped seeds are fresh integers,
  never used before in any run: E1, E2, E4, E5 and B2 use 31..35; E3 uses 99; E6 uses 808; B1 records 31 (it is exhaustive); B3 uses 31.
- Stamped runs set POL_SEEDS=stamped on a clean committed tree, synced by sync.sh. exp/seeds.py refuses stamped seeds on a dirty tree.
  Each stamped configuration runs ONCE, and no development run of the stamped configuration follows this amendment. A stamped FAIL is
  kept and reported. It is never rerun to get a PASS.
- Controls are broken IMPLEMENTATIONS: source variants of a production module, built by the harness (exp/patches.py variant/control,
  and the multi-fragment helpers in the experiment files, each fragment asserted to apply exactly once). No experiment flag, switch or
  hand-written formula exists in production.
- Per-item artifacts hold RAW observations. `python -m verify` recomputes every verdict with verify/<x>.py. These are stdlib-only
  modules, and an AST check refuses any import of pol, exp or tests. The verifier compares its verdict with the experiment's. A
  disagreement, a missing artifact or a verifier error is a FAIL. A result is PASS only when both say PASS.
- Core rule change made in this amendment (CORE-RULES rule 15, pol/_core.py, and the reference reducer, which the rescue owner edited,
  not the reference author): a NON-HUMAN accept whose value equals the current value is refused `no_value_change`.
  - B1 found an agent re-accepting a value that a resolve had already installed. The head moved and attribution changed without any
    value change.
  - B1 invariant I16 is the brief's wording: a non-human event that changes no value never moves the head.
- Liveness bound (B1 L2, N3): a human who reads version v and retries after each `stale` is applied within (open human-authored
  proposals on the requirement at the read) + 1 attempts, so at most MAX_OPEN_PER_REQ + 1 = 257. The control
  `n3_agent_reject_moves_head` must violate it.
- Unchanged: no model call of any kind is made. A run that errors scores FAIL. Partial reruns are never combined.

The per-experiment text below was proposed by the experiment ports after their DEV runs. It is adopted as written, except where the
rules above override it. Its "DEV result" paragraphs are development observations on dev seeds, not proof.

Applies to all three experiments:
- They were rebuilt for the new API at 0e2a8d6: sessions, the private PDP `pol._pdp.evaluate`, `Store`, `DecisionService`.
- Development runs used only the string seeds dev-e1-1..dev-e1-5, dev-e3-1..dev-e3-5 and dev-e6-1..dev-e6-5 (exp/seeds.py DEV).
- Stamped runs use exp/seeds.py STAMPED (E1: 31..35; E3: 99; E6: 808), once, with POL_SEEDS=stamped on a clean tree.
- Every control is a broken implementation, made as a source variant of a production module. It is never a hand-written formula.
- Per-item rows hold raw observations only: labels, probe bits, what was sent and what came back.
- verify/e1.py, verify/e3.py and verify/e6.py recompute every verdict from the rows using only the stdlib. They import nothing from pol or exp.
- A result is PASS only when the experiment's own summary and the verifier both say PASS.

### E1 (exp/e1_lattice.py)
- **Unchanged.** 20000 derivations (4000 per seed), 40 probes per derivation (>= 800000 probes), 5000 law triples.
  - Enforced arm: 0 structural and 0 semantic violations, 0 law failures.
  - Allowed probes are at least 5 percent and at most 95 percent.
  - Control floors, worst seed: structural >= 10 percent and semantic >= 5 percent of derivations with >= 2 inputs, and the violated dimension is exactly the dropped one.
- **Controls.** Ten one-patch variants of `pol/labels.py join2`, one dimension each. The fragments are recorded in the result params.
  - tenants keeps the first input only.
  - acl merges all groups into one OR clause.
  - regions and models use union, where any None means any.
  - sensitivity uses min.
  - banned and holds use intersection.
  - local_only and restricted use AND.
  - subjects keeps the first input only.
- **Semantic check.** A probe is a violation when the derived label is allowed and some input is not. Probes go through `pol._pdp.evaluate` with a fixed runtime clock. The subject-coverage check (amendment 1) counts as semantic.
- **Rows.**
  - Input and derived labels, as lists in DIMENSIONS order.
  - `d`: one character per probe, 1 or 0, for the derived decision.
  - `i`: 1 or 0 for "every input allowed", or `-` when the derived probe was denied. Inputs are evaluated only when the derived label was allowed.
- **New arm: round trip.** 1000 per seed.
  - `Label.from_dict(json(to_dict()))` equals the label, with the dict and digest unchanged.
  - Leaves are appended through `Store.append`. Then `Store.append_derived` is called, and the record is read back through `Store.read`.
  - The stored label must equal the join of the stored lineage. The verifier uses its own join.
  - PASS: 0 mismatches, and >= 2000 derived records stored.
- **Probe-universe change (disclosed).** The new PDP requires each destination's contracted tenants and a declared model, so the generator declares both.
  - The allowed share fell to about 9 percent of probes, against an old share of about 30 percent. That still clears the 5 percent floor.
- **Artifact size at full scale** (dev run): 9.7 MB gzipped, including ten controls, laws and round trip. No reduced recording is needed.

### E3 (exp/e3_clash.py)
- **Sizes per seed.** planted 2200, fault 2400, legit 1400, context 3000, and 10 forge rounds.
  - The stamped run has one seed (99). The dev runs used five seeds, so their counts are five times larger.
- **Credential channel changes (disclosed).**
  - The old "derived attributes" channel no longer exists. A planted override now goes either in the credential or as a top-level request key.
  - The exact expected reasons are `attr_unrecognised_key`, `forged_request` or `attr_source_type`. Any other deny reason for a planted row is a failure ("refused by luck").
- **Legit cases changed (disclosed).** Benign credentials now use only the allowed narrowing keys:
  - `{}`, None, `cap_clearance`, `cap_actions` and `cap_restricted`, alone or combined.
  - Unknown credential keys such as email or roles now deny by design (H2).
- **Escaped exceptions.** An exception that escapes `evaluate` is recorded raw (`raised`), and any such row in the enforced arm fails.
- **Control `ctl_naive_pdp`.** A multi-fragment variant of `pol/_pdp.py`. Each fragment must occur exactly once. The fragments are in the result params:
  - unknown credential keys are ignored;
  - unknown request keys are accepted;
  - credential and top-level request keys are merged over the principal's attributes;
  - the request's clock decides expiry;
  - the request's recipient object is trusted;
  - truthiness is used for rule results, and a raising rule passes;
  - label and evaluation errors are swallowed as allow.
  - Floors are unchanged: >= 10 percent allows on planted, on fault and on context.
- **New arm: forge, derived from the public surface.**
  - The `tests/surface.py` module and signature walk lists every public method of `pol` whose first parameter is `session`. That is 16 request-handler entry points at 0e2a8d6.
  - Every listed entry point must have a driver. An uncovered entry point fails the run.
  - **Session forgeries**, applied to each entry point:
    - a hand-built Session;
    - the attacker's token under the victim's id;
    - a copy of the victim's Session values in a new object;
    - a Principal object, None, a string, a dict, or a SimpleNamespace;
    - an ended victim session;
    - the attacker's own real session;
    - another tenant's real session.
  - **Request forgeries** (`Outbox.enqueue`): tenant, principal, a recipient object, now, region, credential tenant/clearance/groups, confusable, case, space and zero-width narrowing keys, viewer, trusted, and labels.
  - **Body forgeries** (`DecisionService.submit`):
    - tenant, principal, channel or id in the body;
    - value_ref instead of value;
    - a client_id namespaced to another principal, or a traversal client_id;
    - a non-admin revoke of the victim, or a self-grant;
    - an unknown key.
  - Acceptance is decided by the verifier from the observation:
    - an id, a text or payload containing the victim's record, or a queued item;
    - an applied decision code;
    - the victim's identity returned, or a session that claims the victim and verifies;
    - the victim's session ended by someone else;
    - a marker in a read, or an erasure that was not refused.
  - PASS: 0 accepted forgeries.
  - Controls, each with a floor of >= 10 percent of its rows accepted:
    - `ctl_no_session_verify` is a one-patch variant of `pol/enforce.py` with the session identity check removed (session rows on Store, Guarded* and Outbox).
    - `ctl_naive_pdp` sits behind the Enforcer (request rows).
    - `ctl_body_identity_trusted` is a three-fragment variant of `pol/decisions.py` that drops `identity_in_body`, admits identity keys in BODY_KEYS, and lets body keys win over the session stamp (body rows).
    - The single patch `exp/patches.py m1_body_identity` is masked by the BODY_KEYS check: alone it still refuses (`bad_event`). It is therefore not a usable M1 control, and is replaced here.
- **Not proven.**
  - pgcore is reached only through DecisionService. Its engine is wiring, not a request-handler entry point.
  - In-process Python can reach private names.

### E6 (exp/e6_policy_format.py)
- **Unchanged.**
  - Empty and absent documents deny every probe; malformed documents too: 3000 per seed, 5 probes each.
  - The validator raises PolicyError on all of them. Any other escaping exception is recorded and fails.
  - A valid document allows more than 5 percent.
  - The example is marked as an example.
  - Hold and subject inheritance through join is checked.
- **New mutation kinds.** Missing destination tenants, a model id outside its family, an alias cycle and a dangling alias. Unknown-key variants include case, a trailing space and an accent.
- **Control.** A one-patch variant of `pol/policy.py safe_policy`. On a parse error it keeps whatever grants it can read, else grants every action to t1 and t2.
  - Its Policy is re-expressed as the production class with the same fields, so the decision point accepts it.
  - Floor: >= 10 percent of probes allowed. The dev result was 29.7 percent.
- **New service-level erasure arm**, using World and DecisionService:
  - Four values: alice about dana under recorded hold `hold-7`, bob about dana, alice about dana, and bob about erin.
  - A non-admin `erase_subject` is refused `unauthorized`.
  - An admin erasure erases every value about dana whoever wrote it (2), queues the held one (1), and leaves the value about erin.
  - The verifier recomputes this from the raw before and after vault texts.

Common. Every arm drives the rebuilt production (pol.enforce Store, Store.context, Store.append_derived, GuardedChat, GuardedEmbedder,
GuardedExport, Outbox; pol.decisions DecisionService) with sessions issued by SessionAuthority. No experiment flag exists in production.
Controls are source variants built by the harness (exp/patches.py, exp/e245_kit.py). Each per-item artifact holds RAW observations,
and verify/e2.py, verify/e4.py, verify/e5.py recompute every verdict with their own code (stdlib only, no import from pol or exp).
Dev runs used string seeds only; the stamped seeds are the integers in exp/seeds.py.

### E2 egress canary
- Scale: 1000 scenarios per seed, 5 seeds, so 5000 per arm. Amendment 2 had 400 per seed, 2000 per arm. The minimum stays S >= 2000.
  The larger S is needed for the witness floors after the paths changed.
- Paths:
  - Model: GuardedChat (direct and after Store.context injection) and Outbox(send_model).
  - Embedding: GuardedEmbedder.
  - Inbox: Outbox(send_inbox).
  - Export: GuardedExport and Outbox(export).
  - Derived records come from Store.append_derived and from the answers GuardedChat stores.
  - The op mix adds two derive_send weights. Without them the derive control reached 6 percent on the dev smoke run, under its floor.
- Restricted content: GuardedChat has no authorisation parameter, so it always refuses restricted content. Restricted content reaches a
  model only through Outbox(send_model) with an authorisation, and an inbox recipient only when the recipient is restricted_ok. The
  permitted-delivery witness for restricted_channel is met through those paths.
- Inventory: the registered stub actions must equal ACTIONS minus append, read, inject and erase. The audit log must show every one of
  those actions plus inject being evaluated.
- New adversarial arm (forge op, about 1 in 16 ops). Each scenario tries what a caller can send:
  - extra request keys: tenant, principal, now, a recipient object;
  - a credential with a non-allow-listed key (including a Cyrillic homoglyph, a trailing space and the wrong case);
  - a recipient in another tenant, or an unknown recipient;
  - another principal's restricted authorisation;
  - a hand-built Session, a session issued by another SessionAuthority, an ended session, or a Principal passed as the session.
  These go through the inbox, model and export queues, GuardedChat, GuardedEmbedder, GuardedExport and Store.context.
  PASS needs forges >= 1000, 0 forges not refused, and 0 payloads recorded by a forge.
- Controls:
  - Decision bypassed for one action, applied to Enforcer._decide: send_model, embed, send_inbox and export.
  - Derived label set to the first input only, in Store.append_derived.
  - Id-pattern guard. It replaces the decision point and scans texts in Store._sources. It needs two fragments in one module, and it is
    the only control that does.
  - Floors are unchanged: leak in >= 10 percent of scenarios, >= 1 violation, and the guard blocks >= 1 id-shaped string.
  - The inject-only bypass stays informational.
- DEV result on t2-dev (dirty tree, dev seeds): PASS, and verify/e2.py agrees.
  - 5000 scenarios, 0 violations, 2187 forges, all refused.
  - Forbidden attempts per class: 5741, 5262, 2788, 5961 and 2182.
  - Permitted deliveries: eu_only 4396, local_only 3461, model_banned 4681, restricted_channel 4916, plain 2956.
  - Control leak rates: model 0.72, embed 0.43, inbox 0.41, export 0.46, derive 0.26, id guard 0.98 (2513 blocks). Inject-only: 0 violations.
  - Runtime 81 s. Artifact 6.3 MB gz, 511,691 rows.

### E4 tighten after enqueue
- The 15 combinations of amendment 2 are kept, with R = 200 each. Seeds rotate per run, with 3000 runs per arm.
- The source is now erased through Store.erase. The Outbox purge refuses the queued item with "source_erased" when the erasure happens,
  so no delivery decision is written. For that combination PASS needs the refusal "source_erased" plus an allowed erase row in the audit
  log after enqueue. Directory and record changes (group_removed, label_narrowed, legal_hold_recorded) are logged at policy version 1.
  Policy changes are logged at version 2, built with Policy.with_changes.
- New recipient-snapshot check. For every inbox delivery in the positive arm, the recipient object the sink received must be the very
  object the delivery decision evaluated (identities recorded). Required: 0 mismatches.
- Control: an enqueue-time-only Outbox. It is a one-fragment variant of pol/enforce.py Outbox.deliver_all with no decision at delivery.
  The floor is unchanged: >= 90 percent of tightened runs delivered.
- DEV result: PASS, and verify/e4.py agrees.
  - Fenced arm: 0 of 3000 delivered.
  - Positive arm: 3000 of 3000 delivered, 0 snapshot mismatches.
  - Control: 2800 of 3000 delivered (0.933). It cannot deliver source_erased items, because the purge still runs.
  - Runtime 3 s. Artifact 72 KB.

### E5 threads and viewer-scoped reads
- The store-item conflict notice of amendments 1 and 2 no longer exists in production (pol/threads.py was removed and the default is NONE).
  - The thread-world arm (A) measures records, injection, analyst findings through the inbox, and cross-reference expansion through
    Store.read, plus non-interference.
  - The notice contract moves to the production notice path, DecisionService (arm family B). Witness "notices >= 1000" becomes
    "hidden_candidates true >= 1000" in the existence_only arm.
- Arm family A controls:
  - derived label set to the first input only;
  - Store.read without the per-reference decision ("expansion without a decision"; it also affects plain reads).
  - The amendment-2 control "notice carries the other side" is dropped, because the notice it patched was experiment code.
    Its production successor is B_ctl_no_read_check.
- New VIEWER-SCOPED READS ARM (family B). Each world has 4 humans with random groups, clearance, restricted flag and region, and 3
  requirements whose authority sets are random. Its script has open proposals, accepts, contests, and contests followed by a resolve.
  Every human reads explain(r) for each requirement, plus table().
  - Arms: notice none (the default) and existence_only (opt-in).
  - PASS:
    - no marker of a value, proposal or candidate that the viewer cannot read (verifier's own read oracle) appears in the serialised view;
    - the hidden_candidates key is present in an explain or table entry iff the policy opted in;
    - in the opt-in arm the flag equals "something was withheld" for every entry;
    - non-interference: replaying the same events with every value the viewer cannot read changed gives a byte-identical view (SHA-256
      compared; the vault key is fixed across the two replays).
  - Controls:
    - control('h7_flag_always'): floor 10 percent of worlds break the key rule.
    - B_ctl_no_read_check: DecisionService._readable always true, a one-fragment variant of pol/decisions.py. Floor 10 percent of worlds leak.
    - control('h7_unscoped_value') is reported as informational only, because it is vacuous: see the owner report.
- DEV result: PASS, and verify/e5.py agrees.
  - Arm family A: 1000 worlds, 0 leaks, 0 interference. Controls leak in 937 (derive) and 1000 (xref) of 1000 worlds.
  - Arm family B:
    - Notice none: 0 leaks, 0 key violations, 0 interference.
    - Existence_only: 6633 flags true, 0 wrong flags, 0 leaks.
    - Control flag_always: 1000 of 1000 worlds break the key rule.
    - Control no_read_check: 1000 of 1000 worlds leak.
    - Informational h7_unscoped_value: 0 of 1000 worlds leak.
  - Runtime 148 s. Artifact 8.7 MB gz.

### Not proven (E2/E4/E5)
- Stubs only.
- The E2 oracle and the policy are built from the same destination table, and the oracle does not see authorisations.
- Markers are plain strings.
- No in-flight recall exists.
- Timing side channels are not tested, nor the withheld count that Store.context returns to its caller.
- Graph shape is tested only to one hop.

Applies to all three experiments:
- They were rebuilt for the decision core at 0e2a8d6: `pol._core.reduce(view, ev) -> (code, original_code, delta)`, `MemoryState`
  (snapshot / restore / comparable), `pol.pgcore.PgEngine`, `DecisionService` and `MemoryEngine`.
- Development runs used only the string seeds dev-b2-1..dev-b2-5 and dev-b3-1..dev-b3-5 (exp/seeds.py DEV). B1 is exhaustive and uses no seed.
- Stamped runs use exp/seeds.py STAMPED (B1: 31, recorded only; B2: 31..35; B3: 31), once, with POL_SEEDS=stamped on a clean tree.
- Every control is a broken implementation built as a source variant of a production module (`exp.patches.variant`, or the multi-patch
  helpers `multi_variant` in exp/b1_modelcheck.py and `pg_variant` in exp/b3_pg.py, which assert that every fragment applies exactly once).
  The controls not yet in `exp.patches.CONTROLS` are defined inline in the experiment files and listed below as CONTROLS entries to add.
- Per-item rows hold raw observations only. verify/b1.py, verify/b2.py and verify/b3.py recompute every verdict from the rows with the stdlib
  and import nothing from pol or exp. A result is PASS only when the experiment's own summary and the verifier both say PASS.

### B1 (exp/b1_modelcheck.py, exp/b1_invariants.py)
- **Universe.** Principals u1, u2 (human), a1 (agent), n1 (analyst, proposer only). Authority r1 {u1,u2}, r2 {u1}, r3 {u1,a1}; proposers
  r1 {n1}, r3 {n1}; admin u1; tenant T1.
- **Pools.** One pool per requirement (r1, r2, r3), a `caps` pool, and a `joint` pool across requirements. Every pool is asserted at start to
  hold HEAD-bound events of all six binding types (accept, reject, amend, supersede, retract, resolve), so no binding type is only ever stale.
  Pools also hold grant / revoke of the agent's decide right, typed and agent confirmations, same-channel confirmations and redeliveries.
- **Depths (changed from "L = 6 minimum" to per pool).** Enforced: r1 6, r2 8, r3 8, caps 8, joint 4. Controls: r1 5, r2 6, r3 6, caps 6
  (a control needs only one counterexample; the shortest trace is found first because the search is breadth-first).
- **Caps scenario.** The open-proposal caps are not reachable at depth 8 with production constants (16 per principal, 256 per requirement).
  The caps pool runs a SMALL_CAPS variant that changes only those two constants (to 2 and 3). It is not a control: the enforced invariants must
  still hold, and I17 checks the caps.
- **Invariants.** I1 to I14 as adapted (I6 now: a redelivery returns `duplicate` with the ORIGINAL outcome code and leaves state unchanged;
  I13 exempts grants). New:
  - I15 a proposal needs a proposer or authority role on the requirement.
  - I16 a non-human REJECT changes no value and never moves the head (CORE-RULES rule 11). A first draft read N3 more broadly ("any
    non-human event that changes no value never moves the head"). That draft failed on a spec-permitted trace (see "Spec observation"), so
    I16 was narrowed to the rule text, and the run was repeated. This was disclosed before any stamped run.
  - I17 open-proposal caps per principal and per requirement.
  - L1 every contested state is resolvable by some authorised event (checked in every contested state).
  - L2 head starvation (liveness): a non-human event never increases the number of open human-authored proposals on its requirement, and moves
    the head only when it consumes one. This is checked inductively on every transition, and a played game (`l2_game`) gives the witness.
- **Liveness bound (DISCREPANCY with the directive).** The directive said a human needs at most 1 attempt. CORE-RULES section 7 gives
  attempts <= (open human-authored proposals on the requirement at the read) + 1, because a non-human may legitimately move the head by
  accepting an open human proposal. The spec bound is implemented. The control game shows attempts 2 > bound 1 with no open human proposal.
- **Coverage.** Every code in the reducer's code list is reached except `candidates_full`, listed as UNREACHABLE at these depths
  (it needs MAX_CANDIDATES = 8 contested siblings). B2's contest mode reaches it. The verifier requires all codes except the UNREACHABLE list.
- **Witnesses unchanged:** states >= 1000, transitions >= 20000, contested reached and resolved.
- **Controls (replacing the six of THRESHOLDS B1).** Each must yield >= 1 counterexample whose violated invariant is the expected one:

  | Control | Source | Expected |
  |---|---|---|
  | n3_agent_reject_moves_head | exp.patches.CONTROLS (required) | L2_head_starvation |
  | n2_no_role | CONTROLS | I15 |
  | n2_no_cap | CONTROLS (caps pool) | I17 |
  | stale_unchecked | CONTROLS | I4 |
  | agent_may_supersede | CONTROLS | I2 |
  | m2_no_original | CONTROLS | I6 |
  | self_approval_removed | NEW inline | I2 |
  | idempotency_removed (`old = view.idem(...)` becomes `old = None`) | NEW inline | I6 |
  | authority_removed (the head-moving `has_authority` check) | NEW inline | I1 |

  Remark-is-decision and last-writer-wins are dropped: the reducer has no remark-to-decision path and no write without a version check to patch.
- **CONTROLS entries to add to exp/patches.py** (module, old fragment, new fragment; exact text in exp/b1_modelcheck.py OWN):
  - self_approval_removed: pol._core, delete the `if verb == "accept" and not human and prop["author_kind"] != "human": return
    "self_approval", []` branch.
  - idempotency_removed: pol._core, `old = view.idem(pid, ev["idem"])` becomes `old = None`.
  - authority_removed: pol._core, delete the `if not view.has_authority(req, pid): return "unauthorized", []` check under
    `# ---- head-moving types`.
  - SMALL_CAPS (not a control): pol._core, `MAX_OPEN_PER_PRINCIPAL = 16` becomes 2, and `MAX_OPEN_PER_REQ = 256` becomes 3.
    These are two fragments, applied with `multi_variant`. Every B1 control is one fragment. In the caps pool, n2_no_cap is applied on top of
    SMALL_CAPS.
- **Rows.** `run` rows (per arm and pool: states, transitions, contested, codes, violations) and `cex` rows (trace ids, pre / post snapshots,
  event, code, original code, game for L2). The verifier checks trace shape and recomputes each control's invariant from raw pre / post.

### B2 (exp/b2_rebuild.py)
- **Logs.** 3000 logs (600 per seed; env B2_PER_SEED). Modes: random 70 percent (lengths 5 to 300, was 5 to 80, so long logs exercise
  restores and cap pressure), cap 15 percent (fills proposal caps, reaches `proposal_cap`), contest 15 percent (reaches `candidates_full`).
- **Differential against exp/ref_reducer.RefCore**, event by event: outcome code, original code and the comparable view must be equal.
- **Rebuild equals live.** The live path snapshots and restores every k events (k random 1..20); the rebuild folds from empty. Equal
  snapshot hash in 100 percent of logs, recorded outcomes equal, restores >= 3000.
- **Refusal audit.** RefCore.audit equals the refused production codes in every log. Every fifth log is also replayed through
  DecisionService + MemoryEngine with real sessions; each refusal returned must appear, in order, as a decision denial in the AuditLog.
  (B3 covers dc.audit.)
- **Coverage.** Every outcome code reached, including `proposal_cap` and `candidates_full`; contested in >= 10 percent of logs.
- **Controls (replacing the authority-map and drop-refused controls).** One-patch variants of `pol/_core.py`, each run on all 3000 logs:
  n3_agent_reject_moves_head, n2_no_cap, n2_no_role, m2_no_original, stale_unchecked, agent_may_supersede (CONTROLS), plus NEW:
  snapshot_drops_revocations (the snapshot writes `"revoked": []`) and restore_drops_idempotency (`s.idems = {}` on restore).
  Floor: mismatch (differential or rebuild) in >= 10 percent of logs each.
- **CONTROLS entries to add** (exp/b2_rebuild.py SNAP_CONTROLS, one fragment each, pol._core):
  - snapshot_drops_revocations: `"revoked": sorted(self.revoked),` becomes `"revoked": [],` in `snapshot()`.
  - restore_drops_idempotency: `s.idems = {(x[0], x[1]): [x[2], x[3]] for x in snap["idems"]}` becomes `s.idems = {}`.
- **Other changes to THRESHOLDS B2 text.**
  - "Manifest hash equal" is replaced by "snapshot hash of the comparable state equal". The core has no separate manifest.
  - Enforced event rows carry view hashes, not full views, to keep the artifact at about 22 MB. Control rows keep the full views of the
    first mismatch.
- **Rows.** `event` (both codes, original codes, view hashes), `log`, `svc_audit`, `ctl_log` (first mismatch with both full views).

### B3 (exp/b3_pg.py, container emlab-pg-polr-16096, port 25490)
- **Container changed** from emlab-pg-pol-16096 to emlab-pg-polr-16096 (the rescue owns its own). Free disk was checked first (stop line 20 GB).
- **Scenarios.** R = 50. S1, S3, S5 as before. Changes:
  - S2: the stale decision is bound to version 0 while the head is at 2 (binding to version 2 lands in the sibling-contest window, not stale).
  - S4: same idempotency key from two connections. Even rounds send identical payloads (`duplicate`, original `superseded`); odd rounds send a
    different payload (`idem_conflict`, original `superseded`). Exactly one event row. Overlap witness added.
  - S4b NEW: twin events with one idempotency key on two requirements. The advisory lock on (tenant, principal) serialises them, so the
    second gets (`idem_conflict`, `superseded`) and there is one event row. The unique index is the backstop, shown by a control.
  - S6 NEW: tenant isolation (unauthorized, tenant_mismatch, unauthorized).
- **Overlap witness** for S1, S3, S4, S4b, S5 in 50 of 50 rounds: a monitor connection sees the second transaction in pg_stat_activity with
  wait_event_type = Lock between the first transaction's lock point and its commit (database timestamps, re-checked by the verifier).
- **Differential arm NEW.** 40 Gen logs. Each is applied to a fresh PgEngine install and to MemoryState. Per event, codes are equal; per log, the
  Postgres comparable view equals the memory view and the rebuild from dc.events, `verify_chain` holds with chain length equal to the applied
  rows, and the dc.audit row count equals the refused count.
- **Rebuild.** After the scenarios, the rebuild from the event table equals the live head rows, and the chain verifies.
- **Controls (replacing last-writer-wins and no-lock).** pgcore variants, each on its own install, floor >= 90 percent of R:

  | Control | Patch | Pass condition |
  |---|---|---|
  | no_head_lock | head `FOR UPDATE` removed | both decisions `decided_accept`, status current (silent lost decision), overlap >= 90 percent |
  | no_advisory_lock | advisory xact lock removed | the unique-index backstop retried >= 1 time |
  | idem_unprotected | advisory lock and the `events_idem` unique index removed | both twins `superseded`, 2 event rows |

  These are CONTROLS entries to add (pgcore fragments in exp/b3_pg.py PG_CONTROLS):
  - no_head_lock: the head `select ... for update` line becomes `pass`.
  - no_advisory_lock: the `pg_advisory_xact_lock(...)` line becomes `pass`.
  - idem_unprotected: both of the above edits plus removing `create unique index events_idem ...` from the schema. This is a TWO-fragment
    variant (deviation from "one-patch"). It is the only way to show what the unique index protects: with the advisory lock still in place,
    the twins never overlap.
- **Rebuild wording change.** "Rebuild equals live in 100 percent of rounds" becomes: one rebuild over every scenario's rows after all
  rounds, plus a rebuild per differential log (40). Per-round requirements are disjoint, so one rebuild covers every round's head row.

### Dev results (seeds as above, run on t2-dev)
All three verifiers agree with the experiment's own verdict.

- **B1: PASS** with I16 as narrowed. DEV.b1_modelcheck.c49a62200313df9f.json, items 5.1 KB gz. Wall time 172 s (pools run in parallel).
  - 150,490 states, 1,401,814 transitions, 12,822 contested states, 0 enforced violations. No code missing apart from the UNREACHABLE list.
  - Runtime: r1 171.5 s, r2 28.0 s, r3 135.5 s, caps 21.5 s, joint 57.8 s.
  - The superseded draft run DEV.b1_modelcheck.e0a51a60e89bc225.json (broad I16) was FAIL on the spec observation below and nothing else.
  - All nine controls violated their expected invariant. Shortest traces: n3 [P3, AR3] (L2 game: 2 attempts against a bound of 1);
    n2_no_role [NP]; n2_no_cap [C1, C2, C3]; stale_unchecked [S1]; agent_may_supersede [AG1]; m2_no_original [R1, R1];
    self_approval_removed [P3, D4]; idempotency_removed [R1, R1]; authority_removed [S3].
  - n3 game: the read is at version 0, probe 1 is `stale`, probe 2 is `superseded`, giving 2 attempts. P3 is authored by n1, a non-human, so
    there are no open human proposals and the bound is 1.
- Authoritative DEV files are the three named here. DEV.b3_pg.90e08a7e2ac8f1bd.json is a superseded FAIL caused by the S2 test bug.
  Nothing is stamped.
- **B2: PASS.** DEV.b2_rebuild.6f2d4ceb2cac6a4b.json, items 21.8 MB gz. Runtime 1700.6 s.
  - 3000 logs, 454,450 events. 0 mismatched events, 0 unequal rebuilds, 0 bad ref-audit logs.
  - Every code reached, including proposal_cap, candidates_full and concurrent_agree.
  - Contested in 1280 logs (42.7 percent). 81,821 restores.
  - Service audit: 600 logs, 67,690 refusals, 0 bad.
  - Control mismatch rates: n3 0.435, n2_no_cap 0.146, n2_no_role 0.751, m2_no_original 0.574, stale_unchecked 0.878,
    agent_may_supersede 0.728, snapshot_drops_revocations 0.177, restore_drops_idempotency 0.982.
  - verify/b2 takes 18 s and peaks at 2.3 GB RSS. Run it on t2-dev, not the Mac.
  - No divergence from RefCore.
- **B3: PASS.** DEV.b3_pg.d5b01c4c9f299692.json, items 226 KB gz. Runtime 1074.4 s.
  - S1 to S6 and S4b: 50 of 50 each.
  - Overlap witnesses: 50 of 50 for S1, S3, S4, S4b and S5.
  - Differential arm: 40 logs, 7672 events, 0 mismatches. Views, rebuild, chain and dc.audit counts are all OK.
  - Scenario rebuild is equal, and the chain verifies (850 rows). Enforced backstop retries: 0.
  - Controls: no_head_lock 50 of 50 (overlap 49), no_advisory_lock 50 of 50 (overlap 48), idem_unprotected 50 of 50.
  - Not proven, as the result records: a single PG16 instance at READ COMMITTED, two writers per round (not N-way), no process kill or network
    partition mid-transaction. Revoke takes the tenant row FOR UPDATE, which serialises the whole tenant (a scaling limit).

### Spec observation (a rule decision for the owner, not a production bug)
The reducer and RefCore both follow CORE-RULES here. Rule 15 and section 7 let a non-human accept an open human proposal. Reference
ambiguity 11 (exp/ref_reducer.py) says resolve does not change proposal statuses for a set effect.

The consequence is found at depth 6 in pool r1 (ids from POOLS["r1"]):
1. P2: u2 proposes vB.
2. GR1: grant a1 decide on r1.
3. S1h: u1 supersedes at 0, to v1.
4. D2: u2 accepts P2 at base 0, giving contested_opened, v2.
5. RES1: resolve, choosing D2's candidate vB, giving v3. P2 stays open.
6. AGA: a1 accepts P2 at v3, giving decided_accept, v4. The value is still vB, but value_by is now a1.

An agent moves the head and takes attribution for a value a human already adopted. A human holding v3 is then `stale` once. This is within
the section 7 bound, because P2 is consumed and L2 holds.

If that is unwanted, either of these fixes it:
- close the adopted proposal on resolve, or
- refuse an accept whose effect equals the current value.

Either change needs the same edit in CORE-RULES and RefCore.

Resolution (rescue owner, this amendment): the second option is adopted. A non-human accept of the current value is refused
`no_value_change` (CORE-RULES rule 15). I16 is restored to the broad wording ("any non-human event that changes no value never moves the
head"), and the narrowed I16 and the B1 dev runs above that used it are superseded.

## Amendment 4 (2026-11-16T06:29Z, after DEV runs at 0bcabe4; committed ALONE, before the stamped run of this round)

Why: code changed after amendment 3 (931c95a). Each change is listed here so the stamped run is read against the committed rules. The
stamped seeds, the once-only rule and the verifier rule of amendment 3 are unchanged.

Changes since amendment 3:
- f23f119 (code and controls):
  - New control `n3_value_unchanged_moves_head` in exp.patches.CONTROLS. It deletes the `no_value_change` refusal in pol/_core.py.
  - B1 expects I16 for it (r1 pool, control depth 6, the shortest trace found by the spec observation).
  - B2 runs it on all 3000 logs with a floor of 3 percent mismatch, not 10 percent: the trace needs a resolve, a still-open adopted
    proposal and an agent with decide rights, so it is rare in random logs. DEV at 0bcabe4: 4.57 percent (137 of 3000).
  - The reference reducer's `no_value_change` rule was written by an independent helper (agent a633415) from the CORE-RULES text,
    not by the rescue owner. It is recorded as ambiguity 21 in exp/ref_reducer.py, and the selfcheck prints ALL PASS. This replaces
    the rescue owner's edit described in amendment 3.
  - PgVault reads its HMAC key from a 0600 key file. The key is never stored in the database.
  - Stamped preflight: exp/seeds.py refuses stamped seeds unless GIT_STAMP says dirty=false and no dev override variable
    (E2_PER_SEED, E5_PER_SEED, B2_PER_SEED, B1_QUICK) is set.
  - verify/b1, b2 and b3 list `no_value_change` among the refused codes. The DEV disagreement of B2 7f75b6cb and B3 7556d62d came from
    verifiers without it; at f23f119 both agree PASS.
- 0bcabe4 (privacy and M2), found by the new tests.test_pg service-over-Postgres test:
  - The append-only event log, heads and proposal rows held subject names inside the label. DecisionService now puts into the event a
    label whose subjects are keyed per-tenant tokens (vault key). The vault keeps the real subjects for reads and erasure. A body
    label without a value is refused `bad_event`.
  - A duplicate retry returns the original value_ref (M2).
  - The experiments drive the core with their own labels, so their outcome codes are unaffected. E2, E5, E6 and B2's service arm go
    through the service and pass.
- ca427b7 (verifier): verify/b1.py recomputes I16 with the amendment 3 wording (a non-human event that changes no value never moves
  the head), not the narrowed reject-only draft. Before this fix, verify/b1 could not recompute the new control's counterexample.

Seeds used since amendment 3, all outside the stamped set:
- The string seeds "dev-<exp>-1".."dev-<exp>-5".
- For one in-memory differential probe of _core against RefCore at f23f119, the integer seeds 100..159.
- No stamped seed (31..35, 99, 808) has been used, and no run used POL_SEEDS=stamped.

DEV results at 0bcabe4 (dev seeds; development observations, not proof):
- All agree PASS after the verifier fix: E1 b8ca506e, E2 2950c15a, E3 44d90789, E4 9f6a4f86, E5 9d608bb2, E6 3f2d3aa4, B1 97f7f477,
  B2 5c02b1a0, B3 02c0aa95.
- B1: 150,489 states, 1,401,814 transitions, 12,822 contested, 0 enforced violations, every control recomputed. B1 reaches
  `no_value_change` (the verifier's codes_missing is empty).
- B2: 3000 logs, 454,450 events, 0 mismatches, 0 unequal rebuilds, service audit 600 logs and 0 bad.
- B3: S1 to S6 and S4b 50 of 50, overlap witnesses 50 of 50 where required, differential arm 40 logs and 7672 events with 0
  mismatches. Controls: no_head_lock 50 (overlap 49), no_advisory_lock 50 (overlap 48), idem_unprotected 50.
- tests.test_pg: 8 OK on t2-dev, including test_service_over_postgres.
- Superseded DEV artifacts, which fail the current verifiers because they lack the new control or the new code: B1 e0a51a60,
  7bff4be0, c49a6220, 99a66737; B2 6f2d4ceb; B3 90e08a7e.

The stamped run follows this commit once, on a clean tree, with POL_SEEDS=stamped. Its verdict is reported as it falls.
