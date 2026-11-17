Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: 181d33148e6afa464d3f629e3aef9a2c1e9b036f
Reviewed packet SHA-256: 08f26930eb76605a10c02fbd0ab6ceeaa0f179a8f148527deef052a2b0630750
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 3423f4eb0a858cbb10a35cd1da6630eeaa44e06d)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: 30473053fc0b9537b5e5b165ceda41a5ef9e1bb293756c04e0f0b7e34fcb020f

CODE_INSPECTION_RESULT: BLOCK

Inspected HEAD `181d33148e6afa464d3f629e3aef9a2c1e9b036f` and its diff against `3423f4eb0a858cbb10a35cd1da6630eeaa44e06d`. This verdict concerns implementation inspection only. Pending full validation is not itself a blocker.

**F1 — MATERIAL / High: The guard does not protect workflow initialization.**  
`engine/guard_interceptor.py:19–22` checks state only in `execute_workflow`. The workflows load state in `@workflow.init`: `engine/wf/v1.py:51–67` copies and reads it, while `engine/wf/v2s.py:54–69` migrates and reads it. Workflow initialization precedes execution of the run method. Consequently, the claim that refusal occurs “before any workflow code runs” (`engine/DEPLOY.md:12`) is unsupported by this placement; initialization can fail before the named guard runs. The fake-next tests never instantiate a workflow and cannot expose this.

Correction: validate at the beginning of workflow initialization, before copying, migrating, or indexing state, using a shared guard and an explicit supported version for each implementation. Add an SDK lifecycle test showing incompatible state reaches neither initialization logic nor handlers and produces the intended task failure.

**F2 — MATERIAL / Medium: The repository’s standalone replay path bypasses the guard.**  
`engine/exps5.py:67–68` constructs `Replayer` without interceptors. Registration in `engine/worker.py:77` applies only to that worker construction. Offline replay can therefore execute older workflow code against newer state without this named refusal; an additive newer state need not cause non-determinism.

Correction: share guard configuration between workers and replayers, including initialization protection, and add a replay regression using newer state with an otherwise compatible command history.

**F3 — MATERIAL / High: The deployment instructions describe a migration boundary the implementation does not enforce.**  
`engine/DEPLOY.md:28–30` says state migrates at a continue-as-new boundary. In fact, `engine/wf/v2s.py:56` migrates on initialization, including when a newer worker reconstructs an existing run from history. Worker pinning is optional (`engine/worker.py:46–48,66–74`), and deployment mode defaults to `AUTO_UPGRADE` unless `--pinned` is supplied.

Following the documented boundary assumption is therefore insufficient to establish when migration occurs or when worker rollback remains possible.

Correction: document migration on entry/replay and require a concrete routing procedure that prevents incompatible workers from receiving migrated runs. If migration must occur only at a boundary, enforce that in code. Specify the commands and observable checks that establish the rollback eligibility described at `engine/DEPLOY.md:44`.

**F4 — MINOR / Medium: Invalid state and schema versions are not consistently rejected.**  
`engine/deploy_guard.py:59–67` accepts non-mapping input as version 1 and accepts zero or negative integer versions. `version_tuple` at lines 34–38 accepts signed numeric components and repeated leading `v` characters. These inputs can pass the guard despite being outside the documented version formats.

Correction: validate positive known state versions and a defined schema-version grammar. Preserve absent-marker compatibility only as an explicit legacy policy, preferably with legacy-shape validation. Add negative tests for these cases.

Answers to the packet’s questions:

1. **Every loading path:** No. Start and continue-as-new runs reach the execution interceptor, but initialization remains outside its protection (F1). Signals and queries have no explicit guard; their safety depends on lifecycle ordering and successful guarded initialization. Standalone replay bypasses registration (F2). Activities receive IDs and values rather than the session-state dictionary (`engine/activities.py:100–133`); there is no activity compatibility guard. The state guard also does not check database schemas.

2. **Absent, malformed, or spoofed markers:** Absent `ver` means version 1. Non-integer values, including booleans, are refused; non-mappings and non-positive integers pass. The marker is an ordinary payload field, without provenance or integrity validation. The normal session-start path creates it internally (`engine/ctl.py:77–89`), and inspected signal handlers do not overwrite it. Nevertheless, a caller able to submit workflow input can label newer-shaped state as version 1. Payload validation restricts shape and size, not version authenticity, and decoding performs no validation (`engine/codec.py:54–55`).

3. **Upgrade and rolling operation:** `state.migrate` provides the v1-to-v2 transformation and the guard permits older state. That supports the upgrade direction at the function level. Safe mixed-version operation is not established by this implementation or runbook; see F3. A refusal preserves availability only if compatible routing and recovery actually occur.

4. **Determinism:** The comparison itself is pure and emits no workflow commands, so no direct command-sequence change is apparent for accepted input. Existing-history replay compatibility and SDK lifecycle behavior remain unverified. The standalone replay omission is concrete.

5. **CLI refusal exits:** Newer schema/state returns 3. Missing required arguments produce argparse failure; malformed schema strings raise an uncaught exception, yielding non-zero rather than structured refusal JSON. The CLI is a comparison utility: it neither identifies an image nor connects to a database. It cannot independently detect connection failure, stale supplied values, or a falsely supplied image/schema pairing. Client deployment integration is not supplied, despite the present-tense statement at `engine/DEPLOY.md:11`.

6. **Rollback criteria:** They are measurable proposals: five-minute readiness, a one-percent stalled-session threshold, and any ledger discrepancy within the stated window. The `ASSUMPTION` label does not make them merely labels. Operational detail is incomplete: metric queries, reconciliation commands, and verification of worker rollback eligibility are absent.

7. **Failing-first evidence:** Not demonstrated behaviorally. The new test module cannot import `engine.deploy_guard` on the base. The “prior behaviour” test at `engine/tests/test_deploy_guard.py:59–64` demonstrates dictionary access, rather than exercising an old worker. A regression should run the same workflow lifecycle against both implementations and show the change from accidental execution/failure to named refusal.

8. **Skipped interceptor tests and remote claim:** The supplied run establishes ten passing tests and three skips. It leaves even the fake-next SDK interface checks unexecuted locally. Those tests would still not prove initialization ordering, worker startup, replay, handler behavior, task retry, or recovery. `DEPLOY.md:50–52` and the HEAD commit message consistently describe a claimed **combined 25-test run** at `ad63ac9`, with no server. I could not verify that remote run from supplied logs. The documentation correctly disclaims real-server refusal.

9. **Other overclaims:** “Before any workflow code runs” and boundary-only migration are contradicted by the implementation. Immediate resumption when a compatible worker polls (`engine/guard_interceptor.py:5–6`) also needs qualification for routing, pinning, and task retry timing.

**LAB use: PASS WITH LIMITS** for controlled comparison and experimental testing, with explicit input/routing controls and no reliance on the universal guard claims.  
**CLIENT use: BLOCK** on F1–F3 and the absence of demonstrated client deployment wiring.

No tests were run and no files were modified. Real Temporal refusal/recovery, Postgres-backed integration, existing-history replay, and deployed behavior remain unverified. The supplied operations alerts were not investigated because they are outside this snapshot-only review.

Stage: code-inspection only; not PR signoff.
