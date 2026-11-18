#!/bin/sh
# Pin a run to a copied tree (never run from src, which sync.sh may overwrite mid-run). The copy leaves out results/
# and every bytecode cache: the gate runs only from a tree whose files equal its TREE.manifest (GATE-DESIGN 8.2 P4).
# usage (on the fleet host, from WP_HOME/src): sh pin_run.sh  -> prints the pinned directory
set -e
cd "$(dirname "$0")"
D=../runs/$(cat GIT_HEAD)-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p ../runs
mkdir "$D"
tar -c -f - --exclude ./results --exclude __pycache__ --exclude '*.pyc' . | tar -x -C "$D" -f -
echo "$D"
