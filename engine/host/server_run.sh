#!/bin/bash
# Foreground Temporal dev server (start it under `kill_guard.py spawn --host t2-dev --`, so its pid is mine to stop).
# Usage: server_run.sh <cli-version> [db-file]. Binds 127.0.0.1 only. gRPC 48310, http 48320, metrics 48330.
H=${ENGINE_HOME:-$HOME/emlab-16096/engine}
V=$1; DB=${2:-$H/run/temporal.db}
echo 48310 > $H/run/temporal_port
cd $H/run
exec $H/bin/cli-$V/temporal server start-dev --ip 127.0.0.1 --port 48310 --http-port 48320 --metrics-port 48330 \
  --headless --db-filename $DB --log-level warn $EXTRA
