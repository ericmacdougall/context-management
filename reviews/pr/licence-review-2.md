Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: pr
Reviewed HEAD: 813c1ddad1938281029a15078d12c8188d454174
Reviewed repository: ericmacdougall/context-management
Reviewed PR: 4
Reviewed packet SHA-256: 3cdcb88c1c621731aada9ba2347ceda0231cc04ced99cde5c715c9dba484dc3f
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 207136e97a9a8b9f6cfa4df88829ebf4c4f9c1be)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

VERDICT: BLOCK

**F1 — MATERIAL / Medium: invariant 3 lacks evidence for `eval/`.**  
The target commit `ca8e412da32c1cbca949e71744593c49f24d5f6a` is absent from the snapshot. The packet supplies target object IDs for the design documents, `gotchas/` and `reviews/`, but supplies only a directory listing for `eval/`. That establishes its presence, not equality. HEAD’s `eval/` tree is `702f307bc32f386d7a1420707baf10c9c74fd127`. I cannot establish the claim that only `licence/` and `memcore/` differ from lab/integration.

**Correction:** supply the target snapshot, or a complete target-tree comparison including `eval/` and file modes. This is missing verification evidence, not an observed unexpected modification.

**F2 — MATERIAL / Medium: failed bulk requests can appear successful to the migration helpers.**  
At `licence/lib.py:102`, a response without `items` becomes an empty list. Line 106 consequently reports `failed: 0` for a transport failure, HTTP 503 error body, or malformed response lacking item results.

Both `migrate_vectors` (`licence/exp3.py:264`) and `migrate_semantic` (`licence/exp3.py:310`) ignore the returned HTTP status and accept only `failed == 0`. They can return normally after copying none of a submitted batch. Their destination refresh responses are also unchecked. Subsequent destination verification may catch missing documents, but the migration helper itself does not reliably signal failure.

The migration tests replace `bulk` with an always-successful fake at `licence/tests/test_gates.py:203`, so they do not cover this failure.

**Correction:** require a successful HTTP status, exactly one valid result per submitted operation, and successful item statuses. Raise on missing or truncated item results. Validate destination refresh status and shard failures. Add negative cases for transport failure, HTTP 503, missing items, truncated items and destination refresh failure.

**F3 — MINOR / Low: the ledger understates the current fake’s coverage.**  
`LEDGER.md:157` says the in-memory fake “does not validate the scroll id.” The current fake rejects a stale scroll ID in `licence/tests/test_gates.py:196`.

**Correction:** qualify that statement as historical or update it to describe the current test. Live-node validation remains absent.

The remaining requested checks produced these results:

- **HEAD matches** `813c1ddad1938281029a15078d12c8188d454174`.
- **Invariant 1 holds directly.** The feature-base-to-HEAD diff is empty for `licence/` and `memcore/`, including modes. Both subtree IDs match the packet.
- **Invariant 2 is supported by the supplied hashes.** HEAD’s four top-level design-document hashes, `gotchas/` and `reviews/` match the packet. I could not independently inspect the target side.
- **No merge conflict marker, dropped stream file, committed symlink or unexpected top-level path was identified.** The compressed licence results are substantial but intentional evidence artifacts; the largest is approximately 5.2 MB. All 31 licence manifest hashes match.
- **No current T4 verification overclaim was found in the inspected status disclosures.** `licence/DEGRADE.md:15` explicitly states: “T4 is now UNVERIFIED in all twelve lives”. The corrected summary contains twelve `UNVERIFIED` statuses. The documentation discloses the missing oracle inputs, missing writer evidence, fake-only helper validation and client-use block.
- **No real licence payload, API key or password was identified under `licence/`.** A credential-pattern scan included the compressed results and found no candidates. `licence/lib.py:13` contains a password-file path, not a password. There is no discovered secret value to quote. This was a bounded scan, not proof that every possible secret format is absent.

I did not run tests, modify files, access external credentials or contact a live store. The supplied log reports 42 tests passing; it does not cover F2’s negative cases. No merge-specific regression was established, but F1 prevents verifying the full integration scope, and F2 is a concrete failure-handling defect in the imported code. Deployment, live migration and rollback remain unverified.

Overall result: BLOCK pending F1’s comparison evidence and F2’s failure-handling correction and tests.
