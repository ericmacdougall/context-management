#!/bin/sh
# One stamped experiment on the lab host, from a PINNED tree (pin_run.sh), with the run_all validity gates of amendment
# 17c: the start line (20 GB from amendment 24; 17 GB before; the gate measures it again inside its locked admission)
# and exp.disk_gate start / check / end (gate v4, runkit/GATE-DESIGN.md sections 5 and 6: private tmpfs stores bound to
# the endpoints the experiment uses, nothing to watch). A VOID check closes the run in the ledger itself.
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
# Two separate fields (review D F9): experiment_exit=<status|not run> is the experiment's own outcome (a FAIL is a result);
# gate=VALID|VOID|BLOCKED is the gate's verdict on the run. The `end` line keeps its earlier form for window_check.sh.
# gate_version= and gate_limits= (review F F19, GATE-DESIGN 6.2 B8) name the gate, its SHA-256 and the limits of its
# guarantee before any verdict; a gate that cannot name itself runs nothing.
setsid sh -c "echo start $S head $H free ${FREE}G: $*
  if ! $PY -m exp.disk_gate version; then echo 'experiment_exit=not run'; echo 'gate=BLOCKED'; echo 'end BLOCKED'; exit 2; fi
  if ! $PY -m exp.disk_gate start; then echo 'experiment_exit=not run'; echo 'gate=BLOCKED'; echo 'end BLOCKED'; exit 2; fi
  if ! $PY -m exp.disk_gate check '$L'; then echo 'experiment_exit=not run'; echo 'gate=VOID'; echo 'end VOID (disk gate)'; exit 3; fi
  $PY -m $*
  echo experiment_exit=\$?
  if $PY -m exp.disk_gate end; then echo gate=VALID; echo \"end \$(date -u +%FT%TZ)\"
  else echo gate=VOID; echo \"end VOID \$(date -u +%FT%TZ)\"; exit 3; fi" > "$OUT" 2>&1 < /dev/null &
echo "$OUT head $H start $S free ${FREE}G"
