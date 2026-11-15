#!/bin/bash
# Temporal dev server control on t2-dev. Usage: server.sh start <cli-version> | stop | status
# Binds 127.0.0.1 only. State in run/temporal.db (SQLite, shared by both CLI versions for the upgrade test).
set -u
H=${ENGINE_HOME:-$HOME/emlab-16096/engine}; R=$H/run
GRPC=48233; HTTP=48234; MET=48235
case "$1" in
 start)
  V=$2; DB=${3:-$R/temporal.db}
  [ -f $R/server.pid ] && kill -0 $(cat $R/server.pid) 2>/dev/null && { echo already running; exit 1; }
  echo $GRPC > $R/temporal_port
  cd $R && setsid nohup $H/bin/cli-$V/temporal server start-dev --ip 127.0.0.1 --port $GRPC --http-port $HTTP \
     --metrics-port $MET --headless --db-filename $DB --log-level warn \
     ${EXTRA:-} > $H/logs/server-$V-$(date +%H%M%S).log 2>&1 < /dev/null &
  echo $! > $R/server.pid; echo started pid $(cat $R/server.pid) cli $V ;;
 stop)  # SIGKILL when KILL=1 (crash), else SIGTERM
  [ -f $R/server.pid ] && { kill ${KILL:+-9} $(cat $R/server.pid) 2>/dev/null; sleep 1; rm -f $R/server.pid; echo stopped; } ;;
 status) [ -f $R/server.pid ] && kill -0 $(cat $R/server.pid) 2>/dev/null && echo up pid $(cat $R/server.pid) || echo down ;;
esac
