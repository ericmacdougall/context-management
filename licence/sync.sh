#!/usr/bin/env bash
# push licence/ and memcore/ to t2-dev ~/emlab-16096/lic/code (small; no secrets)
set -e
R=$(cd "$(dirname "$0")/.." && pwd)
ssh -F ~/.ssh/config_fleetops t2-dev 'mkdir -p ~/emlab-16096/lic/code'
rsync -a --delete -e "ssh -F $HOME/.ssh/config_fleetops" --exclude results --exclude __pycache__ "$R/licence" "$R/memcore" t2-dev:emlab-16096/lic/code/
