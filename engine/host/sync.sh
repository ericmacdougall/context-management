#!/bin/bash
# rsync the engine package to the host (code only) and stamp it with the git commit it was built from.
# Usage: sync.sh <worktree>. Every experiment result records this commit and whether the tree was dirty.
set -e
W="$1"
DIRTY=false
[ -n "$(git -C "$W" status --porcelain -- engine ':!engine/GIT_COMMIT' ':!engine/results')" ] && DIRTY=true
printf '{"commit": "%s", "dirty": %s}\n' "$(git -C "$W" rev-parse HEAD)" "$DIRTY" > "$W/engine/GIT_COMMIT"
rsync -a --delete -e "ssh -F $HOME/.ssh/config_fleetops" --exclude results --exclude __pycache__ "$W"/engine/ t2-dev:emlab-16096/engine/code/engine/
