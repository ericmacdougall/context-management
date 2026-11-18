#!/bin/sh
# Failing-first receipt (review A A6; review D F10): run the CURRENT focused tests against an EARLIER commit's whole
# writepath tree, importing nothing from the current tree, and save the receipt: the tested blob's hash, the tree's
# commit, the test file's hash, the exact command, every assertion trace, every loaded writepath module with its path
# and SHA-256, and the exit status. Exit 4 (in the receipt) if any writepath module was loaded from outside the
# extracted tree. Run from writepath/. Never prints a secret: the tests generate fake values and print names only.
# usage: sh runkit/failing_first.sh <commit> <path in repo of the file under test> <ENV var naming the file> <tests> <out>
#   e.g. sh runkit/failing_first.sh eeb9b82 writepath/exp/disk_gate.py DISK_GATE_UNDER_TEST tests.test_disk_gate \
#          results/failing-first/<name>.red.out
set -u
C=$1; P=$2; V=$3; M=$4; OUT=$5
[ -e "$OUT" ] && { echo "refusing: $OUT exists (a receipt is never overwritten)"; exit 2; }
CUR=$(pwd)
case $OUT in /*) ;; *) OUT="$CUR/$OUT" ;; esac   # the runner writes from inside the extracted tree
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
# The WHOLE writepath tree at C: the gate spawns helpers as `-m exp...` from its own tree, and imports wp from it.
# Its tests/ is left out: the current tests take its place (below).
git -C "$(git rev-parse --show-toplevel)" archive "$C" writepath | tar -x -C "$W" --exclude 'writepath/tests' ||
  { echo "cannot extract $C:writepath"; exit 2; }
F="$W/$P"
[ -f "$F" ] || { echo "cannot read $C:$P"; exit 2; }
TF="tests/$(echo "${M%% *}" | sed 's/^tests\.//; s/\..*//').py"   # the module of a dotted test name; M may list several
# The current tests replace the old tree's tests, so they run INSIDE the old tree (review D F10: before this, the tests
# put the current tree first on sys.path and the old gate imported current wp modules).
cp -R "$CUR/tests" "$W/writepath/tests" || { echo "cannot copy the current tests"; exit 2; }
{
  echo "# failing-first receipt"
  echo "# date: $(date -u +%FT%TZ)"
  echo "# file under test: $C:$P blob $(git hash-object "$F") sha256 $(shasum -a 256 "$F" 2>/dev/null | cut -c1-64 || sha256sum "$F" | cut -c1-64)"
  echo "# tree: writepath at $C (git archive); the current tests/ copied into it; run from it; nothing imported from the current tree"
  echo "# test file: $TF from HEAD $(git rev-parse --short HEAD) blob $(git hash-object "$TF")"
  echo "# command: (cd <tree>/writepath && $V=<that file> python3 - <tests> < runner) with tests: $M"
} >> "$OUT"
(cd "$W/writepath" && env "$V=$F" PYTHONPATH="$W/writepath" FF_TREE="$W/writepath" python3 - "$M" >> "$OUT" 2>&1 <<'PY'
import hashlib
import os
import sys
import unittest

tree = os.path.realpath(os.environ["FF_TREE"])
result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(
    unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1].split()))
sys.stdout.flush()
bad = []
print("# loaded writepath modules (path in the extracted tree, sha256 prefix):")
for name, m in sorted(sys.modules.items()):
    f = getattr(m, "__file__", None)
    if not f or not os.path.isfile(f):     # __main__ is "<stdin>" here
        continue
    rp = os.path.realpath(f)
    if rp.startswith(tree + os.sep):
        with open(rp, "rb") as fh:
            print(f"#   {name} {os.path.relpath(rp, tree)} {hashlib.sha256(fh.read()).hexdigest()[:16]}")
    elif "/writepath/" in rp:
        bad.append(f"{name} {rp}")
for b in bad:
    print(f"# PROVENANCE FAIL: {b} was loaded from outside the extracted tree")
sys.exit(4 if bad else (0 if result.wasSuccessful() else 1))
PY
)
RC=$?
echo "# exit status: $RC" >> "$OUT"
echo "exit $RC; receipt $OUT"
