#!/bin/sh
# usage: [BG=1] run_remote.sh script.py [args]
# syncs behaviour/ to t2-dev and runs there (foreground <=110s, or detached with BG=1 -> log ~/emlab-16096/logs/<script>.log).
# pull.sh copies results back.
set -e
B=$(dirname "$0"); S="ssh -F $HOME/.ssh/config_fleetops"
COPYFILE_DISABLE=1 tar -C "$B" -cf - --exclude results . | $S t2-dev 'mkdir -p ~/emlab-16096/behaviour ~/emlab-16096/logs && tar -C ~/emlab-16096/behaviour -xf - 2>/dev/null'
script=$1; shift
if [ -n "$BG" ]; then
  $S -n t2-dev "cd ~/emlab-16096/behaviour && ulimit -n 8000; RESDIR=\$HOME/emlab-16096/results nohup \$HOME/emlab-16096/venv/bin/python -u $script $* > \$HOME/emlab-16096/logs/$script.log 2>&1 < /dev/null & echo launched pid \$!" 
else
  $S t2-dev "cd ~/emlab-16096/behaviour && RESDIR=\$HOME/emlab-16096/results timeout 110 \$HOME/emlab-16096/venv/bin/python $script $*"
  "$B/pull.sh"
fi
