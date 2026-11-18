#!/bin/sh
# wphard live recheck (agent a5bed86994269066b, writepath hardening owner). UNSTAMPED; no launch_one, no disk gate
# verdict claimed. Holds the lab run lock only while it runs. Home paths printed as ~.
# Disk rule (amendment H6d, the user's decision): starts only with >= 10485760 KiB free; stops itself under 8388608 KiB.
#   - A watcher (every 5 s) writes $O/STOP under the stop line; from then on it sends SIGTERM once to every direct child
#     of this chain, including children started later (review D1: a grouped step's next command), and every command
#     of a grouped step is guarded by `stopped`. The chain itself checks before every step.
#   - SIGTERM reaches the orchestrators through `timeout`; they turn it into SystemExit (exp/stopsig.py, review D2), so
#     their own cleanup runs; the watcher never signals grandchildren, so that cleanup is not interrupted.
#   - A stopped step's outputs are renamed *.STOPPED, its summary line is not printed, and the chain exits 6.
# Amendment 23a VOID rule (review D3): after each step the ES node's log since the step started is read; a high-watermark
# or flood-stage "exceeded" line, or an unreadable log, makes the step VOID: outputs renamed *.VOID, chain exits 7.
# Steps (full -v output per step in $O; the summary prints one result line per step):
#   EW  recreate this stream's ES node with security off and the H6d watermarks (wp.es_security disable)
#   G1  tests.test_ident            G2 WP_PG_LIVE tests.test_d3_pg -v      G3 WP_PG_LIVE tests.test_schema_broker_pg -v
#   P0  wp.setup --reset            P1 exp.registry_marker                 P2 exp.d4_store_mark 1
#   PC  DEVIATION for P1: a plaintext row carrying registry_marker's PG-grep positive-control marker is seeded in database
#       emlab (empty on this cluster since 2026-11-18): a grep-sensitivity control, not the old code's write path
#   RR  registry.unregistered() with wp/registry.py of the pre-seal tree vs HEAD, same live system
#   RS  tests.test_d3_pg with SCHEMA_UNDER_TEST = pre-seal schema   RH ... = pre-review-H schema
#   RX  tests.test_d3_pg with SCHEMA_UNDER_TEST = $RX_TREE/wp/schema.sql (the "before" of the latest fixes)
#   XB  tests.test_d3_pg from $XB_TREE (its own tests and code)
#   SS  seal_socket_e2e.py (same uid)      SU seal_uids_e2e.py (sealer under its own uid)
#   D6  wp.es_security enable, licence, exp.h_d6_broker 5 (UNSTAMPED; amendments H6c, H6e)
#   RP  rot_proof.py against $ROT_OLD (sha256 list of the key ids printed in the 27094c9 window): counts only
#   LS  runkit/leak_scan.py over the outputs
# usage: sh live_chain.sh <HEAD tree> <pre-seal tree> <pre-H tree> <out dir> [steps]
set -u
ME="wphard a5bed86994269066b"
L=$HOME/emlab2-16096/RUNLOCK
export WP_HOME=$HOME/emlab2-16096/wphard/wp
export WP_UCD_DIR=$HOME/emlab2-16096/wphard/ucd
PY=$WP_HOME/venv/bin/python
NEW=$1; SEAL0=$2; H0=$3; O=$4; STEPS=${5:-"EW G1 P0 G2 G3 PC P1 P2 RR RS RH SS SU D6 RP LS"}
START_KIB=10485760; STOP_KIB=8388608     # amendment H6d (were 20971520 start, 15 GB floor)
free_kib() { df -k --output=avail "$HOME" | tail -1 | tr -dc 0-9; }
mkdir -p "$O"
K=$(free_kib)
[ "$K" -ge "$START_KIB" ] || { echo "below start line: $K KiB"; exit 4; }
mkdir "$L" 2>/dev/null || { echo "lock held: $(cut -c1-60 "$L/owner" 2>/dev/null)"; exit 5; }
echo "$ME $(date -u +%FT%TZ) live-recheck" > "$L/owner"
CHAIN=$$
rm -f "$O/STOP" "$O/.signalled"
: > "$O/.signalled"
( SELF=$(sh -c 'echo $PPID')
  while kill -0 "$CHAIN" 2>/dev/null; do
    if [ ! -e "$O/STOP" ]; then
      k=$(free_kib)
      [ "$k" -lt "$STOP_KIB" ] && echo "below stop line: $k KiB at $(date -u +%FT%TZ)" > "$O/STOP"
    fi
    if [ -e "$O/STOP" ]; then
      for c in $(pgrep -P "$CHAIN"); do
        [ "$c" = "$SELF" ] && continue
        grep -qx "$c" "$O/.signalled" && continue
        echo "$c" >> "$O/.signalled"; kill -TERM "$c" 2>/dev/null
      done
    fi
    sleep 5
  done ) &
WATCH=$!
release() { kill "$WATCH" 2>/dev/null
  grep -q "$ME" "$L/owner" 2>/dev/null && rm -r "$L" && echo "lock released $(date -u +%FT%TZ)"; }
trap release EXIT
S() { sed "s#$HOME#~#g"; }
sum() { grep -E "^(Ran |OK|FAILED)" "$1" | tr '\n' ' '; echo; }
say() { echo "$*" >> "$O/.sum"; }
stopped() { [ -e "$O/STOP" ] || [ "$(free_kib)" -lt "$STOP_KIB" ]; }
ESC=$(cat "$WP_HOME/conf/es_container" 2>/dev/null)
# amendment 23a lines in the node's log since $1 (UTC); prints a count, or "unreadable"
wm_lines() { docker logs --since "$1" "$ESC" > "$O/.eslog" 2>&1 || { echo unreadable; return; }
  grep -ciE "(high|flood.stage) disk watermark.*exceeded" "$O/.eslog"; }
echo "lock taken $(date -u +%FT%TZ) head $(cat "$NEW/GIT_HEAD") seal0 $(cat "$SEAL0/GIT_HEAD") h0 $(cat "$H0/GIT_HEAD") free_kib $K"
cd "$NEW" || exit 2
for st in $STEPS; do
  stopped && { echo "STOPPED before $st: $(cat "$O/STOP" 2>/dev/null || echo "below stop line: $(free_kib) KiB")"; exit 6; }
  t0=$(date +%s); ts=$(date -u +%FT%TZ); : > "$O/.sum"
  case $st in
    EW) timeout 600 $PY -m wp.es_security disable > "$O/EW.out" 2>&1
        say "EW exit=$? $(docker inspect "$ESC" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>&1 | grep -o 'watermark\.[a-z_]*=[0-9a-z]*' | tr '\n' ' ')";;
    G1) timeout 900 $PY -m unittest -v tests.test_ident > "$O/G1.out" 2>&1; say "G1 exit=$? $(sum "$O/G1.out")";;
    P0) timeout 600 $PY -m wp.setup --reset 2>&1 | grep -v '^seal key' > "$O/P0.out"; say "P0 $(tail -2 "$O/P0.out" | S | tr '\n' ' ')";;
    G2) WP_PG_LIVE=1 timeout 2400 $PY -m unittest -v tests.test_d3_pg > "$O/G2.out" 2>&1; say "G2 exit=$? $(sum "$O/G2.out")";;
    G3) WP_PG_LIVE=1 timeout 600 $PY -m unittest -v tests.test_schema_broker_pg > "$O/G3.out" 2>&1; say "G3 exit=$? $(sum "$O/G3.out")";;
    P1) timeout 1800 $PY -m exp.registry_marker > "$O/P1.out" 2>&1; say "P1 exit=$? $(tail -4 "$O/P1.out" | S | cut -c1-400 | tr '\n' ' ')";;
    P2) timeout 900 $PY -m exp.d4_store_mark 1 > "$O/P2.out" 2>&1; say "P2 exit=$? $(tail -4 "$O/P2.out" | S | cut -c1-400 | tr '\n' ' ')";;
    RR) R=$O/rr-tree-$(date +%s); cp -R "$NEW" "$R"; cp "$SEAL0/wp/registry.py" "$R/wp/registry.py"   # kept in $O (small)
        for tr in "$R" "$NEW"; do
          stopped && break
          timeout 300 env -C "$tr" $PY -c '
from wp import db, config, registry
from wp.es import ES
vc, vk = db.verifier(), db.verifier(db=config.KEYS_DB)
print("unregistered", registry.unregistered(vc, vk, ES()))' > "$O/RR.$(basename "$tr").out" 2>&1
          say "RR $(basename "$tr") $(tail -1 "$O/RR.$(basename "$tr").out" | S)"
        done;;
    RS) WP_PG_LIVE=1 SCHEMA_UNDER_TEST=$SEAL0/wp/schema.sql timeout 2400 $PY -m unittest -v tests.test_d3_pg > "$O/RS.out" 2>&1; say "RS exit=$? $(sum "$O/RS.out")";;
    RH) WP_PG_LIVE=1 SCHEMA_UNDER_TEST=$H0/wp/schema.sql timeout 2400 $PY -m unittest -v tests.test_d3_pg > "$O/RH.out" 2>&1; say "RH exit=$? $(sum "$O/RH.out")";;
    RX) XT=${RX_TREE:?RX_TREE names the tree whose schema is the before}
        WP_PG_LIVE=1 SCHEMA_UNDER_TEST=$XT/wp/schema.sql timeout 2400 $PY -m unittest -v tests.test_d3_pg > "$O/RX.out" 2>&1; say "RX exit=$? schema $(cat "$XT/GIT_HEAD") $(sum "$O/RX.out")";;
    XB) XT=${XB_TREE:?XB_TREE names the tree}
        WP_PG_LIVE=1 timeout 2400 env -C "$XT" $PY -m unittest -v tests.test_d3_pg > "$O/XB.out" 2>&1; say "XB exit=$? tree $(cat "$XT/GIT_HEAD") $(sum "$O/XB.out")";;
    D6) { echo "tree $(cat "$NEW/GIT_HEAD") free_kib $(free_kib)"
          stopped || timeout 600 $PY -m wp.es_security enable 2>&1 | tail -1
          stopped || timeout 60 $PY -c '
from wp.es import ES
s, j = ES().req("GET", "/_license", ok=None)
l = j.get("license", {}) if isinstance(j, dict) else {}
print("licence", s, l.get("type"), l.get("status"))'
          stopped || { timeout 3600 $PY -m exp.h_d6_broker 5; echo "exit=$?"; }; } > "$O/D6.out" 2>&1
        say "D6 $(grep -E '^(licence|h_d6_broker|exit=)' "$O/D6.out" | S | tr '\n' ' ')";;
    RP) timeout 120 $PY -I results/failing-first/harness/rot_proof.py "${ROT_OLD:?ROT_OLD names the old key-id hash list}" > "$O/RP.out" 2>&1
        say "RP exit=$? $(tail -1 "$O/RP.out")";;
    PC) timeout 120 $PY -c '
from wp import db
a = db.admin(db="emlab")
a.execute("create table if not exists pc_marker(x text)")
if not a.execute("select 1 from pc_marker limit 1").fetchone():
    a.execute("insert into pc_marker values (%s)", ("zqn6secret plaintext positive control",))
# review J M10: TOAST sensitivity. The same 20 KB compressible value in a table with default storage (compressed) and
# one with storage external (out of line, uncompressed); registry_marker greps both (toast_control)
for t, ext in (("pc_toast_c", False), ("pc_toast_x", True)):
    a.execute(f"drop table if exists {t}")
    a.execute(f"create table {t}(x text)")
    if ext:
        a.execute(f"alter table {t} alter column x set storage external")
    a.execute(f"insert into {t} values (%s)", ("zqtoastpc-" * 2000,))
a.execute("checkpoint")
print("PC seeded", a.execute("select count(*) from pc_marker").fetchone()[0])' > "$O/PC.out" 2>&1; say "PC exit=$? $(tail -1 "$O/PC.out")";;
    SS) timeout 600 $PY results/failing-first/harness/seal_socket_e2e.py > "$O/SS.out" 2>&1; say "SS exit=$? $(tail -1 "$O/SS.out" | S | cut -c1-600)";;
    SU) timeout 1200 $PY results/failing-first/harness/seal_uids_e2e.py > "$O/SU.out" 2>&1; say "SU exit=$? $(tail -1 "$O/SU.out" | S | cut -c1-900)";;
    LS) say "LS $($PY runkit/leak_scan.py "$WP_HOME" "$O" "$WP_HOME/results" "$WP_HOME/logs" "$WP_HOME/state" 2>&1 | S | tr '\n' ' ')";;
    *) say "$st unknown step";;
  esac
  if [ -e "$O/STOP" ]; then
    for f in "$O/$st.out" "$O/$st".*.out; do [ -e "$f" ] && mv "$f" "$f.STOPPED"; done
    echo "STOPPED during $st: $(cat "$O/STOP")"; exit 6
  fi
  n=$(wm_lines "$ts")
  if [ "$n" != 0 ]; then
    for f in "$O/$st.out" "$O/$st".*.out; do [ -e "$f" ] && mv "$f" "$f.VOID"; done
    echo "VOID $st: amendment 23a, ES log watermark lines since $ts: $n"; exit 7
  fi
  cat "$O/.sum"
  echo "  $st seconds=$(( $(date +%s) - t0 )) watermark_lines 0"
done
# leaked throwaway databases of the live tests (ours only, by prefix)
$PY -c '
from wp import db
a = db.admin(db="postgres")
left = [r[0] for r in a.execute("select datname from pg_database where datname like %s or datname like %s", ("wp_d3check_%", "wp_b1check_%")).fetchall()]
for d in left:
    a.execute(f"drop database if exists \"{d}\" with (force)")
print("dropped leaked test databases", len(left))' 2>&1 | tail -1
echo "free_kib_end $(free_kib) chain done $(date -u +%FT%TZ)"
