Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: be746a0dd829178343424be0fee434be6c80f016
Reviewed packet SHA-256: 064231f57b2ef3909b1daaec71bb5ab4f83ce42e77c223a8c2f8360670c77e24
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base 5bb9c5a06b847593c9c58cbbac19e43c97e9ef92)
Worktree status: clean

CODE_INSPECTION_RESULT: BLOCK

Inspection only; this is not PR signoff. HEAD matches `be746a0dd829178343424be0fee434be6c80f016`. The committed pilot numbers recompute, but the scorer has a material failure mode: incomplete cross-encoder evidence can produce `KEEP`, including a misleading completeness flag.

1. **Threshold commit order — could not verify.** The supplied snapshot does not contain the history asserted by the packet. `git log --all` exposes only `be746a0`; `git cat-file -t 4251c9d` returns `fatal: Not a valid object name 4251c9d`. The base-to-HEAD diff adds the plan, thresholds, implementation and results together. `ES-PILOT-PLAN.md:3` says thresholds are committed “before the run,” and `ES-PILOT-THRESHOLDS.md:3` says they are “fixed before the fresh run reads any ES result”; these are documentary assertions, not independently verified chronology. Whether `4251c9d` contained thresholds alone and whether they remained unchanged requires committed-history evidence unavailable here.

2. **Score recomputation — matches numerically.** I called the scorer in memory with bytecode writing disabled; I did not invoke its file-writing CLI. After accounting for JSON tuple/list serialization and the environment-specific `run_dir`, the summary matches. There are 376 unique question/level rows and 376 CE records: 94 questions and 90 clusters per cell. All CE head orderings also match the committed scores and stable tie-breaking rule.

   Recomputed session recall@10, **exact / ES**:

   | Level | BM25 | Dense | RRF | Cross-encoder |
   |---|---:|---:|---:|---:|
   | H0 | .946099 / .946099 | .961525 / .961525 | .968085 / .960993 | .968085 / .966312 |
   | H1 | .820035 / .817908 | .774113 / .774113 | .859220 / .861702 | .888298 / .878014 |
   | H2 | .655319 / .667730 | .550532 / .546631 | .684397 / .672872 | .703014 / .685284 |
   | H3 | .493794 / .510106 | .344681 / .334043 | .475355 / .482979 | .532979 / .525887 |

   Recommendation: `KEEP`; no MEAN-FAIL; three WIDTH-FAILs; tightening unsupported. The largest absolute mean difference is `0.017730496`, CE H2.

3. **Rules absent from the thresholds.** The extra substantive control criterion is `real_es > 0` at `eval/emlab/es_pilot_score.py:123`. It is a tightening absent from T4.3, disclosed at `ES-PILOT.md:19`, and changes no result in these rows. Its timing cannot be established from this snapshot, so I cannot certify that it preceded observation.

   Additional undocumented scoring/validation behavior includes:

   - Missing CE records are skipped (`:69–71`, `:88–90`).
   - Duplicate CE keys silently overwrite earlier records (`:62`).
   - Completeness checks only arm presence, not complete paired denominators (`:138`), and does not gate `KEEP` (`:139–150`).
   - Prior reproduction checks are omitted if their paths do not exist (`:152`, `:161`).

   These are implementation behaviors, rather than declared statistical thresholds. The incomplete-evidence behavior is material; see F1. Session overlap is an additional descriptive output, not an extra acceptance criterion.

4. **T4.7 — correctly reported as missed.** The recomputed H3 difference is `+0.005319148936170248`, above `0.005`. `score.json` records `within_0.005=false`; the table renderer prints that field (`es_pilot_tables.py:44–46`); the write-up explicitly says “NOT MET” (`ES-PILOT.md:21`) and includes the false result (`:63`). The batch-padding explanation is explicitly labelled unverified. It remains a hypothesis.

5. **Needle scope — appropriately limited.** `ES-PILOT.md:20,85` identifies the separate copy node and limits the inference to that code path and filters. I independently aggregated `needle.json`’s recorded ranks: its summary matches, with 94 rank-one results and zero excluded appearances in every arm/level cell. This verifies internal evidence consistency, not the actual Elasticsearch execution or equivalence of the two nodes.

6. **WIDTH-FAIL and projection — appropriately distinguished.** The three recomputed failures are:

   | Cell | Mean ES−exact | 95% interval |
   |---|---:|---:|
   | BM25 H2 | +.012411348 | [−.032269504, +.057706093] |
   | BM25 H3 | +.016312057 | [−.021505376, +.054121864] |
   | CE H3 | −.007092199 | [−.050179211, +.034050179] |

   They remain WIDTH-FAIL in the score and write-up. `ES-PILOT.md:25,68,78,81` labels the projection’s assumption and distinguishes expected future interval width from demonstrated equivalence. The rounded CE table boundary of `−0.050` is clarified by the more precise explanation at `:68`.

7. **Known prior outcome — honestly disclosed, with provenance limits.** `ES-PILOT-PLAN.md:6`, `ES-PILOT-THRESHOLDS.md:3` and `ES-PILOT.md:8,81` acknowledge that BM25 H2/H3’s prior outcome was known. This suffices to disclose that the decision design was not blind. It does not establish threshold chronology or make the fresh pilot an independent confirmation.

8. **Read-only guard — write routes refused; negative test is meaningful.** The anchored method/path allow-list at `es_guard.py:20–30,45–59` excludes settings writes, bulk, document writes and delete/update routes before dispatch. The settings-write test at `test_es_guard.py:27–33` would fail if `GuardedEs` were replaced with the underlying fake client: the expected exception would disappear and the PUT would reach the fake. The preceding test demonstrates that plain-client path. This is non-vacuous regression protection; actual historical failing-first execution is not established by the supplied green log.

   The guard’s prose is broader than its body validation: it rejects only top-level `pit`/`script`, not every nested script or arbitrary search-body field. I found no index-writing endpoint reachable through the allowed pilot calls.

9. **Test split — no evaluated test questions found; “never read” is too strong.** The evaluated question IDs exactly match the prior pilot rerank file’s 94 IDs. Retrieval uses `select_split(..., "pilot")`; the wrong-query map is a bijection across different content clusters. However, `build_rag_contexts.py:44–48` first loads **all** questions and constructs `PoolContext(all_qs)`, and `es_pilot2.py:134` constructs question text for all dataset questions. Thus the literal statement “the locked test split was never read” (`ES-PILOT.md:3`) is unsupported by the code. I found no test-question scoring or test-result-driven tuning in the inspected evidence.

**General findings**

- **F1 — MATERIAL, medium severity: incomplete evidence can receive a positive recommendation.** `eval/emlab/es_pilot_score.py:54–72,88–90,138–150`. In a read-only, in-memory audit using the committed rows, supplying no CE records returned `category="KEEP"` with `complete_all_arms=false`. Removing one CE record returned `KEEP` with **`complete_all_arms=true`**, while H0 CE used only 93 pairs. A partial rerank can silently change denominators and discard adverse observations. Reject missing or duplicate question/level records before scoring; require identical question sets across all levels and arms; validate CE heads against their source RRF heads; and withhold recommendations when evidence is incomplete. Add focused negative tests for missing CE files, individual missing records, duplicate records and mismatched keys. The present committed dataset is complete, so this finding does not invalidate its recomputed numbers.

- **F2 — MINOR, low severity: distinguish test evaluation from dataset loading.** `eval/ES-PILOT.md:3`; `eval/emlab/build_rag_contexts.py:44–48`; `eval/emlab/es_pilot2.py:134`. Replace “never read” with a precise statement that no test questions were evaluated, while the full dataset was loaded for split/corpus construction. If literal non-access is required, isolate pilot inputs before these routines run.

**Use verdicts:** **LAB — PASS WITH LIMITS** for exploratory interpretation of these complete committed rows, retaining the failed T4.7 caveat and unverified chronology. **CLIENT — BLOCK** for claims of established Elasticsearch parity or operational readiness: three cells do not demonstrate equivalence, the needle concerns a copy node, and F1 permits misleading recommendations from incomplete runs.

I inspected the full supplied diff, including the Stage X2 scorer corrections outside the packet’s ES file list. The supplied nine-test green log is evidence, not a test run I reproduced. I did not run write-dependent tests, contact Elasticsearch, read secrets, or access paths outside the snapshot. Original execution, corpus/vector identity, model provenance and intermediate commit chronology remain unverified. Pending full validation is not the reason for this inspection block. No production migration is introduced by the inspected change; there is no deployment or rollback validation claim here.

Stage: code-inspection; not PR signoff.
