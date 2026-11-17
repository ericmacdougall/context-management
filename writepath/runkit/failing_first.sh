#!/bin/sh
# Failing-first receipt (review A, A6): run a focused test module against a file taken from an earlier commit and save
# the receipt with the tested blob's hash, the test file's hash, the exact command, the exit status and every
# assertion trace. Run from writepath/. Never prints a secret: the tests generate fake values and print names only.
# usage: sh runkit/failing_first.sh <commit> <path in repo of the file under test> <ENV var naming the file> <test module> <out>
#   e.g. sh runkit/failing_first.sh 7b4839b writepath/exp/disk_gate.py DISK_GATE_UNDER_TEST tests.test_disk_gate \
#          results/failing-first/reviewA-a1a2a3-disk_gate-7b4839b.red.out
set -u
C=$1; P=$2; V=$3; M=$4; OUT=$5
[ -e "$OUT" ] && { echo "refusing: $OUT exists (a receipt is never overwritten)"; exit 2; }
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
F="$W/$(basename "$P")"
git show "$C:$P" > "$F" || { echo "cannot read $C:$P"; exit 2; }
TF="tests/$(echo "$M" | sed 's/^tests\.//').py"
{
  echo "# failing-first receipt"
  echo "# date: $(date -u +%FT%TZ)"
  echo "# file under test: $C:$P blob $(git hash-object "$F") sha256 $(shasum -a 256 "$F" 2>/dev/null | cut -c1-64 || sha256sum "$F" | cut -c1-64)"
  echo "# test file: $TF at HEAD $(git rev-parse --short HEAD) blob $(git hash-object "$TF")"
  echo "# command: $V=<that file> python3 -m unittest $M -v"
} >> "$OUT"
env "$V=$F" python3 -m unittest "$M" -v >> "$OUT" 2>&1
RC=$?
echo "# exit status: $RC" >> "$OUT"
echo "exit $RC; receipt $OUT"
