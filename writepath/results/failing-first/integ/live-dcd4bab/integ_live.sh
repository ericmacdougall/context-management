#!/bin/sh
# Live items GF16096-I1, I3, I4, I6, I7, I9a, I9b of integ/gate-on-hardening on t2-dev (GoodQA #16096), own home
# ~/emlab2-16096/gatefix/wp (conf: the stream's container names and ports; WP_SECRETS_DIR names the write-path home's
# secrets directory, a path; no secret is read, printed or copied by this kit). Stores emlab2-wphard-pg/-es (tmpfs,
# rebuilt by their owner), used under the coordinator's instruction: never recreated, credentials and ES security
# untouched; the gate itself resets the databases and deletes the stream's indices under its run lock.
# Sent over stdin: ssh t2-dev sh -s <step> [args] < integ_live.sh. Prints exit codes and gate lines only.
# Steps: lock | unlock | pin | item <name> <pinned dir> | guard <pinned dir> | state | stops <pinned dir>
#        | clear-stop <pinned dir> <stop id> <reason> | secrets-mtimes
# Nothing here stops a process: a lingering child of I6 is waited out, never signalled.
set -u
. ~/emlab2-16096/gatefix/env.sh            # WP_HOME, WP_SECRETS_DIR, PY (a path each; no value)
L=$HOME/emlab2-16096/RUNLOCK
ME="gatefix-integ af1a90667f3f5e147"
ES_URL=$(cat "$WP_HOME/conf/es_url")
now() { date -u +%FT%TZ; }
free_kib() { df -k --output=avail "$HOME" | tail -1 | tr -dc 0-9; }
need_start() {  # amendment H6d / GF1: 10 GiB start line, measured like the gate (f_bavail)
  K=$(free_kib); echo "free $K KiB at $(now)"
  [ "$K" -ge 10485760 ] || { echo "REFUSED: below the 10 GiB start line"; exit 4; }
}
own_lock() { [ -f "$L/owner" ] && grep -q "^$ME " "$L/owner" || { echo "REFUSED: RUNLOCK not held by this kit"; exit 5; }; }
gate_lines() { grep -E '^(disk_gate|gate_|chain|[A-Za-z0-9_.-]+: gate exit)' | grep -v '^gate_limits' | cut -c1-500; }
gate() { (cd "$1" && shift && "$PY" -I -B runkit/isolated.py exp.disk_gate "$@" 2>&1 | grep -v '^gate_limits' | cut -c1-500); }
verdict() {  # $1 pinned dir, $2 run id: the ledger's verdict and its exit status
  v=$(cd "$1" && "$PY" -I -B runkit/isolated.py exp.disk_gate verdict "$2" 2>&1); vrc=$?
  echo "verdict $2: $(printf '%s\n' "$v" | grep -v '^gate_limits' | tail -1 | cut -c1-300) (exit $vrc)"
}
launch() {  # $1 pinned dir, $2 label, rest module args; every gate line (all BLOCK and VOID reasons)
  D=$1; shift
  echo "## launch $* at $(now)"
  out=$(sh "$D/runkit/launch_one.sh" "$D" "$@" 2>&1); rc=$?
  printf '%s\n' "$out" | gate_lines
  echo "exit $rc (0 VALID, 3 VOID, 2 BLOCKED)"
  run=$(printf '%s\n' "$out" | grep -oE 'r-[0-9a-f]{32}' | head -1)
  [ -n "$run" ] && verdict "$D" "$run"
  return $rc
}
es() { curl -s -m 15 -o /dev/null -w '%{http_code}' -X "$1" -H 'Content-Type: application/json' "$ES_URL$2" ${3:+-d "$3"}; }
case "${1:-}" in
  lock)
    need_start
    mkdir "$L" 2>/dev/null || { echo "REFUSED: lock held: $(cut -c1-60 "$L/owner" 2>/dev/null)"; exit 5; }
    echo "$ME $(now) integ live items" > "$L/owner"; echo "RUNLOCK taken $(now)" ;;
  unlock)
    own_lock; rm -f "$L/owner" && rmdir "$L" && echo "RUNLOCK released $(now)" ;;
  state)
    echo "free $(free_kib) KiB"; [ -d "$L" ] && echo "RUNLOCK: $(cut -c1-60 "$L/owner")" || echo "RUNLOCK free"
    for c in emlab2-wphard-pg emlab2-wphard-es; do
      docker inspect -f '{{.Name}} {{slice .Id 0 12}} mem={{.HostConfig.Memory}} swap={{.HostConfig.MemorySwap}} tmpfs={{json .HostConfig.Tmpfs}}' "$c"; done
    echo "stream containers (label): $(docker ps -a --filter label=emlab2.stream=wphard --format '{{.Names}}' | tr '\n' ' ')"
    echo "stream containers (name): $(docker ps -a --filter name=emlab2-wphard --format '{{.Names}}' | tr '\n' ' ')"
    echo "ES security: $(curl -s -m 10 -o /dev/null -w '%{http_code}' "$ES_URL/_security/_authenticate") (_security/_authenticate without credentials)"
    echo "templates wp16096-zz-integ-i3/i3b: $(es GET /_index_template/wp16096-zz-integ-i3) $(es GET /_index_template/wp16096-zz-integ-i3b)"
    echo "own sleep 120 processes: $(pgrep -u "$(id -u)" -fc '^sleep 120$')" ;;
  secrets-mtimes)  # a count and a hash of the names and modification times, nothing else (I1 must not change them)
    l=$(find "$WP_SECRETS_DIR" -maxdepth 2 -printf '%P %T@ %s\n' | sort)
    echo "secrets dir entries $(printf '%s\n' "$l" | wc -l) listing sha256 $(printf '%s\n' "$l" | sha256sum | cut -c1-16)" ;;
  stops)
    gate "$2" stops ;;
  clear-stop)
    own_lock; D=$2; id=$3; shift 3; gate "$D" clear-stop "$id" "$*" ;;
  pin)
    own_lock
    D=$(cd "$WP_HOME/src" && sh pin_run.sh) || exit 2
    D=$(cd "$WP_HOME/src" && cd "$D" && pwd); echo "PINNED $D head $(cat "$D/GIT_HEAD")" ;;
  item)
    own_lock; need_start
    N=$2; D=$3
    case "$N" in
      I1) launch "$D" integ-i1-ok exp.live_probe ok ;;
      I1r) launch "$D" integ-reset-ok exp.live_probe ok ;;   # after I9b: the admission's reset removes the grant
      I6) launch "$D" integ-i6-linger exp.live_probe linger
          echo "lingering sleep 120 of this user still alive: $(pgrep -u "$(id -u)" -fc '^sleep 120$')" ;;
      I9a) launch "$D" integ-i9a-selfkill exp.live_probe selfkill ;;
      I9b) launch "$D" integ-i9b-grant exp.live_probe grant ;;
      I3a)  # an index template under the stream prefix made before the run: the reset keeps it, admission must see it
        T=wp16096-zz-integ-i3
        echo "PUT _index_template/$T: $(es PUT "/_index_template/$T" '{"index_patterns":["wp16096-zzintegi3*"],"priority":901}')"
        launch "$D" integ-i3a-template exp.live_probe ok
        echo "DELETE _index_template/$T: $(es DELETE "/_index_template/$T")" ;;
      I3b)  # the same template made during the run (after admission), removed after the end
        T=wp16096-zz-integ-i3b
        before=$(ls "$WP_HOME/results/runs" 2>/dev/null | sort)
        ( sh "$D/runkit/launch_one.sh" "$D" integ-i3b-template-during exp.live_probe sleep 45 > "$WP_HOME/results/i3b.$$.out" 2>&1; echo $? > "$WP_HOME/results/i3b.$$.rc" ) &
        i=0; seen=0; while [ $i -lt 120 ]; do
          [ "$(ls "$WP_HOME/results/runs" 2>/dev/null | sort)" != "$before" ] && { seen=1; break; }; sleep 1; i=$((i + 1)); done
        if [ $seen = 1 ]; then
          sleep 5; echo "run directory appeared after ${i} s; PUT _index_template/$T at $(now): $(es PUT "/_index_template/$T" '{"index_patterns":["wp16096-zzintegi3b*"],"priority":902}')"
        else
          echo "I3b NOT RUN: no run directory appeared within 120 s (the run was refused or did not start); no template made"
        fi
        wait
        gate_lines < "$WP_HOME/results/i3b.$$.out"; echo "exit $(cat "$WP_HOME/results/i3b.$$.rc") (0 VALID, 3 VOID, 2 BLOCKED)"
        run=$(grep -oE 'r-[0-9a-f]{32}' "$WP_HOME/results/i3b.$$.out" | head -1); [ -n "$run" ] && verdict "$D" "$run"
        echo "template after the end: $(es GET "/_index_template/$T")"
        [ $seen = 1 ] && echo "DELETE _index_template/$T: $(es DELETE "/_index_template/$T")" ;;
      I4)  # a crafted link name in a pinned tree of this home only: newline, ' -> ', pointing outside the tree
        P4=$(cd "$WP_HOME/src" && sh pin_run.sh) || exit 2; P4=$(cd "$WP_HOME/src" && cd "$P4" && pwd)
        ln -s /etc/hostname "$P4/exp/x
 -> GIT_HEAD" && echo "crafted link made in $(basename "$P4")/exp"
        launch "$P4" integ-i4-crafted-link exp.live_probe ok ;;
      I7)
        S=$WP_HOME/results/integ-i7.spec
        printf 'integ-i7-a - exp.live_probe ok\ninteg-i7-b - exp.live_probe sleep 5\ninteg-i7-c - exp.live_probe ok\n' > "$S.$$"
        echo "## chain at $(now)"
        out=$(sh "$D/runkit/chain_singles.sh" "$D" "$S.$$" 2>&1); rc=$?
        printf '%s\n' "$out" | cut -c1-400; echo "chain exit $rc" ;;
      *) echo "unknown item"; exit 2 ;;
    esac ;;
  guard)
    D=$2; echo "## guard check at $(now)"
    out=$(cd "$D" && "$PY" -I -B runkit/isolated.py exp.guard_check integ-end 2>&1); rc=$?
    printf '%s\n' "$out" | cut -c1-400 | tail -30; echo "guard exit $rc" ;;
  *) echo "usage: lock | unlock | state | secrets-mtimes | stops <dir> | clear-stop <dir> <id> <reason> | pin | item <I1|I1r|I3a|I3b|I4|I6|I7|I9a|I9b> <pinned dir> | guard <pinned dir>"; exit 2 ;;
esac
