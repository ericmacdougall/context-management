#!/bin/bash
# Temporal dev server with a stop-file mechanism (no external signal needed). Start it under
# `kill_guard.py spawn --host t2-dev --`. Usage: serverloop.sh CLI_VERSION DB_FILE STOPFILE
# Appearance of STOPFILE ends the server gracefully; STOPFILE.hard ends it abruptly (crash test).
# gRPC 48310, http 48320, metrics 48330, 127.0.0.1 only.
H=${ENGINE_HOME:-$HOME/emlab-16096/engine}
V=$1; DB=$2; STOP=$3
TP=${TPORT:-48310}
[ -z "$TPORT" ] && echo $TP > $H/run/temporal_port
cd $H/run
$H/bin/cli-$V/temporal server start-dev --ip 127.0.0.1 --port $TP --http-port $((TP+10)) --metrics-port $((TP+20)) \
  --headless --db-filename $DB --log-level warn $EXTRA &
SP=$!
while [ ! -e $STOP ] && [ ! -e $STOP.hard ] && kill -0 $SP 2>/dev/null; do sleep 0.3; done
if [ -e $STOP.hard ]; then kill -KILL $SP 2>/dev/null; else kill -TERM $SP 2>/dev/null; fi
wait $SP
