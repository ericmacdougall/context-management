# Claims audit for DESIGN.md v2

Ticket: GoodQA goodqa#16096. Branch docs/design-v2. This file traces every numeric claim in DESIGN.md to the file and field it came from. Source keys are defined in DESIGN.md section 0. Paths are relative to the lab work directory: the eval branch is `elastic-memory-lab-eval/eval/`, the review reports are in `research/` and the two agent review folders.

## How the check was done

1. A script (`docs-tools/numerals.py` in the work directory, not in the repo) lists every numeral in every table row of DESIGN.md, after removing identifiers. Exclusion rule: git SHAs and hex ids, WAL entry ids, row and section ids (3.2.1, #12, F4, N7, H3, M1, X04, E9b, MEM-14), version numbers (9.5.6, 1.32.2, v2), dates, the ticket number, file names, inline code, and HTTP status codes in prose (200, 400, 403, 500). Result at the time of commit: 136 rows with 372 numerals.
2. Every numeral in sections 1 to 3 was matched to a row of the table below. Section 5 numerals are UNCOVERED-TOP30 rank numbers and item ordinals, not claims. Section 6 numerals are the contradictions themselves; their sources are named in section 6 and repeated in the last table here. Numerals that are only references to other rows (for example "UNCOVERED-TOP30 #19", "owner question 6", "PREREG 4.3", "row 27", "phase 1", "ADR 0002"), HTTP status codes, and the illustrative versions 3 and 4 in row 3.12.3 are not claims and are not listed. A second script pass listed every DESIGN row in sections 2 and 3 whose numerals have no row here; after adding 3.3.7 and 3.3.10, every remaining entry on that list was one of these references.
3. Check column:
   - **field**: read from the named JSON field in this session by a script.
   - **text**: found by `grep` in the named report or notes file in this session.
   - **carried**: taken from my earlier reading of the source in this task; not re-read after the context reset. Lower confidence; listed so a reviewer can re-check.

## Section 1 (what the design cannot yet claim)

| Claim | Value | Source file | Field or location | Check |
|---|---|---|---|---|
| RRF session recall@10, own haystack | 0.968 | CURVE | levels.H0.rrf.session_recall@10.mean (0.9681) | field |
| RRF session recall@10, whole pool | 0.4754 | CURVE | levels.H3.rrf.session_recall@10.mean (0.47535) | field |
| Pilot size | 94 questions, 90 clusters | CURVE; AFULL | n per level; effective_n 90 | field |
| Drop H0 to H3 and 95% interval | -0.493 [-0.579, -0.401] | CURVE | levels.H3.rrf.session_recall@10.drop_vs_H0.mean_diff, ci_low, ci_high | field |
| Mean gold-session recall in a 4,000-token context, H3 | 0.41 | RAGCTX | arm rag, level H3, budget 4000, gold_session_recall, mean over 94 non-abstention questions (0.4124; my aggregation) | carried |
| Any / all gold session present at 4,000 tokens, H3 | 0.53 / 0.33 | XP | xp_e2.json x01_budget_presence.H3.flat.4000.any_gold_session.rate (0.533), all_gold_sessions.rate (0.328) | field |
| Stale-only / current, knowledge-update, H3, 4,000 | 0.467 / 0.333 (n 15) | RAGCTX | stale_only, gold_latest_in_context over the 15 knowledge-update rows (my aggregation) | carried |
| Same, analyst run's RRF baseline | 0.667 / 0.267 | AFULL | runs.H3.summary.all.RRF.failures_counted.b4000.ku_stale_only, ku_gold_latest | field |
| Two-stage S = 20 vs flat | 0.462 vs 0.481, -0.019 [-0.074, 0.040] | XP | xp_e2.json e2.two_stage_S20.mean_session_recall10, flat_mean, diff_vs_flat | field |
| Planted-gold control vs bar | 0.504 vs 0.90 | XP; LEDGER X02 | EXPLORATORY.md E2; LEDGER row X02 | text |
| Confidence predictor AUROC vs null 97.5th percentile | H1 0.831 / 0.718; H2 0.727 / 0.664; H3 0.572 / 0.631 | XP | xp_e3.json levels.*.auroc_cv, null_p97_5 | field |
| Analyst vs RRF, H3 | 0.473 to 0.603, +0.130 [+0.060, +0.202] | AFULL | runs.H3.summary.all.RRF/ANALYST.failures_counted.session_recall_at10; paired."ANALYST minus RRF".session_recall_at10 | field |
| Analyst vs rerank, H3 | +0.024 [-0.033, +0.079], n 78 | AFULL | paired."ANALYST minus RERANK"; rerank_questions_available 78 | field |
| Analyst vs analyst-rerank-only, H3 | +0.022 [-0.027, +0.070] | AFULL | paired."ANALYST minus ANALYST-RERANK-ONLY" | field |
| Low-overlap, analyst, H3 | 0.072 (23 questions) | AFULL | runs.H3.summary.low_overlap.ANALYST.failures_counted.session_recall_at10 | field |
| Gold in the 50 candidates | 7 of 23 | CANARY; PREREG results section | PREREG "gold is in the 50 tier-0 candidates for only 7 of the 23" | text |
| Links correct / updates linked / bar / flips | 16 of 16; 10 of 16; 12; 9 of 25 | LR | GATE3_RESULT.md rows G4 (16/16), G5 (10/16, needs 12); order-flip line | text (9 of 25 carried) |
| Micro-pilot, no headroom | 9 of 10 | PREREG 4.1; LEDGER critic correction | LEDGER line "n=10 micro-pilot (9 of 10)" | text |
| Round-level parity with Elasticsearch | 0.751; 0.817 | PREREG | section 4.3 item 5(c) (es_round_parity.json) | text |
| Terms cap and lease | 65,536; 120 s | RV-WP | F1, F2 | text |

## Section 2 (purpose and fixed points)

| Row | Value | Source file | Field or location | Check |
|---|---|---|---|---|
| 2.2.2 | 75 dropped doorbells; "lost 6 of 6" | RV-ENG F8 row; LEDGER "Held" row 2 | review table F8; LEDGER (owner count) | text |
| 2.2.3 | 54 duplicates; 825 results for 800 inputs | RV-ENG F4t row | "ledger duplicates: 54", "825 result events for 800 inputs" | text |
| 2.2.6 | 10 of 16 | LR | G5 row | text |
| 2.2.7 | 12 material findings each | LEDGER "Did not hold" | row "Adapter and write-path passes" | text |

## Section 3 (components)

| Row | Value | Source file | Field or location | Check |
|---|---|---|---|---|
| 3.1.2 | 16 writers | SYS C7_writers.json | workload ("16 processes") | field |
| 3.1.3 | 20 of 20 zombie appends | RV-WP | "Zombie row (row 1)" | text |
| 3.2.3 | p50 1,001 ms over 200 writes | BEH E1_refresh.json | delay_active_ms (1001) | text |
| 3.2.4 | 100 of 100; 600 events | GOT CORRECTIONS | line on ORC-14, EFX-02 (C2_lag_tail.json) | text |
| 3.2.11 | up to about 0.08; oversample 3 | PREREG | 4.3 item 5(b) (es_hybrid_pooled.json) | text |
| 3.2.12 | 5% filter | RV-ES | E6 direction | carried |
| 3.2.14 | 0.718 | PREREG | 4.2 and section 9 (es_e5.json) | text |
| 3.3.7 | 65,536 terms | RV-WP | F1 (Elasticsearch default terms limit, from vendor docs) | text |
| 3.3.10 | one run (R = 1) | RV-WP | N6 PARTLY | carried |
| 3.3.8 | 120 s lease; about 200,000 documents | RV-WP | F2 model table ("200k or more", 0 erasures; rate about 1,600 docs/s, ASSUMPTION) | text |
| 3.3.9 | about 30 s | RV-ES | erasure claim (2), lease sync after about 30.4 s | carried |
| 3.3.16 | 40 erasures, 254 s | GOT DESIGN-CHANGES | "still missing": erasure amplification | text |
| 3.4.2 | 2,351 of 2,373, 99.1% | PF PROVENANCE_RESULT.md | headline | text |
| 3.4.3 | 8 mismatches, 53 ambiguous | PF PROVENANCE_RESULT.md | author check | carried |
| 3.4.4 | 9.00, 5.12, 4.46, 2.71 at 1, 4, 8, 16 | PF DEVLOG.md | batch-size curve | text |
| 3.4.8 | 9 of 10; 16 of 16 | PF ID_AUDIT.md | re-test with opaque ids | carried |
| 3.5.2 | 200 runs; 74 and 71 | GOT CORRECTIONS | F3 line (F3_inflight.json) | text |
| 3.5.5 | 10 of 10 | RV-WP | N7 cases (a) to (c); THRESHOLDS R = 10 | carried |
| 3.6.1 | 1,716 renders, 0 malformed | RV-AD confirm | N5 ("1716") | text |
| 3.6.3 | 4,000 tokens | PREREG 5.1; budget_pilot.json | proposed_budget_0.03 = 4000 | carried |
| 3.6.4 | 0.53, 0.62, 0.71, 0.79 at 4k, 8k, 16k, 32k | XP | xp_e2.json x01_budget_presence.H3.flat.{4000,8000,16000,32000}.any_gold_session.rate | field |
| 3.6.7 | 7.59 per scan; 91% | RV-SYS | "Result claims the raw files do not support", I3 | text |
| 3.7.1 | rank constant 60 | PREREG | 4.3 item 3 (grid_pilot.json) | carried |
| 3.7.2 | 0.968, 0.859, 0.684, 0.4754; BM25 0.494, linear 0.492, dense 0.345 | CURVE | levels.H0..H3.{rrf,bm25,linear,dense}.session_recall@10.mean | field |
| 3.7.3 | 23 of 94; rank 100 | PREREG 3.2; xp_e2.json low_overlap_n (23) | lowoverlap_pool_h3.json | field |
| 3.7.4 | AUROC 0.687; null 0.448 to 0.547 | XP; LEDGER X02 | xp_e2.json label_blind_probe | text |
| 3.7.5 | top 20; about 64 s at top 50 | rerank_timing.json | mean_seconds 64.15 (5 questions) | field |
| 3.7.8 | 0 gold sessions removed | PREREG section 2 | verify.json | carried |
| 3.7.10 | +0.059 [+0.016, +0.113]; 78 of 94 | AFULL | runs.H3 paired."RERANK minus RRF"; rerank_questions_available | field |
| 3.8.2 | 46 calls; 3.59 tool calls; about 100k tokens and 22 s; 31k tokens and 10 s | PF DEVLOG.md; CANARY | analyst canary lines; analyst_canary_h3.json tool-call mean | text |
| 3.8.3 | 330 s lease; +40 s and +300 s | RV-AD confirm | N7 | text |
| 3.8.4 | 30 s lease | RV-AD | N7 at 888c94b | carried |
| 3.8.11 | 20 calls $0.7751; 20-turn session $0.1246 | MC h3_rerun_head.json; RV-AD | runs (a) and (b) | text ($0.7751); carried ($0.1246) |
| 3.8.12 | +0.130 at H3; H2 0.660 to 0.758, +0.099 [+0.023, +0.175]; H1 +0.031 [-0.031, +0.089]; failures 2.1% to 4.3% | AFULL | runs.{H1,H2,H3}.summary.all paired."ANALYST minus RRF"; rejection_rate | field |
| 3.8.13 | 0.93 / 0.07; rerank 0.80; RRF 0.27 (n 15) | AFULL | runs.H3.summary.all.{ANALYST,RERANK,RRF}.failures_counted.b4000.ku_gold_latest, ku_stale_only | field |
| 3.8.14 | 42 of 92 with no tool | PREREG results section | "Reading of the pilot numbers", tool-use line | text |
| 3.9.6 | 11 of 12; F1 0.35 | MC NOTES.md | "Review corrections" | text |
| 3.9.8 | about 10.5k tokens | PF DEVLOG.md; h3_rerun_head.json first_turn_input (10,567) | cost line | text |
| 3.11.1 | 34 exits | RV-ENG | F4t row | text |
| 3.11.3 | 20 of 20 replays | RV-ENG | F5a row ("0/0/20 failures", 20 sessions) | text |
| 3.11.8 | 4 of 4 stuck | LEDGER "Did not hold"; ENG F6 | owner count; RV-ENG confirms the rollback failure | text |
| 3.13.2 | 100 names, 2 prefixes, 0 collisions | RV-AD confirm | tenant boundary | text |
| 3.14.9 | 120 s (120000 ms); run at 124.5 s | MC NOTES-addendum-g7.md | variant 3 | text |
| 3.14.10 | 20 s; 25 s | MC NOTES-addendum-g7.md | fail-open template | text |
| 3.14.12 | expiry 2026-12-15 (date, excluded from the count) | GOT NEW-GOTCHAS.md | MEM-14 | text |
| 3.15.2 | 12 unit tests | engine commit ffa4ce5 message | Testing line | text |

## Section 6 (contradictions): the numbers on each side

| # | Values | Sources |
|---|---|---|
| 1 | 0.46 / 0.48 / 0.4754 / 0.481 | OPEN-QUESTIONS (lab main) section H; LEDGER row "Hybrid retrieval alone"; CURVE; xp_e2.json e2.two_stage_S20.flat_mean |
| 2 | 24 vs 23 of 94 | OPEN-QUESTIONS (lab main) section H; PREREG 3.2 |
| 3 | 2,301 and 58 vs 2,351 and 53 | LEDGER at 603bb3b row X04; PF PROVENANCE_RESULT.md |
| 4 | 200/202, 19/18, 15/13, 100/107 s | ENG NOTES; RV-ENG F4b, F4d, P4, F7 |
| 5 | 4.34 s (max 8.55) vs 4.12 s (max 7.77) | PREREG 4.3 item 4; CURVE seconds_per_question.H3 mean 4.1196, max 7.7694 |
| 6 | 6.5 vs about 6.2 (0.7751 / 0.1246 = 6.22) | LEDGER "One fresh headless call per step"; h3_rerun_head.json |
| 7 | about 26 s vs about 64 s | OPEN-QUESTIONS (lab main) section H; rerank_timing.json mean_seconds 64.15 |
| 17 | H3 0.4754 vs 0.473; H2 0.684 vs 0.660 vs 0.695 | CURVE; AFULL runs.*.RRF; PREREG 4.3 item 5(b) "exact: H2 0.695, H3 0.473" |
| 18 | 0.333 / 0.467 vs 0.267 / 0.667; 0.412 vs 0.421 | RAGCTX; AFULL runs.H3.RRF b4000 |

## Untraced claims

These figures appear in the inputs but no artifact behind them was found. None is used as a claim in DESIGN.md v2; each appears only in section 6 as a contradiction or not at all.

| Figure | Where it appears | What was found instead | Treatment |
|---|---|---|---|
| "about 26 s per question" for the cross-encoder rerank | OPEN-QUESTIONS (lab main) section H intro | rerank_timing.json: mean 64.15 s per question at top 50 (5 questions) | Removed from OPEN-QUESTIONS; flagged in DESIGN section 6 row 7 |
| "20 calls cost 6.5 times one 20-turn session" | LEDGER "Did not hold" | h3_rerun_head.json gives $0.7751 / $0.1246 = 6.2 | DESIGN uses the dollar figures; flagged in section 6 row 6 |
| "0.46" whole-pool recall | OPEN-QUESTIONS (lab main) section H intro | Superseded old-sets value (PREREG 4.3 "H3 0.461 to 0.475"); LEDGER critic correction drops it | Replaced by 0.4754 in OPEN-QUESTIONS; flagged in section 6 row 1 |
| "0.48 at the whole pool" | LEDGER "Did not hold" row 1 | No file gives RRF 0.48 at H3; 0.481 is the E2 flat baseline at a different fusion depth | Not used; flagged in section 6 row 1 |
| Engine NOTES figures 200, 19, about 15 turns/s, 100 s | ENG NOTES | Result files: 202, 18, 13, 107 s (RV-ENG) | Not used; flagged in section 6 row 4 |
| PREREG H3 time per question 4.34 s (max 8.55 s) | PREREG 4.3 item 4 | curve_pilot.json gives 4.12 s (max 7.77 s) | Not used in DESIGN; flagged in section 6 row 5 |

Figures in DESIGN.md that rest on an ASSUMPTION inside their source, and are labelled so in the design:

| Figure | Row | Assumption |
|---|---|---|
| About 200,000 documents as the erasure livelock limit | 3.3.8 | RV-WP model uses an assumed rebuild rate of about 1,600 documents/s taken from an earlier old-code measurement |
| 20 s and 25 s fail-open budgets | 3.14.10 | Template not run live; the Kibana step keys are an ASSUMPTION in the source |

## Figures in OPEN-QUESTIONS.md that are not in DESIGN.md

| Figure | Row | Source | Check |
|---|---|---|---|
| 28 at a cut of 50, 35 at 20 | B3 | PREREG 3.2 | carried |
| G4 precision 20 of 32 | B6 | PF GATE_RESULT.md | carried |
| 0.863 (client linear vs Elastic linear) | A11 | PREREG 4.3 item 5(c) | text |
| 1 detection in 3,600 (freshness vector leg) | I3 | RV-SYS | carried |
| about 5,100 calls to resume fact extraction | M10 | main WAL (ask text: "5,100 calls for the pilot plus 500 distractors") | text |
| 16 of 16 (reader picks the current value) | I intro | PF (owner-reported) | carried |
