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

Inspection only; this is not PR signoff. HEAD matches `5bb9c5a06b847593c9c58cbbac19e43c97e9ef92`. The diff against the supplied base adds only the results JSON and the results/write-up section of `eval/STAGE-X2.md`. The numerical primary result is internally consistent, but material interpretation errors need correction.

1. Reproducibility

   I independently tallied `eval/results/stage_x2/stage_x2_score.json:688` (`per_question_primary`), using the committed X0 weak-list IDs and pilot low-overlap IDs to reconstruct the strata.

   | View | n | Control hits | `rrf_facts` hits; wins/losses | Fused hits; wins/losses |
   |---|---:|---:|---:|---:|
   | All | 94 | 81 | 84; 7/4 | 85; 4/0 |
   | Low-overlap | 23 | 12 | 16; 6/2 | 15; 3/0 |
   | Weak-list | 11 | 1 | 5; 5/1 | 3; 2/0 |
   | Other low-overlap | 12 | 11 | 11; 1/1 | 12; 1/0 |
   | Knowledge-update | 15 | 15 | 15; 0/0 | 15; 0/0 |
   | Rest | 56 | 54 | 53; 1/2 | 55; 1/0 |
   | Sensitivity subset | 88 | 75 | 80; 7/2 | 79; 4/0 |
   | Preference | 6 | 3 | 2; 1/2 | 3; 0/0 |

   Thus the headline rates and differences recompute correctly: **0.861702 → 0.893617, +0.031915**, and **0.861702 → 0.904255, +0.042553**. All stored primary hit booleans agree with their stored best-session ranks. The three arms use the same 94 questions, with matching, positive gold counts per question.

   From the exploratory per-question rows I also recomputed the date results: nine questions, seven with gold inside the window, mean window 124 sessions, and hits changing from 3/9, 4/9, 4/9 to 7/9 for all arms. Preference rows sum to 78 facts, 33 user-authored, with rubric-overlap counts 16 user versus 13 assistant.

   **F1 — MINOR, evidence limitation:** The snapshot does not provide the input session/fact records, selection keys, ranked candidate lists, or per-question cluster assignments needed to independently regenerate retrieval and cluster intervals. Item-level figures, the 39-question scaling comparison, and aggregation metrics are stored as summaries without their complete underlying observations. The scaling summaries correspond to 36→28, 37→32 and 37→31 hits out of 39, but that arithmetic is not an independent reproduction. Add a compact audit artifact containing cluster IDs, stratum/type membership, scaling membership and per-question secondary outcomes; describe current reproducibility as partial. References: `stage_x2_score.json:286`, `:375`, `:405`, `:410`, `:688`.

2. Controls

   The fused arm fails D2 for the stated arithmetic reason: **3/23 = 0.130435 < 0.15**, although its net three wins meet the other D2 condition. `rrf_facts` meets D1–D3 when applying the stored intervals and control summary. An interval crossing zero does **not** contradict these screen rules: D1 permits a lower bound down to −0.05, and D2 has no positive-confidence-bound requirement.

   **F2 — MATERIAL, medium severity:** “The facts lists add no spurious matches” does not follow from equal wrong-query rates (`eval/STAGE-X2.md:63`). Equal totals of 1/94 can involve different questions; this control measures accidental gold retrieval under mismatched queries, not factual false positives. Moreover, `eval/emlab/stage_x2_score.py:185–188` skips only one overlapping partner without checking that the replacement is disjoint. Correct the statement to “all arms recorded 1/94 under this control”; retain per-question query pairings/outcomes and assert disjoint gold for every pairing. The current control can fail its threshold, but its implementation does not guarantee the stated exclusion.

   The raw comparison shares the pool and retrieval machinery appropriately. It is not an independent verification of corpus construction or gold mapping. Knowledge-update’s 15/15 ceiling offers little ability to distinguish benefits.

3. Vacuity and selection

   **F3 — MATERIAL, medium severity:** The “12 NEW low-overlap questions” are already evaluated in X1. `eval/STAGE-X1.md:88` explicitly includes them, and line 103 reports their results. All 12 IDs reconstructed for the X2 “other low-overlap” stratum occur in `eval/results/stage_x1/xp_x1_dense.json` under `pool_1000.per_question`. Consequently, `eval/STAGE-X2.md:54`, `:65` and `:74`, and the packet’s “12 new” characterization, misstate their independence. Rename them “other low-overlap, previously evaluated in X1” and remove their use as evidence about previously unseen questions.

   The 11 weak-list questions also overlap the arm-selection evidence, as the write-up acknowledges. D2 therefore reuses development evidence. Its pass is a pilot screening outcome, not independent confirmation.

   D1–D3 and their numerical constants already exist in the supplied base and are unchanged in this diff. That establishes that this commit did not retrofit them. The two-commit snapshot cannot establish that they preceded extraction or every earlier score inspection; the older registration commit and host timestamps are asserted, not independently inspectable here.

   I found no unconditional-pass oracle in the inspected X2 metric code. The supplied 39-test log does not include `tests.test_context`, which contains the actual X1 fusion and X2 helper tests (`eval/tests/test_context.py:402–427`). The fusion tests in the logged suite exercise `emlab.fusion.rrf`, whereas X2 calls `xp_x1_dense.rrf_merge`. This is a focused evidence gap for later validation, not a demand for the pending full validation at this stage.

4. Overclaim

   **F4 — MATERIAL, medium severity:** The final recommendation’s “facts lists do no harm overall” (`eval/STAGE-X2.md:74`) exceeds the evidence. The observed `rrf_facts` result has four losses and a reported interval allowing a −0.032 overall effect. D1 establishes that the chosen screen tolerances passed; it does not establish absence of harm. Likewise, “the safer arm” at line 65 generalizes from zero observed fused losses in this pilot. Replace these with “passed the registered harm-tolerance screen” and “had zero observed session-hit losses in this pilot.”

   **F5 — MATERIAL, medium severity:** The claim that missing records mean “the facts arms can only lose” (`eval/STAGE-X2.md:42`, also the conservative interpretation at line 44) is not guaranteed by the scorer. Missing facts remove distractors for other questions and alter BM25 corpus statistics and fusion ranks. Failed re-extractions retain earlier facts, further preventing a simple monotonic interpretation. State that the direction of aggregate bias is unknown. The 88-question sensitivity view is useful descriptively, but it does not isolate this corpus-wide effect.

   The write-up appropriately limits the experiment to pilot retrieval and separates any-gold from all-gold coverage. However, line 67’s prediction that full-pool rates “will be well below” pilot rates is stronger than one growth comparison supports; phrase that as an expectation requiring measurement. No inspected evidence establishes client answer quality, production retriever parity, latency, cost, access isolation or deployment behavior.

5. Verdicts and verification limits

   - **LAB: BLOCK as currently written.** Correct F2–F5. The artifact can then serve as exploratory lab evidence with explicit reuse, uncertainty and reproducibility limits; the primary counts themselves check out.
   - **CLIENT: BLOCK.** This pilot does not support a deployment decision. Independent held-out evaluation, representative pool size, production retrieval parity and client acceptance evidence remain necessary.

   I inspected only the supplied snapshot and performed read-only arithmetic. I did not follow external packet paths, access secrets, modify files, run model/network retrieval, or run tests. Full validation remains pending by design. There is no code migration or deployment change in this diff; rollback of the artifact is a normal revert.

CODE_INSPECTION_RESULT: BLOCK
