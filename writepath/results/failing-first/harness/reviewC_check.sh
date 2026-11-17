#!/bin/sh
# Review C shake-down on t2-dev (wphard, agent af5c8cce658ab5a89). Unstamped and short; holds the lab run lock only
# while it runs (>= 20971520 KiB free). Green on the new tree, red with the new tests on the 4fe3ac1 code:
#   G1/R1 tests.test_ident offline on this host's Unicode version (whole Default_Ignorable enumeration);
#   G2/R2 tests.test_d3_pg live (throwaway database, real login roles);
#   G3    es_alias_live.py on the lab ES (F5 convergence, F9 parameter).
# usage: sh reviewC_check.sh <new src dir> <src dir with the 4fe3ac1 code>
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
echo "$ME $(date -u +%FT%TZ) reviewC-check" > "$L/owner"
T=$HOME/emlab2-16096/wphard/rc-trees/$(date -u +%Y%m%dT%H%M%SZ)   # red-tree copies are kept (small; listed in the output)
mkdir -p "$T"
release() { grep -q "$ME" "$L/owner" 2>/dev/null && rm -r "$L" && echo "lock released $(date -u +%FT%TZ)"; }
trap release EXIT
H="^(test_|FAIL|ERROR|OK|Ran|FAILED)|AssertionError|Error:"
echo "lock taken $(date -u +%FT%TZ) new $(cat "$NEW/GIT_HEAD") old $(cat "$OLD/GIT_HEAD") free_kib $K"
echo "unicode $($PY -c 'import unicodedata; print(unicodedata.unidata_version)') ucd_dir_files $(ls "$WP_UCD_DIR" | tr '\n' ' ')"
cd "$NEW" || exit 2
echo "== G1 test_ident (new)"
timeout 300 $PY -m unittest tests.test_ident 2>&1 | tail -3
echo "== R1 test_ident (new tests, wp/mentions.py from the old tree)"
cp -R "$NEW" "$T/r1" && cp "$OLD/wp/mentions.py" "$T/r1/wp/mentions.py"
(cd "$T/r1" && timeout 300 $PY -m unittest tests.test_ident 2>&1 | grep -E "$H" | cut -c1-200 | head -30)
echo "== G2 test_d3_pg live (new)"
WP_PG_LIVE=1 timeout 300 $PY -m unittest tests.test_d3_pg -v 2>&1 | grep -E "$H" | cut -c1-200 | head -24
echo "== R2 test_d3_pg live (new test file on the old tree, its schema)"
cp -R "$OLD" "$T/r2" && cp "$NEW/tests/test_d3_pg.py" "$T/r2/tests/test_d3_pg.py"
(cd "$T/r2" && WP_PG_LIVE=1 timeout 300 $PY -m unittest tests.test_d3_pg -v 2>&1 | grep -E "$H" | cut -c1-200 | head -30)
echo "== G3 es_alias_live (new)"
timeout 120 $PY results/failing-first/harness/es_alias_live.py 2>&1 | tail -8
