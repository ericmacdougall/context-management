#!/bin/bash
# Review reruns at one commit: unit reports (new and old tree), integration cases (new and old tree), F5 and F6.
cd ~/emlab-16096/engine/code
P=../venv-new/bin/python
(cd ../code && $P -m engine.tests.run_report new engine.tests.test_review_fixes engine.tests.test_pgstore > ../logs/c1-unit-new.out 2>&1)
(cd ../old && $P -m engine.tests.run_report old engine.tests.test_review_fixes > ../logs/c1-unit-old.out 2>&1)
$P -m engine.it_cases --code new > ../logs/c1-it-new.out 2>&1
$P -m engine.it_cases --code old > ../logs/c1-it-old.out 2>&1
for e in f5a f5b1 f5b2 f5b3 f5c f6; do $P -m engine.exps5 $e > ../logs/c1-$e.out 2>&1; done
$P -m engine.exps5 f6 --broken > ../logs/c1-f6x.out 2>&1
touch ../run/chain1.done
