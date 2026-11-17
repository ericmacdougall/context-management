Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: 080186619958eb83e7c539686bfe6bc2167522cb
Reviewed packet SHA-256: 99f7e8d2e64b9b11a99bcc6ac81eb76ce9fa25f08070cc5d2b39d7578e9cba4e
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 71f3bfaa28eab7992fa9e859e5731e9daefeeb69)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

CODE_INSPECTION_RESULT: BLOCK

Reviewed HEAD `080186619958eb83e7c539686bfe6bc2167522cb`, matching the requested snapshot. The diff against `71f3bfaa28eab7992fa9e859e5731e9daefeeb69` adds 28 result artifacts; implementation and preregistration files are unchanged. This is inspection only, not PR signoff.

1. **Reproducibility**

   I ran the two read-only recomputation scripts against `results2/` and the original recomputation against `results/`; all comparisons matched their stored summaries. I also independently recounted selected JSON fields. This establishes arithmetic consistency, not independent reproduction of the experiments.

   | Headline | Recomputed result | Interpretation |
   |---|---:|---|
   | Crash matrix | 40/40 expected event sets | Named crash hooks, two crash-call positions, scripted stubs |
   | Late-tap control | 20/40 lose a stub-completed response; 8/40 lose one returned to the host | Original results report 16/40 and 8/40 respectively |
   | Real tap loses a stub-completed response | 4/40 | All at `after_send_before_resp_log` |
   | Duplicate/reordered delivery | 20/20 seeds, each provider | Separate from concurrent racing |
   | Thread races | 10/10, each provider | Not 20 race seeds |
   | Process races / stale zombie | 5/5 / 10/10, each provider | SQLite implementation |
   | Provenance | 0 marker leaks in covered placements; role-only control 21/28; altered-block leaks 7/7 | 49 conversations total; only 35 contain a genuine injected block in covered placements |
   | Unwitnessed truncation | 0/20 detected; 8/20 report complete in new results | Original results: 0/20 detected, 20/20 report complete |
   | Sliding/compacting histories | 12/12 preserve event counts | Six host/length combinations per provider; this measures retention, not detection |
   | Scanner | 400/400 tuned; 350/800 held-out | Aggregate predicate counts, with the oracle defect below |

   References: `ingest/results2/item1-be3be7494535.json`, `item2a-*`, `item2b-*`, `item2c-*`, `item2d-*`, `item3-76b99c151ef3.json`, `item4-f60a1b3d65fc.json`, `item11-19a315475d48.json`, and `item12-150ab086bc6a.json`.

   The truncation difference is explainable: both datasets have 12 odd cuts and eight even cuts. The new implementation refuses completeness at the 12 cuts ending with an outstanding request; the eight cuts ending after a response still report complete. `ingest/ingester.py:398–411` and `ingest/proof_items_123.py:456–459` support that distinction. These are different run versions, not conflicting arithmetic.

2. **Controls**

   **F1 — MATERIAL, medium: the “four-layer” control leaves the chain check enabled.**  
   `ingest/run_proofs.py:10` disables fencing, uniqueness and the state guard but omits `chain=False`. The new artifact confirms `"chain": true` in `ingest/results2/item2b_control_4layers-0fda0260bad5.json:16`. It produces zero duplicate runs and zero baseline-identical runs; it does not meet the registered duplicate-sensitivity floor. A stale replay can instead encounter a chain break and stall ingestion.

   Correction: pass `chain=False`, regenerate or relabel that artifact, and state which registered control failed. The separate `item2e` all-off ablation supplies useful evidence: 10/10 duplicate runs. It should be identified explicitly as the successful replacement control.

   Other controls are sensitive within their stated scope:

   - LateTap deliberately delays durability and demonstrates response loss.
   - ArrivalKeyedIngester removes ordering and deduplication together (`controls.py:63–88`); its result cannot isolate which property caused failure.
   - RoleOnlyIngester’s system placement cannot put a marker into a **user** event. Thus 21/28 is a mixed-placement denominator; all seven system-placement runs are outside that particular failure opportunity.
   - ScanAfterWriteTap leaves `_red` as a no-op (`controls.py:118–122`). It demonstrates raw-write exposure, but does not exercise an actual subsequent cleanup attempt.
   - WatermarkFreeReporter reports the maximum sequence by construction and demonstrates the intended hole-blind behavior.

3. **Vacuity and implementation failure modes**

   **F2 — MATERIAL, high: the scanner’s “caught” oracle accepts partial redaction that leaves credential material.**  
   `ingest/heldout.py:42` constructs a private-key header followed by a generated body. The scanner matches only the header (`ingest/scan.py:36`). The oracle at `heldout.py:69–74` passes whenever the entire original variant disappears and the separately generated `sk-live-…` string is absent. For this class, that `sk-live-…` string was never inserted. Removing the header therefore earns 50/50 while leaving the body.

   Correction: define each fixture’s sensitive payload separately and assert its removal, using structurally valid synthetic key fixtures where appropriate. Report detection and complete redaction separately. Preserve this result as the original predicate score; a changed oracle requires a separately labeled evaluation.

   **F3 — MATERIAL, high: content alignment can silently drop legitimate repeated turns.**  
   Keys derive from message content (`ingest/tap.py:201`). At `ingest/ingester.py:304–306`, a request whose keys equal the preceding confirmed request is treated as a retry, suppressing its request events.

   Concrete case: repeatedly submit the same user text, receive the same answer, and retain the last four history messages. Once the window fills, consecutive requests have identical content sequences. The next legitimate user occurrence is suppressed while its assistant response is ingested, and completeness can remain true.

   The supplied window tests use unique `qN`/`aN` text (`ingest/proof_items_followup.py:40–42`; `ingest/tests/test_followup.py:30–35`), so they do not cover this ambiguity.

   Correction: use occurrence identity or explicit retry identity. When identity cannot be established, report ambiguity rather than silently suppressing a turn. Add a focused repeated-content window case.

   **F4 — MATERIAL, medium: the duplication oracle cannot establish semantic completeness.**  
   Both baseline and reordered/racing arms use the same `Ingester` (`ingest/proof_items_123.py:149–155, 179`). Shared omissions can therefore pass comparison. This remains a useful delivery-invariance test, but cannot support an unrestricted “exactly the completed turns” claim.

   Correction: retain this comparison and add an independent expected-event ledger from the scripted conversation, including repeated content and retry distinctions. The crash test does have a separately constructed expected table; the duplication test should provide equivalent semantic independence.

4. **Overclaim and held-out status**

   **F5 — MATERIAL, medium: the headline wording exceeds the measured crash and provenance coverage.**  
   “After a crash at any point” is broader than the named-hook matrix. Four real-tap runs omit a stub-completed response. The stated exception makes the narrower claim defensible, but must appear alongside the headline. Likewise, “never ingested … over seven placements” includes an explicitly failing seventh placement. The provenance preregistration asks for 40 quote-back and forged-block successes, while the implementation runs seven per placement (`ingest/PREREG.json`, `item4_provenance`; `proof_items_456.py:155–176`). Neither amendment documents that sample-size change.

   Correction: state “40/40 named-hook expectations, including four unrecoverable completed responses,” distinguish six covered placements from the failing altered-block placement, and disclose the provenance sample-size deviation.

   **F6 — MINOR: independent held-out provenance is asserted, not demonstrated by this snapshot.**  
   The current `scan.py` blob matches the frozen hash `b4b63d74fe7dd33c31416802e9bae7fe05e57697`. The two-commit history cannot establish the earlier freeze chronology or generator independence. Several held-out classes also directly exercise patterns already present in the scanner, including bearer, AWS and password fields.

   The score is **43.75% on an equally weighted synthetic positive-only set under the implemented predicate**. It is not precision, a false-positive rate, or demonstrated deployment recall. Seven classes score 50/50; nine score 0/50. No negative examples are supplied. `heldout.py` constructs per-example rows but discards them, storing only per-class aggregates.

   Correction: retain an auditable freeze/generator record and per-example outcome evidence, and add a separate benign corpus for precision. Do not edit the frozen scanner and continue calling this same set unseen.

5. **Verdicts and verification limits**

   - **LAB: BLOCK as the current claim package.** The raw observations are useful and should remain available, but F1–F5 require correction or explicit narrowing before the package supports its headline claims.
   - **CLIENT: BLOCK.** F2 and F3 concern secret exposure and silent event loss. The evidence also uses SQLite and scripted provider shapes; it does not establish production database concurrency, actual provider recovery, host integration, or deployment durability.

   I inspected source and committed artifacts and used only read-only recounting. I did not rerun write-producing tests or proof experiments. The supplied runner reports 30 passing tests; full validation remains pending and is not a requirement imposed by this inspection. No production migration, rollback rehearsal, or live deployment evidence was supplied. The arithmetic recounts cannot verify the original on-disk scans or experimental execution.

Stage: code-inspection only; not PR signoff.
