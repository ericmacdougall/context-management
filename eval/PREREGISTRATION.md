# Pre-registration: elastic memory lab evaluation

Ticket: GoodQA goodqa#16096. Design under test: `../DESIGN.md`.
Source of the work: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a/b5d6a478-d70d-4516-8e08-839e60bbf5c7 "double check/finish the research and run the experiments".

Status: DRAFT v2, ready to freeze once main records the open items below as settled. Freezing means recording the git commit hash of this file in the ticket before any run (`git rev-parse HEAD:eval/PREREGISTRATION.md`). After freezing, edits go in a dated "Amendments" section at the end, each with a reason and the data already seen; thresholds are never edited after the test set is first scored.

Provenance of choices: USER decisions come from answer:73716864-6b3b-4deb-94ef-8ed2c8432e4a/02b847b6-8aaf-49cd-9108-3691d2bc3a40 (public benchmark first plus the user's own redacted sessions later; LLM stages on Sonnet over all 500 questions; Elastic trial licence on the throwaway node). Items marked MAIN are implementation choices made by main, not the user's words. Items marked MEASURED were re-measured on the data file by `emlab.corpus.verify_dataset` (output kept at ~/emlab-16096/out/verify.json on t2-dev).

Labels: `ASSUMPTION` = not the user's words. `TARGET_MISMATCH` = result narrower than the original slice. `TO FIX FROM PILOT` = placeholder that must be replaced by a number derived on the pilot set by the stated rule, committed before the test run. Nothing in this file is a measured number.

## 1. Scope and what each phase can decide

| Phase | Needs | Decides |
|---|---|---|
| R: retrieval only | Elasticsearch + external embeddings, no LLM | Neither K1 nor K2 |
| F: facts and answers | LLM extraction and answer calls, same reader for all arms | K1 (and the evidence for K2 inputs) |

Phase R decides neither kill rule: both rules compare systems on extracted facts and on answers. If the run stops after Phase R the outcome is reported as `TARGET_MISMATCH` against the original slice (facts layer vs best tuned baseline on knowledge-update and temporal questions), with Phase R numbers labelled "retrieval-only, kill rules undecided". K2 (concept docs vs facts + retrieval) additionally needs a concept-doc arm that this document does not define yet; it is registered as H2 below but cannot be run until an arm is specified (Section 5).

## 2. Dataset

- Benchmark: LongMemEval-S, cleaned release `xiaowu0162/longmemeval-cleaned`, file `longmemeval_s_cleaned.json`, 277,383,467 bytes, sha256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` (MEASURED: downloaded on t2-dev, hash and size match), MIT licence. The older non-cleaned release is deprecated and not used. The user asked for public data first; the specific benchmark and variant are MAIN's choice (originally an ASSUMPTION).
- Fields (MEASURED): question_id, question_type, question, answer (a string, or an int in 32 questions: cast to str), question_date, haystack_session_ids, haystack_dates, haystack_sessions (list of sessions, each a list of {role, content}; evidence turns carry has_answer: true), answer_session_ids.
- 500 questions; question_type counts: single-session-user 70, single-session-assistant 56, single-session-preference 30, temporal-reasoning 133, knowledge-update 78, multi-session 133. 38 to 62 sessions per question (median 48); 23,867 session references, 19,195 unique session ids.
- Abstention is not a type: 30 questions whose id ends `_abs`. CORRECTION to the relayed facts: all 30 DO carry an answer_session_ids list, and 21 of them have no has_answer turn. Rule (registered): abstention questions are excluded from every retrieval metric (recall, NDCG, gold-latest, low-overlap) by the id convention regardless of any gold list, leaving 470 questions with retrieval gold; they are used only for abstention precision in Phase F. All 470 non-abstention questions have at least one has_answer turn (MEASURED).
- Same session id, same content, DIFFERENT dates: content never differs across questions (0 conflicts) but 3,942 session ids carry different dates in different questions (filler sessions get a per-question date). CORRECTION to the relayed facts. Consequence: the shared index stores no date; the as-of logic uses each question's own haystack_dates, computed client-side.
- 13 session references repeat a session id inside the same haystack (23,867 references, 23,854 distinct per question); the loader keeps the first occurrence.
- haystack_session_ids are not chronological (289 of 500 in order), so no rank or position is used as time.
- Late sessions: 76 questions (60 temporal-reasoning, 16 knowledge-update) contain sessions whose timestamp is later than question_date by under one day; 75 such sessions are gold. As-of rule (registered, day granularity): a session is visible iff its calendar day <= the question's calendar day (`emlab.dataset.visible_as_of`). A strict timestamp comparison would drop gold. These 76 questions are reported as the named sub-stratum `late_session_subset` (16 of them fall in the pilot). MEASURED (`--action diagnostics`): the day rule removes 0 of 23,854 haystack sessions across all 500 questions and 0 of 890 gold sessions, while a strict timestamp rule would remove 70 gold sessions. Consequence stated plainly: on LongMemEval-S as-of filtering is identical to no filtering, so it cannot change any ranking here; it is kept as a safety rule and regression guard and is not run as an arm; the only time-aware arms are the exploratory `expand_filter` and `expand_boost`. Date-parser coverage (MEASURED, structural, no retrieval): a range is extracted for 46 of 500 questions (9.2%; 10 of 102 pilot questions), and `expand_filter` changes the searched session set for 21 of 500 (7 in the pilot). Time-aware results on S are therefore bounded to a small subset and exploratory; this is a threat, not a hidden choice.
- Index sharing: ONE shared index of unique rounds; every search is filtered to the question's haystack_session_ids (and to the time-aware subset). Distractor sessions are shared across pilot and test questions (threat below). answer sessions shared by two questions: 16 references, 8 distinct ids by inference, re-measured by `distinct_answer_session_ids_shared_across_questions` in verify.json ( CORRECTION to the relayed "no answer session appears in another question's haystack"). Questions sharing an answer session form one cluster (union-find over shared gold sessions: 495 clusters for 500 questions, 5 clusters of two or more questions). Clusters, not questions, are the unit of the split and of the bootstrap.
- Knowledge-update gold (MEASURED): all 72 non-abstention knowledge-update questions list exactly 2 answer sessions, both with has_answer turns. Registered stale rule: the CURRENT-value gold session is the one with the latest session date (`Question.gold_latest_session_ids`); the other gold session is stale. The date rule agrees with the id-suffix order (`_2` newer than `_1`) in 71 of 72; NAMED EXCEPTION: `618f13b2` (suffixes inverted, same day, 17:45 vs 22:16); the date rule is kept (main's decision) and the case is listed here. The rule was fixed before any test-split result was read.
- Gold labels: session-level gold = answer_session_ids; round-level gold = rounds containing a has_answer turn. Gold-latest hit (Phase R and F): knowledge-update only; the latest gold session must be in the top k; stale-only counts as a miss.
- The paper's fact-key and time-aware retrieval numbers (Table 3) are on the M variant and are not comparable to an S run; cited as context only. The Table 9 split is not stated.
- The fixture in `eval/fixtures/tiny.json` is hand-written and proves nothing about the real data.

### 2.1 Private sessions (the user's own redacted sessions): NOT YET SPECIFIED

The user wants their own redacted sessions evaluated in addition to the public benchmark. Which sessions, the redaction procedure, the gold-label procedure, where the data may live, and whether any LLM call may see them are all open. Until specified: no private data is read, copied or used; no number in this document or in any report may mix private and public data; a separate pre-registration section and a separate frozen hash are needed before any private run. Benchmark first.

## 3. Sampling and splits (fixed before data is seen)

- Strata by dataset question type: single-session-user, single-session-assistant, single-session-preference, multi-session, knowledge-update, temporal-reasoning (names to confirm), plus abstention (identified by id convention, to confirm). Low-overlap is an overlay defined in 3.2, not a type.
- 3.1 Split: by CLUSTER (questions that share an answer session stay together), stratified by question type. Within a stratum, clusters are ordered by `sha256("16096:" + cluster id)` (cluster id = smallest question id in the cluster) and added to the pilot until it holds at least `ceil(0.2 * n)` questions of that stratum (never all units). Seed constant `SPLIT_SEED = 16096`; pilot fraction 0.2, alpha 0.05 one-sided, power 0.80 (MAIN, confirmed). MEASURED actual sizes (accepted by main): 102 pilot and 398 test questions (pilot by type: multi-session 27, temporal-reasoning 27, knowledge-update 16, single-session-user 14, single-session-assistant 12, single-session-preference 6; 8 abstention questions in the pilot). The earlier "100 / 400" was approximate.
- The pilot set is used for: tuning every arm, fixing thresholds and minimum n, and debugging. The test set is scored once per frozen configuration. Any test-set look before the freeze voids the freeze.
- Test-set size: all 398 test questions for Phase R (no LLM cost). Phase F runs LLM stages on Sonnet over all 500 questions (USER decision), so pilot and test are both available; if the effective n in a primary stratum is below the minimum n from H1/H2 the result is INCONCLUSIVE, never narrowed silently.
- 3.2 Low-overlap stratum, operationally: rank ALL rounds of the question's own haystack (no depth cut) with raw-question BM25 and with dense retrieval, using the same embedding model as every arm, the raw question text and no time filter; a question is low-overlap iff the best-ranked GOLD ROUND (a round containing a has_answer turn) has rank greater than 100 on BOTH (`emlab.metrics.is_low_overlap`, threshold 100, `emlab.runner.low_overlap`). A haystack has about 2,000 to 3,000 rounds, so a rank above 100 is meaningful (the session ranking has at most 62 entries and cannot be used). Questions with no retrieval gold (abstention) are never low-overlap. For the pre-computation, BM25 and exact cosine are computed in-process (`emlab.live.ExactRetriever`; BM25 k1 1.2, b 0.75, global statistics over the shared chunk corpus, lowercase alphanumeric tokens, 33-word English stoplist, no stemming; a deviation from Elasticsearch's english analyzer, to be cross-checked on the node later). It is computed on the pilot split only until this document is frozen. If the pilot stratum is near-empty, that is reported to main as a finding; the threshold is not moved by the owner.

## 4. Phase R: retrieval only (no LLM)

Stack: Elasticsearch 9.5.x single node (throwaway node; the trial licence is approved by the user), localhost only, capped resources (see RUNBOOK). Only Basic-tier features are relied on (BM25, kNN), because Elastic's `rrf`, `linear`, FUSE and reranker retrievers need the Enterprise licence: fusion and reranking are done client-side so results do not depend on a licence.

Index unit (MAIN): one document per ROUND (user message plus the assistant reply that follows it), carrying session_id and round index; the date is not stored (Section 2). Chunk rule (registered): the round text is tokenised with the E5 tokenizer; rounds over 480 tokens are cut at token boundaries into consecutive non-overlapping windows of 480 tokens, each its own document; a round's score is the max over its chunks; the session ranking is the sessions ordered by their best round.

Embedding (MAIN): `intfloat/multilingual-e5-small` (MIT, 384 dimensions, 512-token limit), prefixes `query: ` and `passage: `, mean pooling, L2 normalised, identical in every arm, computed outside Elasticsearch on t2-dev CPU and indexed as `dense_vector` with `bbq_hnsw`. Labelled deviation from the semantic_text path. Reason: Jina v5 is CC BY-NC; E5-small is also Elastic's built-in model, so a parity check through the Elastic inference API on the trial node can be added later.

Reranker (MAIN): `BAAI/bge-reranker-v2-m3` (Apache-2.0), CPU, top 50 fused candidates (rerank only reorders the head; the tail keeps its fused order). Input rule (registered): the (question, chunk) pair is scored for every chunk of a candidate round, with the question kept intact and the chunk truncated to a 512-token pair limit; a round's rerank score is the max over its chunks (same chunk rule as the dense side, so long rounds are not silently truncated). Scores are cached by (question id, round id) because rerank runs under every time-aware mode on mostly the same candidates. The measured CPU rate (`--action rerank-timing`) is reported before the pilot and top-50 is changed, if needed, only by a registered amendment before the pilot. If CPU time for all questions is impractical, a smaller registered top-n is proposed before the pilot, with a measured rate, never after.

Arms, each under every registered mode (`off`, `expand_filter`, `expand_boost`):
1. BM25 (`match` on `text`, english analyzer)
2. Dense (kNN, cosine)
3. Hybrid RRF, client-side (rank constant grid {20, 60, 100})
4. Hybrid linear, client-side (min-max normalised, dense weight grid {0.3, 0.5, 0.7})
5. Hybrid + rerank (RRF then rerank)

Time-aware modes (rule fixed before the pilot; deterministic, no LLM, `emlab.timeparse`):
- `off`: whole haystack. The day-granularity as-of rule (a session is visible iff its calendar day <= the question's calendar day) is applied inside every mode as a safety rule. MEASURED identity: it removes 0 of 23,854 haystack sessions and 0 of 890 gold sessions on S, so it is identical to `off`; it is therefore NOT run as a separate arm (main's decision).
- `expand_filter` (EXPLORATORY): the haystack intersected with a date range extracted from the question text; if no range is found or the intersection is empty it falls back to `off`.
- `expand_boost` (EXPLORATORY): sessions inside the extracted range get their rank-score (1/(60+rank)) multiplied by (1 + beta), beta grid {0.25, 0.5, 1.0}; applied after fusion and before rerank.
Both expand modes are labelled exploratory because the date parser covers only 46 of 500 questions; they carry no hypothesis and cannot support a claim about time-aware retrieval. Date extraction priority: absolute dates (`YYYY/MM/DD`, `Month D[, YYYY]`, `D Month [YYYY]`), `Month YYYY`, `in|during|last|this <month>` (most recent such month), then relative expressions (yesterday, today, last <weekday>, last week = previous Monday to Sunday, last month, last year, past/last N day|week|month|year(s) = [question date minus N units, question date], N day|week|month|year(s) ago = centre +/- slack of 1, 3, 15, 60 days). Both variants are reported side by side as exploratory results.

Pilot tuning rule: the grid values above are evaluated on the pilot only (`--action grid`, `off` mode for fusion parameters, `expand_boost` for beta); the chosen value maximises mean session NDCG@10 over pilot questions with retrieval gold, ties to the smaller parameter value; the choice is committed before any test run.

Metrics: session-level and round-level recall@k and NDCG@k (binary gain) for k in {5, 10, 20, 50}, against gold ids, plus gold-latest hit@k for knowledge-update. Reported per question type, overall (470 questions with gold), for the low-overlap overlay and for the `late_session_subset`, with n alongside; abstention questions are excluded. Differences between arms use the clustered paired bootstrap (Section 7).

Phase R is descriptive and decides neither kill rule (Section 1); it selects the retrieval configuration that Phase F uses, on the pilot only.

## 4.1 Named analysis: pooled-history curve (Phase R, no LLM, pilot split only until frozen)

Why: a micro-pilot (n=10 pilot questions, LLM) showed the reader given the whole S haystack (about 165k tokens) scored the same as the reader given gold evidence only (9 of 10 each, same single miss). On S as distributed a memory layer therefore has no accuracy headroom, and S can support cost and latency claims only. What matters is behaviour as the history grows far past the context window. This analysis measures retrieval, not answers, as history grows. (Source of the micro-pilot: main's relay; not re-measured here.)

Levels, per question, with distractor sets SHARED by every question of a split (fixed seed 16096; main's decision, so later answer-level arms can extract facts once for exactly these sets):
- Sets, defined once per split: eligible = pool sessions that appear in no haystack of any question of that split; eligible sessions are ordered by `sha256("16096:D:<split>:<session id>")`; D500 = the first 500 and D5000 = the first 5,000 of that order (D500 inside D5000); POOL = all 19,195 unique sessions. If fewer than 5,000 are eligible, D5000 is all of them and the actual size is recorded (`d5000_short`).
- H0 = the question's own haystack (as in Section 4). H1 = own + D500. H2 = own + D5000. H3 = own + every other session of POOL except sessions of the question's own cluster (Section 2). Own sessions come first, in their own order.
- Pilot-split work uses sets drawn from sessions in no PILOT question's haystack. The later test-split run uses the same rule with sets drawn from sessions in no TEST question's haystack; this is registered now and NOT built (the runner refuses `poolsets` for the test split until the pre-registration is frozen).
- The session id lists (ids only, with seed, split, eligible count) are written as an artifact by `--action poolsets` (`pool_sets_pilot.json`) so another owner can extract facts for exactly D500 and D5000 once.
- An earlier per-question-draw design was replaced by this one before any result was computed; no per-question-draw numbers exist.
- Approximate size of each level: reported by the run as characters/4 (an estimate, not a tokenizer count), mean and distribution over the pilot questions; no figure is asserted before the run.

Measurements (pilot questions with retrieval gold; abstention excluded):
1. Session-level and round-level recall@k and NDCG@k, k in {5, 10, 20, 50}, for every arm of Section 4 (BM25, dense, RRF, linear, and rerank if the reranker is available), mode `off`, at every level.
2. The drop from H0 to each of H1, H2, H3, per arm and metric, with a clustered paired bootstrap (resampling clusters, 2,000 resamples, seed 16096; `emlab.pool.drop_bootstrap`), reported with the number of clusters. Descriptive: no threshold, no kill-rule role.
3. Time-aware modes are OFF at every pooled level (a session has different dates in different questions; the curve is about relevance at scale, not time).
4. Contamination: distractor sessions come from other questions' simulated users, so a distractor may contain a competing answer. Gold ids stay valid for retrieval metrics. For ANSWER-level use of pooled levels this is a threat (a retrieved competing answer may be correct for another question's user, not wrong for the system). Proxy (at H3): for each pilot question, the number of non-gold ROUNDS ranked above the best gold round on BOTH BM25 and dense over the whole pool (`emlab.pool.contamination_proxy`); the distribution (min, quartiles, p90, max, mean, count of zeros) is reported, and so are the single-leg counts.
5. Cost: per-question wall time at each level with the exact in-process retriever on the host under its thread cap is reported; if the whole-pool level is impractical in-process, that level is run on the Elasticsearch index instead, and the report says which retriever produced which level.

## 4.2 Elasticsearch comparisons (pilot only; differences reported, not tuned away)

- Index: one index `emlab-rounds-v1` on the throwaway node (ES 9.5.6, trial licence), 1 shard, 0 replicas, chunk documents with `vector` (`bbq_hnsw`) and `vector_int8` (`int8_hnsw`) for comparison. Bulk indexing is modest (200 documents per request with a pause) because other agents use the node; their indices, the licence, ML deployments and container settings are not touched.
- kNN recall: Elasticsearch filtered kNN (filter = the question's haystack session ids, about 48 of 19,195 sessions) vs exact cosine over the same sessions, recall of the true top-10 and top-50 chunks, for `index.dense_vector.hnsw_filter_heuristic` = `acorn` and `fanout`, both vector fields, num_candidates 100 and 1000. The settings change is dynamic and applied to this index only. This is a first test, on real embeddings, of an earlier synthetic finding that the default heuristic gave much lower filtered-kNN recall at a 5% filter; the per-question filter here is far more selective.
- Fusion parity (open question A11): client-side RRF (rank constant 60) and linear (min-max, weights 0.5/0.5) over Elasticsearch's own BM25 and kNN chunk lists vs Elasticsearch's `rrf` and `linear` retrievers (window 200) on the same queries: top-10 and top-50 overlap, same-order fraction, same-top-1 fraction.
- Embedding parity: the external E5 vectors vs the node's built-in E5 endpoint (inference id `emlab-e5`) on a sample of 200 rounds (100 pilot gold rounds plus random rounds, seed 16096) and 50 pilot queries, with and without the `passage: ` / `query: ` prefixes: cosine agreement and top-10 overlap within the sample.

## 5. Phase F: facts and answers (needs LLM calls)

LLM stages run on Sonnet over all 500 questions (USER decision): reader and extractor are `claude-sonnet-5-5`; the judge is the benchmark's own question-type-specific judge prompts run on `claude-opus-5-5` (MAIN), preceded by a leniency probe with deliberately wrong answers before the test run; judge and reader come from the same vendor (threat below). Token budget and pull tools are identical in every arm.

Arms:
- A0 facts+hybrid: the design (facts with valid_from/valid_to, modality, trust; hybrid retrieval from Phase R's pilot-selected configuration; budgeted injection plus pull tools).
- B1 tuned time-aware hybrid RAG with session/round keys.
- B2 long context with compaction.
- B3 dated always-present observation log.
- B4 grep/open-by-id agent over raw history.
- B5 "Atlas-style" (MAIN): Elastic's Atlas demo needs Elastic's cloud inference service, which is not approved here, so B5 is a reimplementation on local models of the publicly described shape: three indices, RRF with 80 candidates per leg and rank constant 30, rerank, a supersession filter, and gauss decay with offset 180 d and scale 1825 d. It is NOT Atlas and is reported as "Atlas-style". Vendor-reported numbers are not used as results.
- Concept-doc arm (for K2): registered but CONDITIONAL (MAIN): it runs only if K1 passes, and its arm is defined by a dated amendment committed before any K2 run.
- Every baseline gets the same tuning budget on the pilot (number of configurations `TO FIX`).

Metrics:
- Answer accuracy by stratum (judge-graded; judge model fixed, plus a human-audited sample on the pilot to estimate judge leniency).
- Retrieval gold-latest hit rate: for knowledge-update questions, a hit means the injected or retrieved context contains the evidence for the CURRENT value; context containing only the superseded value counts as a MISS.
- Abstention precision: of the questions where the system abstains, the fraction that are true abstention questions; abstention recall is reported next to it (abstention set: the 30 `_abs` questions).
- Tokens injected per turn (mean, p95), measured from logs of injected ids and block size.

## 6. Hypotheses and thresholds

Primary metric for H1 (one metric, named): END-TO-END ANSWER ACCURACY (judge-graded, Section 5), averaged with equal weight over the two primary strata, knowledge-update and temporal-reasoning (macro-average of the two stratum accuracies; cluster bootstrap over all questions in the two strata). Gold-latest hit rate is defined for knowledge-update only; it is reported next to the primary metric as a mechanism diagnostic and carries no decision.

Sidedness (one choice for the whole section): every decision uses ONE-SIDED 95% bounds (alpha = 0.05), i.e. the 5th and 95th percentiles of the clustered paired-bootstrap distribution (`lower_one_sided`, `upper_one_sided`); sample size uses the same alpha.

- **H1 (kill rule K1).** A0 beats the best tuned baseline (best by pilot score among B1-B5; the baseline is chosen on the pilot) on the primary metric by at least delta1.
  - After the pilot, main takes the proposed delta values to the user; the test run does not start before the user accepts them (MAIN).
  - delta1 = `TO FIX FROM PILOT (pilot set disjoint from test set)`. Rule: the smallest absolute difference that is larger than the pilot's measured noise floor (half-width of the clustered paired-bootstrap interval for the same arm run under two seeds or prompt orderings) AND that the user accepts as worth the facts layer's added complexity. The user's acceptance is a decision sourced in the user's own words; absent it, delta1 stays unfixed and the test run does not start.
  - theta1 = the assumed true effect used for sizing = `TO FIX FROM PILOT (pilot set disjoint from test set)`; rule: the pilot point estimate of the primary-metric difference shrunk to the lower end of its one-sided 80% bound, and it must exceed delta1 (if it does not, H1 is reported as underpowered before the test run instead of being run).
  - Minimum sample: n_min_H1 = `TO FIX FROM PILOT (pilot set disjoint from test set)` independent clusters in the primary strata. Rule: n = ((z_alpha + z_beta) * sd_d / (theta1 - delta1))^2, with sd_d the pilot standard deviation of the per-cluster paired difference, alpha = 0.05 one-sided, power 0.80 (MAIN, confirmed). This sizes a test of "true effect exceeds delta1" against the assumed theta1, not a test against zero. If the test set offers fewer effective clusters than n_min_H1, the outcome is INCONCLUSIVE regardless of the point estimate.
  - Decision: facts layer KEPT only if `lower_one_sided` of (A0 minus best baseline) is above delta1. It is KILLED if `upper_one_sided` is below delta1. Otherwise INCONCLUSIVE (one pre-declared extension to a larger test sample is allowed if budget exists; otherwise reported as inconclusive).
- **H2 (kill rule K2).** Concept docs beat facts+retrieval by at least delta2 on the stratum where concept state is claimed to help (multi-session; stratum choice `TO FIX` before the pilot, because it must follow from the concept-doc design). Conditional: runs only if K1 passes.
  - delta2 = `TO FIX FROM PILOT (pilot set disjoint from test set)`, same rule as delta1.
  - theta2 = `TO FIX FROM PILOT (pilot set disjoint from test set)`, same rule as theta1.
  - n_min_H2 = `TO FIX FROM PILOT (pilot set disjoint from test set)`, same formula as n_min_H1 with theta2 and delta2.
  - Decision rule as H1 with the roles swapped. Not runnable until the concept-doc arm exists (dated amendment).
- Secondary (reported, no decision): the other strata, low-overlap and verbatim assistant-said strata, gold-latest hit, abstention precision, tokens injected per turn, and per-stratum Phase R tables.
- Multiple comparisons: H1 and H2 are each a single pre-registered primary contrast; secondary contrasts carry no claims of difference.

## 7. Statistics

- Single proportions (accuracy, hit rate, abstention precision, recall over questions): Wilson 95% interval (`emlab.metrics.wilson_interval`). Never a Wilson interval on a difference.
- Differences between arms: paired bootstrap resampling whole clusters (question clusters per Section 2: 495 clusters for 500 questions) with replacement, 10,000 resamples, fixed seed argument recorded in the output, percentile intervals (`clustered_paired_bootstrap`): two-sided 95% for reporting, one-sided 95% bounds for the H1 and H2 decisions.
- Effective n is the number of independent clusters (conversations), reported next to every interval; the number of questions is reported separately.
- Recall@k and NDCG@k are per-question means, so they enter the bootstrap as per-question values, not as proportions.
- Pilot and test sets are disjoint; tuning, thresholds and minimum n come from the pilot only.

## 8. Reporting

Every figure is reported with workload (dataset variant, stratum, n, effective n), hardware (host, CPU/RAM caps, Elasticsearch version and settings), baseline, target (the registered threshold) and result, plus commit hash of this repo, the frozen pre-registration hash and the run seed. Measured throughput or latency claims need representative load and are out of scope of this registration.

## 9. Threats to validity

- The benchmark haystacks are about 115k to 1.5M tokens, not multi-year histories: a result here says little about long-term (years) behaviour and nothing about durable concept owners (not exercised by the first slice).
- The data is partly synthetic (simulated user sessions and inserted evidence); distractors may be easier or stranger than real histories.
- LLM judge leniency and drift: mitigated by a fixed judge, a pilot human audit and reporting judge agreement; not eliminated.
- Baseline strength depends on tuning effort: equal pilot tuning budgets are registered, but a tuned baseline can still be weaker than a vendor's production system. Vendor-reported baseline numbers are not reproduced or used here.
- Vectors computed outside Elasticsearch deviate from the semantic_text path; results transfer to it only if the embedding model is the same.
- Distractor sessions are shared across pilot and test questions (19,195 unique sessions over 23,867 references): the pilot sees the same filler text the test set contains, so pilot-tuned settings are mildly optimistic for filler-heavy behaviour. Answer sessions are shared by 5 multi-question clusters; these are kept together in the split and resampled together.
- The judge (Opus) and the reader and extractor (Sonnet) are from the same vendor, which can bias judging towards the reader's style; the leniency probe measures leniency, not self-preference.
- Headroom: a micro-pilot (n=10, relayed by main) found no accuracy difference between the full S haystack and gold-only evidence, so answer-level accuracy gains on S are likely bounded near zero and S supports cost and latency claims; the pooled-history curve (Section 4.1) is the retrieval-side test of growth, and its answer-level reading is limited by contamination.
- This benchmark cannot test as-of filtering: its histories never contain superseded facts separated by more than a day from the question date. Testing it needs histories where superseded facts are separated by more than a day (the M variant or the user's own sessions).
- The in-process exact retriever used for the low-overlap pre-computation differs from Elasticsearch's analyzer; stratum membership may shift slightly once recomputed on the node.
- E5-small with a 512-token limit and chunking may under-represent long rounds; chunk max-pooling is a simplification.
- A single embedding model, reader model and judge model: conclusions are conditional on them.
- Pilot-derived thresholds are noisy when the pilot is small; this is why delta and n_min are derived from pilot variance and the outcome INCONCLUSIVE exists.

## 10. Open items (none resolved silently)

1. Private sessions: NOT YET SPECIFIED (Section 2.1).
2. Phase F token budget: `TO FIX`; after the pilot main gets a measured estimate (tokens per session for extraction, per question for reading and judging) so the user sees a real number before the full run.
3. delta1, delta2, n_min_H1, n_min_H2: placeholders until the pilot; the user must accept delta values in their own words before the test run.
4. The correction items in Section 2 (abstention gold lists, per-question dates, shared answer sessions) differ from the relayed facts; main to confirm the registered handling.
5. If the run stops after Phase R it is reported as `TARGET_MISMATCH` against the original slice.

## Amendments

(none)
