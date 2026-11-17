Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: 5bb9c5a06b847593c9c58cbbac19e43c97e9ef92
Reviewed packet SHA-256: 8de04b2f53ff639f312fe81ea15efddeaa47e64a719663b38c9cededd0c73e3e
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 1b99be24a8a4b190239c66b60495309dded4c984)
Worktree status: clean

CODE_INSPECTION_RESULT: BLOCK

Inspection only, not PR signoff. HEAD matches `5bb9c5a06b847593c9c58cbbac19e43c97e9ef92`. The diff adds the X2 results JSON and results write-up; the scorer is unchanged from the supplied base. Two material interpretation issues need correction.

1. **Reproducibility**

   I recomputed the following from `eval/results/stage_x2/stage_x2_score.json`, using `per_question_primary` and, for strata, the committed low-overlap selection and X1 question records:

   | View | n | Control hits | Facts hits; W/L | Fused hits; W/L |
   |---|---:|---:|---:|---:|
   | All | 94 | 81/94 = 0.861702 | 84/94 = 0.893617; 7/4 | 85/94 = 0.904255; 4/0 |
   | Facts for every gold session | 88 | 75/88 = 0.852273 | 80/88 = 0.909091; 7/2 | 79/88 = 0.897727; 4/0 |
   | Low-overlap union | 23 | 12/23 = 0.521739 | 16/23 = 0.695652; 6/2 | 15/23 = 0.652174; 3/0 |
   | Weak-list | 11 | 1/11 = 0.090909 | 5/11 = 0.454545; 5/1 | 3/11 = 0.272727; 2/0 |
   | Other low-overlap | 12 | 11/12 = 0.916667 | 11/12 = 0.916667; 1/1 | 12/12 = 1.000000; 1/0 |

   Thus the all-question differences are **+0.031915** and **+0.042553**; low-overlap differences are **+0.173913** and **+0.130435**. These match the write-up. All 282 arm/question hit flags agree with their recorded best-gold-session ranks.

   I also recomputed the exploratory date totals: **3/9 → 7/9** for control and **4/9 → 7/9** for both facts arms. Preference records sum to **78 facts, 33 user-authored**, with rubric-overlap counts **16 user / 13 assistant**.

   **F1 — MINOR, evidence limitation:** The committed primary rows omit cluster IDs, stratum/type labels, item-level ranks, wrong-query outcomes and toy-pool outcomes (`eval/emlab/stage_x2_score.py`, final `per_question_primary` serialization). Consequently:
   
   - The stated confidence intervals cannot be independently reconstructed from those rows.
   - Item-level rates **0.851064 / 0.882979 / 0.872340**, wrong-query **1/94** per arm, preference **W1/L2 of six**, and scaling **36→28 / 37→32 / 37→31 of 39** can be checked against stored summaries, but not fully rebuilt from saved per-question outcomes.
   - Pool size **1,098**, extraction completion **92/98 and 27/30**, and actual retrieval rankings require inputs absent from this snapshot.

   Action: save a compact audit table containing these fields and input/model fingerprints. This is a reproducibility limit, not a demand for pending full validation.

2. **Controls**

   The three arms use the same `view()` session-hit oracle and gold mapping; retrieval construction does not consume gold labels in `lists()` (`eval/emlab/stage_x2_score.py:146–169`). I found no arm-specific scoring advantage there. Fused fails D2 for the stated reason: **3/23 = 0.130435 < 0.15**, while its net wins meet the second condition.

   **F2 — MATERIAL, medium severity:** “So the facts lists add no spurious matches” overstates the wrong-query control (`eval/STAGE-X2.md:63`). Equal aggregate rates can hide different matching questions. This control tests accidental retrieval of a designated gold session under a substituted query; it does not establish absence of spurious matches generally. Its implementation also skips only once when queries share gold and does not verify that the replacement is disjoint (`eval/emlab/stage_x2_score.py:185–190`).

   Action: replace that conclusion with “each arm recorded one wrong-query hit in this permutation”; retain per-question outcomes and assert disjoint gold for every substituted query.

3. **Vacuity and pre-registration**

   D1–D3 are meaningful conditions that can fail. Applying their committed definitions gives the reported decisions: facts passes all three; fused fails D2. A confidence interval crossing zero does **not** contradict D1, which explicitly permits a lower bound down to −0.05 (`eval/STAGE-X2.md:19`).

   The base already contains these rules, establishing that this results commit did not change them. The two-commit snapshot cannot prove the stronger chronology that they preceded every score inspection or extraction call; the document acknowledges part of this limitation at line 42.

   The supplied 39-test log covers generic fusion, metrics, pool and confidence utilities. X2-specific helper tests exist in `eval/tests/test_context.py:415`, outside that logged command. Their assertions are non-vacuous, but their execution is not evidenced here. No tests were run during this review.

4. **Overclaim and selection**

   **F3 — MATERIAL, medium severity:** The 12 other low-overlap questions are incorrectly described as “new” and “not seen before” (`eval/STAGE-X2.md:54,65,74`). **All 12 already occur in the committed X1 dense-result per-question records** at `eval/results/stage_x1/xp_x1_dense.json#/pool_1000/per_question`. X1 explicitly reports this stratum in `eval/STAGE-X1.md:103`. The 11 weak-list questions also informed the X2 thresholds, as line 19 openly states. Therefore **none of the 23 low-overlap questions is an unseen-question evaluation**.

   Action: relabel the 12 as “other low-overlap, previously evaluated in X1”; replace “zero benefit on unseen questions” with “no unseen low-overlap questions were evaluated.” Remove the implication that their result estimates an independent held-out effect.

   **F4 — MINOR:** Several conclusions are stronger than the experiment supports: missing facts “can only lose” and are necessarily conservative (lines 42–44), fused is “safer” (line 65), full-pool rates “will be well below” pilot rates (line 67), and facts “do no harm overall” (line 74). Missing documents can change BM25 statistics and fused ordering; the other statements extrapolate beyond observed outcomes. Use conditional language and describe observed gains, losses and uncertainty directly.

5. **Verdicts**

   - **LAB: BLOCK as currently written**, pending F2 and F3 corrections. The numerical screen is internally consistent and suitable to retain as exploratory pilot evidence after those corrections, with the reproducibility and chronology limits above.
   - **CLIENT: BLOCK.** These results do not establish benefit on untouched questions, representative full-pool performance, or safe deployment behavior. The document’s recommendation to design a confirmatory test is appropriate.

   No runtime code, migration or deployment changes are introduced by this diff. I inspected only the supplied committed snapshot, read no secrets, and made no file changes. Full validation remains pending by design; its absence is not the basis for this inspection block.

CODE_INSPECTION_RESULT: BLOCK
