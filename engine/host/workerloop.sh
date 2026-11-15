#!/bin/bash
# Worker wrapper for crash tests: restarts the worker until the stop file appears. While crashes remain, the worker
# ends itself abruptly (os._exit(137): no cleanup, no flush, sockets dropped by the kernel) after a random 2-22 s.
# Start it under `kill_guard.py spawn --host t2-dev --`. Usage:
#   workerloop.sh VENV VARIANT IDENTITY TASKQUEUE CODE_DIR CRASHES SEED STOPFILE LOGPREFIX [worker args...]
VENV=$1; VAR=$2; ID=$3; TQ=$4; CODE=$5; CRASHES=$6; SEED=$7; STOP=$8; LOGP=$9; shift 9
H=${ENGINE_HOME:-$HOME/emlab-16096/engine}
RANDOM=$SEED; n=0; export ENGINE_CRASH_LOG=$LOGP.crashes
cd $CODE
while [ ! -e $STOP ]; do
  CA=0; [ $n -lt $CRASHES ] && CA=$((2 + RANDOM % ${CMAX:-21}))
  echo "$(date +%s.%N) start id=$ID crashes_so_far=$n crash_after=$CA cwd=$CODE" >> $LOGP.starts
  if [ $CA -gt 0 ]; then
    $H/$VENV/bin/python -m engine.worker --variant $VAR --identity $ID --task-queue $TQ --stop-file $STOP --crash-after $CA "$@" >> $LOGP.log 2>&1 < /dev/null
  else
    $H/$VENV/bin/python -m engine.worker --variant $VAR --identity $ID --task-queue $TQ --stop-file $STOP "$@" >> $LOGP.log 2>&1 < /dev/null
  fi
  rc=$?
  echo "$(date +%s.%N) exit rc=$rc" >> $LOGP.starts
  [ $rc -eq 137 ] && n=$((n+1))
  [ -e $STOP ] || sleep 0.$((3 + RANDOM % 17))
done
