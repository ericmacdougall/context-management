#!/bin/bash
# N1 on the fixed tree (boundaries, a worker exit during the wait, one grant), then the real zombie run and its control.
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
$P -m engine.exps9 n1 --code new --to 7200 --poll 0.5 --poll-max 0.5 --minutes 20 > ../logs/c9b-n1-new.out 2>&1
$P -m engine.exps9 n6 --mode ok > ../logs/c9b-n6-ok.out 2>&1
$P -m engine.exps9 n6 --mode brokenkeys > ../logs/c9b-n6-broken.out 2>&1
touch ../run/chain9b.done
