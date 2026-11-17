Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: d05d7315d0784b0d2dc54dba9752f2f23d7ea0a1
Reviewed packet SHA-256: 2a7ae600a2bbaf5b3c8f29010fab0a41e61bb731bab4764a0ad27eaa3ab0f915
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 0a20b64d25059e873ef03883477f0d5ff2ac13a6)
Worktree status: clean

CODE_INSPECTION_RESULT: BLOCK

Inspected HEAD `d05d7315d0784b0d2dc54dba9752f2f23d7ea0a1` against base `0a20b64d25059e873ef03883477f0d5ff2ac13a6`. The block concerns implementation and verification defects found during inspection. Pending full validation is not a reason for this result.

**M1 — MATERIAL, High: transaction-start time lets raw SQL callers defeat proof and session expiry.** `policy/pol/_pgcore.sql:608,614,620,635–638`.

Both proof validation and session validation use `now()`, which stays fixed at the start of the PostgreSQL transaction. A `dc_idp` caller can start a transaction while a proof is valid, wait until it expires, then submit the previously unused proof: the expiry comparison still uses the earlier time. A `dc_app` caller can similarly start a transaction before a token expires and continue calling the API after expiry. All five application entry points use `_session()`, so this affects writes, reads and erasure.

Trusting the database clock does not disclose or justify a clock frozen by the caller’s transaction. This also undermines the stated token-TTL backstop for policy-only revocation.

**Correction:** use current wall-clock time for authentication expiry, including session creation and replay-ledger cleanup; adjust function volatility appropriately. Add explicit-transaction negative cases that cross proof/token expiry and assert refusal on every application call. If expiry must hold after lock waits, recheck at the protected operation. I established this from source; I did not execute database probes.

**M2 — MATERIAL, Medium: S2 verification accepts materially incomplete coverage.** `policy/verify/s2.py:38–60,87–93`; `policy/verify/provenance.py`.

The verifier takes required proof classes and trial counts from the result document, counts mint rows without checking their distinct `(role, what)` identities, and requires only a nonempty origin set. The provenance gate checks seeds but does not close these coverage gaps.

Using the committed synthetic fixture, read-only in-memory probes returned **PASS** independently for:

- Removing the declared proof classes and all corresponding real-arm attack rows, retaining only `replay_first` and `revoked_principal`.
- Replacing the 17 distinct mint probes with 17 copies of one refused probe.
- Removing the agent-origin row.

These are verifier acceptance defects; they do not establish that the pending run omitted coverage.

**Correction:** independently pin the required proof classes, stamped trial count, distinct mint probes, origin kind/channel matrix and concurrent replay size. Reject missing, duplicate and unexpected coverage. Add regression cases for the three demonstrated false passes.

**M3 — MATERIAL, Medium: random-reference allocation breaks successful-request idempotency under an overlapping submission.** `policy/pol/decisions.py:65–82,207–221`.

`Vault._mint()` publishes the retry mapping before the engine decides which submission wins. Consider two different payloads using the same principal and idempotency key:

1. A mints its reference and pauses before engine submission.
2. B mints another reference and is accepted, while `_idem.setdefault()` still points to A.
3. A reaches the engine, receives `idem_conflict`, and deletes its reference and retry mapping.
4. Retrying the accepted B mints another reference and receives `idem_conflict` instead of `duplicate`.

A deterministic in-memory interleaving through the committed service reproduced:

```text
B: proposed
A: idem_conflict
retry B: idem_conflict
stored values: 1
```

**Correction:** serialize allocation, engine acceptance and cleanup per tenant/principal/idempotency key, or publish the retry mapping atomically for the accepted submission. Add an overlapping conflicting-payload regression asserting that the winner remains retryable with its original reference.

**M4 — MINOR, Medium: the supplied step-2 tests do not uniformly establish behavioral red failures.** `policy/tests/test_step2_pg.py:64–83`; `policy/tests/test_step2.py:44–49`.

The PostgreSQL helper explicitly substitutes a placeholder when enrollment is missing, then calls the absent two-argument `open_session` overload. On the pre-step-2 implementation, its positive assertion therefore fails because the API is absent. `test_derivation_table` likewise calls the newly introduced `_core.origin` function. Other tests do inspect behavior, but the blanket claim that the tests fail for behavioral reasons rather than missing APIs is unsupported.

**Correction:** distinguish API-introduction failures from behavioral regressions in the write-up. Preserve compatibility probes that demonstrate the old bare-principal mint succeeding and the fixed implementation refusing it. Include the historical red logs in reviewable evidence.

The earlier findings trace as follows. “Closed” here means the specific defect is corrected by source inspection, not that database execution was verified.

| Finding | Status at HEAD | Concrete trace and limits |
|---|---|---|
| N1 | CLOSED | `PgSessionBridge.issue()` binds the database token to the exact issued session. `_verified()` checks that binding before database access in submit, explain, table and erasure. Mixed principal, tenant and channel tests assert zero calls and no mutation. |
| N2 | CLOSED | `sync.sh` accepts a bounded ASCII component beginning with a letter or digit; shell syntax, newlines, slashes, leading dashes, traversal and Unicode are rejected before SSH. It checks each fixed ancestor for symlinks and uses plain `mkdir` for the new run. This assumes ancestors cannot be concurrently replaced by another filesystem writer. |
| N3 | PARTLY CLOSED | Service submission now asks the shared revocation source. The raw-SQL policy-only revocation limit is disclosed in both `pgcore.py` and Amendment 6. It is acceptable for a constrained lab boundary; client use needs an explicit synchronization/logout strategy. M1 invalidates the claimed TTL backstop as implemented. |
| N4 | CLOSED | Rollback takes exclusive table locks, compares the data mark and refuses changed decision/audit data before dropping anything. The unchanged branch restores the prior schema. Destructive operations remain in one transaction, so an exception should roll them back. No injected mid-rollback failure evidence was supplied. Authentication-state exclusions are disclosed. |
| N5 | CLOSED for the guessing oracle | Memory and SQL references are random; memory deletes the associated retry mapping, and SQL removes the retained HMAC-key table. I found no replacement deterministic subject-name oracle in those references. M3 is a separate reliability regression in the memory fix. |
| N6 | CLOSED for consistency checking | Normal stamped verification invokes the committed provenance gate and separates row acceptance from provenance. It explicitly does not authenticate the host or chronology. M2 concerns additional S2 coverage requirements. |
| N7 | CLOSED | The AST fallback synthesizes dataclass constructors, with agreement tests. Its inheritance limitation is documented. |
| N8 | CLOSED for the reported mechanisms | PG-DIFF counts semantic differences instead of error outcomes; MIG requires the named probes to yield `InsufficientPrivilege`. |
| R3-1 | PARTLY CLOSED | Bare-principal database minting is removed; proofs bind identity to enrolled factors, and composed sessions are checked before use. M1 leaves authentication lifetime enforcement incomplete. |
| R3-3 | CLOSED for the reported retained-reference attack | Random subject tokens and value references remove the identified derivations. This does not prove deletion from backups or previously captured data. |
| R3-8 | PARTLY CLOSED | Zero anchors now fails and coverage is reported. Protection still depends on independently protected anchor storage; a rewritable file and the unanchored suffix retain the disclosed limits. |

The six S2 controls are implementation mutations or historical implementations targeting the stated defects: bare minting, missing replay enforcement, missing MAC verification, identity from the proof body, and origin from the request body. Their source mechanisms are appropriate. No new run-level evidence was available to confirm their catches.

The symmetric-key owner-forgery limit, database-clock dependency and out-of-scope enrollment are stated in Amendment 6 and the experiment’s limitations. They are acceptable constraints for synthetic lab experiments. Client use would require accepting the database owner as an authentication authority and specifying enrollment, key protection and revocation behavior.

The threshold diff adds 99 lines without changing prior text. Amendment 6 nevertheless changes the effective MIG rules and adds S2 requirements, as disclosed. The recorded DEV rehearsal preceded the amendment according to the packet and document; the two-commit snapshot cannot independently establish intermediate commit contents, seed freshness, or whether thresholds were selected after observing DEV results.

**LAB use: PASS WITH LIMITS** for isolated synthetic experiments with disposable databases and operator-controlled destinations. Expiry and idempotency claims must remain qualified until M1 and M3 are corrected; S2 PASS cannot establish complete coverage until M2 is corrected.

**CLIENT use: BLOCK** on M1–M3, alongside the disclosed deployment trust boundaries and unresolved product assumptions.

I inspected the supplied offline test summaries but did not rerun write-producing tests. My executable checks were limited to in-memory verifier and service probes with bytecode disabled. I did not verify live privileges, database concurrency, authentication expiry, deployed behavior, historical red logs or the pending stamped run. No files were modified or secrets read. The supplied critical cron/deadman alerts remain unverified and outside this snapshot review.

Stage: code-inspection — not PR signoff.
