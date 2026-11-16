#!/bin/bash
# N3: idle cost of 200 sessions on the 96a80b2 tree (fixed 20 s reconcile) and on the new tree (backing off).
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
$P -m engine.exps9 n3 --code prev --sessions 200 --minutes 10 > ../logs/c9d-n3-prev.out 2>&1
$P -m engine.exps9 n3 --code new --sessions 200 --minutes 25 > ../logs/c9d-n3-new.out 2>&1
touch ../run/chain9d.done
