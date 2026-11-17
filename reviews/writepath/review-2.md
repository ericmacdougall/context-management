Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: 1fcf3f75c581ae286e21c75c4efb1a1ca72eaa4f
Reviewed packet SHA-256: 7669401b74aba3bf0f9c1705ff70144c615adce91ed83da2b4113d6491cc213b
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 5fc4bd6f00a8f6bceb42572b18f0befeef03aea1)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: 775bf90572a60e293f2896e6913efa36e75968537dc9c28d3b6ec71f0558fd78

CODE_INSPECTION_RESULT: BLOCK

Inspection only, not PR signoff. HEAD matches `1fcf3f75c581ae286e21c75c4efb1a1ca72eaa4f`. The blocking findings concern implementation and focused evidence; pending full validation is not itself a blocker at this stage.

1. **Amendments and chronology.** The runner extract is consistent with the documents and inspected result timestamps, but the two-commit snapshot cannot independently prove intermediate commits, isolated changes, or absence of earlier runs.
   - **14a:** explicitly changes the oracle after a FAIL. Allowing documents whose bulk completed before takeover began is a sound correction: those were legitimate writes. The original FAIL remains reported. The amended stamped run observed **zero in-flight cases**, so that window remains unverified.
   - **15b:** changes execution from inside `run_all` to a standalone run; scanner, corpus and scoring thresholds remain pinned. Its stated commit time precedes the standalone result.
   - **17:** changes the implementation under test and introduces the reproducer. **17a** discloses the fixture’s limited coverage. **17b** increases counts and adds cases; **17c** corrects the starvation measurement and introduces disk-validity rules after a shake-down.
   - **18:** introduces work-based gates after the slow observation. **18a** repairs outdated completion fixtures and controls without changing product acceptance rules. **18b** tightens the residue work gate, but also relaxes the residue control’s required sensitivity from both states to S1 only; “no threshold relaxed” needs that qualification. **18c** repairs the work metric, voids the resulting false PASS, and adds function-local planner settings.
   - **19/19a:** case C originally failed to establish repeated successful acquisitions followed by takeover. Releasing the previous taker before the next acquisition repairs that precondition. The `5d26746` FAIL remains in the JSON, README and amendment table.
   
   According to the extract, these were threshold-file-only commits, although **14a and 19 share one commit**, rather than separate amendment commits.

2. **One stamped run rule.** The committed inventory contains one result per named experiment at `62c6be5`; the additional projector-start result is at `5d26746`. This does not prove that no unrecorded repetitions occurred. The documents explicitly pre-state standalone execution for E1, projector-start and held-out scanning; I did not find a general prospective amendment replacing `run_all` with all the other singles. The external launcher is unavailable for inspection. The supplied `62c6be5..HEAD` extract shows changes only to thresholds and `run_all.sh`, with no `wp/` runtime change; that comparison cannot be independently reconstructed here.

3. **Deadlock fix.** The share-lock changes remove the two demonstrated subject-lock cycles while preserving exclusion against mark and legal-hold operations. The controls restore the relevant pre-fix lock statements, rather than the entire earlier system. The fixed-arm lock-order counts recompute from the committed rows. This is not proof of universal deadlock freedom: overlapping cascades sharing event, tombstone or fact rows are explicitly outside MM’s fixture. I found no additional concrete projector/events-guard cycle in the inspected paths.

4. **Erasure starvation.** The receipt, design notes and results README explicitly disclose unbounded mark delay under overlapping share holders. The MS rows support cancellation at 30 seconds followed by successful retry after contention ends. This qualification is not present everywhere erasure is described. Separately, `DESIGN-NOTES.md:36` calls physical purge “bounded” by 300 seconds, whereas the implementation records overdue jobs and continues waiting. A deadline is not a completion guarantee; see N4.

5. **Cascade and residue rewrites.** The lateral subqueries preserve the original tenant filters and containment predicates. The `EXCEPT` preserves the untombstoned-event set because the footprint is already distinct and event IDs are non-null. Function `SET` clauses scope the planner setting to function execution. They discourage sequential scans; they do not establish a universal complexity guarantee. The disclosed outer-delete tenant scan and GIN pending-list costs remain relevant. The facts-path evidence is vacuous; see N3.

6. **Projector startup.** Moving `start()` inside the stale-error handler fixes the demonstrated startup-race crash. Genuine ES failures are also caught because `fence_es` maps every alias-request `ESError` to `StaleEpoch`. This can silently leave projection unfinished while `run()` returns `True`; see N1. I found no cursor advancement or stale-writer bypass caused by this change. Work remains recoverable on a later invocation, but timely recovery is not assured.

7. **Held-out scanner failure.** The stamped result is FAIL, and its rows independently recompute to **483/1,169**, **150/750**, and **37/1,200 false blocks**. The README and design notes report the failure. The inspected `880/880` references qualify it as the author’s planted corpus. Raw historical PASS results remain historical evidence, rather than evidence of generalisation. The scanner’s module documentation should also point to the broader measured misses.

8. **Results hygiene.** All **70 manifest-listed committed files** match their committed SHA-256 values. E1 and lock-order summary counts checked against their rows also match; the held-out verdict follows from its recomputed counts. Without host originals, I cannot establish that home-prefix shortening was the only post-run edit. I found no obvious non-fixture credential in the inspected portions, but did not independently reproduce the claimed live-secret scan or establish an exhaustive credential-free result.

9. **Earlier Postgres settings.** The design notes explicitly disclose that earlier results depended on settings absent from the repository until `8c7d841`. Earlier result fingerprints generally record the server version, not these effective settings, so those results are not independently reproducible from their fingerprints alone. Carry the settings qualification into the result index and record effective settings in future fingerprints.

10. **VOID-only experiments.** The README lists these as PASS observations **inside an explicitly VOID run**, and separately enumerates those lacking stamped results. I did not find it presenting them as stamped passes. Preserve that distinction in downstream summaries.

Unresolved findings:

- **N1 — MATERIAL, high: ES startup failure can become apparent idle success.**  
  `writepath/wp/projector.py:172–187`, interacting with `writepath/wp/families.py:107–110`. An alias request fails with a genuine ES error; `step()` catches the translated `StaleEpoch` and forgets its lease. The lease remains live in Postgres. Two subsequent acquisitions return no lease, producing two zero-work rounds and `run() == True` despite pending work. Documentation acknowledges this, but does not resolve it.  
  **Correction:** distinguish confirmed stale-CAS failures from infrastructure errors, propagate/report infrastructure failures, and safely release the caller’s lease when appropriate. Add a focused failure-injection test proving pending work cannot produce apparent idle success and remains retryable.

- **N2 — MATERIAL, medium: disk-validity gate fails open when log retrieval fails.**  
  `writepath/exp/disk_gate.py:89–98`. The code ignores `docker logs`’ return code. A failed retrieval without the phrase “flood stage” produces `es_flood_lines 0` and successful completion, even though flood absence was not observed. The final usage measurement also prints a cap breach without rejecting it.  
  **Correction:** make failed or incomplete log retrieval invalidate the run, and enforce the own-usage cap at `end`. Add focused cases for log-command failure and a cap breach during the last experiment. This is a code finding, not an assertion that the supplied runs encountered either condition.

- **N3 — MATERIAL, medium: plan-stability facts gate passes without facts.**  
  `writepath/exp/plan_stability.py:104–108, 122–130, 175–187`. The fixture appends messages and never derives facts. The committed facts-delete measurements are zero while `holds` is true. Missing statement evidence also becomes zero through `per_event()`. The combined control can demonstrate sensitivity through analyst jobs alone.  
  **Correction:** populate matching and surviving facts at both sizes, require evidence that each measured statement was logged and processed representative rows, and demonstrate control sensitivity for the facts path. Alternatively, narrow the claimed proof explicitly to the populated analyst-job path.

- **N4 — MINOR, documentation: bounds and amendment wording overstate the evidence.**  
  `writepath/DESIGN-NOTES.md:36–37` and `writepath/exp/THRESHOLDS.md:438–445`. Replace guaranteed physical-purge completion wording with deadline/overdue semantics, and explicitly identify 18b’s reduced S2 control requirement. Keep the starvation qualification next to general erasure descriptions.

**LAB use: PASS WITH LIMITS** for isolated experimentation and repair, retaining failed/void evidence and the stated scope limits. This does not accept the current implementation or evidence gates as complete.

**CLIENT use: BLOCK.** N1–N3 remain unresolved. Additional disclosed limits include held-out scanner failure, unbounded mark starvation, lab service identities and KMS stand-in, and deferred ES authorization work.

I ran no tests, database/network experiments, or migrations, and modified no files. I could not verify deployment, rollback rehearsal, intermediate history, external-launcher behaviour, host originals, or complete full-validation results. The supplied config-guard success log and wrapper failure remain unreconciled; the wrapper and failure details are unavailable. Session-reported critical cron/deadman alerts were outside this snapshot review and were not independently verified.

Stage: code-inspection — not PR signoff.
