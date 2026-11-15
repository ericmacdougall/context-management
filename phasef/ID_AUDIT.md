# Session-id leakage audit and re-test (opaque ids)

Real LongMemEval gold session ids start with `answer_`; filler ids are `sharegpt_`, `ultrachat_` or hex. Audit by code reading (phasef and rescue branch) plus logged hypotheses.

| Stage | Real id visible to the model? | Results that depended on it |
|---|---|---|
| Reader context ORACLE and FULL (render_session header "### Session <id>") | yes | pilot-10 ORACLE 9/10, FULL 9/10; two FULL answers echoed an answer_ id (078150f1, 311778f1). Re-tested: unchanged |
| Reader context FACTS v1 (stated in session <id>, excerpt headers) | yes | FACTS 2/4 on the 4 questions |
| Reader context FACTS v2, v3 (gate v3 FACTS3 16/16), NOLABEL (rescue line 511) | yes | dev 16/16 and NOLABEL 16/16. Re-tested: unchanged |
| Reader contexts of rescue v4/v5 and gate 3 on dev2 | yes (same builders) | gate 3 dev2 answers and "labels change no answer" on dev2: NOT re-run |
| Extraction v1 and v2 ("### SESSION id=<id>"), including the 668-session gate-v3 run, the rescue dev2 run, the quality sample and the batch curve | yes | all stored extractions. Re-extraction test below |
| Question expansion | no (question text and date) | none |
| Slot canonicalisation | no (slot keys, cardinality, modalities) | none |
| Link judge v4.0 and v4.1, shuffled verifier, rescue v5 judge, verifier and order check | no (dates, fact statements, quotes, round text) | none through ids |
| Link audits (v3 quote audit, v4 rounds audit, rescue audits) | no | none through ids |
| Candidate generation, answer-fact and gold-link identification, G2/G3/G5 scoring | no model | none |
| Gate 3 gold labelling (rescue gate3.py GOLD_TEMPLATE: "[session <id>, date]" and render_sessions of the gold sessions) | yes (gold ids only, and the labeler already received the gold sessions and the answer) | the gold-update labels behind recall 10 of 16; ids add no information the labeler lacked, but it is a visible-id stage |
| Answer judge | only when the hypothesis echoed an id (the two FULL answers) | none (judge compares with the gold answer) |
| Analyst dry run v0 (fake backend candidates) | yes | dry run only; real task files are opaque |

Fix: phasef/anon.py (same function as eval/emlab/anon.py, test with 20 hard-coded ids from that module; no opaque id contains answer, sharegpt or ultrachat), all renderers print opaque ids, extraction parsing maps the echoed opaque id back, recipes bumped (reader-v1.1-anon, reader-v1.2-anon, extract-v1-anon, extract-v2-anon), llm.py PromptLeak guard (patterns answer_<hex>, sharegpt_, ultrachat_, plus an optional set of known real ids) checked before the cache lookup; guard tests fire.

Re-test with opaque ids (cache dir phasef-out/anon, 74 of 160 calls): ORACLE 9/10 -> 9/10 and FULL 9/10 -> 9/10 with identical per-question outcomes (the one miss is the abstention question in both); dev FACTS (labels) 16/16 -> 16/16; NOLABEL 16/16 -> 16/16.
Re-extraction of 99 sessions of 07741c44 and 07741c45 at 8 per call with opaque ids: facts per session 4.37 -> 4.31 on non-gold sessions and 5.75 -> 5.50 on the 4 gold sessions; 73% to 78% of old facts have a lexical match (Jaccard 0.4) in the new list, consistent with run-to-run variation. No sign that ids changed what was extracted.
