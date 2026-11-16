# Elastic memory lab: design v2 (client deployment)

Ticket: GoodQA goodqa#16096. Branch docs/design-v2, written 2026-11-15 from the lab state at main 603bb3b plus the owner worktrees and review reports listed under Sources. It replaces v1 ("design under test", 36 lines). Nothing in this file is new design. Every statement comes from an experiment, a review, a research report, a decision record or an earlier design document, and carries the evidence tag its source supports.

This document does not claim the design is proven. Section 1 lists what it cannot yet claim. Section 5 lists the experiments between this state and a proven design. Every number here is traced in CLAIMS-AUDIT.md.

## 0. How to read this document

### Evidence tags (closed set; one per statement)

| Tag | Meaning |
|---|---|
| REVIEWED | An independent reviewer confirmed the result from artifacts. The cite names the report and the item; the reviewer's limit is part of the statement. |
| OWNER-REPORTED | The building agent reported it with an artifact. No independent confirmation. Cite: branch and commit. |
| OBSERVED | A one-off observation with no control. |
| EXPLORATORY | A pilot or exploratory result. Not confirmatory. |
| DESIGNED-UNTESTED | Part of the design; no experiment has exercised it, or an experiment ran but a review found it did not exercise the claim. |
| DID-NOT-HOLD | Shown false by an experiment or a review. The cite says which, and at what evidence level. |
| OPEN-DECISION | Needs a decision from the user or the client. |

Rules used to choose the tag:
- A claim is tagged at the commit a reviewer read, not at the branch head. A fix committed after a review is OWNER-REPORTED until a delta review confirms it.
- A review verdict of SUPPORTED becomes REVIEWED with the reviewer's limit. A verdict of PARTLY is split into a REVIEWED part and a DESIGNED-UNTESTED or OWNER-REPORTED part. UNSUPPORTED, or a mechanism never run, becomes DESIGNED-UNTESTED. DID-NOT-HOLD is used only when a result or a review shows the claim false.
- A negative exploratory result is DID-NOT-HOLD with "(exploratory)" in the cite. A positive exploratory result is EXPLORATORY.
- A DID-NOT-HOLD row always states the claim or mechanism that was tested, written as a claim, followed by what happened and, where there is one, the design consequence. A design rule adopted because of a failure is stated in the same row after "So:", and the tag belongs to the failed claim, not to the rule.
- `ASSUMPTION` is an extra label on a statement that is neither the user's words nor the user's decision. It never replaces a tag.

Statements are table rows with an Evidence column. Sections 1, 4 and 5 summarise tagged statements; section 6 lists contradictions and carries no tags.

### Terms (defined once)

| Term | Meaning here |
|---|---|
| Log | The append-only, hash-chained event log in Postgres. The source of truth. |
| Projection | Anything rebuilt from the log: Elasticsearch indices, the current-state table, facts. |
| Pool level H0 to H3 | H0 = a question's own haystack. H1 = own + 500 shared distractor sessions. H2 = own + 3,900. H3 = own + the whole deduplicated pool. Defined in PREREG section 4.1. |
| Session recall@10 | Share of a question's gold sessions found in the top 10 sessions, averaged over questions. |
| Exact retriever | The in-process BM25 plus E5-small cosine retriever the eval uses. It is not Elasticsearch. |
| Fence (epoch) | A lease epoch carried in every write and checked by the store, so a writer that has lost its lease is refused. |
| Analyst | An asynchronous agent session that re-ranks and digs above search and delivers findings to a session inbox. |
| Basic required path | The features the design may rely on with an Elasticsearch Basic licence. ASSUMPTION: this target comes from main's brief and decision record w6ffbac116a6, not from the user's words. |

### Sources (cited by key)

| Key | Artifact |
|---|---|
| RV-ENG | research/engine-review-62916ae.md (red-team review of feat/engine-evolution at 62916ae) |
| RV-WP | research/writepath-review-aaa0fec.md (review of feat/writepath-rescue at aaa0fec) |
| RV-SYS | delta review of feat/systems-skeleton f6ade14..a13b524 (delta-review-16096.md) |
| RV-AD | delta review of the adapters at 888c94b, including its confirm section at c20e208 (delta-review-888c94b.md) |
| RV-AD0 | research/wkdcbf32g.output (first adversarial review of the adapters: 22 material findings, 12 challenged, all 12 confirmed) |
| RV-WP0 | research/wxd8dtb2d.output (first adversarial review of the write-path skeleton: 17 material findings, 12 challenged, all 12 confirmed) |
| RV-ES | research/w7r8hj8n6.output (independent re-run of the Elasticsearch behaviour results) |
| RV-G1 | research/w3dv5r478.output (customer-stack research with adversarial challenges) |
| RV-LIC | research/wev7th3nu.output (licence-tier re-verification) |
| RV-SRC | research/wv1gg5s6m.output (five outside sources: mapping and critic) |
| FAB | research/agent-fabric-architecture.md (read-only reading of the agent-fabric kernel) |
| PREREG | eval/PREREGISTRATION.md, frozen at blob a72ee4563142d45b9491f1643e238d7e72b0f851 (feat/eval-scaffold) |
| XP | eval/EXPLORATORY.md, eval/results/exploratory/xp_e2.json and xp_e3.json (feat/eval-scaffold 5d39de1) |
| CURVE | eval/results/pilot/curve_pilot.json (feat/eval-scaffold 5d39de1) |
| RAGCTX | eval/results/pilot/rag_context_metrics.json (feat/eval-scaffold 5d39de1) |
| CANARY | eval/results/pilot/analyst_canary_h3.json (feat/eval-scaffold 5d39de1) |
| AFULL | eval/results/pilot/analyst_full.json and the "Results: full analyst run" section of PREREG (feat/eval-scaffold cc83d26; appended after the freeze, pilot split, descriptive) |
| ENG | engine/NOTES.md and engine/results/final/ (feat/engine-evolution; reviewed at 62916ae; later owner commits in 3.15) |
| WP | writepath/ and its results (feat/writepath-rescue aaa0fec) |
| SYS | systems/results/ (feat/systems-skeleton a13b524) |
| MC | memcore/NOTES.md and memcore/NOTES-addendum-g7.md (fix/adapters-delta-16096 c20e208); memcore-out/results/ |
| BEH | behaviour/results/ (feat/elastic-behaviour 5229e75) |
| PF | phasef/GATE_RESULT.md, PROVENANCE_RESULT.md, DEVLOG.md, ID_AUDIT.md (feat/phasef-harness 27a4c64) |
| LR | phasef/GATE3_RESULT.md (feat/links-rescue 18d07a3) |
| LEDGER, GOT | LEDGER.md and gotchas/ on lab main 603bb3b |
| WAL | main session decision log, by entry id |

## 1. What this design cannot yet claim

The user's targets are "massive long term context" and "extreme recall and precision". No result in this lab shows either. The headline numbers:

| Claim the design cannot make | What was measured instead | Evidence |
|---|---|---|
| High recall as history grows | Hybrid (RRF) session recall@10 is 0.968 on a question's own haystack and 0.4754 against the whole pool (pilot, 94 questions, 90 content clusters, exact retriever). The fall from H0 to H3 is -0.493, 95% interval [-0.579, -0.401]. | [EXPLORATORY] CURVE levels.H3.rrf.session_recall@10; PREREG 4.3 |
| The right evidence reaches the model | With a 4,000-token context at the whole pool, mean gold-session recall in the packed context is 0.41. At least one gold session is present for 0.53 of questions and all of them for 0.33. | [EXPLORATORY] RAGCTX (arm rag, H3, budget 4000, abstention excluded, n 94); XP x01_budget_presence |
| Context is current after a change (with search alone) | At the whole pool and 4,000 tokens, only the stale value is in context for 0.467 of knowledge-update questions, and the current value for 0.333 (n 15). The analyst run's RRF baseline on the same 15 questions gives 0.667 and 0.267; the two files disagree (section 6). | [EXPLORATORY] RAGCTX (knowledge-update questions, H3, budget 4000); AFULL runs.H3 RRF b4000 |
| Two-stage retrieval (sessions first, then rounds) helps at scale | It did not. S = 20 gave 0.462 against flat 0.481 (difference -0.019, interval [-0.074, 0.040]); S = 50 gave no difference. The planted-gold control reached 0.504 against a 0.90 bar. | [DID-NOT-HOLD] XP E2 (exploratory) |
| A confidence gate can skip analysts at scale | The predictor beat its permutation null at H1 (AUROC 0.831 against 0.718) and H2 (0.727 against 0.664) but not at the whole pool (0.572 against 0.631). | [DID-NOT-HOLD] XP E3 (exploratory, at H3) |
| Analysts add value beyond a cross-encoder rerank, or recover the hardest evidence | On the pilot, analysts raise session recall@10 over plain RRF at the whole pool (0.473 to 0.603, +0.130 [+0.060, +0.202]), but are not distinguishable from a cross-encoder rerank of the same candidates (+0.024 [-0.033, +0.079], n 78, rerank run partial), and digging with tools adds little over reordering (+0.022 [-0.027, +0.070]). On the 23 low-overlap questions every arm stays near the floor (analyst 0.072) because gold is among the 50 candidates for only 7 of 23. Candidates were shown in tier-0 order, so anchoring is not controlled. No answer accuracy was measured. | [EXPLORATORY] AFULL runs.H3.summary (pilot, descriptive, no registered decision rule) |
| Supersession in prose is detected | Links that were made were correct (16 of 16), but only 10 of 16 real updates were linked against a bar of 12, and 9 of the 25 pairs linked in either run flipped between two batchings. | [DID-NOT-HOLD] LR GATE3_RESULT (gate G5) |
| A memory layer improves answers on this benchmark | Not measured. The benchmark showed no headroom at the size tried: the full-history reader matched the gold-evidence reader on 9 of 10 questions (an n = 10 micro-pilot). | [OWNER-REPORTED] PREREG 4.1; LEDGER critic correction |
| Any retrieval number above holds on Elasticsearch | Every recall figure comes from the exact retriever. At round level, Elasticsearch BM25 shares 0.751 of the exact top 10, and client RRF against Elasticsearch `rrf` 0.817. | [OWNER-REPORTED] PREREG 4.3 (es_round_parity.json) |
| Erasure is complete for a client | The rebuilt write path holds in the windows its experiments force. A review finds that erasure cannot complete for a subject with more than 65,536 events or a tenant whose rebuild outlasts the 120 s lease, and that the completion guard trusts the caller. | [DID-NOT-HOLD] RV-WP F1, F2, F4 (from code and vendor docs; not run live) |
| The durable engine is safe for several tenants | Tenant isolation, approval binding and signal validation are missing in the reviewed code. Fixes exist on the branch and are unreviewed. | [DID-NOT-HOLD] RV-ENG H1, H2, H3 (at 62916ae) |
| Kill rules K1 and K2 are decided | Neither has run: both need fact extraction and answers, and extraction is held by the user's choice. | [OPEN-DECISION] WAL w11160e65223 |

What does hold, with limits, is listed per component in section 3.

## 2. What the system is, and its fixed points

### 2.1 Purpose and targets

| # | Statement | Evidence |
|---|---|---|
| 2.1.1 | The system gives AI agents long-term memory: an event log of everything said and done, projections that make it searchable, and a per-turn context block plus pull tools that put the relevant part in front of the model. The targets are the user's words "massive long term context" and "extreme recall and precision". | [DESIGNED-UNTESTED] v1 DESIGN.md; WAL w3feb63cc9ea |
| 2.1.2 | The deliverable is a client deployment for an Elastic customer, "not just for my harness/internal use" (user, WAL w55d0a51fc1f). The concrete shape is main's choice, not the user's: a standalone memory core on the client's Postgres and Elasticsearch, with Agent Builder and client model providers behind ports, and agent-fabric and subscription CLIs as adapters. ASSUMPTION. | [OPEN-DECISION] WAL w6ffbac116a6 |
| 2.1.3 | Subscription Claude and Codex CLIs are development adapters only, behind the same model port. The user asked for adapters "so we can test and use it with our subscription based claude/codex instances" (WAL wd8b56ed99b8). Whether they may sit behind Elasticsearch's inference API is an open ask (WAL wc459b4a2cfc). | [OPEN-DECISION] WAL wd8b56ed99b8, wc459b4a2cfc |
| 2.1.4 | Ideas enter the design only from LEDGER's "Held" table, at the evidence class shown there; failed ideas stay recorded with the reason. Source: the user's "incorporate into designs whatever proves to work, keep track of what doesnt". | [OWNER-REPORTED] LEDGER header (lab main 603bb3b); WAL w32d28bf01c9 |

### 2.2 Fixed points

| # | Fixed point | Evidence |
|---|---|---|
| 2.2.1 | The Postgres log is the only source of truth. Elasticsearch holds rebuildable projections only. The durable engine holds ids and cursors only. | [REVIEWED] RV-ENG F8 SUPPORTED with limits: sessions rebuilt from Postgres alone; limits: a known session list, a single tenant, the old engine fully dead |
| 2.2.2 | Every input is a log event before any signal; the workflow signal is only a doorbell. | [REVIEWED] RV-ENG F8 SUPPORTED (75 dropped doorbells covered by reconcile; the signal-only groups fail for the stated reason). The count "lost 6 of 6" is the owner's (LEDGER, ENG F8) |
| 2.2.3 | Idempotency keys derive from stable input identity, never from an attempt number. | [REVIEWED] RV-ENG F4t: attempt-derived keys gave 54 duplicate ledger rows and 825 results for 800 inputs |
| 2.2.4 | A stale writer is refused by the store, not by caller discipline. The lease epoch travels in the write and is checked under lock in Postgres, and by a per-epoch Elasticsearch write alias with require_alias. | [REVIEWED] RV-WP N3, N3S_projector, N5, N8 SUPPORTED; limit: holds for honest stale writers that present their own token (RV-WP F3) |
| 2.2.5 | Hybrid retrieval alone can serve as the memory. It did not: whole-pool recall in section 1 (exploratory, pilot). So: retrieval is one input to context, with a current-state table and an analyst tier above it. | [DID-NOT-HOLD] CURVE; LEDGER "Did not hold" row 1 (exploratory) |
| 2.2.6 | Automatic supersession links can act as a filter. They did not: link recall 10 of 16. So: supersession labels are advisory, never a filter, and the user chose "Dated facts, links optional" (WAL w0fdb289edad). | [DID-NOT-HOLD] LR GATE3_RESULT (gate G5) |
| 2.2.7 | Owner-reported passes hold without independent review. They did not: the first adversarial reviews confirmed 12 challenged material findings in the adapters and 12 in the write-path skeleton. So: no pass counts until an independent review confirms it. | [DID-NOT-HOLD] RV-AD0; RV-WP0 |
| 2.2.8 | The proof rules in gotchas/PROOF-RULES.md apply to every experiment: a failing control above a stated sensitivity floor, witness counts, pre-committed thresholds, per-item artifacts and an environment fingerprint. | [OWNER-REPORTED] GOT PROOF-RULES.md (lab main 603bb3b) |

## 3. Components

### 3.1 Event log and hash chain

| # | Statement | Evidence |
|---|---|---|
| 3.1.1 | Events are appended to a Postgres table with a per-stream sequence and a hash chain (seq, prev_hash). | [OWNER-REPORTED] SYS C7_writers.json (feat/systems-skeleton a13b524) |
| 3.1.2 | 16 concurrent live writers produced a gapless sequence. This says nothing about clock skew, cross-host ordering or a stale writer. | [REVIEWED] RV-SYS "recomputed and matching" (C7); limit from GOT CORRECTIONS |
| 3.1.3 | A zombie session that lost its lease is quarantined: 20 of 20 zombie appends were stored as unadopted in both stall positions, the chain verified and nothing was projected. | [REVIEWED] RV-WP "Zombie row (row 1)": safety SUPPORTED |
| 3.1.4 | Takeover always completes after a zombie loses its lease. It does not: a zombie that stalls after its insert while holding its lease row blocks takeover indefinitely, because `acquire` sets no lock timeout. | [DID-NOT-HOLD] RV-WP F6 (from code; not run) |
| 3.1.5 | The projector cursor reads only rows whose transaction committed below the cluster-wide snapshot xmin, so a late commit is never skipped. | [REVIEWED] RV-WP "Late commit (row 23)": Postgres half SUPPORTED; limit: any long transaction anywhere in the cluster stalls projection for every tenant |
| 3.1.10 | While one transaction is held open for 12 s, a reader still finds a new event through the log tail, and projection catches up 0.73 s after the commit. The run has no failing control and covers one database, not a cluster-wide stall across databases. | [OWNER-REPORTED] SYS XMIN_stall.json (a13b524) |
| 3.1.6 | The signed chain head is anchored outside the database with a promotion epoch, and projection stops at the replication-confirmed LSN. | [DESIGNED-UNTESTED] GOT DESIGN-CHANGES; UNCOVERED-TOP30 #6 |
| 3.1.7 | The hash chain covers stored bytes, so an upcaster bug changes what an old event means without breaking verification. Old-event golden replays are needed. | [DESIGNED-UNTESTED] GOT DESIGN-CHANGES "still missing" |
| 3.1.8 | A memory append must never fail silently. | [DESIGNED-UNTESTED] LEDGER X23 (rule, queued) |
| 3.1.9 | Secret and credential scanning runs before append, including encoded variants. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #19 |

### 3.2 Projections and fencing

| # | Statement | Evidence |
|---|---|---|
| 3.2.1 | Index names are chosen by an immutable field (plain index, op_type=create, deterministic routing), not by a rolled alias or a data stream. On a data stream the same _id is accepted again after a rollover. | [REVIEWED] RV-ES writes E3a, E3b, E3c and alternatives REPRODUCED |
| 3.2.2 | Conditional writes with if_seq_no and if_primary_term give exactly one winner. `_bulk` has no cross-item atomicity, so per-item statuses must be read. | [REVIEWED] RV-ES writes E2 REPRODUCED; E8d PARTLY (HTTP 200 with an item-level 400) |
| 3.2.3 | Search visibility after a write takes about one second at the default refresh (p50 1,001 ms over 200 writes on a loaded host). Recent events are read from the log tail, not from search. | [OWNER-REPORTED] BEH E1_refresh.json (feat/elastic-behaviour 6f53c80) |
| 3.2.4 | Read-after-write through the tail scan held for 100 of 100 reads, and 600 events written during a projector pause were all found. | [OWNER-REPORTED] SYS C2_lag_tail.json; GOT CORRECTIONS |
| 3.2.5 | Every projection is rebuildable from the log. A blue/green rebuild showed no mixed view, but its control never produced a mixed view, so that half is unproven. A dropped bucket stays missing until an explicit repair. | [DESIGNED-UNTESTED] RV-SYS "result claims the raw files do not support" (C1 v2) |
| 3.2.6 | The fence is pinned before any read: the epoch is captured at start, and the bulk goes through the per-epoch write alias with require_alias. | [REVIEWED] RV-WP N1 SUPPORTED, narrow; limit: the control re-acquires the lease by force, and an in-flight bulk is not exercised |
| 3.2.7 | A fence made of a lock plus an alias resolved after the read keeps erased plaintext out of a live generation. It did not. So: the epoch is pinned before the read (3.2.6). | [DID-NOT-HOLD] RV-SYS N1, confirmed by a pure model (fence_window.py) |
| 3.2.8 | The fence binds the scope that owns each table and covers every write. It does not: the trigger checks the scope the row names, and DELETE and TRUNCATE are unfenced on several tables. | [DID-NOT-HOLD] RV-WP F3 (schema reading; reproducer given, not run) |
| 3.2.9 | Elasticsearch `_aliases` can return HTTP 200 with `errors: true`; the client must raise on it. | [DESIGNED-UNTESTED] RV-WP F7 (docs plus code; hypothesis) |
| 3.2.10 | The projector never passes unacknowledged items under an Elasticsearch flood-stage write block. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #22 |
| 3.2.11 | Exact rescoring on approximate vector search is needed: without it the Elasticsearch dense leg cost up to about 0.08 session recall@10; with oversample 3 on bbq_hnsw the loss intervals all include 0. | [OWNER-REPORTED] PREREG 4.3 (es_hybrid_pooled.json) |
| 3.2.12 | At a 5% filter, the `fanout` filter heuristic gives higher kNN recall than the default `acorn` at equal num_candidates. The author's exact figures are not properties of Elasticsearch. | [REVIEWED] RV-ES "E6 direction" PARTLY (direction confirmed; figures not tested) |
| 3.2.13 | The embedder identity (model, revision, prefix, normalisation) and the vector index options are part of each generation's name and _meta. Inference endpoint ids are immutable per revision. | [DESIGNED-UNTESTED] GOT DESIGN-CHANGES; UNCOVERED-TOP30 #20 |
| 3.2.14 | Without the `query: ` and `passage: ` prefixes, E5 vectors from the Elasticsearch endpoint move (top-10 overlap with prefixed vectors 0.718), so a semantic_text deployment must add them. | [OWNER-REPORTED] PREREG 4.2 (es_e5.json) |

### 3.3 Erasure

| # | Statement | Evidence |
|---|---|---|
| 3.3.1 | Erasure is per-subject key destruction in a separate key store, plus removal of derived rows through input-set provenance, then a rebuild of every registered index family. | [OWNER-REPORTED] WP erasure.py at aaa0fec |
| 3.3.2 | Phase 1 bumps the lease first, in one transaction with the tombstones and the job row. Writers racing phase 1 leave no residue. | [REVIEWED] RV-WP N8 SUPPORTED; limit: the control drops every trigger, so it does not isolate the ordering |
| 3.3.3 | A stale rebuild cannot swap: it writes through a staging alias under compare-and-set, and the swap is anchored on `remove ... must_exist`. | [REVIEWED] RV-WP N3 and N3S_projector SUPPORTED (sensitive controls) |
| 3.3.4 | Readers filter erased subjects on every read path, always. | [REVIEWED] RV-WP N5 SUPPORTED (the unfiltered control serves erased rows) |
| 3.3.5 | Every registered index family is purged. Families outside the registry, data streams, ingest pipelines, enrich indices and hidden indices are not detected. | [REVIEWED] RV-WP N2 PARTLY: registered families covered; the detector is name-based |
| 3.3.6 | "Done" requires a verification of zero residue. | [DID-NOT-HOLD] RV-WP F4 and N4 PARTLY: the guard checks a JSON the caller supplies and does not fence INSERT |
| 3.3.7 | Erasure completes for any subject size. | [DID-NOT-HOLD] RV-WP F1 (code plus vendor docs; not run live): verify builds one terms query with every event id, Elasticsearch caps terms at 65,536 by default, and one failing job stops the sweeper for all jobs |
| 3.3.8 | Erasure completes for any tenant size. | [DID-NOT-HOLD] RV-WP F2 (model, not measured): a full-tenant rebuild longer than the unrenewed 120 s lease livelocks; with an ASSUMPTION rebuild rate the limit is near 200,000 documents |
| 3.3.9 | Deleted Elasticsearch text stays in segment files after DELETE. Two purge paths work: delete, flush, wait for the retention-lease sync (about 30 s), force-merge to one segment and verify; or reindex survivors into a fresh index and delete the old index. "Only reindex works" was refuted. | [REVIEWED] RV-ES erasure claims REPRODUCED, PARTLY and REFUTED as stated |
| 3.3.10 | No plaintext or key material remains at rest in the event and key databases after erasure (one run). The key store is a lab stand-in. | [REVIEWED] RV-WP N6 PARTLY: one run (R = 1), lab stand-in only |
| 3.3.11 | Production needs a KMS with real key deletion and a backup-retention bound stated in the erasure receipt. | [OPEN-DECISION] RV-WP owner question 6 |
| 3.3.12 | Erasure covers speaker provenance only: what other users said about the subject survives, and the tenant HMAC keeps slot hashes confirmable. Whether that is enough is an open ask. | [OPEN-DECISION] RV-WP F8 and owner question 7; WAL wc4b140b9a4f |
| 3.3.13 | Legal hold versus erasure needs a defined precedence and an audit trail. | [OPEN-DECISION] RV-WP owner question 10; GOT DESIGN-CHANGES "still missing" |
| 3.3.14 | Erasure reaches every content store (search, async search, Agent Builder conversations, inbox, sealed checkpoints, snapshots) from an inventory enumerated by code, and a snapshot restore is gated on replay. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #5; GOT DESIGN-CHANGES |
| 3.3.15 | Erasure covers the task state written by the evolution phase. It does not: task_view holds plaintext outside the subject key and the cascade. | [DID-NOT-HOLD] RV-SYS N6 (at a13b524; not re-reviewed since) |
| 3.3.16 | Erasure cost grows with the work it triggers: 40 erasures took 254 s. | [OWNER-REPORTED] GOT DESIGN-CHANGES "still missing" (erasure amplification) |

### 3.4 Facts and provenance

| # | Statement | Evidence |
|---|---|---|
| 3.4.1 | Facts carry valid_from and valid_to, modality (asserted, decided, proposed, hypothetical), scope, author and trust origin. Lower trust never supersedes higher. | [DESIGNED-UNTESTED] v1 DESIGN.md; kill rule K1 not run |
| 3.4.2 | Every fact cites session, round and a verbatim quote, and a host check verifies the quote mechanically: 2,351 of 2,373 quotes (99.1%) were found in the cited round, and all three controls were rejected in full. | [OWNER-REPORTED] PF PROVENANCE_RESULT.md (feat/phasef-harness 27a4c64) |
| 3.4.3 | The same check finds 8 facts whose stated author differs from the speaker (assistant-said text stored as the user's, and the reverse) and 53 ambiguous ones. An event cannot rise above its recorded trust origin. | [OWNER-REPORTED] PF PROVENANCE_RESULT.md; rule from GOT DESIGN-CHANGES |
| 3.4.4 | Fact extraction runs one session per call. Facts per session were 9.00 at 1 session per call, 5.12 at 4, 4.46 at 8 and 2.71 at 16. This counts facts against a model-judged reference that no person has validated; it is not recall against gold. | [OWNER-REPORTED] PF DEVLOG batch-size curve; LEDGER critic correction |
| 3.4.5 | A derived fact takes the minimum evidence class of its inputs. A status question is answered from the evidence view, never from extracted text. | [DESIGNED-UNTESTED] GOT DESIGN-CHANGES; UNCOVERED-TOP30 #12 |
| 3.4.6 | Write-time cue keys let a fact be found by a question with no word overlap. | [DESIGNED-UNTESTED] OPEN-QUESTIONS B8 not run; RV-SRC critic on X28 |
| 3.4.7 | Repo-scoped facts carry (repo, commit, path footprint). As-of and supersession use log sequence; wall time is for display only. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #28 |
| 3.4.8 | Model-facing ids are opaque, after real gold session ids were all found to begin with `answer_`. A re-test with opaque ids changed no outcome (9 of 10, 16 of 16); the dev2 gate-3 answers were not re-run. | [OWNER-REPORTED] PF ID_AUDIT.md |

### 3.5 Decisions and current state

| # | Statement | Evidence |
|---|---|---|
| 3.5.1 | A task definition change is an event. Earlier work is kept and re-classified as still valid, needs rework or obsolete. | [OWNER-REPORTED] SYS F1_F2_task_amend.json (a13b524) |
| 3.5.2 | An amendment while work is in flight leaves no torn state: 200 runs per race, and the fence-off controls committed stale state 74 and 71 times. | [OWNER-REPORTED] SYS F3_inflight.json; GOT CORRECTIONS |
| 3.5.3 | A detected prose change becomes a pending update; it becomes current only through an explicit decision event. | [DESIGNED-UNTESTED] OPEN-QUESTIONS I4; the prose detector itself did not hold (section 1) |
| 3.5.4 | Decisions in the systems skeleton are version-bound and idempotent. They were not: a stale acceptance overwrote a newer user decision, a replay re-applied a reverted change, and a retried decision dropped a pending change. | [DID-NOT-HOLD] RV-SYS N7 (pure probes) |
| 3.5.5 | In the rebuilt write path, decisions use compare-and-set: the stale-acceptance, replay and retry cases hold 10 of 10. | [REVIEWED] RV-WP N7 PARTLY: cases (a), (b) and (c) hold |
| 3.5.6 | In the rebuilt write path, decisions are bound to the approval itself. They are not yet: the version comes from the caller, heads are per session, one typed message can authorise unlimited decisions, and an idempotency key reused with different content returns "duplicate". | [DID-NOT-HOLD] RV-WP F5 (code; reproducers given) |
| 3.5.7 | Whether decision items are per session or tenant-wide is a product choice (OPEN-QUESTIONS J1 implies tenant-wide). | [OPEN-DECISION] RV-WP F5(b) |
| 3.5.8 | Every step and every injected block records the definition version and watermark it used (the acted-on version). The generated-scenario oracle shares the fold's version-blind rule, so its result is not independent. | [DESIGNED-UNTESTED] RV-SYS oracle note; SYS I4_I5_decisions.json |
| 3.5.9 | Only an authorised human-typed decision flips state; two agents cannot approve each other; a revoked principal's pending decision is invalidated. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #10 |
| 3.5.10 | The current-state table is a fold of events, checked by an oracle, following agent-fabric's reducer discipline. | [DESIGNED-UNTESTED] FAB section F |
| 3.5.11 | Lower trust never supersedes higher across sibling scopes. | [DID-NOT-HOLD] RV-SYS sibling-scope finding (probe trust_scope.py) |

### 3.6 Context injection and budget

| # | Statement | Evidence |
|---|---|---|
| 3.6.1 | Each turn gets a budgeted block regenerated at the model port; only ids are logged. The block is always well-formed or empty: 1,716 renders across budgets, 0 malformed. | [REVIEWED] RV-AD confirm N5 FIXED at c20e208 (local probes) |
| 3.6.2 | Records are framed with a per-turn random delimiter, one JSON-escaped record per line, so a stored `</memory>` or `SYSTEM:` line cannot forge the framing. | [REVIEWED] RV-AD confirm (forging finding fixed) |
| 3.6.3 | The primary budget is 4,000 tokens, chosen on the pilot after the first selection rule failed to fire. | [EXPLORATORY] PREREG 5.1 (budget_pilot.json); pilot-chosen |
| 3.6.4 | Gold presence grows with budget. At the whole pool, at least one gold session is present for 0.53, 0.62, 0.71 and 0.79 of questions at 4,000, 8,000, 16,000 and 32,000 tokens. No claim about context rot follows. | [EXPLORATORY] XP x01_budget_presence (H3, flat) |
| 3.6.5 | Injection and trimming keep tool-call and tool-result pairing valid (in the Anthropic Messages API a tool_result must follow its tool_use). | [DESIGNED-UNTESTED] LEDGER X24; RV-SRC critic |
| 3.6.6 | Compaction is recorded as an event that ends in-context status and forces the state card and manifest to regenerate. A resumed turn re-sends the sealed block verbatim plus a labelled delta. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #3 and #14 |
| 3.6.7 | Every item presented as current carries its source event and a checked-through watermark, and a bounded scan after that watermark surfaces challengers. | [DESIGNED-UNTESTED] RV-SYS: the I3 PASS is not supported; as built the scan averages 7.59 false challengers per scan, 91% of scans without a planted statement show one, and its vector leg is a hash-embedding stand-in |
| 3.6.8 | The session-id egress guard runs on every outbound path and fails closed without its id list. | [REVIEWED] RV-AD confirm N4 FIXED; residuals: homoglyph, encoding and separator variants pass, and real-id matching is off by default |
| 3.6.9 | The prompt-cache cost of a regenerated block is unmeasured. | [DESIGNED-UNTESTED] OPEN-QUESTIONS C6; UNCOVERED-TOP30 #14 |
| 3.6.10 | Agent Builder injection must use the same randomised framing; a Kibana template that uses the raw `<memory>` format is superseded. | [DESIGNED-UNTESTED] RV-AD confirm |

### 3.7 Retrieval

| # | Statement | Evidence |
|---|---|---|
| 3.7.1 | Retrieval is BM25 plus dense vectors, fused client-side with RRF (rank constant 60) on the Basic required path. The rank constant was selected on the pilot. | [EXPLORATORY] PREREG 4.3 (grid_pilot.json) |
| 3.7.2 | Recall falls monotonically with pool size. RRF session recall@10 is 0.968, 0.859, 0.684 and 0.4754 at H0 to H3. At H3 plain BM25 (0.494) and linear fusion (0.492) score slightly higher than RRF, and dense alone 0.345. | [EXPLORATORY] CURVE levels.*.*.session_recall@10 |
| 3.7.3 | 23 of 94 pilot questions are low-overlap at the whole pool (gold round ranked worse than 100 on both BM25 and dense). The cut was chosen after the counts were seen. | [EXPLORATORY] PREREG 3.2 (lowoverlap_pool_h3.json) |
| 3.7.4 | Session length alone separates gold from other sessions (AUROC 0.687 against a null of 0.448 to 0.547), so this benchmark carries a length shortcut. | [EXPLORATORY] XP E2 label-blind probe |
| 3.7.5 | A cross-encoder rerank (top 20) sits after fusion. It cost about 64 s per question at top 50 on CPU. | [OWNER-REPORTED] PREREG 4.3 (rerank_timing.json) |
| 3.7.10 | The rerank beats plain RRF at the whole pool: session recall@10 +0.059 [+0.016, +0.113], on 78 of 94 questions while the pooled rerank run was still in progress. | [EXPLORATORY] AFULL runs.H3 paired "RERANK minus RRF" (n 78) |
| 3.7.6 | Concept links boost and never filter. | [DESIGNED-UNTESTED] v1 DESIGN.md |
| 3.7.7 | Pull tools for the model: recall(query, as_of), open_event(id), query_facts(subject, predicate, as_of), timeline(entity). A Claude Code headless session used them over MCP to answer a current-versus-past question. | [OBSERVED] MC NOTES "MCP" demo |
| 3.7.8 | As-of filtering is only a safety rule on this benchmark: the day rule removes 0 gold sessions, so it cannot change a ranking here. | [OWNER-REPORTED] PREREG section 2 (verify.json) |
| 3.7.9 | Standing queries (percolator) are lexical only: Elasticsearch rejects vector queries inside the percolator. | [OBSERVED] LEDGER "Did not hold"; BEH percolator result |

### 3.8 Analysts and inbox

| # | Statement | Evidence |
|---|---|---|
| 3.8.1 | Analysts are long-lived headless sessions with read-only pull tools and a fixed tool-call budget. Findings come back as ids with provenance, never on the synchronous path of a turn. The tier was the user's proposal (WAL w014c9fca229). | [DESIGNED-UNTESTED] PREREG 5.2; asynchronous delivery not exercised by the eval (offline scoring only) |
| 3.8.12 | Analysts reorder better than plain RRF at pool scale: session recall@10 +0.130 at H3 and +0.099 at H2 (0.660 to 0.758, [+0.023, +0.175]); not resolvable at H1 (+0.031 [-0.031, +0.089]). Failures (2.1% to 4.3% of files) are counted as misses. | [EXPLORATORY] AFULL runs.H1, H2, H3 paired "ANALYST minus RRF" |
| 3.8.13 | With a 4,000-token context at H3, the analyst puts the current value of a changed fact in context for 0.93 of knowledge-update questions (stale only: 0.07), against 0.80 for the cross-encoder rerank and 0.27 for the RRF baseline (n 15; differences among the reordering arms not resolvable). | [EXPLORATORY] AFULL runs.H3 b4000 ku_gold_latest, ku_stale_only |
| 3.8.14 | Analysts used no tool at all in about half of the answered questions (42 of 92 at H3); a disclosed daemon defect shared one tool budget between two H3 variants. | [EXPLORATORY] PREREG results section "Disclosed defect" and tool-use lines |
| 3.8.2 | The canary ran end to end: 46 calls; the analyst used a mean of 3.59 tool calls per question, about 100k tokens and 22 s per call; a rerank call took about 31k tokens and 10 s. | [OWNER-REPORTED] PF DEVLOG "analyst canary"; CANARY |
| 3.8.3 | Inbox claims carry a fencing token and a 330 s renewable lease. A second turn at +40 s and +300 s gets nothing; a forged token and a superseded claimant confirm 0. | [REVIEWED] RV-AD confirm N7 FIXED (local probes); residuals: renew returns 0 on a lapsed lease while confirm returns 1, and confirm without a token is silent |
| 3.8.4 | A fixed 30 s claim lease delivers each inbox item once. It did not: it double-delivered for turns longer than 30 s. So: the renewable lease in 3.8.3. | [DID-NOT-HOLD] RV-AD N7 at 888c94b |
| 3.8.5 | Inbox delivery is exactly once across a paused turn, a checkpoint rollback and a fork of the native session id; the delivery cursor travels in the checkpoint. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #2 |
| 3.8.6 | An analyst run is keyed on (logical session, step, manifest hash), draws on the originating step's budget reservation and is killed when it stops making progress. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #17 |
| 3.8.7 | Findings follow a fixed schema with an explicit "nothing relevant" outcome that suppresses injection. | [DESIGNED-UNTESTED] LEDGER X07 |
| 3.8.8 | Analysts read untrusted transcripts and must never relay a planted instruction. | [DESIGNED-UNTESTED] OPEN-QUESTIONS H6 |
| 3.8.9 | Policy is re-evaluated at delivery, not only at enqueue. | [DESIGNED-UNTESTED] LEDGER X20 |
| 3.8.10 | Reviewer and click-tester sessions never receive items whose origin is in the author set. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #26 |
| 3.8.11 | A long-lived session is cheaper than one call per step: 20 single calls cost $0.7751 notional against $0.1246 for one 20-turn session (one run). The reviewer matched the single-call figures; the session figures could not be recomputed. | [OWNER-REPORTED] MC h3_rerun_head.json; RV-AD |

### 3.9 Model port and gateway

| # | Statement | Evidence |
|---|---|---|
| 3.9.1 | One set of ports (chat model, embeddings, rerank, search, agent tool surface, injection hook); the core depends on no vendor. | [OWNER-REPORTED] MC contract tests (fix/adapters-delta-16096 c20e208) |
| 3.9.2 | The customer default chat route is the Elasticsearch inference API. Generative AI connectors are deprecated in favour of inference endpoints that support `chat_completion`. | [REVIEWED] RV-G1 (with adversarial challenge) |
| 3.9.3 | Subscription adapters refuse to construct without an explicit development flag. The gateway gate uses an exact-type allow-list, a read-only copy of the backend map and a per-request re-check. | [REVIEWED] RV-AD confirm N1, N2 FIXED; residual: patching the class at runtime is not detected |
| 3.9.4 | The first subscription gate kept subscription adapters out of production paths. It did not: it failed open twice (a wrapper that did not carry the taint; a backend map mutable after the check). | [DID-NOT-HOLD] RV-AD N1, N2 at 888c94b |
| 3.9.5 | Call caps are atomic across processes and fail closed on a corrupt counter. Each budget is a separate counter, so there is no single global cap, and the Elasticsearch embedder and reranker adapters have no budget. | [REVIEWED] RV-AD PARTLY |
| 3.9.6 | Results do not depend on the adapter. The earlier "11 of 12 agree" used a lenient metric; raw text F1 between Codex and Claude answers was 0.35. | [DID-NOT-HOLD] MC NOTES "review corrections" |
| 3.9.7 | Pin and ban rules for models are data read at run time, never model literals in source. | [DESIGNED-UNTESTED] FAB (rule R23); OPEN-QUESTIONS J6 |
| 3.9.8 | Each headless subscription call carries about 10.5k tokens of harness overhead. | [OWNER-REPORTED] MC h3_rerun_head.json first_turn_input; PF DEVLOG |

### 3.10 Policy point and labels

| # | Statement | Evidence |
|---|---|---|
| 3.10.1 | One policy decision point sits at the model port, append, projection, context assembly and inbox delivery, never in a hook. It fails closed and doubles as the out-of-band kill list. | [DESIGNED-UNTESTED] GOT DESIGN-CHANGES; UNCOVERED-TOP30 #7 and #11 |
| 3.10.2 | Labels (tenant, access list, classification, region, handling) travel with every event; a derived row's access is the intersection of its inputs' access and its restrictions the union. | [DESIGNED-UNTESTED] OPEN-QUESTIONS J3 |
| 3.10.3 | Identity and tenant attributes cannot be overwritten by derived attributes; a non-boolean or erroring evaluation denies. | [DESIGNED-UNTESTED] LEDGER X18 |
| 3.10.4 | A refusal or a policy denial is never retried on another model without a recorded, authorised route. | [DESIGNED-UNTESTED] OPEN-QUESTIONS J9 |
| 3.10.5 | The policy point refuses subscription adapters in any production profile. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #30 |
| 3.10.6 | Which concrete policies apply (regions, classifications, approved models) is the client's or the user's choice; the system ships with none assumed. | [OPEN-DECISION] OPEN-QUESTIONS J11 |

### 3.11 Durable sessions and upgrades

| # | Statement | Evidence |
|---|---|---|
| 3.11.1 | The durable engine (Temporal) gives exactly-once effects under abrupt worker exit, for an external system that honours an idempotency key with unbounded retention. 34 exits placed inside the two critical windows gave exact counts; both broken-key controls fail. | [REVIEWED] RV-ENG F4t SUPPORTED; limit M7 (key retention of the stand-in) |
| 3.11.2 | A Postgres crash, a server crash with activities in flight and a crash with a workflow task in flight were not tested; only the SQLite dev server was used. | [DESIGNED-UNTESTED] RV-ENG F4 limits and M6 |
| 3.11.3 | Replaying captured histories before every deploy catches an incompatible build (20 of 20 replays failed); deployed anyway, all sessions froze without failing. | [REVIEWED] RV-ENG F5a SUPPORTED (one class of nondeterminism tested) |
| 3.11.4 | Patching works as a hotfix: old runs adopt the patch only after continue-as-new, and removing a patch early strands idle sessions. | [REVIEWED] RV-ENG F5b1, F5c SUPPORTED |
| 3.11.5 | A continue-as-new hop to a new task queue moves old sessions to new code. | [REVIEWED] RV-ENG F5b3 SUPPORTED; requires the hop in the old code |
| 3.11.6 | Pinned worker deployments keep old sessions on their build; for long sessions the old build runs until every session ends. | [REVIEWED] RV-ENG F5b2 PARTLY: pinning works; the labelled control does not discriminate |
| 3.11.7 | The recommended combination of pinned deployments plus a continue-as-new hop was never run together. | [DESIGNED-UNTESTED] RV-ENG "recommended combination" UNSUPPORTED |
| 3.11.8 | State migrations happen at a continue-as-new boundary. Ship the reader of the next shape before the writer: rolling back after a migration left 4 of 4 sessions stuck. | [REVIEWED] RV-ENG F6 SUPPORTED (rollback failure confirmed); the count "4 of 4" is the owner's (LEDGER, ENG F6) |
| 3.11.9 | Server and SDK upgrades by one minor version kept sessions running. There is no failing control, and the production schema-tool path was not used. | [OWNER-REPORTED] ENG F7; RV-ENG F7 PARTLY |
| 3.11.10 | A session is rebuilt into a fresh engine from the Postgres log alone; doorbells dropped by the new engine are recovered by reconcile. | [REVIEWED] RV-ENG F8 SUPPORTED with limits |
| 3.11.11 | "Reconcile re-derives pending turns after any restart" is false: reconcile runs only at the start of a run and when `more` is set. | [DID-NOT-HOLD] RV-ENG M1 (code; ENG NOTES still carries the claim) |
| 3.11.12 | After a rebuild the engine resolves granted approvals promptly, isolates poison inputs, creates inputs idempotently and keys rollups stably. It did not: a granted approval waits for its full deadline; a poison input (including a crypto-erased one) wedges the session; input creation is not idempotent on a retried API call; the rollup key comes from a counter. | [DID-NOT-HOLD] RV-ENG M2 to M5 (code; M3 shown by an artifact) |
| 3.11.13 | Elasticsearch upgrades, mapping changes and embedding-model changes go through reindex and an alias swap under load. After the cutover, the new index family sat outside erasure. | [OWNER-REPORTED] SYS F9 results; RV-SYS N2 |
| 3.11.14 | Postgres event schema changes use event versions and upcasting on read. | [OWNER-REPORTED] SYS F10_schema_evolution.json |
| 3.11.15 | An extraction-recipe change never serves mixed versions. The rule-based case passes; the real extractor is unmeasured. | [OWNER-REPORTED] SYS F11_recipe_change.json |
| 3.11.16 | The engine fixes for tenant keys, bound approvals and validated doorbells exist on the branch. | [OWNER-REPORTED] feat/engine-evolution ffa4ce5, 05593c7 (no review) |

### 3.12 Task-definition changes

| # | Statement | Evidence |
|---|---|---|
| 3.12.1 | A task amendment is an event; the next turn shows the new definition as current and the old one as superseded. User requirement: WAL w83503ef64b7. | [OWNER-REPORTED] SYS F1_F2_task_amend.json |
| 3.12.2 | Sessions holding an assumption are registered on the ask. On resume, a mandatory pre-turn reconciliation compares the sealed watermark against answers and decisions recorded since. | [DESIGNED-UNTESTED] GOT DESIGN-CHANGES; UNCOVERED-TOP30 #27 |
| 3.12.3 | A stale approval (approve version 3, amend to 4, execute) is refused. | [DID-NOT-HOLD] RV-WP row 27 PARTLY and F5(a): refused only if the executor passes the approval's version; an executor that reads the head first executes it |
| 3.12.4 | Memory written by a rolled-back, cancelled or unmerged run is retracted and cascades through provenance. | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #13 |

### 3.13 Tenancy

| # | Statement | Evidence |
|---|---|---|
| 3.13.1 | One index family per tenant. Document-level security on a shared index leaks term statistics: tenant A's BM25 scores change when tenant B adds documents, and `dfs_query_then_fetch` does not fix it. | [REVIEWED] RV-ES tenancy E9b and E9c REPRODUCED; E9d REFUTED |
| 3.13.2 | The adapters' physical index names are injective: wildcard, encoded and look-alike names are refused and the tenant term is forced. 100 names over 2 prefixes gave 0 collisions. | [REVIEWED] RV-AD confirm (tenant boundary passes); residual: no tenant term on a kNN query without a caller filter |
| 3.13.3 | The engine has tenant isolation. | [DID-NOT-HOLD] RV-ENG H1 (code, at 62916ae): keys and the workflow id carry no tenant, so two tenants sharing a session id collide; fix in 3.11.16 unreviewed |
| 3.13.4 | Cross-tenant red team including the connection pooler (forged tenant field, collisions, fuzzed ids, quota flood). | [DESIGNED-UNTESTED] UNCOVERED-TOP30 #9 |
| 3.13.5 | Tenant validation in the write path holds in every component. | [DID-NOT-HOLD] RV-WP minor finding: it is an `assert`, stripped under `python -O`, and absent from erasure, reader and log |

### 3.14 Licence tiers and the client stack

| # | Statement | Evidence |
|---|---|---|
| 3.14.1 | On an Elasticsearch Basic licence these work: the standard (BM25) retriever, kNN, rescorer, pinned, MMR diversify, and ES|QL MATCH, FORK and LOOKUP JOIN. | [OWNER-REPORTED] BEH E7_basic.json (feat/elastic-behaviour 6f53c80) |
| 3.14.2 | On Basic these return a licence error: the `rrf` and `linear` retrievers, `text_similarity_reranker`, ES|QL FUSE, ES|QL RERANK (`[inference]`), document-level security, and indexing into bbq_disk (the mapping is accepted; each document fails). | [OWNER-REPORTED] BEH E7_basic.json, E9_dls_basic.json, E6_bbqdisk_basic.json |
| 3.14.3 | So the Basic required path is: own embeddings computed outside Elasticsearch and stored as dense_vector, client-side fusion and rerank, one index family per tenant, and model calls made directly to the client's providers. Basic as the required path is main's framing (ASSUMPTION); the build tier is open (OPEN-QUESTIONS D4). | [OPEN-DECISION] WAL w6ffbac116a6; OPEN-QUESTIONS D4 |
| 3.14.4 | bbq_hnsw and int8_hnsw dense_vector indexing on a Basic node has no Basic-labelled artifact in the lab. | [DESIGNED-UNTESTED] RV-LIC untested list |
| 3.14.5 | Elastic-side fusion and rerank are declared optional Enterprise features with a client-side fallback. The licence probe re-checks after a TTL and does not latch on a transient error. | [REVIEWED] RV-AD (licence probe fixed); the Basic fallback was tested only against a 403 stub (MC NOTES) |
| 3.14.6 | The Elasticsearch inference API, semantic_text and the built-in E5 and ELSER models need Enterprise, or a trial, on self-managed clusters. | [REVIEWED] RV-LIC (semantic_text unusable on Basic); RV-G1 (inference API tiers; the exact Basic error is UNCONFIRMED) |
| 3.14.7 | Agent Builder, its `.mcp` connector and workflows were observed only on a trial licence, and research says Agent Builder needs Enterprise outside Serverless. Agent Builder is therefore not on the Basic required path, although the deployment brief lists it. | [OPEN-DECISION] MC NOTES (".mcp" is an enterprise licence type, enabled under the trial); RV-G1 (UNCONFIRMED snippets) |
| 3.14.8 | Agent Builder's pre-execution workflow replaces the user message with the rewritten prompt (one user message, prefixed with a sent-time line). | [OBSERVED] MC NOTES-addendum-g7 variant 1 (Kibana 9.5.6, stub model) |
| 3.14.9 | The pre-execution hook fails closed by itself: a failing step, or a run past 120 s, returns HTTP 500 and the agent run stops. Fail-open has to be built into the workflow. | [OBSERVED] MC NOTES-addendum-g7 variants 2 and 3 |
| 3.14.10 | The fail-open template (memcore returns the original prompt after 20 s or any error; a Kibana step timeout of 25 s with continue-on-failure) has not run live. Its step keys are an ASSUMPTION. | [DESIGNED-UNTESTED] MC NOTES-addendum-g7 ("not run live") |
| 3.14.11 | No real model has driven the Agent Builder path; only a stub has. | [DESIGNED-UNTESTED] GOT DESIGN-CHANGES "still missing" |
| 3.14.12 | A trial licence expires (the lab's on 2026-12-15) and switches features off under live indices. Every licensed feature needs a declared degrade mode that fails closed for isolation. | [DESIGNED-UNTESTED] GOT NEW-GOTCHAS MEM-14; UNCOVERED-TOP30 #21 |
| 3.14.13 | agent-fabric is a later provider adapter. Its memory slice F14 is planned, not built, and specified on a Postgres vector extension; this system would plug in as an out-of-process `memory` provider under ADR 0002. Fabric's append-only triggers conflict with an erasure cascade unless content stays out of its events. | [OPEN-DECISION] FAB headline and section F; WAL w6ffbac116a6 |
| 3.14.14 | Claude Code reaches the memory through a UserPromptSubmit hook or MCP. Codex hooks exist but none were installed, and the Codex pool refuses MCP configuration. | [OWNER-REPORTED] MC NOTES |

### 3.15 Owner work committed after the reviews (unreviewed)

These commits landed after the reviewed commits above. Only their commit messages were read for this document; none has a review, so none changes a tag above.

| # | Statement | Evidence |
|---|---|---|
| 3.15.1 | The write-path owner reproduced review findings F1 to F7 on the reviewed code (all seven "property_holds=false"), and added controls for N3S_eraser and N6 plus a ten-run registry check (dry runs only). | [OWNER-REPORTED] feat/writepath-rescue e5f6aca, 1ebee08 |
| 3.15.2 | The engine owner committed fixes for review findings H1 to H3 and M1 to M5 (tenant keys, approval request ids, validated doorbells, periodic reconcile, prompt approval resolution, rollups keyed by deadline, a dead-letter path, idempotent input append) and result provenance; 12 unit tests fail on 62916ae and pass after. Later commits add a pinned-plus-continue-as-new case (F5b4), approvals that wait for their request, and driver retries across a Postgres restart (effect "to be shown"). | [OWNER-REPORTED] feat/engine-evolution ffa4ce5, 05593c7, 492f900, f834ad2, ff285c2 |
| 3.15.3 | The adapter owner committed fixes for the inbox residuals (lapsed renewals reported, confirm token required), guards and budgets on the embedder and reranker, and a gateway route for the Kibana pre-execution workflow; each with tests that fail on c20e208. | [OWNER-REPORTED] fix/adapters-delta-16096 515cebf, 611d21c, d109124 |
| 3.15.4 | A policy decision point with a label lattice and enforcement exists, with thresholds pre-registered first; only unit tests and dry runs so far. | [OWNER-REPORTED] feat/policy-decisions c2153af, 4fda42f |

## 4. What changed from v1, and why

| v1 statement | v2 position | Evidence |
|---|---|---|
| Retrieval: BM25 + dense (+ sparse where licensed), fused, optional rerank | Kept as one input. Recall at pool scale is low, and no figure has been reproduced on Elasticsearch | [EXPLORATORY] CURVE; PREREG 4.3 |
| Concept state regenerated from facts; no durable process per concept | Concept owners as rows plus a keyed queue; durable per-concept processes stay out (decision record C10) | [OWNER-REPORTED] SYS C8 result; OPEN-QUESTIONS C10 |
| Temporal off the turn path, not exercised | Engine tested under F4 to F8 at the user's request (WAL w83503ef64b7) | [REVIEWED] RV-ENG |
| Write-time cue keys find no-overlap facts | Untested; fact extraction held by the user's choice (WAL w11160e65223) | [DESIGNED-UNTESTED] OPEN-QUESTIONS B8 |
| Kill rules K1 and K2 decide the facts and concept layers | Neither has run; the main eval target moved to a pooled-history curve at the user's choice (WAL we0a5cc3861e) | [OPEN-DECISION] WAL w11160e65223, we0a5cc3861e |
| Supersession links as part of facts | Advisory labels only; prose supersession detection did not reach its recall bar | [DID-NOT-HOLD] LR GATE3_RESULT |
| Public benchmark data only (ASSUMPTION) | The user chose the public benchmark plus their own redacted sessions (WAL w0fd59f34a59); which sessions is open (WAL w9e601030816) | [OPEN-DECISION] WAL w9e601030816 |

## 5. Path from here to proven

Order: review-confirmed material blockers first, then UNCOVERED-TOP30 in its rank order, then the eval steps. "Model calls" says whether the experiment needs LLM calls. Owner streams follow UNCOVERED-TOP30.

| # | Experiment | Closes | Owner stream | Model calls |
|---|---|---|---|---|
| 1 | Erasure verify for a subject above the terms limit (reproducer with a lowered `index.max_terms_count`), a fault-isolated sweeper, verify inside error handling | 3.3.7 (RV-WP F1) | write-path | no |
| 2 | Erasure under a lease shorter than the rebuild, with a concurrent projector, then the fix (lease renewal or a batched rebuild) | 3.3.8 (RV-WP F2) | write-path | no |
| 3 | Fence scope binding and role separation: a stale writer acquires a junk scope and writes facts; a non-owner app role; triggers that cannot be disabled | 3.2.8 (RV-WP F3) | write-path | no |
| 4 | Completion guard computes Postgres counts itself; registry read-only; INSERT fenced | 3.3.6 (RV-WP F4) | write-path | no |
| 5 | Decisions bound to the approval event id; tenant-wide heads if chosen; the idempotency key stores a request hash | 3.5.6, 3.12.3 (RV-WP F5) | write-path (decision core) | no |
| 6 | Takeover with a zombie stalled after its insert; lock and idle-in-transaction timeouts | 3.1.4 (RV-WP F6) | write-path | no |
| 7 | Engine: two tenants with one session id; two approval gates per session; a signal carrying another session's id; delta review of ffa4ce5 and 05593c7 | 3.13.3, 3.11.16 (RV-ENG H1 to H3) | engine | no |
| 8 | Engine: inputs appended to idle live sessions without a signal; grant-to-resolution latency after rebuild; poison-input dead letter; a retried append gives one input | 3.11.11, 3.11.12 (RV-ENG M1, M2, M4, M5) | engine | no |
| 9 | Engine: Postgres restart in the effect gap; two workers on one queue; a zombie old worker during a host move; git SHA and times in every result file; F4 rerun at the branch head | 3.11.2 (RV-ENG M6, P1) | engine | no |
| 10 | Engine: pinned deployments plus a continue-as-new hop, run together | 3.11.7 | engine | no |
| 11 | Session-lease epoch fence on log, inbox, analyst jobs and projections (liveness half of the zombie case) | UNCOVERED #1 | write-path | no |
| 12 | Inbox exactly once under a paused turn, a checkpoint rollback and a fork of the native id | 3.8.5 (UNCOVERED #2) | adapters | no |
| 13 | Checkpoint seal carries watermark, manifest hash and projection generation; resume replays the sealed block | 3.6.6 (UNCOVERED #3) | write-path | no |
| 14 | Stored prompt-injection attack set frozen before tuning, with an absolute bar, including re-ingested injected blocks | 3.8.8 (UNCOVERED #4) | llm-stage | yes |
| 15 | Erasure reaches every content store from a code-enumerated inventory; snapshot restore gated | 3.3.14 (UNCOVERED #5) | write-path | no |
| 16 | Lost log tail on asynchronous Postgres failover; external chain-head anchor | 3.1.6 (UNCOVERED #6) | write-path | no |
| 17 | Policy decision point with labels and egress canaries, failing closed | 3.10.1 to 3.10.5 (UNCOVERED #7) | new stream (policy) | no |
| 18 | Remaining adapter delta checks: streaming production-profile gate, termination mid-stream, concurrent analyst calls against one reservation | 3.9.5 (UNCOVERED #8) | adapters | no |
| 19 | Cross-tenant red team after the fixes, including the connection pooler in transaction mode | 3.13.4 (UNCOVERED #9) | adapters | no |
| 20 | Decision core: authorised principals, contested state, no self-approval | 3.5.9 (UNCOVERED #10) | new stream (decision core) | no |
| 21 | Out-of-band kill reaching analysts, inbox, model port and append | 3.10.1 (UNCOVERED #11) | adapters | no |
| 22 | Evidence class joined from the ledger, with minimum-of-inputs propagation | 3.4.5 (UNCOVERED #12) | write-path | no |
| 23 | Retraction of memory written by rolled-back or unmerged runs | 3.12.4 (UNCOVERED #13) | write-path | no |
| 24 | Compaction re-injection probe and the cache cost of a regenerated block | 3.6.6, 3.6.9 (UNCOVERED #14) | llm-stage | yes |
| 25 | Currency at pool scale with a real embedding leg in the freshness scan and a false-challenger floor set first | 3.6.7 (UNCOVERED #15) | eval | no for the scan; yes for prose-change detection |
| 26 | Engine F4 to F8 verdicts rerun with saved per-run artifacts and corrected NOTES | 3.11.x (UNCOVERED #16) | engine | no |
| 27 | Analyst replay idempotency, budget reservation, progress-based kill | 3.8.6 (UNCOVERED #17) | adapters | no (counting stub) |
| 28 | Ingest live turns from the model-port send/receive log with gap detection | UNCOVERED #18 | new stream | no |
| 29 | Secret scanning before append, including encoded variants | 3.1.9 (UNCOVERED #19) | write-path | no |
| 30 | Embedder identity and vector options pinned per generation | 3.2.13 (UNCOVERED #20) | write-path | no |
| 31 | Licence transition rehearsal on a copy (trial to Basic) with a declared degrade mode per feature, including the inference API and Agent Builder on Basic | 3.14.4, 3.14.6, 3.14.7, 3.14.12 (UNCOVERED #21) | new stream (Elastic operations) | no |
| 32 | Projector under flood-stage write blocks; rebuild-mode settings leak | 3.2.10 (UNCOVERED #22) | write-path | no |
| 33 | xmin stall control, and concurrent writers through the connection pooler | 3.1.5 (UNCOVERED #23) | write-path | no |
| 34 | Eval harness gates: label-leak probe, headroom, pool-size floor, denominator accounting, distractor purity; freeze D2 margins before the analyst arm | UNCOVERED #24 | eval | no |
| 35 | LLM-stage results at the deployed operating point with independent replications and a human-audited sample | 3.4.4, 3.9.6 (UNCOVERED #25) | llm-stage | yes |
| 36 | Reviewer independence at inbox routing; author model recorded on every event | 3.8.10 (UNCOVERED #26) | adapters | no |
| 37 | Answers to asks reach parked assumption holders; stale approvals refused | 3.12.2 (UNCOVERED #27) | write-path | no |
| 38 | Repo-scoped facts with a commit axis; as-of by log sequence | 3.4.7 (UNCOVERED #28) | write-path | no |
| 39 | Injection canary and memory-producer parity across hosts and CLI versions | UNCOVERED #29 | adapters | yes |
| 40 | User decisions that gate whole streams (OPEN-QUESTIONS section M) | UNCOVERED #30 | needs-user | no |
| 41 | Kibana fail-open pre-execution template run live (ok, refused port, past-timeout variants) | 3.14.10 | adapters | no (stub model) |
| 42 | Analyst arms re-run with candidate order shuffled (anchoring control), the tool-budget defect fixed, and the complete rerank arm; then the same comparison on the locked test split with a rule committed first | 3.8.12 to 3.8.14 | eval | yes |
| 43 | Pooled rerank run finished on all 94 pilot questions (top 20) | 3.7.10 | eval | no |
| 44 | The retrieval pipeline re-run end to end on Elasticsearch before any recall figure is quoted for the Basic target | 3.7.2 | eval | no |
| 45 | Facts layer (K1, then K2 if K1 passes), after the user's held decision on extraction volume | 3.4.1, 3.4.6 | eval and llm-stage | yes |

## 6. Contradictions found in the inputs (not resolved here)

| # | Contradiction | Where |
|---|---|---|
| 1 | Whole-pool recall: OPEN-QUESTIONS section H says 0.46; LEDGER says 0.48 in one row and 0.4754 in its corrections; XP E2's recomputed flat baseline is 0.481 at a different fusion depth. The registered figure is 0.4754; 0.46 is the superseded old-sets value. | OPEN-QUESTIONS (lab main); LEDGER; XP |
| 2 | Low-overlap count: OPEN-QUESTIONS section H says 24 of 94; the current whole-pool count is 23 of 94. | OPEN-QUESTIONS (lab main); PREREG 3.2 |
| 3 | X04 quote check: LEDGER at 603bb3b says 2,301 of 2,373 with 58 ambiguous (a rough reviewer pass); the owner's run says 2,351 and 53. An uncommitted LEDGER edit in the lab main checkout already carries the owner's figures. | LEDGER (603bb3b and working tree); PF PROVENANCE_RESULT.md |
| 4 | Engine NOTES (at the branch head) still gives 200 problems, 19 boundaries, about 15 turns/s, an SDK swap 100 s later and "reconcile after any restart"; the files give 202, 18, 13 and 107 s, and the reconcile claim is false. | ENG NOTES; RV-ENG P4, M1 |
| 5 | Exact-retriever time per question at H3: PREREG 4.3 says mean 4.34 s (max 8.55 s); curve_pilot.json says mean 4.12 s (max 7.77 s). | PREREG 4.3; CURVE seconds_per_question |
| 6 | Session cost ratio: LEDGER says 20 fresh calls cost 6.5 times one 20-turn session; the rerun at the head gives 0.7751 / 0.1246, about 6.2. | LEDGER "Did not hold"; h3_rerun_head.json |
| 7 | Rerank cost: OPEN-QUESTIONS section H says about 26 s per question; the only timing artifact gives about 64 s per question at top 50. No artifact for 26 s was found. | OPEN-QUESTIONS (lab main); rerank_timing.json |
| 8 | The deployment brief puts Agent Builder and the inference API in the client stack with Basic as the required path; the lab observed them only on a trial, E7_basic shows `[inference]` refused on Basic, and research says Enterprise. | Brief; MC NOTES; BEH E7_basic.json; RV-G1 |
| 9 | Systems result files report `pass: true` for C1 blue/green v2, C3 crash phases, C5, I3 and I4/I5, which RV-SYS says the raw files do not support; C3d_race_v2.json says `pass: false` while two commits say PASS. | SYS results; RV-SYS |
| 10 | LEDGER "Held" lists four engine results as "owner-reported, under review"; RV-ENG has since reviewed them (F4t, F5a and F8 supported; the reconcile claim false). | LEDGER; RV-ENG |
| 11 | LEDGER classes "bulk partial update on unrefreshed semantic_text drops embeddings" as owner-reported; RV-ES established it independently. | LEDGER; RV-ES |
| 12 | LEDGER says deleted text is "purged only after retention-lease sync plus force merge"; RV-ES found two reliable paths, including reindex and whole-index DELETE. | LEDGER; RV-ES |
| 13 | LEDGER "Held" keeps the Agent Builder pre-execution workflow as observed, but the Kibana template that used the raw `<memory>` format is superseded by the randomised framing. | LEDGER; RV-AD confirm |
| 14 | LEDGER's "evidence in a 4,000-token context 0.41" (mean gold-session recall) and X01's 0.53 / 0.33 (any / all gold present) are different metrics on the same run, not a disagreement. | LEDGER; RAGCTX; XP |
| 15 | The main register (lab main) shows most C, F and I rows as todo while result files exist in worktrees. | OPEN-QUESTIONS (lab main 603bb3b); GOT CORRECTIONS |
| 16 | The C9 row text differs between the systems worktree copy of OPEN-QUESTIONS and lab main. | OPEN-QUESTIONS (two copies) |
| 17 | The plain-RRF session recall@10 baseline differs by file: at H3 0.4754 (CURVE) against 0.473 (AFULL and the exact leg in es_hybrid_pooled); at H2 0.684 (CURVE) against 0.660 (AFULL) and 0.695 (exact leg in PREREG 4.3 item 5b). The cause is not stated in the files. | CURVE; AFULL; PREREG 4.3 |
| 18 | Knowledge-update currency at H3 with 4,000 tokens: RAGCTX gives current 0.333, stale only 0.467; the AFULL RRF baseline gives 0.267 and 0.667 on the same 15 questions; 4k gold-session recall 0.412 against 0.421. | RAGCTX; AFULL |