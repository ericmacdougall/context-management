#!/bin/sh
# Ship this package to the fleet host (WP_HOME/src) as the files of the current COMMIT, never the working tree
# (GATE-DESIGN 8.2 P4): runkit/tree_manifest.py writes the commit's files (without results/) from git objects, with
# TREE.manifest and GIT_HEAD; the gate refuses a pinned tree that differs from that manifest. Uncommitted changes are
# not shipped (a note says so). WP_HOME on the host is this stream's own home, ~/emlab2-16096/wphard/wp. Run there
# with the pinned venv and WP_HOME set (wp/config.py refuses to start without it):
#   ssh -F ~/.ssh/config_fleetops t2-dev 'cd ~/emlab2-16096/wphard/wp/src && WP_HOME=$HOME/emlab2-16096/wphard/wp ../venv/bin/python -B -m <module>'
# Never run this while a run is in flight from src; pin each run to a copied tree (pin_run.sh).
# WP_REMOTE_HOME (a path under the host user's home, letters, digits, '/', '_', '.', '-') ships to another home of
# this stream instead, e.g. a gate check's own home; the default is the stream's home above.
set -e
cd "$(dirname "$0")"
REMOTE_HOME=${WP_REMOTE_HOME:-emlab2-16096/wphard/wp}
case "$REMOTE_HOME" in
  /* | *..* | *[!A-Za-z0-9/_.-]* | '') echo "sync refused: WP_REMOTE_HOME must be a relative path of letters, digits, '/', '_', '.', '-'"; exit 2 ;;
esac
C=$(git rev-parse HEAD)
git diff --quiet HEAD -- . && git diff --cached --quiet -- . || echo "note: uncommitted changes in writepath/ are not shipped; commit $C is"
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
python3 runkit/tree_manifest.py ship "$C" "$W/tree.tar"
mkdir "$W/src"
tar -x -C "$W/src" -f "$W/tree.tar"
# --delete removes files the commit does not hold (and any bytecode cache); the host's src/results is kept
rsync -a --delete --exclude /results -e "ssh -F $HOME/.ssh/config_fleetops" "$W/src/" "t2-dev:$REMOTE_HOME/src/"
echo "synced $(cat "$W/src/GIT_HEAD")"
