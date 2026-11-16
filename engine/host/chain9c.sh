#!/bin/bash
# Unit reports and integration cases on the new tree and on the 96a80b2 tree, then the overload run (L4).
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
(cd ../code && $P -m engine.tests.run_report new-delta engine.tests.test_review_fixes engine.tests.test_pgstore > ../logs/c9c-unit-new.out 2>&1)
(cd ../prev && cp ../code/engine/tests/*.py engine/tests/ && rm -f engine/tests/test_pgstore.py engine/tests/test_keys.py && $P -m engine.tests.run_report prev-delta engine.tests.test_review_fixes > ../logs/c9c-unit-prev.out 2>&1)
$P -m engine.it_cases --code new > ../logs/c9c-it-new.out 2>&1
$P -m engine.it_cases --code prev --cases n2,m1,m2,l1,l2,l3,m4,h3 > ../logs/c9c-it-prev.out 2>&1
$P -m engine.exps f4d --turns 1500 --rate 12 > ../logs/c9c-f4d-overload.out 2>&1
touch ../run/chain9c.done
