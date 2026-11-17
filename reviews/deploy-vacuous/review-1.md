Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: 9465ed8ac010a4bfe3701e0f2a91b07465b265af
Reviewed packet SHA-256: 6aa300bff32822984d849d7df17e10f994952e37a90013d00a98b515ef5f45f8
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base b054d5dcce3375e8e86cbe90046379aa02a60d33)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

CODE_INSPECTION_RESULT: ACCEPT

Inspection only; this is not PR signoff. HEAD matches `9465ed8ac010a4bfe3701e0f2a91b07465b265af`. The diff changes only `memcore/tests/test_phase3_16096.py`. No material implementation issue was found in the inspected change.

1. **Was the base test vacuous as claimed?** Only its constructor-reachability assertion was weak; the whole test was not vacuous. The base already asserted that help contains `--backend` and excludes both subscription backend names. Reintroducing them as ordinary argparse choices would fail those assertions. Its invocation also supplied the now-unsupported `--dev-subscription-backends` flag and accepted any `SystemExit`, so an empty constructor record alone did not establish the rejection reason. The new stderr assertion meaningfully strengthens that part.

2. **Does the new probe detect reintroduced subscription backends?** Yes, for the argparse implementation inspected and the supplied control. Reintroducing the names as choices makes the help assertions fail. Accepting the supplied backend arguments also violates the expected `invalid choice` rejection or records constructor reachability. For historical `c20e208`, this conclusion is conditional: its actual source is absent. The supplied reconstruction exposes both names and reaches each patched constructor, so the production rejection assertions would fail against that reconstruction.

3. **Is the control meaningful?** Yes, as a synthetic implementation that offers the prohibited backends. At lines 556–570, it accepts both backend names and selects their constructors. The recorders raise `_Stop` before real constructor execution, so missing development authorization, adapter initialization, paid fallback checks, and gateway startup cannot explain the control’s recorded success. Lines 624–629 explicitly require both offers, the correct constructor for each backend, and absence of the rejection message. This demonstrates detection of offering and dispatch, not successful HTTP service.

4. **Can the new test pass without exercising its subject?** The ordinary bypasses listed in the packet do not produce a pass:
   - A gateway or adapter import failure becomes a test error; there is no skip handling.
   - An unknown flag, missing ID file, or silent early return does not satisfy the required `invalid choice` stderr assertion.
   - Missing help output fails the `--backend` witness assertion.
   - Construction through the patched adapter or `Gateway` records a failure.

   In the current gateway, backend choice validation occurs at `memcore/gateway.py:414`, before ID loading at lines 415–419. Thus these invocations exercise the intended parser rejection. The test deliberately stops there; it does not establish that an allowed backend can start successfully.

5. **Other findings and overclaims:**

   **F1 — MINOR, unresolved: historical fidelity is asserted without inspectable evidence.**  
   `memcore/tests/test_phase3_16096.py:548–552` and `:621–622` identify the control as the historical `c20e208` main, including “copied verbatim.” The available snapshot cannot establish that provenance. The evidence supports a synthetic control with the relevant parser and dispatch behavior. Rename/document it accordingly, or supply a reviewable historical source excerpt and its comparison. This does not undermine the control’s usefulness for this change.

   The diagnostic check at line 619 is also intentionally narrow: it checks a substring and does not assert the exit status or bind the diagnostic explicitly to `--backend`. That is sufficient for the inspected parser and fixed arguments; a more precise assertion would make future failures easier to diagnose.

**Use verdicts**

- **LAB: PASS WITH LIMITS** — the focused test change is acceptable by inspection. The supplied log reports 21 tests passing; I did not independently execute them.
- **CLIENT: PASS WITH LIMITS** — acceptable as a test-only change. This verdict establishes neither client deployment readiness nor live integration correctness.

No runtime behavior, dependencies, data format, or migration changes are introduced; rollback consists of reverting the test commit. Full validation remains pending as expected for this stage. I could not verify historical `c20e208`, actual mutation-run results, or live Elasticsearch, Postgres, or Temporal behavior. No tests were run and no files were modified.

Stage: code-inspection only; not PR signoff.
