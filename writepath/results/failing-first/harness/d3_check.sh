#!/bin/sh
# D3 store check on t2-dev (wphard, agent af5c8cce658ab5a89): tests.test_d3_pg green (src at the D3 commit) and red
# (SCHEMA_UNDER_TEST = schema.sql at fb60b2e, before D3). Unstamped, short; holds the lab run lock only while it runs
# (>= 20971520 KiB free). Ensures the reviewer login roles first (setup._ensure_roles: role secrets go to their own
# 0600 files; nothing is printed). usage: sh d3_check.sh <src dir>
set -u
ME="wphard af5c8cce658ab5a89"
L=$HOME/emlab2-16096/RUNLOCK
export WP_HOME=$HOME/emlab2-16096/wphard/wp
PY=$WP_HOME/venv/bin/python
SRC=$1
K=$(df -k --output=avail "$HOME" | tail -1 | tr -dc 0-9)
[ "$K" -ge 20971520 ] || { echo "below start line: $K KiB"; exit 4; }
mkdir "$L" 2>/dev/null || { echo "lock held: $(cut -c1-60 "$L/owner" 2>/dev/null)"; exit 5; }
echo "$ME $(date -u +%FT%TZ) d3-store-check" > "$L/owner"
release() { grep -q "$ME" "$L/owner" 2>/dev/null && rm -r "$L" && echo "lock released $(date -u +%FT%TZ)"; }
trap release EXIT
cd "$SRC" || exit 2
echo "lock taken $(date -u +%FT%TZ) src $(cat GIT_HEAD) free_kib $K"
echo "== reviewer roles"
timeout 60 $PY -c "from wp import db, setup; a = db.admin(db='postgres'); setup._ensure_roles(a); print('roles', sorted(r[0] for r in a.execute(\"select rolname from pg_roles where rolname like 'wp_rev%'\").fetchall()))" 2>&1 | tail -2
echo "== green (src schema.sql)"
WP_PG_LIVE=1 timeout 300 $PY -m unittest tests.test_d3_pg -v 2>&1 | tail -12
echo "== red (schema.sql at fb60b2e)"
WP_PG_LIVE=1 SCHEMA_UNDER_TEST=$HOME/emlab2-16096/wphard/schema.fb60b2e.sql timeout 300 $PY -m unittest tests.test_d3_pg -v 2>&1 | grep -E "^(test_|FAIL|ERROR|OK|Ran|FAILED)|sqlstate|Error:|AssertionError" | cut -c1-220 | head -24
