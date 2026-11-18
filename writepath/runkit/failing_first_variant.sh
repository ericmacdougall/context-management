#!/bin/sh
# Failing-first receipt for a module that is NEW in this branch (review F, wp/secret_sink.py): failing_first.sh needs
# the file at the earlier commit, so this runs the CURRENT tests inside the writepath tree of commit C either without
# the module ("-") or against a broken VARIANT copied from this tree (a stated, minimal change of the module), and
# saves the receipt. Like failing_first.sh it runs in a temporary tree, so the receipt holds no local path, and the
# tests generate fake values and print names only. Run from writepath/.
# usage: sh runkit/failing_first_variant.sh <commit> <variant path | -> <ENV var naming the module> <tests> <out>
#   e.g. sh runkit/failing_first_variant.sh e21b99b results/failing-first/variants/secret_sink.broken-a-no-views.r2.py \
#          SECRET_SINK_UNDER_TEST tests.test_secret_sink results/failing-first/<name>.red.out
set -u
C=$1; V=$2; E=$3; M=$4; OUT=$5
[ -e "$OUT" ] && { echo "refusing: $OUT exists (a receipt is never overwritten)"; exit 2; }
CUR=$(pwd)
case $OUT in /*) ;; *) OUT="$CUR/$OUT" ;; esac
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
git -C "$(git rev-parse --show-toplevel)" archive "$C" writepath | tar -x -C "$W" --exclude 'writepath/tests' ||
  { echo "cannot extract $C:writepath"; exit 2; }
cp -R "$CUR/tests" "$W/writepath/tests" || { echo "cannot copy the current tests"; exit 2; }
TF="tests/$(echo "${M%% *}" | sed 's/^tests\.//; s/\..*//').py"
{
  echo "# failing-first receipt (new module)"
  echo "# date: $(date -u +%FT%TZ)"
  echo "# tree: writepath at $C (git archive); the current tests/ copied into it; run from it"
  if [ "$V" = "-" ]; then
    echo "# module under test: none ($E unset; the module is absent at $C)"
  else
    mkdir -p "$W/writepath/$(dirname "$V")" && cp "$CUR/$V" "$W/writepath/$V" || { echo "cannot copy $V"; exit 2; }
    echo "# module under test: $V sha256 $(shasum -a 256 "$CUR/$V" | cut -c1-64), copied into the tree"
    echo "# its difference from wp/secret_sink.py at HEAD $(git rev-parse --short HEAD):"
    diff "$CUR/wp/secret_sink.py" "$CUR/$V" | sed 's/^/# diff: /'
  fi
  echo "# test file: $TF from HEAD $(git rev-parse --short HEAD) blob $(git hash-object "$TF")"
  if [ "$V" = "-" ]; then echo "# command: (cd <tree>/writepath && python3 -m unittest -v $M)"
  else echo "# command: (cd <tree>/writepath && $E=<tree>/writepath/$V python3 -m unittest -v $M)"; fi
} >> "$OUT"
if [ "$V" = "-" ]; then
  (cd "$W/writepath" && env -u "$E" python3 -m unittest -v $M) >> "$OUT" 2>&1
else
  (cd "$W/writepath" && env "$E=$W/writepath/$V" python3 -m unittest -v $M) >> "$OUT" 2>&1
fi
RC=$?
echo "# exit status: $RC" >> "$OUT"
echo "exit $RC; receipt $OUT"
