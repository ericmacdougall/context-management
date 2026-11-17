#!/bin/sh
# Proof runs on t2-dev from one synced copy: `SYNC_RUN=<name> sh sync.sh` (clean tree; <name> is one [A-Za-z0-9._-] component), then on the host:
#   POL_SEEDS=stamped sh ~/emlab2-16096/policy/runs/<name>/src/hostrun.sh all      (stamped: fresh seeds of the latest THRESHOLDS amendment, ONCE)
#   ALLOW_DIRTY=1 sh ~/emlab2-16096/policy/runs/<name>/src/hostrun.sh all --dev    (development: dev-* seeds, results prefixed DEV.)
# B3, pg_diff, mig_rehearsal and s2_sessions need the Postgres env (paths to 0600 files only); hostrun.sh exports it and PY.
# Each experiment writes one result file under results/ (no partial reruns combined); a run that errors is a FAIL. pg_diff and
# mig_rehearsal exit nonzero on their own FAIL verdict (scored FAIL here) and are re-judged by verify/pgdiff.py and verify/mig.py.
#
# Disk guard (amendment 6; the host is shared: at edb693f this run took free space to 13.4 GB during pg_diff and voided another owner's
# stamped run). Before each experiment free space must be at least START_KB, and while it runs free space is sampled every 5 s: under
# STOP_KB the experiment is killed and the run STOPS. Experiments without a result file are scored FAIL by verify, so a stopped run is
# void. The lab database is dropped and recreated by the next install, which returns its space. Each experiment's start, end and lowest
# free space and the lab database size are logged. The limits are fixed here, not environment overrides.
PY=${PY:-../venv/bin/python}
START_KB=16777216                          # 16 GiB before an experiment starts
STOP_KB=16252928                           # 15.5 GiB while it runs (the host floor is 15 GB)
free_kb() { df -Pk "$HOME" | awk 'NR==2 {print $4}'; }
db_kb() { docker exec emlab2-policy-pg du -sk /var/lib/postgresql/data 2>/dev/null | awk '{print $1}'; }
rc=0
for m in e1_lattice e2_egress e3_clash e4_tighten e5_threads e6_policy_format b1_modelcheck b2_rebuild b3_pg pg_diff mig_rehearsal s2_sessions; do
  f=$(free_kb)
  if [ -z "$f" ] || [ "$f" -lt "$START_KB" ]; then
    echo "STOP disk guard before $m: free ${f:-unknown} KB < $START_KB KB; this run is void"; rc=1; break
  fi
  echo "== $m $(date -u +%FT%TZ) free_kb=$f db_kb=$(db_kb)"
  $PY -W ignore -m exp.$m &
  pid=$!
  low=$f
  stopped=0
  while kill -0 "$pid" 2>/dev/null; do
    f=$(free_kb)
    [ -n "$f" ] && [ "$f" -lt "$low" ] && low=$f
    if [ -z "$f" ] || [ "$f" -lt "$STOP_KB" ]; then
      echo "STOP disk guard during $m: free ${f:-unknown} KB < $STOP_KB KB; killing it; this run is void"
      kill "$pid" 2>/dev/null; sleep 5; kill -9 "$pid" 2>/dev/null; stopped=1; break
    fi
    sleep 5
  done
  if wait "$pid"; then :; else echo "ERROR in $m (scores FAIL)"; rc=1; fi
  echo "== end $m $(date -u +%FT%TZ) free_kb=$(free_kb) low_free_kb=$low db_kb=$(db_kb)"
  [ "$stopped" = 1 ] && { rc=1; break; }
done
$PY -W ignore -m verify results "$@" || rc=1
exit $rc
