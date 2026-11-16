#!/bin/sh
# Proof runs. Mac-light experiments (E1-E5, B2) and B1 (multi-process, heavy) and B3 (needs the Postgres container) all run on t2-dev
# from a synced tree: `sh sync.sh` then, on the host in ~/emlab-16096/pol/src: sh run_all.sh
# Each experiment writes one stamped result file under results/ (no partial reruns combined).
set -e
PY=${PY:-../venv/bin/python}
for m in e1_lattice e2_egress e3_clash e4_tighten e5_threads e6_policy_format b2_rebuild b3_pg b1_modelcheck; do
  echo "== $m"; $PY -W ignore -m exp.$m
done
