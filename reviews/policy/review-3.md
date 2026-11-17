Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: 0a20b64d25059e873ef03883477f0d5ff2ac13a6
Reviewed packet SHA-256: aa922195d47571315da66be1f463cb3c65ba09e5478223c74c95e6c2b49256bc
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 80bb6a7b4bafd4f2b390a63121a1f3ef9603335d)
Worktree status: clean

CODE_INSPECTION_RESULT: BLOCK

Inspected HEAD `0a20b64d25059e873ef03883477f0d5ff2ac13a6` against base `80bb6a7b4bafd4f2b390a63121a1f3ef9603335d`. The recorded experiment verdicts reproduce, but material implementation issues remain. This is an inspection result, not PR signoff.

- **N1 — MATERIAL, High: database and in-process sessions are not bound before use.** `policy/pol/pgcore.py:244–314`. Constructing `PgSession(alice.session, bob.db_token)` passes `_principal()` whenever Alice’s session is valid. `submit()` executes as Bob, then checks the returned ID. With the supplied autocommit connection, Bob’s mutation has already committed before the exception. Same-principal sessions from different channels evade even that comparison. `erase_subject()` authorizes candidates using one session’s database identity and evaluates policy using the other session’s identity, without a binding check. This requires possession of valid session components; it does not manufacture Bob’s token. **Correction:** authenticate the pairing before any operation, binding tenant, principal, channel and lifecycle. Prefer an authority-issued opaque pairing and enforce expected identity inside the same database operation. Add mixed-principal, mixed-tenant and mixed-channel negative cases that assert no mutation.

- **N2 — MATERIAL, High: the sync path guard permits remote command injection.** `policy/sync.sh:10–18`. The shell pattern accepts metacharacters and embedded slashes. For example, this destination passes the guard:
  
  ```text
  emlab2-16096/policy/runs/x;touch /tmp/policy-sync-escape;#/src
  ```
  
  Interpolation into the remote command makes the first SSH call execute `touch` outside the allowed tree. I did not execute this payload. **Correction:** validate a single run-name component against a strict character allow-list, construct the destination from that component, quote remote arguments safely, and reject symlinked destination ancestors.

- **N3 — MATERIAL, High: Postgres submissions bypass policy revocation.** `policy/pol/pgcore.py:244–259`, compared with `policy/pol/decisions.py`’s submission check. `principal_of()` checks session identity and directory membership, not policy revocation. Unlike the memory service, `PgDecisionService.submit()` never calls `_revocations._is_revoked()`. A principal listed in the current policy’s revoked set can therefore submit successfully while its database principal remains unrevoked. **Correction:** apply the shared revocation check before submission and define how revocation reaches the database boundary. Add a Postgres-service regression matching `test_policy_revoke_reaches_the_core`.

- **N4 — MATERIAL, High: rollback silently discards post-migration writes.** `policy/ops/migrate_r3_7.py:136–143`. After forward migration, new requests write to the new `dc` schema. Rollback unconditionally drops that schema and restores the frozen pre-migration copy. Any acknowledged changes since migration disappear. The rehearsal checks restoration of the old fingerprint, which does not prove preservation of those changes. **Correction:** either enforce a write freeze for the rollback window and refuse rollback after any mutation, or implement a validated reverse transfer. Document and test the chosen boundary explicitly.

- **N5 — MATERIAL, Medium: memory-vault erasure retains a name-guessing oracle in value references.** `policy/pol/decisions.py:46–50,68–69`. Subject tokens are now random, but `_value_ref()` still HMACs the original label, including subject names, under the retained vault key. A key holder with the logged principal/idempotency metadata and known or guessable text can test candidate subject names against the surviving value reference after erasure. **Correction:** remove raw subject names from the retained-key reference derivation, using random references or an appropriate tokenized derivation, and test guessing through `value_ref` as well as through subject tokens.

- **N6 — MINOR, Medium: provenance validation is outside the inspectable verifier.** `policy/verify/__main__.py:60–75`. It verifies item hashes and recomputes verdicts, but does not enforce clean stamps, a common source/threshold stamp, the thresholds digest, or stamped parameters. The external postflight was unavailable. **Correction:** commit a provenance gate and invoke it from the normal verification command. A row-level PASS should remain distinguishable from provenance acceptance.

- **N7 — MINOR, Low: the manifest scanner has an environment-dependent dataclass omission.** `policy/tests/surface.py:153–175`. Its AST fallback enumerates explicitly defined methods but does not synthesize the dataclass-generated `PgSession.__init__`. **Correction:** normalize dataclass enumeration between runtime and AST paths and check both driver-present and driver-absent environments.

- **N8 — MINOR, Medium: control verification can accept unrelated failures.** `policy/verify/pgdiff.py:47–50` counts `error:*` outcomes as control differences; `policy/verify/mig.py:21–22` accepts any nonempty exception-name string as a denied privilege probe. **Correction:** require the intended semantic mismatch or expected permission exception, and reject infrastructure errors. The current inspected observations support the intended failures, so this does not invalidate their present verdicts.

Responses to the packet’s questions:

1. **Earlier findings:**
   - **R3-1: PARTLY CLOSED.** `SessionAuthority` now requires an adapter assertion and verifies issued objects by identity. The PostgreSQL API derives identity from its session row. N1 leaves the composed session boundary incomplete, and N3 leaves policy revocation incomplete.
   - **R3-3: PARTLY CLOSED.** The direct subject-token HMAC issue is fixed in memory and PostgreSQL: tokens are random and mappings are deleted. N5 leaves an alternative guessing path in the memory implementation.
   - **R3-7: CLOSED for the stated direct-table/apply-authority attack, by source inspection.** The application grants expose the session API, raw reducer functions are not granted, and authority changes occur inside the reducer transaction. This depends on the actual installed role configuration; I did not inspect a running database. Read-label and erasure-hold enforcement remain explicitly trusted Python behavior.
   - **R3-8: PARTLY CLOSED.** An independently protected external anchor detects rewrites through an anchored event. `FileAnchor` itself supplies ordinary append operations, not enforced immutable storage. A database owner who can also rewrite the anchor defeats it; unanchored suffixes remain unprotected. Also, `anchor_check()` can return `ok=True` with zero anchors, so callers must require meaningful anchor coverage.

2. **Thresholds and chronology:** the snapshot diff adds 136 lines to `THRESHOLDS.md`; the prior text is unchanged. Amendment 5 nevertheless changes the effective specification: non-human decision rules, cap semantics, controls, B2 floors, fresh seeds and additional experiments. These changes are disclosed, including the DEV-informed removal of an unreachable B2 control. The supplied chronology is consistent with the documents and result stamps, but the snapshot cannot establish that `edb693f` was a thresholds-only commit preceding the results commit. That remains unverified chronology.

3. **Reproduction: yes.** I performed a read-only equivalent of the verifier loop, loading verifier source without writing bytecode or the output file. All item hashes checked, all **11 verdicts recomputed PASS**, and the reconstructed VERIFY bytes exactly matched the committed file, with SHA-256:
   
   ```text
   ba442234cd8d818359b3ef5eb10d36360f20ed1a2971c878c67e6897a45d09b5
   ```
   
   `pgdiff` recomputes comparisons and coverage from rows. `mig` recomputes arm acceptance from recorded observations rather than trusting each arm’s `ok` flag. Both still rely on experiment-produced observations; neither independently re-executes database behavior.

4. **Controls:** the inspected variants mutate implementation code and generally target the stated defect. In committed PG-DIFF rows, the control arms have **zero error outcomes**, with 373 and 5,586 code differences respectively. The migration clean-arm privilege probes record `InsufficientPrivilege`. B3 judges concrete outcomes such as two accepted conflicting decisions, backstop retries or duplicate applied events. Thus the inspected observations support the intended mechanisms. N8 describes how future unrelated failures could nevertheless satisfy some verifier checks.

5. **External stamp check:** yes, this is an inspectability and reproducibility gap, N6. Reproducing VERIFY establishes the row-verdict calculation, not authentication of the claimed execution environment or source chronology.

6. **PgSession mismatch:** confirmed, **High**, N1. The existing post-submit ID-prefix check is too late for writes and does not bind tenant or channel. Erasure has no corresponding pairing check.

7. **Superseded results:** `THRESHOLDS.md:726–728` states the situation honestly and specifically: old B1/B2/B3 recompute FAIL under round-3 verifiers, the two new experiments are absent, and E1–E6 remain PASS. `DESIGN.md` and the new stamped log do not repeat that explanation. DESIGN also contains stale text at lines 79–80 describing memory subject tokens as HMAC-based, whereas the current implementation uses random tokens. Update that description and link the supersession notice from the results narrative.

8. **Sync guard:** yes. The concrete input in N2 causes an out-of-tree write through remote shell interpretation. The claimed path restriction is therefore ineffective even without `..`.

9. **Credential inspection:** I found no actual password content, private key, SCRAM verifier or live bearer credential in the inspected committed material. `policy/hostrun.sh` and `policy/pol/pgcore.py` contain passfile/password-file references; test fixtures contain synthetic identity material; results contain subject/value identifiers. No values are reproduced here. This inspection is not a guarantee that every possible secret format is absent.

10. **Manifest failure:** the committed manifest includes **both** `PgSession.__init__(session, db_token)` and `PgSessionBridge.__init__(sessions, idp_conn, ttl_s=)`, matching the source. The reported failure is consistent with the missing-driver AST fallback omitting the generated dataclass constructor. It is an environment-sensitive scanner defect, N7, rather than evidence that the pinned bridge signature is stale.

**LAB use: PASS WITH LIMITS** for isolated experiments with synthetic data, fixed operator-controlled destinations and disposable databases. The results support the recorded experimental comparisons. They do not establish secure deployment readiness, erasure guarantees against key holders, or lossless rollback.

**CLIENT use: BLOCK** on N1–N5. The disclosed product-rule assumptions also remain assumptions pending the user’s decision.

I did not run database, network or write-producing tests; verify live privileges, authentication, protected anchor storage or deployment behavior; or access the external postflight and historical review. Full validation remains pending and is not itself the reason for this inspection block. The supplied critical cron/deadman alerts remain unverified and outside this snapshot review.

Stage: code-inspection — not PR signoff.
