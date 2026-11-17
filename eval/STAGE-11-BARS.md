# Stage 11 bars (pilots J1, J2, A1), committed ALONE before the first real call

Ticket GoodQA goodqa#16096. Pilot split only (94 gold questions); the test split is not read. Source of the numbers: PREREGISTRATION-V2.md section 4 (J1, J2), sections 1.2 and 7 (A1). Machine copy: `pilots/stage11_bars.json` (the J1 scorer refuses to run without it). Nothing here is a result.

## J1 judge validation (judge `claude-opus-5-5`, one item per call, benchmark prompts, JUDGE_SYSTEM)
Items: 60 deliberately wrong answers, 60 correct paraphrases, 40 borderline items; each category from distinct content clusters (plain Wilson interval over items, z = 1.96). Item generation, selection rule and audit prompt are in `pilots/j1_items.py` (committed before generation, commit 79c377c).
- Leniency: wrong-answer false-accept rate, Wilson upper bound at most 0.10. In items: at most 1 of 60 (2 of 60 gives 0.114).
- Strictness: correct-paraphrase false-reject rate, Wilson upper bound at most 0.15. In items: at most 3 of 60 (4 of 60 gives 0.159).
- Audit agreement on the 40 borderline items, Wilson lower bound at least 0.80. In items: at least 37 of 40 (36 of 40 gives 0.769).
- `DEVIATION: agent audit, not human`: the 40 borderline items are audited by a second model (Codex, `gpt-6.1-sol`, medium effort, one item per call, blind to the judge's verdicts, audit prompt `j1-audit-1` committed first), by the user's choice; the agreement is against a second model, not against a human.
- Control (broken implementation): the judge given the gold answer of ANOTHER question (same type, different cluster) for 20 correct paraphrases must mark at least 18 of 20 wrong; if not, the paraphrase probe does not depend on the gold and J1 is void.
- Scorer controls (unit tests, no calls): an always-yes verdict set fails the leniency bar, an always-no set fails the strictness bar, a gold-ignoring judge fails the control, and the scorer withholds on any missing, duplicate or wrong-model row.
- G-judge passes iff all four of the above pass. If it fails, J2 does not run (V2 section 1.2) and the result is reported blocked; A1 is retrieval-scored and is not gated by J1.
- Items where the judge disagrees with the by-construction label are listed individually; borderline items have no construction label.

## J2 headroom (only if G-judge passes; separate go from main)
G-headroom: gold-only reader minus plain RAG (RRF, 4,000 tokens) at H3, judged accuracy, paired difference at least +0.10 with the paired lower bound above 0 (content-cluster bootstrap, 2,000 resamples, seed 16096). Also reported, no bar: the same at H1 and H2, the full-haystack H0 accuracy, and the interval half-width as the noise floor of answer-level deltas. Calls: 94 gold-only + 282 RAG (H1, H2, H3) + 94 full-history readers, one judge call each = 940 (the draft's "about 1,130" does not follow from its own list of arms: arithmetic inconsistency, recorded). Controls (ASSUMPTION, owner: floor value not stated in V2): a no-context reader and a wrong-context reader (another question's H3 context), each on 30 questions, must score at most 0.10.

## A1 analyst re-pilot
`ASSUMPTION (main): A1 run per PREREG-V2 section 7, not the 376-call section 4 row`: H2 only, A-REORDER and A-DIG at depth 300, shuffled order seed 16096, model `claude-sonnet-5-5`, 188 calls, same prompts as Stage G (`reorder-prompt-g1`, `analyst-prompt-g1`). A1 has no pass bar; its output is threshold values for V2 sections 4 and 7. Shuffle-order control, using Stage G's paired intervals and no new calls: the Stage G shuffled arms against the earlier unshuffled 50-candidate ANALYST at H3 were A-DIG +0.088 [+0.034, +0.144] and A-REORDER +0.097 [+0.038, +0.160]; the shuffle is read as showing no anchoring cost iff a re-computed interval's lower bound is above -0.05 (ASSUMPTION, owner; main may change it before the first A1 call). Wrong-candidates control: an A-REORDER call given another question's 300 candidates (same level) on 30 questions must give session recall@10 at most 0.05.

## Stopping
A usage or rate limit stops the run (STOPPED.json), is recorded and reported; no other model or account is tried. The Codex audit never falls back to a key.
