#!/usr/bin/env bash
# Counted campaign on t2-dev: 5 down lives and 5 up lives (one up life with the adapter's DEFAULT probe TTL). Run from ~/emlab-16096/lic/code.
set -u
cd "$(dirname "$0")/.."
export LIC_COMMIT="${LIC_COMMIT:?set LIC_COMMIT}"
L=~/emlab-16096/lic
echo "$(date -u +%FT%TZ) campaign start commit $LIC_COMMIT pid $$" >> $L/spawn_ledger.txt
for n in 1 2 3 4 5; do
  python3 licence/life.py down $n --post 70 > $L/results/down_$n.log 2>&1
  if [ $n -eq 5 ]; then python3 licence/life.py up $n --ttl default --post 330 > $L/results/up_$n.log 2>&1
  else python3 licence/life.py up $n --ttl 5 --post 70 > $L/results/up_$n.log 2>&1; fi
done
echo "$(date -u +%FT%TZ) campaign done" >> $L/spawn_ledger.txt
