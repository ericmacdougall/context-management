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

Filled in after the inputs were built (second commit; the rules above were committed in 2d25936 and are unchanged).

Inputs on t2-dev (counts checked): 10 session files in `~/emlab-16096/out/stage_x0b/sessions/<opaque id>.json` (copies of the Stage X0 files), the run order `~/emlab-16096/out/stage_x0b/run_order.json` (key `order`: s4d2671e04b9d, sbe2fabc62aa8, s0964e5661ff2, s827f306f0106, sb09bceea100a, sc699af8d9a5d, sc2e73c115511, sff21ad092f20, s6de582b428f4, s994fd89ef0fe). The key `~/emlab-16096/out/stage_x0b_key/selection_key.json` is for the scorer only; the runner must not read it. Results go to `~/emlab-16096/in/stage_x0b/<opaque id>.json` and `_run.json`.
Composition (56 capped sessions in Stage X0; pools: 35 further capped gold, 17 capped non-gold): 4 forced (1 weak-list session of fca70973, 3 low-overlap sessions of gpt4_ab202e7f), 3 random capped gold (2 knowledge-update, 1 weak-list) and 3 random capped non-gold sessions.
Calls: 10 (hard cap; no retries); prompt `extract-v2-cap30`, sha16 `ae055bca99452fb6`.

## 7. Results (eval owner; scored with `emlab.stage_x0b_score`, committed in 2d25936 before any result was read). PILOT, MECHANICAL, NO JUDGE. TEN SESSIONS GIVE A DIRECTION, NOT A RATE

Run facts (runner, relayed by main): 10 of 10 calls ok, 0 repaired, rejected or failed; prompt `extract-v2-cap30` (sha16 `ae055bca99452fb6`); the runner did not read the key directory. Data: `results/stage_x0b/stage_x0b_score.json`.

### 7.1 Facts per session at cap 30 against the 12 of the same session
Every session returned exactly 12 under the old cap and more under cap 30: 13, 15, 14, 19, 15, 15, 14, 16, 24, 13 (mean 15.8; 3.8 beyond 12 on average, range 1 to 12 beyond). All 10 returned more than 12; NONE reached 30 (0 of 10 at the new cap), so the cap of 30 was not binding here and the loss under the cap of 12 in these sessions was between 1 and 12 facts each (a lower bound only for sessions that would have exceeded 24, which none did). By session type: the 4 forced low-overlap and weak-list sessions 13, 15, 19, 24; the other 3 gold sessions 14, 13, 15; the 3 non-gold sessions 15, 14, 16.

### 7.2 Stability of the original 12 facts (quote match: normalised quotes equal, or one contains the other with the shorter at least 8 characters)
111 of the 120 original facts reappear (92.5 percent); 9 do not (4 sessions lose between 1 and 4: 8, 10, 10 and 11 of 12 reappear, the other six reappear in full). Of the 158 new facts, 45 (28 percent) match no original fact (the 38 facts beyond 12 plus about 7 replacements; one new fact can match two originals, so the counts are not exactly additive). So single-pass extraction is NOT perfectly stable: about 1 original fact in 13 is replaced when the cap is raised. This comparison mixes two things (the changed cap and the model's run-to-run sampling) and cannot separate them: an identical-prompt replicate was not run.

### 7.3 Quote and speaker check on the new facts (vendored phasef checker)
(a) Verbatim in the cited round: 151 of 158 = 0.956 [Wilson 0.911, 0.978] (7 not found); speaker: 151 pass, 0 ambiguous, 0 mismatch, 7 undefined. On the 45 new facts with no original match: 41 of 45 pass (0.911). The checker's controls were all rejected (changed word 151 of 151, adjacent round 149 of 149 with 2 excluded, flipped author 151 of 151). 15 of 158 quotes (9.5 percent) exceed the 12-word limit. The extra facts are about as well anchored as the first 12 (0.956 against 0.960 in Stage X0), with wide uncertainty at this size.

### 7.4 The two missing answers
NO: neither answer appears with cap 30. fca70973: its one gold session was re-extracted (15 facts); the string and key heuristics find the answer in no fact. gpt4_ab202e7f: 3 of its 5 gold sessions were re-extracted (13, 19 and 24 facts); no fact in the question's gold sessions (new facts for the three, cap-12 facts for the other two) carries the answer by either heuristic. So the cap does NOT explain these two misses: the answer is in the raw session text by the key test but not in what the extractor chose to record at 13 to 24 facts per session. Why is not established here (candidates: the answer is spread over several turns or needs counting, it sits in assistant text that the extractor treats as low value, or the heuristic's key match on the raw text is looser than a fact quote); the Stage X0 reading that the two misses were "suggestive" of cap loss is not supported.

### 7.5 Cost and time against cap 12, same 10 sessions
| | cap 12 | cap 30 | change |
| output tokens per call | 2,317 | 2,999 | +682 (+29 percent) |
| seconds per call | 18.0 | 21.4 | +3.4 (+19 percent) |
| notional cost per call (USD) | 0.0856 | 0.0932 | +0.0076 (+9 percent) |
| input tokens (cache creation + read + uncached) | unchanged within about 1 percent | | |

### 7.6 Implied change to the Stage X0 extrapolations (DIRECTION ONLY: 10 sessions, 7 of them gold, are not a random draw of the capped pool sessions)
The pool's capped share, from the Stage X0 random sample by length stratum (0, 8, 17, 13 and 33 percent for strata 1 to 5), is about 14 percent; only capped sessions change; the mean paired per-call increase measured here applies to them.
| | 6,100 calls | 22,500 calls |
| extra output tokens | +0.59 million (10.4 to 11.0 million, +5.7 percent) | +2.17 million (38.3 to 40.5 million, +5.7 percent) |
| extra sequential time | +2,919 s (about 0.8 hour) | +10,767 s (about 3.0 hours) |
| extra notional cost | +6.6 USD (442 to 448) | +24.2 USD (1,630 to 1,654), about +1.5 percent |
The derived no-overhead estimate of Stage X0 scales the same way (about +1.5 percent). Raising the cap to 30 therefore costs little in aggregate because few pool sessions are capped, but per capped call it costs about 9 percent more.

### 7.6b What the 10 sessions show and do not show
Show: in sessions that hit the cap of 12, the extractor given room returns a few more facts (mean +3.8, never close to 30); the extra facts are about as well anchored; about 92 percent of the original facts reappear; and the two answers that were missing from the facts stay missing, so the cap is not their cause. Do not show: the share of the pool whose facts are lost to the cap (10 sessions give a direction, not a rate; they are 7 gold and 3 non-gold); the run-to-run stability of the same prompt (no identical-prompt replicate); anything about uncapped sessions; effects on retrieval or answers.

### 7.7 Recommendation for the V2 draft (not a change to it)
Cap: use 30 in the extraction prompt of part X. None of the 10 capped sessions reached 30 (maximum 24), the extra cost is about +9 percent per capped call and about +1.5 percent overall, and a cap of 20 would have truncated the 24-fact session and sat close to the 19-fact one. One pass or two: the evidence does not require a second pass but also does not clear single-pass extraction. About 7.5 percent of the original facts were replaced and 28 percent of the new facts were new, but the cap and the sampling are confounded, and the two missing answers were not recovered by more facts, which a second pass over the same session (same prompt) would not be expected to fix either. Recommended for V2: (1) part X's pilot uses a single pass at cap 30; (2) register an extraction-stability gate before volume, measured on an identical-prompt replicate of at least 20 sessions (fact overlap by quote match at least 85 percent; the smallest version of that check is 10 calls re-running these same sessions at cap 30, which needs its own approval); (3) add a second pass or a verification step only if that gate fails or if the facts-versus-raw-text answer gap (30 of 32 in Stage X0) does not close; (4) investigate the two missing answers by reading the sessions (a no-model step) before deciding whether they point to a prompt defect.

### 7.8 Why the two answers are missing: classification from a no-model reading of the pilot gold sessions (reading notes kept outside the repository and out of any test-split use; only the classification is recorded here; this supersedes "why is not established" in 7.4)
- gpt4_ab202e7f (multi-session count): HEURISTIC ARTEFACT plus CROSS-SESSION AGGREGATION. Each of the five items is an explicit user aside in a different session (four stated outright, the coffee maker as an implied replacement), and all five are present as facts at BOTH caps; the answer is a count over five sessions, so no single fact can contain it and the one-fact-at-a-time heuristic cannot see it. Content type: aggregation; not an extraction defect and not a cap effect. Single-session extraction cannot capture the count, but it captured every part of it; the join belongs to the reader or analyst.
- fca70973 (single-session preference): HEURISTIC ARTEFACT (the answer is a paraphrased preference rubric) plus a SMALL REAL PROMPT GAP. The four parks the user visited are recorded (one fact), as are the Halloween, food and VIP interests; the user's wish list in the first round (thrill rides, unique food, nighttime shows) is not recorded as a user fact (no fact mentions thrill rides), while the facts are dominated by assistant-stated specifics (3 user against 9 assistant facts at cap 12, 3 against 12 at cap 30). The instructions that pull that way are "concrete facts the assistant stated that the user could ask about again" and "prefer specific details", with no instruction or field that asks for the user's stated preferences or interest lists (a preference stated as a list inside a question). Content type: a preference plus an implication spread over rounds.
- Conclusion: neither is a cap effect (the answers stay absent at 15, 19 and 24 facts, and every gpt4_ab202e7f item is already present at cap 12). One prompt defect candidate (user preferences and interest lists are under-extracted relative to assistant details), no schema gap (subject, predicate and object can hold a list), and one class of content (cross-session aggregation) that single-session extraction cannot join. The string heuristic is not a valid measure for count and preference questions and must not be used as a gate for them.
