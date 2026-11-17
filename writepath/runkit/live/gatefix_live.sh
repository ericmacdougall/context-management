#!/bin/sh
# gatefix live confirmation kit (GoodQA #16096, review A1-A3), run on t2-dev as: ssh t2-dev sh -s <step> < this file
# Only under ~/emlab2-16096/gatefix, only in its own containers emlab2-gatefix-es/-pg (memory limited; ES publishes
# 9200 on 127.0.0.1:29299 only, which the gate's API binding requires, with security off; PG has no published ports and
# trust auth inside the container: no credential exists). The caller holds the lab run lock.
# Steps: setup | probe | cases | teardown. Rows start with "ROW " and carry exit codes and gate reasons only.
set -u
G=$HOME/emlab2-16096/gatefix
T=$G/tree/writepath
ES=emlab2-gatefix-es
PG=emlab2-gatefix-pg
export WP_HOME=$G/home GATE_LIVE_ES=$ES GATE_LIVE_PG=$PG
gate() { (cd "$T" && python3 runkit/gate_live.py "$@" < /dev/null 2>&1); }
ts() { date -u +%FT%TZ; }
wait_es() {  # until ES logs "started" after $1 (or 240 s)
  i=0
  while [ $i -lt 120 ]; do
    docker logs --since "$1" "$ES" 2>&1 | grep -q '"started' && return 0
    docker logs --since "$1" "$ES" 2>&1 | grep -qE 'started[^_]' && return 0
    sleep 2; i=$((i + 1))
  done
  echo "ES not started after 240 s"; return 1
}
ack_all() {  # acknowledge every open stop by id, printing ids and the gate's reply only
  for id in $(gate stops | awk '$2 == "open" {print $4}'); do
    echo "ack $id: $(gate clear-stop "$id" "gatefix live case $1 observed; acknowledged by the kit" | tail -1)"
  done
}
run_case() {  # $1 label, $2 action
  L=$1; A=$2
  S=$(gate start); RS=$?
  printf '%s\n' "$S" | tail -2
  C=$(gate check "$L"); RC=$?
  printf '%s\n' "$C" | tail -1
  T0=$(ts)
  case "$A" in
    none) sleep 5 ;;
    forge) docker exec "$ES" sh -c 'echo "wp16096-disk-gate end 0123456789abcdef0123456789abcdef" > /proc/1/fd/1'; sleep 3 ;;
    stop) gate stop "gatefix live case d: operator stop mid-window" | tail -1; sleep 3 ;;
    watermark) docker exec "$ES" sh -c 'echo "{\"log.level\": \"WARN\", \"message\": \"high disk watermark [90%] exceeded on [gatefix][data] free: 1gb[5%], shards will be relocated away from this node\"}" > /proc/1/fd/1'; sleep 3 ;;
    restart) docker restart "$ES" > /dev/null; wait_es "$T0" ;;
    realwm)  # a real high/flood watermark event in this container, then reset (inside the window)
      curl -s -m 10 -X PUT -H 'Content-Type: application/json' http://127.0.0.1:29299/_cluster/settings -d \
        '{"transient":{"cluster.routing.allocation.disk.watermark.low":"5000gb","cluster.routing.allocation.disk.watermark.high":"4000gb","cluster.routing.allocation.disk.watermark.flood_stage":"3000gb"}}' > /dev/null
      sleep 25 ;;
  esac
  E=$(gate end); RE=$?
  printf '%s\n' "$E" | tail -3
  echo "ROW case=$L action=$A start_rc=$RS check_rc=$RC end_rc=$RE end: $(printf '%s\n' "$E" | tail -1 | cut -c1-300)"
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
    T0=$(ts)
    docker run -d --name "$PG" --memory 512m --log-driver json-file -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16 | cut -c1-12
    docker run -d --name "$ES" --memory 1500m --log-driver json-file --log-opt mode=blocking -p 127.0.0.1:29299:9200 \
      -e discovery.type=single-node -e xpack.security.enabled=false -e ES_JAVA_OPTS="-Xms512m -Xmx512m" \
      -e cluster.info.update.interval=10s \
      -e cluster.routing.allocation.disk.watermark.low=3gb -e cluster.routing.allocation.disk.watermark.high=2gb \
      -e cluster.routing.allocation.disk.watermark.flood_stage=1gb \
      docker.elastic.co/elasticsearch/elasticsearch:9.5.6 | cut -c1-12
    wait_es "$T0" && echo "ES started"
    docker inspect -f '{{.Name}} {{.HostConfig.LogConfig.Type}} {{.HostConfig.LogConfig.Config}} tty={{.Config.Tty}} mem={{.HostConfig.Memory}} ports={{.HostConfig.PortBindings}}' "$ES" "$PG"
    ;;
  probe)  # real docker behaviour the gate relies on: exec event Action strings, both streams, timestamps
    T0=$(date +%s)
    docker exec "$ES" sh -c 'echo gatefix-probe-out > /proc/1/fd/1'
    docker exec "$ES" sh -c 'echo gatefix-probe-err > /proc/1/fd/2'
    sleep 2
    docker events --filter container="$ES" --since "$T0" --until "$(date +%s)" --format '{{json .}}' > "$G/probe_events.jsonl" 2>&1
    python3 -c 'import json,sys; [print("EVENT", (lambda e: (e.get("Type"), (e.get("Action") or "")[:90]))(json.loads(l))) for l in open(sys.argv[1]) if l.strip()]' "$G/probe_events.jsonl"
    docker logs --timestamps --tail 4 "$ES" 2> "$G/probe_err.txt" | grep gatefix-probe | sed 's/^/STDOUT /'
    grep gatefix-probe "$G/probe_err.txt" | sed 's/^/STDERR /'
    # a REAL watermark event in this container only: byte watermarks above current free space, 30 s, then reset
    api() { curl -s -m 10 "http://127.0.0.1:29299$1"; }
    for PH in high:1gb flood:3000gb; do
      T1=$(ts)
      curl -s -m 10 -X PUT -H 'Content-Type: application/json' http://127.0.0.1:29299/_cluster/settings -d \
        '{"transient":{"cluster.routing.allocation.disk.watermark.low":"5000gb","cluster.routing.allocation.disk.watermark.high":"4000gb","cluster.routing.allocation.disk.watermark.flood_stage":"'"${PH#*:}"'"}}' | cut -c1-120; echo
      sleep 20
      echo "WMLINES ${PH%%:*} $(docker logs --since "$T1" "$ES" 2>&1 | grep -ciE 'watermark') lines in 20 s"
      docker logs --since "$T1" "$ES" 2>&1 | grep -iE 'watermark' | sed -E 's/"@timestamp":"[^"]*",//' | sort | uniq -c | cut -c1-400 | sed "s/^/WMTEXT ${PH%%:*} /"
      api "/_nodes/_local/stats/fs?filter_path=nodes.*.fs.data.available_in_bytes,nodes.*.fs.data.free_in_bytes" | sed "s/^/WMAPI ${PH%%:*} /"; echo
    done
    curl -s -m 10 -X PUT -H 'Content-Type: application/json' http://127.0.0.1:29299/_cluster/settings -d \
      '{"transient":{"cluster.routing.allocation.disk.watermark.low":null,"cluster.routing.allocation.disk.watermark.high":null,"cluster.routing.allocation.disk.watermark.flood_stage":null}}' | cut -c1-120; echo
    sleep 15
    api "/_cluster/settings?include_defaults=true&flat_settings=true&filter_path=*.cluster.routing.allocation.disk.watermark.*" | sed 's/^/WMRESET /'; echo
    ;;
  cases)
    df -h /home/eric | tail -1
    run_case a none
    run_case b forge
    run_case d stop
    echo "start while the case-d stop is open (expect BLOCKED, exit 2):"
    B=$(gate start); BR=$?
    echo "ROW case=d-blocked start_rc=$BR: $(printf '%s\n' "$B" | tail -1 | cut -c1-300)"
    ack_all d
    run_case e watermark
    ack_all e
    run_case f realwm
    curl -s -m 10 -X PUT -H 'Content-Type: application/json' http://127.0.0.1:29299/_cluster/settings -d \
      '{"transient":{"cluster.routing.allocation.disk.watermark.low":null,"cluster.routing.allocation.disk.watermark.high":null,"cluster.routing.allocation.disk.watermark.flood_stage":null}}' > /dev/null
    sleep 15
    ack_all f
    run_case c restart
    ack_all c
    echo "ledger: $(wc -l < "$WP_HOME/state/stop_ledger.jsonl") records; open stops: $(gate stops | tail -1)"
    ;;
  teardown)
    docker rm -f "$ES" "$PG" > /dev/null && echo "removed $ES $PG"
    ps -eo pid,command | grep -E 'disk_gate _capture' | grep -v grep || echo "no capture companion running"
    rm -rf "$G/home" "$G/tree" "$G"/probe_*
    du -sh "$G" 2>/dev/null; df -h /home/eric | tail -1
    ;;
  *) echo "usage: setup | probe | cases | teardown"; exit 2 ;;
esac
