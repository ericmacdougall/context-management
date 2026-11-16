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

| Typed decision log reduced to a current-state table, on one real session (45 hand-labelled items, 71 position changes) | Returns the current position on 33 of 45 with 2 harmful stale answers; plain BM25 latest-hit returns 11 of 45 with 12 harmful; BM25 plus recency 10 of 45 with 9 harmful. Staleness labels flagged 20 of 27 superseded hits and none of the current ones. Controls: shuffled decisions wrong 50 to 65%; links removed gives several "current" values on 23 of 23; treating every logged decision as the user's mislabels 14 of 21 | reviewed with limits (independent blind relabel and audit, feat/realsession-audit 1627b5b): reproduces exactly; on independent labels the log returns current on 28 of 40 with 4 harmful, search 16 and 7 harmful; harm per answered item 0.125 for the log versus 0.50 and 0.27 for search. One session; the fully typed result is circular; labeller agreement on transitions is only about 0.70 | feat/realsession-supersession c2667d8 |

| Memory injection on a Basic licence with no Agent Builder: client hook plus a pre-execution route, failing open | Live on a throwaway Basic node with a stub model: injects in 0.3 s; returns the original prompt exactly when the service is stopped, slow (20 s budget), silent (25 s), or trickling one byte per half second (cut at 25.5 s); controls without the wrapper raise or stay blocked 75 to 130 s; node trace shows no licence error and no inference call on the path | reviewed with limits (reviewer read the node trace and model bodies; whole-call deadline held in 7 local attack cases incl. 50 concurrent hooks). Limits: security was off on the test node; an abandoned connect can still send the token once; the block header accepts free text | fix/adapters-delta-16096 28a6545 |
| Client accepts only the exact memory block followed by the prompt byte for byte | 9 crafted replies refused (extra text, two blocks, edited header, normalised prompt); instructions inside records stay inside JSON strings; plain http to non-loopback refused before connecting | reviewed (local probes) | 28a6545 |
| Agent Builder pre-execution workflow with a fail-open wrapper (Enterprise-optional) | Live on Kibana 9.5.6: injects; original prompt on stopped, slow and silent service; controls without the wrapper give HTTP 500 | owner-reported live; reviewer confirmed the evidence files for the earlier run, not the last rerun | 28a6545 |
| Subscription CLIs refused behind any HTTP gateway | 5 adapter classes on 4 binds: 20 of 20 refused | reviewed (local probes) | cfaefee |

| Durable sessions: exactly-once results and effects, tenant in every key, approvals bound to a request, bounded history during long waits and overload, recovery after a Postgres process crash and a real zombie worker | 800 = 800 = 800 after two Postgres crashes under load; zombie's late completions rejected (control: 13 duplicates); approval-wait history 4,045 events against 31,458 on the old code; overload 4,121 against 29,471 | reviewed with limits (three review rounds; code and artifacts read, nothing re-run by the reviewer). Limits: one host; effect ledger shares the engine's Postgres. Final confirm at 3423f4e: the poisoned-input spin is fixed (68 events per second and 0 of 4 newer inputs before; none and 4 of 4 after), the deadline-edge race is fixed (263 violations in 1,500 before, 0 after), approval backoff measured to its 900 s cap with a dropped wake-up resolving at 899 s. Before client use: the alert for a poisoned input must be durable and operational (it is a workflow-state string, lost on a rebuild), and more than 20 poisoned inputs in one session block progress | feat/engine-evolution 3423f4e (stream closed) |
| Temporal on Postgres persistence: crash windows, long timers, upgrade through a real schema change under load, rebuild from the session store | 2,400 = 2,400 = 2,400 through 1.30.9 to 1.31.5; skipped schema update refused; 1,440 turns exactly once after dropping the engine's databases | reviewed with limits (single-container server only). A downgrade is NOT refused by the server; a guard that refuses an older image on a newer schema exists and works in the lab launcher only, so it must be enforced in the client's own deploy path (init container or pipeline gate), with rollback defined as restoring the pre-upgrade snapshot | same |

## Did not hold

| Idea | What happened | Class | Consequence for the design |
|---|---|---|---|
| Hybrid retrieval alone as the memory | Session recall@10 0.968 with no distractors, 0.4754 at the whole pool (curve_pilot.json); evidence in a 4,000-token context 0.41 | owner-reported | Retrieval is one input, not the answer source; current-state table and analysts needed |
| Batched fact extraction to save calls | 9.0 facts per session at 1 per call, 5.1 at 4, 4.5 at 8, 2.7 at 16 | owner-reported | No batching above 1 on the truth path |
| Automatic "this replaces that" links as a filter | Precise 16/16 but recall 10/16, unstable across batching | owner-reported | Supersession labels are advisory, never filtering |
| Analyst over cross-encoder rerank on which sessions are found | No resolvable gain at any pool size (+0.015, +0.043, +0.024, all intervals include 0); the analyst only adds more gold rounds in context at the two smaller pools | pilot observation | An analyst call (63k tokens, 13.6 s) is not justified for reordering alone; rerank first, analyst only where digging is needed |
| Cheap first-stage variants to find the hard questions' sessions (exploratory, pilot, exact retriever, feat/eval-scaffold 79e0564) | None held on the 23 hard questions at whole pool against flat 0.118: session-sum pooling 0.051 (worse, CI -0.135 to -0.009), rare-term boost 0.104, pseudo-relevance feedback 0.124 (+0.006). The wrong-query control was 0.000 for variant and baseline alike, so it was uninformative | exploratory | Lexical and pooling tweaks do not fix candidate generation |
| Cross-encoder over a deeper list (top 200) for the hard questions | 23 hard questions at whole pool: 0.081 vs 0.041 flat (+0.041, CI -0.020 to 0.114); pre-set rule (+0.05 with lower bound above 0) not met; gold among the 200 for 11 of 23; about 243 s per question | exploratory (feat/eval-scaffold c45cf0f) | Not pursued; registered as a negative hypothesis |
| Shallow candidate lists for hard questions | Gold is in the flat candidate list for the 23 hard questions at 30% (50 rounds), 39% (100), 48% (200), 65% (400), 70% (1,000); all 94: 0.79, 0.84, 0.88, 0.92, 0.93 | exploratory | A first stage must go about 400 rounds deep before reordering can help two thirds of hard questions; 30% stay unreachable even at 1,000 |
| Cross-encoder gain over plain retrieval once session length is controlled | Unmatched whole pool 0.528 vs 0.475 (94 questions, run complete); length-matched difference +0.020 (CI -0.004 to +0.049), no longer resolvable. Analyst gain shrinks from +0.130 to +0.098 (CI 0.041 to 0.157) but survives. Size-matched random control not yet run | exploratory | Part of the reranker's apparent gain is the benchmark's length artefact; the analyst's is not |
| Analyst tools (search, open, timeline) versus the same analyst without tools | No resolvable gain on session recall@10 at any level; analysts used no tool in about half of questions; one resolvable gain only on the 23 hard questions at +3,900 | pilot observation | Tool use must be prompted or forced when candidates look weak; current prompt leaves it idle |
| Any reordering arm on the 23 hardest questions at whole pool | All near the floor (0.032 to 0.093); the gold session is among the 50 candidates for only 7 of 23 | pilot observation | The bottleneck is candidate generation, not ordering; needs wider or different first-stage recall |
| Rebuilt write path as fit for a client deployment | Second-generation fence holds against an honest stale writer, but independent review blocks: erasure verify breaks above 65,536 events per subject, erasure livelocks on large tenants, fence trusts callers, done-guard and decision rules trust the caller | overturned (review of aaa0fec) | Fix round under way: per-subject purge, schema-bound fence, separate table owner |
| Durable-session testbed as fit for a client deployment | Exactly-once under crash supported, but tenant missing from keys and approval lookup, approvals not bound to a request, unvalidated turn signal | overturned in part (review of 62916ae) | Fix round under way |
| A typed decision log alone as proof of the latest position | 20 of 71 real position changes (28%) had no typed event; on 10 prose-only items the log abstains every time; both of its harmful stale answers were prose-only changes made after the last typed event; 19 of 20 untyped changes were agent-asserted, 1 was the user's | reviewed with limits: the independent labeller puts the gap at 0.33 to 0.34 (23 of 67), and 22 of those 23 untyped changes are agent-side or reviewer-side, 1 is the user's | A "user turn contradicts a decision creates a pending change" rule fired correctly 14 times with 1 false and 1 ambiguous, but catches only 1 of the 23 untyped changes. The gap closes on the agent side: typed agent decision and correction events naming what they replace, a claim registry keyed by figure or claim id, reviewer verdicts typed against claim ids, user promises kept open until the user closes them, item ids attached at write time |
| Trusting the cited source of a logged decision | Main's own freeze record cited a user message that approved something else; latest-event-wins reported agent authority for a user-approved position | owner-reported, one real session | Authority must be checked against what the cited words actually decide; the log cannot detect a wrong citation by itself |
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

## Write path, second-generation build (feat/writepath-rescue 4960f50; owner-reported unless stated; lab host, stand-in key store, single-node Elasticsearch)

Held (lab evidence; the fence and erasure mechanisms passed an independent delta review with limits at 963e597, later items owner-reported):

| Mechanism | Figures |
|---|---|
| Fence enforced by the stores against a stale writer | 14 of 14 schema attacks refused; races with randomised timing hold with measured overlap |
| Erasure of a 100,000-event subject, per-subject fencing, sweep survives a failed job | done, residue 0 |
| Legal hold protects the held person's own messages when another subject is erased | 5 of 5 in three cases; controls violated 5 of 5 |
| Secret scan before append, including encoded variants | planted secrets caught 880 of 880; 2 false blocks in 3,862 clean files; without decoding only 340 of 880 |
| Forged append, tampering and lost log tail detected against an anchor outside the database | 10 of 10 in four cases; controls violated 10 of 10 |
| Checkpoint seal | edited seal and truncated log rejected 10 of 10 |
| Destroyed key bytes really gone from disk | key on disk in 6 of 6 runs before the purge waited for older transactions; 0 of 6 after |

Did not hold or measured as a problem:

| Finding | Figures | State |
|---|---|---|
| Erasure cost at tenant scale | 1,000,000-document tenant: a 10-event subject takes 48 s, 1,000 events 90 s, 100,000 events did NOT finish (claim lost after 109 s). Every erasure pays a 22 to 30 s Elasticsearch retention-lease wait | open |
| One tenant's erasure does not affect others | Another tenant waited up to 60 s for its events to become visible during the large erasure (the projector waits for the oldest open transaction in the database) | open |
| Role separation | All credential files belong to one OS user; verifier runs in the eraser's process | open: needs separate service identities |
| Elasticsearch fence against a dishonest writer | not started (security off on the test node) | open |
| A test node can hold the per-tenant index layout | The node hit its 1,000-shard limit during the suite | open: layout rule needed for many tenants |

## Stage G: deeper shuffled candidates and a first look at answers (2026-11-16, pilot, feat/eval-scaffold c9e3909; design and scorer committed before any result; judge NOT validated)

Held (pilot observations, owner-reported):

| Finding | Figures |
|---|---|
| An analyst reordering 300 shuffled candidates beats plain retrieval and the cross-encoder at whole pool | Session recall@10, 94 questions, failures counted: reorder-only 0.700, digging arm 0.690, plain retrieval 0.481, cross-encoder 0.528. Reorder minus plain +0.219 (CI 0.146 to 0.292); reorder minus cross-encoder +0.172 (CI 0.105 to 0.243). Earlier 50-candidate analyst in rank order: 0.603 (descriptive; depth and order differ) |
| Shuffled order shows no anchoring cost | Recall rose from 0.603 to about 0.70 with shuffled, deeper lists; depth and order are confounded |
| Superseded facts: the latest value reaches the context | Knowledge-update questions (15): reorder-only 1.000, digging 0.933, plain retrieval 0.333 |
| The benchmark has headroom for a memory layer | Judged answer accuracy, 77 questions, 4,000-token contexts: plain retrieval 0.384, analyst context 0.582, gold-only 0.911. Gold-only minus plain +0.519 (CI 0.408 to 0.636); analyst minus plain +0.195 (CI 0.077 to 0.320). A string-match heuristic gives 0.292, 0.444, 0.667 and disagrees with the judge on 22 to 32% of items |

Did not hold:

| Finding | Figures | Consequence |
|---|---|---|
| Telling the analyst to dig with tools | Digging minus reorder-only: -0.010 (CI -0.043 to 0.024) on all 94; -0.067 on the 23 hard questions; where both arms answered, digging is slightly WORSE (-0.027, CI -0.052 to -0.006). Only 50 of 91 followed the rule; 35 made no tool call | Tools dropped from the default analyst; a digging claim would need an enforced minimum and a new test |
| Digging when the list holds no gold | 10 questions had no gold among 300 candidates; 7 dug; every arm scored 0.000 | Search-and-open tools over the same index do not reach what first-stage retrieval missed; these need a different signal (typed facts, links, decisions) |
| Hard questions fixed by a better analyst | Low-overlap 23: reorder-only 0.186, digging 0.119, plain 0.041; answers on that stratum: analyst minus plain +0.043 (CI -0.130 to 0.217), gold-only minus plain +0.696 | The largest remaining gap is exactly where candidates are missing |

Cost: 640 calls, 24.4 million input tokens, 81 USD notional.

## Stage X0: extraction canary (2026-11-16, pilot, feat/eval-scaffold a28b0a8; 200 calls, one session per call; design and scorer committed first; mechanical scoring, no judge)

| Finding | Figures | Class |
|---|---|---|
| Single-session extraction is reliable and priced | 200 of 200 parsed, 0 repaired or rejected; 15.5k input tokens (10.5k of it fixed harness overhead), 1.9k output, 15.7 s, 0.077 USD notional per call | owner-reported |
| Extracted facts carry the answer when the session does | Gold answer key present in a fact for 30 of the 32 questions whose raw gold text has it (weak-list 9 of 10, other hard 7 of 8, knowledge-update 14 of 14); crude string heuristic | owner-reported |
| Quote check as a write gate at volume | 1,829 of 1,905 quotes verbatim in the cited round (0.960, CI 0.950 to 0.968); 1 speaker mismatch, 22 ambiguous; controls rejected 100%. Lower than the earlier 99.1%: 4% of facts would be refused | owner-reported |
| Fact text is findable when present (toy pool of 200 sessions, optimistic by construction) | Keyword search finds a gold-session fact for 7 of the 11 unreachable questions on statements, 9 of 11 with quote and cue keys; the same search over raw rounds finds 5 of 11 | owner-reported; says nothing about the whole pool |

Did not hold or open:

| Finding | Figures | Consequence |
|---|---|---|
| The 12-fact cap in the extraction prompt | 28% of sessions hit it; 49% of gold sessions against 14% of random ones; cap share rises with length (0, 8, 26, 34, 43% by length band). The two questions whose answer is in the raw text but in no fact both involve capped sessions | The cap binds where it matters; facts lost to it cannot be counted from this run; a 10-call re-extraction at a higher cap would measure it |
| Prompt limits obeyed | 14.6% of quotes exceed the 12-word limit; event time present on only 10.4% of facts | Dated facts are mostly undated in practice; time-aware retrieval cannot rely on event time from this prompt |

Cap check (stage X0B, 10 calls, feat/eval-scaffold 19f19ed; 10 capped sessions re-extracted with the cap at 30; a direction, not a rate):

| Finding | Figures | Consequence |
|---|---|---|
| The 12-fact cap was dropping facts | All 10 sessions returned more than 12: 13 to 24, mean 15.8; none reached 30 | Held: use a cap of 30 (about +9% cost per capped call, about +1.5% overall) |
| The cap explains the two missing answers | Neither answer appears at cap 30 in any re-extracted gold session | Did not hold: the cause of those two misses is not established |
| A single extraction pass gives a stable fact set | 111 of the original 120 facts reappear by quote match (92.5%); 9 replaced; 28% of the new facts match no original. Cap change and run-to-run variation are not separated | Open: needs an identical-prompt replicate before volume (gate proposed at 85% overlap) |
| Quote check on the additional facts | 151 of 158 verbatim in the cited round (0.956); on facts with no original match 41 of 45 (0.911) | The extra facts are somewhat less well grounded |

Reading of the two missing answers (no model, feat/eval-scaffold c3bdb4b): neither is an extraction miss in the sense first assumed. One is a count across five sessions whose five parts are all present as facts (no single fact can hold the count; joining is the reader's or analyst's job). The other is a preference question whose answer is a paraphrase the string heuristic cannot match, plus one real gap: the user's stated wish list was not extracted, because the prompt asks for concrete facts the assistant stated and for specific details, and never for the user's stated preferences. So: the answer-string heuristic is not a valid gate for count or preference questions; the extraction prompt needs a line for user preferences, re-gated under a new version; the facts in that session were mostly assistant-stated (9 of 12).

Stability and preference-line check (stage X0C, 40 calls, feat/eval-scaffold 99f76bf; gate and adoption rule committed first; the scorer was patched once, before any result was read, for records missing the author field):

| Finding | Figures | Verdict |
|---|---|---|
| A single extraction pass gives a stable fact set | Two runs of the identical prompt on 10 sessions reproduce 77.6% of facts by quote match (gate 85%, no session under 60%; lowest session 51%); below the pre-declared inconclusive band | **Did not hold.** About one fact in four differs between two runs of the same prompt on the same session |
| One added sentence makes the extractor record the user's stated preferences | User-authored facts 6.9 to 8.0 per session (+1.1 against a +2.0 bar; higher in 11 of 20 sessions against 14); quote check falls to 0.908; none of the 3 missing wish-list items appears in either arm; displacement and output size within bounds | **Did not hold** (3 of 5 pre-set criteria failed). Effect is real but weak, about twice run-to-run noise |
| The model's own author field can be relied on | One record's 19 facts carried no author at all | Did not hold: author should be set mechanically from the speaker of the cited quote |
| Reported notional cost compares prompt variants | Costs differed 39% between arms with identical token counts, because of cache state | Did not hold: compare tokens, not reported dollars |

Post-hoc analysis of the outputs on disk (no model; exploratory; rules written first; feat/eval-scaffold 5f4e04b):
- A second pass adds about 31% more distinct facts (158 and 177 give a union of 200) and a third adds less; the set does not converge in three draws. By reading the 7 gold sessions, no fact needed for a question was present in only one run. So a second pass is NOT the default; it stays a conditional arm.
- The unstable facts are mostly the less important ones: facts in only one run are 73% assistant-stated (against 42% of stable facts) and pass the quote check less often (0.893 against 0.977); user-stated facts reproduce 88% of the time, assistant-stated 67%.
- Mechanical repair: of 130 quotes not verbatim in the cited round (of 2,698 facts), 98 are fixed by punctuation or ellipsis normalisation and 3 by re-pointing to another round; 29 are paraphrases; none is fully ungrounded. After repair about 98.9% are verbatim. The model's author field agrees with the speaker-derived one on all but 1 of 2,525 (and there the dataset's role labels were wrong), so mechanical author is a fill for missing fields, not a corrector.

Cost extrapolation (random sample only; interval is sampling noise; the pool's longest sessions are 3.7 times longer than any sampled):
6,100 calls about 442 USD notional, 90M input tokens, 13.4 h at concurrency 2; 22,500 calls about 1,630 USD, 333M input tokens, 49 h.
Derived estimate without the harness overhead (what a direct model API would see): about 204 USD and 753 USD. Linking calls not priced.

## Independent replication (2026-11-16, feat/replicate eaaeb53; non-author scripts and controls, thresholds committed first)

| Claim | Owner's figure | Replication | Verdict |
|---|---|---|---|
| Licence matrix on a real Basic node | per research | On Basic: BM25, kNN, rescorer, client-side fusion, quantized HNSW, rescore_vector, ES/QL MATCH and FORK, lexical percolator, data streams, aliases with require_alias and must_exist, force merge. Licence error on Basic: rrf and linear retrievers, reranker, FUSE, creating an inference endpoint, semantic_text indexing, bbq_disk (mapping accepted, 40 of 40 documents fail), document-level security | replicated |
| Elasticsearch BM25 shares 0.751 of the exact top 10; client fusion equals ES rrf | 0.751; 0.8167 | 0.7510; 0.8167; a matched analyzer raises overlap to 0.9637, so the analyzer explains the gap | partly (control fell 0.082, under its 0.10 floor) |
| Read-after-write through the log tail | 100 of 100; 600 during a projector pause | 100 of 100; 600 of 600; with the tail off, 0 | replicated |
| A long open transaction stalls projection; the tail still finds the event | yes | yes; and a transaction in ANY database on the cluster stalls it | replicated, wider than claimed |
| Hash chain detects tamper and gaps | yes | all detected; 2,000 appends from 8 writers gapless | replicated; but the app role can disable the append-only trigger and a correctly chained forged append verifies, so an external anchor is required |
| Quote check 2,351 of 2,373 | same | 2,351 of 2,373, controls rejected 100% | replicated |
| Decision reducer on 2,000 scenarios | 0 failures | 600 scenarios, 0 disagreements | replicated |
| Search visibility about 1 s | p50 1,001 ms | p50 1,002 ms with a sequential loop; 522 ms with random timing | replicated; the 1 s figure is an artefact of the write loop: state it as "up to the refresh interval" |

Did not replicate or only in part:
- **rescore_vector oversample 3 recovers the recall lost to quantization**: holds on the owner's index, but on an independent build it still loses 0.039 (CI -0.078 to -0.005). Withdrawn as a general claim; the Held row above stands only for that one index.
- **Fenced fold and projection in the first write-path build**: the fences exist only in an experiment script, not in the library (superseded by the rebuild in any case).
- **Evidence hygiene**: 0 of 276 owner result files carry all six fields the proof rules require (commit, environment, control, witness counts, thresholds, per-item rows); four design rows cite artifacts that do not exist at the cited commit.

## Reconciliation of the analyst comparison (2026-11-16, feat/eval-scaffold 142b684)

The rows above for the analyst and the cross-encoder were first reported against a 50-candidate baseline and a 78-question
reranker snapshot. Recomputed on the registered baseline (depth 200) with all 94 questions, session recall@10, pilot:

| Comparison | First report | Recomputed | Survives length matching? |
|---|---|---|---|
| Analyst minus plain retrieval, whole pool | +0.130 | +0.127 (CI 0.057 to 0.201) | yes: +0.098 (CI 0.041 to 0.157) |
| Analyst minus plain retrieval, +3,900 | +0.099, resolvable | +0.074 (CI -0.003 to 0.146): **not resolvable** | n/a |
| Analyst minus cross-encoder, whole pool | +0.024, not resolvable (78 questions) | +0.075 (CI 0.012 to 0.137): **resolvable** | yes: +0.073 (CI 0.017 to 0.129) |
| Cross-encoder minus plain retrieval, whole pool | +0.052 | +0.054 against the 50-candidate list | **no**: +0.023 (CI 0.000 to 0.051); random same-size pool +0.008 |
| Any arm on the 23 hard questions | near floor | unresolvable under every definition | n/a |

So: the earlier "analyst is not distinguishable from the cross-encoder" row is withdrawn at the whole pool; the earlier
"analyst beats plain retrieval at +3,900" claim is withdrawn; the cross-encoder's advantage is largely a pool-size and
session-length effect. Size-matched random control (8 seeds): retrieval arms gain 0.02 to 0.08 from the length cue, the
analyst arms gain nothing from it. Baseline differences between files were fusion depth (50, 200, 400), not errors;
canonical whole-pool value 0.4754 at depth 200. The rerank time of 26 s was an estimate, never measured; measured is
64 s at top 50 and about 20 to 27 s at top 20 (loaded host, indicative).

## Corrections to this ledger from the critic (2026-11-15)

- "Full-history reader equals the oracle reader" is an n=10 micro-pilot (9 of 10). It shows no headroom on that sample; it is not evidence about long-context reading in general.
- "Batched extraction loses facts" counts facts, not recall against human gold; the per-call baseline was never validated by a person. Class: owner-reported count, cause unproven.
- Whole-pool session recall: the right figure is 0.4754 (RRF session recall@10, pilot, n 94, eval/results/pilot/curve_pilot.json). 0.46 was the superseded old-sets value and is dropped.
- Thresholds chosen after seeing pilot data (4,000-token budget, rerank top-20, low-overlap cut) weaken anything built on them; new thresholds must be committed before the run.
