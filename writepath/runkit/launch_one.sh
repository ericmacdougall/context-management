#!/bin/sh
# One stamped experiment on the lab host, from a PINNED tree (pin_run.sh), with the run_all validity gates of amendment
# 17c: the start line (20 GB from amendment 24; 17 GB before), exp.disk_gate start / check / end, and a watcher that writes WP_HOME/state/STOP when this
# stream's Elasticsearch logs a flood-stage watermark (disk_gate end then marks the run VOID).
# usage: sh runkit/launch_one.sh <pinned dir> <label> <module> [args...]
#        output WP_HOME/results/one.<head>.<label>.out; prints that path, the head, the start time and free space.
# Needs WP_HOME (this stream's home on the host) and ES_CONTAINER (default emlab2-wphard-es).
set -eu
: "${WP_HOME:?WP_HOME must name the home of this stream}"
export WP_HOME
ES_CONTAINER=${ES_CONTAINER:-emlab2-wphard-es}
FREE=$(df -BG --output=avail "$WP_HOME" | tail -1 | tr -dc 0-9)
[ "$FREE" -ge 20 ] || { echo "BLOCKED free ${FREE}G < 20"; exit 3; }   # amendment 24 (17 before)
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
RP=$!
setsid sh -c "while kill -0 $RP 2>/dev/null; do
  if docker logs --since $S $ES_CONTAINER 2>&1 | grep -qiE 'flood stage disk watermark|high disk watermark .* exceeded'; then
    touch $WP_HOME/state/STOP; echo \"flood stage or high watermark seen \$(date -u +%T): STOP written\"; exit 0; fi
  sleep 10; done; echo 'run ended, watcher exits'" > "$WP_HOME/state/flood_watch-$H.$L.log" 2>&1 < /dev/null &
echo "$OUT head $H start $S free ${FREE}G"
