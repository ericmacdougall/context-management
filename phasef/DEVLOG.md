# v4 dev log (dev = the 16 knowledge-update questions of the v3 gate)

Iteration 1 (link-v4.0, theta 0.25 then 0.15, single-cardinality candidates only, gold pair = max-sim pair across gold sessions): 138 pairs, 29 links (27 replaces, 2 pending),
Opus audit precision 22/29 = 0.76 [0.58, 0.88]; gold pair linked 11 (10 of 15 non-abstention) of 14 identified. Findings: the judge calls "replaces" on additions (bag vs strap, project vs project);
the gold-pair proxy picked a repeated donation plan (a restatement), not the update; count facts are extracted as multi so single-only candidate generation misses them; two questions have no pair at all.
Change for iteration 2: judge prompt v4.1 (stricter replaces, structured reason), candidates of any cardinality, gold pair = answer-fact X plus most similar fact Y in another gold session.

Iteration 2 (link-v4.1: stricter judge prompt with structured reason, candidates of any cardinality, theta 0.15, 3 per fact): 258 pairs, 26 links, Opus audit precision 20/26 = 0.77 [0.58, 0.89].
Remaining false links are borderline refinements the auditor calls adds (trip plan refinement, colleague to employer). The similarity-pair gold proxy proved unreliable (picked a repeated donation plan; the real
old/new pair was not the max-sim pair), so the gold link is now "the answer fact has a replaces/pending link to a fact in another gold session": 12 of 14 questions with an answer fact (non-abstention: 12 of 15).
Change for iteration 3: candidates theta 0.12, 5 per fact, 40 pairs per question; every replaces/pending decision verified by a second, shuffled Sonnet call.

Iteration 3 (link-v4.1 + shuffled verification; theta 0.12, 5 per fact, 40 pairs): 499 pairs (cap of 40 reached on 4 questions), 34 links verified once more, 24 links kept (21 replaces, 3 pending; 10 dropped as unverified),
Opus audit precision 20/24 = 0.83 [0.64, 0.93]; answer fact linked to another gold session on 12 of 14 questions with an answer fact (non-abstention 12 of 15).
STOP RULE NOT MET after 3 iterations (needed precision >= 0.90 and >= 13 of 15): the dev gate is not run on dev2. dev2 received no calls.
Residual false links: a work-versus-home location pair, a colleague's employer change judged adds by the auditor, a plan versus a new topic. Gold links missed: 07741c45 (the shoe-rack plan is never linked to the under-the-bed fact:
judged adds or unrelated), 5c40ec5b (the earlier evidence is a plan to meet, judged adds; no earlier count exists), 89941a94 (no fact covers the answer: extraction miss).
Cost line (candidate judging, link-v4.1, 499 pairs over 764 unique dev sessions): 48 calls (10.4 pairs per call), 1.39M input / 27.7k output tokens including about 10.5k overhead per call, i.e. about 1.8k input and 0.036k output tokens and 0.063 calls per session, plus about 0.005 calls per session for verification.

VOLUME PLAN: sessions needed 5137 (pilot 4637, D500 500), already cached 871, to extract 4266, calls at 16 per call 267, estimated input 18.2M tokens, output 3.0M; limits 360 calls / 26M input (printed before the run)

BATCH-SIZE QUALITY SAMPLE (48 sessions by seed 16096 from the 192 already extracted at 8 per call; re-extracted at 16 per call, 3 Sonnet calls; Opus comparison, 6 calls; volume/cache_q counter 9 of 12):
facts per session batch 8 = 4.38 (210 facts), batch 16 = 2.54 (122 facts), change -41.9%. Opus matching: 70 batch-8 facts have no match in the batch-16 list (33.3% of batch-8 facts dropped), 1 batch-16 fact has no match
in the batch-8 list, 140 matched. All three batch-16 outputs parsed strictly and completely, so the loss is silent (fewer facts, not truncation). Rule from main: more than 10% loss stops the volume run.
VOLUME RUN NOT STARTED (0 extraction calls). Plan at 16 per call was 4,266 sessions, 267 calls, 18.2M input; at 8 per call it would be about 534 calls (over the 360-call limit), about 21M input. The v3 dev gate (14 questions) used batch 16 and
therefore saw about a third fewer facts than batch-8 extraction.

BATCH-SIZE CURVE (24 of the 48 sample sessions, first 24 in seed-16096 order; 1 and 4 per call newly extracted (24 + 6 Sonnet calls), 8 from the v2 run, 16 from the quality run; Opus matches each size against the 1-per-call list, 9 calls; cache_curve counter 39 of 45):
facts per session: 1 per call 9.00 (216 facts), 4 per call 5.12, 8 per call 4.46, 16 per call 2.71.
1-per-call facts with no match at that size: 4 per call 73 of 216 (33.8%), 8 per call 84 (38.9%), 16 per call 136 (63.0%). Facts present at the size but not in the 1-per-call list: 3, 1, 0.
Trivial or duplicate facts (Opus): 2 of 216 (0.9%) in the 1-per-call list and 1 (0.8%), 0, 0 at 4, 8, 16: the extra facts at small batch sizes are real, not padding.
Main's rule (largest size with at most 10% missing against 1 per call): only 1 per call qualifies. Volume run NOT started. Cue keys for the 102 pilot questions generated (102 calls, volume counter) and copied to t2-dev:~/emlab-16096/in/question_cues_pilot.jsonl.
Measured cost per session (model tokens vs per-call harness overhead of about 10.5k tokens): 1 per call: 4.4k in net / 1.88k out, 14.9k in total per call, 36 s per call; 4 per call: 3.8k / 0.94k, 25.7k per call, 36 s per call.

ANALYST RUNNER dry run (fake lexical backend, 2 pilot questions own-haystack, stand-in lexical top-50 candidates; 5 of 10 calls): both arms returned valid strict output on both questions
(rerank 5.7 and 7.8 s, 18k and 32k input tokens; analyst 7.3 and 9.8 s, 19k and 32k input tokens, 0 tool calls: the candidates already held the answer). A forced diagnostic call confirmed the MCP path:
search and open_round were reached through the stdio server (--mcp-config, --strict-mcp-config, built-ins off, neutral cwd) and logged with round ids; the 12-call budget is enforced and unit-tested in analyst_mcp.py.

ANALYST CANARY (H3, 23 whole-pool low-overlap questions, both arms, 46 calls, run_capped, concurrency 2): 44 ok, 2 rejected (one duplicate id in an ANALYST answer, one rerank id not among the candidates), 0 guard blocks, 0 refused tool calls,
max 12 tool calls (budget held), 79 tool calls in total (daemon log 80 including one early probe of mine), wall 384 s, counter 46 of 620. Rerank: 31k input tokens and 10 s per call; analyst: 100k input tokens mean (max 289k, repeated turns) and 22 s per call, 3.4 tool calls per question.
Low-overlap file: out/lowoverlap_pool_h3.json on t2-dev, sha256 c6351d7ffbfacb9e642862b30edcfe187e41b0316eca8f15bdd694431bba73a2, thresholds.100.qids (23). A first push left macOS ._ files on the host (removed); tar now runs with COPYFILE_DISABLE.

ANALYST REST AND SECONDARY PHASES (run_capped, concurrency 2, own counter 587 of 620; wall 2,975 s for the 541 calls after the canary): 587 result files in total (canary included), 26 rejected (4.4%), 0 guard blocks, 0 crashed calls,
0 refused tool calls and no question over 12 tool calls (analyst mean 1.6 tool calls, max 12). Rejections per arm and level: ANALYST H1 3/94, H2 4/94, H3 2/94, H3_top20 1/23; ANALYST-RERANK-ONLY H1 6/94, H2 6/94, H3 4/94.
Types: id not among candidates 11, duplicate id 3, malformed or trailing JSON ("Extra data" or delimiter errors) 11, results count 1. WAL and work directories: 1,752 to 2,294 and 1,921 to 2,463 over this job.

STAGE G CANARY STOPPED AT THE SIZE CHECK: first real part-A prompt (078150f1, 400 candidates cut at 400 characters, both arms) measured 66,388 tokens for A-REORDER (76,886 total input minus the 10,500 per-call overhead), above the 60,000 stop line; the estimate formula gave 53,489 (ratio 1.24;
162,355 candidate characters tokenise at 2.47 characters per token, not 4, because of the id and date prefixes). The runner wrote its stop file after the first result; 2 of the 22 canary calls were made (A-REORDER ok, A-DIG ok with 9 tool calls over 10 turns, 5 ids returned, 604k cumulative input tokens), 2 calls on the part A counter, none on part B. No further calls until main replies.
