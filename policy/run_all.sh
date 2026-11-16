#!/bin/sh
# Proof runs on t2-dev from a synced tree: `sh sync.sh` (clean tree), then on the host in ~/emlab-16096/polr/src:
#   POL_SEEDS=stamped sh run_all.sh        (stamped: fresh seeds of THRESHOLDS amendment 3, each configuration run ONCE)
#   ALLOW_DIRTY=1 sh run_all.sh --dev      (development: dev-* seeds, results prefixed DEV.)
# B3 and pg_diff need the Postgres env (paths to 0600 files only) exported by the caller; see ~/emlab-16096/polr/runall.sh.
# Each experiment writes one result file under results/ (no partial reruns combined); a run that errors is a FAIL.
PY=${PY:-../venv/bin/python}
rc=0
for m in e1_lattice e2_egress e3_clash e4_tighten e5_threads e6_policy_format b1_modelcheck b2_rebuild b3_pg pg_diff; do
  echo "== $m $(date -u +%FT%TZ)"; $PY -W ignore -m exp.$m || { echo "ERROR in $m (scores FAIL)"; rc=1; }
done
$PY -W ignore -m verify results "$@" || rc=1
exit $rc
