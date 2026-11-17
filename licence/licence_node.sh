#!/usr/bin/env bash
# Usage: licence_node.sh fresh|stop   (runs on t2-dev; directory ~/emlab-16096/lic). Owns ONLY emlab-es-lic-16096 and volume emlab-es-lic-vol-16096.
set -euo pipefail
D=$HOME/emlab-16096/lic; N=emlab-es-lic-16096; V=emlab-es-lic-vol-16096; PORT=19270
mkdir -p $D; chmod 700 $D
case "${1:-}" in
fresh)
  docker rm -f $N >/dev/null 2>&1 || true; docker volume rm $V >/dev/null 2>&1 || true
  [ -s $D/es_password ] || { umask 077; head -c 24 /dev/urandom | base64 | tr -d '/+=' > $D/es_password; }
  chmod 600 $D/es_password
  ( umask 077; printf 'user = "elastic:%s"\nsilent\nshow-error\n' "$(cat $D/es_password)" > $D/curl.conf )
  docker volume create $V >/dev/null
  docker run -d --pull never --name $N --restart no -p 127.0.0.1:$PORT:9200 --memory 3g \
    -v $V:/usr/share/elasticsearch/data -v $D/es_password:/run/secrets/es_password:ro \
    -e discovery.type=single-node -e ELASTIC_PASSWORD_FILE=/run/secrets/es_password -e ES_JAVA_OPTS="-Xms1g -Xmx1g" \
    -e xpack.security.enabled=true -e xpack.security.http.ssl.enabled=false -e cluster.name=emlab-lic-16096 \
    -e cluster.routing.allocation.disk.watermark.low=15gb -e cluster.routing.allocation.disk.watermark.high=10gb \
    -e cluster.routing.allocation.disk.watermark.flood_stage=5gb -e cluster.routing.allocation.disk.watermark.flood_stage.frozen=5gb \
    docker.elastic.co/elasticsearch/elasticsearch:9.5.6 >/dev/null
  echo "$(date -u +%FT%TZ) started $N volume $V" >> $D/spawn_ledger.txt ;;
stop) docker stop $N; echo "$(date -u +%FT%TZ) stopped $N" >> $D/spawn_ledger.txt ;;
*) echo usage; exit 2;;
esac
