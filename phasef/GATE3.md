# Phase F links rescue: dev stop condition and gate 3 (registered before any v5 measurement and before any dev2 call)

Source: main's rescue brief (main's choice) and the user's answer answer:73716864-6b3b-4deb-94ef-8ed2c8432e4a/ec3c6904-8969-4afb-94a4-1b7305c6a605
"Fix the links first". GATE.md, GATE_RESULT.md and GATE2.md stay as they are. No criterion below changes after a result it judges is seen.
The dev set is the 16 knowledge-update questions of GATE.md (15 non-abstention). The fresh set is dev2 as listed in GATE2.md; it has received no call.

## 1. Yardstick check (done before this registration; details in LINK_RUBRIC.md)
Opus auditor self-agreement on the final v4 link set (24 links): 24/24 and 23/24 across reorderings and a reworded rubric.
That is above 0.90, so the metric is kept: link precision = share of audited links that the `linkaudit-v1` Opus auditor labels
replaces or pending, one audit per link set. Added, as the brief asks: harmful-link rate = share labelled unrelated or restates.

## 2. Recall ceiling of the brief's gold-update criterion on dev (computed before v5; no LLM call)
Owner's final definition (GATE2): X = the gold-session fact with the highest answer-token coverage (at least 50%); a question is a hit when X has a
replaces or pending link to a fact in a different gold session. Whether a rubric-correct link can produce a hit:

| question | X exists | correct earlier value in another gold session | v4 final | note |
|---|---|---|---|---|
| 07741c44, 0e4e4c46, 2133c1b5, 2698e78f, 26bdc477, 45dc21b6, 50635ada, 5831f84d, 603deb26, 89941a93, 8fb83627 | yes | yes | hit, link audited precise | 11 questions |
| 07741c45 | yes | yes (under the bed -> shoe-rack plan, pending) | miss | candidate miss: lemma turns "stored" into "stor", so the pair never reached the judge |
| 5a4f22c0 | yes | NO: the earlier gold round names no employer for Rachel | hit | the hit rests on relationship -> employer, which all three audits label adds |
| 5c40ec5b | yes | NO comparable value: the earlier session has "met at a festival" and a plan to meet; X ("met up twice") has no candidate pair | miss | a plan fulfilled is not replaces or pending under the rubric |
| 89941a94 | NO (coverage 0.33) | the correct pair (3 bikes -> 4 bikes) exists and is linked, audited replaces | miss | yes/no answer; token coverage cannot find X. Not an extraction miss as DEVLOG says |

So with rubric-correct links the attainable maximum under the owner's definition is 12 of 15 (13 only if the 5c40ec5b plan -> count pair is linked
and accepted, which the rubric does not support). The brief's dev bar (13 of 15) can be met only with a link the auditor rejects or a contested one.
This is recorded as TARGET_MISMATCH: the bar is registered verbatim below, not lowered.

## 3. Dev stop condition (verbatim from the brief; at most 3 iterations, each logged in DEVLOG.md)
All must hold on the dev set:
- S1: link precision >= 0.90 with at least 20 links audited (every replaces and pending link; seeded random 80, seed 16096, if more than 80).
- S2: harmful-link rate <= 0.05 over the same audited links.
- S3: gold update linked (replaces or pending) on >= 13 of the 15 non-abstention dev questions under the owner's final definition above.
Diagnostic, no threshold: S3-correct = S3 hits whose link the auditor labels replaces or pending. Reported next to S3 on every iteration.

## 4. Gate 3 on dev2 (one run, only if the dev stop condition holds)
- G1: answers correct on >= 14 of 16, and at most 1 fewer than the gold-evidence (ORACLE) reader (Opus judge, benchmark prompts, as GATE2).
- G2: gold-value fact B (GATE.md definition) in the reader's context on >= 14 of 16.
- G3: B labelled current or pending-update on >= 13 of 16 (GATE.md exception for earlier-value questions).
- G4: link precision >= 0.90 under the rubric (audit every link, or a seeded random 80 with seed 16096 if more).
- G4b: harmful-link rate <= 0.05 over the same audited links.
- G5: gold update linked (owner's definition) on >= 12 of 16.
Each reported as k of n with a Wilson interval and failures by question id. Extraction recipe extract-v2 at 16 sessions per call, unchanged.

## 5. Budget rule for the gate
Rescue call ceiling: counter value 467 (287 before the rescue + 180). Before the first dev2 call, the remaining calls must cover, with a 10% margin:
extraction (unique uncached dev2 sessions / 16, rounded up; session ids are counted without any call), 16 expansion, 16 ORACLE, 16 reads,
6 answer-judge, link judging at its upper bound from the candidate cap (16 x ceil(max_pairs / 15) first-stage calls, plus second-stage and audit calls
at the dev rate per question scaled to 16). If they do not, no dev2 call is made and the shortfall is reported.

## 6. Amendment, 2026-11-15, before any dev2 call (main's decisions; main's choice, not the user's words)
Source: main's message to the rescue author after the rescue handoff. dev2 is untouched (no LLM call; only its 16 records were copied to phasef-out/rescue/ and its
sessions counted: 743 unique, none already extracted, so 47 extraction calls at 16 per call).
1. Waiver (main's): the dev stop condition of section 3 is waived. Reasons as given by main: dev precision 19/19 fell one link short of the 20-link minimum, and the dev
   recall bar of 13/15 equals the measured ceiling of the answer-fact definition (12 by strict reading). The dev result is in-sample; the held-out gate is the evidence.
2. Frozen recipe: v5 exactly as at HEAD 2b15450, iteration-1 settings: candidates_v5 with CAND_V5 (theta 0.12, 5 per fact, 40 per question, no protection, lemma fix);
   stage 1 = link-v4.1 Sonnet prompt; stage 2 = link-v5-verify Sonnet prompt with the code-side consistency check; derive_status from links.py; reader-v1.2; retrieval
   and question expansion as v3/v4. New code (phasef/gate3.py) only wires these together for dev2 and adds items 3, 4 and 6.
3. G5 gold update pair (replaces the answer-token definition for G5 only; G2 and G3 keep the GATE.md gold-value fact B): before any link is judged on dev2, one Opus call
   per question (recipe goldpair-v1) sees the question, the gold answer, the full text of the question's gold (answer) sessions and the facts extracted from those sessions
   (numbered), and names the fact numbers that state the OLD value and the NEW value, or "none" for a side no extracted fact states. Stored in rescue/dev2_goldpairs.json.
   G5 hit: a final link (item 4) of label replaces or pending whose two facts are one named old fact and one named new fact. A side named "none" is an extraction miss:
   reported separately and a G5 failure. Unparseable output or a number outside the list: G5 failure, reported (no retry).
4. Order independence: stage 1 and stage 2 run twice. Run A: candidate pairs packed 15 per call across questions in pair-id order; stage 2 on run-A positives, 12 per
   call in pair-id order. Run B: the same pairs packed 15 per call after a seeded shuffle (seed 16096); stage 2 on run-B positives, 12 per call after a seeded shuffle
   (seed 7). A link counts only if both runs' final verdicts are replaces or pending; its label is replaces if both say replaces, otherwise pending. Flips reported:
   stage-1 positive/negative disagreements and final link/no-link disagreements.
5. Criteria exactly as section 4: G1 FACTS5 (reader with final v5 labels) >= 14/16 and >= ORACLE - 1; G2 >= 14; G3 >= 13; G4 precision >= 0.90 (every link audited by
   linkaudit-v1 Opus, full source rounds and dates, batches of 10); G4b harmful <= 0.05; G5 >= 12/16 under item 3. Answer judge: Opus, benchmark prompts, batches of 6.
6. Diagnostic arm NOLABEL (not a criterion): same reader, same retrieved facts in the same order, every status label removed; each fact keeps only its date and its
   modality tag (stated as fact, decision or plan), identical to dev diagnostic D2.
7. Budget (replaces the section 5 margin rule): allowance 220 calls (counter 345 -> cap 565). Fixed: extraction 47, expansion 16, gold pairs 16, three reader arms 48,
   answer judge 8 = 135, leaving 85 for link judging and audit. Variable: 2 x ceil(P / 15) stage-1 calls for P candidate pairs, plus about 2 x 4 stage-2 and about 4
   audit calls. P is known only after extraction, by code. Order of work: extraction first; then P is computed without any call; if 2 x ceil(P / 15) + 12 > the calls
   then remaining minus the 88 fixed calls still to spend, the gate stops there and the shortfall is reported (no partial gate). With dev's P = 501 the total is 135 + 68 + 12 = 215.
