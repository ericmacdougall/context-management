# Ideas ledger: what was tried, what held, what did not

Ticket: GoodQA goodqa#16096. One row per idea or mechanism. An idea enters the design only from the "Held" table,
and only at the evidence class shown. Nothing leaves this file: a failed idea stays, with the reason.

Evidence classes: **reviewed** = an independent reviewer confirmed the result from artifacts;
**owner-reported** = the building agent reported it, no independent confirmation; **observed** = a one-off
observation with no control; **overturned** = an earlier pass was withdrawn after review.

## Held

| Idea | Result | Class | Where |
|---|---|---|---|
| Postgres log first, Elasticsearch as rebuildable projection | Engine discarded and rebuilt from Postgres alone; 1,440 turns exactly once | owner-reported, under review | engine F8, feat/engine-evolution 62916ae |
| Input is a log event first; the workflow signal is only a doorbell | Grants that existed only as signals were lost 6 of 6; with events, 0 lost | owner-reported, under review | engine F8 |
| Idempotency keys from stable input identity, never from attempt number | Attempt-derived keys gave 54 duplicate effects and 825 results for 800 inputs | owner-reported, under review | engine F4a targeted |
| Replay captured histories before every deploy | Incompatible build failed 20 of 20 replays; deployed anyway, all sessions froze | owner-reported, under review | engine F5a |
| Index chosen by an immutable field (not a rolled alias or data stream) | Retry-safe; data streams duplicated ids after rollover | owner-reported | feat/elastic-behaviour 5229e75 |
| Exact rescoring on approximate vector search (oversample 3) | Needed to match exact recall | owner-reported | feat/eval-scaffold |
| Opaque session ids in evaluation | Gold ids all began `answer_`; re-test with opaque ids showed no change | owner-reported | feat/phasef-harness |
| One fact extraction call per session | 9.0 facts per session at 1 per call | owner-reported | feat/phasef-harness |
| Agent Builder pre-execution workflow as the injection point | Rewritten prompt reaches the model as the one user message | observed (Kibana 9.5.6) | feat/ports-adapters d8f714b |

## Did not hold

| Idea | What happened | Class | Consequence for the design |
|---|---|---|---|
| Hybrid retrieval alone as the memory | Session recall@10 0.97 with no distractors, 0.48 at the whole pool; evidence in a 4,000-token context 0.41 | owner-reported | Retrieval is one input, not the answer source; current-state table and analysts needed |
| Batched fact extraction to save calls | 9.0 facts per session at 1 per call, 5.1 at 4, 4.5 at 8, 2.7 at 16 | owner-reported | No batching above 1 on the truth path |
| Automatic "this replaces that" links as a filter | Precise 16/16 but recall 10/16, unstable across batching | owner-reported | Supersession labels are advisory, never filtering |
| Fence as a lock plus an alias chosen at read time | Failed two independent reviews; main race run recorded a fail | overturned | Epoch carried in the write, enforced by the store; rebuild under way |
| Verification as an optional step, reader filters callable off | Job completion ignored verify; filter could be switched off | overturned | Verify result required to complete; filters not caller-controlled |
| Bulk partial update on unrefreshed `semantic_text` | Embeddings silently dropped | owner-reported | Full-document writes only on that field type |
| Document-level security as tenant isolation | Term statistics leak across the filter | owner-reported | One index family per tenant |
| Deleting text and assuming it is gone | Purged only after retention-lease sync (about 30 s) plus force merge | owner-reported | Erasure verified by planted markers after merge |
| Vector search inside the percolator | Rejected by Elasticsearch | observed | Standing queries are lexical only |
| One fresh headless call per step | About 10.5k tokens overhead each; 20 calls cost 6.5 times one 20-turn session | owner-reported | Long-lived sessions where contamination allows |
| Temporal patching as the upgrade mechanism for long sessions | Old runs adopt the patch only after continue-as-new; removing a patch early strands idle sessions | owner-reported, under review | Hotfix backstop only |
| Rolling back code after a state-shape migration | 4 of 4 sessions stuck | owner-reported, under review | Ship the reader of the next shape before the writer |
| Agent Builder hook failing open by itself | Error or 120 s timeout returns HTTP 500, no fallback | observed | Fail-open wrapper inside the workflow |
| Adapter and write-path passes reported by their owners | 12 material findings each on first independent review | overturned | No pass counts until reviewed |

## Outside ideas (not yet tested here)

| Source | Idea | Status |
|---|---|---|
| kepano (vault, file-over-app) | Plain-Markdown tree as a second projection and analyst surface | queued |
| kepano | Links written at extraction, dangling links as a work queue | queued |
| kepano | Layered consolidation with random-sample audit | queued |
| Harness knowledge bundle | Decisions register separating user words from agent assumptions | queued; test questions drafted |
| X posts, arXiv 2607.23809, mrinalwadhwa | being read | pending |
