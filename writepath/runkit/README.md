# Run kit: stamped single runs (amendment 20)

Each experiment runs once, from a pinned tree, under the same validity gates as run_all (amendment 17c).

1. **Ship the clean tree.** On the workstation, `sh sync.sh` (the tree must not be dirty).
2. **Pin it.** On the host, from `WP_HOME/src`, run `sh pin_run.sh`. It prints the pinned directory `WP_HOME/runs/<head>-<time>`.
3. **Run the chain.** On the host, with `WP_HOME` set:
   `setsid sh <pinned>/runkit/chain_singles.sh <pinned> <pinned>/runkit/specs/<spec>.txt > WP_HOME/state/chain_singles.<head>.log 2>&1 < /dev/null &`
   - Each experiment goes through `launch_one.sh`:
     - It refuses to start under 20 GB free, measured in exact KiB (amendment 24; exact since H4: `df -BG` rounded up and let 19.3 GiB through).
     - It runs `exp.disk_gate start`, then `check`, the module, and `end`. `end` is VOID (exit 3) on: a STOP file (checked first and again after the last measurement), a failed or timed-out `docker logs`, a different ES container id or StartedAt, another boot, a host clock step, a start or end marker missing from the retained ES log, own usage not measured or over the cap, or a flood-stage or high-watermark-exceeded line between this run's markers (review-2 N2, amendment 23a, step-1 delta review P1 and P2). What it guarantees, and what it does not (own usage is sampled, not bounded between samples), is in the docstring of `exp/disk_gate.py`.
     - A watcher writes its reason into `WP_HOME/state/STOP` at the first flood-stage or high-watermark line; the chain also checks STOP before each step. `disk_gate start` is BLOCKED while a STOP exists and never deletes it: read it, then consume it with `python -m exp.disk_gate clear-stop <reason>` (renamed, reason logged in `state/stop_clears.log`).
   - The chain stops at the first BLOCKED or VOID run. A VOID run may be rerun, and the rerun is recorded. A FAIL is recorded and never rerun.
   - Output goes to `WP_HOME/results/one.<head>.<label>.out`, plus the experiment's own `<name>.<sha16>.json`.
4. **Scan for secrets before anything leaves the host.**
   `python3 runkit/leak_scan.py WP_HOME WP_HOME/results WP_HOME/logs WP_HOME/state <other dirs>`
   - It reads this stream's secret files (`WP_HOME/secrets*`) in memory only.
   - It prints only file names and counts, never a value.
5. **Copy the results to the workstation.** Copy `WP_HOME/results` to a private directory, then run
   `HOST_HOME=<host home>/ python3 runkit/stage_results.py runkit/groups.tsv <copy> <ab copy> results`.
   - It shortens the host home prefix to `~/` and writes `results/MANIFEST.tsv` with both sha256 values for every file.

`specs/step1-a.txt` to `specs/step1-f.txt` are the step-1 batches: a and b under the old gate, c (31112b4) and d (2e1948c) at the post-fix commit (amendments 23 and 23a), e (96214c2, amendment 24a) and f (the one R1 rerun at 0a433bc, amendment 24b). All were stamped under a gate with the step-1 delta review's P1 and P2 open; the hardening branch reruns the full list under the fixed gate (its H-amendment names the spec). `groups.tsv` assigns each committed result to a group and a label.
`window_check.sh` (read-only, on the host: `ssh <host> sh -s <heads...> < runkit/window_check.sh`) reads this stream's ES log over each run window again and prints, per run, the `docker logs` exit code, the line count, the high-watermark and flood-stage line counts and the own usage at `end`.
