#!/bin/bash
# The real Postgres crash run ALONE, then the approval-resume integration case on the 96a80b2 tree and the new tree
# (the first 96a80b2 run used the wrong grant shape for that tree).
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
$P -m engine.exps9 n5 > ../logs/c13-n5.out 2>&1
$P -m engine.it_cases --code prev --cases m2 > ../logs/c13-it-prev-m2.out 2>&1
$P -m engine.it_cases --code new --cases m2 > ../logs/c13-it-new-m2.out 2>&1
touch ../run/chain13.done
