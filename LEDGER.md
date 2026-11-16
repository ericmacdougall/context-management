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

| Tenant boundary in the adapters (index naming, wildcard and encoded names refused, tenant term forced) | 100 names over 2 prefixes, 0 collisions; original reproducers refused | reviewed (local probes) | fix/adapters-delta-16096 c20e208 |
| Subscription gate: exact-type allow-list, read-only backend copy, gate re-run per request | Forwarder, subclass, instance-attribute and class-reassignment attacks refused; post-construction backend gets 404 or 503 | reviewed (local probes) | c20e208 |
| Inbox claim with fencing token and 330 s renewable lease | Second turn at +40 s and +300 s gets nothing on both stores; forged token and superseded claimant confirm 0 | reviewed (local probes) | c20e208 |
| Session-id egress guard on every outbound path, failing closed without its id list | 8 variants and all container types blocked; missing id file refuses everything | reviewed (local probes); pattern gaps remain (homoglyphs, encodings) and real ids are off by default | c20e208 |
| Memory block always well-formed or empty | 1,716 renders across budgets, 0 malformed | reviewed (local probes) | c20e208 |

| Analyst reordering of 50 retrieved candidates versus plain hybrid retrieval | Session recall@10, 94 pilot questions, failures counted: +500 distractors 0.894 vs 0.863 (not resolvable); +3,900 0.758 vs 0.660 (+0.099, CI 0.023 to 0.175); whole pool 0.603 vs 0.473 (+0.130, CI 0.060 to 0.202). Gold rounds in a 4,000-token context at whole pool 0.541 vs 0.332, using fewer tokens. Knowledge-update questions (n 15) at whole pool: latest value present 0.93 vs 0.27 | pilot observation, owner-reported; not confirmatory; analysts saw candidates in rank order; exact in-process retriever | feat/eval-scaffold cc83d26, eval/results/pilot/analyst_full.json |
| Cross-encoder rerank as the cheap first step | Not distinguishable from the analyst on session recall@10 at any pool size (n 78; rerank run incomplete). Knowledge-update latest value present 0.80 vs 0.27 for plain retrieval | pilot observation, owner-reported | same |

## Did not hold

| Idea | What happened | Class | Consequence for the design |
|---|---|---|---|
| Hybrid retrieval alone as the memory | Session recall@10 0.968 with no distractors, 0.4754 at the whole pool (curve_pilot.json); evidence in a 4,000-token context 0.41 | owner-reported | Retrieval is one input, not the answer source; current-state table and analysts needed |
| Batched fact extraction to save calls | 9.0 facts per session at 1 per call, 5.1 at 4, 4.5 at 8, 2.7 at 16 | owner-reported | No batching above 1 on the truth path |
| Automatic "this replaces that" links as a filter | Precise 16/16 but recall 10/16, unstable across batching | owner-reported | Supersession labels are advisory, never filtering |
| Analyst over cross-encoder rerank on which sessions are found | No resolvable gain at any pool size (+0.015, +0.043, +0.024, all intervals include 0); the analyst only adds more gold rounds in context at the two smaller pools | pilot observation | An analyst call (63k tokens, 13.6 s) is not justified for reordering alone; rerank first, analyst only where digging is needed |
| Cheap first-stage variants to find the hard questions' sessions (exploratory, pilot, exact retriever, feat/eval-scaffold 79e0564) | None held on the 23 hard questions at whole pool against flat 0.118: session-sum pooling 0.051 (worse, CI -0.135 to -0.009), rare-term boost 0.104, pseudo-relevance feedback 0.124 (+0.006). The wrong-query control was 0.000 for variant and baseline alike, so it was uninformative | exploratory | Lexical and pooling tweaks do not fix candidate generation |
| Shallow candidate lists for hard questions | Gold is in the flat candidate list for the 23 hard questions at 30% (50 rounds), 39% (100), 48% (200), 65% (400), 70% (1,000); all 94: 0.79, 0.84, 0.88, 0.92, 0.93 | exploratory | A first stage must go about 400 rounds deep before reordering can help two thirds of hard questions; 30% stay unreachable even at 1,000 |
| Cross-encoder gain over plain retrieval once session length is controlled | Unmatched whole pool 0.528 vs 0.475 (94 questions, run complete); length-matched difference +0.020 (CI -0.004 to +0.049), no longer resolvable. Analyst gain shrinks from +0.130 to +0.098 (CI 0.041 to 0.157) but survives. Size-matched random control not yet run | exploratory | Part of the reranker's apparent gain is the benchmark's length artefact; the analyst's is not |
| Analyst tools (search, open, timeline) versus the same analyst without tools | No resolvable gain on session recall@10 at any level; analysts used no tool in about half of questions; one resolvable gain only on the 23 hard questions at +3,900 | pilot observation | Tool use must be prompted or forced when candidates look weak; current prompt leaves it idle |
| Any reordering arm on the 23 hardest questions at whole pool | All near the floor (0.032 to 0.093); the gold session is among the 50 candidates for only 7 of 23 | pilot observation | The bottleneck is candidate generation, not ordering; needs wider or different first-stage recall |
| Rebuilt write path as fit for a client deployment | Second-generation fence holds against an honest stale writer, but independent review blocks: erasure verify breaks above 65,536 events per subject, erasure livelocks on large tenants, fence trusts callers, done-guard and decision rules trust the caller | overturned (review of aaa0fec) | Fix round under way: per-subject purge, schema-bound fence, separate table owner |
| Durable-session testbed as fit for a client deployment | Exactly-once under crash supported, but tenant missing from keys and approval lookup, approvals not bound to a request, unvalidated turn signal | overturned in part (review of 62916ae) | Fix round under way |
| Fence as a lock plus an alias chosen at read time | Failed two independent reviews; main race run recorded a fail | overturned | Epoch carried in the write, enforced by the store; rebuild under way |
| Verification as an optional step, reader filters callable off | Job completion ignored verify; filter could be switched off | overturned | Verify result required to complete; filters not caller-controlled |
| Bulk partial update on unrefreshed `semantic_text` | Embeddings silently dropped | owner-reported | Full-document writes only on that field type |
| Document-level security as tenant isolation | Term statistics leak across the filter | owner-reported | One index family per tenant |
| Deleting text and assuming it is gone | Purged only after retention-lease sync (about 30 s) plus force merge | owner-reported | Erasure verified by planted markers after merge |
| Vector search inside the percolator | Rejected by Elasticsearch | observed | Standing queries are lexical only |
| One fresh headless call per step | About 10.5k tokens overhead each; 20 calls cost about 6.2 times one 20-turn session ($0.7751 vs $0.1246 on the rerun) | owner-reported | Long-lived sessions where contamination allows |
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

### Read 2026-11-15 (research/wv1gg5s6m.output). All readers worked from page summaries, not raw text.

| Source | What it is | Evidence in the source |
|---|---|---|
| X post, sharpeye_wnl | Link to a beginner primer on context engineering; repackages Anthropic's post and two dbreunig posts | Third-hand figures only; no original experiment |
| X post, Promixyz | Essay on agent workflows as dependency graphs, not queues; not about memory | Assertions with illustrative numbers only |
| arXiv 2607.23809 (ACM) | Agent decides when to compress its own context; raw messages offloaded by id and queryable; small model post-trained to use the tools | n=150, no confidence intervals, no ablation isolating the lossless offload or the timing; gains over the nearest baselines within about one standard error |
| GitHub mrinalwadhwa (Autonomy, Ockam, fluent, ai) | Agent identity and attribute-based authorisation; two-tier memory; learner whose drafts are untrusted until the host verifies a cited anchor and hash; checkpoint cadence | Code read, not measured. Counter-examples found in the code: workers start allow-all, memory write failures swallowed, mailbox at-most-once |

| Id | Idea | Verdict | Status |
|---|---|---|---|
| X04 | Every memory write cites session, round and a verbatim quote; a host check verifies quote and speaker mechanically | **HELD** (owner-reported, threshold committed first at 1a62931, feat/phasef-harness 19b74f1) | 2,351 of 2,373 quotes verbatim in the cited round (99.1%, Wilson 98.6 to 99.4). Speaker check: 8 real attribution errors (7 assistant statements stored as the user's), 53 ambiguous. Controls: one word changed, adjacent round, and flipped author all rejected 100%. The 22 failures: 9 wrong round, 3 stitched quotes, 10 punctuation-level. Adopt as a write gate; ambiguous speakers go to review, not to verified |
| X02 | Two-stage retrieval: pick sessions first, then rounds within them | **DID NOT HOLD** (exploratory, pilot, exact in-process retriever, thresholds committed first at 4e14548) | Whole pool, 94 questions: S=20 0.462 vs flat 0.481 (diff -0.019, CI -0.074 to 0.040); S=50 0.481 (diff 0.000). Shuffled-text control 0.000 as required. Planted-gold control 0.504 against a 0.90 bar: missed, so the pipeline's ability to find gold among hard neighbours is itself unconfirmed. Session length predicts gold (AUROC 0.687 vs null 0.448 to 0.547): a length shortcut exists in this benchmark |
| X05 | Skip injection and analysts when retrieval confidence is high; abstain when nothing clears a calibrated score | **HELD at mid pool sizes, NOT at whole pool** (exploratory, pilot, rule committed at a594d3f) | AUROC vs 97.5th percentile of permutation null: +500 distractors 0.831 vs 0.718 yes; +3,900 0.727 vs 0.664 yes; whole pool 0.572 vs 0.631 no; no-distractor level unresolvable (3 negatives). No single cosine threshold transfers across pool sizes. Abstention questions not separable |
| X01 | Pooled token-budget curve | descriptive only | Any gold session present in a packed context at 4,000 / 8,000 / 32,000 tokens: +500 distractors 0.86 / 0.90 / 0.97; +3,900 0.73 / 0.79 / 0.90; whole pool 0.53 / 0.62 / 0.79. All gold sessions at 4,000: 0.65 / 0.45 / 0.33 |
| X24 | Injection and trimming at the model port must keep tool-call and tool-result pairing valid | rule; test when the port fix lands | queued |
| X23 | A memory append must never fail silently | rule; fault-injection test on the rebuilt write path | queued |
| X20 | Re-evaluate policy at delivery, not only at enqueue | rule; add to the kill and inbox tests | queued |
| X18 | Policy point: identity and tenant attributes cannot be overwritten by derived attributes; non-boolean or erroring evaluation denies | test later | blocked on the policy point existing |
| X07 | Analyst findings in a fixed schema with an explicit "nothing relevant" outcome that suppresses injection | rule, untested | queued |
| X13 | Several critics before a finding becomes durable | test later, aimed at false challengers in the freshness scan, not at supersession precision | needs model calls |
| X06 | Prune retrieved text with a small model | test later | needs model calls |
| X15 | Pipeline stages instead of barriers | test later, quiet host only | not started |
| X09 | Agent chooses when to compact | not proven in the source; out of reach for host sessions | not adopted |
| X10 | Post-train a small model to use memory tools | rejected: the target is the client's own models through one port | not adopted |

## Corrections to this ledger from the critic (2026-11-15)

- "Full-history reader equals the oracle reader" is an n=10 micro-pilot (9 of 10). It shows no headroom on that sample; it is not evidence about long-context reading in general.
- "Batched extraction loses facts" counts facts, not recall against human gold; the per-call baseline was never validated by a person. Class: owner-reported count, cause unproven.
- Whole-pool session recall: the right figure is 0.4754 (RRF session recall@10, pilot, n 94, eval/results/pilot/curve_pilot.json). 0.46 was the superseded old-sets value and is dropped.
- Thresholds chosen after seeing pilot data (4,000-token budget, rerank top-20, low-overlap cut) weaken anything built on them; new thresholds must be committed before the run.
