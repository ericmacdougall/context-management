# STAGE X0B: cap-30 re-extraction check on 10 capped sessions (design and rules, committed before any call)

Status: PILOT stage, never confirmatory, exactly 10 headless calls, one per session. Approved by the user for this stage only (user message of 2026-11-16 "Yes, run it", relayed by main). Hard split: pilot sessions only (the 10 are sessions already extracted in Stage X0, see STAGE-X0.md Section 7). The eval owner prepares inputs and scores; the phasef owner runs the calls; no model call and no result read by the eval owner before the scorer is committed. No judge: every score is mechanical. TEN SESSIONS GIVE A DIRECTION, NOT A RATE; nothing here is an estimate of a pool-level percentage.

Question: how many facts does the cap of 12 remove? In Stage X0, 56 of 200 sessions (28 percent; 39 of 79 gold sessions) returned exactly 12 facts, and the two gold questions whose answer is in the raw text but in no fact (fca70973 and gpt4_ab202e7f) involve capped sessions.

## 1. The 10 sessions (rule fixed here, before the list is built)

Population: the sessions with exactly 12 facts in Stage X0 (the cap-12 run; 56 sessions). Rule, in order:
1. FORCED: every capped session that is a gold session of fca70973 or gpt4_ab202e7f (4 sessions expected: 1 and 3).
2. Then a seeded random draw (seeds `16096:X0B:gold` and `16096:X0B:nongold`, Python `random.Random(seed).shuffle` over the ids sorted ascending) of 3 further capped gold sessions (any other gold group) and 3 capped random non-gold sessions, so that both gold and non-gold are present.
Run order: sorted by sha256(`16096:X0B:order:` + opaque id). Opaque ids only; the runner does not learn which sessions are gold or forced (the key is in `out/stage_x0b_key/`, which the runner must not read). Session files, dates (the Stage X0 date rule: earliest date over all occurrences of the content) and turns are exactly those of Stage X0.

## 2. Prompt: `extract-v2-cap30`

Identical to `extract-v2-anon` (EXTRACT_SYSTEM plus EXTRACT_V2_TEMPLATE of `phasef/prompts.py`, one session per call, the same "Below are 1 chat sessions" wording, same parser, no retry) except ONE number in the template: "At most 12 facts per session; prefer specific details." becomes "At most 30 facts per session; prefer specific details." (the sentence occurs once; the "12 words" quote limit is untouched). The eval owner computed sha256 of system + "\n" + template for both versions: `extract-v2-anon` begins `a2336c7976bb8fc2` and `extract-v2-cap30` begins `ae055bca99452fb6`, matching the phasef owner's value (phasef commit b7bbc4e). The two templates have the same length and differ in that number only. Version string: `extract-v2-cap30`. The runner's cache key uses the new version, so no cap-12 output can be served.

## 3. Runner records

Same fields as STAGE-X0.md Section 3 with `prompt_version` = `extract-v2-cap30` and `prompt_sha16` = `ae055bca99452fb6`: inputs `~/emlab-16096/out/stage_x0b/sessions/<opaque id>.json` and `out/stage_x0b/run_order.json`; results `~/emlab-16096/in/stage_x0b/<opaque id>.json` (session_id, order_index, status, reject_reason, prompt_version, prompt_sha16, model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, seconds, cost_usd, dropped, facts with session_id = opaque id, integer round, round_id, raw_output_path) and `_run.json`. Cap: 10 calls, no retries. Expect output tokens to rise from about 1.9k to perhaps 4k to 6k per call (recorded).

## 4. Scoring (`emlab.stage_x0b_score`, committed before any result is read; mechanical)

1. Facts per session at cap 30 against the 12 of the same session: how many beyond 12 (new count minus 12), how many sessions return more than 12, the share still at the new cap (30 or more).
2. Extraction stability: of the original 12 facts, how many reappear among the new facts by quote match (normalised quotes equal, or one contains the other with the shorter at least 8 characters); the new facts with no match among the original 12.
3. The quote-and-speaker check (the vendored phasef checker, `emlab/provenance_x04.py`) on all new facts and, separately, on the new facts that match no original fact; the checker's controls re-run.
4. Whether the two missing answers now appear: for fca70973 and gpt4_ab202e7f, the gold-answer string and key heuristic of Stage X0 (crude) over all the question's gold sessions, using the new facts for the re-extracted sessions and the cap-12 facts for the others, beside the cap-12 result.
5. Per call on the same sessions, cap 30 against cap 12: seconds, output tokens, cost, input tokens (paired mean differences and ratios).
6. Implied change to the 6,100 and 22,500 call extrapolations of Stage X0: the pool's capped share (mean over the five length strata of the capped share of the random non-gold sample of Stage X0) times the mean paired per-call increase measured here, added to the cap-12 totals (output tokens, seconds, cost); sessions below the cap are assumed unchanged. This is a DIRECTION, not a rate: the 10 sessions are 7 gold and 3 non-gold and not a random draw of the capped pool sessions.

## 5. What each outcome changes, and what it cannot show

- If many sessions return well over 12 facts and the missing answers appear, the cap was removing answer-bearing facts: V2 part X must raise or remove the cap (and re-price: the implied extra cost is in item 6). If sessions return about 12 to 15 facts, the cap removes little and the extraction prompt stays. If sessions reach 30, the cap of 30 is itself binding and the check does not bound the loss.
- Stability: if few of the original 12 facts reappear, extraction is unstable at this prompt (a second run would give different facts), which part X's gates must treat as a defect separate from the cap.
- NOT shown: the share of the pool whose facts are lost to the cap; effects on retrieval or answers; anything about uncapped sessions; behaviour on the longest sessions; whether a higher cap helps links or supersession.

## 6. Handoff

Filled in below after the inputs are built (second commit).
