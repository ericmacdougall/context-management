#!/bin/sh
# Live items GF16096-L1 (lock refusals, abort only when dead) and GF16096-L9 (F-1: store tools refuse while a run holds
# the lock), on t2-dev, own home. A stand-in holder takes the run lock exactly as the gate does (flock(2) on
# WP_HOME/state/run.lock, via flock(1)) for 40 s; while it holds it: the gate's run is BLOCKED, abort is refused,
# wp.setup --reset and exp.cleanup refuse (nothing reaches the stores). After it exits: verdict of an unknown run
# exits 2. Sent over stdin: sh -s.
set -u
. ~/emlab2-16096/gatefix/env.sh
cd "$P"
date -u +%FT%TZ
flock -n "$WP_HOME/state/run.lock" sleep 40 &
HOLDER=$!
sleep 2
step() {
  echo "## $1"; shift
  out=$("$@" 2>&1); rc=$?
  printf '%s\n' "$out" | grep -v '^gate_limits' | tail -4
  echo "exit $rc"
}
step "run while the lock is held" sh runkit/launch_one.sh "$P" l1-locked exp.live_probe ok
step "abort while the lock is held" "$PY" -I -B runkit/isolated.py exp.disk_gate abort r-00000000000000000000000000000000 "live lock check"
step "wp.setup --reset while the lock is held (F-1)" "$PY" -I -B runkit/isolated.py wp.setup --reset
step "exp.cleanup while the lock is held (F-1)" "$PY" -I -B runkit/isolated.py exp.cleanup live-lock-check
wait "$HOLDER"
step "holder done; verdict of a run this ledger never admitted" "$PY" -I -B runkit/isolated.py exp.disk_gate verdict r-00000000000000000000000000000000
date -u +%FT%TZ
