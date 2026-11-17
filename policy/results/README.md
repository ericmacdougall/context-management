# policy/results

Stamped result sets of the policy lab (THRESHOLDS.md), the current one at the top level, earlier ones in `superseded/` (moved there by
pure renames, never edited). `DEV.*` files are development runs (dev seeds), not proof. `red/` holds failing-first logs.

## Current stamped set: d05d731 (amendment 6), 2026-11-17

- Run: once, t2-dev, own container emlab2-policy-pg (postgres:16.15, rebuilt before the run), clean synced copy
  runs/stamped-d05d731 (GIT_STAMP dirty=false, commit == thresholds commit == d05d731), kit/launch_stamped.sh
  (`POL_SEEDS=stamped sh hostrun.sh all`, no override variable) after kit/preflight.sh passed; 12:18:15Z to 13:59Z, exit 0.
- Verdict: all 12 experiments PASS (E1-E6, B1, B2, B3, pg_diff, mig_rehearsal, s2_sessions), each recomputed by verify/ PASS with
  agree=True; `VERIFY.3b4275a02a461b58.json` overall PASS, provenance ok (one commit d05d731, thresholds sha256 c8f90f73...).
- **Review found M1-M3 at this commit** (delta review 1, reviews/policy-delta/review-1.md, run ad27751f): M2 means its S2 PASS does
  not establish coverage (the S2 verifier of d05d731 took its coverage from the result it judged); M1 (transaction-start clock in
  proof and session expiry) and M3 (in-memory idempotency under overlapping submissions) change product code, so this set will itself
  be superseded by the amendment 7 stamped run.
- Per-item rows NOT in git: the 12 `*.items.*.jsonl.gz` files (48 MB) stay on t2-dev in
  ~/emlab2-16096/policy/runs/stamped-d05d731/src/results/. Copying them to the Mac was refused by the heavy-job guard (the Mac disk
  is above its 85% floor). `stamped.d05d731.MANIFEST.sha256` lists the sha256 of all 25 files; every result JSON here matches it,
  and each result JSON records its items file's sha256. A recomputation (`python -m verify`) needs those items files.

## Superseded

- edb693f (amendment 5): VERIFY ba442234, moved to `superseded/` with this commit; superseded by review-3's fixes and step 2.
- 8c03f02 and earlier: see `superseded/` and THRESHOLDS.md.
