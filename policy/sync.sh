#!/bin/sh
# Copy the committed policy/ tree to t2-dev ~/emlab2-16096/policy/runs/<name>/src (the policy owner's own directory under coordinator
# 500523cf; no .git there) and write the stamp file on the host; refuses a dirty tree unless ALLOW_DIRTY=1.
# One fresh copy per run: the destination must not exist yet, so a tree is never synced into a run that is using it.
# Never targets ~/emlab-16096 (earlier owners' trees, read only).
# usage: SYNC_DEST=emlab2-16096/policy/runs/<name>/src sh sync.sh   (run from policy/)
set -e
cd "$(dirname "$0")"
DEST=${SYNC_DEST:?SYNC_DEST=emlab2-16096/policy/runs/<name>/src is required}
case "$DEST" in
  *..*) echo "SYNC_DEST must not contain .."; exit 1;;
  emlab2-16096/policy/runs/?*/src) ;;
  *) echo "SYNC_DEST must be emlab2-16096/policy/runs/<name>/src"; exit 1;;
esac
DIRTY=$(git status --porcelain -- . ':!results' | wc -l | tr -d ' ')
if [ "$DIRTY" != "0" ] && [ "$ALLOW_DIRTY" != "1" ]; then echo "dirty tree"; exit 1; fi
ssh -F "$HOME/.ssh/config_fleetops" t2-dev "test ! -e $DEST" || { echo "SYNC_DEST already exists on the host (one fresh copy per run)"; exit 1; }
ssh -F "$HOME/.ssh/config_fleetops" t2-dev "mkdir -p $DEST"
COMMIT=$(git rev-parse HEAD)
TC=$(git log -1 --format=%H -- THRESHOLDS.md)
TREE=$(git ls-files -s . | shasum -a 256 | cut -d' ' -f1)
rsync -a --delete --exclude results --exclude __pycache__ --exclude 'GIT_STAMP.json' -e "ssh -F $HOME/.ssh/config_fleetops" ./ "t2-dev:$DEST/"
# the stamp is built locally by python (no shell redirect) and copied as one file
STAMP=$(mktemp -d)/GIT_STAMP.json
python3 -c 'import json,sys; json.dump({"commit": sys.argv[2], "dirty": sys.argv[3] != "0", "thresholds_commit": sys.argv[4], "tree_sha256": sys.argv[5]}, open(sys.argv[1], "w")); open(sys.argv[1], "a").write("\n")' \
  "$STAMP" "$COMMIT" "$DIRTY" "$TC" "$TREE"
rsync -a -e "ssh -F $HOME/.ssh/config_fleetops" "$STAMP" "t2-dev:$DEST/GIT_STAMP.json"
echo "synced $COMMIT dirty=$DIRTY to $DEST"
