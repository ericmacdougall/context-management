# Write-path lab results (GoodQA #16096)

Raw result documents copied from the lab host. The only change from the host copies is that the host home directory
prefix in paths is shortened to `~/`. `MANIFEST.tsv` lists, for every file, the sha256 of the host copy (the hash the
`RESULT` lines of `run_all.*.out` print, and the 16-hex suffix of the file name) and the sha256 of the committed copy.
Result documents carry ids and hashes only; a leak scan of every file against the lab's live secret values found none.

| Folder | What it is | Status |
|---|---|---|
| `run_all-fd4ed53-VOID/` | The run_all at fd4ed53, started 2026-11-17T07:56:40Z. | **VOID** (amendment 17c): Elasticsearch reached its flood-stage watermark at 10:23:42Z because another tenant of the host filled the disk (this stream's own usage stayed at +0.17 GiB). The run stopped before `cleanup exp.e2e_restored`. races, plan_stability and the final guard_check were not run. Everything here is an observation, not a stamped result. |
| `failing-first/` | Results against the code before a fix: lock_order at eaed478 (amendment 17), plan_stability at fdbecb4 (amendment 18b), projector_start_race at 3c229fc, which still had the fd4ed53 step() (amendment 19), and projector_es_fault at 1539a75, before the review-2 N1 fix (amendment 21: the fixed arm held 0/5 in each of CAS, ITEM, REQ and CONN). | Failing-first evidence. |
| `old-gate-9cbc798/` | Batch step1-b at 9cbc798 (amendment 20): guard_check start to guard_check end, 19 runs. | Old-gate observations, all PASS (n_old violated as intended). Run under the disk gate before review-2 N2 and before the N1 fix; every one is rerun at the post-fix commit (batch c or d). Not stamped results. |
| `step1-31112b4/` | Batch step1-c at 31112b4 (amendment 23), under the fixed gate, from guard_check start to d3. | Stamped. 17 runs PASS (n_old violated as intended). **d3 is FAIL** (amendment 23a, see below). The batch stopped BLOCKED before mutual_mentions; the rest runs as batch d at 2e1948c. |
| `unstamped/` | plan_stability at ba71567 (its PASS is VOIDED by amendment 18c: the work metric did not multiply rows removed by loops) and at 83f7000 (the fix; PASS, a pre-run, not the stamped run). | Not stamped. |
| `single-runs/` | One stamped run per experiment, each from a pinned tree with the same validity gates as run_all (17 GB start line, disk_gate start/check/end, flood watcher). Each has its `one.<head>.<label>.out` log. | Stamped under the old gate (see "Validity gate of these runs"); each one that uses the stores is superseded by its rerun at the post-fix commit (batch c or d). projector_start_race at 5d26746 is FAIL from a harness defect (amendment 19a) and stands for that run; it is superseded by the 62c6be5 run, not deleted. |
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
gate has nothing to judge in it, and its FAIL is not rerun (amendment 20). Main ruled so: the FAIL at 62c6be5 is carried,
not re-observed (wp/secrets.py is the same blob, 58b025fc, at every later commit). What the saved outputs show, per run (`disk_gate end` line of each `one.<head>.<label>.out`):

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
- **Second read, with the high watermark (2026-11-17 ~16:10Z, amendment 23a):** `review-2/window_check_all.out`
  (raw; made by `runkit/window_check.sh`) reads each window again and also counts "high disk watermark ... exceeded"
  lines. Every window above, the 1539a75 failing-first run and every batch-b window below show exit 0, no
  high-watermark line and no flood-stage line. Windows shorter than about 30 s can hold no line at all (ES repeats
  these warnings about every 30 s), so for those the read cannot show absence.
- **Batch step1-b at 9cbc798** (amendment 20, `old-gate-9cbc798/`) also ran under the old gate, from 11:32:11Z to
  13:35:39Z: guard_check start, static_check, es_semantics, n_old, review_fix, scope_hold, n_new, row1, row23,
  e_verify, registry_marker, d1, row19, row6, mutual_mentions, d2, phys_purge, e2e E2 and E3, guard_check end. All
  PASS (n_old violated as intended). Cap held in every run (largest +0.02 GiB). Log retrieval at the time cannot be
  shown, as above; the later read shows exit 0 and no high or flood line in any window. An operator STOP written at
  11:32:50Z did not stop it (the old `end` ignored STOP, and the next run's `start` deleted it; fixed in ba827fa). Its
  code predates the N1 fix (8a03b80), so its results are old-gate observations, not stamped results; every one runs
  again at the post-fix commit.

### Step 1 at the post-fix commit (review-2 N1 to N3)

**Commits.** Both carry the N1 fix (8a03b80).
- **Product code (`writepath/wp`):**
  - Changed between 62c6be5 and 31112b4 in wp/families.py and wp/projector.py (N1), and in wp/erasure.py
    (docstring only, N4).
  - **No change between 31112b4 and 2e1948c**: `git diff 31112b4 2e1948c -- writepath/wp` is empty.
- **Harness changes between 31112b4 and 2e1948c:**
  - exp/disk_gate.py and runkit/launch_one.sh (the high-watermark VOID rule, 39dbad7);
  - tests/test_disk_gate.py;
  - runkit/specs/step1-d.txt;
  - exp/THRESHOLDS.md (amendment 23a).
  - No experiment module and no threshold changed.
- **Gates:** every run at 31112b4 used the fixed gate of 72c0708 (N2). Runs at 2e1948c also used the high-watermark
  rule.

**d3 timing.** Amendment 23a (the high-watermark VOID rule, symmetric) was written **after** d3's FAIL at 31112b4 was
observed. d3 was rerun at the later commit 2e1948c, where it passed. Both records stand. d3 is FAIL at 31112b4 and
PASS at 2e1948c, and is never reported as simply passed. Whether 23a is a legitimate validity rule or a moved
goalpost is put to the delta review. The rule voids a PASS in such a window as well as a failure, and 31112b4's 17
other runs were re-read under it and still stand.

- **Batch c at 31112b4** (`step1-c.txt`, amendment 23, `step1-31112b4/`), 14:05:07Z to 15:56:48Z:
  - **PASS:** guard_check start, static_check (no violations), es_semantics, F3, F4, F5, F7, F1S, F6, F1 (100,000
    events, zero residue, 60 cascade batches, max batch 1.15 s), M, H, N1, N2, N5, N8 (fenced 10/10, controls
    violated 10/10), row1, row23, e_verify, registry_marker, d1, d4 (rescan), row19 (880/880 on the author's own
    planted corpus only; heldout below), row6, d4 (store mark).
  - **Expected violations:** n_old N1 to N7 on the old code.
  - **d3: FAIL** (amendment 23a). No verdict, exit 1: the N1-fixed projector raised ProjectionError on 503
    unavailable_shards_exception. At 15:54:43Z the ES node logged "high disk watermark [17gb] exceeded" (free 15.7
    GiB; other tenants of the host; this stream's own usage +0.00 GiB). d3's index wp16096-d3db4f08-events-base,
    created at 15:54:47Z, never started its shard. The gate of 72c0708 judged only the flood stage; that harness
    defect is fixed in 39dbad7. The FAIL stands and is reported beside its rerun.
  - **mutual_mentions: BLOCKED** at its start (host free 16G, under the 17G start line). The chain stopped there.
- **Batch d at 2e1948c** (`step1-d.txt`, amendment 23a, `step1-2e1948c/`), from 16:04:44Z: d3 again, then
  mutual_mentions, lock_order, d2, phys_purge, E1, E2 and E3, projector_start_race, projector_es_fault, races,
  size_probe, plan_stability (amendment 22) and guard_check end. Started at 28G free, under the 17 GB start line of
  its pinned tree. Amendment 24 (20 GB, 1bad3bd and 2cf33f3) governs only later runs.
  - **d3: PASS** (fenced and control as pre-registered), 0 high-watermark and 0 flood lines. This is the rerun of
    the 31112b4 FAIL, which still stands.
  - **mutual_mentions: PASS.** seq 5/5, conc 10/10 (all overlapped), control violated 5/5.
  - **lock_order: PASS.** MM 30/30, MK 5/5, HP 5/5, HR held, no deadlocks or errors. Own usage +0.01 GiB.
  - **d2: VOID** (not evidence).
    - The experiment itself returned PASS, but other tenants of the host pushed the ES node past its high disk
      watermark at 16:34:45Z (free 15.7 GiB) and into flood stage at 16:35:15Z (free 12.6 GiB).
    - The watcher wrote STOP at 16:34:52Z, and `disk_gate end` voided the run. Its message says "operator stop",
      because it does not record who wrote STOP.
    - Own usage was +0.00 GiB at the run's check.
    - Window read (`review-2/window_check_2e1948c_batch_d.out`): exit 0, 1 high-watermark line, 2 flood lines.
  - **The chain stopped after d2**, as designed. The rest, including d2, runs again as batch e under amendment 24
    (20 GB start line).
- **Batch e at 96214c2** (`step1-e.txt`, amendment 24a, `step1-96214c2/`), 16:47:29Z to 17:19:41Z.
  - Conditions: the 20 GB start line, the lab run lock, and the same gate as batch d.
  - Code: `git diff 2e1948c 96214c2 -- writepath/wp` is empty.
  - Window read (`review-2/window_check_96214c2_batch_e.out`): every run exit 0, no high-watermark or flood line,
    own usage at most +0.08 GiB.
  - **d2: PASS.** The rerun of its 2e1948c VOID.
  - **phys_purge: PASS.**
  - **e2e E1: PASS.**
    - Fenced: 40/40 held, 29/29 overlapping, 0 errors. Control: violated 23/23 overlapping.
    - Fenced windows: 33 after_takeover, 7 before_takeover, **0 in_flight**. The in_flight window is still
      **unverified** for the fixed code (the control arm reached it 3 times).
  - **e2e E2: PASS** (fenced 40/40, all overlapping, 0 errors). **E3: PASS.**
  - **projector_start_race: PASS.** Fixed arm 5/5 in A, B and C; control violated 5/5 in each.
  - **projector_es_fault (N1, amendment 21): PASS.**
    - The fixed arm held 5/5 in CAS, ITEM, REQ, CONN and STALE.
    - The control (1fcf3f7's classification) was violated 5/5 in CAS, ITEM, REQ and CONN.
    - STALE, the true lost-CAS case, holds in both arms.
  - **races:**
    - **R1: INCONCLUSIVE** (amendment 3). The control arm had 19 overlapping runs; 20 per arm are needed. The fenced
      arm had 25 overlapping runs and 0 violations in all 40, with 0 errors. The control was violated 19/19
      overlapping. One rerun under amendment 24b (written after this result) follows, and the two are never
      combined.
    - **R2: PASS** (fenced 39/39 overlapping).
    - **R3: PASS** (fenced 40/40 overlapping).
  - **size_probe** (exploratory, no PASS):
    - Measured 4,478 bytes per document, against 2,071 at 62c6be5. The difference is mostly Postgres WAL refill
      (16 MB to 128 MB) after the setup reset; the data directory after the build is about the same in both runs.
    - Linear projection for 1.1M documents: 4.93 GB, over the 2 GB own-usage cap. **The 1M rerun stays BLOCKED.**
  - **plan_stability (N3, amendment 22): PASS.**
    - S1 and S2: 3/3 runs each held PS-C, PS-R and PS-F3. PS-F1 (both deletes logged in every batch) and PS-F2
      (facts work > 0) are judged within PS-C.
    - Controls failed 3/3 in each state: CTL_APPLY, CTL_RESIDUE, and the facts-only control CTL_FACTS.
  - **guard_check end: PASS.**
- **R1 rerun at 0a433bc** (`step1-f.txt`, amendment 24b, `step1-0a433bc/`).
  - **Attempt 1, 17:27:18Z: BLOCKED** at `disk_gate start` (host free 19.3 GiB < 20.0). My lock waiter read `df -BG`,
    which rounds up. No experiment ran and no result exists, so this is not the amendment-24b rerun. Its `.out` is
    kept as `one.0a433bc.races_R1.blocked-1727.out`.
  - **Attempt 2, 17:29:24Z to 17:32:40Z: PASS.** The waiter now compares exact KiB.
    - Fenced: 40/40 held, 23 overlapping, 0 violations, 0 errors.
    - Control: 21 overlapping, violated 21/21.
    - Window read (`review-2/window_check_0a433bc_r1.out`): exit 0, no high-watermark or flood line.
    - Recomputed by amendment 24b's rule, the result is the same: no fenced violation or error, both arms at least
      20, control sensitive.
  - R1 is reported as INCONCLUSIVE at 96214c2 and PASS at 0a433bc. The rerun rule was written after the first
    result was seen, and the two runs are never combined.
- **Code identity:** `git diff 96214c2 0a433bc -- writepath/wp writepath/exp/races.py` is empty.
- **heldout_secrets (main's ruling):** not rerun. Its FAIL at 62c6be5 is **carried, not re-observed**:
  - wp/secrets.py is the same blob (58b025fc) at 62c6be5, 31112b4, 2e1948c and the final commit;
  - the heldout experiment files are unchanged since 62c6be5;
  - the experiment uses no store.

### Step 1 stamp table

**Evidence** is one stamped run per experiment at a post-fix commit (31112b4, 2e1948c or 96214c2; writepath/wp is
identical at all three). The exceptions, each shown in both rows: d3 (FAIL, then PASS), d2 (VOID, then PASS) and R1
(INCONCLUSIVE, then one rerun). **Observation** is
everything else: the VOID run_all at fd4ed53, and the old-gate runs at 62c6be5, 5d26746 and 9cbc798. Old-gate runs
used the gate from before review-2 N2, which judged flood stage only and did not record whether `docker logs`
succeeded, so their log retrieval at the time cannot be shown; a later window read is in `review-2/`.

Gate versions:
- **old:** flood stage only, before 72c0708.
- **N2:** 72c0708: closed the fail-open cases of review-2 N2. The step-1 delta review at 3c2864f later found two more
  open conditions in this gate (P1: log coverage of the window was not established; P2: a STOP could be missed by `end`
  or deleted by the next `start`), fixed on the hardening branch at edf9f93 and 7b4839b.
- **N2+HW:** N2 plus the high-watermark VOID rule, 39dbad7.

| Experiment | Evidence: commit, gate, verdict | Observations (not evidence) |
|---|---|---|
| guard_check start | 31112b4, N2, PASS | 9cbc798 old PASS |
| static_check | 31112b4, N2, PASS | 9cbc798 old PASS |
| es_semantics | 31112b4, N2, PASS | 9cbc798 old PASS |
| n_old N1 to N7 (old code) | 31112b4, N2, violated as intended | 9cbc798 old, violated as intended |
| review_fix F3, F4, F5, F7, F1S, F6 | 31112b4, N2, PASS | 9cbc798 old PASS |
| review_fix F1 (100,000 events) | 31112b4, N2, PASS | 62c6be5 old PASS |
| scope_hold M, H | 31112b4, N2, PASS | 9cbc798 old PASS |
| n_new N1, N2, N5, N8 | 31112b4, N2, PASS | 9cbc798 old PASS |
| row1, row23, e_verify, registry_marker | 31112b4, N2, PASS | 9cbc798 old PASS |
| d1 | 31112b4, N2, PASS | 9cbc798 old PASS |
| d4 rescan, d4 store mark | 31112b4, N2, PASS | 62c6be5 old PASS |
| row19 (own corpus only) | 31112b4, N2, PASS | 9cbc798 old PASS |
| row6 | 31112b4, N2, PASS | 9cbc798 old PASS |
| d3 | **31112b4, N2, FAIL** (no verdict; high watermark from other tenants) **and 2e1948c, N2+HW, PASS** (rerun after amendment 23a) | 62c6be5 old PASS |
| mutual_mentions | 2e1948c, N2+HW, PASS (BLOCKED at 31112b4, never started) | 9cbc798 old PASS |
| lock_order | 2e1948c, N2+HW, PASS | 62c6be5 old PASS |
| d2 | 96214c2, N2+HW 20 GB, PASS (2e1948c: VOID, other tenants' disk; that run's PASS is not evidence) | 9cbc798 old PASS |
| phys_purge | 96214c2, N2+HW 20 GB, PASS | 9cbc798 old PASS |
| e2e E1 | 96214c2, N2+HW 20 GB, PASS (fenced in_flight window 0: unverified) | 62c6be5 old PASS |
| e2e E2, E3 | 96214c2, N2+HW 20 GB, PASS | 9cbc798 old PASS |
| projector_start_race | 96214c2, N2+HW 20 GB, PASS | 62c6be5 old PASS; 5d26746 old FAIL (harness, 19a) |
| projector_es_fault (N1) | 96214c2, N2+HW 20 GB, PASS | none (failing-first at 1539a75: fixed arm 0/5 per case) |
| races R1 | **96214c2, N2+HW 20 GB, INCONCLUSIVE** (control 19 overlapping < 20; fenced 0 violations, 0 errors) **and 0a433bc, N2+HW 20 GB, PASS** (the one rerun under amendment 24b, written after the INCONCLUSIVE; fenced 40/40, 23 overlapping; control violated 21/21 overlapping); never combined | 62c6be5 old PASS |
| races R2, R3 | 96214c2, N2+HW 20 GB, PASS | 62c6be5 old PASS |
| size_probe (exploratory) | 96214c2, N2+HW 20 GB, measured 4,478 B/doc (WAL refill included); 1M stays BLOCKED | 62c6be5 old, 2,071 B/doc |
| plan_stability (amendment 22, facts) | 96214c2, N2+HW 20 GB, PASS (CTL_FACTS 3/3 per state) | 62c6be5 old PASS without facts (scope N3) |
| heldout_secrets | carried: 62c6be5, old, FAIL (main's ruling; same secrets.py blob; no store) | |
| guard_check end | 96214c2, N2+HW 20 GB, PASS | 9cbc798 old PASS |

### Validity of these stamps under the gate's open conditions (step-1 delta review P1, P2)

Every step-1 stamp from 31112b4 to 0a433bc was issued under a gate with P1 and P2 open; the reviewer did not find
that any run hit either condition, and those containers are gone, so it cannot now be shown either way. The full
step-1 list is rerun under the fixed gate on the hardening branch (one gated chain; the list is committed in an
H-amendment first; heldout_secrets stays carried).

### Hardening probes for D6 (amendments H1 and H1a; two rows, never combined)

| row | commit | cases (3 runs each) | verdict |
|---|---|---|---|
| H1 (`h1-probe-606f457/`) | 606f457 | PB, PC, PE hold 3/3; PA and PD FAIL, but their requests were defective (PA: derived key without an empty `role_descriptors`; PD: invalidate without `owner: true`), so their behaviour was NOT measured | NOT FEASIBLE by the H1 rule; stands |
| H1a (`h1a-probe-c8e0a46/`), the one rerun allowed after H1 | c8e0a46 (bootstrap from 1a0fb11: the first attempt stopped in bootstrap before launch, nothing measured) | PB, PC, PE hold 3/3; PD holds 3/3: write 201 before, 401 after invalidation, 0 writes accepted after it, first refusal at most 0.055 s; PA invalid_request 3/3 (every derived-key form, with, without and with empty descriptors, refused 400 illegal_argument_exception) | PA failed a second time: **derived API keys are not demonstrated on Basic** (final). |

Consequence for D6 (main's ruling): the fence rests on alias-name scoping (PB) plus the alias CAS held by the broker
(PC, PE), not on revocation. The broker authenticates as a native user and never derives keys, so PA is not on its
path. Revocation (PD) is hygiene; its delay was measured at 0.055 s maximum over 3 runs on ONE node (cross-node API
key caches not shown).

### Limits that apply to every erasure result here

- **F1 residuals (step-1 delta review P3):** under stale statistics the outer delete of the cascade reads the tenant
  once per batch; early in a large cascade the GIN pending-list cost is paid by the first batches. F1's PASS (100,000
  events) does not bound either.
- **size_probe vs the 1M run:** size_probe RAN, as an exploratory measurement (no PASS). What is BLOCKED is the
  projected 1,000,000-document run, which has not run on this host (projected own usage over the 2 GB cap).

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

Every step-1 experiment that uses the stores is stamped at a post-fix commit: batch c (31112b4), batch d (2e1948c)
or batch e (96214c2), with R1's one rerun at 0a433bc. row19's 880/880 is on its author's own planted corpus only; on held-out data the scanner caught 483/1,169
and 150/750 (heldout_secrets FAIL). Not run: a full run_all restamp (main's order: stamped singles instead).
