#!/bin/sh
# Review E shake-down on t2-dev (wphard, agent af5c8cce658ab5a89). Unstamped and short; holds the lab run lock only
# while it runs (>= 20971520 KiB free). Home paths in its output are printed as ~.
#   G1  tests.test_ident (matcher m4) on this host's Unicode version
#   R1  the same tests with wp/mentions.py of the 0e8f67c tree (m3)
#   G2  tests.test_d3_pg live on the new tree (A, C, E1-E8)
#   R2  E1-E8 on the 0e8f67c tree (its schema)
#   V*  one broken variant per lock (reviewE_variants.py): the listed E tests must fail
#   P1  wp.setup --reset, then exp.d4_store_mark R=1 (the m4 call sites end to end)
# usage: sh reviewE_check.sh <new src dir> <src dir with the 0e8f67c code>
set -u
ME="wphard af5c8cce658ab5a89"
L=$HOME/emlab2-16096/RUNLOCK
export WP_HOME=$HOME/emlab2-16096/wphard/wp
export WP_UCD_DIR=$HOME/emlab2-16096/wphard/ucd
PY=$WP_HOME/venv/bin/python
NEW=$1
OLD=$2
K=$(df -k --output=avail "$HOME" | tail -1 | tr -dc 0-9)
[ "$K" -ge 20971520 ] || { echo "below start line: $K KiB"; exit 4; }
mkdir "$L" 2>/dev/null || { echo "lock held: $(cut -c1-60 "$L/owner" 2>/dev/null)"; exit 5; }
echo "$ME $(date -u +%FT%TZ) reviewE-check" > "$L/owner"
T=$HOME/emlab2-16096/wphard/rc-trees/E$(date -u +%Y%m%dT%H%M%SZ)   # tree copies are kept (small)
mkdir -p "$T"
release() { grep -q "$ME" "$L/owner" 2>/dev/null && rm -r "$L" && echo "lock released $(date -u +%FT%TZ)"; }
trap release EXIT
H="^(test_|FAIL|ERROR|OK|Ran|FAILED)|AssertionError|Error:"
S() { sed "s#$HOME#~#g" | cut -c1-220; }
E="tests.test_d3_pg.D3Store"
ET="$E.test_e1_rescan_link_waits_for_an_inflight_removal $E.test_e2_hold_on_the_speaker_waits_and_reports_the_removal $E.test_e3_hold_on_a_rescan_linked_subject_waits_and_reports $E.test_e4_hold_first_withholds_and_release_waits $E.test_e5_hold_on_the_subject_itself $E.test_e6_repeated_mark_during_a_batch_does_not_deadlock $E.test_e7_hold_against_mark $E.test_e8_hold_waits_for_the_final_pass"
echo "lock taken $(date -u +%FT%TZ) new $(cat "$NEW/GIT_HEAD") old $(cat "$OLD/GIT_HEAD") free_kib $K"
echo "unicode $($PY -c 'import unicodedata; print(unicodedata.unidata_version)')"
cd "$NEW" || exit 2
echo "== M matcher classes: build time, not-closed count"
$PY -c 'import time; t0 = time.time(); from wp import mentions as M; M._classes(); t1 = time.time(); n = len(M.not_closed()); print("classes_s %.2f not_closed %d total_s %.2f" % (t1 - t0, n, time.time() - t0))' 2>&1 | S
echo "== G1 test_ident (m4)"
timeout 600 $PY -m unittest tests.test_ident 2>&1 | tail -3 | S
echo "== R1 test_ident (new tests, wp/mentions.py of the old tree)"
cp -R "$NEW" "$T/r1" && cp "$OLD/wp/mentions.py" "$T/r1/wp/mentions.py"
(cd "$T/r1" && timeout 600 $PY -m unittest tests.test_ident 2>&1 | grep -E "$H|not matched" | S | head -24)
echo "== G2 test_d3_pg live (new)"
WP_PG_LIVE=1 timeout 600 $PY -m unittest tests.test_d3_pg -v 2>&1 | grep -E "$H" | S | head -30
echo "== R2 E1-E8 on the old tree (new test file, old schema)"
cp -R "$OLD" "$T/r2" && cp "$NEW/tests/test_d3_pg.py" "$T/r2/tests/test_d3_pg.py"
(cd "$T/r2" && WP_PG_LIVE=1 timeout 600 $PY -m unittest $ET -v 2>&1 | grep -E "$H" | S | head -30)
VP="$NEW/results/failing-first/harness/reviewE_variants.py"
$PY -I "$VP" --list | while read -r v tests; do
  echo "== V $v (must fail: $tests)"
  cp -R "$NEW" "$T/v-$v"
  $PY -I "$VP" "$T/v-$v" "$v" || continue
  sel=""
  for x in $(echo "$tests" | tr ',' ' '); do sel="$sel $(echo "$ET" | tr ' ' '\n' | grep "test_$(echo "$x" | tr 'E' 'e')_")"; done
  (cd "$T/v-$v" && WP_PG_LIVE=1 timeout 300 $PY -m unittest $sel -v 2>&1 | grep -E "$H" | S | head -12)
done
echo "== P1 setup --reset, exp.d4_store_mark R=1 (new)"
timeout 300 $PY -m wp.setup --reset 2>&1 | tail -2 | S
timeout 600 $PY -m exp.d4_store_mark 1 2>&1 | tail -6 | S
