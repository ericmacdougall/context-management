# policy/results/red: failing-first logs

Each log is the unittest output of a red run: the new tests against the code they were written to catch (or against a deliberately
broken variant), on local, never-pushed `red/*` branches. Home paths are redacted to `~`. Before commit the logs were scanned for
credential shapes (private-key blocks, AWS key ids, `sk-` keys, user:password URLs); none occur. The 64-hex strings are sha256 digests.

## Final review (reviews/policy-final/review-1.md) at a2d65fd

| Finding | Log | Code under test | Result |
|---|---|---|---|
| P2 S2 trial independence | `final_p2_verifier_a2d65fd.out` | a2d65fd verify/s2.py + the new tests/test_verify_s2.py (good rows carry trial and nonce identities, which a2d65fd ignores) | FAILED (failures=16): a2d65fd returns PASS on each of the 11 duplication manipulations in `P2_MANIPULATIONS` (12 copies of one observation per class, the reviewer's probe; copies renumbered; one trial twice; a trial gap; a seed relabelled from another; replay_first unlinked or on another factor; revoked_principal copies; concurrent rounds copied; rows with no identity; one nonce in two classes) and on 5 non-integer trial indices. The pre-existing S2 verifier tests pass on the same run. |

## Delta review 1 (reviews/policy-delta/review-1.md) at d05d731

| Finding | Log | Code under test | Result |
|---|---|---|---|
| M1 wall clock, every entry point | `delta1_m1_m3_n4_pg_d05d731.out` | d05d731 + red harness 7e8df79 | 14 FAIL, all behavioural assertions: a proof expiring inside a transaction opened a session; a token expiring inside a transaction, or ended after a REPEATABLE READ snapshot, was accepted by submit, explain, read_table, erase_candidates and erase_values; a factor revoked after the snapshot opened a session; submit and erase_values did not judge the session again after a lock wait. The M3 and N4 tests in the same run pass. |
| M2 S2 coverage pinned | `delta1_m2_verifier_d05d731.out` | d05d731 verifier + red harness 8e58460 | FAILED (failures=7, errors=1). The d05d731 verifier returns PASS on the reviewer's three manipulations (`_drop_classes`, `_one_mint_probe`, `_no_agent_origin`), on mine (`_small_replay`, `_fewer_trials`, `_one_seed`) and on duplicated or unexpected coverage; `_token_class_dropped` crashes it with KeyError instead of a verdict. |
| M3 in-memory overlap | `delta1_m3_memory_d05d731.out` | d05d731 | 1 FAIL: the winner's retry got `idem_conflict` instead of `duplicate` of its original ref, because the Vault published the idempotency mapping before the engine decided (delta review 1 M3). |
| M3 Postgres (challenge) | `delta1_m3_pg_challenge_d05d731_6b67f47.out` | unmodified d05d731 code, test as committed in 1ce569c | Ran 4, OK: the per-principal advisory lock already serializes allocation, acceptance and cleanup per (tenant, principal, key). |
| M3 Postgres, broken | `delta1_m3_pg_noadvisory_e9dd0af.out` | d05d731 without the advisory lock | FAIL: with B on another requirement, B gets `error:UniqueViolation` instead of `idem_conflict`. |
| M3 Postgres, first attempt | `delta1_m3_pg_noadvisory_r1only_02eec0e.out` | d05d731 without the advisory lock, r1-only test | OK: on one requirement the head row FOR UPDATE also serializes the pair, so this test did not isolate the advisory lock; the different-requirement cases were added for that. |
| M3 Postgres, no locks | `delta1_m3_pg_nolocks_9e99651.out` | d05d731 without advisory and head locks | ERROR: UniqueViolation on `events_idem` reaches the caller; the unique index is the last line, no value is stored twice. |
| N4 mid-rollback failure | `delta1_n4_split_variant_e494207.out` | rollback with the drops committed before the rename | 1 FAIL, behavioural ("dc_crypto is gone"); the other 14 rollback tests pass. |
| N4, first attempt | `delta1_n4_notxn_variant_573ddc2.out` | rollback with no transaction | 5 ERROR at LOCK TABLE outside a transaction, before the injection point: NOT meaningful red evidence for the injection test, kept for the record; superseded by the split variant above. |

## Review 3 at 0a20b64, step 2 at e15241c

- `n2_sync_guard_0a20b64.out`, `n5_value_refs_0a20b64.out`, `n6_provenance_0a20b64.out`, `n7_surface_ast_0a20b64.out`,
  `n8_n4_verifiers_0a20b64.out`, `n8_mig_probe_reproducer.out`, `review3_pg_0a20b64.out`: review-3 findings against 0a20b64.
- `step2_memory_e15241c.out`, `step2_pg_e15241c.out`: step-2 tests against e15241c.

**Delta review 1 M4: some step-2 red failures show only that an API was missing, not the refused behaviour.**
- `test_derivation_table` (tests/test_step2.py:44-49) failed with AttributeError: `_core.origin` did not exist at e15241c. This holds in both logs.
- The PG tests that go through the `_factor`/`_open`/`_token` helpers (tests/test_step2_pg.py:64-83) failed at the helper's positive control (`'refused' != 'ok'`): e15241c had no proof path, so no valid proof could open a session. Their own refusal assertions were never reached. This covers:
  - bad tokens on every call;
  - a body naming identity or origin;
  - enrolment refusals;
  - events carrying actor and origin;
  - expired and far-future proofs;
  - forged proofs;
  - the other-tenant token;
  - concurrent replay.
- The three bridge tests failed with AttributeError (no `PgIdentity`).

The failures that are behavioural at e15241c:
- memory:
  - agent and human events not stamped with actor and origin;
  - a body naming actor or origin refused as `bad_event`, not `identity_in_body`;
  - look-alike principals accepted;
- PG:
  - dc_idp minted a session from a bare principal id;
  - install accepted look-alike principals.
