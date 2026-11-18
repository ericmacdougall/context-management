#!/bin/sh
# Pin a run to a copied tree (never run from src, which sync.sh may overwrite mid-run). The copy leaves out results/
# and every bytecode cache: the gate runs only from a tree whose files equal its TREE.manifest (GATE-DESIGN 8.2 P4).
# The archive is written to a file first and each tar's own status is checked (review aacf3c0 m-J: in a pipeline only
# the last command's status counts, so a failed archive left a partial tree and exit 0). On any failure the partial
# copy is removed and the script exits non-zero without printing a directory.
# usage (on the fleet host, from WP_HOME/src): sh pin_run.sh  -> prints the pinned directory
set -eu
cd "$(dirname "$0")"
D=../runs/$(cat GIT_HEAD)-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p ../runs
mkdir "$D"
A=../runs/.pin-$$.tar
fail() { rm -rf "$D" "$A"; echo "pin_run: refused: $1; nothing was pinned" >&2; exit 2; }
tar -c -f "$A" --exclude ./results --exclude __pycache__ --exclude '*.pyc' . || fail "the archive of src failed"
tar -x -C "$D" -f "$A" || fail "the extraction into $D failed"
rm -f "$A"
echo "$D"
