#!/bin/bash
# Confirm-round evidence from one commit: unit reports on the new tree, the 96a80b2 export and the 0392f81 export; the
# poison case C1 on the new tree; the deadline-edge race on the 0392f81 export and on the new tree.
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
(cd ../code && $P -m engine.tests.run_report new-c $(echo engine.tests.test_review_fixes engine.tests.test_pgstore) > ../logs/c14-unit-new.out 2>&1)
for t in prev prev2; do (cd ../$t && cp ../code/engine/tests/*.py engine/tests/ && rm -f engine/tests/test_pgstore.py engine/tests/test_keys.py && $P -m engine.tests.run_report $t-c engine.tests.test_review_fixes > ../logs/c14-unit-$t.out 2>&1); done
$P -m engine.it_cases --code new --cases c1 > ../logs/c14-it-c1-new.out 2>&1
(cd ../prev2 && $P -m engine.race prev2 1500 > ../logs/c14-race-prev2.out 2>&1)
(cd ../code && $P -m engine.race new 1500 > ../logs/c14-race-new.out 2>&1)
touch ../run/chain14.done
