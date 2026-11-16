#!/bin/sh
# Pin a run to a copied tree (never run from src, which sync.sh may overwrite mid-run).
# usage (on the fleet host, from WP_HOME/src): sh pin_run.sh  -> prints the pinned directory
set -e
cd "$(dirname "$0")"
D=../runs/$(cat GIT_HEAD)-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p ../runs
cp -a . "$D"
echo "$D"
