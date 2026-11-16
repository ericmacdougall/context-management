#!/bin/bash
# Postgres restart run ALONE (it restarts the shared database container), then the F8 drill and the
# continue-as-new pair above the drain rate (backlog at every boundary).
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
$P -m engine.exps f4pg > ../logs/c5-f4pg.out 2>&1
touch ../run/f4pg.done
$P -m engine.exps8 f8 > ../logs/c5-f8.out 2>&1
$P -m engine.exps f4d --turns 400 --rate 6 --can-len 400 > ../logs/c5-f4d-pair6-v1.out 2>&1
$P -m engine.exps f4d --naive --turns 400 --rate 6 --can-len 400 > ../logs/c5-f4d-pair6-naive.out 2>&1
touch ../run/chain5.done
