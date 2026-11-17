#!/bin/sh
# Review C shake-down, window 4 (wphard, agent af5c8cce658ab5a89). Unstamped and short; holds the lab run lock only while
# it runs (>= 20971520 KiB free). Window 3's P1 ran exp.d4_store_mark against the lab database still on the pre-m3
# schema (UndefinedColumn identifier_epochs.matcher). Here the lab databases are reset from this tree first, as
# run_all.sh does (wp.setup --reset: this lab's own container only; generated role passwords go to the 0600 pgpass
# file, nothing secret is printed), then exp.d4_store_mark R=1 drives log.append (mention_matcher), declare/init_tenant
# and erasure.rescan (matcher argument) end to end.
# usage: sh reviewC_check3.sh <new src dir>
set -u
ME="wphard af5c8cce658ab5a89"
L=$HOME/emlab2-16096/RUNLOCK
export WP_HOME=$HOME/emlab2-16096/wphard/wp
export WP_UCD_DIR=$HOME/emlab2-16096/wphard/ucd
PY=$WP_HOME/venv/bin/python
NEW=$1
K=$(df -k --output=avail "$HOME" | tail -1 | tr -dc 0-9)
[ "$K" -ge 20971520 ] || { echo "below start line: $K KiB"; exit 4; }
mkdir "$L" 2>/dev/null || { echo "lock held: $(cut -c1-60 "$L/owner" 2>/dev/null)"; exit 5; }
echo "$ME $(date -u +%FT%TZ) reviewC-check3" > "$L/owner"
release() { grep -q "$ME" "$L/owner" 2>/dev/null && rm -r "$L" && echo "lock released $(date -u +%FT%TZ)"; }
trap release EXIT
echo "lock taken $(date -u +%FT%TZ) new $(cat "$NEW/GIT_HEAD") free_kib $K"
cd "$NEW" || exit 2
echo "== setup --reset (new schema)"
timeout 300 $PY -m wp.setup --reset 2>&1 | tail -3 | cut -c1-200
echo "== P1 exp.d4_store_mark R=1 (new)"
timeout 600 $PY -m exp.d4_store_mark 1 2>&1 | tail -14 | cut -c1-240
