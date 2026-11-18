#!/bin/sh
# One stamped experiment on the lab host, from a PINNED tree (pin_run.sh), made by the gate itself (gate v5,
# runkit/GATE-DESIGN.md section 8): `exp.disk_gate run` admits the run (pinned tree equal to its commit, registered
# fixtures, clean stores, every storage rule, 10 GB free measured inside the locked admission, amendment GF1), runs the experiment
# in the foreground with the run lock, ends the run and exits with the verdict: 0 VALID, 3 VOID, 2 BLOCKED.
# The verdict is the gate's end record in the ledger (`exp.disk_gate verdict <run>`); the experiment's output goes to
# WP_HOME/results/runs/<run>/experiment.out and never into the gate's own lines (review a108fc3 G-3). No shell string
# is built from the label or arguments (m-5); the gate validates the label. To detach, run this under nohup yourself.
# Gate v6 (GATE-DESIGN 9): the gate resets the stores itself under its run lock (no reset before this script), and
# runs isolated: `python -I -B <tree>/runkit/isolated.py exp.disk_gate run ...` (review aacf3c0 m-G; the gate refuses
# a non-isolated interpreter).
# usage: sh runkit/launch_one.sh <pinned dir> <label> <module> [args...]
# Needs WP_HOME (this stream's home on the host).
set -eu
: "${WP_HOME:?WP_HOME must name the home of this stream}"
export WP_HOME
D=$1; shift
case "$D" in */runs/*) ;; *) echo "refusing: not a pinned tree"; exit 2 ;; esac
cd "$D"
exec "$WP_HOME/venv/bin/python" -I -B "$D/runkit/isolated.py" exp.disk_gate run "$@"
