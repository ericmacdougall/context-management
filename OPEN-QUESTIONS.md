# Open questions register

Ticket: GoodQA goodqa#16096. Requirement: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a/760f5af3-ed9d-4f90-95c3-4e708e139b4e "keep going until you have a proven design / all experiments and questions are resolved so we can build it for real".

"Proven" here means: every row below has reached its exit. A row can exit only one way:

- EXP: settled by a node or systems experiment with a REALLY_TESTED entry.
- EVAL: settled by the pre-registered eval.
- USER: decided by the user in their own words.
- DR: closed by a decision record (the design avoids the question; say why).
- OPEN-BLOCKED: cannot be settled under current approvals; reported as open, never as proven.

Status values: todo, running, done (with evidence id), blocked.

## A. Elastic behaviour (stream: behaviour, node emlab-es-16096 on t2-dev)

| ID | Question | Exit | Status |
|---|---|---|---|
| A1 | Search visibility delay after a write; GET by id realtime; wait_for and the 1000-listener limit | EXP E1 | running |
| A2 | Optimistic concurrency gives one winner; _bulk has no cross-item atomicity | EXP E2 | running |
| A3 | Data stream as idempotent event mirror (create + fixed id; backfill placement; no direct update/delete) | EXP E3 | running |
| A4 | Validity intervals: date_range vs two date fields; as-of, currently-valid, sort | EXP E4 | running |
| A5 | Percolator: lexical triggers at 1k/10k/100k stored queries; knn/sparse/semantic stored queries | EXP E5 | running |
| A6 | Filtered kNN recall and latency (bbq_hnsw, int8_hnsw; acorn vs fanout); bbq_disk licence failure point | EXP E6 | running |
| A7 | Which fusion and rerank features work on Basic vs trial, with exact errors | EXP E7 | running |
| A8 | semantic_text: which update paths re-run inference | EXP E8 | running |
| A9 | Document-level security: hit isolation, score/aggregation leakage, kNN and semantic coverage | EXP E9 | running |
| A10 | Deleted content persistence on disk before and after merge | EXP E10 | running |
| A11 | Parity: client-side fusion vs Elastic rrf/linear; external E5 vectors vs built-in E5 via inference API, same pilot queries | EXP E11 | todo (after node handoff) |
| A12 | Serverless behaviour (5 s refresh minimum, realtime GET, no force merge, no snapshots) | OPEN-BLOCKED: needs a cloud project | blocked |
| A13 | Disk-based vector index recall under heavy filtering at real scale | OPEN-BLOCKED partly: Enterprise feature, trial only, synthetic vectors | todo |

## B. Does the memory design work (stream: eval)

| ID | Question | Exit | Status |
|---|---|---|---|
| B1 | Can this benchmark decide anything about massive context? Reader with gold evidence vs reader with full history on ~10 pilot questions | EVAL micro-pilot | todo |
| B2 | Retrieval recall by method (BM25, dense, hybrid, rerank) on Elastic | EVAL Phase R | running (embedding) |
| B3 | Size of the low-overlap stratum; is it large enough to test write-time cue keys | EVAL Phase R pilot | todo |
| B4 | K1: facts with validity + hybrid retrieval beat the best baseline on update and temporal questions | EVAL Phase F | todo |
| B5 | K2: concept docs beat facts + retrieval | EVAL Phase F, only if K1 passes | todo |
| B6 | Fact extraction quality on its own: supersession precision and recall against gold | EVAL Phase F | todo |
| B7 | Abstention: closed-world check vs retrieval-only | EVAL Phase F | todo |
| B8 | Write-time cue keys recover low-overlap facts | EVAL Phase F ablation | todo |
| B9 | Accuracy as history grows (S vs M variant or a length curve) | EVAL, after B1 | todo |
| B10 | As-of time filtering works on histories with supersession more than a day apart | EVAL on M variant or user sessions; S cannot test it (measured) | todo |
| B11 | Judge validity: leniency on deliberately wrong answers | EVAL pre-test probe | todo |
| B12 | Multi-year, 10M+ token scale | OPEN-BLOCKED: no such public data; partially via user sessions | blocked |

## C. Systems properties of the design (stream: systems, not started)

| ID | Question | Exit | Status |
|---|---|---|---|
| C1 | Rebuild: all Elastic projections reproducible from the Postgres log; cached replay exact, uncached within a measured variance band | EXP | todo |
| C2 | Projection lag: time from append to retrievable; tail-scan fallback covers the gap | EXP | todo |
| C3 | Erasure cascade: key destruction plus derived-row purge via input-set provenance; tombstone racing an in-flight projection | EXP | todo |
| C4 | Stored prompt injection: untrusted text re-injected later does not gain authority (trust origin, nonce envelope, tool gateway) | EXP attack set, absolute bar | todo |
| C5 | Trust-gated supersession: lower trust cannot retire a higher-trust fact | EXP unit-level + attack set | todo |
| C6 | Prompt-cache economics of regenerating the injected block each turn | EXP measured on real calls | todo |
| C7 | Concurrent writers to one session log (sequence, hash chain) | EXP | todo |
| C8 | Concept owners as rows + keyed job queue under load (debounce, starvation of hot concepts) | EXP | todo |
| C9 | Temporal on the turn path | DR for the turn path (kept off it); the engine itself is now tested under F4-F8 | reopened as F4-F8 |
| C10 | Durable per-concept processes | DR for now: admitted only on a measured case needing a timer or long wait; session durability is tested under F4-F8 | done (decision) |
| C11 | Multi-tenant isolation model (index per tenant vs DLS) | EXP E9 + DR | running |

## D. Needs the user

| ID | Question | Status |
|---|---|---|
| D1 | Which private sessions to include after redaction (ask w9e601030816) | open |
| D2 | Pass/fail margins for K1 and K2 after the pilot shows the noise level | open (after pilot) |
| D3 | Real token budget for the LLM stages across all arms (the approved figure covered one arm's extraction only) | open (after micro-pilot) |
| D4 | Build target: free Basic tier (BM25 + own vectors + own fusion) or Enterprise (semantic_text, Elastic fusion and rerank, DLS) | open |
| D5 | LLM call path at volume: each headless call adds a session log directory and about 10.5k tokens of overhead (measured on one call) | open |

## E. Elastic collaboration

| ID | Question | Exit | Status |
|---|---|---|---|
| E1 | Whether Elastic wants a design partner; private roadmap for memory validity and erasure | OPEN-BLOCKED: needs contact with Elastic, which is the user's step | blocked |
| E2 | Q2 FY27 results on 2026-11-19 supersede the Q1 figures | re-check after that date | todo |

## F. Change and evolution

Requirement: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a/738faa20-cd78-4599-bbda-f9ec18226de4 "keep going and dont forget about scenarios where the task definition needs to adjust slightly without loosing progress e..." (continues: "...and when the temporal/engine/etc needs to update/ugprade/evolve"). This reopens C9 and C10: the durable session engine is in scope and gets a real testbed.

| ID | Question | Exit | Status |
|---|---|---|---|
| F1 | Task definition amended mid-session (narrowed, widened, an acceptance criterion changed, a constraint added): the amendment is an event; earlier work is kept and re-classified as still valid, needs rework, or obsolete; nothing completed is lost or silently redone | EXP (plumbing) | todo |
| F2 | After an amendment the next turn's context shows the new definition as current, the old one as superseded, and never injects a stale requirement as current; a crash at the amendment point resumes correctly | EXP + small LLM scenario set | todo |
| F3 | Amendment while work is in flight (a step running, a tool call outstanding, a fold or projection mid-way): no torn state; the in-flight step is reconciled against the new definition | EXP | todo |
| F4 | Durable session engine (Temporal): a long-running session survives worker crash, restart and host move with no lost or repeated external effect | EXP testbed | todo |
| F5 | Workflow code changes while sessions are in flight: incompatible change is caught by replay tests before deploy; compatible paths (patching, worker versioning, continue-as-new as the upgrade point) keep old sessions running | EXP testbed | todo |
| F6 | Session state schema changes: migration at a continue-as-new boundary; old and new sessions coexist | EXP testbed | todo |
| F7 | Engine upgrade: Temporal server and SDK version bump under running sessions | EXP testbed | todo |
| F8 | Engine independence: a session can be rebuilt into a fresh engine namespace (or another engine) from the Postgres log alone, because the engine holds ids and cursors only | EXP drill | todo |
| F9 | Elasticsearch version upgrade with existing indices; mapping change; embedding model change (reindex and alias swap under load) | EXP | todo |
| F10 | Postgres schema change on the append-only, hash-chained log: event schema versions, upcasting old events on read, online migration under writes | EXP | todo |
| F11 | Extraction recipe or model change with facts already derived: mixed versions never served together; cost of re-derivation bounded by the extraction cache | EXP (C1 covered the rule-based case) + measured with the real extractor | todo |
| F12 | Agent harness and model upgrades mid-session (the coding agent itself changes version) | OPEN-BLOCKED here: belongs to the session-runtime design (see the factory-gotchas catalogue, resume section); only the memory-side contract is tested | blocked |
