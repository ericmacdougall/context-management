# Licence transition rehearsal: thresholds (committed before any counted run)

Ticket goodqa#16096. Node: own container emlab-es-lic-16096, Elasticsearch 9.5.6, single node, security on, HTTP loopback only.
Source: user "keep going until everything is proven" (brief from main); thresholds are mine, labelled ASSUMPTION where they bound behaviour.

Documented APIs (read 2026-11-16, https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-license-post-start-trial and
.../operation-license-post-start-basic): start_trial needs `acknowledge=true`, is allowed once per major version per cluster
(after it: `trial_was_started:false`); start_basic needs `acknowledge=true` when features are dropped and cannot run while on basic.
So: one start_trial per fresh volume. "Licence returns" is rehearsed on a fresh Basic volume that then starts its trial.

## Lives (each on a fresh data volume)
- Life A (down): fresh Basic, start_trial, fixtures on trial, baseline on trial, start_basic under load, probes, exp 3 operations, restart on Basic, second start_trial recorded.
- Life B (up): fresh Basic, load running on Basic, start_trial under load, latch flips back.
- N = 5 lives of A and 5 of B (query order and seed permuted per life; the seed is recorded). One B life uses the adapter's DEFAULT probe TTL (300 s); the others use probe_ttl_s = 5 s (parameter).
- Results are reported as worst case over lives and as a Wilson lower bound where a rate is claimed.

## Experiment 1: feature matrix
Cells: BM25 match; dense kNN; kNN with rescore_vector (int8_hnsw); bbq_hnsw index and doc; bbq_disk mapping and doc; client-side fusion;
retrievers rrf and linear; text_similarity_reranker (licence gate only: a nonexistent endpoint id, so "licence passed" shows as 404 endpoint-not-found);
ES|QL MATCH, FORK, FUSE; inference endpoint create (custom service to the loopback stub), list, use; semantic_text mapping, index, semantic query,
match on the semantic_text field; document-level security role create and search by the DLS user; lexical percolator; alias with
require_alias and must_exist; force merge. Columns: basic (fresh), trial, at start_basic, after, and second start_trial.
- EXP1-T1 (determinism): every cell classified AVAILABLE / LICENCE_ERROR (HTTP 403 or 400 and "non-compliant") / OTHER_ERROR with exact status and error type; OTHER_ERROR on a cell that is expected to be licence-gated is a finding.
- EXP1-T2 (moment): licensed probes (rrf, linear, semantic query, inference use, DLS search) are polled every 50 ms across the start_basic call. Bound (ASSUMPTION): the first licence refusal arrives no later than 5 s after the start_basic response, and no probe returns success after its first refusal.
- EXP1-T3 (latch shape): for every LICENCE_ERROR cell of a search path the adapter's `_is_licence_error` must be true for the error text the adapter would build ("HTTP <code>: <first 400 bytes>"). A false is a finding (latch would not flip).
- EXP1-T4 (DLS fail-closed): after start_basic a search as the DLS-restricted user must return an error; returning any document is a FAIL (isolation). Control: the same search before the downgrade returns only the allowed documents (witness: allowed count > 0 and < total).

## Experiment 2: live degrade (memcore ElasticsearchSearch, strategy auto, shared instance across reader threads)
Workload: 8 auto readers, 2 auto+linear readers, 4 control readers (strategy backend), 2 lexical-only and 2 vector-only readers, 3 plain writers, 1 semantic_text writer. Read index is frozen (static, 1 shard, 1,500 docs, 8-dim stub vectors); the writers use separate indices.
- EXP2-T1 no wrong-but-successful answer: for every response labelled client the ids equal the independently computed client RRF (own code, own raw queries, rank constant 60, windows 100) for that query; labelled backend, equal to the raw rrf retriever answer recorded on trial for that query (ties compared as groups). Bound: 0 mismatches over all responses, and responses examined >= 20,000 per life.
- EXP2-T2 no outage: failed auto/lexical/vector read requests (anything not a result) <= 0.1% of requests; the longest run of consecutive failed auto requests (by time, any thread) <= 2.0 s. Expected 0 because the adapter falls back inside the call.
- EXP2-T3 latch down: first auto response labelled client no later than 5.0 s after the start_basic response (life A). Latch up (life B): first auto response labelled backend no later than probe_ttl_s + 5.0 s after the start_trial response (TTL 5 s => 10 s; default TTL => 305 s).
- EXP2-T4 no lost write: every acknowledged write id is present after a final refresh; acknowledged-but-absent = 0. Unacknowledged-but-present and timeouts are counted separately and reported. The semantic_text writer fails loudly on Basic: every failure carries a 403 licence error or 404 for a missing endpoint, none acknowledged, none present.
- EXP2-T5 control: arm strategy=backend after start_basic: >= 95% of requests (after the first 1 s) fail with the licence error; a control below 95% invalidates the run.
- EXP2-T6 recorded, not gated: fraction of queries whose top-10 changes between trial backend fusion and Basic client fusion, for the rrf arm and for the linear arm (a linear-to-RRF fallback changes the ranking function silently).

## Experiment 3: data written with a licensed feature
Indices created and filled on trial: semantic_text (stub endpoint, 200 docs, single chunk), bbq_disk (64 dims, 300 docs), bbq_hnsw (64 dims, 300 docs), plain float (control).
After start_basic: get, search (lexical, knn / semantic, match on the semantic_text field), full-document overwrite, partial update, bulk partial update, scripted update, delete, update_by_query, reindex, force merge, node restart and re-read.
- EXP3-T1 every operation either succeeds with the same observable result as on trial, or fails loudly with a licence error. A 200 whose result differs from the trial baseline (hits, fields, embeddings present) is a silent loss/finding; counted and listed.
- EXP3-T2 migration recipe to a Basic-safe mapping: doc counts equal source (300/300 and 200/200), vectors equal element-wise to the stored source vectors (exact for float _source copies; semantic_text recipe: stored chunk embeddings equal the migrated vectors, and within 1% relative of the stub's float vectors), kNN self-hit top-1 >= 99% on the migrated index, and the migrated index passes every Basic-available operation (search, update, delete, force merge).
- EXP3-T3 control: the same recipe applied to the plain index changes nothing (counts and vectors equal); and a deliberately corrupted migration (one vector perturbed) must be caught by the vector-equality check.

## Experiment 4: start-up probe
States: fresh Basic, trial, after start_basic, node restarting (polled every 100 ms from docker restart until /_license answers 200), plus stub unit cases (expired, inactive, enterprise, platinum, 404, 503, malformed).
- EXP4-T1 safe in every state: the probe never reports tier enterprise unless the node read-back says an active trial/enterprise licence; on error it reports basic with probe_error set; on a Basic node it issues no licensed call (0 calls to _inference or retrievers recorded by the server-side request log of the stub transport).
- EXP4-T2 re-probe: a service that started while the node was unreadable must reach the correct tier within 10 s after the node answers. The reviewed `capabilities.apply` pins `strategy=client` permanently, so the expected result is FAIL for the reviewed code (control) and PASS for licence/reprobe.py (the proposed re-probing wrapper). A control that does not fail invalidates the claim.

## Evidence rules
Every result file carries: code commit, UTC time, parameters, node licence read back from the node (type, status, uid), host load, witness counts. Control arms listed beside every PASS. Artifacts are stored on t2-dev ~/emlab-16096/lic/results/ (bulky) and small summaries in licence/results/.

## Addendum A (committed after two smoke lives, before any counted life; smoke results are not counted)
- Harness deviation, recorded in every result: the reviewed `http_util.urlopen` loads the system CA store on every call (measured by cProfile, 21 ms of GIL-held CPU per request, one process capped near 45 requests/s). The workload patches the adapter's opener to one cached no-redirect opener. Latch, fusion and request shapes are unchanged. The unpatched cost is reported as a finding.
- EXP2-T1 control (sensitivity of the wrong-answer check): arm `brk` is an adapter whose Basic fallback silently returns lexical-only results labelled fused. Floor: the check flags >= 90% of its client-labelled responses. Below 90% the T1 PASS is CLAIMED, not proven.
- EXP2-T4 control: the write verifier is self-tested by injecting one acknowledged-but-absent id; it must report it.
- Workload threads: 8 auto, 2 auto+linear, 4 control (backend), 2 broken-fallback, 2 lexical-only, 2 vector-only readers; 3 plain writers; 1 semantic_text writer; 1 latch sampler (100 ms).
- Vectors are read with `_source.exclude_vectors=false` (Elasticsearch 9 omits dense_vector from _source by default); vector equality compares float32 values as returned by the node, and the ground truth (the stub embedding) within 1e-6 absolute.
- Cluster node counts: `_cat` doc counts for semantic_text include chunk documents; document counts use `_count`.

## Disclosures and Addendum B (committed before the second supplemental life, down_7)
- Timing disclosure: the first version of this file was written after one exploratory trial-to-Basic flip on a throw-away node (it showed `semantic_text` reads, the 403 on update, and `?q=` returning 200 with no hits). The thresholds were not tuned on counted data, but they were written knowing those shapes.
- The unrefreshed-partial-update test in down_6 was not a clean test: its fixture was written before a node restart on trial, and a restart refreshes the shard. down_6 is reported as "not reproduced, n=1, not clean".
- `summarize.py` re-applies thresholds to the figures the harness computed. It is NOT an independent recomputation. `recompute.py` (new) recomputes T1, T2, T3, T5 and the polling figures from raw rows, but needs saved oracle legs and per-probe times, which only down_7 and later lives carry. For lives 1 to 6 the T1 and per-probe figures are harness-computed.
- Docs read 2026-11-16: https://www.elastic.co/docs/deploy-manage/license/manage-your-license-in-self-managed-cluster says an expired licence "reverts to Basic". Whether that is identical to `start_basic` (features, messages, indices) is not documented and was not tested.

Addendum B checks (life down_7, a fresh volume, post 70 s):
- EXP3-T4 lexical-looking queries on a semantic-only index (`lic3-semonly`: the ONLY text is in a `semantic_text` field; 30 docs, 15 mention "paris"): `?q=paris`, `?q=body:paris`, `query_string` (fields and default), `simple_query_string`, `multi_match`, `match`, `semantic`, `match_all`. Pass: after start_basic each form either returns the same total as on trial or a loud licence error. A 200 with a different total is a silent wrong answer (finding). Control: on trial `semantic`, `match` and `match_all` must return hits > 0 (witness).
- EXP3-T5 unrefreshed partial update, clean version: 20 documents written on trial with `refresh_interval=-1` AFTER the trial restart; before the update `searchable_before_update` must be 0 (proves unrefreshed); bulk partial update (tag only) right after the start_basic response with no restart in between. Pass: tags updated 20 of 20 and stored embeddings present 20 of 20, on the twin (trial) and on Basic. Any loss is a finding. n=1 life, 20 documents, stub endpoint, so a pass is "not reproduced", never "ruled out".
- EXP2/EXP1 raw evidence: oracle legs, backend oracles and per-probe start/end times are saved; `recompute.py` must agree with the harness on T1 examined and mismatches, T2, T3 for down_7 (a disagreement invalidates the harness figure). Moment claims use the probe START time: "refusal observed by a probe that started X s after the start_basic response" and "last success by a probe that started Y s after".
- Every result carries `harness.http_patch`.
