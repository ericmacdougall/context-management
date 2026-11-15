# Exploratory no-model experiments (pilot split only)

These do NOT change the frozen pre-registration (blob a72ee4563142d45b9491f1643e238d7e72b0f851). They run on the PILOT split only (never test, dev2 or held_back), use no model call, and state which retriever each number comes from: every number here is from the EXACT IN-PROCESS retriever (BM25 with the stated deviations and E5-small cosine), which is not an Elasticsearch result (round-level parity of that pipeline with Elasticsearch: BM25 top-10 overlap 0.751, client RRF versus ES `rrf` 0.817); a gain here is not a result for the Elasticsearch Basic target until re-run on Elasticsearch. Source and critique: research/wv1gg5s6m.output (ideas X01, X02, X05 with the critic's corrections). Thresholds below are committed (git) before each run; results are appended in later commits.

Whole-pool figure for the register: the current RRF session recall@10 at H3 is 0.4754 (0.48 rounded; results/pilot/curve_pilot.json, own + whole deduplicated pool minus the content cluster, pilot, n = 94). 0.46 is the superseded old-sets value (0.461, results/pilot/curve_pilot.old_sets.json; the old-sets exact-hybrid figure was 0.4585).

## E1. PRECHECK-CURVE (stage-2 ceiling)

Read from results/pilot/curve_pilot.json: flat RRF (round-level hybrid, rolled up to sessions) session recall@20 and @50 at H1, H2, H3 (pilot, 94 questions with gold, 90 content clusters). This is the ceiling for a session-first two-stage design: stage-2 recall cannot exceed stage-1 session recall at the cut S. Rule committed before reading: run E2 only if the H3 ceiling (flat RRF session recall@50, the larger S) is at least 0.58 (flat recall@10 0.475 + 0.10, the E2 pass margin); otherwise E2 is not run and the reason is recorded.
