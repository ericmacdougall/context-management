#!/bin/sh
# usage: [BG=1] run_remote.sh exp/script.py [args]
# Syncs systems/ to t2-dev:~/emlab-16096/systems/src and runs it with the pinned venv.
# Foreground is bounded to 110 s; BG=1 detaches with a log under ~/emlab-16096/systems/logs.
set -e
B=$(cd "$(dirname "$0")" && pwd); S="ssh -F $HOME/.ssh/config_fleetops"
COPYFILE_DISABLE=1 tar -C "$B" -cf - --exclude results --exclude __pycache__ . | $S t2-dev 'mkdir -p ~/emlab-16096/systems/src && tar -C ~/emlab-16096/systems/src -xf - 2>/dev/null'
script=$1; shift
name=$(basename "$script" .py)
if [ -n "$BG" ]; then
  $S -n t2-dev "cd ~/emlab-16096/systems/src && ulimit -n 8000; nohup \$HOME/emlab-16096/systems/venv/bin/python -u $script $* > \$HOME/emlab-16096/systems/logs/$name.log 2>&1 < /dev/null & echo launched pid \$!"
else
  $S t2-dev "cd ~/emlab-16096/systems/src && ulimit -n 8000; timeout 110 \$HOME/emlab-16096/systems/venv/bin/python $script $*"
fi
