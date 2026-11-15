# Gotchas not in the factory catalogue

## Lens: memory-x-factory

### MEM-01 · Checkpoint seals the conversation but not the memory watermark and injected-context manifest, so a resumed turn sees different memory than the turn it replays  `critical · reasoned`

- **Trigger:** Session is resumed or replayed from a sealed checkpoint (RES-03) while the memory log has advanced, an item was superseded, or the index was rebuilt since the seal; the per-turn context block is regenerated rather than restored.
- **Why it bites:** The lab measured ~1 s search visibility and non-reproducible approximate vector results across a rebuild, so regenerating the block gives different evidence for the same turn; the transcript then shows decisions that the visible context no longer supports, and the acted-on-version check (I5) compares against the wrong watermark.
- **Detect:** Ledger checkpoint row lacks (log watermark, manifest hash, projection generation); on resume, hash(regenerated block) != hash(sealed manifest) for the first turn; acted-on-version audit finds steps whose recorded watermark is newer than the checkpoint.
- **Solution:** Make (log watermark, manifest content hash, projection generation) part of the ledger commit row; store the injected block content-addressed. On resume the first turn re-sends the sealed block verbatim, and any fresher retrieval is a separate labelled delta block, never a silent replacement.
- **Test:** Seal at turn N, append a superseding event, resume; assert the prompt at N+1 contains the sealed block hash plus a delta block naming the new event. Control: a regenerate-on-resume variant must fail the same assertion, and a seal row with the manifest hash removed must be refused by the commit.
- **Nearest existing:** RES-03 and RES-10 cover checkpoint atomicity and ledger digest re-injection; lab I5 records version per step but not that the seal must carry and replay the exact manifest. Not in the 40 cases.

### MEM-02 · Inbox delivery cursor lives outside the checkpoint: resume to an earlier checkpoint loses findings, and fork-per-Run orphans or duplicates them  `high · reasoned`

- **Trigger:** A finding is delivered and shown at turn k, then the session is resumed from checkpoint k-1 (discarded tail), or each Run forks into a new native session id (RES-02, WSP-09) while the instance registry and inbox are keyed by native id.
- **Why it bites:** The inbox marked it delivered, but the transcript that contained it was rolled back, so the agent never sees it again; or the new Run id has an empty inbox while the old id's queue is orphaned, or both ids map to the same task items and get the finding twice. The lab already saw double delivery under concurrent turns.
- **Detect:** Delivered-to ids in the inbox table have no matching manifest in the sealed transcript; registry shows more than one live native id per (logical session, task item); undelivered rows addressed to a closed Run id.
- **Solution:** Key inbox and registry on logical session id, not native id. Mark delivery tentative until the seal that contains it commits; on resume, re-offer everything delivered after the sealed delivery cursor. Dedupe by finding id against the manifest, and carry the cursor in the checkpoint row.
- **Test:** Deliver finding F at turn k, kill before seal, resume from k-1, assert F appears exactly once in the next prompt; repeat with a fork-per-Run id change. Control: ack-at-show variant loses F, native-id-keyed variant orphans F; both must fail.
- **Nearest existing:** Lab H4 specifies exactly-once ordered delivery and the adapter review found double delivery; no row covers the interaction with checkpoint rollback or fork-per-Run ids. Not in the 40 cases.

### MEM-03 · A zombie worker after lease loss keeps appending memory events and triggering analysts, and its output is projected into facts  `critical · reasoned`

- **Trigger:** Worker loses its lease (partition, pause) but its agent process still runs turns; the memory service authenticates only tenant and session, not the lease epoch.
- **Why it bites:** FEN-14 handles forks of the factory event log, but the memory log has downstream projections: the zombie's events become derived dated facts, concept docs and analyst jobs (with tool budget and cost) before anyone marks them unadopted, and findings come back addressed to a logical session now owned by another Run.
- **Detect:** Memory append or analyst-start whose fencing epoch is below the session's current epoch; projection rows whose source events have no adopted checkpoint; analyst spend attributed to a stale epoch.
- **Solution:** Every append, analyst job and inbox write carries the lease epoch and is checked in the same transaction; stale-epoch appends are stored as quarantined (not projected, not retrievable) and analysts discard on mismatch. Adoption at the next committed checkpoint promotes events through a normal event.
- **Test:** Freeze worker A, take over with B, thaw A and let it run two turns containing a planted marker claim; assert no fact, vector or analyst call exists for the marker and that adopted events are unchanged. Control: tenant-only auth variant projects the marker and must fail.
- **Nearest existing:** FEN-14, FEN-03, CAP-19 fence the factory log, sinks and spend; none extends the fence to the memory log, projections and analyst fan-out. Not in the 40 cases.

### MEM-04 · Memory written by a run that is later rolled back or whose branch never merges stays current  `high · reasoned`

- **Trigger:** Run writes facts and decisions ("module X now uses design Y", "tests pass at SHA"), then the session is reset to an earlier checkpoint, cancelled with compensation, or its PR is closed unmerged.
- **Why it bites:** The lab's supersession detector found only 10 of 16 real updates, so stale facts cannot be retired by detection; GAP1-01 compensates external effects but nothing retracts the memory the run produced, so later sessions retrieve rejected work as current.
- **Detect:** Facts whose provenance run/epoch maps to a ledger row marked abandoned, CANCELLED, or a PR never merged; retrieval returns such a fact with no status label.
- **Solution:** Events carry run id and adoption status (speculative until the covering checkpoint commits and, for repo-state claims, the commit lands). Rollback or cancel appends a retraction event; projections and context assembly filter by status and cascade through input-set provenance as in the erasure path, rather than deleting.
- **Test:** Run writes a marker decision, reset to prior checkpoint, query "what was decided"; assert the marker is absent from context and present as retracted in the audit view. Control: no-status-filter variant returns it and must fail.
- **Nearest existing:** GAP1-01 (compensation of effects), lab C3 (erasure cascade via provenance) and I4 (pending versus decided); neither covers adoption status of derived memory. Not in the 40 cases.

### MEM-05 · Time-keyed validity intervals are wrong for repository facts because the tree can move backward while the clock moves forward  `high · reasoned`

- **Trigger:** A fact about code (file layout, API shape, test status) was derived at commit A; the session is resumed at the ledger SHA B (checkout of an earlier checkpoint, a rebase, or another session merged first) and as-of or currently-valid filtering uses event time.
- **Why it bites:** The lab's validity design (A4, B10) orders by time, so the fact is served as current although HEAD no longer contains what it describes; the transcript and the workspace then disagree and the agent edits against a phantom.
- **Detect:** Fact with a repo anchor whose commit is not an ancestor of the session's current HEAD, or whose touched-path footprint changed between anchor and HEAD.
- **Solution:** Repo-scoped facts carry (repo, commit or tree hash, path footprint) and a second validity axis, reachability from the session's HEAD, evaluated at context assembly. Unreachable or footprint-changed facts are shown as STALE_BASE with the divergence, never as current.
- **Test:** Derive fact at A, reset to earlier B that predates the change, build context; assert the fact is labelled STALE_BASE. Control: time-only validity variant serves it as current and must fail; also assert a fact on an untouched footprint stays current (no blanket staleness).
- **Nearest existing:** RES-19 and WSP-30 mark old evidence STALE on long pause and EFX-22 uses tree and footprint hashes for evidence; lab A4 validity is time-only. The memory fact store has no commit axis. Not in the 40 cases.

### MEM-06 · Injected context becomes part of the native transcript and is re-ingested as if the user or agent had said it  `critical · reasoned`

- **Trigger:** The memory ingester reads native session JSONL or the SessionStore mirror, where hook-injected blocks, analyst findings and state cards appear as user-role or attachment records.
- **Why it bites:** Each turn re-ingests the previous turn's block as new speech: facts are duplicated and counted as corroboration, untrusted quotes inside findings are promoted to user-origin trust, and context grows by feedback. The lab saw a forgeable delimiter in the prompt hook and the owner's hooks leaking text into headless outputs, so delimiter-based stripping is already unsafe.
- **Detect:** Facts or events whose source span hashes equal a recorded injected block or contains a manifest id; growth of event count per turn exceeding genuine new speech; trust origin user on text that appears in an analyst finding.
- **Solution:** Ingest from the model port send/receive log, not from native transcripts, and tag every injected block with a manifest id plus content hash at the port. The ingester drops spans matching injected content by hash and by id, never by delimiter, and an event may not raise trust above its recorded origin.
- **Test:** Run a 20-turn session with a planted quote inside an injected finding; assert zero events and facts sourced from injected spans and event count linear in genuine turns. Control: ingest-raw-JSONL variant must show duplication and trust upgrade and fail.
- **Nearest existing:** SEC-05 covers injection persisting via memory and transcript replay; the 40 cases include assistant restatement becoming a fact and forged provenance; none cover the injected block itself looping back through transcript ingestion. Lab precursor: hook leak and forgeable delimiter.

### MEM-07 · A CLAIMED factory note is stored as a present-tense fact and loses its evidence class, so retrieval presents it next to VERIFIED  `high · reasoned`

- **Trigger:** An agent's scratch note or ticket comment ("deployed, tests pass") is ingested; the extractor emits a fact with trust from the actor, but the fact schema has trust origin and no evidence class.
- **Why it bites:** Retrieval and the state card cannot tell CLAIMED from REALLY_TESTED, and a derived fact combining claimed and verified inputs gets the higher class by default; the lab's extractor already loses a third of facts and cannot be trusted to carry class in text.
- **Detect:** Fact with class above the minimum of its input events, or a fact asserting done/live/passing with no join to a ledger evidence id.
- **Solution:** Evidence class is never extracted from text. It is joined from the ledger record the event references; derived facts take the minimum class of inputs, and status-like claims without a ledger join are stored as CLAIMED quotes. Status queries are answered from the ledger evidence view, not retrieval.
- **Test:** Plant a CLAIMED "deployed live" note with no deploy record; assert no fact with class VERIFIED/LIVE exists and the status answer is CLAIMED. Control: a variant letting the extractor assign class from wording must produce LIVE and fail.
- **Nearest existing:** OPS-12 and OPS-01 keep status and VERIFIED writes in the evidence view; lab C5 covers trust-gated supersession by origin. Neither defines class propagation (min lattice) through derived facts. Not in the 40 cases.

### MEM-08 · An ask answered in one session never reaches a dependent session that is parked, and inbox delivery has no turn boundary to ride on  `high · reasoned`

- **Trigger:** Session B parked (lease released, no process) while holding an assumption on ask X; session A gets the answer, possibly the opposite; B resumes later from a checkpoint sealed before the answer.
- **Why it bites:** H4 delivers at the next turn boundary, which a parked session does not have; B can resume, replay and act on the assumption, producing work the answer already invalidates. Routing by requirement graph (H5) targets live registry entries, so a parked session is invisible to it.
- **Detect:** Resumed session whose sealed watermark predates an answered ask that it registered as assumption holder; open assumption edges whose ask is answered with no delivery or wake record.
- **Solution:** Register the session as assumption holder on the ask at the time it proceeds. The answer event emits a wake signal and marks the assumption invalidated for parked holders, and resume runs a mandatory pre-turn reconciliation of sealed watermark against answered asks and decisions touching its items, injected as a state-card section before any step.
- **Test:** Park B on assumption yes, answer no in A, resume B; assert the first prompt contains the answered-since-seal entry and no step runs on the old assumption. Control: boundary-only delivery variant lets B proceed and must fail.
- **Nearest existing:** OPS-19 (duplicate or late answers), ORC-11 (approval relay), lab H4/H5/J1 (inbox, routing, cross-thread notify). None handles parked holders without a turn boundary. The 40 cases cover stale-version confirmation, not answers missing parked dependents.

### MEM-09 · Replayed or retried turns re-trigger analysts, so factory retries multiply analyst spend and findings  `high · reasoned`

- **Trigger:** Temporal activity retry, resume replay, or lease takeover re-executes a turn; each turn boundary starts the relevance-analyst cascade keyed only by turn text or timestamp.
- **Why it bites:** The lab measured ~10.5k tokens of overhead and a session-log directory per headless call, and analysts are long-lived sessions with tool budgets; N sessions x retried turns x analysts compounds, duplicate findings reach the inbox, and the cost lands on no step budget (CAP-02 reserves per step).
- **Detect:** More than one analyst run per (logical session, step_n, manifest hash); analyst cost with no parent step id; duplicate finding ids across runs.
- **Solution:** Analyst job idempotency key (logical session, step_n, manifest hash) with a result cache; reuse on replay. Meter analysts through the cost gateway under the originating step's reservation, and skip the cascade when search confidence passes a pre-registered threshold or the replay is flagged.
- **Test:** Kill and retry one turn five times with a counting stub model port; assert exactly one analyst run and one finding set. Control: key-by-timestamp variant produces five runs and must fail; also assert total tokens per step stay within the reservation.
- **Nearest existing:** CAP-02, CAP-07 (spend and fan-out inside a step), ORC-03 (reconcile-first for agent turns); lab H7 caps spend but not replay idempotency. Not in the 40 cases.

### MEM-10 · Different hosts, hook sets and adapter versions produce different memory events for the same session  `high · reasoned`

- **Trigger:** Workers differ in hook configuration, CLI version, adapter version or normalisation (e.g. the owner's own harness hooks leak text into headless output); a session migrates or two hosts serve one task.
- **Why it bites:** Event text, tool-output capture and injected-block shape differ by producer, so a later rebuild or extraction treats equivalent turns differently and polluted events (leaked hook text) enter the append-only log permanently. The lab observed the hook leak on one host; RES-08 and WSP-02 enforce policy parity, not memory-producer parity.
- **Detect:** Same scripted turn on two hosts yields different event streams; events with no producer tuple; hook-origin strings present in agent output events.
- **Solution:** Every event carries a producer tuple (adapter version, CLI version, hook-set hash, host class). A per-lease memory canary event must round-trip with the expected normalised form before the lease is granted, and ingestion quarantines producers outside the allowlist rather than appending.
- **Test:** Replay one scripted 5-turn session on two worker configs and diff normalised events; control with identical configs must show zero diff, and a worker with a deliberately leaking hook must be refused by the canary.
- **Nearest existing:** RES-08, WSP-02, CAP-18, OPS-20 cover policy, config and version-tuple parity for the agent runtime; none require version tuples on memory events or a memory canary. Lab precursor: hook text leaks into headless outputs. Not in the 40 cases.

### MEM-11 · The manifest records what was sent, not what the model still holds after the harness compacts, so acted-on version audits are wrong  `high · reasoned`

- **Trigger:** Agent harness auto-compaction runs inside a long session after a block was injected; the summary absorbs or drops the block, or merges finding text into model-authored prose.
- **Why it bites:** I5 audits work against 'injected at turn t', but after compaction the model may act without the block (false OK) or on its absorbed paraphrase with provenance and trust label gone (false stale), and the summary is later ingested as agent-authored text. RES-10 re-injects the ledger digest but does not repair the manifest or provenance.
- **Detect:** Compaction event with manifest entries older than the compaction still counted as in-context; summary text overlapping finding quotes while the finding id is absent.
- **Solution:** Inject at the model port as ephemeral per-turn blocks that never enter the persisted transcript, so compaction cannot absorb them. Log compaction as an event that ends in-context status for earlier blocks and forces regeneration of the state card and manifest on the next turn.
- **Test:** Force compaction mid-session, then compare finding ids the agent can name against the port send log and manifest; assert the manifest shows only blocks sent after compaction. Control: transcript-injected variant lets the summary carry the finding and must fail.
- **Nearest existing:** RES-10 (compaction drops requirements, re-inject digest) and 40-case 'compaction or summary drops the qualifier'; neither addresses the manifest's truth about in-context state or the summary being re-ingested as agent speech.

### MEM-12 · Erasure cannot reach copies in checkpointed transcripts, handoff notes and ledger digests, and resume re-injects erased text  `critical · reasoned`

- **Trigger:** A subject is erased from the memory log and projections, but earlier injected blocks and quotes sit in sealed transcripts (SessionStore mirror, S3 versions, Object Lock), generated handoff notes and per-session keyed checkpoints, and the session is later resumed.
- **Why it bites:** Per-session keys (SEC-17, OPS-22) shred whole sessions, not one subject's text inside many; the lab already found a subject's rows surviving in an unregistered extraction cache, and checkpoints are the same pattern at larger scale. Resume then replays the erased text into a prompt, possibly to a provider.
- **Detect:** Resume-time scan of the assembled prompt against the erasure tombstone set finds subject text; checkpoint artifacts with no registered input-set provenance back to the subject.
- **Solution:** Register every checkpoint artifact (transcript segment, handoff note, digest) in the derivation registry with the manifest ids it contains. The erasure cascade enumerates affected sessions, marks them tainted, and forces resume from a cut before the first affected injection or from a regenerated, scrubbed handoff path.
- **Test:** Plant a canary subject, inject it, seal, erase, resume; assert the first prompt and any provider-bound payload contain zero canary bytes. Control: unregistered-checkpoint variant leaks the canary and must fail.
- **Nearest existing:** SEC-17/OPS-22 (erasure vs WORM, per-session keys), lab C3 and the unregistered extraction cache finding, 40-case 'erased content survives in summaries, inboxes, caches'. Not covered: sealed transcripts and handoff notes as derived copies that gate resume.

### MEM-13 · Requirement-graph routing delivers the author's analysis to reviewer and click-tester sessions, breaking independence  `high · reasoned`

- **Trigger:** Analysts route findings to every session whose current task items they serve (H5); the author, the independent reviewer and the click-tester all hold the same items, and findings are derived from the author's transcript and claims.
- **Why it bites:** EFX-24 builds a reviewer packet with only ACs, diff and verifier results, but a memory inbox is a second channel that re-adds the author's reasoning and CLAIMED assertions, so a reviewer's pass is no longer independent and the model-family author-set rule (CAP-14) is silently moot.
- **Detect:** Finding or context block delivered to a reviewer or click-tester session whose provenance includes events from a session in the author set of the commit range.
- **Solution:** Add session role and the author set to inbox delivery policy at the existing delivery choke point: reviewer and click-tester sessions receive only items whose origins lie outside the author set, plus verifier-class evidence. Role is read from the ledger, not from session text.
- **Test:** Plant a marker claim in the author session and a shared task item; assert the reviewer's prompts and inbox contain no author-origin items, while the author's inbox does. Control: item-only routing delivers the marker and must fail.
- **Nearest existing:** EFX-24, EFX-25, CAP-14 (reviewer independence by packet and model family); lab H5 routes by item only. The 40 cases cover confused deputy across principals, not role-based independence across sessions of one principal.

## Lens: elastic-operations

### MEM-14 · Trial licence expiry on 2026-12-15 flips features off under live indexes, and fallback code can fail open  `critical · reasoned`

- **Trigger:** The trial ends and the cluster reverts to Basic while semantic_text fields, inference endpoints, rerank, document-level security and bbq_disk indexes exist and live traffic continues.
- **Why it bites:** Each Enterprise feature fails differently (hard error on the query, an indexing rejection, or a role that stops applying) and the lab only recorded the Basic-versus-trial error text. A try/except that falls back to client fusion could also fall back to unfiltered reads where DLS was the isolation. Past results (A7, A9, A13, E11) also stop being reproducible after the date.
- **Detect:** A daily capability probe that runs one canned call per licensed feature (rerank, semantic query, a DLS-restricted search, a bbq_disk kNN, ingest to semantic_text) and records feature, outcome and exact error. Alert on GET _license status or expiry under 30 days.
- **Solution:** Keep a per-feature declared degrade mode in code: fail closed for isolation, a named client-side fallback for fusion and rerank, and a refused write for semantic_text. Never use DLS as the tenant boundary (index per tenant). Rehearse with POST _license/start_basic on the lab node and save the error table.
- **Test:** On a copy of the lab node with all the features populated, run start_basic and then the probe suite. Pass means every row matches its declared mode and the DLS-role search returns an error and never extra documents. The control is the same suite before the downgrade, where all rows must pass.
- **Nearest existing:** Register A7 and A13 measure Basic versus trial at one moment; no catalogue entry covers a licence transition under live data (GAP1-15 is commercial terms only).

### MEM-15 · Embedding space changes silently when the model, its prefix or its normalisation changes under an existing vector field  `critical · reasoned`

- **Trigger:** E5-family vectors are indexed with a 'passage: ' prefix, then a rebuild or adapter swap (external vectors versus the built-in endpoint, A11) embeds queries or new documents with a different prefix, model revision or normalisation but the same dimensions.
- **Why it bites:** kNN keeps returning results with no error, so recall falls without any signal. Old and new documents sit in different spaces in one index, which breaks the rule that mixed versions are never served together.
- **Detect:** Store embedder_id (model, revision, prefix hash, normalisation) on every document and in index _meta. A fixed canary set of about 20 query-passage pairs has expected cosine bands, checked at query time and after every deploy.
- **Solution:** The embedder identity is part of the index generation name, so a change means a new generation and a blue/green swap, never an in-place write. The query path refuses when its embedder_id differs from the index _meta.
- **Test:** Index the pilot with the correct prefix, then query with the prefix dropped and with a different model of the same dimension. The canary check must fail in both cases. The control is the correct prefix, which must pass and match the baseline recall.
- **Nearest existing:** Register F9 and J6 mention embedding change and pinning as questions; no catalogue entry covers the same-dimension silent mismatch.

### MEM-16 · Reusing or deleting an inference endpoint id that mappings and ingest pipelines reference  `high · unverified`

- **Trigger:** An endpoint is deleted with force, or recreated under the same inference_id with a different model, dimensions or service, while semantic_text fields, pipelines or the default endpoint still point at it. The Elastic default endpoint also changes between versions.
- **Why it bites:** Indexing starts failing mid-projection, or old stored embeddings are queried through a different model. The id looks stable but the vector space is not. After an in-place upgrade, a field with no explicit endpoint can resolve to a new default.
- **Detect:** A registry maps endpoint id to model hash and every referencing index. A nightly job compares GET _inference with the registry and flags changed model_settings or an unknown id.
- **Solution:** Endpoint ids are immutable and carry the model revision (name-v3). A change creates a new id and a new generation. Always set inference_id explicitly in templates. The delete tool refuses while the registry shows a reference.
- **Test:** On the 9.4.9 and 9.5.6 testbeds, create a semantic_text index, then delete the endpoint with and without force, and recreate it with another model under the same id. Record the indexing and query results. Pass means the registry guard blocks all three, and the unguarded control reproduces the failure.
- **Nearest existing:** RES-09 and CAP-09 cover agent model retirement; none cover an Elasticsearch inference endpoint lifecycle.

### MEM-17 · Flood-stage disk watermark makes indexes read-only mid-projection, and the projector or erasure sweeper marks the failure as done  `critical · reasoned`

- **Trigger:** The shared lab host reached 97 percent disk. At flood stage Elasticsearch puts a read-only-allow-delete block on the node's indexes while the projector and rebuild keep sending bulks and the erasure sweeper tries tombstone writes.
- **Why it bites:** If a block exception is treated as a poison or dead-letter outcome, the cursor passes events that were never indexed, leaving permanent holes. The Postgres watermark and the Elasticsearch content then disagree, and context assembly trusts the watermark. Erasure jobs that need writes stall while the deleted text stays readable.
- **Detect:** Treat cluster_block_exception and 429 as a distinct class. Export disk percentage and the index block flag. The context manifest compares log watermark with a per-index indexed-through value and shows a degraded flag.
- **Solution:** The cursor advances only for acked bulk items. A block parks the projector with backoff and a visible 'projection degraded' state. Reserve headroom for the erasure path and refuse rebuilds below a disk preflight (for example 2x the new index size). Page at 85 percent, below the watermarks.
- **Test:** On the lab node, set flood_stage to a tiny absolute free-space value to force the block, then run the projector under load. Pass means no cursor advance past unacked events, a degraded flag shown, and exact catch-up after release. The control is a projector that dead-letters, which must visibly lose events.
- **Nearest existing:** WSP-22 is worker disk and checkpoints; register C2 covers projection lag but not block-induced silent loss.

### MEM-18 · Snapshot restore or a rollback to an older index resurrects erased text, old labels and old ACLs  `critical · reasoned`

- **Trigger:** A snapshot taken before an erasure, a label change or a revoked access is restored after a disk loss, or an old blue generation is swapped back. The tombstone table has already been pruned because the sweeper finished.
- **Why it bites:** The erased text is searchable again and the read-path tombstone filter has nothing to match. Restored documents carry the labels and readers from snapshot time, so a revoked reader regains access. Repository retention also keeps the bytes for as long as the snapshots live.
- **Detect:** Every snapshot and generation records its log watermark. A restore job compares the watermark with the newest erasure, label and revocation events, and any gap is a blocking finding.
- **Solution:** Prefer rebuild from the log. If a restore is used, the index stays out of the serving alias until all erasure, label and access events after the snapshot watermark are replayed and a canary scan passes. Never prune a tombstone while any snapshot or retained generation older than it exists. Snapshot repository retention is no longer than the erasure SLA.
- **Test:** Snapshot, erase a subject, prune the tombstone, restore, and query the erased phrase. Without the replay gate the phrase must reappear (the control that fails). With the gate the alias swap is refused until replay, and the phrase is absent in all legs and in the vector search.
- **Nearest existing:** SEC-17, OPS-22 and OPS-10 cover crypto-shred and cross-store DR consistency, not an Elasticsearch snapshot restoring plaintext or old labels.

### MEM-19 · A schema or embedder change doubles every tenant's indexes at once and hits the shard limit mid-migration  `high · reasoned`

- **Trigger:** Index per tenant with a write alias per generation. A blue/green swap for one mapping change creates a second generation for every tenant, or for the first N tenants until cluster.max_shards_per_node or heap gives out.
- **Why it bites:** The migration fails part way through the fleet and leaves some tenants on the new version and some on the old, which breaks the no-mixed-version rule. Cluster-state updates for hundreds of mapping and alias changes also slow the whole cluster for every tenant.
- **Detect:** A capacity preflight computes shards now, shards after the swap and heap per shard against limits. Track cluster state size, pending tasks and shards per node, and alert at 70 percent of the limit.
- **Solution:** The migration is a state machine with a per-tenant version pointer and a wave size set by the preflight, with old generations deleted only after the wave verifies. Small tenants share a routed index class. Primary shards per tenant index are 1 by default.
- **Test:** Set a low cluster.max_shards_per_node on the lab node, create enough tenant indexes to sit near it, and run the migration. The unguarded control must fail part way and leave mixed versions. The preflighted run must refuse or run in waves, and a final check shows one version per tenant.
- **Nearest existing:** ORC-25 is a Temporal namespace per tenant; the register C11 chose index per tenant but never costed it.

### MEM-20 · Rebuild-mode index settings (refresh off, zero replicas) leak into the live index, and partial updates on unrefreshed semantic_text drop embeddings  `high · reasoned`

- **Trigger:** A rebuild sets refresh_interval -1 and replicas 0 for speed and the job crashes or is retried before restoring them, or it targets the alias and not the new generation. The same rebuild applies partial updates to documents not yet refreshed.
- **Why it bites:** The lab's observed 1 s visibility becomes never, and the tail-scan fallback hides it by covering the gap. Zero replicas on a live index loses data on a node loss. The lab's observed silent embedding drop on bulk partial updates of unrefreshed documents then happens during the rebuild itself.
- **Detect:** A watchdog compares live index settings with the declared template every minute. After a rebuild, compare the document count with the log count and run an exists query on the vector field for embedding presence, as a count audit.
- **Solution:** Rebuild settings apply only to the new, unaliased generation and are restored and verified in a finally step before the alias swap. Rebuild uses full-document index operations by id and never partial updates. The swap is blocked unless the counts and the embedding-presence audit match.
- **Test:** Kill the rebuild after the settings change and observe the watchdog alert and the stuck visibility. Separately replay the lab's partial-update case inside a rebuild and require the embedding audit to catch the drop. The control is a rebuild using full index operations, which must pass the audit.
- **Nearest existing:** Register C1 and C2 test rebuild exactness and lag; the lab's A8 finding is known, but the combination with rebuild-mode settings is not in the register.

### MEM-21 · A minor version upgrade changes default vector index options and filter heuristics, so new generations differ from old ones  `high · unverified`

- **Trigger:** Templates omit dense_vector index_options and kNN filter settings. The in-place 9.4.9 to 9.5.6 upgrade keeps old indexes as they were, but the next generation created under the new version gets the new default quantisation and heuristics.
- **Why it bites:** Blue/green swaps then change recall and latency with no mapping error. The lab already needs rescoring at pool size (0.48 at the whole pool), and exact legs are claimed identical across a rebuild, which holds only if the settings are pinned.
- **Detect:** Store the resolved mapping and settings hash per generation. A CI job creates a fresh index on each pinned version from the template and diffs the resolved mapping, including index_options, against a golden file.
- **Solution:** Declare type, similarity, element_type, index_options and any filter settings explicitly in the template. Record the server version in each generation's _meta. A recall canary on a fixed query set must stay within a pre-registered margin before a swap.
- **Test:** Create the same template-based index on the 9.4.9 and 9.5.6 testbeds and diff GET _mapping and a recall run on the pilot. If the defaults differ, the diff fails the gate. With explicit options the diff is empty and recall equal. The control is the version that differs.
- **Nearest existing:** Register F9 covers upgrade and mapping change; A6 measures one setting. Nothing records the default-drift risk.

### MEM-22 · Postgres microsecond timestamps truncated to millisecond Elasticsearch dates make as-of boundaries wrong  `medium · reasoned`

- **Trigger:** Facts have valid_from and valid_to written from timestamptz (microseconds) into date fields (milliseconds, or date_range). A fact ends and its successor starts within the same millisecond, or an as-of query uses now/d without a time_zone.
- **Why it bites:** In Elasticsearch both facts, or neither, are valid at that instant, while Postgres gives exactly one. The stale value can be injected as current, and a day boundary is rolled in UTC, not the tenant's zone.
- **Detect:** Differential test: for random as-of instants, compare the valid-fact set from Elasticsearch with a Postgres oracle. Count disagreements near boundaries.
- **Solution:** Use the log sequence number as the as-of coordinate and keep wall time for display only. If time is needed, store valid_from and valid_to as date_nanos or as integer microseconds, and always pass an explicit time_zone or UTC.
- **Test:** Insert two facts 300 microseconds apart and query as-of between them in both stores. Elasticsearch with millisecond dates must disagree with Postgres (the control that fails). Sequence-based or nanosecond storage must agree on 2,000 random instants.
- **Nearest existing:** Register A4 compares date_range with two fields but not precision loss; OPS-25 orders sessions by sequence, not facts.

### MEM-23 · Projector cursor over a sequence skips events that commit late behind a long transaction  `critical · reasoned`

- **Trigger:** The projector reads WHERE seq > cursor across sessions. A long transaction holds seq 100 uncommitted while seq 101 to 110 commit, the projector reads to 110 and advances, and 100 then commits.
- **Why it bites:** Event 100 is never projected, silently. The lab's gapless-writer result is per session and does not prove a global tail is safe. Context assembly and the freshness scan trust the watermark and miss the decision.
- **Detect:** Compare projected counts and per-event presence with the log over a sliding window. Alert on a transaction older than 30 s in pg_stat_activity and on any hole in the projected sequence.
- **Solution:** Read only up to a safe watermark from the transaction snapshot's xmin (events whose xid is below pg_snapshot_xmin), or use an outbox written in the same transaction and read by a commit-ordered mechanism. Set idle_in_transaction_session_timeout and a statement timeout on the writers.
- **Test:** Two connections: one inserts seq 100 and waits, another inserts 101 and commits, the projector runs, then 100 commits. The naive cursor must skip 100 (the control that fails). The xmin-bounded reader must project 100, in order, with no hole.
- **Nearest existing:** FEN-07 and OPS-25 cover the transactional outbox and per-session ordering; register C2 covers lag but not a skipped late commit.

### MEM-24 · Failover to an asynchronous replica loses acknowledged events and re-issues their sequence numbers with different content  `critical · reasoned`

- **Trigger:** The log on an async or semi-sync replica is promoted after the primary dies. Elasticsearch, the inboxes and Temporal cursors have already seen seq 1000 and the new primary then appends a different event as seq 1000.
- **Why it bites:** The projection holds an event that the log no longer has, and may already have shown it to an agent. Idempotent create-by-id then rejects the real event 1000 as a duplicate, so the two stores diverge permanently and the hash chain silently forks downstream.
- **Detect:** Every consumer stores (seq, event_hash) with its cursor and verifies prev_hash continuity. On a mismatch it halts with 'log regressed'. A promotion epoch in the chain header is checked on every read.
- **Solution:** Project and deliver only up to the replication-confirmed LSN. If acknowledged events can be lost, record a promotion epoch and run a reconcile that diffs consumers against the new log and rebuilds the affected range. The log uses synchronous commit with remote_apply where an acknowledgement is a user-visible promise.
- **Test:** Two Postgres instances with async replication: append, kill the primary before shipping, promote and append a different event. The naive projector keeps the ghost (the control that fails). The checking projector must halt and reconcile, and a final hash-chain verification passes.
- **Nearest existing:** FEN-06 covers lease and token loss on failover and FEN-14 a zombie writer fork; neither covers downstream copies of events the new primary does not have.

### MEM-25 · A stalled replication slot or failed WAL archive fills the log volume and stops appends while Elasticsearch stays healthy  `high · reasoned`

- **Trigger:** A logical slot for a change consumer, a standby, or a failing archive_command retains WAL while the host disk is already at 97 percent. Long transactions and unvacuumed bloat add to it.
- **Why it bites:** When the WAL volume fills, Postgres shuts down and the source of truth is unavailable although search still answers. Retained WAL and base backups can also hold bytes the design thought were erased (the lab found a key in WAL until a log switch).
- **Detect:** Monitor pg_replication_slots retained bytes, archive failures and the oldest transaction age, and alert well before the volume fills.
- **Solution:** Set max_slot_wal_keep_size, a separate WAL volume with a size budget, and a rule that projection consumers read from the table or outbox, not from a slot. Drop an unconsumed slot by runbook with a rebuild from the log. Keep no plaintext of erasable content in tables, so WAL and PITR hold only ciphertext.
- **Test:** On a small volume, create a slot, stop its consumer and keep appending. Without the cap the database must stop (the control that fails). With the cap the slot is invalidated, appends continue, and the consumer rebuilds. A scan of WAL segments for a planted marker finds only ciphertext.
- **Nearest existing:** OPS-09 and OPS-10 cover S3 and DR; the lab's own WAL key finding is a leak finding, not an availability one.

### MEM-26 · A connection pooler breaks session-scoped locks, tenant settings and LISTEN/NOTIFY wake-ups  `high · reasoned`

- **Trigger:** The append and projection paths use session-level advisory locks, a session-scoped tenant setting or LISTEN/NOTIFY, and a transaction-mode pooler such as PgBouncer is put in front of Postgres in production.
- **Why it bites:** A lock can stay held on a server connection used by another client, or never be held by the transaction that needs it. A non-local tenant setting leaks to the next client on that connection. Listeners lose notifications, so the kill and inbox wake-ups silently stop while polling timers hide it in lab-scale tests.
- **Detect:** Run the concurrency and isolation suite through the pooler. Alert if no NOTIFY is received within N seconds in a synthetic canary. Query for non-empty session settings on a freshly checked-out connection.
- **Solution:** Use only transaction-scoped primitives (pg_advisory_xact_lock, SET LOCAL or set_config with is_local true). Listeners use a direct connection, bypassing the pooler, and every wake-up has a polling backstop that is the correctness path. Reset all session state on checkout.
- **Test:** Run the 16-writer gapless test and a two-tenant leak probe directly and through PgBouncer in transaction mode. The session-lock variant must fail through the pooler (the control that fails), and the transaction-scoped variant must be gapless and leak-free in both.
- **Nearest existing:** FEN-09 forbids advisory locks for leases, GAP2-14 uses transaction-local set_config and ORC-05 uses LISTEN/NOTIFY; none treats the pooler as the thing that breaks them.

### MEM-27 · Elasticsearch- and Kibana-side system stores hold unregistered copies of queries, results and conversations that erasure and labels never reach  `high · reasoned`

- **Trigger:** Async search results, Agent Builder conversation history and saved objects in Kibana system indexes, slow-query and audit logs, task results, the inference and ML indexes and snapshots all store text or query content outside the registered projections.
- **Why it bites:** The lab already saw this class once, when an extraction cache survived erasure until it was registered. Here a tenant's erased or restricted content stays in stores that no policy decision point sees, and retention is set by product defaults.
- **Detect:** At startup and daily, list every index and data stream with expand_wildcards=all and fail on any not in the erasure registry with an owner and a strategy. A canary phrase planted through the full flow is searched for in every index, log file and snapshot after erasure.
- **Solution:** A single registry of every store that can hold content, as code, with the erase, label and retention strategy for each. Turn off or bound the product stores (async search keep_alive, slowlog source, audit content) and give Agent Builder its own cluster or tenant boundary. A new unregistered index fails the deploy check.
- **Test:** Plant a unique marker through search, an async search and an Agent Builder conversation, erase the subject, then grep all indexes, node logs and a snapshot. Before registration the marker must be found (the control that fails). After registration it must be absent everywhere, and the unregistered-index alarm fires when a new system index is added.
- **Nearest existing:** The 40 cases cover forgotten copies in general (backups, caches, vectors) but not Elasticsearch and Kibana system stores; register C3 and the extraction-cache finding are the closest.

## Lens: proof-and-eval

### MEM-28 · Gold-correlated metadata leaks the label (ids, file names, counts, order, timing, length)  `critical · observed_in_lab`

- **Trigger:** Every dataset gold session id began with 'answer_', so prompts told the model the answer. The same class covers file names, shard or insert order, token length, and timing that differ between gold and distractor items or between arms.
- **Why it bites:** Retrieval and reader scores rise for reasons unrelated to memory quality. Catching the one leaking field you noticed does not prove the others are clean.
- **Detect:** Label-blind probe: train a trivial classifier (and shuffle-order, length-only and id-only baselines) to predict gold from non-content fields of the exact prompt and index payload; any accuracy above chance is a leak.
- **Solution:** Opaque ids from a keyed hash, shuffled order, and length-matched formatting, applied by the dataset loader. A pre-run gate fails the run if the metadata-only probe beats chance by more than its pre-set bound.
- **Test:** Run the probe on the old 'answer_' build and require it to flag (control that fails). Then run it on the opaque-id build and require chance accuracy, with the probe's own power shown on a planted 1-bit leak.
- **Nearest existing:** No match. OPS-31 and OPS-24 cover pre-registration and chaos, not label leaks. Nearest are the lab's opaque-id fix and the 40 cases on read-path side channels, which concern tenant data, not evaluation labels.

### MEM-29 · Benchmark has no headroom at its size, so a null or a win means nothing  `high · observed_in_lab`

- **Trigger:** A reader given the full 165k-token history scored 9 of 10, equal to a reader given only the evidence. A small benchmark can still be used to claim that the memory design 'works' or 'does not hurt'.
- **Why it bites:** If the no-memory or full-context arm already reaches the ceiling, any memory arm ties it and the comparison is uninformative. Gold errors and judge noise cap the ceiling further.
- **Detect:** Run the arm ladder (no memory, oracle evidence, full history, system) and compute the gap between floor and oracle against the paired noise of n items; if the gap is under the noise, the benchmark cannot discriminate.
- **Solution:** A headroom gate computed by the eval harness: it refuses to publish a comparative claim unless the oracle-minus-floor gap exceeds its pre-registered multiple of paired standard error. Audit a sample of failures for gold errors to estimate the true ceiling.
- **Test:** Feed the gate a synthetic benchmark where floor equals oracle and require refusal. Feed one with a known large gap and require acceptance, then rerun the 10-question pilot and require 'no headroom'.
- **Nearest existing:** Register B1 asks the question once; there is no mechanism that blocks later claims. Not in the 253 or the 40.

### MEM-30 · A lab result at small pool size is quoted as the capability at the deployed pool size  `high · observed_in_lab`

- **Trigger:** Top-10 recall was 0.97 on one conversation, 0.86 with +500 sessions, 0.68 with +3,900 and 0.48 against the whole pool. Any headline that omits the pool size, or that uses a pool smaller than the tenant's real history, inherits the 0.97.
- **Why it bites:** Recall falls steeply with the number of distractors, and approximate vector search needs rescoring only at large pool sizes, so small-pool tests hide the failure region and its fix.
- **Detect:** Every reported metric must carry pool size, index size, vector count and filter selectivity. A claim is rejected when the eval pool is under a set fraction of the stated target pool.
- **Solution:** Report a curve over at least 3 pool sizes with the target size included; the claim is valid only at the largest tested size, and the curve's slope is reported. Synthetic padding to reach the target size must pass the distractor-purity check below.
- **Test:** Replay the pilot at each pool size and require the claim checker to reject the 0.97 headline at 0.48-scale. Add a control where a fake scale-free retriever is flat across sizes and require the checker to accept it.
- **Nearest existing:** Register B9 and B12 plan the growth curve. Nothing blocks a headline stated at a size smaller than deployment, and WSP-27 only covers GPU tuples.

### MEM-31 · Strata chosen from the system's own outputs inflate the recovery effect  `high · reasoned`

- **Trigger:** The 24 low-overlap questions were picked because their evidence ranked worse than 100 on both retrieval legs, then used as the test set for the analyst tier (H1) and for write-time cue keys (B3, B8).
- **Why it bites:** Selecting items by the baseline's failure guarantees regression to the mean: any re-run with a different seed, order or retriever would appear to improve on them. The 'recovery' can be pure noise.
- **Detect:** Re-draw the baseline with a different seed, batch order or model, and see how many of the 24 stay 'hard'. If many drop out with no new component, selection on outcome is the cause.
- **Solution:** Define strata only from input-side properties (term overlap, session age, question type) computed before any system run. If an outcome-defined set is needed, build it on a split disjoint from the one used to score the change.
- **Test:** Control: apply a null change (reshuffled rerank ties) to the outcome-selected stratum and require the harness to show an apparent gain. Then require the pre-defined stratum to show none.
- **Nearest existing:** Register B3 sizes the stratum and H1 uses it, but neither addresses selection on outcome. OPS-31 covers pre-registration of metrics, not strata.

### MEM-32 · Check passes without the property ever being exercised (no witness that the code path ran)  `critical · observed_in_lab`

- **Trigger:** An erased subject's rows survived in an extraction cache that was not registered with the erasure job; the erasure test checked registered stores and passed. Similar: a control that cannot fail because the planted item is never in the checked scope.
- **Why it bites:** A zero-violation result is identical whether the system is correct or the check saw nothing. This repeated across three streams.
- **Detect:** Each check must emit a witness count (items examined, stores enumerated, planted canaries found by the checker before the action). A pass with witness count zero, or with fewer stores than the enumerated storage inventory, is a vacuous pass.
- **Solution:** The checker enumerates targets from the system's own inventory (all caches, indexes, queues, tables via catalog introspection), not from a hand list, and fails if any target is unregistered or if the planted canary is not seen pre-action.
- **Test:** Add a new cache without registering it and require the erasure check to fail. Then require the checker to fail again when the canary is planted outside its scope, proving the control can fail.
- **Nearest existing:** EFX-23 and OPS-02 cover vacuous agent-written code tests and judge edits, and the lab already has the cache incident. This asks for a mechanism that applies to every experiment, not only code tests, and none of the 40 cases require witness counts.

### MEM-33 · Concurrency claim from a run where operations never overlapped  `critical · observed_in_lab`

- **Trigger:** 'Exactly once' or 'gapless' reported from a test that is single-threaded, or whose 16 writers each finish before the next starts because operations are fast. The inbox double delivery was found only under concurrent turns.
- **Why it bites:** Fast operations on one host serialise by accident, so races, lost updates and duplicate delivery never occur in the test and appear in production at higher load or on another node.
- **Detect:** Instrument the test to record start and end timestamps per operation and compute an overlap witness (the number of pairs in flight together, at the contended step). Zero overlap means the claim is unproven.
- **Solution:** Barrier-synchronised start, fault-injected delays at the contended step, and a required minimum overlap count. The same suite must run against a deliberately broken variant (lock removed) and fail there.
- **Test:** Run the suite on the correct build and the lock-removed build; require pass and fail respectively, with overlap above the threshold in both. Repeat with 1 writer and require the harness to report 'unproven'.
- **Nearest existing:** OPS-24 pre-registers chaos criteria and C7 plans concurrent writers; neither requires an overlap witness or a broken-variant control. The inbox exactly-once check (H4) is exactly this.

### MEM-34 · Caching and batch order collapse replications into one draw  `high · observed_in_lab`

- **Trigger:** The extraction cache and result caches return identical outputs on repeat, so 'run it 5 times' is one sample. Supersession links were also unstable across batch order (9 of 25 links), yet earlier runs used a single order.
- **Why it bites:** Variance is hidden, a single lucky order or seed is reported as the result, and an apparent 'deterministic replay' is just a cache hit.
- **Detect:** Count cache hits per run. Rerun with the cache bypassed and with k random permutations of batch order and seed; report the spread, and compare the cached rerun to an uncached one.
- **Solution:** Every eval run records cache hit rate and uses a per-replication cache salt. A verdict requires k at least 5 permutations and is stated as the worst-case or lower bound, not the mean of one order.
- **Test:** Control: feed a stub extractor that depends on batch order and require the harness to flag it. Then confirm the real extractor's 9 of 25 instability reproduces under permutation, with replicated runs showing hit rate zero.
- **Nearest existing:** Register C1 mentions cached versus uncached replay and I1 notes instability, but nothing makes replication independence a gate. EFX-28 covers flaky tests, not model-stage eval variance.

### MEM-35 · Eval operating point differs from the deployed one (batch size, context length, concurrency, cache warmth)  `high · observed_in_lab`

- **Trigger:** Fact extraction lost a third of facts at 4 sessions per call and two thirds at 16, yet the eval, the cost model and production each pick a different batch size. One long session was cheaper than 20 headless calls only because of the prompt cache and a warm start.
- **Why it bites:** Quality and cost are functions of the operating point. A number measured at 1 session per call says nothing about the batch size that production will use to save money.
- **Detect:** Record the batch size, prompt length, concurrency, cache state and adapter in every result. Diff that tuple against the deployment config before accepting the result as evidence.
- **Solution:** A result carries its operating-point tuple and is valid only within a declared tolerance of the deployment tuple. Quality and cost are measured on the same tuple, and the chosen production batch size must be the one with the tested quality.
- **Test:** Run extraction at 1, 4 and 16 per call and require the checker to reject the 1-per-call quality number as evidence for a 16-per-call deployment. A control where quality is flat across batch sizes must be accepted.
- **Nearest existing:** Register D5 and C6 note cost and cache effects; CAP-06 covers cache cost by pause length. None binds quality evidence to batch size or operating point.

### MEM-36 · Harness instrumentation reaches the model input or output, so the measured prompt is not the specified prompt  `high · observed_in_lab`

- **Trigger:** Each headless call carried about 10.5k tokens of harness overhead and created a session log directory, and the owner's hooks leaked text into headless outputs. Judges and readers saw harness text the spec never listed.
- **Why it bites:** Results mix the intended prompt with hidden instructions, memory files and hook output. A stored 'clean' output may contain the owner's own harness rules, and a replacement adapter will not reproduce them.
- **Detect:** Capture the actual wire request at the gateway (not the adapter's intended prompt) and diff it by hash against the specified prompt; scan outputs for known harness marker strings planted in a canary hook.
- **Solution:** Run measurement calls from a clean config dir with no hooks, memory files or CLAUDE.md, with the gateway recording the final request. A gate fails the run if wire-prompt tokens exceed spec tokens by more than a stated constant.
- **Test:** Plant a canary string in a hook and require the gate to see it in the dirty config and not in the clean one. Compare token counts for the identical prompt through both.
- **Nearest existing:** CAP-03 and SEC-26 cover gateway protocol and background traffic; RES-17 covers memory parity. None cover measuring what the model actually received for evaluation validity.

### MEM-37 · Denominator drift: questions silently dropped by guards, timeouts and skips  `high · reasoned`

- **Trigger:** The send-time guard blocks some questions, the reranker takes 26 s per question on CPU and is cut off by timeouts, and analyst arms have fixed tool budgets that end early. Items that fail to run are excluded from the average.
- **Why it bites:** The hard cases are exactly the ones that time out or trip guards, so the reported accuracy is over an easier subset. An arm with more skips looks better.
- **Detect:** Reconcile at the item level: planned item set versus attempted versus scored. Any item without a terminal scored outcome is listed with its reason, and arm item sets are compared.
- **Solution:** Intent-to-evaluate accounting: the denominator is fixed at pre-registration, and a timeout, guard block or budget exhaustion scores as a failure (or the arm is reported as incomplete). Arm comparisons use only items scored in all arms, with the dropped list published.
- **Test:** Inject a guard that blocks the 10 hardest questions and require the harness to reject the headline accuracy. Control: a run with zero drops must pass with identical numerator and denominator.
- **Nearest existing:** Register B-section and H7 mention budgets and skip rules, but nothing reconciles scored items against the planned set. OPS-03 covers scope narrowing by agents, not evaluation drop-outs.

### MEM-38 · Distractors and splits contaminated by duplicate or answer-bearing content  `high · reasoned`

- **Trigger:** The pool grows by adding sessions from other questions' haystacks, which may repeat the same user, near-duplicate sessions or the answer text. Pilot questions are used to tune prompts and also to report 'held-out' results (16 of 16 links).
- **Why it bites:** A distractor that contains the evidence makes the 'miss' counted against retrieval wrong, and a dev item reported as test overstates quality. Deduping by question id misses repeated sessions.
- **Detect:** Hash normalised session text, find near-duplicates with MinHash, and scan all pool sessions for the gold answer string and its aliases. Check overlap between tuning items and reported items by content hash.
- **Solution:** Build the pool and splits in code that dedupes on content hash across all splits, drops or flags answer-bearing distractors, and refuses to score an item that appeared in any tuning log.
- **Test:** Plant a copy of a gold session as a distractor and require the builder to flag it. Plant a pilot question in the held-out list and require the scorer to refuse it.
- **Nearest existing:** Register B-section notes the 18,464 distinct sessions but has no purity or split-leak check. EFX-24 and SEC-15 cover reviewer packet independence, not dataset contamination.

### MEM-39 · Concurrent experiments on one shared host contaminate each other's measurements  `medium · reasoned`

- **Trigger:** E1 to E11 ran at the same time on one Elasticsearch node on a shared host: visibility delay (about 1 s), the retention-lease sync (about 30 s) before merge, 420 appends per second and CPU rerank time all depend on merges, refreshes and load from the other runs.
- **Why it bites:** A latency, timing or throughput number partly measures a neighbour experiment, so it is not reproducible and cannot be extrapolated to a dedicated production node. Timing-based leak claims can flip.
- **Detect:** Log node CPU, merge activity, indexing rate and the experiments active during each timed measurement; rerun the timed claim in isolation and compare.
- **Solution:** Time-sensitive experiments get an exclusive window or node, with a recorded quiescence check beforehand (no merges, no other indexing). Reports state the observed range over isolated reruns and mark non-isolated numbers as indicative.
- **Test:** Run the visibility-delay and merge-persistence tests alone and while a bulk indexer runs; require the harness to record both and the claim checker to reject the loaded value as the stated figure.
- **Nearest existing:** Register A1 and A10 run simultaneously but do not address interference. OPS-24 and EFX-28 cover chaos and flaky tests, not measurement isolation.

### MEM-40 · Evidence expires when the environment envelope changes (licence tier, trial expiry, version, scale, adapter, model alias)  `high · reasoned`

- **Trigger:** Lab results come from Elasticsearch 9.5.6 on a trial or Basic licence, one node, 20,000 documents, and a subscription-CLI adapter. A trial lapse removes Enterprise features such as semantic_text, rerank and DLS, and production uses different inference adapters, a customer's tier, multiple nodes and regions.
- **Why it bites:** A REALLY_TESTED entry is attached to a commit but silently becomes false when the licence, version, document count or adapter differs. Results from the dev adapter are cited for production behaviour.
- **Detect:** Every evidence record stores an environment fingerprint (ES version and licence tier and expiry, node count, data size, adapter id and model id, CLI version). Compare it with the deployment's fingerprint at claim time.
- **Solution:** Evidence is valid only within its recorded fingerprint envelope; a claim at a different tier, scale or adapter shows as UNTESTED_THERE until retested. Licence expiry dates are tracked, and a scheduled canary fails when the fingerprint drifts.
- **Test:** Expire the trial licence on the testbed and require the status view to downgrade the semantic_text and DLS evidence automatically. Control: an unchanged fingerprint keeps the entry valid.
- **Nearest existing:** EFX-22 and OPS-11 key staleness on tree, lockfile and flag hashes, WSP-27 on GPU tuple, and CAP-08 on model aliases. None cover licence tier, trial expiry, data scale, or dev-versus-production adapter as invalidators.

### MEM-41 · Reported figures with no raw per-item artifact cannot be recomputed or audited  `high · observed_in_lab`

- **Trigger:** Owner-reported numbers (for example 'gapless at 420 per second', '9 of 10', '2,000 scenarios exact') were reviewed from prose, and three streams' passes were later overturned. Fixes are 'claimed' without saved outputs.
- **Why it bites:** An aggregate cannot be checked for dropped items, wrong denominators, leaks or arithmetic errors. A reviewer can only trust or distrust, and one stale run can be pasted into a later report.
- **Detect:** For each figure, require the result record to resolve to a content-addressed raw file with one row per item (id, input hash, output, score, adapter, time) and a command. Recompute the figure with an independent script and diff.
- **Solution:** A report linter refuses any number without an artifact id, and the eval harness writes per-item rows with hashes before computing summaries. An independent recompute job runs on every cited figure and the summary shows both.
- **Test:** Alter one raw row so the aggregate no longer matches and require the linter to fail. Then drop the artifact reference from a figure and require the report to be rejected.
- **Nearest existing:** OPS-12 and OPS-31 require evidence-view status and ledger-computed metrics for platform operation; the lab's own streams lacked this, and no catalogue entry demands per-item raw rows and independent recompute for experiment figures.

