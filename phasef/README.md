# phasef: micro-pilot harness (reader, judge, fact memory)

## Running headless `claude -p`: use a neutral non-git directory

Run every headless `claude -p` call with its working directory set to a directory that is NOT a git repository
(phasef uses `phasef-out/neutral-cwd`, see `LLM(..., cwd=...)` in `llm.py`). When the cwd is a checkout with an
uncommitted diff, the user's Stop hook fires inside the headless run: it adds extra turns (about 3x the tokens) and
appends hook chatter ("I did not write a WAL note...") to the answer text, which contaminates both the measurement
and the judged answers. Do not disable hooks or change setting sources to avoid it; just change the cwd.

Measured from the neutral directory with a one-word prompt (`run_v2 overhead`): 2 fresh + 9,565 cache-creation +
940 cache-read = about 10,507 input tokens per call, so the "about 10.5k tokens of harness overhead per call" figure is confirmed.
Each call still creates one session log directory.

## Calls are capped and cached

`llm.py` keys every call by sha256(model, recipe, system, prompt), counts cache misses in a flock-guarded counter
before the subprocess starts, and refuses past the cap. Bump the recipe string whenever a prompt changes.

## Running the analyst arms with the smallest log footprint (plan, no calls made yet)

- One detached job, resumable, no retries: `phasef/analyst_job.py`, started as
  `HARNESS_LOCAL_OK="analyst arms, capped, concurrency 2" python3 ~/.claude/hooks/run_capped.py --max-rss-gb 4 --max-procs 24 --timeout-s 14400 -- python3 -u -m phasef.analyst_job --phase canary ...`
  (a breach pauses the tree; report it, do not resume it yourself). Concurrency 2, own call counter capped at 620, stop file `phasef-out/analyst_job/STOP` ends the loop cleanly.
- Calls: canary 46 (23 low-overlap questions x 2 arms) + rest about 518 (H1, H2, H3 x 94 questions x 2 arms, canary items skipped) + secondary 23 = about 587.
- Long-lived sessions: NOT possible without cross-question contamination. A headless session carries its whole conversation as context; a second question in the same session would see the first question's candidates, tool results and answer.
  The only safe grouping is one fresh session per (question, arm), which is what `claude -p --no-session-persistence` gives. A resumed or interactive session would break the "never sees other questions" rule, so it is not used.
- Footprint per call (measured on this machine): `--no-session-persistence` writes no transcript (no ~/.claude/projects directory exists for the neutral cwd); the harness hooks create one WAL directory (one `main.jsonl` of about 0.5 KB) and one work directory (one `.agent-harness-session.json` of about 0.1 KB) per call.
  Other log writes of mine: the ledger (about 0.3 KB per call), per-question tool-call logs and MCP config files (about 1 KB per ANALYST call) under phasef-out, which is not a harness log tree.
