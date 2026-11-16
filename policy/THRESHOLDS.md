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
