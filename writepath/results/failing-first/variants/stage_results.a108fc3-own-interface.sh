#!/bin/sh
# The a108fc3 stage_results.py driven through ITS OWN interface (groups.tsv: group, source, file, label): a result row
# that no gate record lists (no run, no end record, no ledger at all) is staged. Prints the receipt to stdout.
# usage: sh old_stage_own_interface.sh <repo writepath dir> <empty work dir>
set -eu
W=$1; T=$2
mkdir "$T/results" "$T/ab" "$T/dst"
git -C "$W" show a108fc3:writepath/runkit/stage_results.py > "$T/old_stage_results.py"
printf '{"name": "forged", "at": "2020-01-01T00:00:00Z", "result": {"ok": true}}\n' > "$T/results/forged.0000000000000000.json"
printf 'group\tsource\tfile\tlabel\nforged-group\tresults\tforged.0000000000000000.json\tnever run under any gate\n' > "$T/groups.tsv"
echo "# failing-first receipt (own interface): a108fc3:writepath/runkit/stage_results.py blob $(git -C "$W" rev-parse a108fc3:writepath/runkit/stage_results.py)"
echo "# input: one JSON row that no gate run, end record or ledger lists; command: HOST_HOME=/nohome/ python3 old_stage_results.py groups.tsv results ab dst"
cd "$T"
HOST_HOME=/nohome/ python3 old_stage_results.py groups.tsv results ab dst; echo "exit $?"
echo "staged files:"; (cd dst && find . -type f | sort)
