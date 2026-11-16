#!/bin/bash
# Result-key control rerun after the Postgres restart run has finished (it must not overlap that restart).
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
for i in $(seq 1 720); do [ -e ../run/f4pg.done ] && break; sleep 5; done   # bounded: 1 h
$P -m engine.exps f4t --mode brokenresult > ../logs/c7-f4t-brokenresult.out 2>&1
touch ../run/chain7.done
