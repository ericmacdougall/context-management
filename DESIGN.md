# Elastic memory lab: design under test

Ticket: GoodQA goodqa#16096. Status of every number here: unmeasured hypothesis until an experiment in this repo reports it.

## Architecture being tested

- Source of truth: append-only event log (Postgres). Elastic holds rebuildable projections only.
- Facts: extracted per event, with valid_from / valid_to, modality (asserted, decided, proposed, hypothetical), scope, author, trust origin. Lower trust never supersedes higher.
- Concept state: rows regenerated from facts plus new events, never from the previous summary. No durable process per concept.
- Retrieval (Elastic 9.5.x): BM25 + dense (+ sparse where licensed), fused (RRF or linear), optional rerank. Concept links boost, never filter.
- Write-time cue keys: each fact carries anticipated-use keys so facts with no overlap with the later question can still be found.
- Injection: budgeted block regenerated each turn at the prompt tail; only ids are logged.
- Pull tools for the model: recall(query, as_of), open_event(id), query_facts(subject, predicate, as_of), timeline(entity).
- Temporal: off the turn path; rebuilds and timer owners only. Not exercised by the first slice.

## Kill rules (pre-register thresholds before any run)

- K1: time-aware facts + hybrid retrieval must beat the best tuned baseline on knowledge-update and temporal questions, or the facts layer goes.
- K2: concept docs must beat facts + retrieval by the pre-registered margin, or they are not built.
- The first slice cannot test durable owner processes. Retrieval-only numbers decide neither rule (both need extraction and answers).

## Eval requirements from the design review

- Baselines, same reader model, token budget and pull tools: (B1) tuned time-aware hybrid RAG with session/round keys, (B2) long context with compaction, (B3) dated always-present observation log, (B4) grep/open-by-id agent over raw history, (B5) Elastic's Atlas-style three-index memory.
- Gold labels at retrieval level, separate from answer correctness. Stale-only retrieval of a superseded value is a miss.
- Strata: low-overlap (gold ranks below ~100 on both BM25 and dense for the question text), knowledge-update, temporal, multi-session, abstention / false premise, verbatim assistant-said.
- Statistics: Wilson interval for single proportions; paired, conversation-clustered bootstrap for differences between arms. Effective n is the number of independent conversations. Pilot set disjoint from test set. Thresholds fixed and committed before the test run.
- Same embedding model in every arm. Vectors computed outside Elastic are a labelled deviation from the semantic_text path.
- Report every figure with workload, hardware, baseline, target, result.

## Constraints

- Public benchmark data only (ASSUMPTION, not the user's words). No private session data.
- No secrets in commands, env vars, logs or commits. No cloud projects or accounts. No contact with Elastic.
- Heavy runs on t2-dev (`ssh -F ~/.ssh/config_fleetops t2-dev`), which also hosts GoodQA staging and is at 93% disk: cap CPU, memory and disk, bind localhost only.
- Local git repo, unpublished. Commit each green step with the commit policy format.
