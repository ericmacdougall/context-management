# ES-pilot results and recommended Part E parity rule

GoodQA goodqa#16096. Pilot split only (94 questions with gold, 90 content clusters); zero model calls; the locked test split was never read. Plan: `ES-PILOT-PLAN.md`; thresholds committed before the run: `ES-PILOT-THRESHOLDS.md` (4251c9d). Run code and stamp: commit ccf6b7478364ba367f7d71cd243922241fd6b426 (`results/exploratory/es_pilot2/stamp.json`; the tables below are rendered by `emlab/es_pilot_tables.py`, added after the run, from `score.json`). Raw rows: `results/exploratory/es_pilot2/rows.jsonl.gz` (every exact and Elasticsearch round list at depth 200 per question and level, plus the controls) and `ce.jsonl` (cross-encoder head orderings and scores). `python3 -m emlab.es_pilot_score <dir> results/exploratory/es_pilot.json results/pilot/curve_rerank_v2.json` rebuilds `score.json` from the rows: recomputed on the Mac (python 3.9) and on t2-dev (python 3.12) the two files agree to 1e-9.

This is a RECOMMENDATION. `PREREGISTRATION-V2.md` was not edited and nothing is frozen; main decides.

## 0. Prior run and what is new
An earlier ES-pilot (c7fdb34 rule, 5740b57 script, f531200 results, `results/exploratory/es_pilot.json`) already ran under the previous owner; it had means only. This run is a separate, fresh run. Its BM25, dense and RRF means and exact-retriever means reproduce the earlier run's figures (maximum absolute difference 2e-16), so the earlier numbers stand and this run adds: rows, the cross-encoder arm, round-level overlap, the wrong-query control, the planted needle, the node-health check, the chunk and vector verification, and a read-only guard (the earlier script PUT index settings on the shared node; this run could not). The prior outcome (BM25 at H2 and H3 missing only the interval clause) was known before the plan was written; the decision logic in the thresholds file was fixed before the run reads, and is mechanical.

## 1. Setup as run
- Shared node `emlab-es-16096`, ES 9.5.6, index `emlab-rounds-v1`, reached only through `GuardedEs` (allow-list: GET of health, cat, settings, mapping, count, node thread-pool stats; POST of `_search` and `_count` on that index). Request log of the run: 2,256 searches, 2 settings reads, 4 cat reads, 2 health reads, 2 node-stat reads, 0 refused; plus the verification pass (152 searches). No write, no settings change.
- BM25: `match` on `text` (english analyzer). Dense: kNN on `vector` (bbq_hnsw, k 600, num_candidates 1,000, `rescore_vector` oversample 3, filter heuristic acorn read back, not set). RRF: client side, k 60, depth 200. Cross-encoder: bge-reranker-v2-m3 over the top 20 of each engine's RRF, tail kept in RRF order, one score cache per question. Levels H0 to H3 with the registered pilot sets; `terms` filter at H0 to H2, `must_not` of the excluded sessions at H3. Metric: session recall@10, paired difference ES minus exact, 95% content-cluster bootstrap (2,000 resamples, seed 16096).

## 2. Controls
- Verification of the shared index (T4.1): PASS. 150,526 documents; every chunk id, round, session and text equals `chunks.jsonl`; the stored vectors equal `chunk_vectors.npy` as float32 (0 mismatches, 0 missing, 0 extra); text digest 3f7b198d... and vector digest 64933e42... equal on both sides (`verify.json`).
- Node health (T4.5): PASS. Before and after the run `emlab-rounds-v1` is green with 150,526 documents, 0 deleted, same uuid, identical settings hash, heuristic acorn; all 2,256 search responses had timed_out false and 0 failed or incomplete shards; search rejections 0 before and after. Cluster status was yellow both times because 5 shards of other indices (`.ds-logs-agent_builder...`, `.ds-traces-agent_builder...`, `.workflows-executions`, `.workflows-step-executions`, `sys-evo-task`) are unassigned; none belongs to our index, so the threshold treats it as acceptable.
- Read-only proof (T4.6): the request log contains only allowed classes; the guard has a unit test that fails against the plain client (the earlier script's settings PUT reaches it) and passes with the guard.
- Analyzer-mismatch sensitivity (T4.2): PASS. Whitespace analyzer at H0: -0.245 [-0.326, -0.163] (required at most -0.10); keyword analyzer: ES recall 0.000 at every level (required at most 0.05). The parity test is sensitive to a BM25 defect of this kind.
- Wrong-query (T4.3): PASS at every arm and level: ES wrong-query recall is at most 0.28 and at most 0.5 of the real-query recall (table below). The scorer additionally requires the real recall to be above 0, so the check cannot pass vacuously (this tightening is not in the thresholds file; it did not change any outcome). At H0 the wrong-query recall is near chance (about 0.22 to 0.28, haystacks have about 50 sessions) in both engines. Partners come from a different content cluster by derangement.
- Planted needle (T4.4), on OUR OWN node (`emlab2-eval-es`, copy of the artifacts with the same mapping, plus 94 needle rounds; 150,526 to 150,620 documents): PASS. With the needle's session allowed it is rank 1 for 94 of 94 questions in BM25, dense and RRF at every level (12 of 12 arm-level cells); with it excluded (`terms` at H0 to H2, `must_not` at H3) it appears in the top 200 for 0 of 94 in every cell. This validates the pilot's ES code path and filters at full corpus size; it says nothing about the shared index beyond what the verification shows, because that index cannot be written.
- Reproduction (T4.7): BM25, dense and RRF reproduce the earlier run within 2e-16 (met). The exact cross-encoder arm equals `curve_rerank_v2.json` at H0, H1, H2 exactly and differs by +0.0053 at H3 (half of one question's recall), which is above the committed tolerance of 0.005: NOT MET by 0.0003. Likely cause (unverified; the check that would settle it is rescoring with the original batch composition): batch padding differs between the earlier run and this one (here the union of both engines' heads is scored per question), which changes scores in the last digits and can flip a near-tie. It affects only the exact side's H3 cross-encoder cell.
- Short lists (T4.8): Elasticsearch BM25 returned fewer than 200 rounds for 18 of 94 questions at H0 (it returns only rounds that match a query term; the exact retriever pads with zero-score rounds); no other arm or level had a short list. They are scored as returned.

## 3. Parity per arm and level
Session recall@10 for the exact in-process retriever and Elasticsearch, the paired difference ES minus exact with its 95% content-cluster interval, the class under the committed rule (HOLDS iff |mean| <= 0.03 and the interval is inside +/-0.05; MEAN-FAIL; WIDTH-FAIL), the interval projected to the test denominators 350 and 299 as the observed half-width times sqrt(94/n) (ASSUMPTION: cluster structure like the pilot), and descriptive top-10 overlaps with the exact ranking (rounds and sessions).

| level | arm | exact | ES | diff ES-exact [95% interval] | class | projected interval at n=350 / 299 | round top-10 overlap | session top-10 overlap | short ES lists |
|---|---|---|---|---|---|---|---|---|---|
| H0 | BM25 | 0.946 | 0.946 | +0.000 [-0.017, +0.016] | HOLDS | [-0.009, +0.009] / [-0.009, +0.009] | 0.750 | 0.773 | 18 |
| H0 | dense kNN | 0.962 | 0.962 | +0.000 [+0.000, +0.000] | HOLDS | [+0.000, +0.000] / [+0.000, +0.000] | 1.000 | 1.000 | 0 |
| H0 | RRF 60 | 0.968 | 0.961 | -0.007 [-0.020, +0.000] | HOLDS | [-0.012, -0.002] / [-0.013, -0.002] | 0.835 | 0.862 | 0 |
| H0 | cross-encoder (top 20 of RRF) | 0.968 | 0.966 | -0.002 [-0.005, +0.000] | HOLDS | [-0.003, -0.000] / [-0.003, -0.000] | 0.889 | 0.860 | 0 |
| H1 | BM25 | 0.820 | 0.818 | -0.002 [-0.036, +0.027] | HOLDS | [-0.019, +0.014] / [-0.020, +0.016] | 0.683 | 0.672 | 0 |
| H1 | dense kNN | 0.774 | 0.774 | +0.000 [+0.000, +0.000] | HOLDS | [+0.000, +0.000] / [+0.000, +0.000] | 1.000 | 1.000 | 0 |
| H1 | RRF 60 | 0.859 | 0.862 | +0.002 [-0.019, +0.021] | HOLDS | [-0.008, +0.013] / [-0.009, +0.014] | 0.811 | 0.787 | 0 |
| H1 | cross-encoder (top 20 of RRF) | 0.888 | 0.878 | -0.010 [-0.044, +0.016] | HOLDS | [-0.026, +0.005] / [-0.027, +0.007] | 0.861 | 0.811 | 0 |
| H2 | BM25 | 0.655 | 0.668 | +0.012 [-0.032, +0.058] | WIDTH-FAIL | [-0.011, +0.036] / [-0.013, +0.038] | 0.665 | 0.671 | 0 |
| H2 | dense kNN | 0.551 | 0.547 | -0.004 [-0.022, +0.011] | HOLDS | [-0.012, +0.005] / [-0.013, +0.005] | 0.961 | 0.954 | 0 |
| H2 | RRF 60 | 0.684 | 0.673 | -0.012 [-0.037, +0.011] | HOLDS | [-0.024, +0.001] / [-0.025, +0.002] | 0.766 | 0.802 | 0 |
| H2 | cross-encoder (top 20 of RRF) | 0.703 | 0.685 | -0.018 [-0.046, +0.012] | HOLDS | [-0.033, -0.003] / [-0.034, -0.001] | 0.822 | 0.811 | 0 |
| H3 | BM25 | 0.494 | 0.510 | +0.016 [-0.022, +0.054] | WIDTH-FAIL | [-0.003, +0.036] / [-0.005, +0.038] | 0.601 | 0.606 | 0 |
| H3 | dense kNN | 0.345 | 0.334 | -0.011 [-0.033, +0.000] | HOLDS | [-0.019, -0.002] / [-0.020, -0.001] | 0.974 | 0.976 | 0 |
| H3 | RRF 60 | 0.475 | 0.483 | +0.008 [-0.021, +0.039] | HOLDS | [-0.008, +0.023] / [-0.009, +0.025] | 0.748 | 0.751 | 0 |
| H3 | cross-encoder (top 20 of RRF) | 0.533 | 0.526 | -0.007 [-0.050, +0.034] | WIDTH-FAIL | [-0.029, +0.015] / [-0.031, +0.017] | 0.790 | 0.772 | 0 |

| level | cross-encoder heads with the same top-20 set / same order (of n) |
|---|---|
| H0 | 6 / 0 (of 94) |
| H1 | 1 / 0 (of 94) |
| H2 | 3 / 0 (of 94) |
| H3 | 3 / 0 (of 94) |

| level | whitespace-analyzer BM25: ES recall, diff to exact [interval] | keyword-analyzer BM25 ES recall | wrong-query ES recall BM25 / dense / RRF (real ES recall) | wrong-query exact recall BM25 / dense / RRF |
|---|---|---|---|---|
| H0 | 0.701, -0.245 [-0.326, -0.163] | 0.000 | 0.219 (0.946) / 0.260 (0.962) / 0.276 (0.961) | 0.271 / 0.260 / 0.303 |
| H1 | 0.441, -0.379 [-0.477, -0.280] | 0.000 | 0.014 (0.818) / 0.013 (0.774) / 0.019 (0.862) | 0.021 / 0.013 / 0.031 |
| H2 | 0.254, -0.402 [-0.493, -0.311] | 0.000 | 0.000 (0.668) / 0.000 (0.547) / 0.004 (0.673) | 0.021 / 0.000 / 0.000 |
| H3 | 0.124, -0.370 [-0.458, -0.286] | 0.000 | 0.000 (0.510) / 0.000 (0.334) / 0.000 (0.483) | 0.000 / 0.000 / 0.000 |

recommendation: {"category": "KEEP", "complete_all_arms": true, "mean_fails": [], "width_fails": [["H2", "bm25", "WIDTH-FAIL"], ["H3", "bm25", "WIDTH-FAIL"], ["H3", "ce", "WIDTH-FAIL"]], "width_fails_still_outside_at_test_n": [], "tighten_0.02_0.04_supported": false}
control_checks: {"whitespace_H0_le_-0.10": true, "keyword_le_0.05_all_levels": true, "wrong_query_half_all": true}
reproduction_prior_es {"max_abs": 0.0, "within_0.002": true} ""
reproduction_prior_ce {"within_0.005": false} {"H0": 0.0, "H1": 0.0, "H2": 0.0, "H3": 0.005319148936170248}


Reading:
- No MEAN-FAIL anywhere in 16 cells. The largest mean difference is -0.018 (cross-encoder at H2); BM25 is +0.012 and +0.016 at H2 and H3 (ES stemming helps slightly), dense is within -0.011, RRF within -0.012.
- Three WIDTH-FAILs, all with means inside the band: BM25 H2 (upper bound +0.058), BM25 H3 (upper +0.054), cross-encoder H3 (lower -0.0502, over the line by 0.0002 at the third decimal). Under the sqrt(94/n) projection all three fall inside +/-0.05 at both 350 and 299 (largest projected bound +0.038).
- Dense kNN with `rescore_vector` oversample 3 is essentially identical to the exact dense retriever (round-level overlap 0.96 to 1.00); BM25 differs most in ranking (round overlap 0.60 to 0.75) yet not in the end metric. RRF overlaps 0.75 to 0.84. So overlap is not a usable parity criterion: it measures tokenizer and stemming differences that mostly do not move the session recall.
- The cross-encoder's input heads differ: the ES RRF top-20 has the same member set as the exact top-20 for only 13 of 376 (question, level) cases and never the same order, so the cross-encoder arm was rescored for nearly every case (the "reuse" shortcut of Part E would almost never apply). After rescoring its parity is similar to RRF's (mean differences -0.018 to -0.002).

## 4. Recommended Part E parity rule (for main to decide; evidence above)
Applying the thresholds file mechanically gives category KEEP: no MEAN-FAIL, and every WIDTH-FAIL projects inside +/-0.05 at both test denominators; the tighter band (0.02 mean, 0.04 interval) is NOT supported (BM25 H2 and H3 and the cross-encoder at H1, H2, H3 exceed 0.04 at the pilot interval, and the pilot cannot show otherwise).

Recommendation:
1. Keep the rule as written: replication HOLDS for an arm and level iff the paired mean difference to the exact retriever is within +/-0.03 and its 95% content-cluster interval is within +/-0.05. Apply it to BM25, dense, RRF and the cross-encoder at each of H0 to H3 (16 cells), as the pilot did.
2. Do not tighten and do not loosen. The pilot's largest mean difference (0.018) is inside the band with room, and no cell would need a looser mean clause; loosening was not asked for by any cell.
3. State in advance that the interval clause is the clause expected to bind at small n: on the pilot it failed in 3 of 16 cells by at most 0.008 while the means passed. Pre-state that a cell whose mean is inside +/-0.03 but whose interval leaves +/-0.05 at the test n is reported as "mean within band, equivalence not demonstrated" and is NOT reclassified as holding or as failing by any later change to the band.
4. Keep round-level overlap descriptive only (it is 0.60 to 0.75 for BM25 while the end metric agrees).
5. For the cross-encoder arm record, per question, whether the ES head equals the exact head; where it does not, rescore (as here); do not reuse the exact reranking.
Confidence: moderate. The recommendation is built on 94 questions in 90 clusters, a sqrt(n) projection of interval width with an assumed cluster structure, and a prior outcome known when the logic was written; the first test-split interval is the real check, not this projection.

## 5. Not run, and why
- The `fanout` filter-heuristic variant of the earlier run: it needs `PUT _settings` on the shared index, which this run may not do. The earlier run found it identical to acorn.
- Planted needle through the SHARED index: it needs a write; done on our own node (section 2) instead.
- Elasticsearch `rrf`/`linear` retrievers, inference endpoints, semantic_text and the E5 prefix check: out of scope for Basic features and external vectors (as in Part E).
- Timing: `took` and wall time were not summarised (shared, contended host); the cross-encoder ran at about 90 s per question on a loaded CPU, which says nothing about production latency.
- Anything on the test split, answer-level parts, or other corpora.

## 6. Reproduce
On t2-dev under `~/emlab2-16096/eval/` (code archive at ccf6b74; nothing written under `~/emlab-16096`): `emlab.es_verify`, `emlab.es_pilot2 retrieve`, `emlab.es_pilot2 rerank`, `emlab.es_needle build|needle` (own node), `emlab.es_pilot_score`. Tests: `cd eval && python3 -m unittest` (107 tests, 3 skipped, before the X2 changes).
