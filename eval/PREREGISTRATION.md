# Pre-registration: elastic memory lab evaluation

Ticket: GoodQA goodqa#16096. Design under test: `../DESIGN.md`.
Source of the work: user:73716864-6b3b-4deb-94ef-8ed2c8432e4a/b5d6a478-d70d-4516-8e08-839e60bbf5c7 "double check/finish the research and run the experiments".

Status: DRAFT. Freezing means recording the git commit hash of this file in the ticket before any run (`git rev-parse HEAD:eval/PREREGISTRATION.md`). After freezing, edits go in a dated "Amendments" section at the end, each with a reason and the data already seen; thresholds are never edited after the test set is first scored.

Labels: `ASSUMPTION` = not the user's words. `TARGET_MISMATCH` = result narrower than the original slice. `TO FIX FROM PILOT` = placeholder that must be replaced by a number derived on the pilot set by the stated rule, committed before the test run. Nothing in this file is a measured number.

## 1. Scope and what each phase can decide

| Phase | Needs | Decides |
|---|---|---|
| R: retrieval only | Elasticsearch + external embeddings, no LLM | Neither K1 nor K2 |
| F: facts and answers | LLM extraction and answer calls, same reader for all arms | K1 (and the evidence for K2 inputs) |

Phase R decides neither kill rule: both rules compare systems on extracted facts and on answers. If the run stops after Phase R the outcome is reported as `TARGET_MISMATCH` against the original slice (facts layer vs best tuned baseline on knowledge-update and temporal questions), with Phase R numbers labelled "retrieval-only, kill rules undecided". K2 (concept docs vs facts + retrieval) additionally needs a concept-doc arm that this document does not define yet; it is registered as H2 below but cannot be run until an arm is specified (Section 5).

## 2. Dataset

- LongMemEval is the intended public benchmark. `ASSUMPTION`: it is not the user's stated choice; the user's stated requirement is public data only.
- Everything about its schema in `emlab/dataset.py` (field names `question_id`, `question_type`, `question`, `answer`, `question_date`, `haystack_session_ids`, `haystack_dates`, `haystack_sessions`, `answer_session_ids`, per-turn `has_answer`, the `_abs` suffix for abstention, date string format, question-type names, the S/M variants and their sizes, license) is "to confirm from the dataset card". Which variant is used (S about 115k tokens per question, M about 1.5M tokens) is `TO FIX` after the confirmation and before data is sampled.
- Gold labels: session-level gold = `answer_session_ids`; turn-level gold = turns with `has_answer` (to confirm that every question type has them). Abstention questions have no retrieval gold: they are excluded from recall/NDCG and used only for abstention precision.
- Knowledge-update gold-latest: the gold evidence that carries the CURRENT value. Whether the dataset marks it, or whether `answer_session_ids` includes superseded sessions, is to confirm; if not marked, superseded ids are annotated by a documented rule fixed before the test set is read (the repo's fixture models this with an `x_stale_session_ids` annotation that is ours, not the dataset's).
- The fixture in `eval/fixtures/tiny.json` is hand-written, follows the recalled layout and is not evidence about the real files.

## 3. Sampling and splits (fixed before data is seen)

- Strata by dataset question type: single-session-user, single-session-assistant, single-session-preference, multi-session, knowledge-update, temporal-reasoning (names to confirm), plus abstention (identified by id convention, to confirm). Low-overlap is an overlay defined in 3.2, not a type.
- 3.1 Split: by question id, stratified by question type. Within a stratum, order ids by `sha256("16096:" + question_id)` and take the first `ceil(0.2 * n)` (at least 1, never all) as the pilot set; the rest are the test set (`emlab.dataset.split_pilot_test`, seed constant `SPLIT_SEED = 16096`, pilot fraction 0.2 is a proposed value, decision for main). The hash ordering does not depend on Python version or input order. If conversations share haystacks across question ids (to confirm), the split key becomes the haystack/conversation id instead, so no conversation appears in both sets.
- The pilot set is used for: tuning every arm, fixing thresholds and minimum n, and debugging. The test set is scored once per frozen configuration. Any test-set look before the freeze voids the freeze.
- Test-set size: the whole of the non-pilot questions for Phase R (no LLM cost). For Phase F, the per-stratum sample is the larger of the minimum n from H1/H2 and what the budget (TBD by the user) allows; if the budget is below the minimum n the result is reported INCONCLUSIVE, never narrowed silently.
- 3.2 Low-overlap stratum, operationally: run raw-question BM25 and dense retrieval over the question's full haystack (turns indexed individually, session rank = rank of its best turn, depth at least 200); a question is low-overlap iff the best-ranked gold session has rank greater than 100, or is absent, on BOTH (`emlab.metrics.is_low_overlap`, threshold 100). Computed with the same embedding model used in every arm, on the raw question text, with no time filter. Questions with no gold (abstention) are never low-overlap.

## 4. Phase R: retrieval only (no LLM)

Stack: Elasticsearch 9.5.x single node on t2-dev, localhost only, capped CPU/memory/disk (see RUNBOOK). Granularity: one document per turn with `qid`, `session_id`, `turn_id`, `role`, `session_date`, `text` (and `vector`); every query filters on its own `qid`. One embedding model, identical in every arm (name and revision `TO FIX` before the run); vectors computed outside Elasticsearch (labelled deviation from the semantic_text path). Fusion is client-side so no licensed feature is needed. Rerank uses an open cross-encoder (name `TO FIX`), top 50 of the RRF list.

Arms (each run with `time_aware=off` and `time_aware=on`):
1. BM25 (`match` on `text`)
2. Dense (kNN)
3. Hybrid RRF (`rank_constant` 60 unless pilot tuning picks another from a fixed grid {20, 60, 100})
4. Hybrid linear (min-max normalised, weight grid fixed in advance: dense weight in {0.3, 0.5, 0.7})
5. Hybrid + rerank

Time-aware: (a) hard filter `session_date <= question_date`; (b) temporal expansion for questions whose text carries a relative or absolute time expression (widen to the resolved window, never narrow below the as-of filter). The exact expansion rule is `TO FIX` and written down before the pilot; the scaffold implements only (a).

Metrics: session-level and turn-level recall@k and NDCG@k (binary gain) for k in {5, 10, 20, 50}, against gold ids; session ranking is derived from the turn ranking (best turn per session). Reported per stratum with the mean over questions that have gold, n and effective n alongside. Arm differences use the clustered paired bootstrap (Section 7), never a bare comparison of means.

Phase R is descriptive. It carries no pass/fail threshold; it selects the retrieval configuration (fusion, weights, rerank on/off, time-aware on/off) that Phase F uses, chosen on the pilot set only.

## 5. Phase F: facts and answers (needs LLM calls)

Reader model, extractor model, judge model, answer budget and token budget are identical across arms and `TO FIX` after the user's answers (not chosen here). Pull tools and token budget are the same in every arm.

Arms:
- A0 facts+hybrid: the design (facts with valid_from/valid_to, modality, trust; hybrid retrieval from Phase R's pilot-selected configuration; budgeted injection plus pull tools).
- B1 tuned time-aware hybrid RAG with session/round keys.
- B2 long context with compaction.
- B3 dated always-present observation log.
- B4 grep/open-by-id agent over raw history.
- B5 Atlas-style three-index memory, reproduced from public descriptions only. `ASSUMPTION`: no contact with Elastic; a faithful reproduction may not be possible, in which case B5 is reported as "approximation, labelled" or dropped with a stated reason before the run. Vendor-reported numbers are not used as results.
- Concept-doc arm (for K2): not defined here. Registered so K2 is not forgotten; it needs its own definition committed before any Phase F run that includes it.
- Every baseline gets the same tuning budget on the pilot (number of configurations `TO FIX`).

Metrics:
- Answer accuracy by stratum (judge-graded; judge model fixed, plus a human-audited sample on the pilot to estimate judge leniency).
- Retrieval gold-latest hit rate: for knowledge-update questions, a hit means the injected or retrieved context contains the evidence for the CURRENT value; context containing only the superseded value counts as a MISS.
- Abstention precision: of the questions where the system abstains, the fraction that are true abstention questions; abstention recall is reported next to it.
- Tokens injected per turn (mean, p95), measured from logs of injected ids and block size.

## 6. Hypotheses and thresholds

Primary strata for H1: knowledge-update and temporal-reasoning, pooled with equal weight per stratum.

- **H1 (kill rule K1).** A0 beats the best tuned baseline (best by pilot score among B1-B5) on the primary metric, the pooled gold-latest hit rate and answer accuracy on the primary strata, by at least delta1.
  - delta1 = `TO FIX FROM PILOT (pilot set disjoint from test set)`. Rule: the smallest absolute difference that is larger than the pilot's measured noise floor (half-width of the clustered paired-bootstrap interval for the same arm run under two seeds or prompt orderings) AND that the user accepts as worth the facts layer's added complexity. The user's acceptance is a decision sourced in the user's own words; absent it, delta1 stays unfixed and the test run does not start.
  - Minimum sample: n_min_H1 = `TO FIX FROM PILOT (pilot set disjoint from test set)` independent conversations in the primary strata. Rule: n = ((z_alpha + z_beta) * sd_d / delta1)^2 using the pilot standard deviation sd_d of the per-cluster paired difference, with alpha = 0.05 one-sided and power 0.80 (proposed conventions, decision for main). If the test set offers fewer effective conversations than n_min_H1, the outcome is INCONCLUSIVE regardless of the point estimate.
  - Decision: facts layer KEPT only if the lower bound of the 95% clustered paired-bootstrap interval of (A0 minus best baseline) is above delta1 on the primary metric. It is KILLED if the upper bound is below delta1. Otherwise INCONCLUSIVE (one pre-declared extension to a larger test sample is allowed if budget exists; otherwise reported as inconclusive).
- **H2 (kill rule K2).** Concept docs beat facts+retrieval by at least delta2 on the stratum where concept state is claimed to help (multi-session; stratum choice `TO FIX` before the pilot, because it must follow from the concept-doc design).
  - delta2 = `TO FIX FROM PILOT (pilot set disjoint from test set)`, same rule as delta1.
  - n_min_H2 = `TO FIX FROM PILOT (pilot set disjoint from test set)`, same rule as n_min_H1.
  - Decision rule as H1 with the roles swapped. Not runnable until the concept-doc arm exists.
- Secondary (reported, no decision): the other strata, low-overlap and verbatim assistant-said strata, abstention precision, tokens injected per turn, and per-stratum Phase R tables.
- Multiple comparisons: H1 and H2 are each a single pre-registered primary contrast; secondary contrasts carry no claims of difference.

## 7. Statistics

- Single proportions (accuracy, hit rate, abstention precision, recall over questions): Wilson 95% interval (`emlab.metrics.wilson_interval`). Never a Wilson interval on a difference.
- Differences between arms: paired bootstrap resampling whole clusters (conversation or, if each question has its own haystack, question) with replacement, 10,000 resamples, fixed seed argument recorded in the output, percentile 95% interval (`clustered_paired_bootstrap`).
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
- Per-question haystacks may overlap across questions, so clusters may be fewer than questions; the effective n accounts for this once the structure is confirmed.
- A single embedding model, reader model and judge model: conclusions are conditional on them.
- Pilot-derived thresholds are noisy when the pilot is small; this is why delta and n_min are derived from pilot variance and the outcome INCONCLUSIVE exists.

## 10. Open decisions (for main and the user; none has been silently resolved)

1. Dataset choice and variant (LongMemEval is an ASSUMPTION).
2. Embedding model, reranker, reader, extractor and judge models, and the Phase F budget.
3. Pilot fraction (0.2 proposed), alpha and power (0.05 one-sided and 0.80 proposed).
4. User acceptance of delta1 and delta2 in the user's own words once the pilot noise floor is known.
5. Definition of the concept-doc arm and whether K2 is in this phase at all.
6. Whether B5 is reproduced or dropped.
7. If the run stops after Phase R: confirm it is reported as `TARGET_MISMATCH`.

## Amendments

(none)
