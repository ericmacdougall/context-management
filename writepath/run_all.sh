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
  $PY -m exp.cleanup exp.es_semantics
  $PY -m old_repro.n_old
  $PY -m exp.cleanup old_repro.n_old
  $PY -m exp.review_fix F3 F4 F5 F7 F1S F6 "$R"
  $PY -m exp.cleanup exp.review_fix
  $PY -m exp.review_fix F1
  $PY -m exp.cleanup exp.review_fix
  $PY -m exp.scope_hold M H 3
  $PY -m exp.cleanup exp.scope_hold
  $PY -m exp.n_new "$R"
  $PY -m exp.cleanup exp.n_new
  $PY -m exp.row1_zombie "$R"
  $PY -m exp.cleanup exp.row1_zombie
  $PY -m exp.row23_latecommit "$R"
  $PY -m exp.cleanup exp.row23_latecommit
  $PY -m exp.e_verify "$R"
  $PY -m exp.cleanup exp.e_verify
  $PY -m exp.registry_marker "$R"
  $PY -m exp.cleanup exp.registry_marker
  $PY -m exp.d1_hold_mentions 5
  $PY -m exp.cleanup exp.d1_hold_mentions
  $PY -m exp.d4_rescan_es 5
  $PY -m exp.cleanup exp.d4_rescan_es
  $PY -m exp.row19_secrets
  $PY -m exp.cleanup exp.row19_secrets
  $PY -m exp.row6_chain "$R"
  $PY -m exp.cleanup exp.row6_chain
  $PY -m exp.d4_store_mark 5
  $PY -m exp.cleanup exp.d4_store_mark
  $PY -m exp.phys_purge
  $PY -m exp.cleanup exp.phys_purge
  $PY -m exp.races R1 R2 R3 40
  $PY -m exp.cleanup exp.races
  $PY -m exp.guard_check end || echo "GATE guard_check end FAILED"
  echo "end $(date -u +%FT%TZ)"
} > "$OUT" 2>&1
echo "$OUT"
