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

# Links rescue (Opus author, branch feat/links-rescue; GATE3.md, LINK_RUBRIC.md)

Step 1, yardstick: the final v4 link set (24 links) re-audited twice by Opus (seeded shuffle with linkaudit-v1; seeded shuffle with a reworded equivalent rubric,
linkaudit-v1-alt), 6 calls. Precise/not agreement 24/24, 23/24, 23/24; precision 20/24, 20/24, 21/24. The only flip: a plan for one video versus another video topic.
The rescue author's own labels (written first) agree with the majority on all 22 links it called clear. The yardstick is sound; the metric is kept, harmful-link rate added.
Step 2, diagnosis: the 4 false links are all judge errors of one kind, two related but different attributes read as one (office vs home location, work vs home zip,
relationship vs employer, one video plan vs another video). Gold misses: 07741c45 is a candidate miss ("stored" lemmatised to "stor"), not a judge error;
5c40ec5b has no comparable earlier value (a plan to meet, then "met twice") and no candidate; 89941a94 is a definition artefact (yes/no answer, coverage 0.33),
its correct pair is linked. 5a4f22c0's v4 hit rests on a false link. Ceiling with correct links under the owner's definition: 12 of 15 (GATE3.md section 2).

Rescue iteration 1 (v5 = v4 candidates + lemma fix "stored" -> "store"; stage 1 = link-v4.1 Sonnet, cached, 2 new pairs; stage 2 = link-v5-verify Sonnet on every stage-1
positive, attribute-explicit: each side names entity and attribute, same_entity / same_attribute / both_true checked in code, explicit unsure = abstain; replaces the v4
shuffled re-judge): 501 pairs, 35 stage-1 positives, 19 links (17 replaces, 2 pending), 2 abstentions (unsure). Calls 7 (2 stage-1, 3 verify, 2 audit).
S1 precision 19/19 Wilson95 [0.83, 1.00] but only 19 audited (bar: at least 20): FAIL on the count. S2 harmful 0/19 [0.00, 0.17]: pass. S3 12/15 [0.55, 0.93]: FAIL
(misses 5a4f22c0, 5c40ec5b, 89941a94: the three rows of the GATE3 ceiling table); S3-correct 12. STOP CONDITION NOT MET.
The 19/19 is IN-SAMPLE: the verifier's examples (workplace vs home, relationship vs employer, two different videos) are the four v4 dev errors. No held-out precision exists.
Recall price: of the 20 v4 links the original audit accepted, v5 keeps 18; it drops sculpting one month -> classes three months (auditor: replaces) and lessons with Alex ->
plan to find a guitar teacher (auditor: pending); the 4 audited-false links are all dropped. New: 07741c45 under the bed -> shoe-rack plan (audited pending).
Not demonstrated: order independence of stage 2 (batches of 12 across questions in pair-id order; a verdict may depend on its neighbours).
Gate: not run (stop condition not met). No dev2 call, no dev2 session count.

Registered before running (diagnostics for the recommendation; none of these is a link or counts toward S1-S3):
- D1 (1 Opus call): linkaudit-v1 on X of 5c40ec5b ("met up with Alex twice already") against the two Alex facts of the earlier gold session (met at a festival; plan to
  meet when he visits). If both are rejected, the GATE3 ceiling of 12 of 15 is measured rather than read; if one is accepted, a dated correction goes here and GATE3 stays as committed.
- D2 (label-free reader arm on dev, about 19 calls): the FACTS3 reader (reader-v1.2, same v3 facts, same retrieval, same order; score_v2 does not use status) with every
  link label replaced by the fact's modality, i.e. dated facts only. Baseline: FACTS3 16/16, which was read with v3 labels (link precision 0.625); no v4- or v5-label read
  exists on dev. Reading rule: at n=16 with arms at ceiling only a large drop is informative. A label-free score of 15 or 16 means no measurable answer benefit from links
  on dev; a drop of 3 or more means labels matter here; 1 or 2 is within noise. A v5-label arm is run only if the label-free arm drops.
D1 result (2026-11-15, correction to the GATE3 ceiling table, which stays as committed): the auditor labels "plans to meet Alex when he visits" -> "has met up with Alex
twice already" REPLACES (and the festival fact -> X adds). So under the measuring rubric the 5c40ec5b row is satisfiable; the measured ceiling is 13 of 15, not 12.
Reaching it needs that pair in the candidate set (it is not: X has no candidate at theta 0.12 / 40 pairs) and both link stages to accept it.
D2 result: label-free reader 16/16 [0.81, 1.00], same 16 of 16 fact lists as FACTS3; FACTS3 (v3 labels) 16/16; ORACLE3 16/16. Under the registered reading rule:
no measurable answer benefit from links on dev.

Rescue iteration 2, registered before running. Purpose: (a) a precision check on pairs the verifier was not designed on (the 287 new pairs; their links are reported
separately from iteration 1's 19), (b) whether S3 can reach the measured ceiling of 13 with correct links, (c) the volume cost of wider candidates.
Config: theta 0.10, 5 per fact, 50 pairs per question, each fact's best partner protected from the per-question cut, lemma fix (741 pairs, +48% over iteration 1).
Stage 1 unchanged (link-v4.1 Sonnet); new pairs packed 15 per call across questions in pair-id order (each pair carries its own rounds). Stage 2 unchanged (Sonnet).
Audit: one linkaudit-v1 audit per link; links already audited in iteration 1 keep that label. Plus an order check: stage 2 re-run on all stage-1 positives with
seeded shuffled batches (seed 7), verdict flips reported. Even if the stop condition were met, the GATE3 budget rule cannot be satisfied after this iteration
(about 115 calls would remain against at least 107 plus extraction), so the gate will not be run; that is decided now, not after the result.

Rescue iteration 2 result: 741 pairs; 287 new pairs gave only 3 new stage-1 positives; 19 links (17 kept from iteration 1, 2 new). The wider cut displaced two
iteration-1 links (89941a94 California -> Chicago -> Bay Area, filler-session noise). New links (pairs the verifier was not designed on): 2 of 2 audited replaces
(phone GPS -> new bike computer, in two haystacks); held-out n is too small to say anything beyond "no new error". S1 19/19 [0.83, 1.00] on 19 links: FAIL on the count;
S2 0/19: pass; S3 12/15: FAIL. The 5c40ec5b pair now reaches the judges: stage 1 says replaces, the verifier says none (plan to meet vs number of meetups:
same_attribute=no) while the auditor says replaces (D1). The verifier is stricter than the auditor; that strictness is what removed the four v4 errors and it also
costs this one gold link and two auditor-accepted v4 links. Calls 25 (20 stage-1 packed, 1 verify, 1 audit, 3 order check). Order check: stage 2 re-run with seeded
shuffled batches keeps the link/no-link verdict on 32 of 33 positives (one flip: unsure -> replaces on Samsung S22 -> iPhone 12).
STOPPED after 2 of 3 iterations. A third would only relax the verifier for one dev pair to reach 13 (overfitting), and the gate cannot run under the GATE3 budget
rule (122 calls remain; the gate needs at least 107 plus about 44-48 extraction calls plus 10%). The dev stop condition is NOT met; dev2 received no call.
Cost line for volume (iteration-1 config, 764 dev sessions): stage 1 as v4 (0.063 calls, 1.8k input and 0.036k output tokens per session) plus stage 2
(3 Sonnet calls, 93.9k input and 3.8k output tokens: 0.004 calls, 0.12k input tokens per session) = about 0.067 Sonnet calls and 1.9k input tokens per session.
The iteration-2 config adds 48% stage-1 pairs for 2 links. No Opus judge was used (the Sonnet verifier removed every v4 error); by the ledger's cost field Opus
costs about 1.9x Sonnet per input token here (audit 7.9e-6 vs verifier 4.3e-6 USD-equivalent per token), i.e. an Opus verifier would cost about 0.95 instead of
0.52 USD-equivalent per 1,000 sessions for stage 2 (stage 1 dominates: about 7.7 per 1,000 sessions on Sonnet).
Companion from the v3 gate: of 12 audited-false v3 links, 3 touched a fact shown to the reader (07741c45, 0e4e4c46, 603deb26); all three were answered correctly.
