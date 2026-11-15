#!/bin/bash
# Long runs at one commit: F4a (measured drain, abrupt exits, abrupt server stop), F7 and its control, F8, F4d
# (history bound) and an overload run above the drain rate.
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
$P -m engine.exps f4a > ../logs/c3-f4a.out 2>&1
$P -m engine.exps8 f7 > ../logs/c3-f7.out 2>&1
$P -m engine.exps8 f7ctl > ../logs/c3-f7ctl.out 2>&1
$P -m engine.exps8 f8 > ../logs/c3-f8.out 2>&1
$P -m engine.exps f4d > ../logs/c3-f4d.out 2>&1
$P -m engine.exps f4d --turns 1500 --rate 12 > ../logs/c3-f4d-overload.out 2>&1
touch ../run/chain3.done
