#!/bin/bash
# F4 reruns at one commit: targeted windows with controls, host move (and zombie old worker), Postgres restart,
# long waits, and the continue-as-new pair (passing and naive at the same rate and threshold).
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
for m in ok brokenkeys brokenresult; do $P -m engine.exps f4t --mode $m > ../logs/c2-f4t-$m.out 2>&1; done
$P -m engine.exps f4b > ../logs/c2-f4b.out 2>&1
$P -m engine.exps f4b --zombie > ../logs/c2-f4bz.out 2>&1
$P -m engine.exps f4pg > ../logs/c2-f4pg.out 2>&1
$P -m engine.exps f4c > ../logs/c2-f4c.out 2>&1
$P -m engine.exps f4d --turns 400 --rate 3.3 --can-len 400 > ../logs/c2-f4d-pair-v1.out 2>&1
$P -m engine.exps f4d --naive --turns 400 --rate 3.3 --can-len 400 > ../logs/c2-f4d-pair-naive.out 2>&1
touch ../run/chain2.done
