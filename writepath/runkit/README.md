# Run kit: stamped single runs (amendment 20)

Each experiment runs once, from a pinned tree, under the same validity gates as run_all (amendment 17c).

1. **Ship the clean tree.** On the workstation, `sh sync.sh` (the tree must not be dirty).
2. **Pin it.** On the host, from `WP_HOME/src`, run `sh pin_run.sh`. It prints the pinned directory `WP_HOME/runs/<head>-<time>`.
3. **Run the chain.** On the host, with `WP_HOME` set:
   `setsid sh <pinned>/runkit/chain_singles.sh <pinned> <pinned>/runkit/specs/<spec>.txt > WP_HOME/state/chain_singles.<head>.log 2>&1 < /dev/null &`
   - Each experiment goes through `launch_one.sh`:
     - It refuses to start under 17 GB free.
     - It runs `exp.disk_gate start`, then `check`, the module, and `end`. `end` fails closed (exit 3, VOID): on a STOP file, a failed or timed-out `docker logs`, an ES container restarted during the run, own usage not measured or over the cap, or a flood-stage or high-watermark-exceeded line in this stream's Elasticsearch log for the run window (review-2 N2, amendment 23a).
     - A watcher writes `WP_HOME/state/STOP` at the first flood-stage or high-watermark line; the chain also checks STOP before each step.
   - The chain stops at the first BLOCKED or VOID run. A VOID run may be rerun, and the rerun is recorded. A FAIL is recorded and never rerun.
   - Output goes to `WP_HOME/results/one.<head>.<label>.out`, plus the experiment's own `<name>.<sha16>.json`.
4. **Scan for secrets before anything leaves the host.**
   `python3 runkit/leak_scan.py WP_HOME WP_HOME/results WP_HOME/logs WP_HOME/state <other dirs>`
   - It reads this stream's secret files (`WP_HOME/secrets*`) in memory only.
   - It prints only file names and counts, never a value.
5. **Copy the results to the workstation.** Copy `WP_HOME/results` to a private directory, then run
   `HOST_HOME=<host home>/ python3 runkit/stage_results.py runkit/groups.tsv <copy> <ab copy> results`.
   - It shortens the host home prefix to `~/` and writes `results/MANIFEST.tsv` with both sha256 values for every file.

`specs/step1-a.txt` to `specs/step1-d.txt` are the step-1 batches (c and d at the post-fix commit, amendments 23 and 23a). `groups.tsv` assigns each committed result to a group and a label.
`window_check.sh` (read-only, on the host: `ssh <host> sh -s <heads...> < runkit/window_check.sh`) reads this stream's ES log over each run window again and prints, per run, the `docker logs` exit code, the line count, the high-watermark and flood-stage line counts and the own usage at `end`.
