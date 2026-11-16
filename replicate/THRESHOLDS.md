# Replicate stream: thresholds committed before any run

Ticket: GoodQA goodqa#16096. Stream: REPLICATE (non-author reruns). Written 2026-11-16 before any replication script ran.
Every result file under replicate/results/ is stamped with the commit of this file's first version, its own script
commit, UTC time, parameters and an environment fingerprint (ES version, licence type/uid/expiry, node count, index doc
count, host load average, Python version). A row's verdict follows the mapping written here; nothing is re-tuned after
seeing data. Any deviation is written in the result file and in REPORT.md as a labelled DEVIATION.

Verdicts: REPLICATED (owner figure reproduced within tolerance AND the control fails for the stated reason above its
floor), PARTLY (part reproduced, or reproduced but the control is under its floor), NOT REPLICATED (outside tolerance, or
the control does not fail, which makes the original claim vacuous as stated), COULD NOT RUN (with the reason).

Standing conditions:
- Host t2-dev is shared (load average 20 to 27 on 48 cores when this was written). Every timing figure is INDICATIVE;
  a timing row's verdict rests on its structural control, never on milliseconds alone.
- No model calls of any kind (task instruction). Rows that need an embedder or a cross-encoder at run time are
  COULD NOT RUN here; stored vectors computed earlier by the owner may be read (reading stored vectors is not a call).
- System under test vs harness: where a row is a property of the owner's system code (memsys/, evolution/ at the cited
  commit), that code is imported read-only from `git archive <sha>`; the driver, workload, tamper steps, oracle and
  checker are written here. Owner experiment scripts (exp/*, scen*.py, evolution/oracle.py, escompare*.py) are read,
  never imported.
- Shared Elasticsearch emlab-es-16096 (trial): read only, except indices/templates prefixed `rep16096-`. The client
  guard refuses any other write. `index.dense_vector.hnsw_filter_heuristic` on emlab-rounds-v1 is read back and
  recorded, never set.

## L. Licence matrix (3.14.1, 3.14.2, 3.14.4, plus 3.7.9)

Nodes: (B) own emlab-es-rep-16096, default Basic licence, verified by `_license` before every cell; (T1) the same node
after `POST _license/start_trial` (DEVIATION from "Basic only": gives a same-node trial control without writing to the
shared node); (T2) shared emlab-es-16096 trial, only cells that need no non-`rep16096-` write.
Cell classes: AVAILABLE (round trip done: created, written, read back with a non-empty witness), LICENCE_ERROR (exact
reason string kept), OTHER_ERROR (any other error; counts as untested, never as available).
Features: BM25 match; standard retriever; knn (top-level and retriever) on plain float dense_vector; rescorer retriever;
pinned; diversify MMR; rescore_vector on bbq_hnsw; bbq_hnsw and int8_hnsw index + query; bbq_disk index + query;
client-side fusion (two legs fetched, fused in the client); rrf retriever; linear retriever; legacy rank.rrf;
text_similarity_reranker; inference API (PUT endpoint, GET list); semantic_text (mapping + index one doc);
ES|QL MATCH, FORK, FORK+FUSE, FUSE LINEAR, LOOKUP JOIN, RERANK; percolator (term query stored and matched; knn query
stored = must be rejected); document-level security (role with DLS query, user searches); data streams (template,
create, write, read); aliases with must_exist (remove) and require_alias (bulk write through alias; and to a missing
alias = must fail); force merge (to 1 segment, segment count read back).
No inference is ever executed: inference cells use a deliberately nonexistent model/endpoint id, so a licence error
versus a not-found error discriminates the licence gate without running a model. Preconfigured endpoints are listed
(GET) only.
Thresholds:
- 3.14.1 REPLICATED iff every feature it names (standard, knn, rescorer, pinned, MMR, ES|QL MATCH, FORK, LOOKUP JOIN)
  is AVAILABLE on B. Any LICENCE_ERROR = NOT REPLICATED for that feature.
- 3.14.2 REPLICATED iff every feature it names (rrf, linear, text_similarity_reranker, FUSE, ES|QL RERANK `[inference]`,
  DLS, bbq_disk indexing with mapping accepted and each document failing) is LICENCE_ERROR on B AND is not
  LICENCE_ERROR on T1 (control: the same request body passes the licence gate on a trial). A feature that is
  LICENCE_ERROR on both is NOT REPLICATED (the probe, not the licence, would be refusing).
- 3.14.4 (DESIGNED-UNTESTED today): this run supplies the artifact; result stated as AVAILABLE or LICENCE_ERROR on B.
- 3.7.9 REPLICATED iff a stored knn percolator query is rejected on B and T1 AND a stored term query is accepted and
  matches (control).

## P. Elasticsearch parity (DESIGN 1.9 and 3.2.11; 3.2.14)

Items: the 102 pilot question ids listed in the owner's es_round_parity.json (ASSUMPTION: the owner's item list is
taken as the item definition; it is an input, not a result). Haystack = the question's own sessions (H0). Round ids
are `<session>:<round index>`; chunk hits roll up to rounds by first occurrence (ES) or by max score (exact).
Exact BM25 is re-implemented here from the written spec (lowercase `[a-z0-9]+`, 33-word stoplist, no stemming, k1 1.2,
b 0.75, idf ln(1 + (N - df + 0.5)/(df + 0.5)), N, df and avgdl over the whole 150,526-chunk corpus).
- P1 (owner 0.751): mean top-10 overlap of ES BM25 on shared emlab-rounds-v1 versus my exact BM25. REPLICATED iff
  |mine - 0.751| <= 0.02.
- P2: same against my node's `english` field over the full 150,526 chunks; expected |P2 - P1| <= 0.02 (node
  independence; reported, not a verdict gate).
- P3 (mechanism, diagnostic): my node, field analysed to match the exact tokeniser (pattern `[a-z0-9]+`, lowercase,
  the same 33 stopwords). Prediction: >= 0.95. If it holds, the analyzer explains the 0.751 gap.
- C-P (control, the task's mismatched analyzer): my node, `whitespace` analyzer (no lowercasing, no stopwords) over the
  same corpus. Must DROP: mean overlap <= P1 - 0.10 AND lower than P1 on at least 10% of questions. If it does not drop,
  the parity metric is insensitive and P1 is NOT REPLICATED.
- P4 (owner 0.817 rrf, 0.863 linear): client RRF k 60 (and min-max linear 0.5/0.5) over my exact BM25 top-200 and exact
  cosine top-200 round lists versus ES `rrf` / `linear` retrievers (rank_window 200, knn num_candidates 1000) on the
  shared trial node at the heuristic in force (read back). REPLICATED iff |mine - owner| <= 0.03 for each.
- P5 (diagnostic): client RRF over ES's own BM25 and kNN legs versus ES `rrf`: prediction >= 0.95.
- 3.2.11 (owner: no-rescore loss up to about 0.08 session recall@10 at H2; with oversample 3 on bbq_hnsw every loss
  interval includes 0). On my node: chunks indexed with bbq_hnsw and int8_hnsw, heuristic acorn set on MY index. H2 =
  own haystack + the owner's DL list (3,900 sessions, pool_sets_pilot_v2.json) minus sessions whose content equals an own
  session or that hold a round whose content equals a gold round (my own content hashing). Non-abstention pilot
  questions with gold (n stated in the result). Hybrid = RRF 60 over exact BM25 top-100 rounds and a dense leg of the
  top-100 chunks rolled up by first occurrence. Configs: {bbq, int8} x num_candidates {100, 1000} x rescore {none,
  oversample 3}. Paired cluster-free bootstrap over questions (2,000 draws, seed 16096) of ES-leg minus exact-leg.
  REPLICATED iff (a) the worst no-rescore mean loss is in [0.04, 0.12] and at least one no-rescore CI excludes 0, AND
  (b) every bbq oversample-3 CI includes 0. Control (must fail): a dense leg from a fixed random unit vector per
  question must lose at least 0.10. Sensitivity: rescore none must be below oversample 3 for bbq at nc 100.
- 3.2.14 (E5 prefixes): COULD NOT RUN by rule (needs E5 inference on the node or locally).

## V. Visibility and read-after-write (3.2.3, 3.2.4, 3.1.10)

- 3.2.3 (owner p50 1,001 ms over 200 writes, default refresh): my node, 1 shard, refresh_interval unset, 200 writes,
  each polled by term search until visible. Claim holds iff p50 in [500, 1500] ms (INDICATIVE timing) AND realtime GET
  finds 20 of 20 immediately while an immediate search finds at most 2 of 20. Control: refresh_interval 30s index, 30
  writes, poll up to 3 s: at least 90% must still be invisible at 3 s (refresh drives visibility).
- 3.2.4 (owner 100 of 100; 600 of 600 during pause): owner's memsys reader and store at a13b524, my Postgres and my ES.
  (a) 100 append-then-immediate-search trials with the tail on: found 100 of 100 required. Control: same with tail off:
  at most 10 of 100 found (projection has not caught up), else the trial does not exercise the tail.
  (b) Projector stopped, 600 events written, tail (bound 2000) finds 600 of 600; control bound 300 finds at most 300 and
  flags truncation.
- 3.1.10 (owner: probe found through the tail while a 12 s transaction is open; catch-up 0.73 s): hold a transaction
  with an assigned xid open 12 s in the SAME database; append a probe; the tail reader must find it during the hold;
  search-only (tail off) must NOT find it during the hold (control). Catch-up after commit reported INDICATIVE.
  Extension (the limit the row states): the same hold in a SECOND database of the same cluster; record whether
  projection of the probe stalls (prediction: yes, xmin is cluster-wide). Reported as a finding, not a verdict gate.

## H. Hash chain (3.1.1)

Owner memsys at a13b524 (store.append_events, store.session_rows, chain.verify_chain), my Postgres.
- 8 writer processes x 250 appends on one session: verify_chain ok, seq 1..2000 gapless, 2000 distinct payloads.
- Tamper tests, each must make verify fail at the stated seq: (t1) UPDATE content_enc of seq 700; (t2) DELETE seq
  900 (gap); (t3) swap content of seq 1000 and 1001. (t4) INSERT a duplicate (tenant, session, seq) must be refused
  by the database. (t5) INSERT seq max+5 (gap at the end) must fail verify.
- Control (must fail for the stated reason): a verifier with the hash comparison removed (seq and prev link only
  from the stored hash column) PASSES t1, showing detection comes from the hash.
- Recorded, not a gate: a full re-hash of the chain after t1 (attacker recomputes every hash) passes verify, i.e. the
  chain alone does not detect a writer with database access (the anchor of 3.1.6 is still needed).
REPLICATED iff all of the above hold.

## Q. Quote and speaker check (3.4.2, 3.4.3)

Inputs read from the owner's files: phasef-out/facts_v3_raw.jsonl and phasef-out/gate16_s.json; my own checker
written from PROVENANCE_CHECK.md at 1a62931 (not provenance.py). Round = a user turn plus the following non-user turns;
round text = turn contents joined by one space; normalisation = collapse whitespace + lowercase.
- 3.4.2 (owner 2,351 of 2,373): REPLICATED iff my count equals 2,351 exactly (same rule, same inputs). Also reported:
  strict byte-exact count. Controls with my own mutator (seed 16096): one word replaced, adjacent round, author flipped;
  each must be rejected 100% (excluding the stated legitimate exceptions, reported).
- 3.4.3 (owner 8 mismatch, 53 ambiguous): REPLICATED iff my counts equal 8 and 53.

## D. Decisions, task amendments, concept queue (3.5.1, 3.12.1, 3.5.2, 4.2)

Owner evolution/ and memsys/ at a13b524 imported read-only; my scenario driver and my oracle written from the DESIGN
rule text, not from evolution/oracle.py.
- 3.5.1 / 3.12.1: for N >= 200 generated amendment scenarios (my generator, seed 16096): after an amendment event,
  the next view shows the new definition as current and the old one as superseded, and earlier steps are classified
  still valid / needs rework / obsolete by my oracle's rule. REPLICATED iff 0 disagreements. Controls (each must
  produce disagreements in >= 10% of scenarios): (c1) a classifier that marks every step still valid; (c2) a view
  that ignores the amendment event.
- 3.5.2 (owner fence-off controls 74 and 71 of 200): 200 runs per race with an overlap witness (the amendment commits
  while the step is between read and commit, counted) and a stale-token writer; torn state 0 with the fence; the
  fence-off variant must commit stale state in >= 10% of runs.
- 4.2 (owner: 0 overlapping folds, lease-expiry case rejected): concept queue under 8 workers; overlapping folds of one
  concept 0; a late commit by an expired lease holder rejected. Control: workers claiming without the queue key
  (no lease) must produce overlapping folds in >= 10% of hot-concept windows.
If the owner code exposes no seam to build a control without editing it, the row is PARTLY with the reason.

## O. Other rows

- 3.7.8 (owner: the day rule removes 0 gold sessions): recount from the raw dataset (not verify.json). REPLICATED iff
  the day rule removes 0 gold sessions over non-abstention questions AND the control (timestamp rule) removes > 0
  gold sessions. Also reported: non-gold sessions the day rule removes (if > 0, "cannot change a ranking" is too
  strong: removing a non-gold session above gold changes ranks).
- 3.9.1 (core depends on no vendor), memcore at c20e208: core = memcore/*.py excluding adapters/, hosts/, stubs/,
  remote/, tests/. Static import graph (ast): no core module imports a vendor SDK (anthropic, openai, elasticsearch,
  elastic_transport, boto3, google, temporalio, httpx-based vendor clients) or memcore.adapters/hosts/remote, directly
  or transitively through core. Control: a mutant copy with one vendor import added to a core module must be flagged.
  REPLICATED iff 0 violations and the mutant is flagged.
- 3.7.5 (rerank 64 s per question): COULD NOT RUN by rule (cross-encoder inference).
- 3.3.16 (40 erasures took 254 s) and 3.2.5 (blue/green, dropped bucket): run only if time remains after the rows
  above; otherwise COULD NOT RUN (time), stated.

## Lints (2.1.4, 2.2.8)

- Tag lint (2.1.4): for every DESIGN.md row with an evidence tag, check (i) the tag is in the closed set, (ii) every
  cited artifact path/commit exists (`git cat-file -e <sha>:<path>` in the owner worktree that holds the sha), (iii)
  where LEDGER has a row for the same idea, the LEDGER class is compatible with the DESIGN tag (REVIEWED vs
  owner-reported etc.). Known positives it must rediscover: DESIGN section 6 items 10 and 11. A planted DESIGN row
  citing a nonexistent artifact must be flagged.
- Artifact lint (2.2.8): every JSON result file in every owner results directory checked for the PROOF-RULES fields:
  commit (rule 5), environment fingerprint with ES version / licence / host load (rule 6), a control result (rule 1),
  witness counts (rule 2), committed thresholds reference (rule 4), per-item rows (rule 5). Field detection is by key
  name patterns stated in the script. A planted artifact missing every field must fail; a planted complete one must
  pass. Violations are reported per stream; this is a lint, not a verdict on the science.
