#!/bin/bash
# rsync the engine package to the host (code only)
rsync -a --delete -e "ssh -F $HOME/.ssh/config_fleetops" --exclude results --exclude __pycache__ "$1"/engine/ t2-dev:emlab-16096/engine/code/engine/
