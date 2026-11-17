Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: be746a0dd829178343424be0fee434be6c80f016
Reviewed packet SHA-256: bd00cd7ba0296f4dff466db877cdcc69fee19ec26009367bf39741bc0b5a0234
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 5bb9c5a06b847593c9c58cbbac19e43c97e9ef92)
Worktree status: clean

CODE_INSPECTION_RESULT: ACCEPT

Inspection only; **not PR signoff**. HEAD matches `be746a0dd829178343424be0fee434be6c80f016`. I inspected the Stage X2 delta against `5bb9c5a06b847593c9c58cbbac19e43c97e9ef92`; the separate ES-pilot implementation is outside this review. No material implementation defect remains in the inspected delta.

1. **Prior findings F1–F5**

   | Finding | Status | Evidence and limits |
   |---|---|---|
   | F1 | **CLOSED for the requested audit-artifact correction** | `eval/emlab/stage_x2_score.py:279–285` adds cluster IDs, types, strata, scaling membership and per-question secondary outcomes. `eval/STAGE-X2.md:81` explicitly calls reproducibility partial. I rebuilt primary, stratum, type and sensitivity intervals without discrepancies. Retrieval regeneration remains unavailable. |
   | F2 | **CLOSED** | `wrong_partner()` at `eval/emlab/stage_x2_score.py:56–64` searches all other questions cyclically; its caller asserts a disjoint partner at line 198. Pairings and outcomes are retained at lines 203–206. `eval/STAGE-X2.md:63` correctly limits the control to accidental gold retrieval under mismatched queries. |
   | F3 | **CLOSED** | Labels and interpretation are corrected at `eval/STAGE-X2.md:53–54,65,78`. All 23 low-overlap IDs occur among the 38 committed X1 `pool_1000.per_question` entries; I checked that membership directly. |
   | F4 | **PARTIALLY CLOSED — minor wording remains** | The material generalizations are removed at `eval/STAGE-X2.md:65,74`. The recommendation still calls fused the “no-loss arm”; see N3. |
   | F5 | **CLOSED** | `eval/STAGE-X2.md:42–44,80` and `stage_x2_score.py:286–288` state that the aggregate direction is unknown and that dropping six affected questions does not isolate the corpus-wide effect. |

2. **`wrong_partner()` tests versus the base**

   Both new tests would fail on the unmodified base **by ImportError**, because the base has no `wrong_partner` API:

   - `StageX2WrongQuery.test_partner_gold_is_disjoint_even_when_the_second_candidate_overlaps` — import at `eval/tests/test_context.py:432`. Its subsequent fixture does demonstrate the behavioral defect: the old rule selects overlapping `"c"` for `"a"`, while the replacement selects `"d"`.
   - `StageX2WrongQuery.test_wraps_and_reports_none_when_everyone_overlaps` — import at line 445. It checks wraparound and exhaustion, but does not execute the base implementation.

   **N2 — MINOR, regression evidence:** These are useful current-code tests, but they do not establish a behavioral failing-first run against the base. For that claim, add a comparison harness that executes the prior partner-selection logic on the same fixtures and records the behavioral failure.

3. **`StageX2MissingRecords`**

   At `eval/tests/test_context.py:450–458`, this is presented as an illustration of the mechanism: removing distractors can move a gold session into the top ten. It tests the unchanged `view()` function and would pass with either scorer version.

   I found no assertion in the scoped write-up that this test proves a newly fixed behavior or measures the actual missing-record bias. It demonstrates possibility, not the direction or magnitude in this dataset.

4. **D1–D3 are untouched**

   Direct base/HEAD block comparisons show identical constants at `eval/emlab/stage_x2_score.py:31–33`:

   - D1: mean ≥ `-0.02`, lower bound ≥ `-0.05`
   - D2: mean ≥ `0.15`, wins minus losses ≥ `3`
   - D3: wrong-query rate ≤ `0.05`

   The decision block at lines 215–224 is also identical. The zero-context diff contains no changes to those constants or expressions. Section 3’s registered criteria are unchanged.

5. **Rescore equality and row recomputation**

   Every key present in the base score JSON is deeply equal in the new JSON. Only `audit_rows`, `wrong_query_pairs` and `missing_records_note` were added.

   Recomputed from committed question rows:

   | Group | n | Control hits | Facts hits | Fused hits | Facts W/L | Fused W/L |
   |---|---:|---:|---:|---:|---:|---:|
   | All | 94 | 81 | 84 | 85 | 7/4 | 4/0 |
   | Low-overlap | 23 | 12 | 16 | 15 | 6/2 | 3/0 |
   | Weak-list | 11 | 1 | 5 | 3 | 5/1 | 2/0 |
   | Other low-overlap | 12 | 11 | 11 | 12 | 1/1 | 1/0 |
   | Knowledge-update | 15 | 15 | 15 | 15 | 0/0 | 0/0 |
   | Rest | 56 | 54 | 53 | 55 | 1/2 | 1/0 |

   Overall rates are **0.861702128**, **0.893617021**, and **0.904255319**, matching the earlier file. The 39-question scaling figures also reproduce: control `0.923077 → 0.717949`, facts `0.948718 → 0.820513`, fused `0.948718 → 0.794872`.

   Rebuilding the primary, stratum, type and sensitivity bootstrap intervals with the committed cluster assignments, seed and algorithm produced **zero discrepancies**.

6. **Remaining reproducibility gaps**

   Yes: `eval/STAGE-X2.md:81` plainly states that ranked candidate lists and input session/fact records are not committed, retrieval cannot be regenerated from this artifact, and reproducibility is partial.

   The 94 wrong-query pairings and hit outcomes are inspectable. However, complete per-question gold-session sets are absent, so I could not independently verify each pairing’s disjointness from the JSON alone. The implementation enforces it; the artifact records the claimed result.

7. **Remaining wording and new findings**

   - **N1 — MINOR, stale provenance:** `eval/STAGE-X2.md:40,42` still says the scorer is unchanged since `1b99be2`, contradicting the current diff and the correction history at line 82. Label those statements as provenance of the **original scoring run**, then identify the rescore separately.
   - **N3 — MINOR, residual overstatement:** `eval/STAGE-X2.md:74` says “include fused_facts_raw as the no-loss arm.” Replace this with “the arm with zero observed session-hit losses in this pilot.” The surrounding caveats make the intended scope clear, but the shorthand weakens F4’s correction.
   - No new material security, migration or rollback issue was found in this offline scorer/documentation delta. Existing score fields retain identical values; downstream consumers now receive three additional top-level fields.

**LAB use: PASS WITH LIMITS** — suitable for exploratory screening and confirmatory-test design, with partial reproducibility and reused development questions.

**CLIENT use: BLOCK** for deployment, generalized safety, or benefit claims. This remains a small-pool pilot with no unseen-question confirmation and an overall facts-arm interval that includes harm. Sharing it as an explicitly limited pilot report is a narrower use.

I inspected the supplied log reporting 77 tests, OK with three skips; I did not rerun tests. Full validation is pending and was not required for this inspection decision. I did not verify extraction inputs, model outputs, remote run timestamps, retrieval rankings, or deployment behavior. No files were modified.

Session metadata separately reports critical cron/deadman alerts; these are outside the snapshot and were not verified in this review.

Stage: code-inspection only; not PR signoff.
