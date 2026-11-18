#!/bin/sh
# wphard live recheck (agent a5bed86994269066b, writepath hardening owner). UNSTAMPED; no launch_one, no disk gate
# verdict claimed. Holds the lab run lock only while it runs (>= 20971520 KiB free at start). Home paths printed as ~.
# Full -v output per step goes to $O (on t2-dev); the summary prints per-step result lines.
#   G1  tests.test_ident (HEAD)            G2 WP_PG_LIVE tests.test_d3_pg -v (HEAD schema)
#   G3  WP_PG_LIVE tests.test_schema_broker_pg -v      P0 wp.setup --reset (HEAD)
#   P1  exp.registry_marker (HEAD; unregistered() on the live system, quarantine and verifier end to end)
#   P2  exp.d4_store_mark 1 (HEAD)
#   RR  registry.unregistered() with wp/registry.py of the pre-seal tree vs HEAD, same live system
#   D6  exp.h_d6_broker 5, UNSTAMPED (amendment H6c), licence type read first;  LS runkit/leak_scan.py over outputs
#   PC  DEVIATION for P1: registry_marker's PG-grep positive control expects the OLD code's plaintext marker in
#       database emlab; that database is empty on this cluster (observed 2026-11-18), so a plaintext row carrying the
#       same marker is seeded there (a grep-sensitivity control, not the old code's write path)
#   XB  tests.test_d3_pg from XB_TREE (bca7764: review H tests on 6ddeca3 code, the F14/F16 "before")
#   SS  seal_socket_e2e.py (same uid)      SU seal_uids_e2e.py (sealer under its own uid, H2 container pattern)
#   RS  tests.test_d3_pg with SCHEMA_UNDER_TEST = pre-seal schema (179412e)   RH  ... = pre-review-H schema (6ddeca3)
# usage: sh live_chain.sh <HEAD tree> <pre-seal tree> <pre-H tree> <out dir> [steps]
set -u
ME="wphard a5bed86994269066b"
L=$HOME/emlab2-16096/RUNLOCK
export WP_HOME=$HOME/emlab2-16096/wphard/wp
export WP_UCD_DIR=$HOME/emlab2-16096/wphard/ucd
PY=$WP_HOME/venv/bin/python
NEW=$1; SEAL0=$2; H0=$3; O=$4; STEPS=${5:-"G1 P0 G2 G3 PC P1 P2 RR RS RH XB SS SU D6 LS"}
mkdir -p "$O"
K=$(df -k --output=avail "$HOME" | tail -1 | tr -dc 0-9)
[ "$K" -ge 20971520 ] || { echo "below start line: $K KiB"; exit 4; }
mkdir "$L" 2>/dev/null || { echo "lock held: $(cut -c1-60 "$L/owner" 2>/dev/null)"; exit 5; }
echo "$ME $(date -u +%FT%TZ) live-recheck" > "$L/owner"
release() { grep -q "$ME" "$L/owner" 2>/dev/null && rm -r "$L" && echo "lock released $(date -u +%FT%TZ)"; }
trap release EXIT
S() { sed "s#$HOME#~#g"; }
sum() { grep -E "^(Ran |OK|FAILED)" "$1" | tr '\n' ' '; echo; }
echo "lock taken $(date -u +%FT%TZ) head $(cat "$NEW/GIT_HEAD") seal0 $(cat "$SEAL0/GIT_HEAD") h0 $(cat "$H0/GIT_HEAD") free_kib $K"
cd "$NEW" || exit 2
for st in $STEPS; do
  t0=$(date +%s)
  case $st in
    G1) timeout 900 $PY -m unittest -v tests.test_ident > "$O/G1.out" 2>&1; echo "G1 exit=$? $(sum "$O/G1.out")";;
    P0) timeout 600 $PY -m wp.setup --reset 2>&1 | grep -v '^seal key' > "$O/P0.out"; echo "P0 $(tail -2 "$O/P0.out" | S | tr '\n' ' ')";;
    G2) WP_PG_LIVE=1 timeout 2400 $PY -m unittest -v tests.test_d3_pg > "$O/G2.out" 2>&1; echo "G2 exit=$? $(sum "$O/G2.out")";;
    G3) WP_PG_LIVE=1 timeout 600 $PY -m unittest -v tests.test_schema_broker_pg > "$O/G3.out" 2>&1; echo "G3 exit=$? $(sum "$O/G3.out")";;
    P1) timeout 1800 $PY -m exp.registry_marker > "$O/P1.out" 2>&1; echo "P1 exit=$? $(tail -4 "$O/P1.out" | S | cut -c1-400 | tr '\n' ' ')";;
    P2) timeout 900 $PY -m exp.d4_store_mark 1 > "$O/P2.out" 2>&1; echo "P2 exit=$? $(tail -4 "$O/P2.out" | S | cut -c1-400 | tr '\n' ' ')";;
    RR) R=$O/rr-tree-$(date +%s); cp -R "$NEW" "$R"; cp "$SEAL0/wp/registry.py" "$R/wp/registry.py"   # kept in $O (small)
        for tr in "$R" "$NEW"; do
          (cd "$tr" && timeout 300 $PY -c '
from wp import db, config, registry
from wp.es import ES
vc, vk = db.verifier(), db.verifier(db=config.KEYS_DB)
print("unregistered", registry.unregistered(vc, vk, ES()))' 2>&1 | tail -1 | S | sed "s#^#RR $(basename "$tr") #")
        done;;
    RS) WP_PG_LIVE=1 SCHEMA_UNDER_TEST=$SEAL0/wp/schema.sql timeout 2400 $PY -m unittest -v tests.test_d3_pg > "$O/RS.out" 2>&1; echo "RS exit=$? $(sum "$O/RS.out")";;
    RH) WP_PG_LIVE=1 SCHEMA_UNDER_TEST=$H0/wp/schema.sql timeout 2400 $PY -m unittest -v tests.test_d3_pg > "$O/RH.out" 2>&1; echo "RH exit=$? $(sum "$O/RH.out")";;
    D6) { timeout 120 $PY -m wp.es_security enable 2>&1 | tail -1
          timeout 60 $PY -c '
from wp.es import ES
s, j = ES().req("GET", "/_license", ok=None)
l = j.get("license", {}) if isinstance(j, dict) else {}
print("licence", s, l.get("type"), l.get("status"))'
          timeout 3600 $PY -m exp.h_d6_broker 5; echo "exit=$?"; } > "$O/D6.out" 2>&1
        echo "D6 $(grep -E '^(licence|h_d6_broker|exit=)' "$O/D6.out" | S | tr '\n' ' ')";;
    PC) timeout 120 $PY -c '
from wp import db
a = db.admin(db="emlab")
a.execute("create table if not exists pc_marker(x text)")
if not a.execute("select 1 from pc_marker limit 1").fetchone():
    a.execute("insert into pc_marker values (%s)", ("zqn6secret plaintext positive control",))
a.execute("checkpoint")
print("PC seeded", a.execute("select count(*) from pc_marker").fetchone()[0])' > "$O/PC.out" 2>&1; echo "PC exit=$? $(tail -1 "$O/PC.out")";;
    XB) XT=${XB_TREE:?XB_TREE names the bca7764 tree}
        (cd "$XT" && WP_PG_LIVE=1 timeout 2400 $PY -m unittest -v tests.test_d3_pg > "$O/XB.out" 2>&1); echo "XB exit=$? tree $(cat "$XT/GIT_HEAD") $(sum "$O/XB.out")";;
    SS) timeout 600 $PY results/failing-first/harness/seal_socket_e2e.py > "$O/SS.out" 2>&1; echo "SS exit=$? $(tail -1 "$O/SS.out" | S | cut -c1-600)";;
    SU) timeout 1200 $PY results/failing-first/harness/seal_uids_e2e.py > "$O/SU.out" 2>&1; echo "SU exit=$? $(tail -1 "$O/SU.out" | S | cut -c1-900)";;
    LS) echo "LS $($PY runkit/leak_scan.py "$WP_HOME" "$O" "$WP_HOME/results" "$WP_HOME/logs" "$WP_HOME/state" 2>&1 | S | tr '\n' ' ')";;
  esac
  echo "  $st seconds=$(( $(date +%s) - t0 ))"
done
# leaked throwaway databases of the live tests (ours only, by prefix)
$PY -c '
from wp import db
a = db.admin(db="postgres")
left = [r[0] for r in a.execute("select datname from pg_database where datname like %s or datname like %s", ("wp_d3check_%", "wp_b1check_%")).fetchall()]
for d in left:
    a.execute(f"drop database if exists \"{d}\" with (force)")
print("dropped leaked test databases", len(left))' 2>&1 | tail -1
echo "free_kib_end $(df -k --output=avail "$HOME" | tail -1 | tr -dc 0-9) chain done $(date -u +%FT%TZ)"
