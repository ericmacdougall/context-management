# Pre-registered thresholds (committed before any proof run)

Applies to every experiment under exp/ in this branch. Proof rules: gotchas/PROOF-RULES.md on lab main.

Common
- Interleavings are forced by named in-process hooks (the wrapped function runs the competing operation at the exact
  point), not by sleeps. Each arm runs R = 10 times on a fresh tenant.
- PASS for a scenario: the fenced arm satisfies the property in 10/10 runs AND the control arm violates it in at least
  9/10 runs, with the violation reason recorded per run, AND every witness count named below is > 0 in every run.
- A run that errors (exception other than the expected refusal) scores as a failure of that arm.
- Results: content-addressed JSON with environment fingerprint under WP_HOME/results.

N1 read-before-pin: witness = rows read containing the subject before the erasure; overlap witness = DB clock times
  read < erasure phase-1 commit < write attempt. Control = projector that resolves its epoch after the read.
N2 every family: witness = families enumerated from wp.families.FAMILIES (>= 2) each with > 0 subject docs before.
  Control = a writer for an unregistered family; it must leave residue AND be flagged by registry.unregistered().
N3 stale rebuild: witness = A's survivor read contains Y; overlap = A read < B done < A write. Control = A writes by
  concrete index name.
N4 completion: witness = residue count > 0 at completion time. Control = the same run on wp_ctl (no guard trigger),
  where 'done' is accepted with residue, plus a reader that filters only while a job is open.
N5 pending reads: witness = raw ES count of the subject > 0 while pending. Control = facts read without the filter.
N6 no plaintext in Postgres: marker hits in every table of wp_events and wp_keys = 0 (SQL scan of all columns cast to
  text, and grep of the database's files on disk after CHECKPOINT). Positive control: the same grep finds the old
  code's plaintext marker in database 'emlab' on the same cluster.
N7 decisions: (a) stale acceptance refused, (b) replay refused or duplicate, (c) same-id retry is one event.
  Control = wp_ctl (no head compare-and-set, no idempotency constraint): must accept (a) and duplicate (c).
Row 1 zombie session: 20 zombie appends + 1 confirm per run. Pass = 0 accepted, 20 quarantined, chain verifies,
  0 analyst jobs and 0 ES docs from the zombie, inbox item shown once and confirmed by the new holder only.
  Control = wp_ctl: at least 1 zombie event accepted or the item confirmed by the zombie.
Row 23 late commit: hold one transaction open while 10 later ones commit. Naive cursor control must skip >= 1 row;
  the xmin cursor must skip 0 and project all 11.
Registry: unregistered() == [] on a clean system; controls: one rogue table and one rogue index must each be listed.
ES on-disk marker grep: if the pre-erasure grep finds 0 files, the on-disk claim is NOT made (API level only).

## Amendment 1 (committed before the second, reported run; the a482a22 run is superseded)
Added after an advisor review found two unexercised paths:
N3S_projector stale swap: eraser A has bulk-written its staging index; a projector takes over (force) before A's swap.
  Pass = A's swap refused, read alias on exactly 1 index, the new projector's write alias present and a new event W
  projected, survivor Z live. Control = A swaps without the compare-and-set: must leave W unprojected or the alias gone.
N3S_eraser stale swap with an eraser B as taker: fenced arm only. No sensitive control exists in this harness because
  B's swap deletes A's staging index; reported as SUPPORTING evidence, never as PASS.
N8 writers racing phase 1: a derive that read X's fact before phase 1, an inbox enqueue for X and a stale append for X
  are released while phase 1 is open. Overlap witness = backends waiting on a lock during phase 1 (>= 1).
  Pass = job done and zero X residue in facts, inbox, quarantine and ES. Control = wp_ctl: residue > 0 after done.
ES probe P8: remove_index combined with a failing must_exist remove must leave the index in place.
Row 27 (stale-approval half): amended -> execution refused 10/10; not amended -> accepted 10/10; control accepts >= 9/10.
