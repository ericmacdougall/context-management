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
