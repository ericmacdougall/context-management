#!/bin/sh
# Full proof run on the fleet host (from WP_HOME/src). One reported run per experiment; no partial reruns.
# Output: WP_HOME/results/<name>.<sha16>.json (content-addressed) and WP_HOME/results/run_all.<head>.out
set -u
cd "$(dirname "$0")"
PY=../venv/bin/python
R=${1:-10}
OUT=../results/run_all.$(cat GIT_HEAD).out
{
  echo "start $(date -u +%FT%TZ) head $(cat GIT_HEAD) R=$R"
  $PY -m exp.cleanup
  $PY -m wp.setup --reset
  $PY -m exp.guard_check start --controls || echo "GATE guard_check start FAILED"
  $PY -m exp.static_check
  $PY -m exp.es_semantics | tail -3
  $PY -m old_repro.n_old
  $PY -m exp.review_fix F3 F4 F5 F7 F1S F6 "$R"
  $PY -m exp.review_fix F1
  $PY -m exp.scope_hold M H 3
  $PY -m exp.n_new "$R"
  $PY -m exp.row1_zombie "$R"
  $PY -m exp.row23_latecommit "$R"
  $PY -m exp.e_verify "$R"
  $PY -m exp.registry_marker "$R"
  $PY -m exp.races R1 R2 R3 40
  $PY -m exp.guard_check end || echo "GATE guard_check end FAILED"
  echo "end $(date -u +%FT%TZ)"
} > "$OUT" 2>&1
echo "$OUT"
