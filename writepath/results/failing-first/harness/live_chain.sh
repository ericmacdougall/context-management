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
#   M3  m3_pad_log.py: throwaway seal pads in failing install calls; the node's log searched by pad hash (review J M3)
#   D6  wp.es_security enable, licence, exp.h_d6_broker 5 (UNSTAMPED; amendments H6c, H6e)
#   RP  rot_proof.py against $ROT_OLD (a sha256 list of earlier key ids) when set; otherwise IN-WINDOW: hash the ids D6
#       minted, rot_proof (before: they are the valid ones), wp.es_keys create (rotation), rot_proof (after): counts only
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
# review 42cfd11 R6: every step command runs under `timeout -k 120`: a step that ignores TERM (stopsig turns the first
# into SystemExit and ignores later ones while it cleans up) is killed 120 s after its deadline or the watcher's TERM
T="timeout -k 120"
# review 42cfd11 R3: the step list is printed and checked before anything starts; an empty list or an unknown step is
# refused (exit 3), so CHAIN OK always names what it covered. The judge (step_judge.py) runs under the host's python3.
KNOWN=" EW G1 G2 G3 P0 P1 P2 PC RR RS RH RX XB SS SU M3 D6 RP LS "
JPY=$(command -v python3)
JUDGE="$NEW/results/failing-first/harness/step_judge.py"
n_steps=0
for st in $STEPS; do
  case "$KNOWN" in *" $st "*) n_steps=$((n_steps + 1));; *) echo "CHAIN FAILED: unknown step $st (exit 3)"; exit 3;; esac
done
[ "$n_steps" -gt 0 ] || { echo "CHAIN FAILED: no steps (exit 3)"; exit 3; }
[ -n "$JPY" ] && [ -f "$JUDGE" ] || { echo "CHAIN FAILED: no judge (python3 and $JUDGE needed; exit 3)"; exit 3; }
echo "steps ($n_steps): $(echo $STEPS)"
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
PGC=$(cat "$WP_HOME/conf/pg_container" 2>/dev/null || echo emlab2-wphard-pg)
CG=${CGROUP_ROOT:-/sys/fs/cgroup/system.slice}
# review 42cfd11 R5: each store's cgroup oom_kill count, read before and after every step ("name=id:count" per store,
# "unreadable" when it cannot be read); a kill during the step, or an unreadable count, fails the step (step_judge oom)
oomk() { for c in "$PGC" "$ESC"; do
    id=$(docker inspect -f '{{.Id}}' "$c" 2>/dev/null | head -1)
    n=$(grep '^oom_kill ' "$CG/docker-$id.scope/memory.events" 2>/dev/null | cut -d' ' -f2)
    [ -n "$id" ] && [ -n "$n" ] && printf '%s=%s:%s ' "$c" "$id" "$n" || printf '%s=unreadable ' "$c"
  done; }
# amendment 23a lines in the node's log since $1 (UTC); prints a count, or "unreadable"
wm_lines() { docker logs --since "$1" "$ESC" > "$O/.eslog" 2>&1 || { echo unreadable; return; }
  grep -ciE "(high|flood.stage) disk watermark.*exceeded" "$O/.eslog"; }
# Declared outcomes (main, 2026-11-19: a step that ends otherwise makes the chain exit 3, never 0). Every step is
# "pass" except XB, RS, RH, RX, which are "red": a non-zero exit AND the step's failing tests ("FAIL: t" / "ERROR: t")
# exactly the "<FAIL|ERROR> <test>" lines of results/failing-first/harness/expected-red/<step>.txt (kind pinned, nothing
# missing, nothing extra). An expected red that passes, or fails otherwise, is a mismatch; so is an unknown step or a
# red step without a signature file (RX has none yet). P1 and P2 are judged on their verdict line as well as their exit.
expected() { case $1 in XB|RS|RH|RX) echo red;; *) echo pass;; esac; }
ex0() { grep -q "^$1 exit=0 " "$O/.sum"; }
exn() { grep -o "^$1 exit=[0-9]*" "$O/.sum" | head -1 | cut -d= -f2; }
# review 42cfd11 R3: unittest steps are judged by step_judge.py against a pinned file: Ran count, skips, and (red) each
# failing test's kind and assertion text, with any other FAIL or ERROR block (setUpClass, a module crash) as extra
ujudge() { $JPY -I "$JUDGE" judge "results/failing-first/harness/expected-$2/$1.txt" "$O/$1.out" "${3:-$(exn "$1")}"; }
observed() {
  case $1 in
    G1|G2|G3) ujudge "$1" pass;;
    EW|PC|M3|SS|SU) ex0 "$1" && echo pass || echo "fail($(grep -o "^$1 exit=[0-9]*" "$O/.sum" | head -1))";;
    # registry_marker and d4_store_mark exit 0 whatever their verdict: their verdict line is judged as well
    P1) if ! ex0 P1; then echo "fail($(grep -o "^P1 exit=[0-9]*" "$O/.sum" | head -1))"
        elif grep -q "^registry_marker PASS " "$O/P1.out"; then echo pass; else echo "fail(verdict not PASS)"; fi;;
    P2) if ! ex0 P2; then echo "fail($(grep -o "^P2 exit=[0-9]*" "$O/.sum" | head -1))"
        elif grep -q "^d4_store_mark {'pass': True," "$O/P2.out"; then echo pass; else echo "fail(verdict not pass)"; fi;;
    P0) grep -q "^setup ok" "$O/P0.out" 2>/dev/null && echo pass || echo "fail(no setup ok)";;
    # review 42cfd11 R3: D6 needs es_security enable to exit 0 and the licence "200 basic active" (amendment H6e)
    D6) if ! grep -qx "enable exit=0" "$O/D6.out" 2>/dev/null; then echo "fail(enable $(grep -E '^enable exit=' "$O/D6.out" 2>/dev/null | head -1))"
        elif ! grep -qx "licence 200 basic active" "$O/D6.out" 2>/dev/null; then echo "fail(licence)"
        elif grep -qx "exit=0" "$O/D6.out" 2>/dev/null; then echo pass
        else echo "fail($(grep -E '^exit=' "$O/D6.out" 2>/dev/null | head -1))"; fi;;
    RR) [ "$(tail -1 "$O/RR.$(basename "$NEW").out" 2>/dev/null)" = "unregistered []" ] && echo pass || echo "fail(head registry)";;
    RP) a=$(grep -E '^after |^\{' "$O/RP.out" 2>/dev/null | tail -1)
        case $a in *'"ids_changed": true'*'"valid_per_name": {"projector": 1, "eraser": 1, "verifier": 1, "admin": 1}'*'"old_ids_valid": 0'*)
          if [ -n "${ROT_OLD:-}" ] || grep -q '^before .*"ids_changed": false' "$O/RP.out"; then echo pass; else echo "fail(no before)"; fi;;
          *) echo "fail(rotation)";; esac;;
    LS) grep -qE "^LS .*files with a secret value: 0( |$)" "$O/.sum" && echo pass || echo "fail(leak scan)";;
    XB|RS|RH|RX) if [ ! -s "results/failing-first/harness/expected-red/$1.txt" ]; then echo "fail(no signature file)"
        else ujudge "$1" red; fi;;
    *) echo "fail(unknown step)";;
  esac; }
VERDICT=""; MISMATCH=0
echo "lock taken $(date -u +%FT%TZ) head $(cat "$NEW/GIT_HEAD") seal0 $(cat "$SEAL0/GIT_HEAD") h0 $(cat "$H0/GIT_HEAD") free_kib $K"
# review 42cfd11 R5: say where the stores live; seconds measured on tmpfs stores are not comparable with disk-backed runs
echo "store_fs pg=$(docker exec "$PGC" stat -f -c %T /var/lib/postgresql/data 2>/dev/null) es=$(docker exec "$ESC" stat -f -c %T /usr/share/elasticsearch/data 2>/dev/null)" \
  "(timings on tmpfs stores are not comparable with disk-backed receipts; correctness results are)"
cd "$NEW" || exit 2
for st in $STEPS; do
  stopped && { echo "STOPPED before $st: $(cat "$O/STOP" 2>/dev/null || echo "below stop line: $(free_kib) KiB")"; exit 6; }
  t0=$(date +%s); ts=$(date -u +%FT%TZ); : > "$O/.sum"; oom0=$(oomk)
  case $st in
    EW) $T 600 $PY -m wp.es_security disable > "$O/EW.out" 2>&1
        say "EW exit=$? $(docker inspect "$ESC" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>&1 | grep -o 'watermark\.[a-z_]*=[0-9a-z]*' | tr '\n' ' ')";;
    G1) $T 900 $PY -m unittest -v tests.test_ident > "$O/G1.out" 2>&1; say "G1 exit=$? $(sum "$O/G1.out")";;
    P0) $T 600 $PY -m wp.setup --reset 2>&1 | grep -v '^seal key' > "$O/P0.out"; say "P0 $(tail -2 "$O/P0.out" | S | tr '\n' ' ')";;
    G2) WP_PG_LIVE=1 $T 2400 $PY -m unittest -v tests.test_d3_pg > "$O/G2.out" 2>&1; say "G2 exit=$? $(sum "$O/G2.out")";;
    G3) WP_PG_LIVE=1 $T 600 $PY -m unittest -v tests.test_schema_broker_pg > "$O/G3.out" 2>&1; say "G3 exit=$? $(sum "$O/G3.out")";;
    P1) $T 1800 $PY -m exp.registry_marker > "$O/P1.out" 2>&1; say "P1 exit=$? $(tail -4 "$O/P1.out" | S | cut -c1-400 | tr '\n' ' ')";;
    P2) $T 900 $PY -m exp.d4_store_mark 1 > "$O/P2.out" 2>&1; say "P2 exit=$? $(tail -4 "$O/P2.out" | S | cut -c1-400 | tr '\n' ' ')";;
    RR) R=$O/rr-tree-$(date +%s); cp -R "$NEW" "$R"; cp "$SEAL0/wp/registry.py" "$R/wp/registry.py"   # kept in $O (small)
        for tr in "$R" "$NEW"; do
          stopped && break
          $T 300 env -C "$tr" $PY -c '
from wp import db, config, registry
from wp.es import ES
vc, vk = db.verifier(), db.verifier(db=config.KEYS_DB)
print("unregistered", registry.unregistered(vc, vk, ES()))' > "$O/RR.$(basename "$tr").out" 2>&1
          say "RR $(basename "$tr") $(tail -1 "$O/RR.$(basename "$tr").out" | S)"
        done;;
    RS) WP_PG_LIVE=1 SCHEMA_UNDER_TEST=$SEAL0/wp/schema.sql $T 2400 $PY -m unittest -v tests.test_d3_pg > "$O/RS.out" 2>&1; say "RS exit=$? $(sum "$O/RS.out")";;
    RH) WP_PG_LIVE=1 SCHEMA_UNDER_TEST=$H0/wp/schema.sql $T 2400 $PY -m unittest -v tests.test_d3_pg > "$O/RH.out" 2>&1; say "RH exit=$? $(sum "$O/RH.out")";;
    RX) XT=${RX_TREE:?RX_TREE names the tree whose schema is the before}
        WP_PG_LIVE=1 SCHEMA_UNDER_TEST=$XT/wp/schema.sql $T 2400 $PY -m unittest -v tests.test_d3_pg > "$O/RX.out" 2>&1; say "RX exit=$? schema $(cat "$XT/GIT_HEAD") $(sum "$O/RX.out")";;
    XB) XT=${XB_TREE:?XB_TREE names the tree}
        WP_PG_LIVE=1 $T 2400 env -C "$XT" $PY -m unittest -v tests.test_d3_pg > "$O/XB.out" 2>&1; say "XB exit=$? tree $(cat "$XT/GIT_HEAD") $(sum "$O/XB.out")";;
    D6) { echo "tree $(cat "$NEW/GIT_HEAD") free_kib $(free_kib)"
          stopped || { $T 600 $PY -m wp.es_security enable > "$O/D6.enable.out" 2>&1; echo "enable exit=$?"; tail -1 "$O/D6.enable.out"; }
          stopped || $T 60 $PY -c '
from wp.es import ES
s, j = ES().req("GET", "/_license", ok=None)
l = j.get("license", {}) if isinstance(j, dict) else {}
print("licence", s, l.get("type"), l.get("status"))'
          stopped || { $T 3600 $PY -m exp.h_d6_broker 5; echo "exit=$?"; }; } > "$O/D6.out" 2>&1
        say "D6 $(grep -E '^(enable exit=|licence|h_d6_broker|exit=)' "$O/D6.out" | S | tr '\n' ' ')";;
    RP) if [ -n "${ROT_OLD:-}" ]; then
          $T 120 $PY -I results/failing-first/harness/rot_proof.py "$ROT_OLD" > "$O/RP.out" 2>&1
          say "RP exit=$? $(tail -1 "$O/RP.out")"
        else
          # in-window rotation (the stores are on tmpfs: a recreated node holds no earlier window's keys): hash the ids
          # D6 minted (sha256 only, 0600), prove the proof sees them, rotate, prove again
          { ( umask 077; $T 60 $PY -c '
import glob, hashlib, json, sys
from wp import config
for p in sorted(glob.glob(str(config.SECRETS / "es_api_key*.json"))):
    print(hashlib.sha256(json.load(open(p))["id"].encode()).hexdigest())' > "$O/rot-old.sha256" )
            echo "old ids hashed $(wc -l < "$O/rot-old.sha256")"
            stopped || { printf 'before '; $T 120 $PY -I results/failing-first/harness/rot_proof.py "$O/rot-old.sha256" | tail -1; }
            stopped || $T 300 $PY -m wp.es_keys create 2>&1 | tail -1
            stopped || { printf 'after '; $T 120 $PY -I results/failing-first/harness/rot_proof.py "$O/rot-old.sha256" | tail -1; }
          } > "$O/RP.out" 2>&1
          say "RP in-window $(grep -E '^(old ids|before|created|after)' "$O/RP.out" | S | tr '\n' ' ')"
        fi;;
    PC) $T 120 $PY -c '
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
    M3) $T 300 $PY results/failing-first/harness/m3_pad_log.py > "$O/M3.out" 2>&1; say "M3 exit=$? $(tail -1 "$O/M3.out" | S | cut -c1-700)";;
    SS) $T 600 $PY results/failing-first/harness/seal_socket_e2e.py > "$O/SS.out" 2>&1; say "SS exit=$? $(tail -1 "$O/SS.out" | S | cut -c1-600)";;
    SU) $T 1200 $PY results/failing-first/harness/seal_uids_e2e.py > "$O/SU.out" 2>&1; say "SU exit=$? $(tail -1 "$O/SU.out" | S | cut -c1-900)";;
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
  oom1=$(oomk); oj=$($JPY -I "$JUDGE" oom "$oom0" "$oom1")
  e=$(expected "$st")
  if [ "$oj" = ok ]; then o=$(observed "$st"); else o="$oj"; fi
  [ "$e" = "$o" ] || MISMATCH=1
  VERDICT="$VERDICT $st:$e/$o"
  echo "  $st seconds=$(( $(date +%s) - t0 )) watermark_lines 0 oom_kill $oom1 expected $e observed $o"
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
echo "steps expected/observed:$VERDICT"
[ $MISMATCH -eq 0 ] || { echo "CHAIN FAILED: a step's outcome differs from its declared one (exit 3)"; exit 3; }
echo "CHAIN OK: every step as declared"
