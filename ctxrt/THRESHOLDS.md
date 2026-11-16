# ctxrt thresholds (ticket goodqa#16096), committed BEFORE any proof run

Common (PROOF-RULES 1, 2, 4, 7). Every proof runs with 5 seeds (16096001..16096005) and reports the WORST seed. Control sensitivity floor: the
broken variant must violate the invariant in at least 10% of its eligible runs on EVERY seed, or the proof for that item is reported
VACUOUS (not PASS). Timeouts and refusals score as failures. Every result file carries commit, UTC time, parameters, python version and
host. A race claim reports `overlapped / runs`; below the stated overlap floor the race result is VACUOUS. Stub models only: no model call.

## P1 trimming and injection (provider-valid conversations)
- Per seed: 2000 random conversations per shape (anthropic, openai), each with a random budget between the size of the mandatory tail
  and the full size. Conversations contain parallel tool calls, results in shuffled order, text after results, empty and huge results, unicode.
- PASS: 0 invalid outputs from trim, from inject, and from trim+inject, in both shapes (validator = ctxrt/msgshape.py; recomputed by an
  independent validator in ctxrt/recompute.py).
- Witness floors per seed and shape: >= 30% of cases trimmed at least one unit; >= 40% of cases hold >= 1 tool pair; >= 25% of cases end in a
  tool-result turn (the hard injection case); >= 200 cases where a message-level tail trim would cut inside a pair.
- Validator sensitivity: 5 mutation classes (orphan result, missing result, text before result, wrong id, duplicate id) x 200 mutants per shape,
  100% rejected; 0 false positives on the generated valid conversations.
- Budget: when over_budget is False the result is within budget in 100% of cases.
- Controls (each >= 10% invalid among cases with >= 1 pair, per seed): (C1) naive message-level tail trim; (C2) naive injector that puts the
  block before the tool results (anthropic) or between the assistant tool_calls and its tool messages (openai).

## P2 compaction and resume
- Per seed: 300 random sessions (2 to 8 turns, random compaction points, checkpoints, resumes, log appends after the checkpoint), with
  summarisers that: copy the block, paraphrase it, inject a stale value, drop it.
- PASS: (a) the block after resume has the record-set hash of a regeneration from the log at the sealed watermark in 100% of resumes, and
  contains no record with seq > watermark outside the labelled delta; the delta holds exactly the records with seq > watermark;
  (b) a marker planted in an injected block, in three layers (own message; merged into the user message text; paraphrased into the compaction
  summary), is re-ingested as a new memory event in 0 of >= 300 plants per layer per seed (identical events with the original id are not new);
  (c) a marker the USER types is still ingested (0 false drops in >= 300 per seed).
- Controls (>= 10% violations per seed): (C1) ingest without the provenance filter re-ingests the marker; (C2) block taken from the compaction
  summary serves summary content that is not in the log; (C3) skipping regeneration serves a superseded value after a log change.
- Ablation (must fail): origin allow-list alone fails the merged layer; delimiter strip alone fails the paraphrase layer.

## P3 pull tools (search, open, timeline, current_value)
- Per seed: 300 random stores incl. events up to 200 KB.
- Bounded: 100% of results <= 6000 characters serialized, `truncated` flag set whenever data was cut.
- Opaque ids: 0 occurrences of internal event keys, session ids or sequence numbers in >= 5000 outputs; 1000 forged or mutated ids give typed
  `not_found` 100%.
- Typed nothing found: 100% of >= 300 empty queries return status `no_results` with a reason from a closed set; never an empty list or None.
- Pagination under concurrent appends: 100 runs per seed with real threads and randomised timing; every run: pages are disjoint and their union
  equals the snapshot set at first page (100%). Overlap floor: >= 30% of runs have >= 1 append committed between the first and last page fetch.
- Current value: 300 planted change chains per seed (2 to 5 values, retractions, as_of in the past): 100% equal to the decision state fold;
  every answer carries the deciding event id and the checked-through watermark.
- Controls: (C1) offset pagination over live data duplicates or skips in >= 10% of overlapped runs; (C2) search-backed current_value returns a
  superseded value in >= 10% of planted changes.

## P4 analyst runtime
- 4a keyed runs: 200 keys per seed; replays: sequential, 8-thread concurrent launch behind a barrier, and retry after a crashed run.
  PASS: exactly 1 model-call set and 1 run row per key; identical results. Overlap floor: >= 100 launches per seed where >= 2 threads contended.
  Control (unkeyed): duplicates in >= 10% of keys.
- 4b budget: reservation made before the first call; calls charged to the originating step only; spent <= reserved in 100%; a misbehaving stub
  that tries 3x its reservation is refused at the reservation limit; with 8 concurrent runs against an allowance for 5, at most 5 start (100%).
  Controls: (C1) charge-after-the-run lets a runaway stub overspend in >= 10% of runs; (C2) non-atomic check-then-reserve overshoots the
  allowance in >= 10% of concurrent trials.
- 4c stall and stop: loop stub (same call repeated, still heartbeating), hang stub (blocked in a model call), slow-but-progressing stub, and a
  stub PROCESS that ignores SIGTERM. PASS: loop and hang killed within stall_s + 2 ticks in 100% of 100 runs; 0 of 100 progressing stubs killed;
  the SIGTERM-ignoring process is gone after the stop in 100% of 30 runs; after stop() returns, 0 inbox writes and 0 model calls land from the
  stopped run while the zombie attempted >= 1 of each in >= 90% of runs (witness). Control: a cooperative-only flag lets zombie writes land in >= 10%.
- 4d findings: 10 malformed classes x 50: 100% rejected with a typed code, 0 injected; `nothing_relevant` renders the empty string; dead ends
  stored; seen-set: a second run on the same question makes 0 model calls on candidates rejected in the first run (control without the
  seen-set pays for >= 1 each).
- 4e host check (quote and speaker): 400 findings per seed: valid accepted 100%; mutated controls (word replaced, adjacent round, speaker flipped)
  rejected 100%; ambiguous speaker goes to review and is injected 0 times; a planted instruction in the analyst claim is present in the
  model-facing block in 0 cases (control: injecting the claim leaks in >= 10%); a planted instruction inside a verified quote stays inside a
  framed data record line.
- 4f async: with a hanging analyst, turn latency <= 1.5x baseline + 50 ms; the synchronous control adds >= the analyst delay; write attempts by the
  analyst are refused 100% and the log hash is unchanged.

## P5 inbox delivery end to end
- Schedules (each 200 runs per seed, real threads, randomised pauses against a real short lease): paused turn, crashed turn (before and after
  commit), checkpoint rollback, forked session.
- PASS: each finding appears exactly once in the surviving lineage (0 duplicates, 0 lost) in 100% of runs; stale-token commits are refused 100%.
  Witness floors per schedule per seed: paused turn: >= 30 runs where the paused turn attempted its commit with a stale token AFTER the takeover
  turn claimed; rollback and fork: >= 100 runs where the schedule really rewound or forked with an item already delivered.
- Controls (>= 10% duplicate-or-lost runs per seed): delivery without the token (confirm by turn id; fixed lease), cursor kept outside the checkpoint,
  fork sharing the native session id and global delivered state.
- Policy re-check at delivery: 200 cases per seed where policy is tightened between enqueue and delivery (revocation, tenant kill, label tighten):
  0 delivered; the enqueue-only control delivers >= 10%.

## P6 reviewer routing
- Per seed: 2000 random records and pools, including delegation chains, id aliases of the same lineage and pools containing only the author.
- PASS: 0 assignments or approvals by any member of the author set (author, its delegates, its alias ids); no eligible reviewer gives a typed refusal
  (never a fallback to the author); author id and author model present on 100% of records created in the proofs; record creation without them refused 100%;
  reviewer and click-tester inbox claims receive 0 author-origin items.
- Controls (>= 10% per seed): availability-only routing assigns the author set; inbox without the origin filter delivers author-origin items.

## Not provable here (stated beforehand)
Real model behaviour of any analyst; real provider acceptance of the shapes (validators encode the documented rules, not a live API call); Elasticsearch point-in-time
semantics; prompt-cache cost; multi-host clock skew; hosts that strip provenance from transcripts entirely.

## Amendment 1 (committed before any proof run; supersedes the text above where it differs)
- Artifacts (rule 5): every proof P1-P6 writes a hashed per-case JSONL (`results/<proof>-<seed>.jsonl`, sha256 recorded in the summary JSON) and
  `ctxrt/recompute.py` recomputes the headline counts from the JSONL for P1-P6. recompute.py is written by the same author: it is a SECOND
  implementation, not independent evidence; the report says so. The runner (`ctxrt/harness.py`) refuses a dirty tree and stamps code commit,
  threshold commit (67a2842 + this amendment's commit), UTC time, parameters, python version, host, load average.
- Canaries: every "0 of N" criterion has a positive canary in the SAME arm and the same code path that must be non-zero: P2b user-typed marker
  ingested; P5/policy: the same item IS delivered when policy is untightened; P6 inbox: a non-author reviewer DOES receive the item; 4d/4e: a
  non-planted valid finding IS injected. A zero canary makes that criterion VACUOUS. Delivery-time eligibility (expiry, requirement graph) is
  asserted in the canary arm so a graph mismatch cannot yield a vacuous 0.
- Control reasons: each control records a histogram of violation classes; the class named in the criterion must itself reach the 10% floor
  (P1 C1: class `orphan_result_or_split_pair`, not a leading-assistant start; P3 C1: class `duplicate_or_skip`; P5: class `duplicate` for paused turn and crash,
  class `lost` for rollback and fork).
- P1: also the head-orphan case (trimmed output that starts with a tool result) is a validator class and must be 0 for the real trimmer; trimming may add
  one synthetic user marker message when the surviving head would be an assistant message (counted, reported). Pairing rules source: Anthropic Messages tool
  use ("all tool_result blocks for one assistant turn in a single user message; tool_result blocks first, text after"), cross-checked in the claude-api skill
  shared/tool-use-concepts.md (parallel tool use paragraph), 2026-11-15, confidence medium-high; NOT verified against a live API (no model call allowed).
  OpenAI-compatible rule: an assistant message with tool_calls must be followed by one `tool` message per call id before any other role; confidence high, not live-verified.
- P2: the write-path checkpoint seal is a STUB in this branch (a recorded watermark + hash-chain digest of the log prefix). Results hold for a stub seal only.
  Deviation from the plan row "byte-identical": the proof is on the record-set hash plus rendered text with the delimiter sealed per checkpoint; the real
  per-turn random delimiter is an open design question (whether to seal it). The provenance filter is keyed on a LEDGER of issued blocks plus message origin; the
  delimiter strip is applied only to ledgered delimiters, never a bare regex on the framing.
- P3: decision state = vendored pol/core.py reducer (515e594: supersede, retract, contested, explain); the egress guard stays on in PASS arms (real-id
  helper from memcore/tests/_realids.py); no `egress_guard=None` in any PASS arm.
- P4: the run key is the task's (question or trigger, input watermark, prompt version) plus logical session as namespace. DESIGN 3.8.6 says (logical session,
  step, manifest hash): mismatch recorded as an open design question, including who is charged when two steps share a key (first launcher is charged; later
  launchers attach and are charged 0). 4b adds an overlap witness and a stale-epoch zombie actor to the concurrent reservation test (>= 100 trials per seed
  where >= 2 threads were inside reserve at once; a zombie run with a revoked epoch attempts >= 1 spend and is refused). The stop proof uses child PROCESSES
  in their own process group with SIGKILL escalation, a parent-death watchdog and a hard self-timeout; children are registered and reaped; threads are used
  only for non-killable logic. An in-flight call at stop is DISCARDED (its result is refused at the gateway), not recalled. Timing results (4c, 4f) record the
  load average and are labelled NOT QUIESCED; stall decisions use an injected clock wherever the case allows it.
- P5: appearance is counted in the committed transcript of the surviving lineage, not by delivery rows. The checkpoint cursor is the single source of truth;
  the turn commit (transcript + cursor + confirm) is one store transaction with a fence check by the store, not by the caller. Rollback and fork use
  lineage-scoped inbox session ids with re-enqueue of everything not in the checkpoint cursor. Controls map to schedules: tokenless delivery -> paused turn and
  crash (duplicate); cursor outside checkpoint -> rollback (lost); shared native id -> fork (lost for one branch). Each control is judged only on the schedule
  it can fail. memcore has no reject transition for an item denied at delivery: ctxrt handles it and it is reported as an upstream gap.
- 4e: quotes that also occur verbatim in the adjacent round are excluded from the adjacent-round control and counted separately (as in PROVENANCE_CHECK).

## Amendment 2 (written AFTER the first runs of P1-P4 and P6 and BEFORE any rerun; every change strengthens or discloses, none loosens a number)
Review of the first results found weaknesses in the proofs themselves. They are fixed or disclosed here; the first-run numbers stay in the record.
- 4f first run: seed 16096003 failed the latency criterion (99 of 100) under host load average 12 to 23 with the async arm unchanged. It is reported as FAILED under load, not rerun for a pass; a rerun is reported next to it. The synchronous control was also fixed (it must measure from the analyst launch; first run: 71 to 76 of 100 above 0.2 s).
- 4b C1 ("charge after the run") was arithmetic on a coin flip. It becomes a real broken gateway class (`ChargeAfterRunDB`: no reservation limit, no fence) driven by the same stub population through AnalystRun; the real gateway runs the same population and must overspend 0 times.
- 4c adds the DISCARD path: a stub that ignores the cancel event and returns after the stop; the real gateway must refuse its late result in 30 of 30 runs (one `in_flight_result_discarded` refusal, 0 steps consumed); the control without the post-call liveness check must consume it in >= 10%.
- P2(a) witness is recounted from the stored `ops` rows: turns served after a resume and turns served after a compaction, each >= 100 per seed (the first run counted resume operations, some of which had no following turn). This is evaluated in recompute.py from the per-case rows.
- Author id and author model: carried with every finding that reaches an inbox (`item_origin`, NOT NULL), written inside the same write-gate transaction; `FilteredInbox` reads the origin from that table; criterion: 100% of findings that reach an inbox in 4c, 4d, 4e have both. P5's `d_findings` and the chain test below read the same table.
- P3: the empty-store check no longer uses `surface(None)`; it runs with the egress guard on (Amendment 1 forbids a guard-less PASS arm).
- P4 and P6 write per-case rows (all cases, not aggregates), as Amendment 1 promised.
- `stopper.py` passes the caller's WAL and agent id to the audited stop (CTXRT_WAL, CTXRT_AGENT) so audit rows do not land in the main WAL; the 21 rows that the dry runs already put in the main WAL are reported to main.
- New proof P7 (integration smoke, 200 cases per seed, 5 seeds): analyst finding -> inbox with recorded origin -> policy-checked turn commit -> trimmed provider message with injected block -> shape validator. PASS: valid findings give a valid conversation in both shapes (0 invalid) containing the verified quote once and the finding id once; nothing_relevant, malformed and stopped runs leave the conversation byte-identical to the un-injected one (0 injected); a planted instruction in a verified quote appears only on one framed data line; a reviewer session in the author set receives 0 of the items; origin model recorded 100%; witness floors: >= 40 cases per class per seed. Control: the same chain with the naive injector gives invalid conversations in >= 10% of tool-tail cases.
- Disclosures (not thresholds): P1 budgets were drawn from 0.6 x the mandatory tail (not from the tail): about 31% of cases were over budget (flagged, still valid) and about 60% added the user marker; P5 policy has about 133 tightened cases per seed (threshold text said 200); P5 concurrency runs on one in-process SQLite connection behind a lock (no cross-process or Postgres evidence; the fork schedule reports no overlap count); the P4 process proof composes the fence and the audited stop by hand and `scan_stalls` only fences; a crash mid-run with calls already spent is untested; P3 controls fail at 93 to 100% and the 4d/4e render controls run on constant input (weak controls, sensitivity floor met but not graded).

## Amendment 3 (written after the independent review of 1d19cd8, BLOCK, and before the rerun at one commit; strengthens, never loosens)
Review: research/ctxrt-review-1d19cd8.md. Reviewer probes confirmed 33 of 33 findings at 1d19cd8 (ctxrt/results/repro-at-1d19cd8.txt). Every proof is rerun at ONE commit.
- New criteria and controls (each criterion has a control that fails at >= 10%): F1 mixed claim limits (control: the 1d19cd8 partial-confirm commit); F2 analyst findings through the durable path under rollback
  (control: direct enqueue); F2b finding id in two logical sessions (control: global id); F10 turn id reused after rollback (control: lookup by turn id alone); F3 cross-session citation (control: host check without the
  readable-session rule), sub-span quotes (control: 1d19cd8 sub-span rule), speaker rendered as trust_origin; F4 tenant and principal scope, owner-bound signed cursors surviving a restart, bounds in UTF-8 bytes (controls:
  unscoped tools, unbound cursors); F14 keyset paging with an idf scorer pinned to the snapshot (control: live idf statistics); F5 seen-set scoped by (tenant, session, question), filled only from host-verified outcomes and
  fenced (control: session-blind declaration-seeded set); F11 stop withdraws written-undelivered findings (control: no withdrawal); F12 heartbeat instead of a wall-clock progress rule (control: no heartbeat);
  F6 origin per finding instance, fail closed on missing origin or unmapped recipient, approve needs a routing record and a registered different-model reviewer, delegators and same-model families excluded
  (controls: 1d19cd8 first-writer-wins fail-open inbox, availability routing, unfiltered inbox); F7 thinking blocks with signatures, server-tool blocks, images and documents, tool errors, empty and list results, the user's task never
  trimmed inside a tool loop (turn-atomic trimming), block injected inside the last tool_result (control: plain text after results, which is a 400 when a server tool is unresolved; control: message-level trim that loses the task);
  P2 persisted ledger across a restart, summary recorded as a user message, truncated block keeps the user's text, resume and ingest together with unique entry ids, regeneration compared with an INDEPENDENT implementation (controls: in-memory
  ledger, no compaction-event marking, 1d19cd8 strip, id counter restored on resume).
- Latency criterion 4f is DROPPED (a 1 ms stub makes it meaningless); replaced by a structural ordering test: a turn completes while the analyst is blocked inside a model call; a synchronous turn does not.
- Policy dependence (F8): P5's policy arms, P7's policy step and P3's current_value for contested or multi-principal states are labelled NOT PROVEN until the rescued policy point lands. Sender, recipient and label are derived
  server-side behind `PolicyBridge`; with no bridge nothing is delivered (fail closed). P5 policy arm now runs 200 tightened cases plus 50 canaries per seed (the earlier run had about 133 of 200).
- Raw per-case storage: P1 stores raw input, trim, final and control outputs for 300 cases per seed and shape; P2 raw plant transcripts; P3 raw page ids; P5 raw committed transcript rows and master lists; P6 raw registry; P7 raw final
  conversations. recompute.py re-judges those with its own implementations. P4 stores per-case outcomes of its own judgments only.
- Disclosed deviations since the first run: the P5 policy arm ran about 133 of 200 tightened cases; the 4f synchronous control and the p4_process readiness wait were changed after they failed; the first failed runs are kept under
  ctxrt/results/run1-* and listed in HEADLINE.json. The vendored memcore is rebased onto the adapters branch at b054d5d (the stricter turn pattern; ctxrt turn ids use the `<id>@<utc second>` form).
- Not changed: stub models only; one in-process SQLite connection behind a lock except p4_process; no live provider call.
