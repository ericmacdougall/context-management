Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: f80e34dbe8cf4dc5b1fb19c51c138501ca16492f
Reviewed packet SHA-256: 05e983368e54303af31dbeb67560bafaddb16fd7fd4ee887a205ac996a7550b1
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 207136e97a9a8b9f6cfa4df88829ebf4c4f9c1be)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

CODE_INSPECTION_RESULT: BLOCK

Reviewed HEAD `f80e34dbe8cf4dc5b1fb19c51c138501ca16492f` against base `207136e97a9a8b9f6cfa4df88829ebf4c4f9c1be`. This is code inspection only, not PR signoff. The block is based on implementation defects; pending full validation is not itself a blocker.

- **F1 — MATERIAL / High: sustained probe failures eventually kill the worker.** `memcore/reprobe.py:55` evaluates `backoff_s * (2 ** (failures - 1))` before applying the interval cap. With the default float backoff, sufficiently many failures cause `OverflowError`. The exception handler increments the counter and repeats the same overflowing calculation at line 86, so the exception escapes and terminates the thread. This occurs around the 1,025th consecutive failure, roughly 17 hours with immediate errors and the default interval. **Correction:** cap the exponent or saturate the delay before exponentiation; make exception recovery safe independently. Add a focused test spanning more than 1,025 failures and subsequent recovery.

- **F2 — MATERIAL / Medium: invalid configuration silently disables tracking.** `memcore/hosts/basic_service.py:59` silently treats every negative interval as “off,” although the help documents only zero. The float parser at line 92 accepts `nan` and `inf`, and `ReprobingTier`’s positivity check at `memcore/reprobe.py:34` accepts both. A NaN deadline never becomes due; an infinite interval prevents subsequent scheduled refreshes. **Correction:** require finite, positive values in the tracker; allow exactly zero as an explicit service-level opt-out, reject negatives and non-finite values before starting the gateway, and log intentional disabling. Cover these cases in tests.

- **F3 — MATERIAL / Medium: a failed capability-file update is never retried for the same tier.** `memcore/reprobe.py:73` updates `self.report` before invoking `on_change` at lines 78–79. If the callback’s log or atomic file replacement fails (`memcore/hosts/basic_service.py:64–69`), the retry sees an unchanged tier and skips the callback permanently. After a lapse, `capabilities.json` can therefore continue advertising enterprise features while searches use Basic. **Correction:** track pending publication separately from the applied tier and retry it until successful. Add a test where publication fails once and then succeeds without another tier transition.

The requested questions, numbered:

1. **Both directions and startup:** The normal sequential paths support Basic → licensed and licensed → Basic. `build()` probes and applies the startup result; an initial probe error schedules an early retry. The scripted tests inspect actual request shapes and fusion results, which is useful evidence.

2. **Errors, timeouts, malformed tiers and flapping:** `capabilities.probe()` converts transport and parsing exceptions into a Basic report. Unknown types and non-active statuses also select Basic. This fails toward client fusion once the refresh finishes; the prior strategy remains active while the request is pending. Flapping follows each observed result without hysteresis. F1 prevents reliable recovery during prolonged failures.

3. **Refresh/search races:** There is no shared synchronization: the tracker lock does not protect `ElasticsearchSearch.fused()` or its latch. A query can pass the strategy check before a Basic refresh, then send a retriever request after that refresh completes. A qualifying licence 403 falls back within the call, so the server remains the enforcement boundary; this does **not** guarantee that no licensed request is attempted after a downgrade. An in-flight adapter probe can also overwrite a concurrent latch reset. Add deterministic barrier-based concurrency tests and, if strict downgrade ordering is required, synchronize request admission with tier changes or use a generation check.

4. **Latch scope:** `reset_probe()` assigns instance attributes only. No new shared or class-level latch is introduced. It leaves `probe_error` and `_probe_at` unchanged, so diagnostic fields can remain stale until a later adapter probe.

5. **Lifecycle:** The thread is a daemon and sequential double-start is ignored. Stop joins for only five seconds, while the transport timeout defaults to 30 seconds; `stop()` can return before the worker exits, and a completed request can still apply state or invoke the callback afterward. Start after stop does not restart it. Concurrent start and post-fork recovery are unsupported. Ordinary callback exceptions are caught, but F1 disproves the unconditional loop-survival claim.

6. **Interval:** Sixty seconds is a plausible polling default, with a detection delay that also includes request duration and scheduling. Zero is documented as disabling. Negative and non-finite values have the defects in F2. Scheduling uses wall time, so a backward clock adjustment can defer refreshes; elapsed-time scheduling should use a monotonic clock.

7. **Three mutations:** **Not verified.** The supplied log shows only 11 passing tests. The source includes two passing controls demonstrating broken/disabled behavior, but no evidence of three mutation runs failing by assertion. Provide each mutation, targeted test, and failure output distinguishing assertion failure from exception, hang or import failure.

8. **Evidence limits and claims:** `memcore/tests/test_reprobe.py:1–6` explicitly states that tests use a scripted node and fake clock and marks live testing pending. That is appropriate. The statement “The loop never dies on one bad refresh” in `memcore/reprobe.py:15` overclaims because of F1. The Basic-node permission statement at line 16 should also distinguish licence entitlement from authorization failures.

9. **Other defects:** F3 is an additional publication-recovery defect. Every successful enterprise refresh also clears the adapter latch, even when the tier is unchanged, causing a fresh retriever capability query after each interval. That adds periodic work beyond the single licence GET described in the module; either preserve a valid latch on unchanged tiers or document and test the intended behavior.

**LAB use: PASS WITH LIMITS** for isolated investigation using finite positive intervals, scripted nodes, and direct observation of worker health and capability output. This does not accept the implementation for delivery.

**CLIENT use: BLOCK** on F1–F3. No migration is introduced. Rollback to the base restores startup-only tier selection and loses recovery from a Basic/unreachable startup; that limitation should be documented.

I inspected committed source and the supplied test log only. I ran no tests and verified no live Elasticsearch, Postgres, Temporal, deployment, mutation result, or concurrency behavior. Full validation remains pending.

CODE_INSPECTION_RESULT: BLOCK
