# Proof rules

Standing rules for every experiment in this programme. A PASS that does not meet them is CLAIMED.

1. Every PASS is reported next to a broken-variant control from the same harness that FAILS. The control must meet a sensitivity floor stated beforehand, such as violations in at least 10% of runs. A control at 0 or 2 of 200 is not a control.
2. Every check emits witness counts: items examined, stores enumerated from the system inventory (not a hand list), and canaries seen before the action. A zero or short witness count makes the result vacuous.
3. A concurrency or exactly-once claim needs an overlap witness at the contended step plus a stale-token or zombie actor. N live writers alone prove nothing about fencing.
4. Thresholds, sample size, strata (from input-side properties only), denominator and arm items are committed before the run. Timeouts, guard blocks and budget exhaustion score as failures.
5. Every figure resolves to a content-addressed per-item artifact with command and commit and is recomputed by an independent script. The register row moves to done with that evidence id the same day. Anything else is CLAIMED.
6. Every result records its environment fingerprint: ES version, licence tier and expiry, node count, data size, adapter, model id, batch size, cache state, host load. A claim holds only inside that fingerprint and shows UNTESTED_THERE elsewhere.
7. Replications are independent: per-run cache salt and at least 5 order and seed permutations, reported as the worst case or a Wilson lower bound, never one lucky order.
8. Model-stage claims are measured at the deployed operating point and at or above the target pool size. They pass a headroom gate, and a label-blind metadata probe must score at chance.
9. Safety attack sets are frozen before the defence is tuned and judged against an absolute zero bar with n stated. Model-judged gold is labelled as such and audited on a human or other-family sample with its agreement rate reported.
10. Fault-injection steps are written in a form that runs inside the harness guards (named in-process stall or exit hooks, processes in the spawn ledger). Boundaries without instrumentation are listed as untested, never implied as covered.
11. A found fault counts as fixed only when an independent delta review reproduces the original failure on the old commit and its absence on the new commit. Timing claims also need an isolated or quiesced run.
