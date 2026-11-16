#!/bin/bash
# Long continue-as-new runs (after the Postgres restart run has finished): the 5,400-turn history-bound run and an
# overload run above the drain rate. Started after chain5's Postgres restart; see the order in the report.
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
while [ ! -e ../run/f4pg.done ]; do sleep 5; done
$P -m engine.exps f4d > ../logs/c6-f4d.out 2>&1
$P -m engine.exps f4d --turns 1500 --rate 12 > ../logs/c6-f4d-overload.out 2>&1
touch ../run/chain6.done
