Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: 801a3d283cb290412ae98f3ecae1c889ee388ddf
Reviewed packet SHA-256: 5d96a7883909279d3c92606fb134f76134bebb2a3b210f35e9434232de424991
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 3423f4eb0a858cbb10a35cd1da6630eeaa44e06d)
Worktree status: clean

CODE_INSPECTION_RESULT: BLOCK

Inspected HEAD `801a3d283cb290412ae98f3ecae1c889ee388ddf` against base `3423f4eb0a858cbb10a35cd1da6630eeaa44e06d`. This verdict covers code inspection only; pending full validation is not itself a blocker.

F1 — MATERIAL, high: `release` reports success without making normally poisoned inputs retryable.  
Source: `engine/pgstore.py:209–217`, `engine/pgstore.py:288–294`, `engine/keys.py:75–76`, `engine/wf/v1.py:150`, `engine/wf/v1.py:163`, `engine/wf/v1.py:240–244`.

The new command changes only the alert status. After successful dead-lettering, the input already has an effects entry under its result key, so `unprocessed_inputs` still excludes it after release. After failed dead-lettering, the workflow retains the input in `done` and `poisoned`, so reconciliation and processing still reject it. The carried poisoned set can preserve this exclusion across continue-as-new. Thus the documented “release retries it” behavior fails on both ordinary poison outcomes.

The release test at `engine/tests/test_alerts.py:199–209` creates an alert directly without exercising either outcome, so it misses the defect.

Correction: implement an explicit recovery protocol that reconciles durable completion markers and workflow exclusion state, preserves external-effect idempotency, and defines recovery for finished sessions. Alternatively, reject unsupported releases and narrow the command’s documented contract. Test both successful and failed dead-letter paths through actual workflow processing.

F2 — MATERIAL, high: a persistent alert-write failure can prevent the dead-letter failure handler from ever completing.  
Source: `engine/activities.py:75–77`, `engine/activities.py:96`, `engine/wf/v1.py:20–37`, `engine/wf/v1.py:145–153`.

All alert-write errors escape the wrapper as ordinary activity failures. Turn activities eventually reach their ten-minute schedule-to-close timeout and are requeued, but `dead_letter` uses `act`, which has no schedule-to-close bound. If dead-lettering repeatedly fails and its alert upsert also repeatedly fails—for example, because alert-table writes are denied while other database operations work—the activity retries indefinitely. The workflow never reaches the handler that adds the input to `poisoned`, and newer inputs cannot progress.

Correction: define a bounded escalation path for failure to persist an alert, including the dead-letter activity. Preserve the requirement against silently dropping an input, while making the blocked state externally observable and recoverable. Add focused coverage where alert writes fail persistently after an original poison alert was successfully recorded. Document the remaining availability tradeoff explicitly.

Answers to the packet’s questions:

1. **Durability before Poison:** For positional session activity calls, the upsert’s connection context exits before `_alert_poison` returns and the wrapper raises Poison. This gives the intended commit-before-failure ordering on inspection. It is not universal: `get_config` deliberately raises Poison without an alert, and `_alert_poison` silently returns when it cannot extract session arguments. Repeated write failures do not intentionally drop the input; they cause retries and can block progress as described in F2.

2. **Retry and crash idempotency:** The composite primary key and upsert prevent duplicate rows. A crash after commit but before delivery of Poison can increment `count` again on retry; this is row deduplication, not an exactly-once occurrence count. If the retried activity succeeds, the existing alert can remain open. Crash behavior has not been demonstrated.

3. **More than twenty alerts and ordering:** Both SQL queries exclude open and acked alerts without a twenty-item alert cap. They retain numeric `seq` comparisons and ordering for remaining inputs. This supports progress past more than twenty alerted inputs on inspection. The corresponding database and Temporal behavior remains unverified. The design interpretation remains an explicitly documented assumption, not a confirmed user decision.

4. **Tenant actions and release loops:** Status updates match tenant, session, event and kind, preventing an update for tenant A from accidentally matching tenant B. However, these are privileged operator functions: the CLI accepts any supplied tenant and operator name, with no authenticated tenant binding. It must remain restricted to trusted administrators. A released alert reopens on another poison, so release does not itself introduce automatic continuous release/retry. F1 prevents the promised recovery in normal workflow states.

5. **Payload and exception text:** The new wrapper sends `type(exc).__name__`, not `str(exc)`, to the alert row and JSON log. No original exception payload is added by this path. The table also stores operator metadata (`updated_by`), so “only ids and exception class” is not literally its entire schema. IDs and operator names are not proven free of personal data. Earlier retry failures still propagate the original exception through the existing path; the new tests do not establish exception-text confidentiality for the complete Temporal failure history.

6. **Query scoping:** The upsert conflict key, status update and SQL skip correlation include tenant and session. `list_alerts` intentionally supports a global operator view when no tenant is supplied. DDL is global. No accidental cross-tenant join was found; tenant authorization is outside these helpers.

7. **Migration and rollback:** The DDL is additive and repeatable against an already compatible table. Worker startup invokes it before polling. An older image can ignore the extra table structurally, but will also ignore its durable skip semantics and restore the prior cap behavior. There is no demonstrated migration/rollback rehearsal or documented operational rollback procedure. Concurrent first-start DDL and runtime-role DDL privileges also remain unverified.

8. **Workflow changes:** Confirmed: the diff changes neither `engine/wf` nor `engine/gen_variants.py`. There is no workflow-source replay change in this commit. That does not establish full replay or deployment safety; activity and database behavior changed.

9. **Tests against prior code:** Source inspection supports seven assertion failures among the ten offline tests: five wrapper tests plus the SQL and schema tests. Three existing-behavior checks should continue passing. This was not executed against the base, so assertion-based regression evidence remains inferred. The supplied log reports fourteen tests, four skipped, hence ten executed. The skipped tests leave real SQL execution, persistence across pools, status transitions, tenant isolation and the twenty-five-input database case unproven. They also do not test Temporal recovery, and the release test omits F1’s real states.

10. **Overclaims:** `engine/ALERTS.md:21–23` overstates release behavior and calls the carried poisoned list a “harmless optimisation”; F1 shows it remains authoritative for recovery. The statement at line 11 that nothing previously survived a worker restart is also too broad: workflow state is restored from Temporal history, and successful dead-letter records already persisted in Postgres.

LAB use: **BLOCK** for the advertised acceptance flows until F1 and F2 are resolved; controlled experiments can continue as validation work.  
CLIENT use: **BLOCK** for the same implementation defects. Pending database, Temporal and deployment validation is an additional delivery limitation, not the basis for this inspection verdict.

No files were modified, secrets read, or tests run. Review used only the supplied snapshot and test log. Live deployment, real database behavior, Temporal execution and rollback were not verified.

Stage: code-inspection. This is not PR signoff.
