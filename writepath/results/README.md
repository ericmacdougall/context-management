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
- **races R1, R2 and R3: PASS.** Fenced: 25/25, 39/39 and 40/40 overlapping runs held. Control: 22/22, 40/40 and 39/39 violated.
- **plan_stability: PASS** in S1 and S2, 3/3 runs each.
  - Apply: 6 rows of work per event. Residue: about 14 rows per event, ratio 1.99 when M doubles.
  - Both controls were violated 3/3 per state.
- **size_probe at N=50,000** (exploratory, no PASS):
  - Measured: 2,071 bytes of store growth per document.
  - Linear projection: 2.28 GB for the 1.1M-document scale_1m run. That is over this stream's 2 GB own-usage cap before any transient peak (ES segment merges, WAL growth, the key-table VACUUM FULL).
  - Result: the 1M rerun stays BLOCKED on this host.

Not run at 62c6be5: guard_check end, and a full run_all restamp.
