#!/bin/bash
# Temporal on Postgres: the upgrade that really changes the schema (server 1.30.9 to 1.31.5, schema 1.18 to 1.19 and
# visibility 1.13 to 1.14) under load plus the SDK bump, and the refused-upgrade controls on that pair.
cd ~/emlab-16096/engine/code2
export ENGINE_CODE_ROOT=$PWD ENGINE_TEMPORAL_BACKEND=pg
P=../venv-new/bin/python
$P -m engine.exps8 f7 --pg-old 1.30.9 --pg-new 1.31.5 > ../logs/c11-f7.out 2>&1
$P -m engine.exps8 f7pgctl --pg-old 1.30.9 --pg-new 1.31.5 > ../logs/c11-f7pgctl.out 2>&1
touch ../run/chain11.done
