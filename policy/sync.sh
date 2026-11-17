#!/bin/sh
# Copy the committed policy/ tree to t2-dev ~/emlab2-16096/policy/runs/<name>/src (the policy owner's own directory under coordinator
# 500523cf; no .git there) and write the stamp file on the host; refuses a dirty tree unless ALLOW_DIRTY=1.
# usage: SYNC_RUN=<name> sh sync.sh
#   <name> is ONE path component: a letter or digit, then up to 63 of [A-Za-z0-9._-], and no "..". The destination is built from it here;
#   no caller-supplied path reaches ssh or rsync (review-3 N2: the earlier SYNC_DEST path guard let shell metacharacters through to the
#   remote command). SYNC_DEST is no longer read.
# On the host: every ancestor (emlab2-16096, emlab2-16096/policy, emlab2-16096/policy/runs) must be a real directory, not a symlink, and
# runs/<name> must not exist yet (created with one plain mkdir, so a tree is never synced into a run that exists or is in use).
# Never targets ~/emlab-16096 (earlier owners' trees, read only). tests/test_sync_guard.py checks this with ssh and rsync stubbed.
set -e
LC_ALL=C
export LC_ALL
cd "$(dirname "$0")"
NAME=${SYNC_RUN-}
refuse() { echo "sync.sh: SYNC_RUN must be one name of [A-Za-z0-9._-], starting with a letter or digit, at most 64 characters, no '..'" >&2; exit 1; }
case "$NAME" in
  ""|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*|*..*) refuse;;
esac
[ "${#NAME}" -le 64 ] || refuse
RUNS=emlab2-16096/policy/runs
DEST="$RUNS/$NAME/src"
DIRTY=$(git status --porcelain -- . ':!results' | wc -l | tr -d ' ')
if [ "$DIRTY" != "0" ] && [ "$ALLOW_DIRTY" != "1" ]; then echo "dirty tree"; exit 1; fi
SSHCFG="$HOME/.ssh/config_fleetops"
# The remote command is a fixed text; the only variable part is the validated name, inside single quotes.
ssh -F "$SSHCFG" t2-dev "for d in emlab2-16096 emlab2-16096/policy $RUNS; do if [ -L \"\$d\" ] || [ ! -d \"\$d\" ]; then echo \"not a plain directory: \$d\" >&2; exit 3; fi; done; mkdir '$RUNS/$NAME' && mkdir '$DEST'" \
  || { echo "sync.sh: host refused (runs/$NAME exists, or an ancestor is missing or a symlink)" >&2; exit 1; }
COMMIT=$(git rev-parse HEAD)
TC=$(git log -1 --format=%H -- THRESHOLDS.md)
TREE=$(git ls-files -s . | shasum -a 256 | cut -d' ' -f1)
rsync -a --delete --exclude results --exclude __pycache__ --exclude 'GIT_STAMP.json' -e "ssh -F $SSHCFG" ./ "t2-dev:$DEST/"
# the stamp is built locally by python (no shell redirect) and copied as one file
STAMP=$(mktemp -d)/GIT_STAMP.json
python3 -c 'import json,sys; json.dump({"commit": sys.argv[2], "dirty": sys.argv[3] != "0", "thresholds_commit": sys.argv[4], "tree_sha256": sys.argv[5]}, open(sys.argv[1], "w")); open(sys.argv[1], "a").write("\n")' \
  "$STAMP" "$COMMIT" "$DIRTY" "$TC" "$TREE"
rsync -a -e "ssh -F $SSHCFG" "$STAMP" "t2-dev:$DEST/GIT_STAMP.json"
echo "synced $COMMIT dirty=$DIRTY to $DEST"
