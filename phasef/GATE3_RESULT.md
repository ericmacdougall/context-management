# Gate 3 result (dev2, one run, criteria as registered in GATE3.md sections 4 and 6): FAIL on G5

Recipe v5 frozen at 2b15450 (driver phasef/gate3.py at c6bd8a5). 16 dev2 knowledge-update questions, none abstention. Calls 211 (counter 345 -> 556 of 565).
Extraction: 743 sessions in 47 calls at 16 per call, all batches strict JSON with every session present. Candidate pairs 494 (cap 40 reached on 3 questions).

| Criterion | Result | Pass |
|---|---|---|
| G1 FACTS5 answers, within 1 of ORACLE | FACTS5 16/16 [0.81, 1.00]; ORACLE 16/16 [0.81, 1.00] | yes |
| G2 gold-value fact B in context | 14/16 [0.64, 0.97]; failures 031748ae (coverage 0.35), 42ec0761 (coverage 0.00): no B found by token coverage | yes |
| G3 B labelled current or pending | 14/16 [0.64, 0.97]; same two questions | yes |
| G4 link precision (all 16 links audited, Opus linkaudit-v1) | 16/16 [0.81, 1.00] | yes |
| G4b harmful-link rate | 0/16 [0.00, 0.19] | yes |
| G5 gold update pair linked (Opus-named old/new facts, link in both runs) | 10/16 [0.39, 0.82] | NO (needs 12) |

G5 failures (no extraction misses: the gold-pair call named an old and a new fact on all 16):
- judged no link: 0977f2af (Instant Pot, then Air Fryer: both judges say adds), 0ddfec37 (20 vs 15 baseballs: run B links, run A's verifier says unsure, so the
  order-independence rule drops it), 10e09553 (7 bass on one trip, 9 on a later trip: run A adds; run B replaces then verifier none), 22d2cb42 (plan to service the guitar
  at one shop, serviced at another: run A restates; run B replaces then verifier none);
- not a candidate pair: 3ba21379 (Mustang model -> F-150 model; 10 pairs only), 41698283 (50mm lens -> 70-200mm lens).
Note, no criterion changed: in 0977f2af, 41698283 and 10e09553 the "update" is a new item or event, which the rubric labels adds; G5 assumes every
knowledge-update question has a replace pair.

Order independence (two seeded runs per stage): stage-1 positive/negative flips 17 of 494 pairs; final link/no-link flips 9 of the 25 pairs that either run linked;
16 links survive in both runs.
Diagnostic NOLABEL (same facts, no status labels): 16/16, identical verdicts to FACTS5 on every question.
Cost of link judging with both runs (74 Sonnet calls over 743 sessions): 0.100 calls, 3.5k input and 0.085k output tokens per session (about half of it is the second run).
Gate verdict: not passed (G5). Nothing was adjusted after results were seen.
