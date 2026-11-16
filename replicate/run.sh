#!/bin/sh
# usage: [BG=1] replicate/run.sh <relative script path> [args]
# Syncs replicate/ to t2-dev ~/emlab-16096/rep/code, runs with the lab venv, pulls results to replicate/results/.
set -e
B=$(cd "$(dirname "$0")" && pwd); S="ssh -F $HOME/.ssh/config_fleetops"
COMMIT=$(git -C "$B" rev-parse --short HEAD); DIRTY=$(git -C "$B" status --porcelain -- . | grep -v '^?? results/' | head -1)
[ -n "$DIRTY" ] && COMMIT="$COMMIT-dirty"
COPYFILE_DISABLE=1 tar -C "$B" -cf - --exclude results . | $S t2-dev 'mkdir -p ~/emlab-16096/rep/code ~/emlab-16096/rep/results ~/emlab-16096/rep/logs && tar -C ~/emlab-16096/rep/code -xf - 2>/dev/null'
script=$1; shift
name=$(basename "$script" .py)
if [ -n "$BG" ]; then
  $S -n t2-dev "cd ~/emlab-16096/rep/code && REP_COMMIT=$COMMIT nohup ${PYBIN:-\$HOME/emlab-16096/venv/bin/python} -u $script $* > \$HOME/emlab-16096/rep/logs/$name.log 2>&1 < /dev/null & echo \"\$(date -u +%FT%TZ) pid \$! $script $*\" | tee -a \$HOME/emlab-16096/rep/spawn_ledger.txt"
else
  $S t2-dev "cd ~/emlab-16096/rep/code && REP_COMMIT=$COMMIT ${PYBIN:-\$HOME/emlab-16096/venv/bin/python} -u $script $*"
  "$B/pull.sh"
fi
