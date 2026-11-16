#!/bin/sh
B=$(cd "$(dirname "$0")" && pwd); mkdir -p "$B/results"
ssh -F $HOME/.ssh/config_fleetops t2-dev 'tar -C ~/emlab-16096/rep/results -cf - .' | tar -C "$B/results" -xf -
