#!/bin/sh
# Copy the committed policy/ tree to t2-dev (no .git there) and write the stamp file on the host; refuses a dirty tree unless ALLOW_DIRTY=1.
# usage: sh sync.sh   (run from policy/)
set -e
cd "$(dirname "$0")"
DIRTY=$(git status --porcelain -- . ':!results' | wc -l | tr -d ' ')
if [ "$DIRTY" != "0" ] && [ "$ALLOW_DIRTY" != "1" ]; then echo "dirty tree"; exit 1; fi
COMMIT=$(git rev-parse HEAD)
TC=$(git log -1 --format=%H -- THRESHOLDS.md)
TREE=$(git ls-files -s . | shasum -a 256 | cut -d' ' -f1)
rsync -a --delete --exclude results --exclude __pycache__ --exclude 'GIT_STAMP.json' -e "ssh -F $HOME/.ssh/config_fleetops" ./ t2-dev:emlab-16096/pol/src/
printf '{"commit": "%s", "dirty": %s, "thresholds_commit": "%s", "tree_sha256": "%s"}\n' "$COMMIT" "$([ "$DIRTY" = 0 ] && echo false || echo true)" "$TC" "$TREE" \
  | ssh -F "$HOME/.ssh/config_fleetops" t2-dev 'cat > emlab-16096/pol/src/GIT_STAMP.json'
echo "synced $COMMIT dirty=$DIRTY"
