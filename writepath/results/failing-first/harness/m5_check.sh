#!/bin/sh
# Matcher m5 shake-down on t2-dev (wphard, agent af5c8cce658ab5a89). Unstamped and short; holds the lab run lock only
# while it runs (>= 20971520 KiB free). Home paths in its output are printed as ~.
#   M   both-readings counts and not-closed count on this host's Unicode version, first-use time
#   G1  tests.test_ident (matcher m5) on this host's Unicode version
#   R1  the same tests with wp/mentions.py of the m4 tree (unchanged from 0bff333 to 15b3255)
#   G2  tests.test_d3_pg live on the new tree (its schema, receipt text included)
#   P1  wp.setup --reset, then exp.d4_store_mark R=1 (the m5 call sites end to end: declare, scan, erasure rescan)
# usage: sh m5_check.sh <new src dir> <src dir with the m4 code>
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
echo "$ME $(date -u +%FT%TZ) m5-check" > "$L/owner"
T=$HOME/emlab2-16096/wphard/rc-trees/M5$(date -u +%Y%m%dT%H%M%SZ)   # tree copies are kept (small)
mkdir -p "$T"
release() { grep -q "$ME" "$L/owner" 2>/dev/null && rm -r "$L" && echo "lock released $(date -u +%FT%TZ)"; }
trap release EXIT
H="^(test_|FAIL|ERROR|OK|Ran|FAILED)|AssertionError|Error:"
S() { sed "s#$HOME#~#g" | cut -c1-220; }
echo "lock taken $(date -u +%FT%TZ) new $(cat "$NEW/GIT_HEAD") old $(cat "$OLD/GIT_HEAD") free_kib $K"
echo "unicode $($PY -c 'import unicodedata; print(unicodedata.unidata_version)')"
cd "$NEW" || exit 2
echo "== M both readings, not closed"
$PY -c '
import time
t0 = time.time()
from wp import mentions as M
name = {M.canonical(x): x for x in "ilo01"}
c = {}
for g, rs in M.both_readings().items():
    k = "".join(sorted(name.get(r, "?") for r in rs))
    c[k] = c.get(k, 0) + 1
print("both", sorted(c.items()), "not_closed", len(M.not_closed()), "first_use_s %.2f" % (time.time() - t0), "id", M.matcher_id()[:2])
' 2>&1 | S
echo "== G1 test_ident (m5)"
timeout 600 $PY -m unittest tests.test_ident 2>&1 | tail -3 | S
echo "== R1 test_ident (new tests, wp/mentions.py of the m4 tree)"
cp -R "$NEW" "$T/r1" && cp "$OLD/wp/mentions.py" "$T/r1/wp/mentions.py"
(cd "$T/r1" && timeout 600 $PY -m unittest tests.test_ident 2>&1 | grep -E "^(FAIL|ERROR|Ran|FAILED)" | S | head -24)
echo "== G2 test_d3_pg live (new)"
WP_PG_LIVE=1 timeout 600 $PY -m unittest tests.test_d3_pg 2>&1 | tail -3 | S
echo "== P1 setup --reset, exp.d4_store_mark R=1 (new)"
timeout 300 $PY -m wp.setup --reset 2>&1 | tail -2 | S
timeout 600 $PY -m exp.d4_store_mark 1 2>&1 | tail -6 | S
