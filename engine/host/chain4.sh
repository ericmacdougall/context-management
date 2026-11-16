#!/bin/bash
# Follow-ups at one commit: the steadier H3 integration case on the new and the old tree, and the pinned plus
# continue-as-new combination on server 1.32.2 with the demotion-signal setting off and on.
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
$P -m engine.it_cases --code new --cases h3 > ../logs/c4-it-new-h3.out 2>&1
$P -m engine.it_cases --code old --cases h3 > ../logs/c4-it-old-h3.out 2>&1
$P -m engine.exps5 f5b4 > ../logs/c4-f5b4.out 2>&1
$P -m engine.exps5 f5b4 --demotion on > ../logs/c4-f5b4-demotion.out 2>&1
touch ../run/chain4.done
