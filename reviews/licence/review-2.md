Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: cc45153374f58a228aeb313b921f5fbeb3fa43f4
Reviewed packet SHA-256: 99abdbf9fba70d42c5f784f97753cf7ad799a7dabf8ed8e9e305b5831dd4a0b8
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base c2c7f64c205ff98b889155e74cbb275c4ae5d786)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

CODE_INSPECTION_RESULT: BLOCK

Inspected HEAD `cc45153374f58a228aeb313b921f5fbeb3fa43f4` and its diff against `c2c7f64c205ff98b889155e74cbb275c4ae5d786`. This is an inspection verdict, not PR signoff. The blockers below concern implementation and evidence integrity; they do not require the pending full-validation result.

1. **Reproducibility**

   **F1 — MATERIAL, high: the raw evidence needed for independent reproduction is absent from the snapshot.** `licence/results/MANIFEST.sha256:17–30` lists twelve `.rows.gz` files plus `down_7.oracle.gz` and `down_7.poll.gz`; none is committed. All sixteen available JSON artifacts match their manifest hashes. Hashes of unavailable files cannot establish their contents, and `recompute_all.json` is a recorded result, not an independently reproducible computation in this snapshot.

   The following values can be recomputed by adding or comparing the committed per-life JSON fields:

   | Headline | Recomputed from committed JSON | Verification limit |
   |---|---|---|
   | Original ten-life response total | **387,045 examined; 0 reported mismatches** | Cannot recheck response IDs against oracle inputs |
   | Original ten-life writes | **134,290 acknowledged; 0 reported absent; 0 failed batches; 0 present-unacknowledged** | No exported acknowledged-ID and final-present-ID sets |
   | Downgrade fallback, down_1–5 | **0.032008–0.056370 s** | Arithmetic agrees with “0.03–0.06 s”; timestamps cannot be rechecked |
   | Linear versus client RRF | **60/60 lists differ in each original life; 58–60/60 sets differ** | Saved ranking inputs are unavailable |
   | Broken-fallback control, original ten lives | **62,449/62,449 flagged** | Aggregate fields only |
   | down_7 independent-recompute artifact | **27,599 examined; 0 mismatches; 5,925/5,925 control responses flagged** | These are recorded outputs, not independently verified here |
   | Basic `bbq_disk` bulk | **40 items, 40 failed, 0 successful**, with a licensing error | `matrix.py` discards the bulk HTTP status, so this cell does not independently establish HTTP 200 |

   Correction: include the compressed read rows, oracle inputs and polling rows in the reviewable evidence package. Export write acknowledgements and final observed IDs as well. Until then, label these as harness-reported figures whose arithmetic agrees.

2. **Controls**

   **F2 — MATERIAL, high: licensing controls can pass for unrelated failures.** In `licence/summarize.py:80–89`, the downgrade T5 gate uses total failure fraction instead of `licence_failures / n`. The upgrade gate likewise accepts arbitrary pre-trial failures. I changed only in-memory fields to make every downgrade control failure unrelated to licensing; `T5_pass` remained `True`. `licence/exp2.py:213–214` also classifies any error-bearing 403 or 404 as a loud licensing-or-missing-endpoint failure, including unrelated authorization failures or missing indices.

   The original downgrade JSON records do report licensing failures specifically—**59,656/59,656**—so this does not demonstrate that those runs failed for another reason. It demonstrates that their acceptance gate does not enforce the stated control.

   Correction: require the expected licensing error signature and feature, or the specific missing-endpoint error type, with nonzero denominators. Add focused negative cases for unrelated 403, missing-index 404 and transport failures.

   The lexical-only broken-fallback control is meaningful: it changes the returned ranking, and the independent RRF checker can reject it. The migration corruption control also compares against the unmodified source: `licence/life.py:167–170`; committed results report exactly one mismatch. Neither is inherently unable to fail. Reranker success remains untested: an endpoint-not-found response demonstrates progression past a gate, not successful reranking.

3. **Vacuity and implementation failure modes**

   **F3 — MATERIAL, high: T1 can count an unchecked answer as examined and correct.** `licence/exp2.py:244–251` increments `examined`, then skips comparison when the backend oracle is missing. `licence/summarize.py:91` does not require `T1_backend_without_oracle == 0`. An in-memory check setting that counter to the entire examined count still produced `T1_pass=True`.

   The committed lives report zero missing-oracle answers, so this is a verifier defect rather than proof of observed mismatches. Correction: fail closed on missing references, count only actual comparisons, and gate explicitly on complete oracle coverage.

   **F4 — MATERIAL, high: an interrupted migration can be accepted as a complete migration.** In `licence/exp3.py:193–212` and `250–269`, a non-200 scroll response terminates collection without raising. The code then migrates the documents collected so far. Verification compares the destination against that same partial collection; `summarize.py:136–143` compares counts derived from it rather than an independently established source count. A failure after the first nonempty page can therefore leave a partial destination that passes count, vector and self-hit checks.

   Correction: reject failed, timed-out or partial-shard search responses; use returned scroll IDs; establish the expected source count independently; and require complete ID coverage before declaring success. Add a failure case after a successful first page. This matters directly to the migration recipe presented in `DEGRADE.md:56–61`.

   The oracle is independent of the adapter’s fusion implementation: `licence/oracle.py` obtains raw legs and implements RRF without importing `memcore.fusion`. However, `recompute.py` closely repeats that verifier’s algorithm. It provides a second implementation, not a separate ground truth. Its focused tests cover several ranking corruptions but do not cover the acceptance gaps above.

4. **Overclaim and limits**

   **F5 — MINOR: the revised denominator description is incorrect.** `licence/DEGRADE.md:16` associates **387,045** responses with down lives 1–6 plus the up lives. That population totals **405,865**. The original ten lives total **387,045**; all twelve total **433,464**. Name those populations explicitly.

   One recomputed life cannot independently validate the other eleven lives or retrospectively supply their missing oracle inputs. The write-up usefully distinguishes harness computation from recomputation, but the headline must retain that distinction.

   The down_7 unrefreshed-update records support the narrow “not reproduced” statement: both twins record zero searchable documents before update, twenty updated tags and twenty readable embeddings (`down_7.json:3739–3790`). They establish embedding presence, not equality of every embedding value.

   Thresholds were written after exploration, as disclosed in `THRESHOLDS.md:60–61`; their historical ordering cannot be verified with this two-commit snapshot. Repeated responses over sixty queries and a frozen index do not establish general retrieval correctness or independent statistical trials. The stub, patched transport and single-node workload also cannot establish real-model quality, production throughput, TLS behavior, multi-node behavior, clock expiry or recovery after downgrade on the same node.

5. **Verdicts**

   - **LAB: PASS WITH LIMITS** for retaining the committed records as exploratory, harness-reported observations. The arithmetic is consistent, and the new narrow disclosures improve the report. Independent raw-row reproduction and the affected automated acceptance claims remain unresolved under F1–F4.
   - **CLIENT: BLOCK.** The partial-migration acceptance path can conceal omitted documents, and the control and oracle gates can accept evidence that does not meet their stated requirements. These need correction before relying on this work as deployment assurance.

   I inspected the supplied seven-test success log and source. I performed read-only arithmetic, manifest checks and in-memory verifier checks; I did not rerun live services, access external artifact paths, read secrets or modify files. Full validation, deployment behavior and the unavailable raw evidence remain unverified.

Stage: code-inspection only; not PR signoff.
