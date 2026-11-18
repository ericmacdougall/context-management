# Seal authority: failing-first receipts (offline, Mac)

- `test_sealer-on-1a4a5c2.red.out`: previous owner's red run (10 failures by assertion, 0 errors).
- `test_sealer-on-179412e.red.out`: rerun on the exact test commit 179412e (tests committed, no wp/sealer.py):
  `WP_UCD_DIR=<ucd> python3 ~/.claude/hooks/run_capped.py --max-rss-gb 2 --timeout-s 600 -- python3 -m unittest tests.test_sealer`
  -> FAILED (failures=10), 0 errors.
- `test_sealer-green.out`: the same command on the seal-authority commit: 10 OK.
- `focused14-green.out`: the 14 focused offline modules on the same commit: 235 run, OK, 41 skipped (the live
  Postgres/Elasticsearch modules skip offline; they are NOT evidence for the store-side checks).
Paths in the outputs are replaced by placeholders.
