#!/bin/sh
# Review C shake-down, window 3 (wphard, agent af5c8cce658ab5a89). Unstamped and short; holds the lab run lock only while
# it runs (>= 20971520 KiB free). Two questions the first two windows left open:
#   C2  does test_c2 fail on lock order alone? Green on the new tree; red on a BROKEN VARIANT of the new tree whose
#       only change is the propose lock (FOR SHARE removed from wp_identifier_propose's erased check); and its result on
#       the 4fe3ac1 tree (whose cascade also kept proposals, so there the first assertion now names the lock).
#   P1  the Python call sites of the changed SQL signatures against a real store: exp.d4_store_mark with R=1 drives
#       log.append (mention_matcher), declare/init_tenant and erasure.rescan (matcher argument) end to end.
# usage: sh reviewC_check2.sh <new src dir> <src dir with the 4fe3ac1 code>
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
echo "$ME $(date -u +%FT%TZ) reviewC-check2" > "$L/owner"
T=$HOME/emlab2-16096/wphard/rc-trees/$(date -u +%Y%m%dT%H%M%SZ)   # tree copies are kept (small)
mkdir -p "$T"
release() { grep -q "$ME" "$L/owner" 2>/dev/null && rm -r "$L" && echo "lock released $(date -u +%FT%TZ)"; }
trap release EXIT
H="^(test_|FAIL|ERROR|OK|Ran|FAILED)|AssertionError|Error:"
C2=tests.test_d3_pg.D3Store.test_c2_proposal_cannot_commit_after_the_erasure
echo "lock taken $(date -u +%FT%TZ) new $(cat "$NEW/GIT_HEAD") old $(cat "$OLD/GIT_HEAD") free_kib $K"
cd "$NEW" || exit 2
echo "== G2 test_d3_pg live (new)"
WP_PG_LIVE=1 timeout 300 $PY -m unittest tests.test_d3_pg -v 2>&1 | grep -E "$H" | cut -c1-200 | head -24
echo "== B2 test_c2 on the broken variant (new tree, propose lock removed)"
cp -R "$NEW" "$T/b2"
PAT="from subjects where tenant_id = t and subject_id = s for share;   -- lock before check (F8)"
N=$(grep -cF "$PAT" "$T/b2/wp/schema.sql")
[ "$N" = 1 ] || { echo "broken variant: pattern found $N times, not 1; skipped"; N=; }
if [ -n "$N" ]; then
  $PY -I -c 'import sys; p=sys.argv[1]; s=open(p).read(); a=sys.argv[2]; open(p,"w").write(s.replace(a, a.replace(" for share;", ";")))' "$T/b2/wp/schema.sql" "$PAT"
  echo "variant diff lines: $(diff "$NEW/wp/schema.sql" "$T/b2/wp/schema.sql" | grep -c '^[<>]')"
  (cd "$T/b2" && WP_PG_LIVE=1 timeout 300 $PY -m unittest "$C2" -v 2>&1 | grep -E "$H" | cut -c1-200 | head -12)
fi
echo "== R2 test_c2 on the 4fe3ac1 tree (new test file)"
cp -R "$OLD" "$T/r2" && cp "$NEW/tests/test_d3_pg.py" "$T/r2/tests/test_d3_pg.py"
(cd "$T/r2" && WP_PG_LIVE=1 timeout 300 $PY -m unittest "$C2" -v 2>&1 | grep -E "$H" | cut -c1-200 | head -12)
echo "== P1 exp.d4_store_mark R=1 (new)"
timeout 600 $PY -m exp.d4_store_mark 1 2>&1 | tail -12 | cut -c1-240
