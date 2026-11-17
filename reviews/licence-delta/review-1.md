Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: fad672c080970f69a6b6012fcc2b8372ee235878
Reviewed packet SHA-256: f888f0e1c30ad828f54b721ece384207830247d05c79c3b56fbd8130111c3ea5
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base cc45153374f58a228aeb313b921f5fbeb3fa43f4)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

CODE_INSPECTION_RESULT: BLOCK

Inspection only; this is not PR signoff. HEAD matches `fad672c080970f69a6b6012fcc2b8372ee235878`. I inspected its diff against `cc45153374f58a228aeb313b921f5fbeb3fa43f4`, the changed source, tests, documentation, and committed results. Pending full validation is not the reason for this block.

1. **Prior findings F1–F5**

   | Finding | Status | Evidence and remaining limits |
   |---|---|---|
   | **F1: missing reproducible evidence** | **PARTIALLY CLOSED** | All twelve `.rows.gz` files and down_7’s oracle/poll files are present. I verified all **31 manifest hashes** and recomputed `recompute_all.json` exactly from the committed artifacts. However, write acknowledgement/final-presence IDs, semantic-writer rows, and eleven lives’ oracle inputs remain unavailable, as disclosed at `licence/DEGRADE.md:18`. `licence/matrix.py:163` now records `bulk_http`; this does not establish the missing HTTP status for historical initial bulks. |
   | **F2: unrelated failures passing licensing gates** | **PARTIALLY CLOSED** | T5 now requires positive denominators and licence-specific failures: `licence/summarize.py:103` and `:109`. The semantic classifier at `licence/exp2.py:59` distinguishes unrelated 403/404 responses. Historical T4 verdicts still pass without evidence satisfying that classifier; see **N1**. |
   | **F3: unchecked backend answers counted correct** | **CLOSED for implementation** | `licence/exp2.py:259` skips examination when the backend oracle is absent; examination increments only after a comparison. `licence/summarize.py:112` requires an explicitly present zero missing-oracle counter. Independent historical verification remains limited by F1. |
   | **F4: partial migration accepted as complete** | **PARTIALLY CLOSED** | `licence/exp3.py:199` rejects unsuccessful, timed-out, and shard-failed scroll pages; `:220` rejects duplicate IDs; `:227` updates the scroll ID; `:231` checks the independent count. Both migration paths collect before bulk writes. `licence/summarize.py:167` checks historical source counts against separately recorded counts. The new count acquisition still accepts failed refreshes and partial counts; see **N2**. |
   | **F5: incorrect response population** | **CLOSED** | `licence/DEGRADE.md:17` correctly attributes **387,045** responses to the original ten lives and **433,464** to all twelve. |

   Read-only recomputation covered **1,055,037 request rows**. It reproduced the committed recomputation output exactly: zero T2 failures in every life, T5 licence-specific fractions of **1.0** in every life, and matching recorded T2/T3 figures. For down_7, it reproduced **27,599 T1 examinations, zero mismatches, and 5,925/5,925 broken-control detections**. These results do not independently validate the other eleven lives’ T1 answers or any life’s write-ID reconciliation.

2. **Do all 26 new tests fail against the old code for the right reason?**

   **No.** The statement at `licence/tests/test_gates.py:3` that every test fails on the old code is inaccurate. The following assessment comes from inspecting both versions, rather than running the test suite in this sandbox.

   | Tests in `licence/tests/test_gates.py` | Assessment against old code |
   |---|---|
   | `test_manifest_matches_every_file` | Detects the missing artifacts against the complete old snapshot, but its first failure is the hardcoded **31 versus 30 manifest entries**. That failure alone does not demonstrate artifact integrity. |
   | `test_recompute_from_committed_rows_reproduces_recompute_all` | Against the old snapshot, fails because the row files are absent. With current artifacts and old recomputation logic, the newly added output fields also cause an expected-output mismatch. Useful reproduction coverage, but not an isolated failing-first test of the corrected formulas. |
   | `test_down_control_failing_for_unrelated_reasons_does_not_pass`; `test_up_control_before_trial_failing_for_unrelated_reasons_does_not_pass`; `test_up_legacy_record_without_licence_count_fails_closed` | Fail for the intended gate behavior: old logic accepts the supplied unrelated or unclassified failures. |
   | `test_down_control_with_licence_failures_still_passes`; `test_down_control_with_no_requests_does_not_pass` | **Already pass under old logic.** They are useful compatibility/boundary tests, not regression failures demonstrating the fix. |
   | `test_up_legacy_record_is_backfilled_from_rows` | Old code fails on the missing `licence_failures` key. It checks that backfilling produces a value and a passing gate, but not that the exact independently expected count is returned. |
   | All six `SemFailureClassTests`: `test_licence_signature_is_loud`, `test_unrelated_403_is_not_loud`, `test_missing_index_404_is_not_loud`, `test_missing_endpoint_404_is_loud`, `test_real_recorded_404_bodies_are_missing_endpoint`, `test_transport_failure_is_not_loud` | **All fail with `AttributeError` because old code has no `classify_sem_failure`.** They check the new helper, but do not demonstrate the old verifier’s incorrect classification through its public behavior. The unrelated-403 and missing-index cases are the substantive behavioral regressions. |
   | `test_backend_answer_without_oracle_is_not_examined`; `test_gate_requires_complete_oracle_coverage`; `test_gate_fails_closed_when_counter_absent` | Fail for the intended missing-oracle counting/gating defects. |
   | `test_backend_answer_with_oracle_is_examined` | **Already passes under old logic.** |
   | `test_non200_scroll_after_first_page_raises_and_writes_nothing`; `test_shard_failure_raises`; `test_independent_count_mismatch_raises`; `test_semantic_non200_scroll_raises`; `test_check_vectors_non200_scroll_of_destination_raises` | **All first fail with `AttributeError` while evaluating `exp3.ScrollError`, before invoking the migration/check.** Their intended cases are valuable, but the old-version failures do not establish those behaviors. |
   | `test_complete_scroll_migrates_every_document` | **Already passes under old logic.** |
   | `test_recorded_lives_have_independent_source_counts_and_pass` | Old code fails on the new `source_count_independent` output key after checking the existing migration verdicts. This checks new output structure and historical consistency. |
   | `test_source_count_not_matching_independent_count_fails` | Fails for the intended reason: old logic accepts a partial source/destination pair agreeing with itself. |

   None is intrinsically incapable of failing. Four already pass against the old logic, and several others fail because a newly introduced API is missing. The fake also never validates the submitted scroll ID (`:160`), so it cannot detect reuse of a stale ID. Timed-out pages, duplicate IDs, and count/refresh failures have no direct test here.

   **N3 — MINOR: overstated failing-first coverage.** Correct the claim at `licence/tests/test_gates.py:3` and the corresponding wording in `licence/DEGRADE.md:69`. For the behavioral regressions, exercise the existing verifier/migration entry points and use an exception assertion available in both versions, or document an explicit compatibility adapter.

3. **New defects and assumptions**

   **N1 — MATERIAL, high: corrected T4 verdicts silently accept unclassifiable historical failures.**  
   At `licence/summarize.py:97`, `w.get("sem_failed_other", 0)` treats a missing classification counter as proof that there were no unrelated failures. Every committed life lacks this field; every life nevertheless returns `T4_pass: true`, including down_1’s 254 semantic failures. Those classifications came from the old rule that accepted arbitrary error-bearing 403/404 responses.

   The prose disclosure at `licence/DEGRADE.md:70` is candid, but the machine-readable corrected verdict contradicts it. This is a material evidence problem in the implementation, independent of pending live tests.

   **Correction:** represent historical T4 as unknown/unverified, or fail it closed when classification evidence is missing. Preserve the legacy result in a separately named field with its rule version. Regenerate `summary_corrected.json` and add a regression case for a legacy record lacking the counter.

   **N2 — MATERIAL, medium: the independent source count does not establish a successfully refreshed, fully counted source.**  
   `licence/exp3.py:192` ignores the refresh response. Lines 193–196 accept an HTTP-200 `_count` with an integer count without inspecting shard failures. I evaluated this function in memory with a failed refresh and a partial count response; it returned the count successfully.

   A failed refresh can leave acknowledged documents invisible to both `_count` and the subsequent scroll. Their counts then agree while omitting those documents. The new completeness guarantee is therefore conditional on an unchecked prerequisite.

   **Correction:** require successful refresh status and zero refresh shard failures; require zero count shard failures. Add focused negative cases. Document that the source is quiescent between refresh/count and scroll, or enforce that condition. Separate requests do not establish a common source snapshot during concurrent mutation.

   The text-only `"non-compliant"` signature is another explicit assumption. It improves the observed licensing distinction but does not prove the rejected feature is the intended one. For this fixed laboratory workload it is a disclosed limit; broader client claims should check the expected status/error type and feature where available.

4. **Documentation, addendum C, and post-hoc gates**

   Addendum C clearly discloses its timing and the absence of a campaign rerun (`licence/THRESHOLDS.md:72`). T1, T5, and scroll changes strengthen the original stated requirements without relaxing numeric thresholds. Applying those corrections to existing evidence is reasonable where the retained evidence supports them.

   T4 is different: its evidence cannot be reclassified, yet the corrected summary still reports a pass. `licence/DEGRADE.md:15` stating that no recorded verdict changed needs qualification alongside the machine-readable correction in N1.

   The migration documentation explicitly says the new completeness steps have not run against a live node. That appropriately limits its claim, but the prescribed implementation needs N2 fixed. Historical source-count agreement supports the recorded migration counts; it does not validate the revised failure handling or make the recipe safe under concurrent writes.

5. **Use verdicts and verification limits**

   - **LAB: PASS WITH LIMITS** for exploratory use in the disclosed single-node, stub-model environment, treating historical T4 as unverified and the migration helpers as rehearsal code. N1 and N2 block acceptance of this delta’s corrected-gate/completeness claims.
   - **CLIENT: BLOCK.** The missing write evidence, eleven unrecomputable T1 lives, unclassifiable historical semantic-write failures, and unchecked migration prerequisites prevent client-facing assurance. The expressly pending live validation is also necessary before client reliance, but is not itself an inspection-stage finding.

   The in-memory-only exercises are sufficient to inspect the intended algorithm changes, not Elasticsearch integration. No rerun is acceptable for historical re-scoring only where retained inputs support it. Missing writer/oracle artifacts limit laboratory conclusions and block the corresponding client assurance.

   I did not run the supplied test suites, contact a node, access secrets, modify files, or follow packet paths outside the snapshot. I inspected the supplied reports of **26 gate tests** and **7 offline tests** passing. My additional execution was limited to read-only artifact hashing/recomputation and the isolated in-memory count check. I could not verify deployment, live behavior of the revised code, or commit-order claims beyond the two supplied snapshots.

STAGE: code-inspection
