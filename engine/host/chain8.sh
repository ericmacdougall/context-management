#!/bin/bash
# Reruns whose result files were overwritten by later runs of the same experiment (results are now named per run):
# usage: chain8.sh long | pair33 | pair6
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
case "$1" in
  long)   $P -m engine.exps f4d > ../logs/c8-f4d-long.out 2>&1 ;;
  pair33) $P -m engine.exps f4d --turns 400 --rate 3.3 --can-len 400 > ../logs/c8-f4d-pair33-v1.out 2>&1
          $P -m engine.exps f4d --naive --turns 400 --rate 3.3 --can-len 400 > ../logs/c8-f4d-pair33-naive.out 2>&1 ;;
  pair6)  $P -m engine.exps f4d --turns 400 --rate 6 --can-len 400 > ../logs/c8-f4d-pair6-v1.out 2>&1 ;;
esac
touch ../run/chain8-$1.done
