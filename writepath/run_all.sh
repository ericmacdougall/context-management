#!/bin/sh
# Full proof run on the fleet host, from a PINNED tree (pin_run.sh prints it; never from src, which a sync may
# overwrite mid-run). One reported run per experiment; no partial reruns. WP_HOME is required (wp/config.py refuses
# to start without it and without this stream's conf marker).
# Output: WP_HOME/results/<name>.<sha16>.json (content-addressed) and WP_HOME/results/run_all.<head>.out
set -u
: "${WP_HOME:?WP_HOME must name the home of this stream}"
export WP_HOME
cd "$(dirname "$0")"
case "$(pwd)" in */runs/*) ;; *) echo "refusing: not a pinned tree ($(pwd)); run pin_run.sh first" >&2; exit 2 ;; esac
PY=$WP_HOME/venv/bin/python
R=${1:-10}
OUT=$WP_HOME/results/run_all.$(cat GIT_HEAD).out
# amendment 17c run validity: own usage may grow by at most 2 GiB (checked before every experiment), else VOID
gate() { $PY -m exp.disk_gate check "$1" || { echo "VOID: run stopped before $1 (disk gate)"; echo "end VOID $(date -u +%FT%TZ)"; exit 3; }; }
run() { gate "$*"; $PY "$@"; }
{
  echo "start $(date -u +%FT%TZ) head $(cat GIT_HEAD) R=$R"
  $PY -m exp.disk_gate start || { echo "end BLOCKED $(date -u +%FT%TZ)"; exit 2; }
  run -m exp.cleanup
  run -m wp.setup --reset
  $PY -m exp.disk_gate preflight || { echo "end BLOCKED $(date -u +%FT%TZ)"; exit 2; }
  run -m exp.guard_check start --controls || echo "GATE guard_check start FAILED"
  run -m exp.static_check
  gate exp.es_semantics          # not inside the pipeline: an exit there would only leave a subshell
  $PY -m exp.es_semantics | tail -3
  run -m exp.cleanup exp.es_semantics
  run -m old_repro.n_old
  run -m exp.cleanup old_repro.n_old
  run -m exp.review_fix F3 F4 F5 F7 F1S F6 "$R"
  run -m exp.cleanup exp.review_fix
  run -m exp.review_fix F1
  run -m exp.cleanup exp.review_fix
  run -m exp.scope_hold M H 3
  run -m exp.cleanup exp.scope_hold
  run -m exp.n_new "$R"
  run -m exp.cleanup exp.n_new
  run -m exp.row1_zombie "$R"
  run -m exp.cleanup exp.row1_zombie
  run -m exp.row23_latecommit "$R"
  run -m exp.cleanup exp.row23_latecommit
  run -m exp.e_verify "$R"
  run -m exp.cleanup exp.e_verify
  run -m exp.registry_marker "$R"
  run -m exp.cleanup exp.registry_marker
  run -m exp.d1_hold_mentions 5
  run -m exp.cleanup exp.d1_hold_mentions
  run -m exp.d4_rescan_es 5
  run -m exp.cleanup exp.d4_rescan_es
  run -m exp.row19_secrets
  run -m exp.cleanup exp.row19_secrets
  run -m exp.heldout_secrets
  run -m exp.row6_chain "$R"
  run -m exp.cleanup exp.row6_chain
  run -m exp.d4_store_mark 5
  run -m exp.cleanup exp.d4_store_mark
  run -m exp.d3_identifier 5
  run -m exp.cleanup exp.d3_identifier
  run -m exp.mutual_mentions 5 10 5
  run -m exp.cleanup exp.mutual_mentions
  run -m exp.lock_order
  run -m exp.cleanup exp.lock_order
  run -m exp.d2_verifier_process 5
  run -m exp.cleanup exp.d2_verifier_process
  run -m exp.phys_purge
  run -m exp.cleanup exp.phys_purge
  run -m exp.e2e_restored E1 E2 E3 40
  run -m exp.cleanup exp.e2e_restored
  run -m exp.races R1 R2 R3 40
  run -m exp.cleanup exp.races
  # amendment 18: resets the database for each of its runs, so it runs after every experiment that keeps state
  run -m exp.plan_stability
  run -m exp.guard_check end || echo "GATE guard_check end FAILED"
  # D6 (per-role ES keys with security on) is build step 2: its projector role cannot move the epoch alias yet
  # (security_exception in the 4f403dd shake-down), so it is not part of the step-1 run.
  $PY -m exp.disk_gate end || { echo "end VOID $(date -u +%FT%TZ)"; exit 3; }
  echo "end $(date -u +%FT%TZ)"
} > "$OUT" 2>&1
echo "$OUT"
