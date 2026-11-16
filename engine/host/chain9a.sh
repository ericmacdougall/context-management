#!/bin/bash
# N1 reproduction on the 96a80b2 tree: a long approval wait with a short poll (observed up to 50 minutes).
cd ~/emlab-16096/engine/code
../venv-new/bin/python -m engine.exps9 n1 --code prev --to 7200 --poll 0.5 --poll-max 0.5 --minutes 50 > ../logs/c9a-n1-prev.out 2>&1
touch ../run/chain9a.done
