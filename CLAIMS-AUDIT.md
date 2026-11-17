# Claims audit for DESIGN.md v3

Ticket: GoodQA goodqa#16096. Branch docs/design-v3. Every number in DESIGN.md v3 is listed here with the file and place it came from. Source keys are defined in DESIGN.md section 1. Paths are relative to the lab work directory: LEDGER.md and the docs are in `elastic-memory-lab/` (main f746fef); eval files in `elastic-memory-lab-eval/eval/` (feat/eval-scaffold 5bb9c5a); `research/` holds the review reports; the adapters review is `agent-a9f5acd2dec569d7c/delta-review-888c94b.md`.

## How the check was done
1. Every numeral in DESIGN.md v3 was extracted by script (`docs3-numerals.py` in the work directory, not in the repo), excluding identifiers (short commit hashes, WAL ids, row and section ids such as D1 and R3-7, finding ids, dates, version numbers and the ticket number). The script then confirmed each numeral occurs in this file. This is a coverage check, not a proof of correctness: for small integers the match is trivial, so those rows were checked by reading.
2. Check column: **file** = read in the named file in this session; **ledger** = taken from LEDGER.md, which cites the owner or reviewer artifact; **derived** = arithmetic on traced numbers, shown.
3. I did not open the underlying result JSON files. For figures not read in an eval or review file, LEDGER.md is the source; where LEDGER and a report both state a number they agree unless listed under Contradictions.
4. Real-session figures come only from the LEDGER rows. That is deliberate: the two real-session branches contain private strings in their history and were not opened.

## Section 0: where this stands
| Claim | Values | Source | Location | Check |
|---|---|---|---|---|
| Components table statuses and counts | HISTORICAL (superseded; current counts are 9, 6, 3, 5, 5, see Corrections after the independent reviews 2026-11-17): 28 rows; 7, 8, 3, 5, 5 | DESIGN.md | counted from the table | derived |
| Typed-log review | one session | LEDGER | Held, row "Typed decision log reduced..." | ledger |
| Replication of quote check | 2,351 of 2,373 | LEDGER | Independent replication table | ledger |
| Link recall | 10 of 16 against a bar of 12 | LEDGER; PREREG-V2 | Did not hold row on links; section 8 | file |
| Extraction stability | 77.6%, gate 85% | LEDGER; STAGE-X0C | X0C table | ledger |
| Whole-pool plain retrieval | 0.968 (own history), 0.4754 (whole pool), 94 questions | CURVE levels.H0 and H3 rrf session_recall@10; LEDGER Corrections | LEDGER "Did not hold" row 1 and Corrections | ledger |
| Analyst reorder over 300 | 0.700; plain depth-300 0.481; cross-encoder 0.528; +0.219 [0.146, 0.292]; +0.172 [0.105, 0.243] | STAGE-G | section 9.1 table and paired lines | file |
| Judged accuracy | 0.384, 0.582, 0.911; 77 questions; 4,000 tokens | STAGE-G | section 9.2 table | file |
| Real session harm per answered item | 0.125 log; 0.27 to 0.50 search | LEDGER | Held, real-session row ("0.125 for the log versus 0.50 and 0.27 for search") | ledger |
| Untyped position changes | 28% (20 of 71); 0.33 to 0.34 (23 of 67) | LEDGER | Did not hold, "typed decision log alone" row | ledger |
| Facts X2 headline | 0.862 control; 0.894, +0.032 [-0.032, +0.104], 7 wins 4 losses; 0.904, +0.043 [+0.010, +0.092], 4 wins 0 losses; 94 questions; 1,098 sessions; 0.091 to 0.455; other 12 low-overlap (also used in X1), 1 win 1 loss; 11 seen questions | STAGE-X2 | section 7.3 table, 7.5 | file |
| Pause date | resets 2026-11-17 08:00 America/Vancouver | WAL | w205c473bd1a, wc66de476c06 | file |

## Section 2.1: log, fence, erasure
| Claim | Values | Source | Location | Check |
|---|---|---|---|---|
| Fence reviewed | 14 of 14 schema attacks refused | RV-WP delta | status table F3 row | file |
| Erasure at 100,000 events | done, residue 0 | RV-WP delta | F1 row | file |
| Secret scan | 880 of 880; 2 false blocks in 3,862 clean files | LEDGER | write-path section, Held | ledger |
| Anchor, seal | 10 of 10 each | LEDGER | write-path section, Held | ledger |
| Tuned scanner held-out (HISTORICAL; superseded by 300 of 800 under the corrected predicate, then 800 of 960 on a fresh set) | 350 of 800 (44%); 400 of 400 tuned | LEDGER | Ingestion Did not hold | ledger |
| Review findings D1 to D6, F-ids | ids only | RV-WP delta | New material issues | file |

## Section 2.2: Elasticsearch and Basic path
| Claim | Values | Source | Location | Check |
|---|---|---|---|---|
| Visibility | 1,002 ms; 522 ms | LEDGER | Independent replication table | ledger |
| bbq_disk | 40 of 40 documents fail | LEDGER | Replication table, licence matrix | ledger |
| Degrade rehearsal | 387,045 responses (original ten lives; 433,464 over all twelve), 0 mismatches; 134,290 writes, 0 lost; 0.03 to 0.06 s; 1 of 12 lives recomputed | LEDGER; DEGRADE.md | Licence transitions section; evidence ids | ledger |
| Traps | 60 of 60 queries differ; 5 of 5 restarts stayed on Basic; about 45 requests per second | LEDGER | Licence "Did not hold" table | ledger |
| Parity | 0.751; 0.8167; 0.9637; rescore_vector 0.039 (CI -0.078 to -0.005) | LEDGER | Replication table and "Did not replicate" list | ledger |

## Section 2.3: retrieval, analysts, decisions
| Claim | Values | Source | Location | Check |
|---|---|---|---|---|
| Pool levels | 0.968, 0.859, 0.684, 0.4754; BM25 0.494; linear 0.492; dense 0.345 | CURVE levels.H0 to H3 session_recall@10.mean; the v2 audit traced these by field (row 3.7.2) | v2 CLAIMS-AUDIT, section 3 | file (v2) |
| Candidate depth | 30% (50), 39% (100), 48% (200), 65% (400), 70% (1,000); 23 hard questions | LEDGER | Did not hold, "Shallow candidate lists" row | ledger |
| Analyst reorder settings | 300 candidates; at most 20 ids | STAGE-G; PREREG-V2 section 7 | Stage G paragraph; 9.1 | file |
| Cross-encoder length-matched | +0.020 (CI -0.004 to +0.049) | LEDGER | Did not hold, length-controlled row | ledger |
| Analyst length-matched | +0.098 (CI 0.041 to 0.157) | LEDGER | same row and Reconciliation table | ledger |
| Typed log independent | 28 of 40, 4 harmful; search 16, 7 harmful; agreement 0.70 | LEDGER | Held real-session row | ledger |
| Gap closing rule | 14 correct, 1 false, 1 ambiguous; 1 of 23 | LEDGER | Did not hold, "typed decision log alone" row | ledger |

## Section 2.4: extraction
| Claim | Values | Source | Location | Check |
|---|---|---|---|---|
| Volume mechanics | 800 of 800, 1 repaired; 0.955 raw; 0.991 repaired; 13 authors filled | LEDGER | Stage X1 table | ledger |
| Earlier quote check | 2,351 of 2,373; 8 and 53 | LEDGER | X04 row | ledger |
| Batching | 9.0, 5.1, 4.5, 2.7 at 1, 4, 8, 16 | LEDGER | Did not hold row 2 | ledger |
| Cap | 28% of sessions; 49% of gold vs 14% of random; 11.7 vs 8.6 facts; +33% output; +24% time | LEDGER | X0 table; X1 table | ledger |
| Stability | 77.6%; gate 85%; lowest 51%; about 31% more distinct facts; 73% vs 42%; three draws | LEDGER | X0C table and post-hoc analysis | ledger |
| Event time and preference bar | 10%; 6.9 to 8.0; +2.0; +1.1 | LEDGER | X0, X0C | ledger |
| Candidate tests | 2 of 11; rule 6; dense 4, fused 5, raw controls 1, 0, 1; 1,000-session pool | LEDGER | X1 table; dense and fused table | ledger |
| X2 rows | 0.949 to 0.821 vs 0.923 to 0.718; 39 questions; 200 to 1,098 sessions; 6%; 1 win 2 losses of 6; D1 to D3 ids; +0.130 vs +0.15 | STAGE-X2 | sections 2, 7.3, 7.4, 7.5, 7.6 | file |

## Section 2.5 and 2.6: composition, runtime, adapters, engine, ingestion
| Claim | Values | Source | Location | Check |
|---|---|---|---|---|
| Review ids R3-1 to R3-8, D-ids | ids only | RV-POL; RV-WP | named sections | file |
| Adapters | 100 names, 2 prefixes, 0 collisions; 20 of 20; 330 s; 1,716 renders; 9 crafted replies | LEDGER | Held rows (adapters) | ledger |
| Engine | 800 = 800 = 800; 13 duplicates; 4,045 vs 31,458; 4,121 vs 29,471; 68 events per second; 2,400 | LEDGER | Held rows (durable sessions, Temporal) | ledger |
| Ingestion (HISTORICAL figures as first recorded; 20 of 20 exactly-once is WITHDRAWN, 12 of 20 is in no result file and is replaced by 8 of 20, 350 of 800 is replaced by 300 of 800 then 800 of 960: see the 2026-11-17 corrections) | 40 of 40; 20 of 40; 20 of 20; 7 placements; 21 of 28; 12 of 12; 12 of 20; 350 of 800 | LEDGER | Ingestion tables | ledger |
| Seal and composition | no numbers | SEAL; COMP | docstring; items 1 to 8 | file |

## Section 3: dead ideas
| Claim | Values | Source | Location | Check |
|---|---|---|---|---|
| Hybrid alone | 0.4754; context recall 0.41 | LEDGER | Did not hold row 1 | ledger |
| Two-stage | 0.462 vs 0.481; -0.019 [-0.074, 0.040]; 0.504 vs 0.90 | LEDGER X02 | Outside ideas table | ledger |
| Confidence gate | 0.831, 0.727, 0.572 vs 0.631; +500 and +3,900 | LEDGER X05 | Outside ideas table | ledger |
| First-stage variants | 0.051, 0.104, 0.124 vs 0.118 | LEDGER | Did not hold row on cheap variants | ledger |
| Cross-encoder top 200 | 0.081 vs 0.041; +0.041 [-0.020, 0.114]; about 243 s | LEDGER | same | ledger |
| Digging | -0.010 [-0.043, 0.024]; 50 of 91; -0.027; 10 questions; 7 dug; 0.000 | STAGE-G; LEDGER | 9.1 and Did not hold rows | file |
| Links | 16 of 16; 10 of 16; 9 of 25 | LEDGER; v2 audit | Did not hold row on links | ledger |
| Facts as candidates, preference, all-gold | 2 of 11; 6; 0.609, 0.565, 0.609, 0.783 | LEDGER; STAGE-X2 | X1 row; 7.3, 7.7 | file |
| Date filter | 9 of 94; 20 of 26 temporal | STAGE-X2 | 4 (1) and 7.7 | file |
| Untyped changes | 28% to 0.34 | LEDGER | see Section 0 | ledger |
| Review counts | 12 material findings each | LEDGER | "Adapter and write-path passes" row | ledger |
| Write-path first review | 65,536 | RV-WP | F1 | file |
| Purge wait | about 30 s | LEDGER | Did not hold row on deleting text | ledger |
| Session cost | 6.2 = 0.7751 / 0.1246 (rounded) | LEDGER | Did not hold row on headless call | derived |
| Rollback | 4 of 4 | LEDGER | Did not hold row | ledger |
| Timeout | 120 s | LEDGER | Agent Builder row | ledger |
| Held-out scanner, truncation (HISTORICAL; superseded: 300 of 800 then 800 of 960 on a fresh set; 8 of 20 not 12 of 20) | 400 of 400, 350 of 800; 12 of 20 | LEDGER | Ingestion Did not hold | ledger |
| Evidence hygiene | 0 of 276 | LEDGER | Did not replicate list | ledger |

## Section 4: unsolved
| Claim | Values | Source | Location | Check |
|---|---|---|---|---|
| No gold in 300 candidates | 11 of 94; 10 of 91 | PREREG-V2 section 14; STAGE-G 9.1 | file | file |
| Aggregation | 0.3 to 0.4; 22% = 1 - 0.783 | STAGE-X2 | 7.7 (3) | derived |
| Time | 9 of 94 | STAGE-X2 | 7.7 (1) | file |
| Erasure scale | 1,000,000 documents; 48 s; 90 s; 100,000 events; 109 s; 22 to 30 s; 60 s; 1,000 shards | LEDGER | write-path "Did not hold" table | ledger |
| Extraction cost | 333M input; 51M output; 58.5 h; 1,756 USD; 1,630 USD; 49 h; 22,500 calls | STAGE-X1 table; LEDGER | file | file |
| One in four | about 1 fact in 4 differs | LEDGER | X0C table | ledger |
| Judge | 22 to 32% | STAGE-G | 9.2 | file |

## Section 5: path
| Claim | Values | Source | Location | Check |
|---|---|---|---|---|
| Part R compute | 10 to 11 h; 1.5 h; 6 h; 0 calls | PREREG-V2 | section 3 | file |
| J1, J2, A1 | about 160 calls, 40 items; about 1,130 calls; 15 million tokens; 376 calls | PREREG-V2 | section 4 table | file |
| Part A | 1,400 plus 60; 70 million; 350 reorder calls; 1,460 | PREREG-V2 | sections 7, 12 | file |
| Part K | 8,400; 15,900; 26,000; 35,000 | PREREG-V2 | section 12 | file |
| Part X, S | 6,100; 22,500; 2,100 | PREREG-V2 | sections 8, 12, 14 | file |
| Split | 363 questions; 350 with retrieval gold | PREREG-V2 | header | file |
| Facts test | about 60 questions | STAGE-X2 | 7.8; coordinator message of 2026-11-16 (relayed) | file |
| Erasure cost targets | 300,000; 1,000,000 | RV-WP delta | D5 smallest experiments | file |
| Lock order, steps | list of 9 | COMP | item 5 | file |

## Section 7: decisions
All entries cite WAL ids: wb7ed3da806f (decided defaults), w7562bc5d6b7 (assumed defaults), waade37da9b7 (hold). Numbers 40 judge items and 100 facts: PREREG-V2 section 4 (J1) and section 14 (X-G1). The user message id 83329413-06c1-4512-8c54-9b791636062f is the user's cited message.

## Contradictions (sources disagree; canonical choice)
| # | Disagreement | Canonical, and why |
|---|---|---|
| 1 | Whole-pool plain retrieval: 0.4754 (CURVE, fusion depth 200); 0.473 (analyst_full file and a LEDGER row); 0.475 (LEDGER length-matched row); 0.481 (STAGE-G depth 300 list; EXPL E2 flat); 0.46 (superseded old sets) | 0.4754 at depth 200: LEDGER Reconciliation and Corrections name it. STAGE-G's +0.219 is against 0.481 and is stated as such |
| 2 | 0.700 (reorder over 300 shuffled, 94 questions, recall) vs 0.582 (judged accuracy of the earlier 50-candidate analyst context, 77 questions) | Not a disagreement; different arms and metrics. DESIGN labels both. STAGE-G 9.4 states the part B analyst context is the earlier run |
| 3 | 22,500-call extraction cost: 1,756 USD and 58.5 h (STAGE-X1 table; LEDGER X1 row "58 h") vs 1,630 USD and 49 h (LEDGER dense follow-up extrapolation); 6,100 calls 476 USD vs 442 USD | LEDGER names none. DESIGN reports the X1 table and mentions the other; both are notional |
| 4 | Untyped position changes 28% (20 of 71, owner labels) vs 0.33 to 0.34 (23 of 67, independent labeller) | LEDGER gives both; DESIGN gives both |
| 5 | Weak-list size: 10 of 91 ok (STAGE-G) vs 11 of 94 (PREREG-V2 section 14) | Same set; DESIGN states both |
| 6 | PREREG-V2 section 12 Part A 1,460 calls vs Stage G paragraph 350 reorder calls | Draft inconsistent; reported as written |
| 7 | PREREG-V2 lists Part X volume (6,100 and 22,500 calls) after the X1 rule stopped volume extraction | Reported; V2 predates X2 |
| 8 | Cap cost "about +1.5% overall" (X0B) vs +33% output, +24% time (X1) | LEDGER X1 row: the latter supersedes |
| 9 | Whole-pool fact count about 295,000 vs about 220,000 | LEDGER "Corrected" row (not used in DESIGN) |
| 10 | Quote check 99.1% (2,351 of 2,373), 0.960 (1,829 of 1,905, X0) and 0.955 raw, 0.991 repaired (X1) | Different samples and volumes; DESIGN uses the first and the X1 pair |
| 11 | Rerank time 26 s estimate vs 64 s measured | LEDGER Reconciliation: 26 s was never measured; not used |
| 12 | Analyst vs cross-encoder +0.024 (first report) vs +0.075 (recomputed) | LEDGER Reconciliation: recomputed; DESIGN uses it only as a "withdrawn claim" |
| 13 | Idempotency scope: COMP item 5 per session; RV-POL default per tenant, principal and key | Unresolved; listed as decision 9 |
| 14 | LEDGER Held rows still say "owner-reported, under review" for the engine; RV-ENG has since reviewed to 3423f4e | LEDGER durable-sessions row (reviewed with limits) is used |
| 15 | Stage X2 provenance: the score file is dated after the scorer's last commit, but STAGE-X2 7.1 says it cannot prove from files alone that no earlier score output existed | Disclosed in X2; DESIGN labelled X2 "not independently reviewed" (SUPERSEDED 2026-11-17: reviewed with limits, reviews/eval-x2delta/review-1.md) |
| 16 | Pilot analyst "saw candidates in rank order" (0.603) vs shuffled depth 300 (0.700): depth and order confounded | LEDGER says descriptive only; DESIGN does not compare them |

## Untraced list
| Item | Where it appears | Treatment |
|---|---|---|
| Which two build streams are paused | DESIGN section 0 | Main log says only "two Opus-rung streams" (WAL w205c473bd1a); DESIGN does not name them |
| "Shared root" wording for identity and tenant resolution | DESIGN 2.5 | No document uses the phrase; attributed to the brief in DESIGN, COMP quoted for the mechanism |
| Which search variant gives 0.27 and which 0.50 harm per answered item | DESIGN section 0 | LEDGER does not map them; DESIGN gives the range |
| Call count for the facts confirmatory test | DESIGN path item 14 | None in any source; stated as not yet estimated |
| Whether the write-path owner's fixes after 4960f50 exist | DESIGN component 2 | Only the main log statement at 4960f50 was read; no later commit read |
| Whether the ctxrt rescue has started | DESIGN component 12 | Main log says queued behind the policy rescue; no commit after 143fa52 was found |
| RV-SYS report | DESIGN section 1, component 20 | Cited through LEDGER and v2 only; not re-read |
| Independent review of the eval harness | component 23 | Cited through WAL ids wf989ae14f68 and w21b65218be1 and LEDGER Corrections; report files not re-read |

Figures resting on an ASSUMPTION in their source: erasure livelock model (RV-WP aaa0fec, not used in v3); idempotency scope, agent-propose-only, identifier rule and conflict notices (DESIGN section 7).

## Corrections after the primary-file check (2026-11-16)

Six read-only checkers compared DESIGN.md with the result JSON files, stage
documents, review reports and git logs (about 266 claims confirmed, 40
discrepancies reported, 42 statements changed). The checkers ran on the same
model rung as the author: this is a check, not an independent review. Main
confirmed the rows below from the files named.

| Design statement before | Primary source says | Source |
|---|---|---|
| Truncation with no witness: 12 of 20 falsely report complete | 0 of 20 detected, 8 of 20 falsely complete (20 of 20 in the first run). 12 of 20 is in no result file; LEDGER corrected | ingest/results2/item3-76b99c151ef3.json at 0801866 |
| Cross-encoder length-matched gain +0.020 (CI -0.004 to +0.049) | +0.023 (CI 0.000 to 0.051), random same-size pool +0.008, on the final 94 questions; +0.020 is the superseded 83-question value | eval/EXPLORATORY.md line 130; LEDGER reconciliation table |
| Analyst gain survives length matching at +0.098 | True for the earlier 50-candidate unshuffled analyst; the 300-candidate arm was not length-matched | eval/EXPLORATORY.md |
| 12 material findings each (write path, engine, adapters) | Adapters 12, write path 8 (F1 to F8), durable-session testbed 10 (H1 to H3, M1 to M7); LEDGER corrected | the three review reports |
| Evaluation harness REVIEWED-WITH-LIMITS (HISTORICAL; component 23 is LAB-EVIDENCE-UNREVIEWED as the next cell and DESIGN.md say; the ES-pilot scorer reviews of 2026-11-17 are in reviews/eval-espilot*/) | Latest independent verdict: "NOT safe to freeze Phase R at a81d42d"; fixes and freeze checked by main only. Now LAB-EVIDENCE-UNREVIEWED | agent-a63b2d2ca8f38c01c/delta_review_goodqa16096.md |
| Per-role Elasticsearch keys as lab evidence at 4960f50 | Code at 5fc4bd6 only; commit records py_compile and "run waits for the cluster" | git log, feat/writepath-rescue |
| Policy: "Postgres port not built" | Decision core in plpgsql exists on the branch (20 commits after 80bb6a7, unreviewed); the composition into the write-path trigger is what is not built | policy/COMPOSITION.md at 3fa69c1 |
| Freshness scan "failed" | Owner's pass withdrawn (7.59 false challengers per scan, hash-embedding stand-in); no real-embedder test exists | research/wr128embl.output statement 3.6.7 |
| Build path: freeze V2, then threshold pilots | V2 section 4: thresholds are fixed from pilots BEFORE the freeze; order reversed in the path | eval/PREREGISTRATION-V2.md line 57 |
| 1,630 USD and 49 h attributed to the dense follow-up | It is the Stage X0 estimate at cap 12, superseded by 1,756 USD and 58.5 h at cap 30 | eval/STAGE-X0.md line 75; eval/STAGE-X1.md line 76 |
| 20 of 20 refused "behind any HTTP gateway" | 5 named backend classes on 4 binds; relies on a self-declared flag (R3-5) | adapters delta review, cfaefee confirm |
| Provider 400 on `search_result` injection | From provider documents and an offline probe; no live call | research/ctxrt-review-1d19cd8.md |
| Elasticsearch parity: "client RRF equals Elasticsearch rrf at 0.8167" | 0.8167 is top-10 overlap over the exact legs (same order in 1%); over Elasticsearch's own legs it is 1.0; replication verdict partly | replicate/results/round_parity.json |

Other changes are added caveats (sample sizes, "after a fix", unreviewed later
commits on the write-path, policy and adapter branches, statements the reviews
predicted from code and did not run). Numbers added to DESIGN.md by these
changes and traced above or in the cited files: 0.023, 0.000, 0.051, 0.008, 83,
7.59, 0.955, 0.960, 0.55, 35, 0.47, 0.94, 0.58, 0.082, 0.10, 1.0, 1%, 400, 8, 10,
20, 5, 4, 7, 42, 47a932a, dcb311f, 38cf34a, 5c599d8, 5fc4bd6, b98da9a, 94035e8,
ec4191f, f7efdc1, a81d42d.

Not checked by anyone (27 items the checkers could not trace to a primary
file), among them: the ids-and-cursors-only property of the engine; the per-claim
mapping of the Elasticsearch re-run verdicts to component 4; the composition
order in section 5.

## Corrections after the independent reviews (2026-11-17)

Each row changes a statement in DESIGN.md or LEDGER.md because a review or a main check at a named commit found it false, overstated or superseded. Sources are under reviews/ and in the main WAL.

| Statement before | Now | Source |
|---|---|---|
| Ingestion: "exactly-once, ordered ingestion under duplication, reordering and racing ingesters, 20 of 20 seeds" | WITHDRAWN. A legitimate earlier message could vanish through content alignment with no flag while the exactly-once claim was true. The contract is a host-declared boundary (`new_count`), at-least-once with flagged repeats and a blocking `ambiguous_alignment` gap when undeclared and ambiguous | reviews/ingest-delta/review-1.md (N1); WAL wda2d06a705c; ingest/CLAIMS.md at d76e525 |
| Tuned scanner held-out 350 of 800 (44%) | 300 of 800 under the corrected predicate (a private-key body survived in 50 of 50); after the repair 800 of 960 (83.3%, Wilson lower 80.8%) on a fresh held-out set, 0 of 720 benign changed, four cells 0 of 40 | reviews/ingest-delta/review-1.md (F2); ingest/CLAIMS.md |
| Injected block never ingested as user content over 7 placements | 6 covered placements, 0 marker occurrences in 42 conversations; the seventh (`host_alters_block`) is not covered | ingest/CLAIMS.md |
| Write path secret scan 880 of 880 | Holds only on the author's corpus; the pre-registered held-out test FAILED, 483 of 1,169 and 150 of 750 | WAL w63f62ffdf2a; reviews/writepath/review-2.md |
| Licence rehearsal: oracle comparison 0 mismatches over 387,045 responses, T4 passing | T4 UNVERIFIED for all 12 lives (every life passed T4 only because a missing count defaulted to 0); the oracle comparison is recomputable for 1 of 12 lives; the migration helpers have run only against an in-memory fake | reviews/licence-delta/review-1.md (N1, N2); WAL wb297c3d1172 |
| Stage X2 "not independently reviewed" | Delta review at be746a0 ACCEPT, LAB PASS WITH LIMITS, CLIENT BLOCK; reproducibility partial; no X2 question is unseen; later wording fixes main-checked | reviews/eval/review-2.md, review-3.md, reviews/eval-x2delta/review-1.md |
| ES-pilot as a future item | Done at 854ed7f, reviewed with limits; no MEAN-FAIL; three cells mean-in-band, equivalence not demonstrated; T4.7 missed (0.0053 against 0.005); needle on a copy node; not blind; scorer bound to a pilot manifest (main-verified) | eval/ES-PILOT.md; reviews/eval-espilot, eval-espilot-delta; WAL w287450f28a8 |
| Policy: subject tokens deterministic per tenant (R3-3) and an HMAC value reference | Random tokens and references in memory and Postgres at a2d65fd (checked in `decisions.py` and `_pgcore.sql`); R3-3 closed in the delta review; the policy branch's own DESIGN.md still describes the HMAC text at lines 76 and 79 to 84 and needs the policy owner's proposed replacement | reviews/policy-delta/review-1.md; agent-a9fd13f613df8860c/design-lines-proposal.txt |
| Counts of components by status (6 / 9 / 3 / 5 / 5) | 9 / 6 / 3 / 5 / 5 (REVIEWED-WITH-LIMITS / LAB-EVIDENCE-UNREVIEWED / FAILED-REVIEW-REBUILD-IN-PROGRESS / DID-NOT-HOLD / NOT-BUILT) | DESIGN.md section 0 |

Figures added to DESIGN.md and LEDGER.md on 2026-11-17 and where each comes from (paths relative to the stream branch; review files under reviews/):

| Figure | Source |
|---|---|
| Ingestion (EVIDENCE LOCATION: Outcomes at feat/port-ingest d76e525 (docstring fix 2b23546) are owner-reported and coordinator-checked, NOT independently reviewed, and the artefacts live on that branch, not yet merged into lab/integration): 18 of 18 and 8 of 8 declared-boundary runs; 1,884 and 588 flagged extra events; 20 of 20 identical-resend probes; 6 covered placements, 42 conversations; 800 of 960, 80.8%; 0 of 720; four cells 0 of 40; 53 offline tests | ingest/CLAIMS.md at d76e525 (lines 30 to 36, 44 to 54); 53 tests re-run by this branch's integration owner (ingest branch integ/ingest) and by main (WAL w1680249dbf6) |
| Ingestion: 300 of 800 and 50 of 50 private-key bodies | reviews/ingest-delta/review-1.md item 1 (F2) |
| Licence (the 27,599, 5,925 and 1,055,037 figures are in committed report reviews/licence-delta/review-1.md; "4 FAIL and 1 ERROR then Ran 35 OK" and the T4 outcomes are at 207136e: Outcomes at feat/licence-rehearsal 207136e are owner-reported and coordinator-checked, NOT independently reviewed, and the artefacts live on that branch, not yet merged into lab/integration): 27,599 examined, 5,925 of 5,925, 1,055,037 request rows, 31 manifest hashes, 93 to 257 failures per life, 4 FAIL and 1 ERROR then Ran 35 OK | reviews/licence-delta/review-1.md items on F1 and N1; licence/DEGRADE.md; WAL wb297c3d1172 |
| ES-pilot: 94 questions, 90 clusters, 16 cells, largest mean difference -0.018, 0.0053 against 0.005, 13 attacks with 7 KEEP on the old scorer | eval/ES-PILOT.md lines 21 and 60 to 68; WAL w287450f28a8 |
| Write path: 483 of 1,169, 150 of 750, 37 of 1,200 false blocks | reviews/writepath/review-2.md item 7 |
| Stage X2: 38 X1 per-question entries containing the 23 low-overlap ids | reviews/eval-x2delta/review-1.md (F3) |
| Policy: final review figures (P1 to P4, M1 to M3 partly, M4 closed) | reviews/policy-final/review-1.md at a2d65fd |
| Stage 11 (J1 outcomes at feat/eval-pilots a6ca17e are owner-reported, NOT independently reviewed, and the artefacts live on that branch, not yet merged into lab/integration; the user's decisions are cited as the user decision recorded in the coordinator's log on 2026-11-17, not verifiable from this repository): J2 940 calls, A1 188 calls (against 376 shown to the user), about 1,130 and 376 as asked, 160 J1 calls | WAL w187906c60f1 (ask), wf5f7a7d73bc (answer), w5f38b1de953 (main's readings, ASSUMPTION) |
| Free-space figures: 13 GB swing, 13.4 GB, 20 GB start line | OWNER-COMMON addendum; WAL w7cefba34fa3 |
