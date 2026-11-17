#!/bin/sh
# Proof runs on t2-dev from one synced copy: `SYNC_DEST=emlab2-16096/policy/runs/<name>/src sh sync.sh` (clean tree), then on the host:
#   POL_SEEDS=stamped sh ~/emlab2-16096/policy/runs/<name>/src/hostrun.sh all      (stamped: fresh seeds of THRESHOLDS amendment 5, ONCE)
#   ALLOW_DIRTY=1 sh ~/emlab2-16096/policy/runs/<name>/src/hostrun.sh all --dev    (development: dev-* seeds, results prefixed DEV.)
# B3, pg_diff and mig_rehearsal need the Postgres env (paths to 0600 files only); hostrun.sh exports it and PY.
# Each experiment writes one result file under results/ (no partial reruns combined); a run that errors is a FAIL. pg_diff and
# mig_rehearsal exit nonzero on their own FAIL verdict (scored FAIL here) and are re-judged by verify/pgdiff.py and verify/mig.py.
PY=${PY:-../venv/bin/python}
rc=0
for m in e1_lattice e2_egress e3_clash e4_tighten e5_threads e6_policy_format b1_modelcheck b2_rebuild b3_pg pg_diff mig_rehearsal; do
  echo "== $m $(date -u +%FT%TZ)"; $PY -W ignore -m exp.$m || { echo "ERROR in $m (scores FAIL)"; rc=1; }
done
$PY -W ignore -m verify results "$@" || rc=1
exit $rc
