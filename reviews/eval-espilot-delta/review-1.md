Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: 275f7444bba079389ec89b3c0d391a7514335ac3
Reviewed packet SHA-256: 5a2bc9aa7c443b87fa2ba2f4b478d4559d2c1f5f118d245a5f944cc84322d9ee
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base be746a0dd829178343424be0fee434be6c80f016)
Worktree status: clean

CODE_INSPECTION_RESULT: BLOCK

Inspection only, **not PR signoff**. HEAD matches `275f7444bba079389ec89b3c0d391a7514335ac3`. The targeted CE checks work, but incomplete pilot evidence can still receive KEEP. Pending full validation is not the reason for this block.

Findings are based on the committed snapshot and read-only, in-memory audits.

- **N1 — MATERIAL, medium: completeness is checked against the supplied questions, not the expected pilot.** `eval/emlab/es_pilot_score.py:93–104` compares question sets across levels without checking expected membership. Removing question `51a45a95` from all four levels and its corresponding CE records returned **KEEP**, `evidence.complete=true`, and 93 questions per level. Adding one cloned question under a new ID at every level likewise returned KEEP with 95 questions. This permits silent denominator changes; projection still uses the fixed `PILOT_N=94` at lines 21 and 50. **Correction:** validate exact question membership against an independently recorded pilot manifest before scoring. Add negative cases for whole-question removal, addition, and replacement; a count check alone cannot detect replacement.

- **N2 — MATERIAL, medium: cluster metadata is accepted without validation.** `eval/emlab/es_pilot_score.py:80–89,168–180` requires a `cluster` key but trusts its value. Replacing every committed row’s cluster with `"one"` returned **KEEP**, `evidence.complete=true`, and one effective cluster instead of 90. This changes the bootstrap’s statistical meaning and can collapse interval width. **Correction:** validate each question’s cluster against the pilot manifest, require consistency across levels, and reject mismatches before calculating intervals. Test cluster collapse and incorrect assignments that preserve the total cluster count.

- **N3 — MINOR, low: malformed evidence does not consistently produce the documented withheld result.** `eval/emlab/es_pilot_score.py:109–112` detects missing CE keys, but scoring continues through `lists_for()` at lines 139–142. Removing `ce_exact` raises `KeyError` rather than returning WITHHELD. A NaN round ID similarly reaches `session_of()` and raises `AttributeError`. CE `scores` containing NaN or nonnumeric values are ignored and still receive KEEP. **Correction:** validate consumed field types, stop scoring records known to be malformed, and clarify whether CE score validity/order consistency is covered by the evidence claim.

- **N4 — MINOR, low: the revised test-split statement still exceeds what code inspection establishes.** `eval/ES-PILOT.md:3` says no test question was “inspected.” `eval/emlab/es_pilot2.py:134` constructs a question-text map from the entire dataset; `build_rag_contexts.py:44–48` loads all questions, and `pool.py:36–39` processes their gold metadata. The code supports pilot-only evaluation, but cannot establish an unrestricted claim about inspection or tuning history. **Correction:** state that no test-split question was evaluated by this run, while full-dataset text and metadata were loaded for the documented preparation steps.

**1. F1: PARTIALLY CLOSED.**

The original missing/duplicate CE and mismatched-head cases are addressed by `validate()` at `es_pilot_score.py:73–133`, with recommendation withholding at lines 216–217. Missing arms are also rejected at lines 85–88.

N1 remains an incomplete-evidence route to a positive recommendation. N2 additionally shows that `evidence.complete` does not establish the validity of the bootstrap grouping.

**2. Adversarial attempts and outcomes.**

I loaded the committed rows and CE records, mutated copies in memory, and replaced only the scorer’s loading function. No evidence files were changed.

| Attempt | Observed outcome |
|---|---|
| Remove one question and its CE records at every level | KEEP; evidence complete; 93 questions, 89 clusters |
| Add one cloned question with a new ID at every level, including CE | KEEP; evidence complete; 95 questions, 91 clusters |
| Collapse all cluster IDs to one | KEEP; evidence complete; one effective cluster |
| Swap H0 and H3 labels consistently in rows and CE | KEEP; evidence complete; all reported control checks true |
| Remove one CE record | WITHHELD; missing record identified |
| Duplicate a CE record | WITHHELD; duplicate identified |
| Add an unmatched CE question | WITHHELD; no source row identified |
| Replace a CE head member with an ID outside its RRF head | WITHHELD; permutation mismatch identified |
| Remove `exact.dense` from a row | WITHHELD; missing arm identified |
| Supply an absent prior ES path | WITHHELD; `PATH_ABSENT` |
| Supply an absent prior CE path | WITHHELD; `PATH_ABSENT` |
| Omit prior paths | KEEP remains possible; both checks explicitly report `NOT_CHECKED` |
| Put NaN values in one CE `scores` map | KEEP; evidence complete |
| Put nonnumeric strings in that score map | KEEP; evidence complete |
| Add an unknown row key | KEEP; key ignored |
| Put NaN in a consumed round-ID list | `AttributeError`; no positive result |
| Remove required CE key `ce_exact` | `KeyError`; no structured withheld result |

The existing assertion cases additionally confirm withholding for no CE records, duplicate rows, differing question sets between levels, and inconsistent head flags. Unknown extension keys being ignored is not itself a defect. The swapped-level and CE-score results demonstrate limits on evidence authentication beyond the implemented cross-file consistency checks.

**3. Behavioural regression value of the new tests.**

I did not run the original tests that create temporary directories. I executed their assertion methods with equivalent in-memory fixtures against the base and HEAD scorers. All 11 assertion cases passed against HEAD.

These nine fail against `be746a0` because of observable behaviour, without an ImportError or missing callable API:

| Test in `IncompleteEvidence` | Base outcome causing failure |
|---|---|
| `test_no_cross_encoder_records_withholds` | KEEP |
| `test_one_missing_cross_encoder_record_withholds` | KEEP |
| `test_duplicate_cross_encoder_record_withholds` | MEAN-FAIL |
| `test_extra_cross_encoder_record_withholds` | KEEP |
| `test_duplicate_row_withholds` | KEEP |
| `test_question_set_differing_across_levels_withholds` | KEEP |
| `test_cross_encoder_head_not_a_permutation_of_its_rrf_head_withholds` | KEEP |
| `test_cross_encoder_head_flags_must_match_the_rows` | KEEP |
| `test_prior_reproduction_path_given_but_absent_is_a_failure_not_a_skip` | KEEP |

The remaining two test newly added reporting fields:

- `test_complete_fixture_still_keeps`: base returns KEEP, then fails with `KeyError: 'evidence'`.
- `test_prior_not_supplied_is_recorded_as_not_checked`: fails with `KeyError: 'reproduction_prior_es'`.

`Guard.test_nested_pit_or_script_is_refused` fails behaviourally against the base because its first nested script body reaches the fake client without raising. It passes against HEAD.

Thus section 7’s blanket statement about the `IncompleteEvidence` tests returning KEEP or MEAN-FAIL should distinguish the nine behavioural failures from the two reporting-field failures.

**4. Rescoring and preservation of existing results.**

Three comparisons give different, compatible conclusions:

- **Committed base JSON versus committed HEAD JSON:** every pre-existing key is unchanged.
- **Base scorer versus HEAD scorer, both rescored locally:** every pre-existing key is identical after JSON normalization.
- **Local Python 3.9.6 rescore versus committed base JSON:** category remains KEEP, but 72 numeric leaves differ by at most **`3.3306690738754696e-16`**. All are below the documented `1e-9` agreement tolerance. I normalized the caller-dependent `run_dir` path for this comparison.

The literal numeric differences are listed below using paths relative to `levels.<level>`. Projection indices are zero-based; the same listed subfields differ in both `projection` and `projection_tight`.

| Level | Paths that differ |
|---|---|
| H0 | `arms.bm25.session_top10_overlap`; `arms.dense.{exact,es}`; `arms.rrf.{es,round_top10_overlap}`; `arms.ce.{es,round_top10_overlap,session_top10_overlap}` |
| H0 | `controls.ctl_whitespace.{es_recall,mean_diff_to_exact_bm25}`; `controls.ctl_keyword.mean_diff_to_exact_bm25`; `wrong_query.dense.es_real`; `wrong_query.rrf.{es_real,es_wrong}` |
| H1 | `arms.bm25.{es,round_top10_overlap}`; `arms.rrf.{mean_diff,round_top10_overlap,session_top10_overlap}`; `arms.ce.{mean_diff,round_top10_overlap,session_top10_overlap}` |
| H1 | `arms.ce.{projection,projection_tight}`: `[0].hi`, `[1].lo`, `[1].hi` |
| H1 | `controls.ctl_whitespace.{es_recall,mean_diff_to_exact_bm25}`; `wrong_query.bm25.es_real` |
| H2 | `arms.bm25.{mean_diff,round_top10_overlap,session_top10_overlap}`; `arms.dense.session_top10_overlap`; `arms.rrf.{es,round_top10_overlap,session_top10_overlap}`; `arms.ce.{es,round_top10_overlap}` |
| H2 | `arms.bm25.{projection,projection_tight}`: `[0].lo`, `[1].lo` |
| H2 | `controls.ctl_whitespace.mean_diff_to_exact_bm25`; `wrong_query.rrf.es_real` |
| H3 | `arms.bm25.{exact,session_top10_overlap}`; `arms.dense.round_top10_overlap`; `arms.rrf.{exact,round_top10_overlap}`; `arms.ce.{mean_diff,round_top10_overlap,session_top10_overlap}` |
| H3 | `arms.ce.{projection,projection_tight}`: `[0].lo`, `[0].hi`, `[1].lo`, `[1].hi` |
| H3 | `controls.ctl_whitespace.mean_diff_to_exact_bm25`; `controls.ctl_keyword.mean_diff_to_exact_bm25` |

Outside `levels`, differences occur in:

- `reproduction_prior_es.max_abs`
- `reproduction_prior_es.cells["H0/dense"].{exact_diff,es_diff}`
- `reproduction_prior_es.cells["H0/rrf"].es_diff`
- `reproduction_prior_es.cells["H1/bm25"].es_diff`
- `reproduction_prior_es.cells["H2/rrf"].es_diff`
- `reproduction_prior_es.cells["H3/bm25"].exact_diff`
- `reproduction_prior_es.cells["H3/rrf"].exact_diff`

No recommendation, classification, control result, or reproduction pass/fail changes. These rounding differences are not a material finding.

**5. F2 wording.**

The original “test split was never read” error has been corrected. The parenthetical disclosure accurately acknowledges full-dataset loading. Pilot-only evaluation is consistent with the inspected retrieval and reranking loops. The remaining “inspected” claim should be narrowed as described in N4; absence of historical tuning cannot be established from this snapshot.

**6. Guard assessment.**

The recursive dictionary/list traversal at `es_guard.py:18–32`, invoked before the inner request at lines 64–70, rejects nested `pit` and `script*` keys. The new negative test exercises nested dictionaries and lists and verifies zero inner calls.

I also sent **16 representative search bodies** through the guard and a fake client, covering H0–H3 filters, ordinary BM25/dense searches, and whitespace/keyword analyzer controls. All were accepted, including query text containing the words “script pit.” I found no guard regression affecting the inspected pilot search shapes. Elasticsearch execution was not verified.

**7. Other findings and use verdicts.**

Section 2 appropriately discloses the failed CE reproduction tolerance and labels its batch-padding diagnosis unverified. The own-node needle claim is appropriately bounded. Section 7 overstates completeness in light of N1–N3 and should describe the checks actually established.

- **LAB: PASS WITH LIMITS** for exploratory analysis of the inspected, pinned 376-row/376-CE artifact, with 94 questions and 90 clusters independently counted. This does not accept the scorer’s general completeness guarantee.
- **CLIENT: BLOCK.** Resolve N1 and N2 before relying on automated recommendations as validated client evidence. The disclosed CE reproduction discrepancy and pilot-only projection also remain limits on interpretation.

The code-inspection block follows from concrete implementation findings, independently of pending full validation. No migration is involved. Reverting the validation fix would restore the known CE omission defect; correcting validation and regenerating the derived score is the appropriate repair path.

**8. Threshold chronology.**

The supplied extract is consistent with the plan and write-up: plan, thresholds, implementation, then results. I independently verified that base and HEAD both reference threshold blob `8d868f520cdea4fe30fd2d91474b54230b4a2271`, matching the extract.

I cannot verify intermediate commit chronology from the two-commit snapshot. Its chronology remains supplied, unverified evidence. Section 2 explicitly discloses `real_es > 0` as a tightening added in the scorer and absent from the thresholds file. The committed real-query recalls are positive, consistent with the claim that this addition changed no observed control outcome.

I did not verify Elasticsearch, model execution, remote logs, historical human inspection, or full validation. The supplied 21-test success log was inspected as evidence, not independently reproduced through its filesystem-writing test harness. No repository files were modified. Unrelated session ops alerts were reported separately and left untouched.

CODE_INSPECTION_RESULT: BLOCK
