#!/bin/bash
# Final reports and the runs that need the shared database to themselves: stamped unit reports (new tree and the 96a80b2
# export), the zombie run and its control with the measured criterion, then the real Postgres crash ALONE.
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
(cd ../code && $P -m engine.tests.run_report new-final engine.tests.test_review_fixes engine.tests.test_pgstore > ../logs/c12-unit-new.out 2>&1)
(cd ../prev && cp ../code/engine/tests/*.py engine/tests/ && rm -f engine/tests/test_pgstore.py engine/tests/test_keys.py && $P -m engine.tests.run_report prev-final engine.tests.test_review_fixes > ../logs/c12-unit-prev.out 2>&1)
$P -m engine.exps9 n6 --mode ok > ../logs/c12-n6-ok.out 2>&1
$P -m engine.exps9 n6 --mode brokenkeys > ../logs/c12-n6-broken.out 2>&1
$P -m engine.exps9 n5 > ../logs/c12-n5.out 2>&1
touch ../run/chain12.done
