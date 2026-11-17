Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: add7c86881ae2793ec32ecc3fa0f79dfd8d8add4
Reviewed packet SHA-256: 0b8d2632a614920de9797636b3b03716f25166bcc2417e693735caf11c7c2543
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 080186619958eb83e7c539686bfe6bc2167522cb)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

CODE_INSPECTION_RESULT: BLOCK

Inspected HEAD `add7c86881ae2793ec32ecc3fa0f79dfd8d8add4` against base `080186619958eb83e7c539686bfe6bc2167522cb`. This is an inspection result, not PR signoff. The block is based on implementation and claim issues below, not the pending full validation.

1. **Prior findings F1–F6**

   | ID | Status | Evidence and remaining limits |
   |---|---|---|
   | F1 | **CLOSED** | `ingest/run_proofs.py:10` now passes `chain=False` alongside the other three disabled layers. The committed four-layer control records all four flags false and duplicate rows in **10/10** runs. The original three-layer control remains disclosed as **0/10**. |
   | F2 | **PARTIALLY CLOSED** | `ingest/heldout.py:90` separates whole-string absence from the fragment-based complete-redaction predicate. Recomputed results are **300/800**, versus **350/800** under the old predicate; private-key examples change from **50/50 to 0/50**. The underlying scanner defect remains open: `ingest/scan.py:36` matches the header, leaving the body. Item 5’s disk check remains based on whole-string needles. |
   | F3 | **PARTIALLY CLOSED** | `ingest/ingester.py:312–337` removes implicit retry suppression and preserves the final request message unless a matching retry is declared. The recorded repeat matrix passes **18/18**, with ambiguity flags in every repeated-content run. Earlier newly authored messages can still disappear through alignment; see N1. |
   | F4 | **PARTIALLY CLOSED** | `ingest/proofs_common.py:130–173` constructs expectations independently of the ingester, log records, event keys, and production shape parser. The thinking-omission control is effective: baseline equals arm **20/20**, while the oracle detects the omission **20/20**. However, tool payloads are excluded from the comparison; see N2. |
   | F5 | **PARTIALLY CLOSED** | `ingest/CLAIMS.md:11–12` limits crash coverage to named hooks and explicitly reports four absent completed responses. Lines 21 and 37–39 disclose the failing seventh placement and **7 rather than 40** examples per placement. Lines 47–48 still make an unsupported universal at-least-once claim; see N1. |
   | F6 | **PARTIALLY CLOSED** | Per-example rows are retained, positive-only synthetic coverage is identified, and a synthetic benign set reports **0/600** changes. `ingest/CLAIMS.md:32–33` correctly admits that independence from tuning is not demonstrated. The opening assertion in `ingest/heldout.py:1–3` remains stronger than that evidence. |

2. **Failing-first evidence**

   The committed log’s **7 failures, 4 errors, 1 pass** is consistent with source inspection, assuming the added test-support code described in the log was present. It does **not** establish that every new test failed behaviorally on an untouched base.

   - **Behavioral failures supported by the old code:** `test_four_layer_control_disables_all_four_layers`; `test_same_text_windows`; `test_alternating_and_cycle_windows`; `test_repeats_are_reported_not_hidden`; `test_stateless_host_two_identical_turns_both_ingested`; and `test_undeclared_identical_resend_is_a_flagged_duplicate`. Their early assertions expose the old configuration or dropped-event counts.
   - **Missing-field/API failures:** `test_private_key_body_left_behind_is_not_complete_redaction` fails because `complete_redaction` is absent, rather than directly demonstrating surviving body material. `test_unique_text_windows_carry_no_ambiguity_flag` encounters missing `ambiguous_calls`. The two declared-retry tests encounter unsupported `retry_of`. `test_delivery_item_reports_oracle_match_per_run` encounters missing `oracle_identical`.
   - **Expected to pass on the old implementation:** `test_shared_omission_passes_baseline_comparison_but_fails_the_oracle`. It demonstrates control sensitivity; it is not a failing-first regression test.

   These tests are not inherently incapable of failing. However, the “each must FAIL” statement in `PREREG-amend-3.json` is contradicted by the recorded passing control test. Correct that statement and distinguish behavioral regressions, contract additions, and control demonstrations. The two-commit snapshot cannot independently establish the claimed chronology.

3. **Unresolved implementation findings**

   **N1 — MATERIAL, high: alignment can still omit a legitimate occurrence while the new exactly-once API reports success.**

   Locations: `ingest/ingester.py:325–349`, `ingest/ingester.py:441–444`, `ingest/CLAIMS.md:47–48`.

   Consider prior history `[user:"yes", assistant:"ok"]`. A host then sends two newly authored messages `[user:"yes", user:"new"]`, without `retry_of`. The longest-common-run result is `(start=0, length=1, ties=1)`. Only `"new"` enters the request-event tail. The new `"yes"` occurrence disappears; neither ambiguity condition fires.

   A write-free probe of the committed `_lcsub` function confirmed that alignment and emitted tail. From the surrounding source, a successful response leaves no alignment gap, so `claim_exactly_once()` can return true despite the omitted occurrence.

   The table discloses that multiple new messages are uncovered, but the API does not enforce that limitation, and the universal prose claim contradicts it.

   **Correction:** introduce stable host occurrence identities or an explicit new-message boundary, or enforce a supported host contract and conservatively report unsupported alignment. Scope `claim_exactly_once()` and the prose claim accordingly. Add a focused regression for this example asserting both event preservation and truthful completeness.

   **N2 — MATERIAL, medium: the independent oracle cannot establish tool-payload completeness.**

   Locations: `ingest/proofs_common.py:141–151`, `ingest/proofs_common.py:184–190`.

   Tool calls and results are reduced to their IDs. Tool names, arguments, result content, and error information do not participate in equality. A shared defect that preserves an ID while emptying or corrupting its payload passes both baseline comparison and the new oracle.

   A write-free probe confirmed that OpenAI tool messages with identical IDs but different result content produce identical oracle tuples.

   **Correction:** compare independently normalized tool payloads as well as IDs, and add a corruption control that preserves IDs while changing arguments or results. Until then, describe these results as equality of the specified tuple projection rather than complete semantic event equality.

   **F2 remains MATERIAL, high for client secret protection:** acknowledging the surviving private-key body does not repair it. Fix or replace the reference scanner before relying on it for client data, and rescore the persistence evidence using a predicate that detects partial credential survival.

4. **Does `CLAIMS.md` exceed measured denominators?**

   **Yes.** Lines 47–48 say every message is ingested at least once, despite the multiple-new-message exclusion and N1. The new `claim_exactly_once()` also lacks a host-contract qualification.

   Most numerical claims are now appropriately bounded. Both recomputation commands completed with **zero mismatches**:

   - `python3 -B ingest/recompute.py ingest/results3`
   - `python3 -B ingest/recompute2.py ingest/results3`

   This verifies arithmetic consistency with stored rows. In particular, recomputing `oracle_match` totals counts stored booleans; it does not reconstruct semantic equality from independently stored expected and actual payloads.

5. **Specific F3 attack answers**

   - **(a) Can a legitimate message still be dropped?** Yes—an earlier newly authored message can align to old content and disappear, as in N1. The final-message safeguard protects a narrower case.
   - **(b) Can a dishonest or buggy host suppress a real message using `retry_of`?** Yes. If it names the last confirmed call and reproduces its keys, the request is suppressed even when it represents a real new occurrence. The port event makes suppression auditable, but ordinary `events()` excludes port events. This remains an explicit trusted-host assumption, not verified retry identity.
   - **(c) Is HostLedger independent?** Yes, in how expectations are obtained and projected. Its coverage is limited by its lossy tuple schema, particularly tool payloads.
   - **(d) Is a non-blocking gap adequate?** For controlled lab exploration of possible duplicates, yes, provided consumers inspect `ambiguous_calls` and avoid treating the stream as unique. It is inadequate as a general completeness guarantee: `claim_complete()` remains true across ambiguity, and N1 can produce loss without any ambiguity gap.

6. **Use verdicts and verification limits**

   - **LAB: PASS WITH LIMITS** for synthetic, offline experiments restricted to the demonstrated host patterns. Treat scanner misses as experimental results, use no real credentials, and do not rely on the universal completeness or tool-payload claims.
   - **CLIENT: BLOCK.** Known credential-body survival, unflagged occurrence loss, and incomplete semantic validation remain material. The altered-block provenance leak also requires a constrained integration contract or a repair.

   The smaller provenance sample supports the stated observed counts, not the registered 40-example thresholds. Stubs and SQLite support laboratory conclusions; they do not establish provider integration or production database behavior. Loaded-machine timings support indicative measurements only.

   I did not rerun filesystem-writing tests, verify the supplied 42-test success report independently, or verify deployment, migration/rebuild behavior, rollback, real providers, or production concurrency. No files were modified, and no external checkout or secret source was accessed.

Stage: code-inspection; not PR signoff.
