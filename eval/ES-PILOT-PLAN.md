# ES-pilot plan (V2 Part E rule-setting; pilot split only; zero model calls)

GoodQA goodqa#16096. This file is committed BEFORE any code or run of the fresh ES-pilot. Thresholds that this plan adds are committed alone in `ES-PILOT-THRESHOLDS.md` (next commit), before the run.

## Prior run, disclosed
An ES-pilot already ran under the previous owner (rule commit c7fdb34, script 5740b57, results f531200, `results/exploratory/es_pilot.json`, narrative in `EXPLORATORY.md`). It holds means only (no rows), skipped the cross-encoder, the planted needle, the node-health check, the wrong-query control and the chunk-hash verification, and its script wrote index settings on the shared node (`set_heuristic` PUTs `hnsw_filter_heuristic`). Its numbers are PRIOR evidence: they are never presented as this run. Outcome known before this plan: BM25 at H2 and H3 met the +/-0.03 mean clause and missed only the +/-0.05 interval clause at 90 clusters. Anything added below is therefore NOT blind to that outcome; the decision logic is written down in advance and the prior figures serve as a reproduction check (this run's BM25, dense and RRF must reproduce them to the printed precision, or the mismatch is explained before anything else is written).

## What is run
Pilot split, the 94 questions with gold (90 content clusters); levels H0 (own haystack), H1, H2, H3 with `pool_sets_pilot_v2.json` and `PoolContext.level_sessions`, as in the exact retriever. Arms, each as Elasticsearch candidate list versus the exact in-process list at depth 200 (chunk hits rolled up to rounds by best hit):
1. BM25: ES `match` on `text` (english analyzer) versus the exact BM25.
2. Dense: ES kNN on `vector` (bbq_hnsw, k 600, num_candidates 1,000, `rescore_vector` oversample 3, heuristic read back, never set) versus exact cosine.
3. RRF: client-side RRF k=60 over the two lists of the same engine, depth 200.
4. Cross-encoder: BAAI/bge-reranker-v2-m3 (local, chunk-max) over the top 20 of each engine's RRF (the registered `RERANK_TOP_N` 20), tail in RRF order. One process, one (qid, round) score cache, so identical candidates are scored once and a differing head is rescored. The count of heads that differ is reported.

Metric: session recall@10 (first 10 distinct sessions of the round ranking). Paired difference ES minus exact, 95% content-cluster bootstrap (2,000 resamples, seed 16096, `clustered_paired_bootstrap`). Round-level top-10 overlap `|top10 rounds ES and exact| / 10` per arm and level, as a descriptive companion.

## Elasticsearch targets and what may be sent
- Shared node `emlab-es-16096`, index `emlab-rounds-v1`: READ-ONLY. A guarded client allows only `GET` and `POST` to `<index>/_search` and `<index>/_count`, and `GET` of `<index>/_settings`, `<index>/_mapping`, `_cluster/health`, `_cat/indices`, `_cat/shards`, `_nodes/stats/thread_pool`; everything else raises before any bytes leave the process. A unit test shows the plain client would have sent a settings PUT and the guard refuses it. The `fanout` heuristic variant of the prior run needs a settings write and is NOT run here (stated in the report).
- Verification before the run: count 150,526; mapping and settings read; every chunk read back with `search_after` on `chunk_id` and hashed (id, round, session, text; vector bytes if readable) against `out/emb/chunks.jsonl` and `chunk_vectors.npy`. If it fails, the run does not happen on that node.
- Own node `emlab2-eval-es` (ES 9.5.6, loopback port, memory limit, security off because loopback-only and mine): a copy of the index from the same artifacts plus the planted needles. Needle results validate the code path and the filters at full corpus size, NOT the shared index (which cannot be written).

## Controls
- Heuristic read-back: GET `_settings` before and after; value recorded; never set.
- Analyzer-mismatch sensitivity (read-only queries, as in the prior run): `analyzer: whitespace` and `analyzer: keyword` on the BM25 query.
- Wrong-query control: every question searched with another question's text and vector, derangement across content clusters, seed 16096; gold and filter stay the question's own.
- Planted needle (own node): per question a needle round (question text plus a nonce; vector = the question's query vector) in its own session; present in the allowed ids it must rank 1 in BM25, dense and RRF; absent from the allowed ids it must not appear in the top 200.
- Node health (shared node): index health, count, deleted docs, uuid and settings hash unchanged before and after; every search response `timed_out` false and shards failed 0; no search-rejection increase; cluster status recorded with the reason.

## How the parity rule is judged
The rule under test is the one already committed (c7fdb34, V2 Part E): HOLDS iff |mean difference| <= 0.03 and the 95% interval lies within +/-0.05. This run applies it as written to every arm and level, and splits each failure into MEAN-FAIL (mean outside +/-0.03) and WIDTH-FAIL (mean inside, interval beyond +/-0.05). For each WIDTH-FAIL the interval half-width is projected to the test denominators 350 and 299 by sqrt(94/n) (ASSUMPTION: similar cluster structure). The recommendation categories and the numbers that choose between them are in `ES-PILOT-THRESHOLDS.md`. This task recommends; it does not edit `PREREGISTRATION-V2.md` and freezes nothing.

## Where it runs
t2-dev only, under `~/emlab2-16096/eval/` (code archive at the committed HEAD, outputs, a copied model cache so nothing is written under `~/emlab-16096`). `df -h /home/eric` before each data step (30 GB free at planning time; own index copy about 0.7 GB, model copy about 2 GB). Results come back to `results/exploratory/es_pilot2/`, stamped with the commit, and `es_pilot_score.py` recomputes every table figure from the rows. Own containers and data are removed after the copy-back is committed.

## Commit order
plan, thresholds (alone), guarded client plus tests, run scripts, results, `ES-PILOT.md`. The feature branch is pushed at every green step.
