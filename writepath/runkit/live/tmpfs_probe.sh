#!/bin/sh
# gatefix tmpfs probe (GoodQA #16096, review D decision evidence). Run on t2-dev under the lab run lock:
#   ssh t2-dev 'sh -s <step>' < this file      steps: fill | es | pg | restart | teardown
# Only the gate's own containers emlab2-gatefix-*; memory limited; ES on 127.0.0.1 only with security off; PG trust
# auth with no published port (no credential exists). No sudo, no loop devices, no host settings.
set -u
ES=emlab2-gatefix-es
PG=emlab2-gatefix-pg
IMG_ES=docker.elastic.co/elasticsearch/elasticsearch:9.5.6
cg() { echo "/sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' "$1").scope"; }
case "${1:-}" in
  fill)
    df -h /home/eric | tail -1
    echo "== tmpfs 128 MiB > memory limit 64 MiB (no swap): write 120 MiB"
    docker run --name emlab2-gatefix-fill --memory 64m --memory-swap 64m \
      --mount type=tmpfs,dst=/d,tmpfs-size=134217728 --entrypoint sh postgres:16 \
      -c 'stat -f -c "FS type=%T blocks=%b bsize=%S" /d; dd if=/dev/zero of=/d/f bs=1M count=120 2>&1 | tail -1; echo "dd done"'
    echo "fill exit $?"
    docker inspect -f 'OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}} Mem={{.HostConfig.Memory}} MemSwap={{.HostConfig.MemorySwap}} Tmpfs={{json .HostConfig.Tmpfs}} Mounts={{json .Mounts}}' emlab2-gatefix-fill
    docker rm emlab2-gatefix-fill > /dev/null
    echo "== tmpfs 32 MiB < memory limit 256 MiB: write 40 MiB"
    docker run --rm --name emlab2-gatefix-fill2 --memory 256m --memory-swap 256m \
      --mount type=tmpfs,dst=/d,tmpfs-size=33554432 --entrypoint sh postgres:16 \
      -c 'dd if=/dev/zero of=/d/f bs=1M count=40 2>&1 | tail -1'
    echo "fill2 exit $?"
    ;;
  es)
    docker run -d --name "$ES" --memory 1500m --memory-swap 1500m --log-driver json-file --log-opt mode=blocking \
      -p 127.0.0.1:29299:9200 --mount type=tmpfs,dst=/usr/share/elasticsearch/data,tmpfs-size=2147483648 \
      -e discovery.type=single-node -e xpack.security.enabled=false -e ES_JAVA_OPTS="-Xms512m -Xmx512m" \
      -e cluster.routing.allocation.disk.watermark.low=400mb -e cluster.routing.allocation.disk.watermark.high=300mb \
      -e cluster.routing.allocation.disk.watermark.flood_stage=200mb "$IMG_ES" | cut -c1-12
    i=0; until curl -s -m 5 http://127.0.0.1:29299/ > /dev/null || [ $i -ge 90 ]; do sleep 2; i=$((i + 1)); done
    echo "ES answered after about $((i * 2)) s"
    curl -s -m 10 'http://127.0.0.1:29299/_nodes/_local/stats/fs?filter_path=nodes.*.fs.data' | sed 's/^/ESFS /'; echo
    C=$(cg "$ES"); echo "cgroup dir: $C"; ls -l "$C/memory.max" "$C/memory.events" 2>&1 | sed 's/^/CGLS /'
    for f in memory.max memory.swap.max memory.current memory.peak; do echo "CG $f $(cat "$C/$f" 2>&1)"; done
    grep -E '^(oom|oom_kill|max) ' "$C/memory.events" | sed 's/^/CG events /'
    grep -E '^shmem ' "$C/memory.stat" | sed 's/^/CG stat /'
    docker inspect -f 'ES Mounts={{json .Mounts}} Mem={{.HostConfig.Memory}} MemSwap={{.HostConfig.MemorySwap}}' "$ES"
    curl -s -m 10 -X PUT http://127.0.0.1:29299/gatefix-probe > /dev/null
    curl -s -m 10 -X POST -H 'Content-Type: application/json' 'http://127.0.0.1:29299/gatefix-probe/_doc?refresh=true' -d '{"a":1}' > /dev/null
    curl -s -m 10 'http://127.0.0.1:29299/_cat/indices/gatefix-probe?h=index,docs.count' | sed 's/^/IDX before restart /'
    ;;
  pg)
    docker run -d --name "$PG" --memory 512m --memory-swap 512m --log-driver json-file \
      --mount type=tmpfs,dst=/var/lib/postgresql/data,tmpfs-size=805306368 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16 | cut -c1-12
    i=0; until docker exec "$PG" pg_isready -q 2> /dev/null || [ $i -ge 60 ]; do sleep 2; i=$((i + 1)); done
    echo "PG ready after about $((i * 2)) s"
    docker exec "$PG" sh -c 'stat -f -c "PGFS type=%T blocks=%b bsize=%S bfree=%f" /var/lib/postgresql/data; stat -c "PGDIR %a %U" /var/lib/postgresql/data'
    ;;
  restart)
    S0=$(docker inspect -f '{{.State.StartedAt}} rc={{.RestartCount}}' "$ES")
    docker restart "$ES" > /dev/null
    i=0; until curl -s -m 5 http://127.0.0.1:29299/ > /dev/null || [ $i -ge 90 ]; do sleep 2; i=$((i + 1)); done
    echo "after restart: before $S0 now $(docker inspect -f '{{.State.StartedAt}} rc={{.RestartCount}} id={{.Id}}' "$ES" | cut -c1-80)"
    echo "IDX after restart: $(curl -s -m 10 'http://127.0.0.1:29299/_cat/indices/gatefix-probe?h=index,docs.count' | cut -c1-200)"
    curl -s -m 10 'http://127.0.0.1:29299/_nodes/_local/stats/fs?filter_path=nodes.*.fs.data' | sed 's/^/ESFS after restart /'; echo
    echo "== docker update of the memory limit while running"
    docker update --memory 1600m --memory-swap 1600m "$ES" > /dev/null && echo "CG memory.max after update $(cat "$(cg "$ES")/memory.max")"
    ;;
  teardown)
    docker rm -f "$ES" "$PG" emlab2-gatefix-fill emlab2-gatefix-fill2 > /dev/null 2>&1
    docker ps -a --filter name=emlab2-gatefix --format '{{.Names}}' | wc -l | sed 's/^/remaining containers: /'
    df -h /home/eric | tail -1
    ;;
  *) echo "usage: fill | es | pg | restart | teardown"; exit 2 ;;
esac
