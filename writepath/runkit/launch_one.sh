#!/bin/sh
# One stamped experiment on the lab host, from a PINNED tree (pin_run.sh), with the run_all validity gates of amendment
# 17c: the start line (20 GB from amendment 24; 17 GB before) and exp.disk_gate start / check / end. Since review A the
# gate's own capture companion (started by `disk_gate start`) records a stop in the ledger at the first flood-stage or
# high-watermark line it captures; the launcher's wall-clock `docker logs --since | grep` watcher is gone
# (runkit/GATE-DESIGN.md). A VOID check closes the run in the ledger itself.
# usage: sh runkit/launch_one.sh <pinned dir> <label> <module> [args...]
#        output WP_HOME/results/one.<head>.<label>.out; prints that path, the head, the start time and free space.
# Needs WP_HOME (this stream's home on the host).
set -eu
: "${WP_HOME:?WP_HOME must name the home of this stream}"
export WP_HOME
# exact KiB (amendment H4): df -BG rounds up and let 19.3 GiB through; disk_gate measures free / GiB >= 20.0
FREEK=$(df -k --output=avail "$WP_HOME" | tail -1 | tr -dc 0-9)
FREE=$(awk -v k="$FREEK" 'BEGIN{printf "%.1f", k/1048576}')
[ "$FREEK" -ge 20971520 ] || { echo "BLOCKED free ${FREE}G < 20"; exit 3; }   # amendment 24; exact KiB since H4
D=$1; L=$2; shift 2
case "$D" in */runs/*) ;; *) echo "refusing: not a pinned tree"; exit 2 ;; esac
cd "$D"
H=$(cat GIT_HEAD)
S=$(date -u +%FT%TZ)
OUT=$WP_HOME/results/one.$H.$L.out
PY=$WP_HOME/venv/bin/python
setsid sh -c "echo start $S head $H free ${FREE}G: $*
  $PY -m exp.disk_gate start || { echo 'end BLOCKED'; exit 2; }
  $PY -m exp.disk_gate check '$L' || { echo 'end VOID (disk gate)'; exit 3; }
  $PY -m $*
  echo exit=\$?
  $PY -m exp.disk_gate end || { echo \"end VOID \$(date -u +%FT%TZ)\"; exit 3; }
  echo \"end \$(date -u +%FT%TZ)\"" > "$OUT" 2>&1 < /dev/null &
echo "$OUT head $H start $S free ${FREE}G"
