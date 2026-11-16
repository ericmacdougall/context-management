#!/bin/bash
# Temporal on Postgres persistence (server containers, temporal-sql-tool schema path), from the code2 tree:
# F4a targeted with its broken-key control, F4c, F7 (schema update as the upgrade path) with the refused-upgrade
# controls, and F8 (engine discarded = its databases dropped).
cd ~/emlab-16096/engine/code2
export ENGINE_CODE_ROOT=$PWD ENGINE_TEMPORAL_BACKEND=pg
P=../venv-new/bin/python
$P -m engine.exps f4t --mode ok > ../logs/c10-f4t-ok.out 2>&1
$P -m engine.exps f4t --mode brokenkeys > ../logs/c10-f4t-broken.out 2>&1
$P -m engine.exps f4c > ../logs/c10-f4c.out 2>&1
$P -m engine.exps8 f7 > ../logs/c10-f7.out 2>&1
$P -m engine.exps8 f7pgctl > ../logs/c10-f7pgctl.out 2>&1
$P -m engine.exps8 f8 > ../logs/c10-f8.out 2>&1
touch ../run/chain10.done
