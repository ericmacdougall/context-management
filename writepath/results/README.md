# Write-path lab results (GoodQA #16096)

Raw result documents copied from the lab host. The only change from the host copies is that the host home directory
prefix in paths is shortened to `~/`. `MANIFEST.tsv` lists, for every file, the sha256 of the host copy (the hash the
`RESULT` lines of `run_all.*.out` print, and the 16-hex suffix of the file name) and the sha256 of the committed copy.
Result documents carry ids and hashes only; a leak scan of every file against the lab's live secret values found none.

| Folder | What it is | Status |
|---|---|---|
| `run_all-fd4ed53-VOID/` | The run_all at fd4ed53, started 2026-11-17T07:56:40Z. | **VOID** (amendment 17c): Elasticsearch reached its flood-stage watermark at 10:23:42Z because another tenant of the host filled the disk (this stream's own usage stayed at +0.17 GiB). The run stopped before `cleanup exp.e2e_restored`. races, plan_stability and the final guard_check were not run. Everything here is an observation, not a stamped result. |
| `failing-first/` | Results against the code before a fix: lock_order at eaed478 (amendment 17), plan_stability at fdbecb4 (amendment 18b), and projector_start_race at 3c229fc, which still had the fd4ed53 step() (amendment 19). | Failing-first evidence. |
| `unstamped/` | plan_stability at ba71567 (its PASS is VOIDED by amendment 18c: the work metric did not multiply rows removed by loops) and at 83f7000 (the fix; PASS, a pre-run, not the stamped run). | Not stamped. |
| `single-runs/` | One stamped run per experiment, each from a pinned tree with the same validity gates as run_all (17 GB start line, disk_gate start/check/end, flood watcher). Each has its `one.<head>.<label>.out` log. | Stamped. projector_start_race at 5d26746 is FAIL from a harness defect (amendment 19a) and stands for that run; it is superseded by the 62c6be5 run, not deleted. |
| `void-earlier/` | The run_all logs of the two earlier VOID runs, 1f059f4 and 83f7000. | VOID, logs only. |

Verdicts observed in the fd4ed53 run before the void (from `run_all.fd4ed53.out`):

- **Expected violations:** old_repro N1 to N7 reproduce the original defects on the old code, with property_holds false as intended.
- **PASS:** guard_check start, static_check (no violations), es_semantics, F3, F4, F5, F7, F1S, F6, F1 (cascade 16.3 s over 27 batches), M, H, N1, N2, N5, N8, row1_zombie, row23_latecommit, e_verify, registry_marker, d1, d4 (rescan), row19, row6, d4 (store mark), d3, mutual_mentions, lock_order, d2, phys_purge, E2, E3.
- **heldout_secrets: FAIL**, as pre-registered in amendment 15. The scanner does not generalise.
  - H1 in-scope: 483 of 1,169 caught.
  - H2 in-scope: 150 of 750 caught.
  - False blocks: 37 of 1,200 negatives.
  - The harness control is valid (a gap of 0.32).
- **e2e E1: FAIL.** Two causes are recorded in amendment 14a:
  - an oracle defect, where P1's writes made before the takeover were counted as stale;
  - a projector start-race crash, where `step()` did not catch a stale error raised in `start()`.
  - The fenced arm held in 28 of 28 overlapping runs, and the control violated in 23 of 23.

Stamped single runs after the void (`single-runs/`, code at 62c6be5 unless noted):

- **projector_start_race: PASS** (amendments 19 and 19a). In the fixed arm, cases A, B and C each held 5/5 with 0 errors; in case C, P1 returned False after 5 starts. The control (the fd4ed53 step()) was violated 5/5 in every case.
- **e2e E1: PASS** (amendments 14 and 14a), with 0 errors.
  - Fenced arm: 40/40 runs held, including 28/28 overlapping runs. Windows: 34 after_takeover, 6 before_takeover, 0 in_flight.
  - Control arm: 24/24 overlapping runs violated.
  - The in_flight window (a P1 bulk in flight while P2's alias CAS applies) was never observed, so that window is
    **unverified** by this run.
- **races R1, R2 and R3: PASS.** Fenced: 25/25, 39/39 and 40/40 overlapping runs held. Control: 22/22, 40/40 and 39/39 violated.
- **plan_stability: PASS** in S1 and S2, 3/3 runs each.
  - Apply: 6 rows of work per event. Residue: about 14 rows per event, ratio 1.99 when M doubles.
  - Both controls were violated 3/3 per state.
  - Scope (review-2 N3): the fixture had no facts. The facts delete did zero work while its gate held, and CTL_APPLY's
    sensitivity can come from analyst jobs alone. This PASS covers the analyst-job delete and the residue only.
    Amendment 22 adds facts and a facts-only control; plan_stability runs again under it.
- **size_probe at N=50,000** (exploratory, no PASS):
  - Measured: 2,071 bytes of store growth per document.
  - Linear projection: 2.28 GB for the 1.1M-document scale_1m run. That is over this stream's 2 GB own-usage cap before any transient peak (ES segment merges, WAL growth, the key-table VACUUM FULL).
  - Result: the 1M rerun stays BLOCKED on this host.

- **heldout_secrets: FAIL**, as pre-registered (amendment 15b), with the same figures as the VOID-run observation.
  - H1 in-scope: 483/1,169 caught.
  - H2 in-scope: 150/750 caught.
  - False blocks: 37/1,200.
  - Control gap: 0.3247.
- **review_fix F1: PASS.** 100,000 events, zero residue.
  - Cascade: 12.5 s over 24 batches; the first five batches took 2.48 s and the last five 2.69 s.
  - verify: 12.0 s. Postgres completion: 3.2 s.
- **d4_rescan_es: PASS.** Fenced 5/5, control violated 5/5.
- **d4_store_mark: PASS.**
- **d3_identifier: PASS.**
- **lock_order: PASS**, with no deadlocks or errors.
  - MM held 30/30, DA 30/30, MK 5/5, HP 5/5, HR 5/5 and M2 10/10.
  - The pre-fix FOR UPDATE controls deadlocked 10/10 each.
  - MS (measured, not thresholded): in 3/3 runs, 59 FOR SHARE locks were granted while the erasure mark waited, and the mark was cancelled by statement_timeout at 30 s. The job stayed requested until the sweep's retry moved it to logically_erased. There is no worst-case bound under sustained concurrent appends.
  - Scope: the controls restore only the pre-fix lock statements, not the whole earlier system. Overlapping cascades
    that share event, tombstone or fact rows are outside the MM fixture. This is not a proof of deadlock freedom.

### Validity gate of these runs (review-2 N2)

Every single run above ran under the disk gate as it was before review-2 N2 (fixed in 72c0708). That gate's `end`
ignored the return code of `docker logs`, so a failed log read passed as "no flood stage". It also printed, without
rejecting, an own-usage growth over the cap. None of these runs is re-judged by the fixed gate. Every one that uses the
stores is rerun under it at the post-fix commit (amendment 23). heldout_secrets uses no store, so the flood-stage
gate has nothing to judge in it, and its FAIL is not rerun (amendment 20). ASSUMPTION, open to main's ruling: it stays
as recorded at 62c6be5. What the saved outputs show, per run (`disk_gate end` line of each `one.<head>.<label>.out`):

| Run | Own usage at end | Flood lines printed | Log read now, same window (exit, lines, flood) |
|---|---|---|---|
| 5d26746 projector_start_race (FAIL) | +0.00 GiB | 0 | 0, 117, 0 |
| 62c6be5 projector_start_race | +0.00 GiB | 0 | 0, 117, 0 |
| 62c6be5 e1 | +0.01 GiB | 0 | 0, 360, 0 |
| 62c6be5 races | -0.00 GiB | 0 | 0, 1140, 0 |
| 62c6be5 plan_stability | -0.08 GiB | 0 | 0, 320, 0 |
| 62c6be5 size_probe | -0.02 GiB | 0 | 0, 4, 0 |
| 62c6be5 heldout | +0.00 GiB | 0 | 0, 0, 0 |
| 62c6be5 f1 | +0.18 GiB | 0 | 0, 4, 0 |
| 62c6be5 d4_rescan | +0.00 GiB | 0 | 0, 44, 0 |
| 62c6be5 d4_store_mark | +0.00 GiB | 0 | 0, 20, 0 |
| 62c6be5 d3 | +0.00 GiB | 0 | 0, 40, 0 |
| 62c6be5 lock_order | +0.01 GiB | 0 | 0, 292, 0 |

- **Cap:** held in every run (largest growth +0.18 GiB against +2 GiB), from the saved end lines.
- **Log retrieval at the time:** the saved outputs **cannot show** whether `docker logs` succeeded during these runs;
  the old gate did not record its return code.
- **Supplementary, read afterwards (2026-11-17 ~11:55Z):** the ES container has run since 05:07:12Z without a restart
  and logs with the json-file driver without rotation. `docker logs --since <start> --until <end>` for each window
  exited 0 and holds no flood-stage line. The container's only flood-stage lines after 10:30Z are five at 10:31:12 to
  10:33:12Z, the tail of the episode that voided fd4ed53, before the first run here (10:34:31Z). This is evidence
  about the windows, read later; it is not the gate's own observation. Raw output: `review-2/n2_retro_log_check.out`
  (a local read, not a host result document, so not in MANIFEST.tsv).
- **Batch step1-b at 9cbc798** (amendment 20) also ran under the old gate: guard_check start, static_check,
  es_semantics, n_old, review_fix and the experiments after it. An operator STOP written at 11:32:50Z did not stop it
  (the old `end` ignored STOP, and the next run's `start` deleted it; fixed in ba827fa). Its results are recorded as
  old-gate observations, not stamped results.

### Limits that apply to every erasure result here

- **Timing under concurrent writes:** an erasure mark can wait without bound while concurrent writes keep taking the
  subject row FOR SHARE (lock_order MS above, amendment 17c). Each attempt is cut at 30 s and the sweep retries. No
  result here shows erasure completing in bounded time under concurrent appends.
- **Physical purge:** the 300 s physical deadline is a deadline, not a completion bound. Past it the job is reported
  overdue (physical_purge_overdue) and stays open until the purge completes.
- **Key service:** a lab stand-in for a KMS; every erasure result is conditional on a KMS with real key deletion.
- **Postgres settings:** these results depend on wal_recycle=off, wal_keep_size=0, archive_mode=off and
  max_connections=200 (host/bootstrap.py, in the repo since 8c7d841). Their fingerprints record only the server
  version, so they cannot be reproduced from the fingerprint alone. Fingerprints from cf8f08e on record the effective
  settings (exp/common.PG_SETTINGS).

The remaining step-1 experiments were observed only in the VOID fd4ed53 run: F3, F4, F5, F7, F1S, F6, M, H, N1, N2, N5, N8, row1, row23, e_verify, registry_marker, d1, row19 (880/880 on its author's own planted corpus only; on held-out data the scanner caught 483/1,169 and 150/750), row6, mutual_mentions, d2, phys_purge, E2 and E3. Not run at 62c6be5: guard_check end, and a full run_all restamp.
