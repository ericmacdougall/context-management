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
