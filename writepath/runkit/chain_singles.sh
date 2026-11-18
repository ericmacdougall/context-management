#!/bin/sh
# Stamped single experiments, one after another, from one pinned tree (amendment 20), each made by the gate itself:
# `exp.disk_gate run LABEL MODULE ARGS` admits, runs and ends the run in one process (gate v5, GATE-DESIGN 8).
# The chain decides on exit statuses only, never on text (review a108fc3 G-3): the gate exits 0 VALID, 3 VOID,
# 2 BLOCKED; any other step exits 0 or the chain stops. Before each stamped line the chain deletes the stream's indices
# (exp.cleanup) and resets the databases (wp.setup --reset): the gate admits a run only on clean stores (P7). After a
# stamped line its cleanup module, if any, runs as before. The chain stops at the first non-zero status (exit 3).
# Every step's output goes to its own file in a NEW chain directory WP_HOME/results/chains/<utc>-<pid>/, made by
# mkdir, which refuses an existing one (P10: a rerun never overwrites an earlier attempt's record); the verdict itself
# is the gate's end record in the ledger
# (`exp.disk_gate verdict <run>`), and the experiment's output is in results/runs/<run>/experiment.out.
# usage: sh runkit/chain_singles.sh <pinned dir> <spec file>      (keep its own output: > <log> 2>&1)
# Spec file: one experiment per line, "<label> <cleanup module or -> <module> [args...]"; '#' lines are comments.
# A line "@ <module> [args...]" runs that module directly (not stamped). Labels: letters, digits, '_', '.', '-'
# (review a108fc3 m-5: a label is data, never shell text).
set -u
set -f
: "${WP_HOME:?WP_HOME must name the home of this stream}"
export WP_HOME
D=$1; SPEC=$2
H=$(cat "$D/GIT_HEAD") || { echo "chain refused: $D has no GIT_HEAD (not a pinned tree)"; exit 2; }
PY=$WP_HOME/venv/bin/python
CD=$WP_HOME/results/chains/$(date -u +%Y%m%dT%H%M%SZ)-$$
mkdir -p "$WP_HOME/results/chains" && mkdir "$CD" || { echo "chain refused: cannot create a new directory $CD"; exit 2; }
echo "chain start $(date -u +%FT%TZ) head $H spec $(basename "$SPEC") records $CD"
N=0
# step NAME MODULE [ARGS]: a module of the pinned tree, its output to a new file; returns its exit status
step() {
  N=$((N + 1)); o="$CD/$N.$1.out"; shift
  (cd "$D" && exec "$PY" -B -m "$@") < /dev/null > "$o" 2>&1
}
while read -r L C M; do
  case "$L" in ''|'#'*) continue ;; esac
  if [ "$L" = "@" ]; then
    echo "direct: $C $M"
    step direct $C $M || { echo "chain stopped: direct step $C failed (exit $?)"; exit 3; }
    continue
  fi
  case "$L" in
    *[!A-Za-z0-9_.-]* | [!A-Za-z0-9]*) echo "chain stopped: the label of a line holds characters other than letters, digits, '_', '.', '-'"; exit 3 ;;
  esac
  step stops exp.disk_gate stops || { echo "chain stopped: an unacknowledged stop before $L (exit $?)"; exit 3; }
  step "$L.cleanup-before" exp.cleanup "$L" || { echo "chain stopped: index cleanup before $L failed (exit $?)"; exit 3; }
  step "$L.reset-before" wp.setup --reset || { echo "chain stopped: store reset before $L failed (exit $?)"; exit 3; }
  step "$L.gate" exp.disk_gate run "$L" $M
  G=$?
  echo "$L: gate exit $G (0 VALID, 3 VOID, 2 BLOCKED); record $CD/$N.$L.gate.out"
  [ "$G" -eq 0 ] || { echo "chain stopped after $L"; exit 3; }
  if [ "$C" != "-" ]; then
    step "$L.cleanup-after" exp.cleanup "$C" || { echo "chain stopped: cleanup $C after $L failed (exit $?)"; exit 3; }
  fi
done < "$SPEC"
echo "chain end $(date -u +%FT%TZ)"
