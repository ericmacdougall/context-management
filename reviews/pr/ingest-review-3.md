Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: pr
Reviewed HEAD: 321ce74045871f2b2497f49c8b0379b59a6bbdc6
Reviewed repository: ericmacdougall/context-management
Reviewed PR: 3
Reviewed packet SHA-256: 492dde64cf8d93f1a4ad6e2c78de019178b04f64b214f7a4c547d575569970c9
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 2b23546f0c46352bcddf271b43b27b432d30bc13)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

VERDICT: BLOCK

**F1 from the earlier review — CLOSED.** The specific overstatements were corrected:

- `LEDGER.md:164–165` explicitly narrows the crash finding and marks the old exactly-once finding withdrawn.
- `DESIGN.md:160` says: **“‘Exactly once’ is WITHDRAWN”** and acknowledges four unrecoverable completed responses.
- `ingest/ingester.py:1` now describes duplicate suppression and explicit completeness limits.

I found no remaining universal ingestion exactly-once or complete-crash-recovery claim presented as a current finding in those locations. References to exactly-once results for separate durable-session experiments remain; they do not establish ingestion guarantees.

**F2 — MATERIAL, high severity: undeclared requests with no history overlap can silently lose user provenance while reporting complete.**  
Location: `ingest/ingester.py:354–379`, particularly line 357; conflicting contract at `ingest/CLAIMS.md:14–17`.

After history exists, a request containing two entirely new user messages has no matching history run. The ingester assumes only the last message is new and classifies the earlier message as `host_authored_user`. `_clean()` accepts this case when none of the keys were seen, so no ambiguity gap opens.

I reproduced this through `deliver()` and `drain()` using SQLite `:memory:`, in-memory blobs, and correctly chained records:

1. Call 1: user “old question”, assistant “old answer”.
2. Call 2, without `new_count`: users “first new question” and “second new question”, assistant “new answer”.

Observed results:

```text
first new question  -> prov=host, kind=host_authored_user
second new question -> prov=user, kind=text
state=complete, watermark=4, blocking_gaps=[]
claim_complete(seq=4)=True
claim_exactly_once=True
```

The first message remains stored, but loses its user attribution and disappears from the user-event sequence without a warning. This contradicts the current statement at `ingest/CLAIMS.md:17`:

> A stateless or repeating host that cannot declare its boundary therefore gets a superset of events plus a blocking gap, never silent loss.

**Correction:** treat an undeclared no-overlap request as ambiguous when the boundary cannot be established. Preserve every potentially new message with its original provenance, flag possible repeats, and block completeness claims. Add a regression covering two or more entirely unseen user messages after existing history, for both providers. The current multiple-new-message test at `ingest/tests/test_delta2.py:86` covers repeating patterns, which misses this case.

The remaining gate results are:

1. **Invariant 1 verified directly.** HEAD is `321ce74045871f2b2497f49c8b0379b59a6bbdc6`. The requested base-to-HEAD diff is empty for `ingest/`, `memcore/`, and `.gitignore`, including modes.

2. **Invariant 2 is supported by the supplied object-ID evidence.** HEAD’s four top-level document blobs and the `gotchas/` and `reviews/` trees match the packet’s IDs. The target commit is unavailable in this snapshot, so I could not independently verify its side of the equalities.

3. **Invariant 3 is supported with a verification limit.** HEAD contains exactly the target’s listed top-level paths plus the three stream paths. The feature-to-HEAD diff contains only target-side documents, `eval/`, `gotchas/`, and `reviews/`. HEAD’s `eval/` tree is `702f307bc32f386d7a1420707baf10c9c74fd127`, matching the committed earlier eval review. The packet omits a target-side eval tree ID, so complete equality against the stated target remains unverified.

4. **No merge-specific conflict-resolution error or dropped imported file was identified.** No committed symlinks, submodules, cache files, database files, or conflict markers were found by the inspections performed. The approximately 2.8 MB compressed eval result file is consistent with the earlier eval review. This was not an exhaustive secret or privacy audit of every artifact.

5. **No static integration break was identified, but the ingestion contract fails as described in F2.** All 175 Python files parsed successfully. The source contains 53 ingestion test methods, consistent with the supplied coordinator summary reporting 53 passing tests. That summary does not cover the reproduced failure.

**Packet discrepancy:** `reviews/pr/ingest-review-2.md` is present, despite the packet saying it is absent. Correct that statement in the next packet.

I did not rerun the filesystem-writing suites. The dynamic probes used only in-memory SQLite and blobs; no files were modified. Live stores, providers, deployment, production concurrency, migrations, rollback, target ancestry, and full public-branch content hygiene remain unverified. Session metadata reports 57 existing operational alerts, including a critical disk alert; these were outside the snapshot review and were not investigated.

Overall result: BLOCK pending F2’s implementation correction and regression evidence; the earlier documentation finding F1 is closed.
