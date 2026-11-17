Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: pr
Reviewed HEAD: d254fa4a0be7b80ca729b60d25a95ef5608321ec
Reviewed repository: ericmacdougall/context-management
Reviewed PR: 3
Reviewed packet SHA-256: c093e13a8263ccbe40198ded73210a092f55dc22d1ba691d9a01238a628beea0
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base d76e5258afc6be4ba400be787668fde0a3fb6b9f)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

VERDICT: BLOCK

F1 — MATERIAL, medium severity — Withdrawn ingestion claims remain presented as findings in the merged documentation.

- `LEDGER.md:157` says: “A crash at any point in a call leaves a log from which exactly the completed turns are ingested”.
- `LEDGER.md:158` says: “Exactly-once, ordered ingestion under duplication, reordering and racing ingesters”.
- `DESIGN.md:156` repeats “exactly-once under duplication and racing” and the crash claim.
- `ingest/ingester.py:1` describes the module as “Exactly-once, ordered ingestion”.

These exceed the packet’s stated withdrawal. Naming the old commit and marking results “owner-reported” or “unreviewed” does not explicitly mark the claims as withdrawn. The current `ingest/CLAIMS.md` provides materially narrower evidence: four named-hook crash runs lose a stub-completed response; ambiguous alignment produces duplicates and blocks completeness; declared boundaries depend on trusted host assertions.

**Correction:** mark the old documentation rows as superseded historical claims and point to `ingest/CLAIMS.md`. Describe ingestion as ordered application with duplicate suppression and explicit completeness limits. Because the merge promises to preserve target documentation byte-for-byte, make the documentation correction on the target branch and rebuild the merge, or explicitly revise that scope.

The remaining review results are:

1. **Invariant 1 holds directly.** HEAD is `d254fa4a0be7b80ca729b60d25a95ef5608321ec`. The requested diff against `d76e5258afc6be4ba400be787668fde0a3fb6b9f` is empty for `ingest/`, `memcore/`, and `.gitignore`, including file modes.

2. **Invariant 2 is supported by the supplied evidence, with a verification limit.** HEAD’s object IDs for `DESIGN.md`, `LEDGER.md`, `OPEN-QUESTIONS.md`, `CLAIMS-AUDIT.md`, and `gotchas/` match the packet exactly. The target commit is absent, so I could not independently establish its side of those equalities.

3. **Invariant 3 holds on the supplied target-tree evidence.** HEAD contains exactly the five documented target paths plus `ingest/`, `memcore/`, and `.gitignore`. No unexpected top-level path appears. The feature-to-HEAD diff contains only the expected target documentation changes.

4. **No merge-specific conflict resolution error or dropped imported file was found.** Exact subtree equality preserves the feature’s files and modes. The tree listing showed source, documentation, fixtures, and recorded results; no cache files, database files, large binaries, or committed symlinks were apparent. This was not a comprehensive secret-content audit.

5. **The merged `.gitignore` is appropriate.** It contains only `__pycache__/` and `*.pyc`; neither pattern hides required source or fixtures.

6. **No static import or fixture break was identified in the inspected flows.** Ingestion’s referenced `memcore.guard`, `memcore.memtools`, and `memcore.ports` modules are present, as is the Kibana fixture referenced by the reported memcore subset. Both package trees are unchanged from the feature commit.

I did not execute tests. The supplied coordinator log reports 53 ingestion tests passing at HEAD; the 106-test memcore subset is an owner report without a detailed execution log supplied here. The committed failing-first record documents relevant alignment, payload-oracle, and scanner failures. Live stores, providers, deployment, migration, and rollback remain unverified, consistent with lab-only scope.

An additional automated static inspection was rejected by the command guard; it supplied no verification evidence. No files were modified. Session metadata also reports 59 existing operational alerts, including a critical stale cron-watch alert; these were not investigated within this snapshot-only review.

Overall result: BLOCK pending F1’s documentation correction; no merge-specific code regression was identified in the inspected evidence.
