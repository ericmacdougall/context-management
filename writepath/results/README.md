# Write-path lab results (GoodQA #16096)

Raw result documents copied from the lab host. The only change from the host copies is that the host home directory
prefix in paths is shortened to `~/`. `MANIFEST.tsv` lists, for every file, the sha256 of the host copy (the hash the
`RESULT` lines of `run_all.*.out` print, and the 16-hex suffix of the file name) and the sha256 of the committed copy.
Result documents carry ids and hashes only; a leak scan of every file against the lab's live secret values found none.

| Folder | What it is | Status |
|---|---|---|
| `run_all-fd4ed53-VOID/` | The run_all at fd4ed53, started 2026-11-17T07:56:40Z. | **VOID** (amendment 17c): Elasticsearch reached its flood-stage watermark at 10:23:42Z because another tenant of the host filled the disk (this stream's own usage stayed at +0.17 GiB). The run stopped before `cleanup exp.e2e_restored`. races, plan_stability and the final guard_check were not run. Everything here is an observation, not a stamped result. |
| `failing-first/` | Results against the code before a fix: lock_order at eaed478 (amendment 17) and plan_stability at fdbecb4 (amendment 18b). | Failing-first evidence. |
| `unstamped/` | plan_stability at ba71567 (its PASS is VOIDED by amendment 18c: the work metric did not multiply rows removed by loops) and at 83f7000 (the fix; PASS, a pre-run, not the stamped run). | Not stamped. |
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
