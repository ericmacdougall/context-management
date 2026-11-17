Reviewer: Codex gpt-6.1-sol xhigh (rung R3)
Stage: code-inspection
Reviewed HEAD: a2d65fdc3ed997e3d65fa91194aa7c37274611bc
Reviewed packet SHA-256: b09706e661eb0bceb22a9fa990fde0ae70ed2f18e8255ff85b4ab58433497b14
Author-Rung: R2
Reviewer-Rung: R3
Reviewer-Selection: openai gpt-6.1-sol xhigh; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base d05d7315d0784b0d2dc54dba9752f2f23d7ea0a1)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

CODE_INSPECTION_RESULT: BLOCK

Inspected HEAD `a2d65fdc3ed997e3d65fa91194aa7c37274611bc` against base `d05d7315d0784b0d2dc54dba9752f2f23d7ea0a1` in the supplied snapshot. Material implementation and verifier issues remain. Pending full validation is **not** a reason for this inspection result.

**P1 — MATERIAL, Medium: `submit` can wait past session expiry after its final authentication check.** `policy/pol/_pgcore.sql:692,712,731`.

The new recheck follows `_lock_event`, but subsequent operations can also block. A concrete source-derived interleaving is:

- Alice submits on `r1` with a previously unseen subject and leaves her transaction open, holding the new `subject_tokens` entry.
- Bob submits on `r2` with the same subject and a short-lived token. His tenant, principal and head locks succeed, and the new session recheck succeeds.
- Bob blocks at `insert into dc.subject_tokens ... on conflict do nothing`.
- Bob’s token expires—or `end_session` commits—before Alice commits.
- Bob resumes, allocates the value and reaches `_reduce_apply` without another session check.

Different principals and requirements avoid the locks covered by the existing regression. This leaves the advertised post-wait authentication guarantee incomplete.

**Correction:** place the final authentication check after all relevant blocking acquisition, before applying the request, with transactional cleanup of any staging writes on refusal. Add this two-principal, different-requirement, shared-new-subject regression for both expiry and logout. I traced this from source; I did not execute it against PostgreSQL.

**P2 — MATERIAL, Medium: S2 still accepts duplicated proof observations as independent trials.** `policy/verify/s2.py:87–102`; `policy/exp/s2_sessions.py:255–277`.

Coverage categories are now independently pinned, but proof coverage is checked only by class counts. The experiment does not emit its loop index or another identity that distinguishes individual trials.

A read-only, in-memory probe using the committed fixture returned:

```text
S2 baseline: PASS
S2 one proof observation copied 12 times per class: PASS
```

For every seed and proof class, the probe replaced all twelve observations with copies of that class’s first observation. Thus incomplete trial evidence can still produce PASS. The provenance gate checks stamps and seeds; it does not detect this duplication.

**Correction:** emit non-secret trial identities and sufficient case descriptors, independently require the exact expected trial set, reject repeated identities, and link each `replay_first` observation to its corresponding replay trial. Add a regression replacing twelve trials with twelve copies of one observation. This concerns detectable omissions and duplication; it does not require authenticating the host or defending against arbitrary fabrication of evidence.

**P3 — MATERIAL, Medium: exceptions leave a stale vault mapping that recreates the idempotency failure.** `policy/pol/decisions.py:224–232`; `policy/pol/_core.py:65–67`.

The striped lock releases correctly on exceptions, but vault allocation has no exception cleanup. This is reachable through ordinary malformed JSON input: a submission with `"type": []` passes the service’s preliminary checks, mints a value, then raises `TypeError` when `_shape` tests the list against the `REQUIRED` dictionary.

A read-only, in-memory probe through the committed service produced:

```text
Memory malformed type: TypeError
Stored values after exception: 1
Memory next valid request: proposed
Memory its exact retry: idem_conflict
Stored values after retry: 2
```

The valid request reused the malformed request’s principal and idempotency key with different text. `_idem.setdefault()` retained the orphan’s mapping, so the accepted request was no longer retryable.

**Correction:** validate malformed event types before allocating values and make staging, acceptance and cleanup exception-safe under the key lock. Ensure pre-acceptance failures remove their value and mapping, while failures after acceptance preserve the accepted reference. Add this malformed-request → successful-request → exact-retry regression.

**P4 — MATERIAL, Medium: a proof blocked on the replay ledger can open a session after its factor is revoked.** `policy/pol/_pgcore.sql:601–607,622–629`.

`open_session` reads factor revocation before its potentially blocking ledger operations. After those operations, it rechecks expiry only.

A source-derived schedule is: transaction A opens a session from proof P without committing; B presents P, reads the still-live factor and blocks on the nonce insertion; the administrator revokes the factor and commits; A rolls back; B’s insertion succeeds and B opens a session using its cached, unrevoked factor row. `_session` subsequently checks session state, without consulting factor revocation.

**Correction:** coordinate session opening with factor/principal revocation and revalidate the relevant authentication state after ledger waits. Add a concurrent regression using first-presenter rollback and committed factor revocation. This database schedule was not executed in this review.

The requested questions resolve as follows.

1. **M1–M4 status at HEAD:**

   | Finding | Status | Inspection conclusion |
   |---|---|---|
   | M1 | **PARTLY CLOSED** | The transaction-start clock defect is corrected. Proof checks, replay cleanup and session creation use `clock_timestamp()`; `_session` is explicitly VOLATILE; all five application entry points reach it. Non-READ-COMMITTED authentication is refused. The initial submit and erasure lock rechecks are present, but P1 leaves later submit waits uncovered. P4 concerns authentication state retained across a proof-ledger wait. |
   | M2 | **PARTLY CLOSED** | Required classes, trial count, mint identities, token/call matrix, origins, replay rounds/width and controls are pinned independently of result parameters. The earlier three false-pass mechanisms are addressed. P2 leaves repeated proof trials undetected. |
   | M3 | **PARTLY CLOSED** | The original successful overlapping-submission defect is corrected in memory. All normal submissions take a vault-owned stripe, allocation through refusal cleanup remains inside it, and I found no inverted acquisition in the supplied engine/vault paths. Exception cleanup remains defective under P3. |
   | M4 | **CLOSED** | The committed red-log README explicitly separates missing-API failures from behavioral failures, and the historical logs support that distinction. Bare-principal mint and look-alike compatibility probes remain. |

   I found no source-level isolation-guard bypass through changing `default_transaction_isolation`, savepoints, prepared transactions or invocation from a trigger. The checks inspect the current transaction’s isolation level. Preparing an already executed transaction does not create a fresh authenticated API execution. These conclusions were not database-tested; SERIALIZABLE and the listed invocation variants lack execution evidence in this review.

   **The PostgreSQL M3 challenge is sound.** `_lock_event` takes an advisory transaction lock derived from tenant and principal before reference lookup/allocation, engine acceptance and cleanup. Requirement and session token are absent from the lock key, so different requirements and different sessions of the same principal still serialize. The committed logs show four passing challenge cases and a `UniqueViolation` in the different-requirement case when that lock is removed. The same-requirement-only mutant passing is correctly explained by the head lock masking the missing advisory lock. Distinct session tokens are covered by source reasoning, rather than by that test’s execution.

2. **N4 mid-rollback injection is meaningfully addressed.** `policy/tests/test_review3_pg.py:333` verifies that the migrated schema has been dropped before injecting at the rename, then checks restoration of the migrated schema, retained old schema, crypto schema and stored value, followed by a successful ordinary rollback. Production rollback keeps the destructive steps inside one transaction.

   The first no-transaction variant failed at `LOCK TABLE`, before injection, and supplies no meaningful evidence for this test. The later split-transaction log does: it reaches the post-injection assertion and fails because `dc_crypto` is gone. That is a behavioral counterexample. I inspected the committed test and logs, not the historical mutant source or a PostgreSQL rerun.

3. **Step 2’s ordinary proof and binding paths look sound within the disclosed boundary, subject to P4.** The MAC covers the body; exact tenant, principal and channel binding is checked against the enrolled factor; the unique `(factor, nonce)` ledger arbitrates concurrent committed consumption; and session tokens are random and stored hashed. The bridge checks exact issued-session/token binding before database access. Look-alike configuration refusal and exact proof binding remain in place.

   Enrollment, actual requesting-device possession and a production identity provider remain outside this implementation’s demonstrated scope. The symmetric-key database-owner authority and policy-only revocation window remain deployment constraints.

4. **The READ COMMITTED contract is mostly accurate, but its lock-wait statement overstates the implementation.** The documented responses for `open_session` and the five session-keyed calls match the source. P1 contradicts the blanket statement that submit judges the session again after its lock waits. For completeness, document that READ UNCOMMITTED is also rejected by the exact comparison, and distinguish `end_session`, which has no isolation guard. The wall-clock statement itself is accurate.

5. **New unresolved findings are P1–P4 above, all MATERIAL.** A minor documentation correction also remains: `policy/DESIGN.md:79–84` still describes HMAC-derived memory subject tokens and value references, whereas the current implementations use random references/tokens. **P5 — MINOR, Low:** update those descriptions to match the code. References to Amendment 7 also need reconciliation when that amendment is committed; I have not treated its pending state as a validation failure.

**LAB use: PASS WITH LIMITS** for isolated synthetic development with disposable databases. Current expiry-after-wait, complete S2-trial coverage and exception-safe idempotency claims must remain qualified.

**CLIENT use: BLOCK** on P1–P4, alongside the disclosed enrollment, owner-key, revocation and trusted in-process policy boundaries.

Verification was limited to committed source, supplied logs and two in-memory probes run with bytecode generation disabled. No files were modified or secrets read. I did not verify PostgreSQL execution at HEAD, live privileges, deployed behavior, historical mutant contents or the pending stamped run. The committed stamped results belong to `d05d731` and establish no run-level result for this HEAD. The supplied critical cron/deadman alerts remain unverified and outside this snapshot review.

Stage: code-inspection — not PR signoff.
