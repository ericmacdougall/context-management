#!/bin/sh
# Copy the committed policy/ tree to t2-dev ~/emlab-16096/polr/src (the RESCUE owner's own directory; no .git there) and write
# the stamp file on the host; refuses a dirty tree unless ALLOW_DIRTY=1. Never targets ~/emlab-16096/pol (previous owner).
# usage: sh sync.sh   (run from policy/)
set -e
cd "$(dirname "$0")"
DEST=${SYNC_DEST:-emlab-16096/polr/src}       # SYNC_DEST=emlab-16096/polr/runs/<name>/src: one fresh copy per run, never synced mid-run
case "$DEST" in emlab-16096/polr/*) ;; *) echo "SYNC_DEST must be under emlab-16096/polr/"; exit 1;; esac
ssh -F "$HOME/.ssh/config_fleetops" t2-dev "mkdir -p $DEST"
DIRTY=$(git status --porcelain -- . ':!results' | wc -l | tr -d ' ')
if [ "$DIRTY" != "0" ] && [ "$ALLOW_DIRTY" != "1" ]; then echo "dirty tree"; exit 1; fi
COMMIT=$(git rev-parse HEAD)
TC=$(git log -1 --format=%H -- THRESHOLDS.md)
TREE=$(git ls-files -s . | shasum -a 256 | cut -d' ' -f1)
rsync -a --delete --exclude results --exclude __pycache__ --exclude 'GIT_STAMP.json' -e "ssh -F $HOME/.ssh/config_fleetops" ./ "t2-dev:$DEST/"
printf '{"commit": "%s", "dirty": %s, "thresholds_commit": "%s", "tree_sha256": "%s"}\n' "$COMMIT" "$([ "$DIRTY" = 0 ] && echo false || echo true)" "$TC" "$TREE" \
  | ssh -F "$HOME/.ssh/config_fleetops" t2-dev "cat > $DEST/GIT_STAMP.json"
echo "synced $COMMIT dirty=$DIRTY to $DEST"
