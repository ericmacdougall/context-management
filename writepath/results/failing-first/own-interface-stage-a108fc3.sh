#!/bin/sh
# Failing-first receipt through the tool's OWN interface (review aacf3c0 asked for behavioural reds of the a108fc3
# staging tool, whose interface differs from the tests' 4-argument one: groups.tsv, a results copy, an ab copy, dst).
# Three scenarios the gated tool refuses (tests/test_stage_results.py): a row of a VOID run, a row changed after the
# run, and a row whose ledger was edited to VALID. The a108fc3 tool has no ledger or run input, so in each it is given
# exactly what its interface takes, and the receipt shows whether it staged the row. All values are generated here.
# usage (from writepath/): sh results/failing-first/own-interface-stage-a108fc3.sh <out>
set -u
OUT=$1
[ -e "$OUT" ] && { echo "refusing: $OUT exists (a receipt is never overwritten)"; exit 2; }
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
git show a108fc3:writepath/runkit/stage_results.py >> "$W/old_stage_results.py" || exit 2
{
  echo "# failing-first receipt (own interface): a108fc3:writepath/runkit/stage_results.py blob $(git rev-parse a108fc3:writepath/runkit/stage_results.py)"
  echo "# date: $(date -u +%FT%TZ); script: results/failing-first/own-interface-stage-a108fc3.sh at HEAD $(git rev-parse --short HEAD)"
} >> "$OUT"
for S in void-run changed-after-run edited-ledger; do
  D=$W/$S
  mkdir -p "$D/results" "$D/ab" "$D/dst"
  case $S in
    void-run) row='{"name": "e", "gate_run": "r-void", "verdict_of_run": "VOID"}' ;;
    changed-after-run) row='{"name": "e", "gate_run": "r-valid", "edited": "after the end record hashed it"}' ;;
    edited-ledger) row='{"name": "e", "gate_run": "r-ledger-edited-to-valid"}' ;;
  esac
  printf '%s\n' "$row" >> "$D/results/e.0000000000000000.json"
  printf 'group\tsource\tfile\tlabel\n%s\tresults\te.0000000000000000.json\t%s\n' "$S" "$S" >> "$D/groups.tsv"
  (cd "$D" && HOST_HOME=/nohome/ python3 "$W/old_stage_results.py" groups.tsv results ab dst) >> "$D/out" 2>&1
  RC=$?
  {
    echo "## scenario $S: the gated tool refuses it; command: HOST_HOME=/nohome/ python3 old_stage_results.py groups.tsv results ab dst"
    sed 's/^/# tool: /' "$D/out"
    echo "# exit $RC"
    echo "# staged files:"
    (cd "$D/dst" && find . -type f | sort | sed 's/^/#   /')
  } >> "$OUT"
done
echo "receipt $OUT"
