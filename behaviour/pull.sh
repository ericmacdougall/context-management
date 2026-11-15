#!/bin/sh
B=$(dirname "$0"); mkdir -p "$B/results"
ssh -F $HOME/.ssh/config_fleetops t2-dev 'tar -C ~/emlab-16096/results -cf - .' | tar -C "$B/results" -xf - 2>/dev/null
