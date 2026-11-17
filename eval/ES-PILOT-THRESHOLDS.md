# ES-pilot thresholds and decision logic (committed alone, before the fresh run)

Companion to `ES-PILOT-PLAN.md`. GoodQA goodqa#16096. Pilot split, 94 questions, 90 content clusters, 0 model calls. Disclosure: the prior ES-pilot outcome (BM25 at H2 and H3 missing only the interval clause) was known when this was written; the rules below follow the plan, not the outcome, and are fixed before the fresh run reads any ES result.

## T1. The rule under test (unchanged, committed in c7fdb34)
For an arm at a level: HOLDS iff |mean paired difference ES minus exact| <= 0.03 AND the 95% content-cluster bootstrap interval (2,000 resamples, seed 16096) lies within [-0.05, +0.05]. Arms: BM25, dense, RRF 60 at depth 200, cross-encoder over the top 20 of RRF. Levels: H0, H1, H2, H3. Metric: session recall@10.

## T2. Failure classes and projection
MEAN-FAIL: |mean| > 0.03. WIDTH-FAIL: |mean| <= 0.03 and the interval leaves [-0.05, +0.05]. Projection to the test denominators n = 350 and n = 299 (ASSUMPTION: cluster structure like the pilot): half-width h(n) = observed half-width x sqrt(94 / n); projected interval = mean +/- h(n); it passes if inside [-0.05, +0.05].

## T3. Recommendation logic (applied mechanically to the rows)
- KEEP: no MEAN-FAIL in any arm and level, and every WIDTH-FAIL projects inside [-0.05, +0.05] at both 350 and 299. Recommendation: keep the rule as written.
- KEEP-MEAN-CLAUSE-AND-STATE-INTERVAL-LIMIT: no MEAN-FAIL, but some WIDTH-FAIL still projects outside at 350 or 299. Recommendation: keep the mean clause; for the listed arms and levels, state in V2 that the interval clause is not expected to be met by that arm and report it as "mean within band, equivalence not demonstrated" instead of widening the band.
- MEAN-FAIL: any MEAN-FAIL. Recommendation: the rule is not loosened to admit it; parity does not hold for that arm and level; Part E reports the Elasticsearch values separately and V2 must not quote the exact-retriever figure as an Elasticsearch result there.
- TIGHTEN (considered only when the category is KEEP): recommend a mean band of 0.02 and an interval band of 0.04 iff every arm and level also holds at 0.02 / 0.04 on the pilot and projects inside at both test denominators.
- Round-level top-10 overlap is descriptive only and is not part of the rule.
- Loosening is never recommended because an arm failed.

## T4. Controls and their pass criteria
1. Hash verification of the shared index before the run: 150,526 ids, each id's (round_id, session_id, text) equal to `out/emb/chunks.jsonl`; if the vector is readable from the stored source, equal as float32 to `chunk_vectors.npy`; zero mismatches, zero missing, zero extra. Otherwise the run does not use that node.
2. Analyzer-mismatch sensitivity (prior thresholds reused): `whitespace` BM25 at H0 paired difference to exact <= -0.10; `keyword` BM25 session recall@10 <= 0.05 at every level.
3. Wrong-query control: for BM25, dense and RRF at every level, ES wrong-query mean session recall@10 <= 0.5 x ES real-query recall. The exact retriever's wrong-query recall is reported beside it (information).
4. Planted needle on the own node (one needle per question, in its own session): with the needle's session allowed, the needle is round-rank 1 in BM25, dense and RRF for at least 93 of 94 questions at every level; with it not allowed (excluded by `terms` at H0 to H2, by `must_not` at H3), the needle appears in the top 200 for 0 questions in every arm and level. The control validates the code path and filters on a full-size copy; it does not claim anything about the shared index.
5. Node health on the shared node: before and after the run `emlab-rounds-v1` health green, docs.count 150,526, docs.deleted 0, index uuid and settings (as returned by GET) identical; every search response has timed_out false, shards failed 0, shards successful equal to total; no HTTP error; search thread-pool rejected count not increased. Cluster status is recorded; "yellow" is acceptable only if no unassigned shard belongs to `emlab-rounds-v1`.
6. Read-only proof: the guarded client records every request made (method, path class, count); the log shows only the allowed classes; any other request aborts the run.
7. Reproduction of prior figures: this run's session recall@10 means for BM25, dense and RRF at each level and the exact retriever's means equal the prior committed means (EXPLORATORY.md, es_pilot.json) within 0.002. The exact cross-encoder arm equals `results/pilot/curve_rerank_v2.json` rerank within 0.005. A mismatch is explained before any other figure is written.
8. Short lists (fewer than 200 rounds from an engine) are counted and reported, not dropped.

## T5. What counts as a failed run
Any control above not met means that control is reported FAILED and the parity numbers are reported with that caveat; no parity setting is tuned afterwards to change a difference.
