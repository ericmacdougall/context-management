#!/bin/sh
# Ship this package to the fleet host (WP_HOME/src) with the current commit id; run there with the pinned venv:
#   ssh -F ~/.ssh/config_fleetops t2-dev 'cd ~/emlab-16096/wp/src && ../venv/bin/python -m <module>'
set -e
cd "$(dirname "$0")"
rsync -a --delete --exclude results --exclude __pycache__ -e "ssh -F $HOME/.ssh/config_fleetops" ./ t2-dev:emlab-16096/wp/src/
H=$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- . || echo "-dirty")
ssh -F "$HOME/.ssh/config_fleetops" t2-dev "echo $H > ~/emlab-16096/wp/src/GIT_HEAD"
echo "synced $H"
