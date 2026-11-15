#!/bin/sh
# Rest phase then secondary phase of the analyst arms (run under run_capped; see README). Resumable; STOP file ends it cleanly.
W=/Users/eric/.agent-harness/work/73716864-6b3b-4deb-94ef-8ed2c8432e4a/phasef-out/analyst_job
cd /Users/eric/.agent-harness/work/73716864-6b3b-4deb-94ef-8ed2c8432e4a/elastic-memory-lab-phasef
BACKEND="ssh -F /Users/eric/.ssh/config_fleetops t2-dev /home/eric/emlab-16096/bin/analyst_tool --question {qid} --level {level}"
python3 -u -m phasef.analyst_job --phase rest --tasks-dir $W/tasks --top20-dir $W/tasks_top20 --backend "$BACKEND" --questions-file $W/q94.json --lowoverlap-ids $W/low23.json --canary-level H3 --levels H1 H2 H3 --secondary-level H3_top20
python3 -u -m phasef.analyst_job --phase secondary --tasks-dir $W/tasks --top20-dir $W/tasks_top20 --backend "$BACKEND" --questions-file $W/q94.json --lowoverlap-ids $W/low23.json --canary-level H3 --levels H1 H2 H3 --secondary-level H3_top20
