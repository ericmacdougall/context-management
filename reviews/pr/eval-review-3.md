Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: pr
Reviewed HEAD: 45d9ec57c0f96408f3e8d1064866d13e086880e0
Reviewed repository: ericmacdougall/context-management
Reviewed PR: 2
Reviewed packet SHA-256: b45147ac43d12b36f1cc6afe0fe056b4dad140378a9b0414f80808f58e59cae8
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base b50f9c8a70a586e2f50cdb070bd5f7666856c1ec)
Worktree status: clean

VERDICT: BLOCK

F1 — MATERIAL, evidence gap — `eval/` provenance. Invariant 1 cannot be verified from the supplied snapshot: feature commit `854ed7f15d425e883f6c5b2c00f4c89afb8f1ea6` is absent (`git cat-file -e …^{commit}` fails). HEAD names it as a parent, but that does not prove subtree equality or rule out dropped files or conflict-resolution changes. The reviewed HEAD’s `eval/` tree is `702f307bc32f386d7a1420707baf10c9c74fd127`. Supply the feature commit and its tree objects in the review snapshot so the reviewer can independently compare them. This blocks the claimed merge invariant; it is not proof that the merge is incorrect.

F2 — MINOR, documentation — `eval/RUNBOOK.md:44`. The documented legacy indexing command passes `--credential-file`, but `eval/emlab/indexer.py` requires `--curl-config` and does not accept `--credential-file`. The command fails argument parsing. Update the example to match the supported interface or remove the obsolete indexing example.

The remaining requested checks produced these results:

- **Expected HEAD:** confirmed as `45d9ec57c0f96408f3e8d1064866d13e086880e0`.
- **Invariant 2:** verified. `DESIGN.md`, `LEDGER.md`, `OPEN-QUESTIONS.md`, `CLAIMS-AUDIT.md`, and `gotchas/` have identical Git object IDs to base `b50f9c8a70a586e2f50cdb070bd5f7666856c1ec`.
- **Invariant 3:** verified as “nothing added outside `eval/`.” The diff consists of 162 added files under `eval/`, with no modifications or deletions elsewhere.
- **Integration and artifacts:** all 83 Python files parsed successfully, and the inspected relative imports resolve within the snapshot. The tiny test fixture and pilot evidence files are committed. `.gitignore` excludes only Python caches. Both artifact hashes in the pilot manifest match the committed files. No conflict markers, symlinks, submodules, or unexpected file extensions were found.
- **Public-branch contents:** the largest binary is the approximately 2.8 MB compressed pilot rows artifact, explicitly documented as evidence. Machine-specific paths exist in the runbook and stage documents; they are reproducibility references, not missing repository dependencies. I found no concrete publication blocker in the inspected content, but did not establish that every artifact is free of sensitive information.
- **Claims:** the merge message preserves lab-only status, client blocking, and later coordinator checks. The inspected stage and pilot documents distinguish exploratory results, unvalidated judgments, reproduction failures, and evidence limits. I found no claim that this merge establishes client readiness.

Validation was limited to read-only source and artifact inspection. The supplied test evidence reports **141 tests, OK, 3 skipped**, consistent with the 141 test methods present in source. I did not rerun the suite because tests create temporary files, and I could not independently verify the skipped cases, live-store behavior, model behavior, or historical review receipts. No files were modified.

Overall result: BLOCK until F1 is resolved with independently inspectable feature-tree evidence.
