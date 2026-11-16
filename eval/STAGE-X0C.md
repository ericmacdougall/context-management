# STAGE X0C: extraction stability and preference-line check (design and rules, committed before any call)

Status: PILOT stage, never confirmatory, exactly 40 headless calls: 20 sessions, each run once under arm S and once under arm P. Approved by the user for this stage only (user message of 2026-11-16 "Yes, run it", relayed by main). Hard split: pilot sessions only (all 20 are sessions already extracted in Stage X0). The eval owner prepares inputs and scores; the phasef owner runs the calls; the eval owner makes no model call and reads no result before the scorer is committed. No judge: every score is mechanical, except that the wish-list check for fca70973 is read by the eval owner and recorded as present or absent per item (not computed by code). TWENTY SESSIONS GIVE A DIRECTION, NOT A RATE.

Questions: (1) Is single-pass extraction stable when the identical prompt is run twice (the gate set in STAGE-X0B.md Section 7.7 and PREREGISTRATION-V2.md X-G5 asked for at least 85 percent; here a first pilot reading)? (2) Does a one-sentence preference instruction recover the user's own stated preferences and interests (the one prompt gap found in STAGE-X0B.md Section 7.8) without displacing other facts or damaging quote and speaker accuracy?

## 1. Prompts

- Arm S: `extract-v2-cap30`, sha16 `ae055bca99452fb6`, UNCHANGED (the Stage X0B prompt), one session per call, same parser, no retry. The runner puts a replicate id in the arm-S cache key so no earlier cap-30 output can be served; the records keep prompt_version `extract-v2-cap30` and the same sha16. Replicate id string for arm S: `x0c-s-1`.
- Arm P: `extract-v3-pref`, sha16 `56c79c2119472532` (phasef commit 5f1b3f3; recomputed by the eval owner from the cap-30 template and matching). It is `extract-v2-cap30` plus ONE sentence inserted right after "Skip small talk." and before "At most 30 facts per session": `Always record the user's own stated preferences, interests, wishes and plans as facts with author "user".` Removing that sentence gives the cap-30 template byte for byte (checked by a phasef test and by the eval owner). Cap stays 30; nothing else changes (same date rule, one session per call, same parser).

## 2. The 20 sessions (rule fixed here, before the list is built)

1. The 10 sessions of Stage X0B (they have a prior cap-30 output of the identical prompt `extract-v2-cap30`, so arm S on them gives an identical-prompt REPLICATE PAIR directly; they include the fca70973 gold session and the three gpt4_ab202e7f sessions that were re-extracted).
2. Every other gold session in the Stage X0 set of a single-session-preference or single-session-user question (expected 4).
3. The rest of the 20 (expected 6): random non-gold sessions of the Stage X0 random sample not already chosen, stratified by length: one from each of the five whole-pool length quintiles (edges 4,935, 8,579, 12,051 and 15,112 characters) plus one more from quintile 4, seeded `16096:X0C:nongold`.
For the 10 sessions WITHOUT a prior cap-30 output, arm S gives a first cap-30 output only; it is compared with their cap-12 output and labelled as such (the cap changed, so it is not an identical-prompt comparison). The stability estimate proper comes from the 10 replicate pairs only. Opaque ids only; the runner stays blind to which sessions are gold, which have a prior output, and which question they belong to (the key is in `out/stage_x0c_key/`, which the runner must not read). Session files, dates and turns are exactly those of Stage X0.

Judgment on the split (stay within 40 calls): the requested design gives 10 replicate pairs (about 160 facts per run) and 20 sessions for the preference arm. 20 fresh pairs would need all 40 calls and leave no preference arm. I keep the requested split because the preference arm tests the one defect actually found, and a stability reading from 10 pairs is enough to say whether the pre-set 85 percent is plausible; if the pooled stability lands between 0.80 and 0.90 it is inconclusive and the proposed next step is 20 fresh pairs (40 calls).

## 3. Run order, directories and records

One run-order file `~/emlab-16096/out/stage_x0c/run_order.json`: the 20 sessions in a seeded order (sha256 of `16096:X0C:order:` + id); the two arms are INTERLEAVED session by session (both arms of a session run back to back, S first for even positions and P first for odd positions), so any drift over the run (load, time of day, rate limits) affects both arms equally; `jobs` lists the 40 jobs in order with `arm` and `replicate`. Inputs: `out/stage_x0c/sessions/<opaque id>.json` (copies of the Stage X0 files). Results: `~/emlab-16096/in/stage_x0c/S/<opaque id>.json` and `~/emlab-16096/in/stage_x0c/P/<opaque id>.json`, plus `in/stage_x0c/_run.json`. Fields: those of STAGE-X0.md Section 3 (session_id, order_index, status, reject_reason, prompt_version, prompt_sha16, model, tokens by kind, seconds, cost_usd, dropped, facts with session_id = opaque id, integer round and round_id, raw_output_path) plus `arm` and `replicate`. Hard cap 40 calls, no retries; a failed or missing job stays in every denominator.

## 4. Scoring (`emlab.stage_x0c_score`, committed before any result is read; mechanical)

1. STABILITY (identical prompt, the 10 replicate pairs: the Stage X0B output against arm S): facts reproduced by quote match (normalised quotes equal, or one contains the other with the shorter at least 8 characters), BOTH directions: stability = (facts of run 1 found in run 2 + facts of run 2 found in run 1) / (facts in run 1 + facts in run 2). Reported pooled over the facts, per session, and as the mean of per-session rates with a Wilson interval by session (n = 10 sessions). Pre-set gate: pooled stability at least 0.85 AND no session below 0.60. The first cap-30 outputs of the other 10 sessions are compared with their cap-12 outputs and labelled (cap change confounded).
2. ARM P against arm S on the same session: user-authored facts (count and share of all facts), total facts, sessions at the cap of 30, facts displaced (arm S facts with no quote match in arm P) and, on the 10 replicate sessions, the EXCESS displacement (displaced share minus the share of the same arm-S facts that were not reproduced by the earlier identical-prompt run), the quote-and-speaker check (vendored phasef checker) on arm P facts and arm S facts, and for the user-authored facts the speaker verdict among those passing (a); tokens, seconds and cost per call, paired, arm P against arm S.
3. fca70973 wish list: the scorer writes the user-authored facts of the fca70973 gold session in both arms to `out/stage_x0c_reading/fca70973_user_facts.json`; the eval owner reads them and records, per item (thrill rides; unique food experiences; nighttime shows), whether a user-authored fact states it (present or absent). No string heuristic is used.

## 5. What counts as "the preference line is safe to adopt" (fixed before the run; ALL must hold)

A1. More user content: the mean per-session increase in user-authored facts, arm P over arm S, is at least +2.0, and arm P has more user-authored facts in at least 14 of the 20 sessions.
A2. Accuracy not damaged: the quote check on arm P facts passes (a) at a rate of at least 0.95, and among arm P's user-authored facts that pass (a) the speaker check passes at least 0.95 (the user line must not make the model attribute assistant text to the user).
A3. Little displacement: arm S facts with no match in arm P are at most 25 percent overall, and on the 10 replicate sessions the excess displacement over the identical-prompt non-reproduction is at most 10 percentage points.
A4. Cap and cost: arm P at the cap of 30 in at most 2 of the 20 sessions, and output tokens per call at most 30 percent above arm S.
A5. The target is hit: at least 2 of the 3 fca70973 wish-list items are present as user-authored facts in arm P and more than in arm S.
If all hold, the preference line is recommended for the V2 draft's part X as a new version string to re-gate (it is still a 20-session pilot: never an adoption without the gate-set check); if any fails, it is not adopted as worded and the failing criterion says what to change. The stability gate (item 1) is reported separately: met means single-pass extraction is stable enough to proceed to the gate-set X-G5 check; not met means a second pass or verification step is added before volume.

## 6. What the stage cannot show

A rate for the pool (20 sessions, 10 replicate pairs); stability of extraction for sessions other than these (the 10 replicate sessions are all capped, long, mostly gold sessions); the effect of the preference line on retrieval or answers; whether the extra user facts are the RIGHT ones beyond the one fca70973 reading; behaviour at the volume of 22,500 calls.

## 7. Handoff

Filled in after the inputs were built (second commit; the rules above were committed in f040706 and are unchanged).

Inputs on t2-dev (counts checked): 20 session files in `~/emlab-16096/out/stage_x0c/sessions/<opaque id>.json`; the run order `~/emlab-16096/out/stage_x0c/run_order.json` (key `order`: the 20 ids, key `jobs`: the 40 jobs in order with `arm`, `replicate`, `order_index`); the key `~/emlab-16096/out/stage_x0c_key/selection_key.json` is for the scorer only (the runner must not read it). Results: `~/emlab-16096/in/stage_x0c/S/<opaque id>.json`, `~/emlab-16096/in/stage_x0c/P/<opaque id>.json` and `~/emlab-16096/in/stage_x0c/_run.json`.
Composition: the 10 X0B sessions, 4 other gold sessions of single-session-preference or single-session-user questions, and 6 random non-gold sessions (length strata 1, 2, 3, 4, 5 and a second from 4). Order (first six jobs): s0bafda9b9382 S, P; se821aca3328d P, S; sd2c1ef3cf116 S, P; and so on, alternating which arm goes first.
Prompts: arm S `extract-v2-cap30` sha16 `ae055bca99452fb6` with replicate id `x0c-s-1`; arm P `extract-v3-pref` sha16 `56c79c2119472532`. Calls: 40 (hard cap; no retries).

