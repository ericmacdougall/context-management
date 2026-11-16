#!/bin/bash
# N1b: the approval backoff at its real default (10 s doubling to 900 s) across several boundaries, then a grant whose
# doorbell is dropped (Postgres only), measured right after a poll at the cap.
cd ~/emlab-16096/engine/code
../venv-new/bin/python -m engine.exps9 n1b --to 7200 --can-len 30 --minutes 75 > ../logs/c15-n1b.out 2>&1
touch ../run/chain15.done
