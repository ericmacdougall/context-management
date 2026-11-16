# Open questions register

Ticket: GoodQA goodqa#16096. Requirement: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a/760f5af3-ed9d-4f90-95c3-4e708e139b4e "keep going until you have a proven design / all experiments and questions are resolved so we can build it for real".

Refreshed 2026-11-15 (branch docs/design-v2) from the evidence named in each row. Source keys (RV-ENG, RV-WP, RV-SYS, RV-AD, RV-ES, RV-G1, RV-LIC, RV-SRC, FAB, PREREG, XP, CURVE, RAGCTX, CANARY, ENG, WP, SYS, MC, BEH, PF, LR, LEDGER, GOT, WAL) are defined in DESIGN.md section 0. No row has been deleted. Sections K and L are new. Section M lists every question only the user can answer.

"Proven" here means every row below has reached its exit. A row can exit only one way:

- EXP: settled by a node or systems experiment with a REALLY_TESTED entry.
- EVAL: settled by the pre-registered eval.
- USER: decided by the user in their own words.
- DR: closed by a decision record (the design avoids the question; say why).
- OPEN-BLOCKED: cannot be settled under current approvals; reported as open, never as proven.

Status values, each backed by the artifact or report in the Evidence column:

| Status | Meaning |
|---|---|
| answered-reviewed | An independent review confirmed the answer from artifacts (with the review's limits). |
| answered-owner-reported | The building agent answered it with an artifact; no independent review yet. |
| exploratory | A pilot or exploratory result exists; not confirmatory. |
| did-not-hold | The idea the row tests was shown false (the cite says at what evidence level). |
| in-progress | An owner is working on it now (WAL shows the launch). |
| todo | Not started, or started and stopped without a usable result. |
| needs-user | Only the user (or the client) can settle it; see section M. |

Where a row has several sub-questions, the status is the most conservative across them. A result file with `pass: true` that a review contradicted is not counted as a pass. No row is answered until its exit is reached; "answered-owner-reported" rows still need a review before the design can call them proven.

## A. Elastic behaviour (stream: behaviour, test node on the shared lab host)

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| A1 | Search visibility delay after a write; GET by id realtime; wait_for and the 1000-listener limit | EXP E1 | answered-owner-reported | BEH E1_refresh.json (feat/elastic-behaviour 6f53c80): visibility p50 about 1,001 ms over 200 writes |
| A2 | Optimistic concurrency gives one winner; _bulk has no cross-item atomicity | EXP E2 | answered-reviewed | RV-ES E2 REPRODUCED; E8d PARTLY (item-level error inside HTTP 200) |
| A3 | Data stream as idempotent event mirror (create + fixed id; backfill placement; no direct update/delete) | EXP E3 | answered-reviewed | RV-ES E3: create gives 409 within a backing index, but a rollover accepts the same id again and backfill goes to the write index; design uses plain indices (DESIGN 3.2.1) |
| A4 | Validity intervals: date_range vs two date fields; as-of, currently-valid, sort | EXP E4 | answered-owner-reported | BEH E4 results (6f53c80) |
| A5 | Percolator: lexical triggers at 1k/10k/100k stored queries; knn/sparse/semantic stored queries | EXP E5 | answered-owner-reported | BEH E5 results (e1da2d9 Basic, 15e0743 trial); vector queries rejected in the percolator |
| A6 | Filtered kNN recall and latency (bbq_hnsw, int8_hnsw; acorn vs fanout); bbq_disk licence failure point | EXP E6 | answered-owner-reported | BEH E6 results; bbq_disk on Basic: mapping 200, every document 403 (E6_bbqdisk_basic.json). RV-ES confirmed only the direction (fanout better at a 5% filter); exact figures not re-tested |
| A7 | Which fusion and rerank features work on Basic vs trial, with exact errors | EXP E7 | answered-owner-reported | BEH E7_basic.json, E7_trial: on Basic rrf, linear, text_similarity_reranker and ES|QL RERANK return 403, ES|QL FUSE 400; all fine on trial |
| A8 | semantic_text: which update paths re-run inference | EXP E8 | answered-reviewed | RV-ES E8a (bulk partial: 0 inference calls; embeddings lost after an unrefreshed write), E8b PARTLY, E8c, E8e |
| A9 | Document-level security: hit isolation, score/aggregation leakage, kNN and semantic coverage | EXP E9 | answered-reviewed | RV-ES E9a hit isolation, E9b term-stat leak, E9c index per tenant removes it, E9d dfs does not, E9e constant-score removes it |
| A10 | Deleted content persistence on disk before and after merge | EXP E10 | answered-reviewed | RV-ES: deleted text persists in segments; lease-synced single-segment merge purges (6 of 6); whole-index DELETE removes at once; "only reindex purges" refuted |
| A11 | Parity: client-side fusion vs Elastic rrf/linear; external E5 vectors vs built-in E5 via inference API, same pilot queries | EXP E11 | answered-owner-reported | PREREG 4.2 and 4.3 (es_round_parity.json, es_e5.json): round top-10 overlap BM25 0.751, client RRF vs Elastic rrf 0.817, linear 0.863; E5 cosine 1.0 with prefixes, top-10 overlap 0.718 without. Parity is partial; see K27 |
| A12 | Serverless behaviour (5 s refresh minimum, realtime GET, no force merge, no snapshots) | OPEN-BLOCKED: needs a cloud project | needs-user | Section M, M14 |
| A13 | Disk-based vector index recall under heavy filtering at real scale | OPEN-BLOCKED partly: Enterprise feature, trial only, synthetic vectors | todo | No artifact at real scale |

## B. Does the memory design work (stream: eval)

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| B1 | Can this benchmark decide anything about massive context? Reader with gold evidence vs reader with full history on ~10 pilot questions | EVAL micro-pilot | answered-owner-reported | PREREG 4.1: n = 10 micro-pilot, the full-history reader matched the gold-evidence reader on 9 of 10, so the benchmark shows no answer-level headroom at that size. The eval moved to the pooled-history curve (WAL we0a5cc3861e). LEDGER critic: this is not evidence about long-context reading in general |
| B2 | Retrieval recall by method (BM25, dense, hybrid, rerank) on Elastic | EVAL Phase R | exploratory | CURVE: session recall@10 at H3 RRF 0.4754, BM25 0.494, linear 0.492, dense 0.345 (exact retriever, not Elastic). Elastic parity partial (A11). Pooled rerank not rerun (K28) |
| B3 | Size of the low-overlap stratum; is it large enough to test write-time cue keys | EVAL Phase R pilot | answered-owner-reported | PREREG 3.2 (lowoverlap_pool_h3.json): 23 of 94 pilot questions at the whole pool (28 at a cut of 50, 35 at 20). The cut was chosen after the counts were seen. Replaces the stale "24 of 94" |
| B4 | K1: facts with validity + hybrid retrieval beat the best baseline on update and temporal questions | EVAL Phase F | needs-user | Fact extraction held by the user's choice (WAL w11160e65223 "Analysts and search first"); resuming it is M10 |
| B5 | K2: concept docs beat facts + retrieval | EVAL Phase F, only if K1 passes | todo | Depends on B4 |
| B6 | Fact extraction quality on its own: supersession precision and recall against gold | EVAL Phase F | did-not-hold | LR GATE3_RESULT: link precision 16 of 16 but recall 10 of 16 against a bar of 12 (gate G5 fails); 9 of 25 pairs flip between batchings. PF GATE_RESULT: recipe v3 failed G4 precision (20 of 32) |
| B7 | Abstention: closed-world check vs retrieval-only | EVAL Phase F | exploratory | XP E3: abstention questions not separable by the retrieval-confidence predictor (pilot). Closed-world check not run |
| B8 | Write-time cue keys recover low-overlap facts | EVAL Phase F ablation | todo | Needs extraction (B4); RV-SRC critic on X28 |
| B9 | Accuracy as history grows (S vs M variant or a length curve) | EVAL, after B1 | in-progress | Pooled-history retrieval curve done (CURVE); analyst arms scored on the pilot (AFULL, cc83d26; run started on WAL w8408a173d29, ASSUMPTION that "do it" meant run now); rerank arm still running; no answer-level curve yet |
| B10 | As-of time filtering works on histories with supersession more than a day apart | EVAL on M variant or user sessions; S cannot test it (measured) | todo | PREREG section 2: the day rule removes 0 gold sessions on S |
| B11 | Judge validity: leniency on deliberately wrong answers | EVAL pre-test probe | todo | No artifact |
| B12 | Multi-year, 10M+ token scale | OPEN-BLOCKED: no such public data; partially via user sessions | needs-user | Depends on M1 (which private sessions) |

## C. Systems properties of the design (stream: systems)

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| C1 | Rebuild: all Elastic projections reproducible from the Postgres log; cached replay exact, uncached within a measured variance band | EXP | answered-owner-reported | SYS C1_rebuild.json (a13b524, no pass field). RV-SYS: the blue/green v2 control never produced a mixed view, so "no mixed view" is unproven |
| C2 | Projection lag: time from append to retrievable; tail-scan fallback covers the gap | EXP | answered-owner-reported | SYS C2_lag_tail.json: 100 of 100 read-after-write; 600 events written during a projector pause all found; BEH E1 visibility about 1 s |
| C3 | Erasure cascade: key destruction plus derived-row purge via input-set provenance; tombstone racing an in-flight projection | EXP | in-progress | Systems version failed review (RV-SYS N1, N2, N3, N4, N6). Rebuilt write path: N1, N3, N5, N8 supported, N2, N4, N6, N7 partly, and material findings F1 to F8 (RV-WP, verdict BLOCK); write-path owner working on them (WAL) |
| C4 | Stored prompt injection: untrusted text re-injected later does not gain authority (trust origin, nonce envelope, tool gateway) | EXP attack set, absolute bar | todo | Framing part reviewed (RV-AD: block cannot be forged); attack set not built (UNCOVERED-TOP30 #4) |
| C5 | Trust-gated supersession: lower trust cannot retire a higher-trust fact | EXP unit-level + attack set | did-not-hold | RV-SYS: sibling scopes bypass trust gating (probe trust_scope.py); the C5 `pass: true` covers whole-slot cases only |
| C6 | Prompt-cache economics of regenerating the injected block each turn | EXP measured on real calls | todo | No artifact (UNCOVERED-TOP30 #14) |
| C7 | Concurrent writers to one session log (sequence, hash chain) | EXP | answered-reviewed | RV-SYS recomputed C7 from the artifact (16 writers, gapless). Limit: no clock skew, cross-host or stale-writer case; zombie writers covered separately (RV-WP row 1) |
| C8 | Concept owners as rows + keyed job queue under load (debounce, starvation of hot concepts) | EXP | answered-owner-reported | SYS C8_concept_queue.json (a13b524) |
| C9 | Temporal on the turn path | DR for the turn path (kept off it); the engine itself is now tested under F4-F8 | answered-owner-reported | Decision record by main, not the user; engine results under F4 to F8. The row text differs between the systems worktree copy and lab main (DESIGN section 6) |
| C10 | Durable per-concept processes | DR for now: admitted only on a measured case needing a timer or long wait; session durability is tested under F4-F8 | answered-owner-reported | Decision record by main, not the user |
| C11 | Multi-tenant isolation model (index per tenant vs DLS) | EXP E9 + DR | answered-reviewed | RV-ES E9b, E9c, E9d: DLS leaks term statistics; index per tenant removes the leak. Adapter index naming reviewed (RV-AD confirm). Engine tenancy separate (K1) |

## D. Needs the user

| ID | Question | Status | Evidence |
|---|---|---|---|
| D1 | Which private sessions to include after redaction (ask w9e601030816) | needs-user | WAL w9e601030816 open; M1 |
| D2 | Pass/fail margins for K1 and K2 after the pilot shows the noise level | needs-user | Pilot noise now known (PREREG 4.3 between-draw spread); M5 |
| D3 | Real token budget for the LLM stages across all arms (the approved figure covered one arm's extraction only) | needs-user | M6 |
| D4 | Build target: free Basic tier (BM25 + own vectors + own fusion) or Enterprise (semantic_text, Elastic fusion and rerank, DLS) | needs-user | BEH E7_basic.json; RV-LIC; RV-G1. Basic as the required path is main's framing (ASSUMPTION, WAL w6ffbac116a6); M7 |
| D5 | LLM call path at volume: each headless call adds a session log directory and about 10.5k tokens of overhead (measured on one call) | needs-user | MC h3_rerun_head.json (first-turn input 10,567 tokens); long-lived sessions cost less (DESIGN 3.8.11); M11 |

## E. Elastic collaboration

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| E1 | Whether Elastic wants a design partner; private roadmap for memory validity and erasure | OPEN-BLOCKED: needs contact with Elastic, which is the user's step | needs-user | M15 |
| E2 | Q2 FY27 results on 2026-11-19 supersede the Q1 figures | re-check after that date | todo | Date not reached |

## F. Change and evolution

Requirement: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a/738faa20-cd78-4599-bbda-f9ec18226de4 "keep going and dont forget about scenarios where the task definition needs to adjust slightly without loosing progress e..." (continues: "...and when the temporal/engine/etc needs to update/ugprade/evolve"). This reopens C9 and C10: the durable session engine is in scope and gets a real testbed.

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| F1 | Task definition amended mid-session (narrowed, widened, an acceptance criterion changed, a constraint added): the amendment is an event; earlier work is kept and re-classified as still valid, needs rework, or obsolete; nothing completed is lost or silently redone | EXP (plumbing) | answered-owner-reported | SYS F1_F2_task_amend.json (a13b524) |
| F2 | After an amendment the next turn's context shows the new definition as current, the old one as superseded, and never injects a stale requirement as current; a crash at the amendment point resumes correctly | EXP + small LLM scenario set | answered-owner-reported | SYS F1_F2_task_amend.json; LLM scenario set not run |
| F3 | Amendment while work is in flight (a step running, a tool call outstanding, a fold or projection mid-way): no torn state; the in-flight step is reconciled against the new definition | EXP | answered-owner-reported | SYS F3_inflight.json: 200 runs per race; fence-off controls committed stale state 74 and 71 times (GOT CORRECTIONS) |
| F4 | Durable session engine (Temporal): a long-running session survives worker crash, restart and host move with no lost or repeated external effect | EXP testbed | in-progress | RV-ENG F4t SUPPORTED (34 worker exits in the critical windows, exact counts); F4a, F4b, F4c, F4d PARTLY; Postgres and server crashes untested (M6); client blockers H1 to H3. Owner committed fixes after the review (ffa4ce5, 05593c7; unreviewed) |
| F5 | Workflow code changes while sessions are in flight: incompatible change is caught by replay tests before deploy; compatible paths (patching, worker versioning, continue-as-new as the upgrade point) keep old sessions running | EXP testbed | answered-reviewed | RV-ENG F5a, F5b1, F5b3, F5c SUPPORTED; F5b2 PARTLY; pinned plus continue-as-new combination never run (K11) |
| F6 | Session state schema changes: migration at a continue-as-new boundary; old and new sessions coexist | EXP testbed | answered-reviewed | RV-ENG F6 SUPPORTED; rollback after migration left 4 of 4 stuck |
| F7 | Engine upgrade: Temporal server and SDK version bump under running sessions | EXP testbed | answered-owner-reported | RV-ENG F7 PARTLY: no failing control; the swap was 107 s after start, not 100 s as ENG NOTES says |
| F8 | Engine independence: a session can be rebuilt into a fresh engine namespace (or another engine) from the Postgres log alone, because the engine holds ids and cursors only | EXP drill | answered-reviewed | RV-ENG F8 SUPPORTED with limits (known session list, single tenant, old engine fully dead); "reconcile after any restart" false (RV-ENG M1, K4) |
| F9 | Elasticsearch version upgrade with existing indices; mapping change; embedding model change (reindex and alias swap under load) | EXP | answered-owner-reported | SYS F9_es_upgrade.json, F9_mapping_embedding_bluegreen.json; RV-SYS N2: after the cutover the new family sat outside erasure |
| F10 | Postgres schema change on the append-only, hash-chained log: event schema versions, upcasting old events on read, online migration under writes | EXP | answered-owner-reported | SYS F10_schema_evolution.json; RV-SYS minor: the fill-once trigger can rewrite the idempotency key; upcaster golden replays missing (K35) |
| F11 | Extraction recipe or model change with facts already derived: mixed versions never served together; cost of re-derivation bounded by the extraction cache | EXP (C1 covered the rule-based case) + measured with the real extractor | todo | SYS F11_recipe_change.json covers the rule-based case only; real extractor unmeasured |
| F12 | Agent harness and model upgrades mid-session (the coding agent itself changes version) | OPEN-BLOCKED here: belongs to the session-runtime design (see the factory-gotchas catalogue, resume section); only the memory-side contract is tested | todo | Out of this lab's scope; memory-side contract only |

## G. Works with what Elastic's customers use; cheap to develop against

Requirement: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a/bbac7e5d-b0f4-41b6-a1ea-9d5c29df9534 "i want you to build this to work with what elastic's clients likely use -- while having adapters so we can test and use" (continues: "it with our subscription based claude/codex instances for cost while we develop it").

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| G1 | What Elastic customers actually run for models, embeddings, agents and orchestration (inference API services, managed models, Agent Builder, MCP, frameworks), from primary sources | research, adversarially checked | answered-reviewed | RV-G1: Elastic managed LLMs are the default on Serverless and Elastic Cloud; Generative AI connectors deprecated for inference endpoints; Agent Builder and the inference API likely Enterprise outside Serverless (UNCONFIRMED search snippets; K32) |
| G2 | One set of ports (chat model, embeddings, rerank, search, agent tool surface, injection hook) with the core depending on no vendor | EXP: contract tests | answered-owner-reported | MC contract tests (fix/adapters-delta-16096 c20e208) |
| G3 | Customer-side adapters: Elastic inference API (chat completion, embeddings, rerank), Elastic-side fusion where licensed with client-side fallback on Basic, an MCP tool server usable from Agent Builder | EXP on the test node | answered-owner-reported | MC NOTES (observed on trial). RV-AD: the G3 artifact ran on an uncommitted tree with a 40-document toy corpus; Basic fallback tested only against a 403 stub |
| G4 | Development adapters: headless Claude Code and Codex on the user's subscriptions behind the same chat-model port, with caching, call caps and usage ledgers | EXP: same contract suite | answered-owner-reported | MC (c20e208); gate reviewed (RV-AD confirm N1, N2); call caps PARTLY (no global cap) |
| G5 | The Elastic inference path exercised end to end without paid API keys: the node's inference endpoint pointed at a local OpenAI-compatible gateway backed by the subscription instances | EXP on the test node | needs-user | Gated on G8 (WAL wc459b4a2cfc) |
| G6 | Results do not depend on which adapter is used: the eval's reader and extractor give equivalent outputs through two adapters on a sample | EXP: parity sample | did-not-hold | MC NOTES review corrections: "11 of 12 agree" used a lenient metric; raw text F1 0.35 |
| G7 | Just-in-time injection reaches each agent host: Agent Builder (pre-execution workflow), Claude Code and Codex (their hook or MCP mechanisms) | EXP per host; Serverless-only hooks OPEN-BLOCKED | in-progress | Agent Builder pre-execution observed on trial (MC NOTES-addendum-g7); fail-open template not run live, test launched (WAL); Claude Code via hook/MCP observed; Codex pool refuses MCP |
| G8 | Terms: whether subscription instances may be used as a local development backend this way | USER (main reports what the published terms say; not legal advice) | needs-user | WAL wc459b4a2cfc open; M3 |

## H. Relevance analyst cascade (asynchronous agent tier above search)

Requirement: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a (message beginning "why not use a relevance analyst cascade - where agents can run via headless interactive session harnesses with acp and secondary researchers can help additionally rerank and dig/inject context while analyzing the requirement graph and transcripts and agent instance registry"). Motivation from this lab's own measurements (corrected 2026-11-15): hybrid search finds a gold session in the top 10 for 0.968 of pilot questions on their own conversation but 0.4754 against the whole pool (CURVE; the earlier 0.46 was the superseded old-sets value); 23 of 94 pilot questions have evidence ranked worse than 100 on both legs (PREREG 3.2; the earlier 24 was the old-sets count); the cross-encoder reranker costs about 64 s per question at top 50 on CPU (rerank_timing.json; the earlier "about 26 s" has no artifact behind it).

Shape: tier 0 search (ms) -> tier 1 rerank -> tier 2 relevance analysts: long-lived headless agent sessions with pull tools (search, open, timeline, facts) that re-rank and dig, driven by the turn, the task's requirement graph and the registry of live agent sessions; what they find is delivered asynchronously to the right session's inbox as ids with provenance and trust labels, and injected at that session's next turn boundary. Never on the synchronous path of a turn.

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| H1 | Does an analyst tier recover evidence that search misses as history grows (gold in context at the +500, +3,900 and whole-pool levels; the 23 low-overlap questions), and at what cost per question (calls, tokens, seconds) | EVAL arm on the pilot | exploratory | AFULL (feat/eval-scaffold cc83d26, pilot, descriptive, no registered rule): session recall@10 over RRF +0.130 [+0.060, +0.202] at H3, +0.099 at H2, not resolvable at H1; not distinguishable from the cross-encoder rerank (n 78, rerank run partial); tools add little over reordering; the 23 low-overlap questions stay near the floor (0.072 at H3; gold among the 50 candidates for 7 of 23). Candidate order not shuffled; a tool-budget defect is disclosed. Level names corrected from "+5,000" to +3,900 |
| H2 | Precision: how often does an analyst deliver irrelevant or stale items; does injected analyst context ever make an answer worse | EVAL arm | in-progress | AFULL: at H3 with 4,000 tokens the analyst leaves only the stale value for 0.07 of knowledge-update questions (n 15); answer-level effect not measured (no reader ran) |
| H3 | Long-lived agent sessions through a session protocol (ACP) versus one process per call: overhead per request, session-log footprint, mid-session delivery of new context | EXP, after the protocol facts are verified from primary docs | todo | Session vs per-call cost measured once (MC h3_rerun_head.json; RV-AD recomputed the per-call half only); ACP not measured; Codex has no multi-turn ACP route (MC NOTES) |
| H4 | Asynchronous delivery: inbox per agent session keyed from the instance registry; exactly-once, ordered, expiring; delivered at a turn boundary; survives restart; nothing delivered to a session whose task no longer needs it | EXP (plumbing) | todo | Claim fencing reviewed (RV-AD confirm N7: 330 s renewable lease); exactly-once across pause, rollback and fork untested (UNCOVERED-TOP30 #2) |
| H5 | Routing by requirement graph: an analyst finding is routed to the sessions whose current task items it serves, and re-routed when the task is amended | EXP (builds on F1) | todo | No artifact |
| H6 | Safety: analysts read untrusted transcripts and then write into other agents' context. Findings are ids plus quotes with trust origin, never instructions; a planted instruction in a transcript must not be relayed as one | EXP attack set | todo | No artifact |
| H7 | Budget control: per-turn and per-day analyst spend caps, and a rule for when the cascade is skipped (search already confident) | EXP + measured thresholds | did-not-hold | XP E3 (exploratory): the confidence rule beats its null at H1 and H2 but not at the whole pool (AUROC 0.572 vs 0.631); no single threshold transfers. Spend caps PARTLY reviewed (RV-AD) |

## I. Currency: is the context true to the latest state of the discussion

Requirement: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a (message beginning "heres important things to think of tho: what about when requests get superseeded in plain text with decisions and discussions ... what 'proves' that the right information is updated and context is appropriate and accurate to the latest version of the discussion").

State of evidence (refreshed 2026-11-15): nothing proves it. Structured amendments work as plumbing (F1 to F3, owner-reported). Supersession stated in prose is detected with high precision but low recall (LR GATE3_RESULT, held-out: 16 of 16 links correct, 10 of 16 real updates linked, 9 of 25 pairs flip across batchings). Plain search at pool scale puts only the stale value in context for 0.467 of update questions and the current value for 0.333 (RAGCTX, H3, 4,000 tokens, n = 15). A reader shown both dated statements picks the current one (16 of 16, small set; owner-reported, PF).

Position: currency cannot rest on a detector being right. It is a property to be checked at three points: when state changes (recorded, attributable, confirmable), when context is built (freshness proven against the log tail, staleness visible), and after the fact (what version was acted on; independent audits).

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| I1 | Benchmark of plain-text supersession with gold "state after every turn": explicit reversal, implicit change, partial scope, conditional, proposal versus decision, decision reverted, change by someone without authority, change buried in a long message, offhand contradiction, restatement that is not a change, multi-party disagreement left unresolved | design reviewed adversarially, then built | todo | No artifact |
| I2 | State accuracy after each turn; stale-as-current injection rate; detection latency in turns; false-change rate; unresolved items reported as unresolved, on I1 and on the benchmark's update questions | EVAL | todo | Retrieval-only baseline exists (RAGCTX); no state-accuracy run |
| I3 | Freshness check at injection: every item presented as current carries its source event and a checked-through watermark, and a bounded scan of everything after that watermark for statements touching the item (lexical, vector, then analyst) either finds nothing or surfaces the challenger alongside; measured miss rate of the scan | EXP + EVAL | todo | Owner's I3 pass not supported (RV-SYS): 7.59 false challengers per scan, 91% of scans without a plant show one, vector leg a hash-embedding stand-in that detected 1 in 3,600 |
| I4 | Decisions are recorded, not inferred silently: a detected prose change becomes a pending update with its quotes; it becomes current only by an explicit decision event (user confirmation or a read-back that the user accepts); discussions and assistant suggestions never change state on their own | EXP (plumbing, builds on F1 and trust gating) | in-progress | Systems version not version-bound (RV-SYS N7); write-path compare-and-set holds for cases a to c (RV-WP N7) but F5 open; decision-core work launched (WAL) |
| I5 | Acted-on version: every step and every injected block records the definition version and watermark it used, so work done on a superseded version is found mechanically when a later version appears | EXP (plumbing) | todo | SYS I4_I5_decisions.json: oracle shares the fold's rule, so not independent (RV-SYS) |
| I6 | Independent audit in operation: an auditor re-derives the current state of sampled items from the raw transcript without seeing the system's state; disagreement rate is a live metric with an alarm | EVAL of the auditor against gold, then EXP | todo | No artifact |
| I7 | Real-session test: replay a real long working session whose decisions were logged with sources at the time (this lab's own session has such a ledger: asks, answers, requirements, reversals) and score "what is the current decision on X" at many points against that ledger | USER (private data; needs the user's OK) then EVAL | needs-user | WAL we04482af9e0 open; M4 |
| I8 | Read-back cost: how often the system must ask for confirmation, and whether a confirmation prompt is itself a burden or a source of error | EVAL | todo | No artifact |

## J. Parallel threads and governance (who and what may see, hold and process which data)

Requirement: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a/a1bc3afd-0f3a-48e1-bf1e-f873107b1b29 "what about when a lot of things are discussed, in parallel threads, that relate to each other.. what about access rights" (continues: boundaries and tenancy; regional provenance and sovereignty; model pin and ban requirements; local or specific inference for certain contexts; security work that needs a specially approved model or programme, for example the cyber-specialised models the user's Codex pool reserves for red-team purposes, continuing without blocking the main threads).

Position: one policy decision point, enforced at the choke points the design already has (append to the log, projection, context assembly, the model port, inbox delivery), with labels that travel with data and are inherited by everything derived from it. Proof is a decision log plus tests that plant marked data and show it never crosses a boundary.

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| J1 | Parallel related threads: events carry thread ids; items are shared across threads with typed edges (depends on, affects, conflicts with); a decision records the item version it was based on, so two threads deciding the same item concurrently produce a surfaced conflict, never last-writer-wins; a decision in one thread notifies the threads whose items it affects | EXP (plumbing, builds on F1, I4, H4) + scenario set | todo | RV-WP F5(b): decision heads are per session; tenant-wide is a product choice (M13) |
| J2 | Cross-thread context: a session working one thread gets the related threads' current decisions that bear on its items, and nothing else; measured on generated multi-thread discussions with gold "what thread B must know from thread A" | EVAL | todo | No artifact |
| J3 | Labels on every event (tenant, access list, classification, region of origin, handling rules); a derived row's access is the intersection of its inputs' and its restrictions the union; caches keyed by scope; index per tenant | EXP | in-progress | Policy decision point and label lattice committed with pre-registered thresholds (feat/policy-decisions c2153af, 4fda42f; unit tests and dry runs only); index per tenant answered (C11) |
| J4 | No confused deputy: analysts, projectors and concept jobs act with the rights of the principal they serve; an inbox item is delivered only to a session entitled to every input behind it | EXP + leak probe set | in-progress | Policy-point work launched (WAL) |
| J5 | Residency: storage placement by region for log, indexes, keys and caches; derived data inherits the strictest region rule; cross-region retrieval only where policy allows; a placement audit that lists where each subject's bytes are | EXP with region-tagged stub stores; real multi-region OPEN-BLOCKED without more infrastructure | todo | No artifact |
| J6 | Model policy at the model port: allowed, pinned and banned models, providers, regions and versions per tenant and classification; every call checked before send and logged; pin drift detected (the endpoint reports a different model or version than pinned; end-of-life dates) | EXP (contract tests) | todo | FAB rule R23 (no model names in source) is compatible |
| J7 | Required inference class: some classifications may only be processed by local or named endpoints. Context assembly either routes the whole turn to a compliant model or withholds the restricted items with a visible placeholder and delegates the part that needs them to a compliant model; outputs keep the labels of what they were computed from unless a declassification rule says otherwise | EXP + scenario set | todo | No artifact |
| J8 | Egress proof: marked canary data of each label, planted in the log, never appears in any prompt, tool result, cache entry, index or log outside its boundary, across all adapters; the send-time guard generalised from id patterns to labels | EXP attack and leak set with an absolute bar | in-progress | Id-pattern guard reviewed with residuals (RV-AD confirm N4); label generalisation part of the policy-point work (WAL) |
| J9 | Restricted-capability side tasks: when a step needs a model or programme that only approved, scoped work may use (for example security analysis under a recorded authorisation), the step is delegated to that channel as a side task with its authorisation and scope attached; the main thread continues on other items and receives the result through the inbox under the result's own labels. A refusal or a policy denial is never retried on another model without such a recorded, authorised route | EXP (plumbing, stub channels) + policy tests | todo | No artifact |
| J10 | When a model is banned or a policy tightens after the fact: everything derived by that model or under the old policy is found from provenance and invalidated or rebuilt | EXP (builds on C1, F11) | todo | No artifact |
| J11 | Which concrete policies apply (regions, classifications, approved models, security programmes) | USER or customer; the system is policy-driven and ships with none assumed | needs-user | M9 |

## K. Questions raised by the reviews (added 2026-11-15)

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| K1 | Engine tenant isolation: workflow ids and keys carry the tenant, so two tenants with one session id never collide | EXP + delta review | in-progress | RV-ENG H1 (client blocker); owner fix ffa4ce5 unreviewed |
| K2 | Engine approvals are bound to the request they approve; two approval gates in one session cannot cross | EXP + delta review | in-progress | RV-ENG H2; owner fix ffa4ce5 unreviewed |
| K3 | Engine doorbell signals are validated (a signal carrying another session's id is refused) | EXP + delta review | in-progress | RV-ENG H3; owner fix ffa4ce5 unreviewed |
| K4 | Inputs appended to an idle live session are processed without a signal (reconcile is not only at run start) | EXP | todo | RV-ENG M1: "reconcile after any restart" is false |
| K5 | A granted approval resolves promptly after a rebuild instead of waiting for its deadline | EXP | todo | RV-ENG M2 |
| K6 | Rollup keys derive from stable identity, not a counter | EXP | todo | RV-ENG M3 (artifact shows it) |
| K7 | A poison input (including a crypto-erased one) goes to a dead letter instead of wedging the session | EXP | todo | RV-ENG M4 |
| K8 | Input append is idempotent on a retried API call | EXP | todo | RV-ENG M5 |
| K9 | Engine behaviour under Postgres failure and restart, server crash with activities in flight, two workers on one queue | EXP | todo | RV-ENG M6, F4 limits; only the SQLite dev server used |
| K10 | Exactly-once holds when the external system's key retention is bounded | EXP + DR | todo | RV-ENG M7 |
| K11 | Pinned worker deployments plus a continue-as-new hop, run together | EXP | in-progress | RV-ENG "recommended combination" UNSUPPORTED; owner commit 492f900 adds the F5b4 case (no result read, unreviewed) |
| K12 | Engine results carry their git SHA and times, and NOTES matches the files | EXP rerun | todo | RV-ENG P1, P4 (200/202, 19/18, 15/13, 100/107) |
| K13 | Erasure verify works for a subject with more than 65,536 events; one failing job does not stop the sweeper | EXP | in-progress | RV-WP F1 (HIGH); write-path owner reproduced F1 to F7 on aaa0fec (e5f6aca, owner-reported); fixes not yet committed |
| K14 | Erasure completes when a tenant rebuild outlasts the 120 s lease | EXP | in-progress | RV-WP F2 (HIGH; model with an ASSUMPTION rate) |
| K15 | The fence binds the scope that owns the table, and DELETE and TRUNCATE are fenced; the app role does not own its tables | EXP | in-progress | RV-WP F3 |
| K16 | The erasure completion guard computes residue itself rather than trusting a caller-supplied JSON | EXP | in-progress | RV-WP F4 |
| K17 | Decisions are bound to the approval event; idempotency keys detect reuse with different content | EXP | in-progress | RV-WP F5 |
| K18 | Takeover cannot wait forever on a stalled zombie (lock and idle timeouts) | EXP | in-progress | RV-WP F6 |
| K19 | `_aliases` responses with `errors: true` are raised, not ignored | EXP | in-progress | RV-WP F7 |
| K20 | A long transaction anywhere in the Postgres cluster cannot stall projection for every tenant (xmin stall) | EXP | todo | RV-WP row 23 limit; UNCOVERED-TOP30 #23 |
| K21 | Erasure scope: speaker provenance only, or also statements about the person | USER | needs-user | RV-WP F8, owner question 7; WAL wc4b140b9a4f; M2 |
| K22 | Production key store (KMS with real deletion) and a backup-retention bound in the erasure receipt | USER or client | needs-user | RV-WP owner question 6; M12 |
| K23 | Whether the tenant HMAC may survive a subject's erasure (slot hashes stay confirmable) | USER | needs-user | RV-WP F8; M2 |
| K24 | Legal hold versus erasure: precedence and audit trail | USER or client | needs-user | RV-WP owner question 10; GOT DESIGN-CHANGES; M8 |
| K25 | Adapter residuals: runtime class patching past the gate; homoglyph, encoding and separator gaps in the egress guard; real-id matching off by default; renew returns 0 on a lapsed lease while confirm returns 1; token-less confirm is silent; kNN query without a tenant term | EXP | in-progress | RV-AD confirm (low residuals; the item-7 test is vacuous); owner fixes 515cebf (inbox renew and token) and 611d21c (embedder and reranker guards and budgets), unreviewed |
| K26 | Kibana fail-open pre-execution template, run live (normal, refused port, past-timeout) | EXP | in-progress | MC NOTES-addendum-g7 "not run live"; launch recorded in WAL; template must use the randomised framing (RV-AD) |
| K27 | Every retrieval figure re-run end to end on Elasticsearch before it is quoted for the client | EVAL | todo | All recall figures come from the exact retriever (PREREG section 9; A11 partial) |
| K28 | Pooled rerank on the current sets (top 20) | EVAL | in-progress | AFULL: 78 of 94 questions done when scored; rerank beats RRF at H3 by +0.059 [+0.016, +0.113] on those 78 |
| K29 | Does the benchmark carry a length shortcut that inflates retrieval scores | EVAL probe | exploratory | XP E2: session length alone AUROC 0.687 vs null 0.448 to 0.547 |
| K30 | Do pilot-chosen thresholds (4,000-token budget, rerank top 20, low-overlap cut, confidence cut) transfer to the test split and other pool sizes | EVAL | did-not-hold | XP E3 (exploratory): no single cosine threshold transfers across pool sizes; the others untested; LEDGER critic correction |
| K31 | Trial expiry (lab trial ends 2026-12-15): each licensed feature has a declared degrade mode, and isolation fails closed | EXP rehearsal | todo | GOT NEW-GOTCHAS MEM-14; UNCOVERED-TOP30 #21 |
| K32 | Do the inference API and Agent Builder work on Basic outside Serverless (exact errors) | EXP | todo | RV-G1 and RV-LIC: likely Enterprise (UNCONFIRMED); BEH E7_basic: ES|QL RERANK `[inference]` 403 on Basic |
| K33 | Can this system satisfy agent-fabric's `memory` provider kind and its conformance suite, given fabric's append-only triggers, Go kernel and lack of tenancy | DR + EXP | todo | FAB (ADR 0002; F14 planned, not built) |
| K34 | Integrity failures (broken chain, missing key) are distinguishable from legitimate erasure | EXP | todo | GOT DESIGN-CHANGES |
| K35 | Upcaster correctness: old-event golden replays, since the chain covers stored bytes | EXP | todo | GOT DESIGN-CHANGES "still missing" |
| K36 | Erasure amplification: cost of erasure at scale (40 erasures took 254 s) | EXP measured under load | todo | GOT DESIGN-CHANGES "still missing" |
| K37 | Plaintext task state (task_view) brought under the subject key and the erasure cascade | EXP | todo | RV-SYS N6 |
| K38 | Status drift: registers and ledgers updated from evidence, not left at todo while results exist | process check | todo | GOT CORRECTIONS; RV-SRC critic "missed" list |
| K39 | Model-graded gold (extraction reference, judge) validated by a person on a sample | EVAL | todo | LEDGER critic correction; GOT DESIGN-CHANGES "still missing" |
| K40 | Crash coverage beyond instrumented points, and blast radius on the shared lab host | EXP | todo | GOT DESIGN-CHANGES "still missing" |

## L. Questions from outside sources (added 2026-11-15)

Source: RV-SRC (five sources; readers worked from page summaries) and LEDGER "Outside ideas".

| ID | Question | Exit | Status | Evidence |
|---|---|---|---|---|
| L-X04 | Every memory write cites session, round and a verbatim quote; a host check verifies quote and speaker | EXP | answered-owner-reported | PF PROVENANCE_RESULT.md: 2,351 of 2,373 (99.1%); 8 speaker mismatches, 53 ambiguous; controls rejected |
| L-X02 | Two-stage retrieval (sessions first, then rounds) | EVAL exploratory | did-not-hold | XP E2 (exploratory): -0.019 at S = 20, 0.000 at S = 50; planted-gold control missed its bar |
| L-X05 | Skip injection and analysts when retrieval confidence is high; abstain below a calibrated score | EVAL exploratory | did-not-hold | XP E3: holds at H1 and H2, not at the whole pool |
| L-X01 | Pooled token-budget curve | EVAL descriptive | exploratory | XP x01_budget_presence |
| L-X24 | Injection and trimming keep tool-call / tool-result pairing valid | EXP | todo | LEDGER queued; RV-SRC critic: tool-pair API shape |
| L-X23 | A memory append never fails silently | EXP fault injection | todo | LEDGER queued |
| L-X20 | Policy re-evaluated at delivery, not only at enqueue | EXP | todo | LEDGER queued |
| L-X18 | Identity and tenant attributes cannot be overwritten by derived attributes; erroring evaluation denies | EXP | todo | Blocked on the policy point existing (J3) |
| L-X07 | Analyst findings in a fixed schema with an explicit "nothing relevant" outcome | EXP | todo | LEDGER queued; RV-SRC critic |
| L-X13 | Several critics before a finding becomes durable (aimed at false challengers in the freshness scan) | EVAL | todo | Needs model calls |
| L-X06 | Prune retrieved text with a small model | EVAL | todo | Needs model calls |
| L-X15 | Pipeline stages instead of barriers | EXP | todo | Needs a quiet host (RV-SRC critic) |
| L-X30 | Planted instructions in transcripts are never relayed by analysts | EXP attack set | todo | Same as H6 |
| L-X09 | Agent chooses when to compact | DR | answered-owner-reported | Not adopted: not proven in the source and out of reach for host sessions (LEDGER) |
| L-X10 | Post-train a small model to use memory tools | DR | answered-owner-reported | Not adopted: the target is the client's own models through one port (LEDGER) |
| L-K1 | Plain-Markdown tree as a second projection and analyst surface (kepano) | EXP | todo | LEDGER queued |
| L-K2 | Links written at extraction; dangling links as a work queue (kepano) | EXP | todo | LEDGER queued |
| L-K3 | Layered consolidation with random-sample audit (kepano) | EXP | todo | LEDGER queued |
| L-K4 | Decisions register separating user words from agent assumptions (harness knowledge bundle) | EVAL | todo | LEDGER: test questions drafted |

## M. Questions only the user can answer

Each item gives the options and a recommended safe default. The defaults are recommendations from this register, not the user's decisions; none is in force until the user answers.

| # | Question (rows) | Options | Recommended safe default (RECOMMENDATION) |
|---|---|---|---|
| M1 | Which private sessions to include after redaction (D1, B12; WAL w9e601030816) | (a) none; (b) a named small set of the user's own sessions, redacted and kept local; (c) a wider set | (b) a small named set chosen by the user, redacted, processed only locally, never sent to outside reviewers |
| M2 | Erasure scope (K21, K23; WAL wc4b140b9a4f) | (a) speaker provenance only; (b) also statements about the person by others; (c) (b) plus destroying the tenant HMAC linkage | (b), with the HMAC question settled by the client's legal requirement; until then treat erasure as speaker-only and say so in every receipt |
| M3 | Subscription CLIs behind the Elasticsearch inference API (G8, G5; WAL wc459b4a2cfc) | (a) yes, local development only; (b) no, use direct adapters only; (c) wait for a terms reading | (c) then (b): keep subscription CLIs as direct development adapters, outside any client path, until the terms are read |
| M4 | Use this session's own transcript and decision ledger for the real-session test (I7; WAL we04482af9e0) | (a) yes, local only; (b) yes, redacted; (c) no | (b) redacted and local only; no external reviewer sees it |
| M5 | Pass/fail margins for K1 and K2 (D2) | margins set now from the pilot's between-draw spread, or after more pilot data | Set them before the next run from the measured spread, and commit them first |
| M6 | Token budget for LLM stages across all arms (D3) | a fixed cap per arm; a total cap; stage-by-stage approval | A total cap with per-stage approval, so each model-calling stage starts only after the user sees its estimate |
| M7 | Build target tier (D4) | (a) Basic required, Enterprise optional; (b) Enterprise required; (c) Serverless | (a) is main's framing (ASSUMPTION). Recommendation: confirm (a), with Agent Builder and the inference API marked Enterprise-only until K32 shows otherwise |
| M8 | Legal hold versus erasure (K24) | hold wins with an audit trail; erasure wins; per-tenant policy | Hold wins over erasure only under a recorded hold, with the erasure queued and the delay in the receipt; the client confirms |
| M9 | Concrete policies (J11) | client supplies; ship examples | Ship with none active; the client supplies them; tests use synthetic labels |
| M10 | Resume fact extraction (B4; follow-up to WAL w11160e65223) | resume at volume (about 5,100 calls); a small sample first; keep holding | A small sample first under M6's budget, then volume only after the analyst and search arms report |
| M11 | LLM call path at volume (D5) | per-call headless; long-lived sessions; client API | Long-lived sessions for development (cheaper, DESIGN 3.8.11); the client's own providers for anything client-facing |
| M12 | Production key store and backup-retention bound (K22) | client KMS; managed cloud KMS; lab stand-in | The client's KMS with real key deletion, and the backup retention stated in every erasure receipt |
| M13 | Decision heads per session or tenant-wide (J1; RV-WP F5(b)) | per session; tenant-wide | Tenant-wide, since parallel threads deciding one item must conflict visibly (J1) |
| M14 | Serverless project for A12 | create one; skip Serverless | Skip until the client asks for Serverless; report A12 as open |
| M15 | Contact with Elastic (E1) | contact now; later; never | Later, after the design has reviewed results to show |
| M16 | Client-first standalone build target, with agent-fabric as an adapter (WAL w6ffbac116a6, ASSUMPTION by main) | confirm; make fabric the host | Confirm the standalone core; fabric integration stays a later adapter (K33) |
