Reviewer: Codex gpt-6.1-sol xhigh (rung R3)
Stage: code-inspection
Reviewed HEAD: 3c2864f8806fa9e808bf65c33f5df6031f8faa31
Reviewed packet SHA-256: 5a084aa85bc0110c771052a2320fb1aa94f5f6f3ae12bd93d9f569d2f6469358
Author-Rung: R2
Reviewer-Rung: R3
Reviewer-Selection: openai gpt-6.1-sol xhigh; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 1fcf3f75c581ae286e21c75c4efb1a1ca72eaa4f)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: 775bf90572a60e293f2896e6913efa36e75968537dc9c28d3b6ec71f0558fd78

CODE_INSPECTION_RESULT: BLOCK

Inspection only, not PR signoff. HEAD matches `3c2864f8806fa9e808bf65c33f5df6031f8faa31`. Two material validity-gate issues remain. Neither finding establishes that the supplied runs encountered the failure.

**P1 — MATERIAL, medium: successful log retrieval does not establish complete log coverage.**  
Source: [writepath/exp/disk_gate.py](/var/folders/g7/bszfxt551211997q9yzkvkrw0000gn/T/peer-review-q89_z7z5/repo/writepath/exp/disk_gate.py:108).

`end` accepts `docker logs` exit 0 and checks only the container’s current `StartedAt`, compared at whole-second precision. It records no starting container ID or log-retention state. Rotated or truncated logs can therefore omit an earlier watermark and still pass. A replacement within the starting second also passes the timestamp comparison; replacement after `inspect` is not checked again. A backward clock adjustment can exclude subsequent records from `--since`.

**Correction:** pin the container ID and full-precision start metadata at admission, establish retained coverage for the complete window, and recheck identity at completion. Detect invalid clock windows and reject unknown coverage. Add focused cases for successful-but-incomplete log retrieval, same-second replacement, and replacement between inspection and retrieval. The later window reads provide supplementary evidence but have the same coverage limitation.

**P2 — MATERIAL, medium: STOP can be ignored during finalization or deleted during admission.**  
Sources: [disk_gate.py](/var/folders/g7/bszfxt551211997q9yzkvkrw0000gn/T/peer-review-q89_z7z5/repo/writepath/exp/disk_gate.py:102), [chain_singles.sh](/var/folders/g7/bszfxt551211997q9yzkvkrw0000gn/T/peer-review-q89_z7z5/repo/writepath/runkit/chain_singles.sh:21).

`end` checks STOP once, before container inspection, log retrieval and usage measurement. A STOP written during those operations is ignored before success returns. Separately, a STOP arriving after the chain’s check but before `start` reaches its unconditional `STOP.unlink()` is deleted.

**Correction:** preserve STOP until an explicit acknowledgement or run-scoped reset, coordinate admission with that reset, and recheck STOP before reporting successful completion. Add cases that inject STOP during log retrieval, usage measurement and admission.

**P3 — MINOR: the results summary omits the F1 performance residuals.**  
Source: [results/README.md](/var/folders/g7/bszfxt551211997q9yzkvkrw0000gn/T/peer-review-q89_z7z5/repo/writepath/results/README.md:264).

The common erasure limits cover starvation, purge deadlines and the KMS stand-in, but omit the documented outer-delete `O(tenant)` work per batch and GIN pending-list cost. Add these qualifications or link directly to the corresponding design-note paragraph beside the F1 and plan-stability claims.

**P4 — MINOR: run-kit instructions describe superseded admission rules.**  
Source: [runkit/README.md](/var/folders/g7/bszfxt551211997q9yzkvkrw0000gn/T/peer-review-q89_z7z5/repo/writepath/runkit/README.md:10).

The README still says 17 GB and lists batches only through d. Update it to the current 20 GB admission rule and include batches e/f.

1. **Earlier findings**

   - **N1: CLOSED for the reported defect.** Infrastructure failures propagate; confirmed stale cases return `-1`; cleanup matches epoch and holder. CAS retries are bounded at three requests. The saved failing-first rows show CAS/ITEM returning apparent success, while the fixed rows show failures propagating and recovery succeeding, 5/5 per case. I found no new stale-write path or unbounded retry loop in the changed implementation. Lease release remains best-effort when the database connection fails.
   - **N2: PARTLY CLOSED.** Nonzero or timed-out log retrieval, ordinary later restarts, measured end-cap breaches, failed usage commands and an already-present STOP reject the run. P1/P2 prevent calling the complete gate fail-closed. Usage measurements also establish sampled growth, not a transient peak bound.
   - **N3: CLOSED for the populated fixture.** At M=1,500/3,000, the saved rows contain 750/1,500 X facts before erasure and zero afterward. Facts work is positive in every recorded batch, with no missing delete statements. I recomputed all 24 fixed-arm PS-C gates without disagreement. `CTL_FACTS` changes the facts fragment and header, and counts only facts-gate failures: its recorded sensitivity is 3/3 in each state. Raw control plans are not retained, so that portion cannot be independently recomputed from plans.
   - **N4: CLOSED for the original wording corrections.** The 300-second deadline is distinguished from completion, amendment 18b’s relaxed control requirement is disclosed, and starvation is qualified. P3/P4 are remaining documentation issues.

2. **Amendments and reruns**

   - **23a:** technically justified as an environmental validity rule, but post hoc. Keeping d3’s FAIL beside its later PASS is essential; the latter is not a clean success under the original protocol. The implementation applies the watermark predicate independently of the experiment verdict. The retained d2 PASS subsequently marked VOID supports the symmetry claim.
   - **24:** a legitimate prospective tightening of admission. It provides margin, not protection against arbitrary growth by other tenants.
   - **24a:** a legitimate continuation after the documented d2 VOID, with the prior record retained.
   - **24b:** a post hoc relaxation of the once-only retry policy, although the numeric overlap threshold stays unchanged. It is acceptable as a disclosed lab extension, not as evidence that the original protocol succeeded unchanged. Recomputed R1 rows support INCONCLUSIVE first—25/19 overlaps—and PASS on the single rerun—23/21 overlaps, zero fenced violations/errors and 21 overlapping control violations.

   No excess experiment execution is visible in the retained records. The 17:27 attempt stopped before the module ran. This snapshot cannot exclude omitted host executions.

3. **Stamp table and scope**

   The table retains d3 FAIL/PASS, d2 VOID/PASS and R1 INCONCLUSIVE/PASS separately. Old-gate runs are labelled observations; held-out scanner failure is carried explicitly; E1’s fixed arm reached `in_flight` zero times; and the 1M run remains BLOCKED.

   The size probe itself ran and produced an exploratory measurement; it is the projected 1M run that remains blocked. Calling the current gate fully “fails closed” overstates its implementation because of P1/P2.

   I verified the final product/host tree IDs and the final scanner blob. Intermediate-commit identity and chronology remain runner-supplied evidence because those commits are absent from this snapshot.

4. **Results hygiene**

   All **198 manifest entries** match their committed SHA-256 values. JSON filename suffixes agree with original-hash prefixes, and JSON commit fields agree with the manifest. I checked **89 RESULT-to-document links** without hash, commit or recorded-window disagreement.

   Five supplementary `review-2` outputs are outside the manifest. The raw host-copy hashes cannot be recomputed from the path-normalized copies alone.

   A limited credential-shape scan found no private-key blocks, recognized token prefixes, credential-bearing URLs or authorization values in manifest-listed results. This does not verify the claimed comparison against live secret values.

5. **Erasure qualifications**

   Starvation and deadline semantics are stated in the design notes, erasure module and common results limits. F1 residuals appear in the design notes but are not consistently attached to the results summary—P3. The evidence establishes neither bounded-time erasure under sustained writes nor general deadlock freedom.

6. **Use verdicts and verification limits**

   - **LAB: BLOCK for issuing validity stamps with the current gate**, pending P1/P2. The retained measurements remain useful for qualified lab analysis.
   - **CLIENT: BLOCK.** Beyond the gate findings, the evidence explicitly leaves the KMS stand-in, service-identity isolation, E1’s unobserved window, scanner generalization failure and 1M scale validation unresolved.

No repository files were modified. I inspected source and supplied test evidence without rerunning tests or services. No schema migration is introduced; reverting the projector changes would reintroduce N1. Full validation, deployment testing and PR signoff remain pending.
