#!/bin/sh
# usage: run_remote.sh script.py [args]  -> syncs behaviour/ to t2-dev, runs there, pulls results back
set -e
B=$(dirname "$0"); S="ssh -F $HOME/.ssh/config_fleetops"
COPYFILE_DISABLE=1 tar -C "$B" -cf - --exclude results . | $S t2-dev 'mkdir -p ~/emlab-16096/behaviour && tar -C ~/emlab-16096/behaviour -xf -'
script=$1; shift
$S t2-dev "cd ~/emlab-16096/behaviour && RESDIR=~/emlab-16096/results timeout 110 ~/emlab-16096/venv/bin/python $script $*"
$S t2-dev 'tar -C ~/emlab-16096/results -cf - .' | tar -C "$B/results" -xf -
