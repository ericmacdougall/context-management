#!/bin/bash
# C3 on Postgres persistence: the deploy guard refuses the older image; today's unguarded start (control) and the
# snapshot-restore rollback are recorded in the same run.
cd ~/emlab-16096/engine/code2
export ENGINE_CODE_ROOT=$PWD ENGINE_TEMPORAL_BACKEND=pg
../venv-new/bin/python -m engine.exps8 f7pgctl --pg-old 1.30.9 --pg-new 1.31.5 > ../logs/c16-f7pgctl.out 2>&1
touch ../run/chain16.done
