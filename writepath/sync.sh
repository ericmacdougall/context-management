#!/bin/sh
# Ship this package to the fleet host (WP_HOME/src) with the current commit id. WP_HOME on the host is this stream's
# own home, ~/emlab2-16096/writepath/wp (never ~/emlab-16096, the previous lab's read-only reference). Run there with
# the pinned venv and WP_HOME set (wp/config.py refuses to start without it):
#   ssh -F ~/.ssh/config_fleetops t2-dev 'cd ~/emlab2-16096/writepath/wp/src && WP_HOME=$HOME/emlab2-16096/writepath/wp ../venv/bin/python -m <module>'
# Never run this while a run is in flight from src; pin each run to a copied tree (pin_run.sh).
set -e
cd "$(dirname "$0")"
REMOTE_HOME=emlab2-16096/writepath/wp
rsync -a --delete --exclude results --exclude __pycache__ -e "ssh -F $HOME/.ssh/config_fleetops" ./ "t2-dev:$REMOTE_HOME/src/"
H=$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- . && git diff --cached --quiet -- . || echo "-dirty")
ssh -F "$HOME/.ssh/config_fleetops" t2-dev "echo $H > ~/$REMOTE_HOME/src/GIT_HEAD"
echo "synced $H"
