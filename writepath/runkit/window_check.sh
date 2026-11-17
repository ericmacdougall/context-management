#!/bin/sh
# Read-only, on the lab host: for each stamped single's .out, read the ES container log over the run window
# (start .. end as the .out records them) and count high-watermark-exceeded and flood-stage lines. Prints names,
# return codes and counts only. usage: ssh <lab host> sh -s <heads...> < runkit/window_check.sh
R=$HOME/emlab2-16096/writepath/wp/results
for h in "$@"; do
  for f in "$R"/one."$h".*.out; do
    [ -e "$f" ] || continue
    s=$(head -1 "$f" | sed -n 's/^start \([0-9TZ:-]*\) .*/\1/p')
    [ -n "$s" ] || { echo "$(basename "$f") no start line"; continue; }
    own=$(grep -E '^disk_gate end' "$f" | sed -n 's/.* own \([+-][^ ]* GiB\).*/\1/p')
    e=$(grep -E '^end [0-9]' "$f" | tail -1 | awk '{print $2}')
    [ -n "$e" ] || e=$(date -u -r "$f" +%Y-%m-%dT%H:%M:%SZ)
    t=$(docker logs --since "$s" --until "$e" emlab2-writepath-es 2>&1); rc=$?
    hi=$(printf '%s\n' "$t" | grep -ciE 'high disk watermark .* exceeded')
    fl=$(printf '%s\n' "$t" | grep -ci 'flood stage')
    echo "$(basename "$f") window $s..$e rc=$rc lines=$(printf '%s\n' "$t" | grep -c .) high=$hi flood=$fl own_end=${own:-none}"
  done
done
