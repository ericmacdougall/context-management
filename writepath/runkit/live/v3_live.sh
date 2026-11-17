#!/bin/sh
# Gate v3 live confirmation kit (GoodQA #16096, review D), run on t2-dev under the lab run lock:
#   ssh t2-dev 'sh -s <step>' < this file       steps: setup | cases | teardown
# Only under ~/emlab2-16096/gatefix, only in its own containers emlab2-gatefix-* (memory limited, data on private tmpfs;
# ES publishes 9200 on 127.0.0.1:29299 only, with security off; PG has trust auth inside the container and no published
# port: no credential exists). The gate runs through runkit/gate_live.py (only _targets() replaced). No sudo, no loop
# devices, no host settings. Rows start with "ROW ", reasons with "REASON ", measurements with "EVIDENCE ".
set -u
G=$HOME/emlab2-16096/gatefix
T=$G/tree/writepath
ES=emlab2-gatefix-es
PG=emlab2-gatefix-pg
PGV=emlab2-gatefix-pgvol
IMG_ES=docker.elastic.co/elasticsearch/elasticsearch:9.5.6
PG_TMPFS="--mount type=tmpfs,dst=/var/lib/postgresql/data,tmpfs-size=1073741824"   # 1 GiB > 512 MiB + 256 MiB
export WP_HOME=$G/home GATE_LIVE_ES=$ES GATE_LIVE_PG=$PG
gate() { (cd "$T" && python3 runkit/gate_live.py "$@" < /dev/null 2>&1); }
cg() { echo "/sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' "$1").scope"; }
row() { echo "ROW $*"; }
reasons() { printf '%s\n' "$2" | grep -E 'VOID|BLOCKED|refused' | cut -c1-320 | sed "s/^/REASON $1 /"; }
es_up() { i=0; until curl -s -m 5 http://127.0.0.1:29299/ > /dev/null || [ $i -ge 90 ]; do sleep 2; i=$((i + 1)); done; }
pg_up() { i=0; until docker exec "$1" pg_isready -q 2> /dev/null || [ $i -ge 60 ]; do sleep 2; i=$((i + 1)); done; }
run_pg() {  # $1 name, $2 data mount options
  docker run -d --name "$1" --memory 512m --memory-swap 512m --log-driver json-file $2 \
    -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16 | cut -c1-12
  pg_up "$1"
}
ack_all() {
  for id in $(gate stops | awk '$2 == "open" {print $4}'); do
    echo "ack $id: $(gate clear-stop "$id" "v3 live case $1 observed; acknowledged by the kit" | tail -1 | cut -c1-120)"
  done
}
evidence() {
  for c in "$ES" "$PG"; do
    d=$(cg "$c")
    echo "EVIDENCE $c max=$(cat "$d/memory.max") swap=$(cat "$d/memory.swap.max") current=$(cat "$d/memory.current") peak=$(cat "$d/memory.peak") $(grep '^shmem ' "$d/memory.stat") oom_kill=$(grep '^oom_kill ' "$d/memory.events" | cut -d' ' -f2)"
  done
  echo "EVIDENCE es-cgroup $(cat /proc/"$(docker inspect -f '{{.State.Pid}}' "$ES")"/cgroup | cut -c1-120)"
  curl -s -m 10 'http://127.0.0.1:29299/_nodes/_local/stats/fs?filter_path=nodes.*.fs.data.type,nodes.*.fs.data.total_in_bytes,nodes.*.fs.data.free_in_bytes,nodes.*.fs.data.low_watermark_free_space_in_bytes' | sed 's/^/EVIDENCE es-fs /'; echo
  docker exec "$PG" stat -f -c 'EVIDENCE pg-fs type=%T blocks=%b free=%f bsize=%S' /var/lib/postgresql/data
  echo "EVIDENCE pg-env $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$PG" | grep '^PGDATA=')"
}
case "${1:-}" in
  setup)
    df -h /home/eric | tail -1
    mkdir -p "$WP_HOME/conf"
    printf 'emlab2-wphard\n' > "$WP_HOME/conf/stream"
    printf '25999\n' > "$WP_HOME/conf/pg_port"
    printf 'http://127.0.0.1:29299\n' > "$WP_HOME/conf/es_url"
    printf 'emlab2-wphard-gatefixnone-pg\n' > "$WP_HOME/conf/pg_container"
    printf 'emlab2-wphard-gatefixnone-es\n' > "$WP_HOME/conf/es_container"
    # ES: limit 1500 MiB, tmpfs 2304 MiB >= 1500 + 400 (low watermark) + 256 MiB
    docker run -d --name "$ES" --memory 1500m --memory-swap 1500m --log-driver json-file -p 127.0.0.1:29299:9200 \
      --mount type=tmpfs,dst=/usr/share/elasticsearch/data,tmpfs-size=2415919104 \
      -e discovery.type=single-node -e xpack.security.enabled=false -e ES_JAVA_OPTS="-Xms512m -Xmx512m" \
      -e cluster.routing.allocation.disk.watermark.low=400mb -e cluster.routing.allocation.disk.watermark.high=300mb \
      -e cluster.routing.allocation.disk.watermark.flood_stage=200mb "$IMG_ES" | cut -c1-12
    es_up
    run_pg "$PG" "$PG_TMPFS"
    echo "init: $(gate init 'gate v3 live confirmation home' | tail -1)"
    ;;
  cases)
    df -h /home/eric | tail -1
    # (a) a valid run
    S=$(gate start); RS=$?; C=$(gate check a); RC=$?; sleep 3; E=$(gate end); RE=$?
    row "case=a valid start_rc=$RS check_rc=$RC end_rc=$RE"
    printf '%s\n' "$S" | tail -1 | cut -c1-320 | sed 's/^/START a /'
    reasons a "$S$E"
    evidence
    # (b) PG's data on a Docker volume instead of a tmpfs: BLOCKED
    run_pg "$PGV" "-v /var/lib/postgresql/data"
    S=$(GATE_LIVE_PG=$PGV gate start); RS=$?
    row "case=b pg-on-volume start_rc=$RS"; reasons b "$S"
    docker rm -fv "$PGV" > /dev/null
    # (c) PG restarted inside the window (its tmpfs is wiped; StartedAt changes): VOID
    S=$(gate start); RS=$?
    docker restart "$PG" > /dev/null; pg_up "$PG"
    E=$(gate end); RE=$?
    row "case=c restart start_rc=$RS end_rc=$RE"; reasons c "$S$E"
    # (d) a stop recorded inside the window: VOID; then start BLOCKED until acknowledged
    S=$(gate start); RS=$?
    gate stop "v3 live case d: operator stop mid-window" | tail -1
    E=$(gate end); RE=$?
    row "case=d stop start_rc=$RS end_rc=$RE"; reasons d "$E"
    B=$(gate start); BR=$?; row "case=d-blocked start_rc=$BR"; reasons d-blocked "$B"
    ack_all d
    # (g) legacy STOP writers: `>` fails; touch and cp into the directory are stops
    (echo "legacy stop" > "$WP_HOME/state/STOP") 2> /dev/null; row "case=g-redirect write_rc=$?"
    touch "$WP_HOME/state/STOP"
    B=$(gate start); BR=$?; row "case=g-touch start_rc=$BR"; reasons g-touch "$B"
    ack_all g-touch
    printf 'legacy cp stop\n' > "$G/why"; cp "$G/why" "$WP_HOME/state/STOP/"
    B=$(gate start); BR=$?; row "case=g-cp start_rc=$BR"; reasons g-cp "$B"
    ack_all g-cp
    # (h) the ledger's last record cut off: BLOCKED; ledger-reset opens a new ledger with a stop; an intact one refused
    gate stop "v3 live case h: a stop that truncation must not lose" | tail -1
    L=$WP_HOME/state/stop_ledger.jsonl; n=$(wc -l < "$L")
    head -n $((n - 1)) "$L" > "$G/ledger.cut" && cat "$G/ledger.cut" > "$L"
    B=$(gate start); BR=$?; row "case=h-truncated start_rc=$BR"; reasons h "$B"
    R=$(gate ledger-reset "v3 live case h: truncated by the kit"); RR=$?; row "case=h-reset rc=$RR"
    printf '%s\n' "$R" | tail -1 | cut -c1-320 | sed 's/^/RESET h /'
    B=$(gate start); BR=$?; row "case=h-after-reset start_rc=$BR"; reasons h-after "$B"
    ack_all h
    I=$(gate ledger-reset "v3 live case h: trying an intact ledger"); IR=$?; row "case=h-reset-intact rc=$IR"
    reasons h-intact "$I"
    # (e) an OOM kill of one process in PG (a backend asks for 900 MB under a 512 MiB limit): VOID by oom_kill
    S=$(gate start); RS=$?
    docker exec "$PG" psql -U postgres -Atc "select length(repeat('x', 900000000))" > /dev/null 2>&1; XR=$?
    sleep 2; pg_up "$PG"
    E=$(gate end); RE=$?
    row "case=e oom start_rc=$RS hog_rc=$XR end_rc=$RE oom_kill=$(grep '^oom_kill ' "$(cg "$PG")/memory.events" | cut -d' ' -f2) OOMKilled=$(docker inspect -f '{{.State.OOMKilled}}' "$PG") running=$(docker inspect -f '{{.State.Running}}' "$PG")"
    reasons e "$S$E"
    docker rm -fv "$PG" > /dev/null; run_pg "$PG" "$PG_TMPFS"
    # (f) the ES limit raised, the tmpfs filled past the bound, the file removed and the limit restored: VOID by peak
    S=$(gate start); RS=$?
    docker update --memory 2200m --memory-swap 2200m "$ES" > /dev/null
    docker exec "$ES" sh -c 'dd if=/dev/zero of=/usr/share/elasticsearch/data/gatefix-fill bs=1M count=700 2>&1 | tail -1; rm -f /usr/share/elasticsearch/data/gatefix-fill'
    docker update --memory 1500m --memory-swap 1500m "$ES" > /dev/null
    E=$(gate end); RE=$?
    row "case=f raise-fill-restore start_rc=$RS end_rc=$RE peak=$(cat "$(cg "$ES")/memory.peak") max=$(cat "$(cg "$ES")/memory.max")"
    reasons f "$S$E"
    echo "ledger: $(wc -l < "$WP_HOME/state/stop_ledger.jsonl") records; $(gate stops | tail -1 | cut -c1-120)"
    ;;
  teardown)
    docker rm -fv "$ES" "$PG" "$PGV" > /dev/null 2>&1
    docker ps -a --filter name=emlab2-gatefix --format '{{.Names}}' | wc -l | sed 's/^/remaining containers: /'
    rm -rf "$G/home" "$G/tree" "$G/why" "$G/ledger.cut"
    du -sh "$G" 2> /dev/null; df -h /home/eric | tail -1
    ;;
  *) echo "usage: setup | cases | teardown"; exit 2 ;;
esac
