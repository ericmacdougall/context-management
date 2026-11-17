#!/bin/sh
# Stamped single experiments, one after another, from one pinned tree (amendment 20). Each goes through launch_one.sh;
# the chain stops at the first BLOCKED or VOID run (a VOID may be rerun and is recorded; a FAIL is recorded, never rerun).
# usage: sh runkit/chain_singles.sh <pinned dir> <spec file>      (log it: > WP_HOME/state/chain_singles.<head>.log)
# Spec file: one experiment per line, "<label> <cleanup module or -> <module> [args...]"; '#' lines are comments.
# A line "@ <module> [args...]" runs that module directly (not stamped), e.g. the cleanup and setup reset that run_all
# does at its start.
set -u
: "${WP_HOME:?WP_HOME must name the home of this stream}"
export WP_HOME
D=$1; SPEC=$2
K=$(cd "$(dirname "$0")" && pwd)
H=$(cat "$D/GIT_HEAD")
PY=$WP_HOME/venv/bin/python
echo "chain start $(date -u +%FT%TZ) head $H spec $(basename "$SPEC")"
while read -r L C M; do
  case "$L" in ''|'#'*) continue ;; esac
  if [ "$L" = "@" ]; then
    echo "direct: $C $M"
    OUTD=$(cd "$D" && $PY -m $C $M < /dev/null 2>&1); RC=$?
    printf '%s\n' "$OUTD" | tail -2
    [ "$RC" -eq 0 ] || { echo "chain stopped: direct step failed (exit $RC)"; exit 3; }
    continue
  fi
  R=$(sh "$K/launch_one.sh" "$D" "$L" $M < /dev/null) || { echo "$L: $R"; echo "chain stopped before $L"; exit 3; }
  echo "$R"
  OUT=$WP_HOME/results/one.$H.$L.out
  while ! grep -qE "^end" "$OUT"; do sleep 5; done
  grep -E "^end" "$OUT"
  grep -qE "^end [0-9]" "$OUT" || { echo "chain stopped after $L"; exit 3; }
  [ "$C" = "-" ] || (cd "$D" && $PY -m exp.cleanup "$C" < /dev/null | tail -1)
done < "$SPEC"
echo "chain end $(date -u +%FT%TZ)"
