# Write path: design notes and open decisions (goodqa#16096)

Evidence lives in exp/THRESHOLDS.md (pre-registered rules) and the content-addressed results; this file records the
decisions, the deployment requirements that follow from them, and what is still OPEN. An OPEN item is not implemented.

## OPEN decisions

### OPEN-1. Elasticsearch index layout per tenant (item 5, 4960f50 round)
- Measured cost today: one index per projection family per tenant, 1 primary shard, 0 replicas in the lab: 2 indices and
  2 shards per tenant (x (1 + replicas) in production). A node's default cluster.max_shards_per_node is 1,000, so a
  node holds at most ~500 tenants before replicas, system indices and generations (row 20 adds one index per family per
  generation during a rebuild).
- Proposal: keep 1 shard per index. Small tenants could share POOLED family indices (one shared index per family, a
  tenant_id keyword on every document, per-tenant read / write / erase aliases with a tenant filter and routing).
- Effect on the projector fence: unchanged in kind; the alias compare-and-set is per tenant alias, aliases are objects of
  their own even when they point at a shared index. require_alias still forces writes through an alias, but a filtered
  write alias does not stop a write for another tenant's id; with pooling, the per-role API key (D6) must restrict the
  projector to its tenant's aliases.
- Effect on erasure: expunge (retention-lease wait + only_expunge_deletes merge) becomes pool-wide: one wait and one
  merge serve every tenant of the pool (cheaper per erasure, but the merge rewrites other tenants' segments).
- Cost, the reason this is OPEN: BM25 term statistics (document frequency, average field length) are per shard, so in a
  pooled index they mix tenants. Ranking for tenant A depends on tenant B's documents, and document-frequency
  differences leak, weakly but measurably, whether a term occurs in other tenants' documents (a cross-tenant side
  channel). dfs_query_then_fetch does not separate them. Mitigation if pooling is chosen: dedicated indices for every
  tenant that requires isolated statistics, pooling only for tenants that accept the leak in writing.
- Decision needed from: the user (privacy and cost). Not implemented. run_all now deletes indices between experiments
  (exp/cleanup.py prints the index and shard count before deleting).

## Decisions with deployment requirements

- Cross-tenant projection delay (amendment 8a): the projector fence is the cluster's oldest running xid, so any long
  transaction delays every tenant. Required: statement_timeout 30 s and idle_in_transaction_session_timeout 5 s on every
  role, AND transaction_timeout (PostgreSQL >= 17) or the transaction-age watchdog (wp/xact_watchdog.py, 30 s), an alert
  on the age of the oldest xid / xmin, no long superuser sessions, prepared transactions monitored; or one cluster per
  tenant. Superuser sessions, prepared transactions and replication slots are NOT bounded by the watchdog.
- Physical purge (amendment 9): "logically erased" is reported at once; "done" waits for the physical purge. The store
  sets a physical deadline (logical_at + 300 s) when the job becomes logically erased. The deadline is not a completion
  guarantee: past it the job stays open, is reported overdue (last_error_type physical_purge_overdue), and the sweep
  keeps trying until the purge completes. Nothing bounds how long that takes. The ES floor is the retention-lease sync
  (about 30 s). With a real KMS the key's physical deletion is the KMS's own record and its pending-deletion window,
  which may be days: the receipt must then say "logically erased, key deletion scheduled until <date>".
  The erasure mark itself has no time bound under concurrent writes either (see "Erasure timing under concurrent
  writes" below).
- Identifier rule (amendment 11, now rule r3 of "Redesign after review C"): ASSUMPTION, not a user decision:
  single-token identifiers without a digit need >= 6 characters; identifiers made only of common words, or of
  look-alikes of them, are refused. Decision R1 (decided by the coordinator on the user's delegation, 2026-11-17;
  "Decisions after review E" below): there is no span redaction; an event that
  mentions an erased subject is removed whole, whoever wrote it, and the receipt lists those of other speakers
  (`other_speakers_removed`).
- Service separation (D2): the verifier, the key service and the application must run as separate service identities,
  each able to read only its own credential. The lab runs them as separate OS processes with separate credential files
  on one host and one OS user (exp/d2_verifier_process.py); separate OS users or hosts are a deployment requirement,
  not proven here.
- Subject-row locks (amendment 17, exp/lock_order.py): the order is unchanged (lease, subjects, identifier epochs,
  mentioned subjects, chain, decision head). Only wp_hold_place, wp_hold_release and wp_erasure_mark take a subject row
  FOR UPDATE, each one row in its own statement; every other path (appends, cascade batches, wp_hold_check,
  wp_identifier_declare) takes FOR SHARE and never upgrades it. Until 5ab1e2e the hold check and the declare took FOR
  UPDATE, which deadlocked two erasures of subjects who mention each other, and a declare against an append that
  mentions the subject. Step 3 (decision core in events_guard) must keep to this order.
  Evidence scope: the lock_order controls restore only the pre-fix lock statements, not the whole earlier system; the
  test is not a proof of deadlock freedom, and overlapping cascades that share event, tombstone or fact rows are
  outside its MM fixture. FOR SHARE also means an erasure mark can wait without bound (next bullet).
- Erasure timing under concurrent writes (amendment 17c): PostgreSQL grants a new FOR SHARE ahead of a waiting FOR
  UPDATE, so a sustained overlap of writes touching a subject can delay that subject's erasure mark without bound. Each
  attempt is cut by statement_timeout (30 s); the job stays open and the sweep retries it. Receipts say so ("timing");
  nothing in this lab claims bounded-time erasure under concurrent appends. A deployment that needs a bound has to pause
  writes for the subject (for example refuse appends naming a subject with an open erasure job) and prove it.
- Plan stability of the erasure path (amendment 18, exp/plan_stability.py): the cascade deletes and the residue probe
  input_event_ids one event at a time through its GIN index. A probe that can also see the tenant predicate is turned
  into a scan of the whole tenant by any low tenant estimate (plpgsql's generic plan from the 6th batch, stale
  statistics, or a SQL function planned with unknown parameters): 1,410 s for 100,000 events in the voided 1f059f4
  run. Since 83f7000 each probe sits behind a fence (lateral subquery with OFFSET 0, tenant filtered outside; event ids
  are uuids) and the two functions run with enable_seqscan off, so the only path inside the fence is the GIN index;
  the residue's untombstoned count is an EXCEPT. Any new per-event probe must follow the same shape and be added to
  plan_stability. Known residuals: under stale statistics the outer primary-key delete can read the tenant once per
  batch (O(tenant) per batch, not per event); GIN fastupdate pending lists make every probe scan the pending entries
  until vacuum flushes them (about 2 to 3 ms per event early in a large cascade, in every version since 7e8f28f).
  Evidence scope (review-2 N3): the stamped run at 62c6be5 had no facts in its fixture, so it shows plan stability for
  the analyst-job delete and the residue only; amendment 22 adds facts at both sizes, facts gates and a facts-only
  control, and plan_stability runs again under it. Plan stability bounds the work per event of the cascade; it does not
  bound when an erasure starts or finishes (see "Erasure timing under concurrent writes" and "Physical purge").
- Projector start race (amendment 19, exp/projector_start_race.py):
  - Fixed in 5d26746: a projector that loses the start-up race to a forced takeover now drops the lease and returns -1
    from step(). Until then, the StaleEpoch (alias compare-and-set) or StaleWrite (cursor insert) raised inside start()
    escaped step() and run() and killed the losing process.
  - Safety was never affected: no stale write reached Elasticsearch.
- Projector under Elasticsearch failures (review-2 N1, goodqa#16302, amendment 21, exp/projector_es_fault.py):
  - Until the N1 fix, families.fence_es turned every ESError on the alias request into StaleEpoch, and _bulk did the
    same for every refused item other than 409. step() then forgot its lease while the lease row stayed live until its
    TTL, the next acquires returned None, and run() returned True as if idle with work pending. A bulk request that
    raised, or a dropped connection, propagated but left the lease live for its TTL.
  - Now a failure is a lost epoch only when that is confirmed: a write alias newer than the projector's epoch is
    visible after the failure, or the lease no longer renews (LeaseLost). Those still return -1 from step().
  - Everything else propagates out of step() and run() as ProjectionError (or as the original error). Examples: a
    failed alias request with the aliases unchanged, refused items while the lease is still ours, a bulk request
    error, a dropped connection, a failed re-read of the aliases.
  - A failed CAS whose aliases changed with no newer epoch (a lower epoch moved them) is observed again and retried,
    at most 3 requests. That branch is covered only by the offline test tests/test_fence_es.py.
  - On every error exit, step() releases its own lease (wp_lease_release matches epoch and holder, so it is a no-op
    once another projector holds a newer epoch). The release is best-effort and never replaces the original error, so
    a fresh projector can take over at once rather than after the TTL.
  - run() returning True means there is nothing for this projector to do: the work is projected, or another live
    projector holds the lease.
  - A deployment's projector process must treat a raised error as a failure to report and retry, not as success.
- Secret scanning (amendment 15): wp/secrets.py is a list of known families and encodings, not a general detector.
  - In the stamped run at 62c6be5 (amendment 15b; results/single-runs/heldout_secrets.930e32ec3d99a161.json), it caught
    483/1,169 (H1) and 150/750 (H2) in-scope held-out secrets, and blocked 37 of 1,200 negatives. Row 19's 880/880
    holds only on its author's own corpus.
  - Seven H1 families were never caught. HTML entities and URL double-encoding are never decoded.
  - The scanner's own docstring says removing whitespace "catches a key split across lines or spaces". The held-out
    variants space_split and hyphen_split were each caught 0/50. The docstring is left as it is, because any edit to
    wp/secrets.py changes the frozen blob (amendment 15).
  - A deployment must not treat a clean scan as proof that no secret is present.
  - The scanner was not edited after the held-out run. Any later edit needs a new held-out set.
- Postgres settings are part of the result: the earlier lab's numbers depended on wal_recycle=off, wal_keep_size=0,
  archive_mode=off and max_connections=200, which were not in the repo until 8c7d841 (host/bootstrap.py sets them).
  Result fingerprints before cf8f08e record only the server version, not these effective settings, so those results
  cannot be reproduced from their fingerprints alone. From cf8f08e every fingerprint records the effective values of
  a named list of settings (exp/common.PG_SETTINGS).

## Invariant: the write-alias generation (review B2, D6)

The Elasticsearch fence rests on alias-name scoping plus the alias compare-and-set held by the key broker, so the
alias state itself must be safe under every interleaving of family creation, takeover CAS and key minting:

- A family index is born with its generation alias `-w0` in the SAME create request (is_write_index=false; no lease
  ever holds epoch 0; the broker refuses a key for epoch < 1). A family therefore never exists without a `-w` alias.
- Only the CAS (`families.fence_es`) installs a `-w<E>` alias, and only in ONE atomic `_aliases` request that also
  removes, with `must_exist=true`, every `-w` alias it observed. It never adds to a family with no `-w` alias
  (ProjectionError). ES rejects such a request as a whole when any observed alias is gone (probe P3, 9.5.6).
- Consequence: a request that observed generation g and stalls cannot install its alias after anyone else's CAS moved
  the family past g; the re-read then sees the newer epoch (StaleEpoch) or the same epoch (no action). At most one
  `-w` alias exists per family, and it is the newest epoch whose CAS completed. A key minted for an epoch whose alias
  is later removed can write nowhere; revocation stays hygiene.
- Callers: the broker (writer_key: lease check, CAS, lease re-check, mint) and the non-broker projector start use
  fence_es; erasure's placeholders (ensure=False in broker mode) and eraser_key only create families (with `-w0`) and
  never touch `-w` aliases. tests/test_alias_generation.py enumerates the interleavings (pause before create, before
  the CAS, before the re-check; against a retry, one, two and three later CAS requests; invalidation down).
- Not covered: families created before this rule have no `-w0`; if one has no `-w` alias at all, the CAS refuses it
  (lab data is recreated by setup; a deployment would add `-w0` in a maintenance window with writers stopped).

## Design: D3 identifiers, reviewer role and span redaction (ASSUMPTION under decision 10)

Status: design written before tests and code (2026-11-17), per main's D3 instruction. Decision 10 and P3 are
ASSUMPTIONS (main's decisions), not the user's words. Code and unit tests only; no stamped runs.

### Principle
The previous D3 put the identifier rule in the application (`mentions.declare`) while the app role could call
`wp_identifier_declare` directly: a check that could be skipped. This design moves WHO may make an identifier active
into the database, so for the app role an unreviewed or malformed identifier has no path to `subject_identifiers`
(the only table the matcher, the rescan and the cascade read). Trust boundary, stated plainly: the database enforces
who proposes, who approves, that they differ, and that the approved identifier is exactly the one proposed (HMAC
binding). The reviewer process enforces the rule on the plaintext. The store never sees plaintext, so it cannot judge
"malformed" itself.

Redaction follows the same idea: whole removal (tombstone) stays the default for every mentioning event, and span
redaction is an optional step that must succeed completely before the cascade reaches the event; anything short of
that falls through to the existing tombstone path, and the store, not the app, lists those fallbacks.

### Mechanism
1. `identifier_proposals` (append-only except the state transition): tenant, proposal id, subject, `ident_hmac`,
   `ident_enc` (the normalised identifier sealed under the SUBJECT key, AAD `proposal:<id>`, so destroying the subject
   key shreds it), `rule_version`, `proposed_by` (= session_user, set by the store), state proposed|approved|rejected,
   `reviewed_by`, reason.
2. `wp_identifier_propose` (wp_app): inserts a proposal. `wp_identifier_declare` is DROPPED; wp_app gets no function
   or table grant that writes `subject_identifiers`.
3. `wp_identifier_approve(proposal, hmac, rule_version)` executable only by members of the new group role
   `wp_reviewer` (one login per reviewer principal): refuses session_user = proposed_by (WPS30), a non-proposed state,
   an HMAC that differs from the proposal's (the reviewer recomputes it from the plaintext it reviewed; WPS31), a
   rule_version other than the store's current one (WPS32); then does what declare did, with the same lock order
   (subject FOR SHARE, identifier_epochs FOR UPDATE, WPS20 ambiguity, WPS03 erased).
4. Rule r2 (`wp/ident.py`), run by the reviewer and as early feedback by the app: NFKC, casefold, NFKC again (an
   approximation of toNFKC_Casefold); default-ignorable and format characters (Cf, variation selectors) removed;
   tokens are runs of letters, marks and digits. Refused: empty, more than 4 tokens, only stop words, a single token
   under 6 characters without a digit, a token mixing scripts, any unassigned code point, any token in a script
   written without spaces (Han, Hiragana, Katakana, Thai, Lao, Khmer, Myanmar: whole-token matching cannot find them,
   so declaring them would promise erasure that cannot happen). Script is taken from the Unicode character name
   (unicodedata has no Script property): a labelled HEURISTIC, not UTS 39 conformance.
5. Matcher m2 (`wp/mentions.py`): the same normalisation applied per grapheme-like cluster (base plus combining
   marks) with an offset map back to the ORIGINAL text (UAX 15: normalisation changes length, so offsets on the
   normalised text do not map back). Windows of 1..4 tokens are HMAC'd as before under the label
   `ident:m2:ucd<version>:`, so identifiers declared under one Unicode version are never compared with text normalised
   under another (the Mac has UCD 13.0, t2-dev 15.0).
6. `event_redactions` (append-only; `redacted_enc` may only be nulled by the owner, like `events.content_enc`): tenant,
   event, n (1, 2, ... per event), job, `base_digest` (the digest of the text it was computed from), `redacted_enc`
   (under the SPEAKER key, AAD `redaction:<event>:<n>`), `redacted_digest`, `spans` (start/end code-point offsets in
   the base text, ids and integers only), `prev_hash` (events.hash for n = 1, else the previous redaction's hash),
   `hash` (keyed with the tenant chain key like the event chain).
7. `wp_erasure_redact(job, event, n, base_digest, redacted_enc, spans)` (wp_app, during the cascade, before the event's
   batch): refuses an event of the subject itself, an event under another subject's hold, empty or overlapping or
   unordered spans, a base_digest that is not the event's current text (compare-and-set; a concurrent or repeated
   redaction fails cleanly), and a second redaction by the same job. In one transaction it inserts the row, nulls
   `events.content_enc` (and the previous redaction's `redacted_enc`) and records the event in `erasure_redacted`.
8. The cascade is unchanged in what it removes: every mentioning event is still TOMBSTONED (ES placeholder, derived
   facts and jobs deleted, D4 strict verifier covers it), UNLESS it is in `erasure_redacted` for this job, in which case
   its ES document is replaced by the placeholder too (ES gets no redacted text in this version: over-removal from the
   derived index, stated in the receipt) and its facts are deleted. Only the Postgres record keeps the redacted text.
   The residue function counts a mentioning event with present content and no tombstone as residue whether or not it
   was redacted, so a missing redaction can only fall back, never leak.
9. Receipt: `other_speakers_redacted` (event ids, span counts), `fallback_removed` (event ids with the store's reason:
   no redaction row for this job), matcher m2 and its not-covered list.
10. Chain verification (`log.verify_chain`): content NULL is accepted only if the event has a tombstone or a redaction
    row; the redaction chain of each event is walked (prev_hash links, keyed hash, redacted_digest of present text).

### Scenarios and how each is ruled out
| # | Scenario | Ruled out by |
|---|---|---|
| S1 | identifier too short or a common word ("alex", "the team") | rule r2 in the reviewer; for the app role, no write path to subject_identifiers at all (grants; live test) |
| S2 | identifier is a substring of another word ("Kim" in "Kimball") | whole-token windows only; offsets come from token boundaries |
| S3 | case and compatibility forms ("ALEX KIM", "Ａｌｅｘ", "ﬁ", "straße"/"STRASSE") | NFKC + casefold on both sides, same function |
| S4 | invisible characters inside a name in text (zero width joiner, variation selector) | default-ignorables removed before tokenising, offsets kept |
| S5 | look-alikes in the DECLARED identifier (Cyrillic "а" in "Аlex") | mixed-script token refused (heuristic) |
| S6 | look-alikes in OTHER speakers' text | NOT ruled out: needs the UTS 39 skeleton and confusables.txt, which is on neither machine; fetching it is a download that needs the user's permission (ask to main). Receipt lists it under not covered. |
| S7 | identifier inside another speaker's quoted text | the span inside the quote is redacted like any other span; quoted words of the subject that carry no identifier are not found (receipt: not covered) |
| S8 | redaction that changes nothing (offset drift, empty spans) | the store refuses empty/unordered spans; the app builds the text only from spans, inserts a fixed marker that cannot be an identifier, rescans the result and falls back to removal on any hit; a missing redaction leaves the event to the tombstone path |
| S9 | reviewer approves its own proposal | WPS30 on session_user; tested with one login that holds both rights |
| S10 | approved identifier differs from the reviewed one | the reviewer's recomputed HMAC must equal the proposal's (WPS31) |
| S11 | re-erasure / retry idempotency | unique (job, event); compare-and-set on base_digest; tombstones and placeholders already idempotent |
| S12 | a second subject erased later on an already-redacted event | the rescan and the redaction read the latest redacted text; the new row chains to the previous one, whose text is nulled |
| S13 | hash chain after redaction | the event hash covers the ciphertext digest, untouched; redaction rows chain from the event hash; verification walks both |
| S14 | redaction under another subject's legal hold | wp_erasure_redact refuses it; the event is withheld exactly as today |
| S15 | Unicode version skew between hosts | UCD version in the HMAC label and the rule version |
| S16 | scripts without spaces | refused at declaration with the reason |

### Sources (read 2026-11-17)
- UTS 39 Unicode Security Mechanisms v18.0.0 (2026-08-27), https://www.unicode.org/reports/tr39/ : skeleton =
  NFD plus confusables.txt mapping, no NFKC or casefold; skeletons are for internal comparison only, never stored as a
  normalisation; mixed-script = empty resolved script set (5.1); restriction levels (5.2). Confidence high (primary).
- UAX 15 Unicode Normalization Forms rev 58 (2026-08-12), https://www.unicode.org/reports/tr15/ : NFKC fits
  identifiers, not arbitrary text; normalisation changes length, so offsets do not map back; stability holds for
  assigned characters only. Confidence high (primary).
- Crypto-shredding with hash chains over ciphertext: conduktor.io (crypto-shredding in Kafka) and a 2026
  systematisation on redactable ledgers, https://eprint.iacr.org/2026/1109 (search summary only, not read in full).
  Supports chaining ciphertext digests and destroying keys rather than rewriting the chain. Confidence medium
  (secondary and vendor-authored); the lab's own chain already hashes the ciphertext digest.

### D3 implementation notes (5357472, 0aaf789; differences from the design above)
- `wp_identifier_declare` is kept as an INTERNAL function with no grant (not dropped): `wp_identifier_approve` calls it,
  so the lock order that exp/lock_order.py tests (subject FOR SHARE, identifier_epochs FOR UPDATE) is unchanged.
- No separate erasure_redacted table: `event_redactions.job_id` records which erasure made each redaction. A redacted
  event is STILL tombstoned by the cascade (ES placeholder, facts and jobs deleted, D4 strict verifier unchanged);
  `wp_erasure_apply` nulls every present redaction text of a tombstoned event except the erasing job's own latest one.
  `fallback_removed` in the receipt is computed by the store: other speakers' events this job tombstoned without a
  redaction of its own.
- Store refusals: WPS30 not a reviewer or self-approval; WPS31 HMAC differs; WPS32 rule version; WPS33 no open
  proposal; WPS34 redaction target invalid (own event, no mention, already tombstoned); WPS35 another subject's hold;
  WPS36 malformed spans; WPS37 stale base, wrong chain head, or already redacted by this job. The runner treats
  WPS34-37 as "fall back to whole removal".
- A redaction is a pure function of (current text, the subject's HMACs): `mentions.apply_spans` refuses spans that
  differ from the matcher's own (offset drift cannot produce a partial cut) and rescans the result.
- The HMAC label is applied in one place (`mentions.window_hmacs`); erasure's rescan had its own copy of "ident:",
  which would have silently stopped matching under m2 (test_ident LabelInOnePlace).

Not done or not verified (stated, not parked):
- S6 (look-alikes in other speakers' text) needs confusables.txt from unicode.org; fetching it is a download that needs
  the user's permission. Asked of main.
- The reviewer reads the sealed identifier through the key service with ordinary subject-key access; an op scoped to
  proposals only (its own AAD label and peer rule) is not built.
- ES holds no redacted text (over-removal from the derived index, stated in the receipt).
- Experiments that call `M.declare` (d4_store_mark, scope_hold, d1_hold_mentions, lock_order, mutual_mentions,
  d3_identifier, d4_rescan_es) now propose through their connection and approve through reviewer login wp_rev1:
  the approval's locks are taken on that second connection. lock_order's DA case observes the declaring backend, so it
  must be re-read before any rerun. No stamped run has used this code.

## Redesign after review C (reviewC-1 on 4fe3ac1; written before tests and code, 2026-11-17)

SUPERSEDES, in "Design: D3" and "D3 implementation notes" above: everything about span redaction (mechanism items 6-10,
scenarios S7, S8, S12, S13, S14 as redaction cases, the event_redactions notes) and S6's "not covered". The saved
evidence `results/failing-first/d3/d3-store-pg-fb60b2e.red-green.out` is superseded with them.

### Decision R1 (class 1, F1): no span redaction; whole-event removal is the only erasure path
Source: main's message (review C round) offering "drop span redaction for unverifiable cases and use whole-event
removal ... as the only path the app can take". DECIDED 2026-11-17 by the coordinator on the user's delegation (the
user was asked and answered "you decide"; not the user's own pick): see "Decisions after review E" below.
Options: (a) redaction computed by the app (rejected: F1, the app holds every key, so it can store the original as a
"redaction"); (b) computed or checked by the verifier (rejected: by D2 the verifier holds no key material, it reads only
key ids; giving it keys makes it one more key holder and breaks D2); (c) whole removal only (chosen). Under the current
key architecture every party that can read plaintext is a key holder equal to the app, so NO redaction is independently
verifiable; therefore none is offered. Removed: event_redactions, wp_erasure_redact, the runner's redaction step, the
rescan of redacted text, the residue key, the redaction chain. F1, F2, F4 and F7 (as redaction findings) cannot be
expressed. Other speakers' removed events are listed per event in the receipt (F10). Span redaction returns only with an
independent redactor identity holding scoped key access (keysvc per-peer ops), which is not built.

### Class 2 (F8; and F2/F4 before R1 removed them): judge only what you hold locked
Source: PostgreSQL 18 docs, Transaction Isolation, https://www.postgresql.org/docs/current/transaction-iso.html (read
2026-11-17, confidence high): in Read Committed a plain SELECT sees a snapshot as of statement start, so a check by a
plain SELECT can be invalid by the time a later write runs; FOR UPDATE / FOR SHARE wait for a concurrent writer and
return the updated row version. Rule: every function locks the rows it judges in the statement that judges them.
- `wp_identifier_propose` takes the subject row FOR SHARE and checks `erased` from that locked row (the same barrier as
  approve/declare): an open mark (FOR UPDATE) makes it wait; after the mark commits it sees erased and refuses (WPS03).
  A proposal that committed first is deleted by the cascade. "Proposal after erasure" cannot commit.
- WITHDRAWN by review E F12 (false: mark x cascade could deadlock; see "Redesign after review E"). Lock order, every path: subject rows (sorted by subject id) -> identifier_epochs (tenant row) -> proposal row ->
  event rows. Mark takes the subject FOR UPDATE; propose, approve/declare, append and cascade take subjects FOR SHARE
  first. Exclusive subject lockers (mark, hold placement) never wait on an event or proposal row, so no cycle exists.
- Pairs tested live: propose x open mark (waits, then WPS03); approve x open mark (waits, then WPS03).

### Class 3 (F3): the matcher version is stored and enforced, never implied
(Matcher id now m4: see "Redesign after review E" F11; the storage and enforcement below are unchanged.)
- Matcher id `m3:ucd<unicodedata version>:di<sha12 of the ignorable table>:cf<sha12 of the confusables table>`; the
  HMAC label carries it, so a different Unicode version or a different table is a different matcher.
- `identifier_epochs.matcher` is the tenant's matcher, set only by the owner function `wp_matcher_set` (setup/admin);
  never by a proposal. `subject_identifiers.matcher` with a composite foreign key to (tenant, matcher): an identifier
  under another matcher than its tenant's cannot exist, so a scan can never silently skip one.
- Refused, not skipped: approve when the proposal's matcher is not the tenant's (WPS32); an append whose
  `mention_matcher` differs (WPS38, events_guard, under the identifier_epochs FOR SHARE it already takes); a rescan
  page or completion with another matcher (WPS38). `wp_matcher_set` on a tenant that has identifiers is refused (the
  foreign key), so switching cannot orphan them.
- The event hash covers `mention_matcher` (with mentions and scan version), so a stored tag cannot be changed silently.
- Migration path (stated, not built): re-review every identifier from its sealed proposal under the new matcher into a
  staging set, then one owner transaction swaps the tenant's identifier set and matcher (no committed state mixes
  versions; appends are refused while the swap waits for identifier_epochs). Rollback is the same procedure back,
  possible while subject keys exist (an erased subject has no identifiers). m1 identifiers from earlier code have no
  matcher and are refused; the lab rebuilds its databases.

### Class 4 (F6, S6): Unicode properties from Unicode data, pinned
(The tables and pins below are unchanged. The KEY construction below, including the as-written key, is SUPERSEDED by
matcher m4: "Redesign after review E" F11.)
- Default_Ignorable_Code_Point is enumerated from the host perl's Unicode data (Unicode::UCD; its version must equal
  Python's unicodedata version: Mac 13.0.0, t2-dev 15.0.0) by `tools/ucd_tables.py`; the table's sha256 is pinned per
  version in `wp/mentions.py`. A missing or different table fails closed (UCDUnavailable): no fallback to matching
  without it. A test enumerates the WHOLE property: every code point, inside a name, still matches with correct offsets.
- Look-alikes (S6): the matching key is a UTS 39 skeleton per cluster (NFD, each code point mapped by confusables.txt
  18.0.0, 2026-08-06, sha256 6ed3ee96..., downloaded with the user's approval, NFD again), applied to the case-folded
  normal form, then casefold and NFKC. Exact matches are a subset (identical strings have identical keys), so only the
  key HMAC is stored, one per identifier.
- CORRECTION (found by tests/test_ident.py while writing the code, before any commit of it): the order written here
  first, skeleton BEFORE casefold, broke ordinary case matching. confusables.txt is case-sensitive (capital I maps to
  small l; small i, dotless i and iota map to i), so "KIM" got the key "klrn" and "Kim" the key "kirn". No single
  case-insensitive key can also see capital-shape look-alikes without merging i and l (which would make "Ian" equal
  "Lan"). Design adopted: identifiers and text share the case-folded key above; TEXT tokens also get an as-written key
  (skeleton of the cluster as written, then the key), and a text window is looked up under every combination of its
  tokens' two keys (at most 2^4 = 16; one for text without such capitals). "AIex Kim" is found that way. Stated over-match:
  "AIEX KIM" (it looks like "AlEX KIM"). Stated not-covered: a look-alike of a capital letter of the declared identifier
  ("lan" for a declared "Ian").
- Rule r3 judges BOTH forms: scripts, unassigned code points, digits and token length on the normal form (a skeleton
  hides mixed script, and m -> rn lengthens a token); the stop list on keys ("custorner" has the key of "customer").
  The reviewer therefore reads the identifier AS WRITTEN (sealed under the subject key), not a normalised copy.
- Cost, stated in the receipt: confusable variants also match ("Kim" and "Kirn" share a skeleton), and with whole-event
  removal more of other speakers' events are removed; two subjects whose identifiers share a skeleton are refused
  (WPS20).
- Not vendored: the Unicode License V3 (https://www.unicode.org/license.txt, read 2026-11-17) permits redistribution only
  with its copyright and permission notice; the repository holds the generator and the pins, the lab keeps the tables in
  its data directory (WP_UCD_DIR). The download was parsed as data only (python3 -I), nothing in it was executed.

### Class 5 (F7): verification must notice deletion
With R1 there is no redaction chain. NULL content verifies only with a tombstone, and a tombstone only with its erasure
job present: deleting the tombstone, or the job, under a nulled event fails verification; deleting the event breaks the
main chain (and its external anchor, row 6).

### F5: one write alias, converged from any observed state
The CAS converges each family to exactly one write alias, `-w<E>` on the family's base index with is_write_index true:
it removes (must_exist) every other `-w` alias it observed, including a `-w<E>` that is not the write index or sits on
another index, and adds the correct one, in one atomic request; a newer epoch is StaleEpoch; no write alias at all is
refused. After the request it re-reads and requires exactly that state before returning (the broker mints only after).
Tested from enumerated starting states, single and mixed across families.

### F9: a partial search is not absence
Each per-copy search sends allow_partial_search_results=false and requires timed_out false, terminated_early not true,
and _shards total = successful = 1, failed = 0, skipped = 0; anything else counts es:copy_unverified (non-zero, so
the job stays open), and an incomplete base copy shows no id placed. Source: the
Elasticsearch search API reference and partial-results options (search summary of elastic.co reference pages and a
discuss thread, read 2026-11-17; confidence medium: secondary summary, no Elastic page fetched).

### F10
DESIGN-NOTES line 45 corrected; the receipt lists other speakers' removed events per event (`other_speakers_removed`,
event ids; with R1 there is no later whole removal of a redacted event to account for).

### Scenario map (reviews B and C; the S6 row is now ruled out by m4, see review E)
| Finding | Class | Ruled out by |
|---|---|---|
| C-F1 app stores the original as a redaction | 1 | R1: no redaction write path exists |
| C-F2 redaction CAS without event lock | 1, 2 | R1 (function removed) |
| C-F4 redaction hold check without locking rescan-only subjects | 1, 2 | R1 (function removed) |
| C-F8 proposal after erasure | 2 | propose locks the subject before judging erased |
| C-F3 Unicode/matcher skew silently stops matching | 3 | matcher stored, FK, refusals |
| C-F6 Khmer U+17B4/17B5 not ignored | 4 | DI table from Unicode data, whole-property test |
| S6 look-alikes in text | 4 | UTS 39 skeleton key |
| C-F7 deleted redaction history verifies | 5 | R1; tombstone and job required under NULL content |
| C-F5 extra stale alias hidden by a current one | alias | converge to exactly one, re-read |
| C-F9 partial search counted as absence | ES | partial responses rejected |
| B2 interleavings | alias | unchanged (-w0 generation, must_exist removal) |

## Redesign after review E (reviewE-1 on 0e8f67c; written before tests and code, 2026-11-17)
Open after review E: F4 (high, legal-hold race on whole-event removal), F11 (medium, token keys miss mixed case and
look-alike choices inside one token), F12 (minor, the universal lock-order claim of class 2 is false). Decision R1
(whole-event removal only) is still an ASSUMPTION awaiting the user. Each is fixed by class, not instance.

### F4, the hold class: every subject whose hold a removal judges is locked in the judging statement's transaction
The hold decision for an event e reads H(e) = {speaker} + mentions + {subjects linked by erasure_mentions}. Speaker and
mentions are immutable (events_change_guard); erasure_mentions grows (mark: 'write' rows, which repeat mentions;
wp_erasure_add_mentions: rescan rows, which ADD subjects). Reviewer's schedule: A's batch locks H(e) as read, C's rescan
links e to C and commits, A judges with C unlocked, a hold on C commits, A removes e.
Rule (the F8 rule applied to the whole class): the set a judgement reads is fixed by a lock before it is read, and
every member is locked before it is judged.
- wp_erasure_apply locks the candidate EVENTS first (FOR NO KEY UPDATE, ordered by event id), then reads H(e) and
  share-locks every subject in it (ordered by subject id), then judges holds, then tombstones and nulls.
- wp_erasure_add_mentions locks the events it links FOR SHARE (ordered) before inserting the link: a link to an event
  whose removal is in flight waits until that removal commits; a link that committed first is seen by the removal,
  whose subject (C) it then locks and judges.
- wp_hold_place / wp_hold_release take the subject FOR UPDATE (unchanged), which conflicts with the share lock of an
  in-flight removal that judges that subject. So a hold either commits before the judging statement (the event is
  withheld) or waits and then finds the event already removed; wp_hold_place now RECORDS that truthfully: it returns
  linked_removed (count) and linked_removed_ids (first 20, sorted) of the events in the subject's footprint (speaker,
  mentions, erasure_mentions) whose content is already gone when the hold lands.
- A hold on C placed while C's link to e is still waiting lands first and does not cover e (not linked yet); the link
  commits after e's removal. Serial order: hold, removal, link, which is a consistent history.
- Own subject: mark judges under the subject FOR UPDATE; each cascade batch under FOR SHARE held to commit (unchanged).
Added after the first window (advisor review): E7 hold x mark (a hold first refuses the mark, WPS16; a hold during a
paused mark waits; a regression guard, because mark holds the subject three ways: FOR UPDATE, the hold check's FOR SHARE
and the erased update, so no single-lock variant can fail it); E8 hold x the cascade's final pass (only the batch's
hold check protects it; variant: the hold check without FOR SHARE); variant: hold release without FOR UPDATE (E4).
Not covered by a test: a hold on C active while e is removed and the link to C lands afterwards (E1's order) is
consistent but nothing tells legal operations about that later link; the footprint query of wp_hold_place runs under
the subject's FOR UPDATE and is not measured at scale.
Tests (live, forced interleavings; tests/test_d3_pg.py E*): E1 the reviewer's schedule (link during a paused removal
must wait); E2 a hold on the speaker during a paused removal waits and then reports the event removed; E3 a hold on a
rescan-linked subject (link committed first) waits and reports it; E4 a hold placed first withholds the event, its
release during a paused batch waits, and a later pass removes the event; E5 a hold on the subject itself after mark
stops the cascade (WPS16) and a hold placed during a paused batch waits. Broken variants, one per lock, each must make
a test fail: apply without the event lock (E1), add_mentions without the event lock (E1), apply without the subject
share lock (E2, E3), the footprint without its erasure_mentions branch (E3), hold placement without FOR UPDATE (E2).

### F12: one acquisition order where an exclusive locker meets a cascade; claim only what is tested
Reviewer's cycle: a cascade holds the subject FOR SHARE and later updates the job cursor; a repeated wp_erasure_mark
takes the job FOR UPDATE and then waits for the subject FOR UPDATE. Fix: mark reads the job identity (immutable,
erasure_guard) without a lock, takes the subject FOR UPDATE, then the job FOR UPDATE. The class 2 claim "lock order,
every path ... no cycle exists" is withdrawn. The order the code now follows on the erasure paths is: subject rows ->
event rows (apply, add_mentions) -> subject rows FOR SHARE (apply's footprint; only share, and exclusive subject
lockers never wait on events) -> job row. Append, propose and approve take only subject share locks before their own
rows. This is a table of acquisition orders, not a proof: the tested schedules are E6 (the reviewer's mark x cascade
schedule, no deadlock) and the earlier C2/C3; any other pair is unproven, and a deadlock between untested paths is
still possible and retryable (40P01 rolls one transaction back; every step is idempotent).

### F11: one canonical key per token (matcher m4)
SUPERSEDED by matcher m5 ("Decisions after review E" below): the coordinator rejected merging i, l and 1 into one
letter. The UTS 39 source, the class construction and the CORRECTION below still describe m5's identity key, except
where m5 says otherwise.
Source: UTS 39 revision 34, version 18.0.0, 2026-08-27, https://www.unicode.org/reports/tr39/ (read 2026-11-17,
confidence high). Section 4 "Confusable Detection": internalSkeleton(X) = (1) NFD, (2) remove Default_Ignorable_Code_Point
characters, (3) concatenate the prototype of each character (confusables.txt), (4) reapply NFD; X and Y are confusable
iff skeleton(X) = skeleton(Y); the data is closed under transitivity, and the test "may be overly inclusive". The
standard does NOT combine the skeleton with case folding or NFKC and gives no order for doing so (toCasefold and NFKC are
listed only among the building blocks of identifier profiles in section 3), and section 6.1 says skeletons should not
be used as a normalisation of identifiers (here they are never shown; only HMAC'd).
Why no single composition of the standard's steps suffices: confusables.txt maps capital I to small l, while case
folding maps I to small i. The reviewer's "AIIson" for "Alison" uses one capital I as a look-alike of l and one as the
capital of i. skeleton(casefold(x)) gives aiison against alison; casefold(skeleton(x)) breaks KIM against Kim. Any one
key that accepts both readings must make i and l the same letter. Closing the union of case folding and the
confusables relation over ALL scripts is far worse (probe on the pinned tables: n = v, s = f, r = t = u = y, o = c),
because other scripts' case pairs link different-looking letters (Greek capital Nu looks like N, its small form like v).
Matcher m4 (one key per token, the same function for identifier and text):
- per code point after NFKD and removal of Default_Ignorable and Cf: if it belongs to a CLOSED case pair it is kept as
  written, otherwise it is case-folded; then the UTS 39 prototype (steps 3-4); then each prototype code point is mapped
  to the representative of its class.
- classes: union-find over prototypes; for each case pair (a cased letter and its single-code-point case fold, both
  with single-code-point prototypes) the two prototypes are joined, Latin pairs first, then other scripts' pairs in code
  point order, a pair being CLOSED only if joining does not merge two classes that already contain different ASCII
  letters or digits. Expected ASCII classes: {i, l, 1}, {o, 0}, {s, 5} (corrected below: {i, l, 1}, {o, 0}).
- NOT_COVERED gains a computed list: the case pairs that are not closed (for example Greek Nu, Eta). Such a capital is
  matched by its small form, so it is not found as a look-alike of a Latin capital (ALISON with Greek Nu).
- OVER_MATCH: the merged classes; events of other speakers whose words differ from a declared identifier only by such
  letters (Allson, Aiison, Ailson, A1ison for Alison) are removed with the subject's erasure. Receipts state it.
- the matcher id becomes m4 (same tables); the store refuses a matcher change while identifiers exist (WPS38), so an
  existing tenant's identifiers must be re-declared (no migration, as before).
- CORRECTION (found by tests/test_ident.py F11 while writing the code, before any commit of it): joining every LATIN
  case pair unconditionally merged f, s and 5 (long s folds to s but looks like f; Latin tone five), and joining a
  pair whose prototype itself case-folds put sharp s in B's class (Greek capital Beta looks like B, small beta's
  prototype is sharp s, which folds to ss). Rule as built: ASCII case pairs always; any other pair only if both
  prototypes are stable under case folding (or already closed) and the join merges no two ASCII classes. ASCII classes
  are {i, l, 1} and {o, 0}; the computed not-closed list has 63 capitals on Unicode 13.0.0 (Greek Beta, Eta, Mu, Nu,
  Sigma, Upsilon; Cyrillic Be, Ghe, El, Em, ...): each is matched by its small form, so Greek and Cyrillic text in
  capitals still matches, but one such capital used as a look-alike of a Latin capital (Greek Beta in BOB) is not found.
- OVER-MATCH OF REAL NAMES (checked after the code, before the return): with i and l one class, names that differ
  only by i and l share a key (Delia and Della, Hailey and Halley, Neil and Nell, Elia and Ella). Erasing one removes
  other speakers' events naming the other. m3 kept small i and l apart. Stated in OVER_MATCH and the receipt, pinned
  in tests. The alternative the reviewer offered keeps i and l apart: bounded per-character readings for ambiguous
  capitals only (capital I read as i and as l; at most 2^k keys for a token with k such capitals, capped), at the cost
  of more than one key per token for text in capitals. Not built: main asked for one canonical key; the choice is open.
Tests (tests/test_ident.py F11*): the reviewer's case; a seeded generator applying random mixes of case changes,
confusable substitutions taken from the raw table (sources whose prototype equals the prototype of the letter or of its
case variant, excluding the published not-closed pairs), Default_Ignorable insertions and composed/decomposed and
reordered combining marks to approved identifiers, each required to match; the ASCII partition; near-miss negatives
(a letter replaced by one of another ASCII class, a letter dropped or added) that must not match; closure properties
enumerated over the tables (case variants and confusable sources share the key, except the published list).

## Decisions after review E (main's relay, 2026-11-17; written before tests and code)

Source: main (500523cf) relaying the user's answer "you decide" on the two open erasure decisions. Both are DECIDED
BY THE COORDINATOR ON THE USER'S DELEGATION, 2026-11-17; neither is the user's own pick.

### R1: whole-event removal is the only path
Unchanged in substance from "Decision R1" above; only its label changes (no longer ASSUMPTION). The stated cost stays:
an event of ANOTHER speaker that mentions the erased subject is removed whole, including everything else that speaker
said in it, and the receipt lists those events (`other_speakers_removed`). Decision 10 (the identifier mechanism as a
whole) and rule r3's thresholds remain ASSUMPTION: they were not part of this decision.

### Name matching: matcher m5 (i, l and 1 are never one letter)
Main's ruling: do not treat i, l and 1 as one letter; only ambiguous glyphs get both readings; distinct letters never
come together; the same reasoning for o and 0; a digit standing for a letter is caught in TEXT, but approved
identifiers that genuinely contain digits must stay apart; a capped number of keys, refusal at proposal beyond the cap;
versioned m5.

1. IDENTITY KEY (what a glyph IS; the one key of an identifier, and the one reading of every unambiguous glyph): the m4
   pipeline (UTS 39 prototypes from the pinned tables, Default_Ignorable and Cf removed, case folding unless the case
   pair is closed, class representatives, iterated to a fixed point) with three changes:
   - a code point whose NFKD is ASCII and contains I, 0 or 1 is replaced by that NFKD first (fullwidth, mathematical,
     superscript, subscript and circled forms; Roman numerals; the ligature IJ). No other code point is decomposed
     first, so long s and the m4 CORRECTION cases are unchanged;
   - the digits 0 and 1 keep themselves (UTS 39 maps 0 to O and 1 to l; m5 reads those maps only as look-alikes, item 2);
   - NO case pair is closed, ASCII pairs included, if joining would join two classes anchored by different ASCII
     letters or digits (the anchor of a letter is the prototype of its SMALL form). So I/i is not closed and capital I
     case-folds to i; every ASCII letter and digit is its own class (m4: {i, l, 1}, {o, 0}).
   Identifiers have one key: their identity key. A capital I in a DECLARED identifier is the letter i (the declarer
   wrote a letter; the reviewer reads the normal form, in which it is i).
2. BOTH READINGS, computed from the tables, never listed by hand: a glyph g gets the readings {identity(g),
   lookalike(g)} when they differ and both are among the ASCII letters i, l, o and digits 0, 1 (exactly m4's joined
   classes), where lookalike(g) is the identity key of g's UTS 39 prototype. A DIGIT glyph also reads as every letter
   whose capital has the digit's prototype (a digit has no case, so it stands for a capital as well: 1 for capital I,
   hence i). On Unicode 13.0.0 with confusables 18.0.0:
   - {i, l}: 23 glyphs: capital I and its fullwidth and mathematical forms, Greek capital Iota, Cyrillic capital
     Byelorussian-Ukrainian I, Latin capital Iota, Roman numeral one, modifier capital I, Coptic Iauda, ... Why: its case
     makes it i; UTS 39 says it looks like l.
   - {1, l, i}: 11 glyphs (digit 1 and its compatibility forms). Why: it is 1; UTS 39 says it looks like l; it also
     looks like capital I, which is i ("1 with l or I").
   - {0, o}: 11 glyphs (digit 0 and its compatibility forms). Why: it is 0; UTS 39 says it looks like O.
   - a code point that NFKD-expands (Roman numerals two to twelve, IJ) is read per letter: Roman two as ii, il, li, ll.
   NOT given both readings, deliberately: small l (it is l and looks like l: never read as i), small i, o, capital O,
   capital L. A letter is never read as a digit, and a letter is never read as another letter except a capital I
   look-alike. So Delia and Della, Hailey and Halley, Neil and Nell, Elia and Ella have different keys and plain text
   naming one never matches the other.
3. DIGITS READ AS LETTERS ONLY INSIDE WORDS: the letter readings of a digit glyph apply only in a token whose identity
   key has more letters than digits (A1ison, J0hn, N1ck: yes; 1001, 10am, user1001, XQ-1001, 0x00000001: read
   literally). Why: a digit standing for a letter is a disguise inside a word; a token that is mostly digits is a
   number or a code, and reading its digits as letters would blur different numbers and multiply cost. Sound because
   readings only turn digits into letters: if a text token's identity key has more letters than digits, so does every
   key it can produce.
4. KEYS: an identifier has ONE key (its identity key). A text token has the product of its glyphs' readings; a window
   (1..4 tokens) has the product over its tokens. Invariants (tested): the identity key of any string is one of its
   text keys; a string with no both-readings glyph has exactly one text key, its identity key; identity keys are fixed
   points (0 and 1 survive every pass; no second pass maps 1 to l or 0 to o).
5. THE CAP: at most P = 4096 readings per window. An identifier's CAPACITY is the largest number of readings a window
   that matches it can have: the product, over its identity key's characters, of the most readings any glyph producing
   that character has (i, l, 1: 3; o, 0: 2; other characters, and digits in a token with no more letters than digits:
   1). An identifier whose capacity exceeds P is REFUSED at proposal (rule r3 at propose, again at review) with the
   reason, never silently under-matched; the declarer can declare a shorter form (fewer tokens). A text window with
   more than P readings is not enumerated; that loses nothing, because a window that matches an approved identifier has
   at most that identifier's capacity, which is at most P. One window may still hit several subjects.
   Refusal rate, measured on a synthetic list (seed 16096: about 200 common first names and 230 surnames in English,
   Spanish and Italian, combined; probe agent-af5c8cce658ab5a89/m5_probe.py, Unicode 13; not a real population):
   2-token names 0.3% of 3000, 3-token 4.3% of 1000, 4-token 11.4% of 1000, numbers and codes 0% of 1504. (P = 16384:
   0.0%, 1.4%, 5.6%; P = 65536: 0.0%, 0.3%, 1.5%. If 1 were read as l only: P = 4096 refuses 0.0%, 0.0%, 0.4%.)
   P = 4096 is chosen for cost (item 6); it is one constant, and main may change it.
   PRECISION (advisor check on abceae9): the capacity is an UPPER BOUND, not the largest count reachable: under item
   3's rule digits cannot fill every i, l or o position of a word, so a tight bound would refuse fewer names than the
   rates above. The rates are for Latin-script names only (NOT VERIFIED for other scripts: Greek iota and omicron,
   Cyrillic o, Arabic alef and heh and Hebrew vav carry the same weights).
6. COST (measured, same probe, HMAC about 8 us per key in Python on the Mac): keys per token, all windows of one event
   counted with repeats: m4 about 4; m5 prose with the pronoun I 7.8; all-caps legal text 34.4; text dense with codes
   and versions 7.4; hex dump 8.2. ADVERSARIAL text (tokens built to put just under P readings in every window) reaches
   about 4,600 keys per token at P = 4096 (about 40 ms per token here), and every one of them is sent in the store's
   lookup array. Content size is not bounded by the write path, so one crafted event can cost minutes of matcher time.
   OPEN (availability, for main): (a) a per-event key budget that refuses the write when exceeded, so crafted text is
   refused (it may also refuse a legitimate pathological text); (b) let an over-budget event be written unscanned and
   found by the erasure-time rescan (a store change to WPS17); (c) accept and monitor. Not built: m5 implements none.
   Also (advisor check on abceae9): _windows_at builds the whole key list in memory, so a long crafted event is a
   memory problem as well as CPU (measured at 7fd3164: 300 crafted tokens gave 1.39 million window keys, 0.35 s to
   enumerate); and the erasure-time rescan reads the same events, so one crafted event taxes every later erasure's
   rescan, which weighs against (b). Recommendation to main: (a) with the budget set at least 10 times above the
   worst measured realistic text (34.4 keys per token), the enumeration made lazy so that the budget bounds memory,
   and the refusal reported to the writer with the reason.
   DECIDED (main, 2026-11-17, recorded as ASSUMPTION under the user's "you decide" delegation): (a). Design in
   "Matcher budget (m5 b1)" below; it supersedes the OPEN text above.
7. NOT COVERED (receipt and NOT_COVERED): a letter standing for a digit (XQ-l00l or XQ-IOOI for XQ-1001); small l
   written for i or small i for l (Allson, Aiison for Alison: m4 matched them); digits standing for letters in a token
   with no more letters than digits (4l1s0n for Alison); non-ASCII digits that look like 0 or 1 read as the letter
   their prototype names, not as the digit; the computed not-closed capitals, 66 on Unicode 13: m4's 63 unchanged plus
   three that m4 covered only through its joined i/l class (Latin capital I with stroke, Cyrillic capital Yeru and Yeru
   with back yer); every m4 NOT_COVERED item.
   ADDED (advisor check on abceae9; m4 matched these): a look-alike of small l or small i from ANOTHER script written
   for the other letter is read as the letter its prototype names only: 89 look-alikes of l (Cyrillic palochka, Latin
   dental click, Hebrew vav, Arabic alef, ...) and 39 of i (dotless i, Cyrillic small i, Greek iota, ...) besides the
   ASCII letters, on Unicode 13 and 15 (AlӀson with palochka does not match Alison). Stated, not added to the
   both-readings table: vav and alef are ordinary letters in their own scripts, and reading them as i would make
   every such word a reading of an i-word.
8. STILL OVER-MATCHED (receipt and OVER_MATCH): confusable variants (Kim and Kirn) as before; a capital I or one of its
   look-alikes in TEXT is read as l too, so text in capitals over-matches (DELIA also matches Della, NEIL matches
   Nell, HAILEY matches Halley); a digit 1 or 0 inside a word in text is read as the letters too (Ne1l matches Neil and
   Nell; a handle maria1 matches a declared Marial); one text window can hit several subjects (EIIa Moreno hits Elia
   Moreno and Ella Moreno; NEIL YOUNG hits
   Neil Young and Nell Young). CORRECTION (while writing the tests, 3c0da12 said "AIIson hits Alison and Allison",
   which is wrong: AIIson has six letters and Allison seven).
9. CHANGED FROM m4 beyond i/l and o/0: five code points now read as their digit or letter instead of their UTS 39
   prototype (superscript one and zero, circled one and zero, modifier capital I), by item 1's NFKD rule.
   CORRECTION (found by the closure test while writing the code): six word code points, adding circled number ten
   (read as 10). Parenthesised and full-stop numbers (U+2474..U+249B, U+1F100..U+1F102) also expand, but they contain
   punctuation, so they never lie inside a token (as under m4). The closure test committed in 8dab50f did not encode
   item 1 for these; it now checks the NFKD reading for them and skips code points that cannot be inside a token.
10. VERSION: matcher id m5:ucd<version>:di<sha12>:cf<sha12>; rule version r3:<matcher id>. The store keeps the tenant's
   matcher and refuses work under another (WPS32 at approval, WPS38 at append and rescan): an m4 tenant must re-declare
   its identifiers; there is no migration and no fallback.
11. RULE r3 STOP LIST (CORRECTION, advisor check on abceae9): the stop list was built from identity keys while text now
   has readings, so "ln the" was accepted and would then match every "In the" (m4 refused it, because i and l were
   one class). The stop keys are now every TEXT key of each common word written in lower case, capitalised and in
   capitals (53 words, 65 keys). Red on abceae9, green after (results/failing-first/m5).
   THE RULE, stated (main's relay after the m5 return): an identifier is refused as TOO GENERIC when every one of its
   tokens' identity keys is a key that some common word (STOP) can have in TEXT written in lower case, capitalised or
   in capitals; separately, a single token shorter than MIN_SINGLE = 6 characters with no digit is refused. A
   consequence the tests use as an independent oracle: an identifier that would match a phrase made only of common
   words, in one of those three case forms, is refused. Tested with both readings applied: for every STOP word and
   every substitution of its i, l and o by I, l, i, 1, 0 and O, the identifier "<variant> the" (and "the <variant>")
   is refused whenever its key is among the window keys of "<word> the" ("the <word>") in one of the case forms.
12. NOT VERIFIED: refusal rates outside Latin script (item 5); whether exp.d4_store_mark's corpus contains any
   both-readings glyph (it passed under m5, which shows the call sites work, not that readings were exercised there;
   tests/test_ident.py exercises them); the adversarial cost on t2-dev (measured on the Mac only).
Tests first (tests/test_ident.py F11M5*, red on 15b3255 by assertion through key(), windows(), find_spans() and
identifier_problem(), which exist there): the four real-name pairs do not match each other in either direction; AIIson
and A1ison still match Alison; the seeded generator of case, look-alike (only glyphs from the computed table and the
confusable sources of a letter's own prototype), ignorable and combining-mark variants still matches, with the
documented exceptions excluded from it and tested as misses; near misses (now including i for l and l for i) do not
match; XQ-1001 and XQ-l00l stay distinct and text XQ-1001 does not match XQ-l00l; the cap (an identifier over P
refused, one at P accepted, a window over P not enumerated; the last is a guard, m4 passes it); the invariants of item
4; the both-readings table pinned by its counts and members on this Unicode version.

## Matcher budget (m5 b1; written before tests and code)

Source: main's relay after the m5 return (2026-11-17): main's decision, recorded as ASSUMPTION (the user delegated
the erasure design choices with "you decide"); main chose option (a) of "Decisions after review E" item 6 with the
requirements below. The numbers are this owner's, measured as stated; every one is an ASSUMPTION main may change.

B1. MEASUREMENT (keys counted as the budget counts them: every window of 1..4 tokens, repeats included, windows over
   P = 4096 readings skipped; probes agent-af5c8cce658ab5a89/budget_probe.py, budget_real.py, budget_short.py, Mac,
   Unicode 13). Keys per token, synthetic samples: prose with the pronoun I 7.8; all-caps legal text 34.4; a code
   block dense with i, l, I, 0 and 1 (identifiers such as IIlI1, l10, 0x10) 38.0; a table of codes 25.2; leetspeak
   chat 13.6; prose repeated to 39 KB 7.8. Real files (Python 3.9 LICENSE.txt, difflib.py, argparse.py, this.py), as
   written: 4.0 to 6.2; upper-cased: 4.8 to 11.8, worst 4 KiB slice 15.3. Short all-caps strings (headings, places,
   names): at most 5,344 keys for a whole string ("MINNEAPOLIS ILLINOIS INDIANA MISSISSIPPI MISSOURI", 5 tokens).
   Keys per byte: worst realistic 6.82 (the code block). ADVERSARIAL: "1I1I1I" tokens 4,635 keys per token; "I1x"
   1,550; "IIII" plus five letters 4,364; two-letter "II" tokens 340 keys per token, which at 64 KiB is 7.4 million
   keys. PLAIN ENGLISH THAT IS PATHOLOGICAL: all-caps sentences in which nearly every word has several I's
   ("IMMEDIATE INITIALIZATION IS IMPOSSIBLE IN ILLINOIS ...") reach 1,546 keys per token.
   Cost: lazy enumeration plus one HMAC per key 1.6 us per key; plain hmac.new 3.4 us, a copied HMAC 2.7 us (one run
   each on a loaded Mac, not a controlled benchmark; item 6's earlier 8 us went through windows(), sorting and the
   test key service, and is superseded). The store lookup of each 8,192-key chunk is NOT MEASURED (t2-dev is below
   the 20 GB run floor).
B2. THE BUDGET, one versioned record (BUDGET in wp/mentions.py), named in the matcher id so that changing any number
   changes the matcher id (and WPS38 then treats it as another matcher):
   - KEYS_PER_TOKEN = 512: 13.5 times the worst realistic per-token figure (38.0), 14.9 times all-caps legal text, 33
     times the worst real upper-cased slice;
   - KEYS_FLOOR = 131,072 (2^17): an event with few tokens may use this many whatever its token count, so a short
     all-caps heading full of I's is admitted (24.5 times the worst short string measured; about 0.2 s of matcher
     time);
   - EVENT_KEYS_MAX = 5,000,000: the most any event may use: 11.2 times the worst realistic text at the size bound
     (6.82 keys per byte times 65,536 bytes is 447,000); prose at the size bound is about 106,000 keys (47 times
     under);
   - budget(event) = min(EVENT_KEYS_MAX, max(KEYS_FLOOR, KEYS_PER_TOKEN times the event's token count));
   - per CALL (B10): at most 1,000 events, 1,048,576 bytes and 5,000,000 keys in all.
   A window with more than P readings is not counted and not enumerated (unchanged from m5 item 5).
   COST STATED WHEREVER THE BUDGET IS (main's decision 3): the worst admitted event costs about 14.5 s of matcher CPU
   (Mac, Python; store lookups not measured) and the worst admitted call about 16 s (B10); there is NO per-tenant rate
   limit, so a writer may repeat such calls.
B3. THE SIZE BOUND: MAX_CONTENT_BYTES = 65,536 UTF-8 bytes of event content. Why this number: it bounds the matcher's
   worst admitted work per event (EVENT_KEYS_MAX is derived from it), the tokenizer's work and the ciphertext the
   store keeps; it admits a long document (about 10,000 English words; the 39 KB sample) in one event. ASSUMPTION: the
   repository has no evidence of real event sizes (no loader, fixture or mapping states one), so this turns some
   writes that were accepted into refusals; a writer with a longer document splits it into several events. The same
   bound applies to an inbox payload and an analyst job result (also sealed and stored; not matcher-scanned). Checked
   on the code-point count first (a string longer than the bound in code points is longer in bytes, refused without
   encoding it), then on the UTF-8 length, before the secret scan, before any key is created or anything is sealed.
   Defence in depth: check constraints in the store on events and quarantine ciphertext (bound plus the 28 bytes of
   nonce and tag), so a path that skips the check cannot store an oversized event.
B4. HOW IT RUNS (append): admit(texts) first: size, then the key COUNT, computed arithmetically from each token's
   reading count (a product of small integers, stopped above P) and summed over windows; nothing is enumerated before
   the count is known to be within the budget. Then enumeration is lazy (a generator over windows keeping at most the
   key lists of the last 4 tokens) and matching is chunked: at most CHUNK = 8,192 distinct pending keys are HMACed and
   looked up at a time. So the work is at most the budget and the memory at most one chunk plus four token key lists
   (each at most P), whatever the text; nothing proportional to the unbudgeted key count is built. The same functions
   serve scan() (append), the erasure rescan, find_spans() and windows() (tests and diagnostics).
B5. REFUSAL: an event over the size bound raises ContentTooLarge, code WPM01; an event over its budget raises
   MatcherBudgetExceeded, code WPM02. Both are WriteRefused (attribute `code`), distinct from the store's WPSnn
   SQLSTATEs and from SecretDetected. The message gives the reason with numbers only (bytes and the bound; keys, tokens
   and the budget; the index of the event in the batch), never content. The whole append is refused (nothing of the
   batch is stored): no events row, no quarantine row, no subject key created. The refusal is RECORDED: a store
   function wp_write_refused(tenant, code) adds one to write_refusals(tenant_id, day, code, n, last_at) in its own
   transaction (count and reason code, no content, no subject); an operator reads the table. If recording fails, the
   writer still gets the WPM error (the exception says recorded = False); a recording failure never masks or replaces
   the refusal. A refused event is NOT stored unscanned (option (b) of item 6 is not built).
B6. RETRIES: append rescans after WPS17 (identifiers declared meanwhile), up to 7 more times, so one admitted event can
   cost up to 8 budgets. Stated, not changed.
   WORST ADMITTED COST, MEASURED after the code (agent-af5c8cce658ab5a89/budget_worst.py, Mac, real HMAC-SHA256, fake
   lookup): 14,600 tokens "IIa", "IIb", ... (58,399 bytes) have 4,963,088 window keys, just under the event maximum;
   scan() took 14.5 s (2.9 us per window key; 3.9 million distinct keys HMACed in 476 chunks of at most 8,192; peak RSS
   grew by 6 MB). Adding 4,000 bytes puts it over the maximum: refused (WPM02) in 0.19 s. So one crafted event that is
   ADMITTED can still cost about 15 s of matcher CPU in Python plus 476 store lookups and 476 key-store reads (NOT
   MEASURED), and up to 8 times
   that with WPS17 retries; realistic text costs well under a second. This is the trade-off main asked to see, not
   tuned away: holding the 10 times margin at the size bound fixes the event maximum near 4.5 million keys; a smaller
   size bound (16 KiB: maximum about 1.2 million keys, about 4 s) or a per-tenant rate limit would lower it.
   THE RESCAN PAGE: the rescan reads up to 2,000 events per page; with the size bound a page's ciphertext is at most
   about 131 MB (2,000 times 65,564 bytes), decrypted one event at a time. Stated, not changed.
   EQUIVALENCE, CHECKED after the code (agent-af5c8cce658ab5a89/budget_equiv.py): over 3,010 strings (seeded, built
   from I, l, i, 1, 0, o, O, Roman numerals, IJ, fullwidth and Greek capitals, superscript one, circled one, combining
   marks and zero-width space), windows() at 819258c and under b1 are identical; the keys enumerated equal key_count()
   and each token's reading count equals its number of distinct keys (tests/test_ident.py checks 500 such strings), so
   the cap skips exactly the windows it skipped before.
   DECIDED (main, decision 1; was OPEN here at 77fb0e7): the call is bounded in the store path, B10. scan() admits the
   whole batch first and then rebuilds each event's plan as it streams (no plan is kept), so memory is one event's
   tokens plus one chunk.
   ADMISSION DOES NOT DEPEND ON IDENTIFIERS: every event is counted, also in a tenant with no identifiers (819258c did
   no matcher work there), because the erasure rescan later reads those events under the budget (B7).
   NORMAL-PATH COST, MEASURED after the code (agent-af5c8cce658ab5a89/budget_normal.py, Mac, fake lookup; old is
   819258c): scan() of a 170-byte chat message 0.01 ms old against 0.55 ms new with no identifiers, 1.6 against 2.6
   ms with; 3 KB prose 0.01 against 12.9 ms, 18.6 against 32.0 ms; 60 KB prose 0.03 against 198 ms, 304 against 568
   ms. The added cost is tokenising each event again (append admits once before anything, scan admits and plans
   again); append pays one more tokenisation than scan() alone.
   THE FLOOR IS THE COSTLIEST POINT PER BYTE: a 144-byte event of 29 "IIIa"-like tokens has 122,344 keys, under the
   floor, and took 0.50 s to scan. Every chunk also costs one key-store round trip (tenant_hmac_many reads the tenant
   key per call) besides the lookup.
B7. THE ERASURE RESCAN runs under the same budget by construction: it counts each event's keys with the same function
   before enumerating and streams the keys, checking each HMAC against the subject's identifier HMACs (no lookup
   array). Every event admitted under this matcher is within the budget. An event over the current budget can exist
   only if it was admitted under another matcher id (an older, larger budget, or no budget at all: every event
   written before b1), which is possible because a tenant may change matcher while it has no identifiers. Such an
   event makes the rescan raise MatcherBudgetExceeded: the page is not recorded, so WPS24 refuses completion and the
   job records error_type MatcherBudgetExceeded (fail closed). CONSEQUENCE: one such event blocks that subject's
   erasure until an operator acts, indefinitely. Raising the budget strands nothing; lowering it can. Because the
   budget is in the matcher id, WPS38 already stops a tenant WITH identifiers from moving to a new budget (there is no
   migration; m5 item 10). OPTIONS were: (i) stay fail closed (built; visible in error_type); (ii) treat an
   over-budget event as a mention, removed whole with the subject (it cannot be checked; over-erasure of another
   speaker's event, irreversible); (iii) re-admit at migration: a matcher migration refuses to finish while
   over-budget events exist and quarantines them for review.
   DECIDED (main, decision 2; ASSUMPTION under the user's delegation, source: main): (i) now, (iii) at migration. SUCH
   AN ERASURE CANNOT COMPLETE UNTIL A MATCHER MIGRATION RE-ADMITS THE EVENT, AND NO MIGRATION EXISTS YET. Stated in the
   erasure receipt (matcher_budget) and results/README.md.
B8. STATED LIMITATIONS: an all-caps text in which most words carry several I's, longer than about 85 such tokens, is
   refused (the measured 200-token sample is 309,286 keys, over max(131,072, 512 times 200)); lower-casing it or
   splitting it into short events admits it. A code-dense text above 512 keys per token beyond the floor is refused.
   The refusal rate of real traffic is NOT MEASURED (no real corpus here).
B9. TESTS FIRST (red on 819258c by assertion, never by a missing name: the tests reach the new behaviour through
   functions that exist there, scan(), find_spans(), windows() and a counting fake key service, and read the error
   code with getattr): the crafted text (about 4,600 keys per token) is refused with code WPM02 having HMACed no key,
   within an operation ceiling (keys HMACed 0) and a generous time ceiling, with a bounded peak of traced memory;
   realistic prose, all-caps legal text, a long document near the size bound, code blocks and tables are admitted and
   still find a declared name; content one byte over the bound is refused with WPM01 and content at the bound is
   admitted; an admitted adversarial-but-under-budget text never HMACs more than CHUNK keys in one batch nor more than
   its budget in all; the matcher id names the budget; the schema's size constraint equals the bound plus 28; the
   stop-word class test of item 11. Live (tests/test_d3_pg.py, needs the stores): a refused write leaves no events,
   quarantine or subject key row and adds one write_refusals row with its code; the rescan of an over-budget legacy
   event fails closed. The live tests wait for t2-dev space (the run floor and lock rule stand).
B10. PER-CALL LIMIT (main's decision 1, 2026-11-18; ASSUMPTION under the user's delegation, source: main): the design
   aim is that worst-case matcher work is bounded by something the store path enforces, so one call (one append, one
   scan) is bounded, not left to an API layer that does not exist here. In the versioned record (so a change is a
   matcher version change): CALL_EVENTS = 1,000 (the experiments append at most 1,000 events per call:
   exp/erasure_cost.py), CALL_BYTES = 1,048,576 (16 events at the size bound), CALL_KEYS = 5,000,000 (the event
   maximum: a call may do no more matcher work than one worst event). Why a key total and not only counts: one 144-byte
   event under the floor has 122,344 keys, so 1,000 of them (144 KB) would be 122 million keys. Refused with
   CallBudgetExceeded, code WPM03 (count first; then bytes and keys as they add up, naming the event that crossed),
   before any key is HMAC'd, counted in write_refusals like WPM01 and WPM02. The erasure rescan is not a write call
   and has no call limit (it reads a tenant's history page by page, each event under its own budget).
   WORST ADMITTED CALL, MEASURED (agent-af5c8cce658ab5a89/budget_worst_call.py, Mac, real HMAC-SHA256, fake lookup):
   16 events, 1,026,413 bytes, 4,999,038 keys (15 events of 64 KiB two-letter words, the most tokenisation per byte,
   and one crafted event filling the key total): admit() alone 2.8 s, scan() 13.6 s (2.9 million distinct keys HMACed
   in 355 chunks, peak RSS 33 MB), so an append costs about 16 s of matcher CPU (append admits once more than scan),
   up to 8 times that with WPS17 retries; store lookups and key-store reads (355 each) not measured. No per-tenant rate
   limit exists. Tests first (tests/test_ident.py F13CallBudget, red on 77fb0e7 by assertion: 7 failures): 1,001
   events, 17 times 64,000 bytes and 41 floor-shaped events refused with WPM03 and no HMAC, 1,000, 16 and 40 admitted;
   the matcher id names the seven numbers; the store accepts WPM03. Live: tests/test_d3_pg.py B1 counts WPM03.

## Review H: one resource model, unscannable events, F15, F16 (reviewH-1 on 6ddeca3; written before tests and code)

Source: main's relay of reviewH-1 (Codex R3, BLOCK on F13 to F16) and main's decisions on Class A, Class B and F16
(main's decisions under the user's "you decide"; ASSUMPTION, numbers this owner's). Main's order: this section, the
failing tests and the code for F13, F15, F16 and the unscannable rule; then, in a separate note, the seal authority
(option (b), Step 2). ADMISSION IS COST CONTROL UNTIL THE SEAL AUTHORITY LANDS: after this change no erasure depends on
admission having been honest (H3); that admission ran at all is attested by nothing the store can check until Step 2.

### H1. Class A: one resource model

THE MODEL. Every quantity a call holds or does is bounded by what admission already bounds (content bytes, key count)
and ONE structural constant, so that no entry point has a bound of its own to forget:
  T = BUDGET.token_cp = 64: a token is at most 64 code points AS WRITTEN (ignorables removed, combining marks counted)
  AND as identity key. A longer token is OVER-LONG: it is not normalised past the 65th code point, it has no readings,
  and every window containing it is skipped in the count and in enumeration alike (the reading count of an over-long
  token is None, the path a token over P readings already takes, so count equals enumeration by construction). Rule r3
  refuses an identifier with an over-long token, so no approved identifier can match a skipped window (the matching
  window would have to equal the identifier's key token by token). T is in BUDGET and therefore in the matcher id.
From T:
  - a window key is at most L = 4 x 64 + 3 = 259 code points (at most 1,036 bytes as a Python str, 4 bytes per code
    point at worst) and every reading of a token has the length of its identity key (invariant, tested over every
    glyph of both_readings() alone and with marks);
  - planning (tokenising and counting) does constant work per code point of content: no cluster or token longer than
    T is normalised (a 64 KiB single cluster of marks took 3.4 s at 6ddeca3, linear; it is now cut at T);
  - so: DERIVED BYTES HELD AT ONCE <= k x admitted content bytes + C, and WORK <= planning(content bytes) + keys x L,
    where keys <= the budget the call was admitted under. The tests assert the formula on generated worst shapes (peak
    traced memory and counted operations), not a threshold per shape.
Every container that grows with input, and its bound:
  - the call's texts: <= BUDGET.call_bytes (1 MiB) of UTF-8, as Python strs <= 4 MiB; the caller's;
  - one plan (_tokens of one event; plans are rebuilt one event at a time, never kept): proportional to the event's
    code points, at most about 150 bytes per content byte (measured 9.5 MiB for 64 KiB of one-letter tokens);
  - the window cache of _iter_windows: the key lists of the last MAX_N = 4 tokens, each <= P = 4,096 keys of <= T
    code points, so <= 4 x 4,096 x 305 bytes = 5 MB, plus one token's key set while it is built;
  - pending keys in _stream: flushed at CHUNK = 8,192 distinct keys OR CHUNK_TAGS = 65,536 (key, tag) pairs, whichever
    comes first (the pairs bound is new: 1,000 identical events made every pending key carry 1,000 tags); keys <= 8,192
    x 1,036 bytes = 8.5 MB, the labelled copies HMACed the same again, 8,192 HMACs of 64 hex characters 0.9 MB;
  - hits: the value (subject id) or the tag only, never the key; per event at most the subjects it is found to
    mention, which is what the write stores (output-sized, stated, not bounded further);
  - the cluster cache _CLUSTERS: clusters of at most T code points only, its size tracked in bytes (strings counted at
    their str size) and the cache emptied when it passes CLUSTER_CACHE_BYTES = 8 MiB (performance only; not in the
    matcher id);
  - the tables (_TABLES, _CLASSES, _BOTH, _WEIGHTS, _STOP_KEYS): fixed per Unicode version, loaded once.
So one call holds at most about 4 + 10 + 5 + 19 + 8 MiB plus the fixed tables, whatever is admitted.

ENTRY POINTS (input; bytes in; derived bytes held at once; work; where each bound is enforced; the test):

| entry point | input | bytes in | derived held at once | work | enforced | test (worst admitted input, peak and time) |
|---|---|---|---|---|---|---|
| append (log.append, scan) | <= 1,000 events | <= 64 KiB each, 1 MiB per call | one plan + window cache + one flush (H1 list) | planning linear in bytes + <= 5,000,000 keys x L | Python admission (WPM01 to 03) BEFORE the seal; SQL events_content_size; T and the budget in the matcher id | test_ident H13Resource: the F13 shape, a 64 KiB mark cluster, 1,000 identical events, the worst admitted event and call: tracemalloc peak and counted HMACs/canonical code points |
| erasure rescan | the tenant's candidate events, page by page | ids and speakers per page of 2,000; content in sub-batches of <= 16 events or 1 MiB | one sub-batch of ciphertext + one plan + one flush | per event: decrypt + plan (linear in <= 65,564 bytes); enumeration only within the event's budget | SQL events_content_size bounds every stored event (inserted by anyone); the unscannable rule (H3) bounds what admission did not | test_ident H14Unscannable (offline, counting key service: 0 HMACs for an unscannable event); test_d3_pg B4 to B7 (live) |
| identifier proposal (propose, check_identifiers) | <= 64 identifiers per call | <= 256 UTF-8 bytes each, checked before normalisation | the identifiers' tokens (<= 16 KiB of text) | r3 per identifier; capacity saturates above P | Python (count, bytes); SQL ident_enc <= 284 bytes and <= 64 open proposals per subject (WPS39) | test_ident H15Identifiers: 257-byte and 65-identifier calls refused with 0 code points normalised; capacity of 60 I's returns <= 9 P; test_d3_pg (live) the constraints |
| review | one proposal | <= 284 bytes ciphertext, so <= 256 bytes plaintext | one identifier | r3 once | SQL ident_enc constraint (the reviewer reads only stored proposals) | covered by the proposal rows |
| hold place | one subject | ids | one row | lock the subject, insert one row | SQL (no footprint work in the placing transaction, H5) | test_d3_pg H16a (live): placement with 20,000 removed events in the footprint |
| hold report | one hold | ids | <= 20 ids + a count capped at 10,001 | streams the tenant's removed events in the footprint until both limits | SQL LIMIT; its own transaction; re-runnable; failure leaves the hold installed | test_d3_pg H16a, H16b (live): the report under statement_timeout fails, the hold stays, a rerun reports |
| hold release | one hold | ids | one row | lock, update | SQL | existing E tests |
| cascade batch | <= batch candidates (adaptive, <= 5,000) | ids | the batch's footprint | per candidate: its footprint (speaker, mentions array, links) | SQL batch size; the mentions array of an event is NOT bounded (disclosed below) | existing cost runs (exp/erasure_cost.py) |
| completion receipt | one job | none | <= 1,000 ids per list + counts | counts linear in the job's tombstones | SQL LIMIT on every id list in the receipt (H6) | test_d3_pg H6 (live): a receipt with more than 1,000 other-speaker removals |
| inbox enqueue / claim | one payload | <= 64 KiB | one payload | seal | Python WPM01; SQL inbox_payload_size (new; wp_app may also UPDATE inbox, a CHECK covers updates) | test_ident static schema check; test_d3_pg (live) |
| analyst job result | one result | <= 64 KiB | one result | seal | Python WPM01; SQL analyst_jobs_result_size (new; covers UPDATE) | as above |

ROWS WITH NO ENFORCED BOUND (disclosed, not closed here): (a) app-written columns other than the sealed contents
above: ids, idem_key, req_hash, the mentions and input_event_ids arrays, facts.body_enc; a wp_app holder can store large
values in them (PostgreSQL's 1 GB per value). The one that reaches erasure work is events.mentions (the cascade
share-locks every subject it names): an event naming a million subjects costs that cascade batch a million subject
lookups; only existing subjects are locked. (b) Repetition: no per-tenant rate limit on appends, proposals (tenant-wide
open proposals are unbounded; 64 per subject) or holds. (c) Erasure-time work is linear in the tenant's candidate
events and the subject's footprint, by design (D5).

### H2. F13, specifically

The F13 input "I" x 12 + "a" x 65,524 is ONE over-long token: it is skipped with no reading built (0 keys counted and
enumerated, nothing HMACed), where 6ddeca3 built 4,096 keys of 65,536 code points (518.5 MiB traced, 691 MB RSS, 6.9 s).
_stream flushes by pairs as well as keys (H1). _CLUSTERS is bounded in bytes. COST OF T: a token over 64 code points
is never matched, which only matters for an identifier with such a token, now refused by r3 ("a token longer than 64
code points: declare a shorter form"); text cases: a 65-letter run (a hash, a base64 blob, a word glued by a joiner
that is not ignorable) is not searched for a declared name, which is correct for whole-token matching. NOT_COVERED
gains it.

### H3. Class B (F14): an event the rescan cannot scan is treated as containing the subject

Main's decision: erasure must not depend on admission having been honest. RULE: in the erasure rescan, a candidate
event the matcher cannot scan within budget is UNSCANNABLE and is treated as a mention of the subject being erased:
recorded through wp_erasure_add_unscannable (erasure_mentions source 'unscannable', same locks as
wp_erasure_add_mentions), removed whole by the cascade under the same rules as any mention (a legal hold on its speaker,
its mentioned subjects or a linked subject still withholds it), and listed separately in the receipt
(unscannable_removed: count, the first 1,000 ids, and where the full list is). The cost, stated in the receipt: the
other speaker's content in such an event is lost.
WHAT IS UNSCANNABLE (deterministic per event, decided without enumerating): content over the size bound; a key count
over the event's budget (the arithmetic count, H1); ciphertext that does not authenticate under the speaker's key
(InvalidTag) or a nonce/blob too short; plaintext that is not UTF-8. WHAT IS NOT (raises, as before; nothing is
recorded for the page, so WPS24 refuses completion and the job retries): a key-service failure, UCDUnavailable,
MatcherMismatch, any database error. A transient fault must never delete other speakers' content. An event whose
speaker key is gone (speaker erased: content crypto-shredded) is skipped, as before, and that is stated (nothing is
left to read).
PER-EVENT WORK of the rescan is bounded by the SQL size constraint (every stored event, inserted by anyone, is at most
65,564 bytes): decrypt, tokenise and count, linear in that.
WHO CAN MAKE AN UNSCANNABLE EVENT (verified in the schema, not assumed; a static test pins it): wp_app has SELECT and
INSERT on events and no UPDATE; the only UPDATE of events in the schema is the cascade's `content_enc = null`
(wp_erasure_apply, owner). So an event's content never changes after insert, and an honest write under the tenant's
matcher was admitted under the same budget and sealed by the writer: it is never unscannable (property test: any text
admit() accepts is never classified unscannable). An unscannable event is therefore one that a wp_app holder inserted
around admission (or with a bad seal), or one written while the tenant had no matcher by an app under another budget.
It is not limited to the planter's OWN subjects: the app writes for every speaker, so a planted event can carry any
speaker id; whatever it carries is removed. That is the stated cost; Step 2 (the seal authority) removes the bypass.
THE OLD B7 POSITION IS WITHDRAWN: "possible only under another matcher" was false (reviewH F14) and the fail-closed
stall it led to is replaced by this rule; the receipt no longer says that such an erasure cannot complete.
GUARANTEES THAT LIVED ONLY IN PYTHON, each now a SQL constraint or a disclosed non-guarantee (H1 table): event and
quarantine content (SQL, unchanged); inbox payload and analyst job result (SQL, new); proposal ciphertext and open
proposals per subject (SQL, new); identifiers per call and per-call bytes (Python only: cost control, disclosed); the
matcher's key count (Python only until Step 2; erasure no longer depends on it).

### H4. F15: proposals

check_identifiers refuses, before any normalisation or key: more than MAX_IDENTIFIERS = 64 identifiers in one call; an
identifier over MAX_IDENTIFIER_BYTES = 256 UTF-8 bytes (code points checked first, as check_size does). identifier_problem
applies the byte bound too (the reviewer path) and refuses an over-long token. capacity() stops multiplying once the
product passes P (it returns a value above P, at most 9 P, never 3^n). SQL: identifier_proposals_size check
(octet_length(ident_enc) <= 284: 256 plus the 28 bytes of nonce and tag) and wp_identifier_propose refuses a 65th open
('proposed') proposal for one subject (WPS39), counted under the subject lock it already takes.

### H5. F16: the hold is installed alone; the report is separate, bounded and re-runnable

wp_hold_place locks the subject FOR UPDATE (unchanged: the F4 conflict with in-flight removals), inserts the hold with
report_state 'pending' and the SNAPSHOT of that insert statement (pg_current_snapshot(), taken after the lock was
granted, so every removal the hold waited for is visible in it), and returns {hold_id, open_erasure_state,
report_state}. Nothing proportional to the footprint runs in that transaction, so it cannot time out on a large
footprint, and the exclusive lock is held for one insert.
wp_hold_report(hold_id) (wp_legal), its own transaction, no exclusive lock: the footprint events (speaker, mentions,
erasure_mentions links) whose removal is VISIBLE IN THE HOLD'S SNAPSHOT (tombstones.removed_xid, a new xid8 column
defaulting to pg_current_xact_id(), tested with pg_visible_in_snapshot): the first 20 ids by a query with LIMIT 20 and
the count by a subquery with LIMIT 10,001 (reported as "more than 10,000"), one scan with an OR condition over the
three footprint sources (no UNION, so nothing is de-duplicated in memory). It stores the report on the hold row
(report jsonb, report_state 'done') and returns it; it is idempotent and may be re-run. Why the snapshot and not a
timestamp: a column default is evaluated when the removal's row is formed, BEFORE that insert can wait on a lock, so
a removal could carry a time earlier than a hold it commits after (the E1 schedule pauses a batch exactly there);
visibility in the hold's snapshot is the old in-transaction report's semantics exactly (the same pattern as
events.commit_xid). Sources: PostgreSQL 18 documentation 9.27.8, pg_current_snapshot, pg_visible_in_snapshot and
pg_current_xact_id (https://www.postgresql.org/docs/current/functions-info.html, read 2026-11-17, high confidence for
the functions; whether a statement in a READ COMMITTED function sees its own statement's snapshot is general
PostgreSQL behaviour that the live E tests must confirm, medium until they run).
erasure.place_hold places, then reports in a second transaction; if the report fails or times out it returns the hold
with report_state 'pending' and the error's SQLSTATE, and the hold stays. PL/pgSQL cannot catch a statement timeout
with OTHERS (QUERY_CANCELED is excluded), so the two-transaction split is the only way to keep the hold through a
failing report (https://www.postgresql.org/docs/current/plpgsql-control-structures.html, trapping errors, read
2026-11-17, high confidence).
E1 to E8 change only where they read the report: after placement they call wp_hold_report.

### H6. The completion receipt is bounded the same way

wp_erasure_complete aggregated every other-speaker removal id with jsonb_agg in the completing transaction (F16's
shape on the completion path). Every id list in the receipt (other_speakers_removed, unscannable_removed) is the first
1,000 ids by LIMIT plus an exact count, with the statement that the full list is in tombstones by job_id (and
erasure_mentions by source). Main asked for ids: this keeps them, bounded; main is told.

### H7. Minors

schema.sql's D3 comment no longer says "one lock order everywhere" (withdrawn in "F12"); it names the order this path
takes. OVER_MATCH and the receipt gain separator collisions (AB-1001, AB/1001 and AB 1001 have one key: punctuation ends
tokens), with a test. The uncovered-capital count under m5 is 66 (not m4's 63) wherever a number is stated. The
identity-key inclusion invariant is qualified: an identifier's identity key is among the keys of the same text only
for windows within the caps (T code points per token, P readings), which every identifier r3 accepts satisfies.

### H8. Tests first (red on 6ddeca3 by assertion, never by a missing name)

Offline, tests/test_ident.py: H13Resource (the F13 shape scanned with a peak below the model's ceiling and no HMAC; a
64 KiB mark cluster planned with at most T code points of any cluster normalised, counted through a wrapper of
canonical; 1,000 identical events with the peak below the ceiling; the cluster cache under a patched byte limit;
count equals enumeration over strings with over-long tokens; every reading has its key's length); H15Identifiers
(65 identifiers, a 257-byte identifier and a 65-letter token refused, the first two before any normalisation;
capacity saturates); H14Unscannable (stream_matches with an unscannable list: an over-budget item classified with 0
HMACs while the item beside it is still matched; admitted texts never classified; called without the list it still
raises); static schema checks (the new constraints, no UPDATE grant on events for wp_app and no UPDATE of events
content but the cascade's, the unscannable source and function grant, the removal xid, no footprint aggregate in
wp_hold_place, wp_hold_report granted to wp_legal, the bounded receipt lists, no "CANNOT COMPLETE" text, the lock-order
comment, the separator over-match). Live, tests/test_d3_pg.py (needs the stores; red not run on 6ddeca3, stated):
B4 now expects the over-budget legacy event removed, listed and the erasure done while an honest event survives; B5 a
planted over-budget event and B6 a garbage-ciphertext event inserted through wp_app with triggers on, removed and
listed; B7 wp_app cannot UPDATE events; H16a a large footprint (placement quick, report bounded); H16b a report under
statement_timeout fails and the hold stays; H6 a bounded receipt; the new constraints refuse oversize rows.

### H10. Measured after the code (Mac, Python, real HMAC-SHA256, fake lookup; agent-af5c8cce658ab5a89 probes)

| shape | 6ddeca3 | after |
|---|---|---|
| F13 token ("I" x 12 + "a" x 65,524) | 6.9 s, 518.5 MiB traced, 691 MB RSS | 0.52 s, 3.8 MiB (over-long: no reading) |
| one 64 KiB cluster of marks | 3.4 s | 0.08 s (cut at the cap) |
| 1,000 identical events, 3,848 keys each | 20.3 s, 123.0 MiB traced | 7.4 s, 4.6 MiB |
| 64 KiB of one-letter tokens (plan) | 0.9 s, 9.5 MiB | 0.49 s, 9.5 MiB |
| worst event by key bytes (worst_bytes_probe.py) | not measured | 2.1 s, 13.1 MiB traced, 34 MB RSS; 495,418 keys, 144 M code points |
| worst call by key bytes (10 such events) | not measured | 23.7 s scan, 33 MB RSS; 4.93 M keys, 1.44 G code points |
| worst short-key event (budget_worst.py) | 14.5 s | 10.1 s, 27 MB RSS |
| worst short-key call (budget_worst_call.py) | about 16 s | 15.4 s scan + 2.4 s admission, 39 MB RSS |

So memory is now bounded as the model says (every admitted shape measured stayed under 40 MB RSS; the test ceiling is 64 MiB traced),
and the WORST ADMITTED CALL IS THE LONG-KEY SHAPE, ABOUT 24 S OF MATCHER CPU (plus admission and store lookups, not
measured), up to 8 times that with WPS17 retries; there is no per-tenant rate limit. That is the cost of a key-count
budget with keys up to 259 code points; a budget counted in key code points instead would cap it, at the price of
re-deriving every b1 number (not done; stated for main).
CORRECTION (review J M4, probe P2 at f36766c, the reviewer's measurement, not re-run here): the table understates the
worst admitted EVENT. A 65,003-byte event of 10,284 tokens with a 64-code-point token "I" x 12 + "a" x 46 + 6 hex
(4,096 readings) every 80th token is admitted at 4,387,720 keys (budget 5,000,000) and took 28.2 s of matcher time for
ONE event (12.7 MiB traced). Scaled to the call's 5,000,000-key cap that is about 32 s per call, plus about three
tokenisations of the content (admission in the authority, admission again in scan, the plan), the store lookups and the
key-service round trips; a WPS17 retry repeats all of it (up to 8). Not bounded further here: the authority is one
GIL-bound process, so one allowed peer can occupy it for every tenant for that long per call. The clients' 120 s
timeout (review J M11) is set above it. A budget in key code points remains the stated alternative.

### H9. Sources (read 2026-11-17)

- OWASP API Security Top 10 2023, API4 Unrestricted Resource Consumption
  (https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/): limits on memory, CPU,
  payload size and number of operations per request, enforced server side. USED for the per-entry-point table (every
  resource, not only count). High confidence.
- PostgreSQL documentation, functions-info 9.27.8 and plpgsql error trapping (URLs above). USED for H5. High.
- Decompression-bomb guidance (zip-bomb write-ups and safe-unzip libraries: bound the expanded size and the ratio, and
  stream instead of materialising): USED for the shape of the model (bound derived bytes by a ratio of admitted bytes
  and stream products). Medium confidence (secondary sources). REJECTED: vendor-specific numeric thresholds, which
  do not transfer to a matcher.

## Seal authority: "sealed" implies "scanned" (main's option (b); written before tests and code)

Source: main's decision on this owner's writer-scan-claim finding (option (b), in this stream, after F13, F15 and F16;
main's decision under the user's "you decide"; ASSUMPTION, source: main). Today wp_app holds every capability a write
needs: subject keys (it seals), the tenant HMAC (it matches) and INSERT on events (it states what it matched). So
events.mentions, mention_scan_upto, mention_matcher and secret_scan are the writer's word: review H F14 showed a writer
planting content the matcher never admitted. After this change the store refuses any event that was not sealed by ONE
service outside wp_app, which seals only content it has admitted, scanned and secret-checked itself.

### A1. Trust boundary (stated once)

The guarantee is store-side and holds against a wp_app holder who uses every privilege wp_app has (its database role,
its key-service and broker sockets, its files) and deviates from the code in any way. It does NOT hold against: a
compromised seal authority (it holds the seal key, subject keys and plaintext and can seal anything); an application
that copies plaintext before handing it to the authority (exfiltration is not a store property); anyone who can read the
store's private seal schema (wp_owner, a superuser, the operator); a host where the kernel's uid separation fails
(docker access on the lab host is root-equivalent, as H2 states). wp_app still receives subject keys and can read and
re-encrypt any subject's content (app-side decryption is unchanged); what it can no longer do is make the store accept
content the authority did not scan.

### A2. The authority

A sibling service, NOT the D6 broker: the broker holds Elasticsearch manage rights and the broker password; merging
the seal key into it would put two unrelated capabilities behind one identity, and a broker compromise would become a
seal compromise. The authority (wp/sealer.py) reuses the key service's transport exactly (wp/keysvc.py Server: unix
socket, kernel peer uid by SO_PEERCRED, fail-closed allow list WP_SEALER_ALLOW_UIDS = the app's uid), runs as its own
uid (lab: 20005, its own 0700 directory /svc/sealer holding only seal_key, mode 0600, and pgpass.wp_sealer), and is a
client of the key service (its uid added to WP_KEYSVC_ALLOW_UIDS) and of Postgres as wp_sealer (SELECT on
identifier_epochs and subject_identifiers only; nothing it can write).
ONE operation, seal(tenant, lease_scope, lease_epoch, batch_id, events=[{event_id, subject_id, content}]):
  1. admission: the m5 b1 + review H budget (WPM01 to WPM03) over the batch, exactly as M.admit; refused batches get
     the code back and nothing is sealed (the app records the refusal as today);
  2. the secret scanner (wp/secrets.py SEC.check; its version becomes secret_scan): row 19's claim moves here too;
  3. the matcher: identifier-set version v and the tenant's matcher read FIRST, then the identifiers, as M.scan does
     (a declaration in between makes v stale and the store refuses with WPS17); mentions per event, the speaker
     removed, sorted by code point;
  4. encryption under the subject key (K.seal, AAD tenant/subject/event_id, unchanged format);
  5. the attestation per event: seal_mac = HMAC-SHA256(seal key, M) where M is the length-prefixed encoding
     ("<octets>:<value>" per field, so no separator can be forged) of: "wp-seal-v1" (since review J M2: "wp-seal-v2",
     then the purpose, events or quarantine), seal key id, tenant, event_id,
     subject_id, sha256 hex of the ciphertext, v, the matcher id, the secret-scan version, the mentions (count, then
     each), lease_scope, lease_epoch, batch_id, index in batch, batch size.
  Returns {event_id: (ciphertext, mentions, v, matcher, secret_scan, key_id, mac)}. The app inserts exactly that.
WPS17 retry: the app calls seal again (new nonce, new attestation); the cost is stated.

### A3. The store

A private schema wp_seal (owner wp_owner; USAGE granted to nobody) with seal_keys(key_id, ipad, opad, active):
ipad = K XOR (0x36 x 64) and opad = K XOR (0x5c x 64), K being the 32-byte key zero-padded to SHA-256's 64-byte
block (RFC 2104), computed by setup in Python, so the store computes
HMAC-SHA256 as sha256(opad || sha256(ipad || M)) with PostgreSQL's built-in sha256() (no pgcrypto needed). These two
values ARE the key (XOR with a constant): the schema's privileges are what protect it. Not in public, because wp_verifier
holds SELECT on all tables in schema public.
events gains seal_key_id text and seal_mac text; events_guard (SECURITY DEFINER, wp_owner) recomputes M from the NEW
row's own columns and refuses: no attestation (WPS40 seal_missing), an attestation that does not verify under an active
key (WPS41 seal_invalid; the message says only that, never the expected value). The comparison is made between
sha256 of the two MACs, so a timing difference reveals nothing about the expected MAC's prefix. quarantine_guard does
the same for quarantine rows (they carry content and mentions the cascade relies on; a quarantined row is the event's
attestation with the presented, stale epoch). The existing checks stay (digest WPS19, matcher WPS38, scan version
WPS17, secret scan WPS21): the attestation binds the values those checks compare, so they now check authority-written
values. Legacy rows (inserted before the change) are not re-verified; the rescan and the unscannable rule (review H
H3) still cover them, and stay as defence in depth for everything.

### A4. Attacks, one by one

- FORGED CLAIM (mentions dropped, scan version or matcher lied about, secret scan claimed): any changed field changes
  M; without the seal key the app cannot produce the MAC. WPS41.
- MISSING ATTESTATION (the F14 plant, an app that skips the authority): WPS40.
- REPLAY onto another row: event_id is in M (and in the ciphertext's AAD), and unique per tenant: a copy under another
  event_id is WPS41; the same event_id is the existing duplicate path (WPS14/23505). Another tenant: tenant in M, WPS41.
  Another subject: subject in M and the AAD. Another session or epoch: lease_scope and lease_epoch in M, WPS41 (and the
  fence refuses a stale epoch anyway).
- REPLAY of an attestation made against an OLDER IDENTIFIER SET (the version race): v is in M, so it cannot be raised;
  the store refuses v below the current max_seq (WPS17, existing, under the identifier_epochs share lock it already
  takes). Lowering is refused the same way. So the only accepted attestation is one scanned against the current set.
- REPLAY under another matcher or budget: the matcher id (which names the budget and the token cap) is in M; WPS38
  compares it with the tenant's.
- NOT BOUND (review J M2; see "Review J" below): seq, prev_hash, hash, kind, actor, trust_origin, ts_event, idem_key,
  req_hash and the decision fields. A sealed row can be withheld and inserted later at another position of its own
  session chain; batch completeness (all indexes of a batch present) is not checked. Since v2 the target table is
  attested, so an events attestation is refused in quarantine and the reverse (WPS41).
- BATCH: batch_id, index and size are in M, so the attestation names the admission call it came from (audit: a batch
  is visible as admitted together). It bounds nothing across calls: an app may make many calls (no rate limit; the
  authority is the natural place for one, not built here).
- SEAL-CAPABLE KEY FOR wp_app? Every key and key-like capability wp_app can obtain today, enumerated:
  1. subject keys (key service `keys`, also with create): encrypt and decrypt; no MAC.
  2. the tenant key as an HMAC ORACLE (`tenant_hmac`, `tenant_hmac_many` on ANY text) and `chain_key` (an HMAC of the
     tenant key): therefore the seal key must NOT be derived from the tenant key or any key the key service holds; it
     is an independent random 32-byte key, held only by the authority and the store.
  3. key-service `destroy`, `purge`, `is_destroyed`, `key_exists`, `physical_pending`, `describe`: no key material.
  4. the broker's `writer_key` and `eraser_key`: Elasticsearch API keys scoped to aliases; unrelated to the store.
  5. its own pgpass.wp_app and, in a deployment, its ES role key files.
  6. database: SELECT on the tables granted to it; EXECUTE on the functions granted to it. None of them reads wp_seal
     or returns anything computed from it (static test: wp_seal is referenced only by events_guard, quarantine_guard
     and the setup function that installs keys; no function builds dynamic SQL from its arguments).
  The authority's own socket returns attestations for content it admitted and scanned, never the key.
- AUTHORITY DOWN: seal() fails (connection refused or closed); the append raises SealUnavailable and nothing is
  stored. There is no fallback in the app, and none is possible: the store requires the attestation. Writes stop while
  the authority is down: a new availability dependency, stated.
- AUTHORITY LOGS: operation, peer uid, tenant id, batch id, event count, byte total, key count, refusal code, seconds.
  Never content, ciphertext, keys, HMACs, identifiers, mentioned subject ids or event ids.
- KEY HANDLING: setup generates the key (os.urandom(32)), writes it to /svc/sealer/seal_key (0600, the authority's
  uid) and installs ipad/opad in wp_seal.seal_keys as wp_owner; the value is never printed, logged or committed;
  key_id is a random label (not derived from the key) recorded in results. Rotation: a new key_id, both active during
  the switch, the old one deactivated; stated, not built.

### A5. Cost per write

One more unix-socket round trip per append batch (plaintext in, ciphertext out: up to the call limit of 1 MiB each
way) and the authority's own key-service and lookup calls, which today the app makes; the matcher and secret-scan CPU
moves from the app process to the authority (the same work: review H H10). In the store: two sha256 over about 300
bytes and the encoding per event inside events_guard. Measured when the live run is possible (t2-dev below the run
floor at 2026-11-18T01:46Z); NOT MEASURED until then.

### A6. Tests first (step 3), through wp_app with triggers on

Live (tests/test_d3_pg.py, red on 7c90b41 by assertion, run when the floor allows): a valid attestation from an
in-process authority with a test key is accepted; FORGED: the same row with mentions dropped, a raised scan version or
another matcher is WPS41; MISSING: no attestation is WPS40 (the review H F14 plant); REPLAYED: an accepted attestation
reused for another event_id, tenant or session is WPS41; STALE: an attestation made at version v after a declaration
moved the set to v+1 is WPS17, and the same with v rewritten is WPS41; a quarantine row without an attestation is
WPS40. Offline (tests/test_sealer.py, red on 7c90b41): the authority refuses over-budget batches with WPM02 and seals
nothing; changing any attested field changes the MAC; the encoding is length-prefixed (no two field lists encode
alike); its log lines contain no content, key, HMAC or subject id; the app's append raises SealUnavailable when the
socket is absent and never inserts; the seal key is not derivable from any key-service operation (the authority's key
file is the only source); schema static checks (wp_seal private, guards verify, no grant on wp_seal).

### A7. Sources (read 2026-11-18)

- RFC 2104, HMAC (https://www.rfc-editor.org/rfc/rfc2104, read 2026-11-18): ipad 0x36 and opad 0x5C repeated B times, a
  key shorter than B zero-padded, HMAC = H(K xor opad, H(K xor ipad, text)); used as is in the store. High.
- The confused deputy (Hardy 1988) and capability separation, as the pattern behind "the writer must not hold the
  capability that certifies its own writes". Medium (classic paper; applied by analogy).
- Envelope encryption and tokenisation services (cloud KMS documentation: a separate service performs the
  cryptographic operation and never releases its key): USED for the shape (key stays in the service; callers get
  results). Medium. REJECTED: asymmetric signatures, because PostgreSQL cannot verify Ed25519 or ECDSA without an
  extension the lab does not have; and per-tenant seal keys derived from the tenant key (the key service is an HMAC
  oracle for that key, A4 item 2).

### A8. Amendments before the tests (gaps the code exposed; agent-af5c8cce658ab5a89, 2026-11-18)

A3 named only seal_key_id and seal_mac; the guard must recompute M from the row alone, so every attested field needs a
column. Fixed here, before any test or code:

- COLUMNS. events gains seal_key_id, seal_mac, seal_batch (batch_id), seal_index, seal_count. quarantine gains
  event_id, mention_scan_upto, mention_matcher, secret_scan and the same five seal columns, and is unique on
  (tenant_id, event_id); for a quarantine row the guard derives lease_scope as 'session:' || session_id and lease_epoch
  as presented_epoch (the epoch the writer presented, which is what the authority attested). The digest of a
  quarantine row is computed from its content_enc.
- GUARD ORDER. The attestation is checked first, right after the scope check (WPS12), before the fence, so a forged
  row is refused as forged whatever else is wrong with it. A row with neither seal_key_id nor seal_mac is WPS40; any
  other failure is WPS41: a mac that is not 64 hex digits, an unknown or inactive key_id, or ANY attested field that
  is null (the SQL encoding refuses a null element instead of skipping it, since string_agg skips nulls and a skipped
  field would make two rows encode alike). The comparison uses `is distinct from`, so a null can never compare as
  equal. Matcher forgery: an attestation made under matcher X on a row rewritten to the tenant's matcher is WPS41; the
  row left at X verifies and is then refused by the existing matcher check (WPS38).
- ENCODING PARITY. Each field is "<UTF-8 octet length>:<value>"; integers in decimal without sign or padding (Python
  str(int), SQL bigint::text); mentions as their count, then each in stored order (the authority sorts by code
  point). Tests cover a non-ASCII subject id and mention (octets, not characters), empty and multi-element mentions.
- QUARANTINE AND THE IDENTIFIER SET. The erasure rescan reads events only, and the final pass removed quarantine rows
  only by speaker or by mentions: a quarantine row scanned against a set older than the erased subject's identifiers
  kept content that may mention the subject (Class B again: erasure depending on a scan that never covered it). Rule,
  by arithmetic and without decrypting: the final pass also removes the tenant's quarantine rows whose
  mention_scan_upto is below the job's identifier-set version (ident_xmax), unless the row's speaker is under an
  active legal hold. Cost: stale-scanned quarantine rows of other speakers are removed; they are never promoted (an
  unadopted stale write, at most 1,000 per session). Not covered: a held speaker's stale quarantine row is kept and
  not retried after release (quarantine has no withheld list); disclosed. quarantine_guard does not require the
  current version (the stale path runs after a lost lease; refusing there would lose the row), the version rule above
  covers it.
- WHICH SEALER. log.append(..., sealer=None): an explicit sealer is used as given (tests inject an authority with a
  test key, never the lab's key file); otherwise WP_SEALER set means the authority's socket ONLY, never an
  in-process authority, even when a key file is readable; otherwise (operator tools and experiments that hold every
  credential, as keysvc.key_store()) an in-process authority with the key from WP_SECRETS_DIR/seal_key; no socket and
  no key file is SealUnavailable. The app now calls the key service only AFTER the authority sealed (for req_hash,
  without create), so a refused batch still creates no subject key (b1).
- SCHEMA CREATION. schema.sql runs as wp_owner, which cannot create a schema in the database; the admin creates
  wp_seal (authorization wp_owner) next to public, in setup and in every test fixture that builds a database, and a
  reset drops both (otherwise old pads survive a reset). USAGE on wp_seal and EXECUTE on its functions are revoked
  from public explicitly; the install function wp_seal.install(key_id, ipad, opad) is reachable only by its owner.
- NOT CHANGED BY THE AUTHORITY: erasure runs as wp_app, so the rescan's links (wp_erasure_add_mentions only adds)
  remain the eraser's word: an eraser that omits a link leaves a mentioning event. That is a different trust (the
  eraser, not the writer) and out of this design's scope; disclosed.
- LIVE STATUS. t2-dev 16,938,356 KiB free at 2026-11-18T02:18:09Z (floor 20,971,520) and the run lock held by another
  owner: the live S-series is written but NOT RUN, neither red nor green; the offline receipts are not evidence for
  the store-side checks.

## Review J (writepath-review-f36766c, relayed by main 2026-11-18): what changed, what the seal claims, stated limits

Commits on feat/writepath-hardening after the reviewed 5811368: e087433 (D1 to D3), fcb2779 (N1), 8615532 (M2),
b2b31be (N2, M11, M13), bd2fd17 (M8), 6e290ce (amendment H6e, alone), ec86d75 (M7), c8a3206 (M5, M6), and the commit
that carries this section (M10 control, the statements below). Each fix carries a test that fails by assertion on the
tree before it (receipts under results/failing-first/reviewJ/); the live tests run in one window at the final head.

### What the seal attests (M2), stated so that it is true as written
The seal authority's attestation makes the store refuse a row whose CONTENT, MENTIONS AND SCAN CLAIMS (identifier-set
version, matcher, secret-scan version) were not produced by the authority for THIS tenant, event id, speaker, session
scope, lease epoch and TARGET TABLE (events or quarantine, since wp-seal-v2). In those terms a wp_app holder cannot
plant (WPS40), forge (WPS41) or replay an attestation onto another event, tenant, speaker, session, epoch or table
(WPS41), nor below the current identifier set (WPS17). It does NOT bind seq, prev_hash, hash, kind, actor,
trust_origin, ts_event, idem_key, req_hash or the decision fields: those remain the writer's word as before the
authority, and a sealed row can be withheld and inserted later at another position of its own session chain. Batch
completeness is not checked (index and count are attested, never verified as a set). "Cannot plant, forge or
replay rows" without these qualifiers over-claims and is not used.

### Fixed
- N1 (material): quarantine_guard applies WPS38, WPS17 and WPS18 exactly as events_guard; the cascade records the
  event ids of the quarantine rows it removes (quarantine_removed, owner-only, registered) and the guard refuses them
  afterwards (WPS42); the residue counts quarantine rows scanned below the job's identifier set
  (pg:quarantine_unscanned), so a held speaker's stale row makes completion wait. Supersedes the A8 sentence "a held
  speaker's stale quarantine row is kept and not retried after release": it is kept and completion waits for it.
  log.py reseals a fenced (WPS01) batch for quarantine, which is also a fresh scan.
- N2 (material): the transport of the key service, the broker and the authority reads a request line with a bound
  and a deadline from its first byte (idle connections between requests have none), sends replies under the same
  deadline, and caps live connections per peer uid and in all; over a cap the accept loop answers busy before a
  thread starts. Authority: 8 MiB lines (6 x call_bytes for the worst JSON escaping plus 1 KiB per event, asserted),
  30 s, 8 per uid, 32 in all. CORRECTED by review 42cfd11 R2a: "request buffers hold at most 256 MiB" was wrong
  (raw bytes only; a parsed line of empty objects costs about 28 times its size and stayed referenced while the
  connection idled: 1.1 GB with 8 such requests). Superseded by "Review 42cfd11 / R2" below. Key service and broker: 2 MiB, 30 s, 32, 128,
  and the client splits tenant_hmac_many and keys under 1 MiB per request. A per-uid cap still lets one allowed uid
  occupy its own slots (and, through the authority's CPU, every tenant: M4); there is no per-tenant rate limit.
- M11: every client (key service, broker, authority) times out after 120 s (purge: plus its own timeout), closes the
  connection and raises; the appender never blocks forever.
- M13: the socket is created mode 0666 by umask at bind; no chmod by path follows a symlink. NOT CHECKED in code: the
  directory holding the socket must be writable only by the service (an impostor that can bind the path while the
  service is down receives plaintext; it cannot forge attestations). The lab's directories are 0755, service-owned.
- M8: role keys rotate mint-first (successor minted and written, then the name's other active ids invalidated);
  the broker logs and the D6 results carry key-id fingerprints, never ids.
- M5: unregistered() lists large objects, data streams and .ds- backing indices; TEMPORARY is revoked from public on
  both databases; S5 matches unlogged, if-not-exists, uppercase and quoted tables. Dynamic SQL in DO blocks remains
  outside the static check; the runtime gate covers relkinds r, p, v, m, f.
- M6: static S7 (any-case grants on wp_seal, alter default privileges in schema wp_seal, grant wp_owner to); live S8
  covers every login role, including wp_verifier, wp_sealer and wp_broker, and wp_owner membership.
- M7: amendment H6e; C9 opens the three role-key files and seal_key; C14a to C14d.

### Stated limits (not built here)
- Run validity (main's relay, 2026-11-19): the stamped-run validity gate failed its final independent review and was
  dropped. Every stamped write-path run is lab evidence only, and its result document carries the line "run validity
  not independently enforced (validity gate dropped after final review, GoodQA #16096)". Only one home drives the
  emlab2-wphard stores; every live step holds ~/emlab2-16096/RUNLOCK; the owner's chain judge and stop rules are the
  lab's run discipline.
- M1, tenant authority (MATERIAL for client use): the seal authority, the key service and the broker trust every
  allowed peer uid for EVERY tenant. Through the authority an allowed peer can ask whether a text contains another
  tenant's declared identifiers (the mentions reply names subject ids), creates subject keys in any tenant
  (keys(..., create=True)) and receives ciphertext under any tenant's subject keys. In the lab wp_app is one
  all-tenant role with SELECT on subject_identifiers, so nothing new is exposed there; a client deployment needs a
  mapping from peer identity to allowed tenants at all three services (design path item 2).
- M3, symmetric key in the database (MATERIAL for client use): wp_seal.seal_keys holds the HMAC pads, which ARE the
  key. Any Postgres superuser, any physical or base backup, any pg_dump of wp_seal and any replica can mint valid
  attestations; the lab's superuser is the docker host. A client deployment needs an asymmetric signature verified in
  the store (the database holds only a public key) or an HSM/KMS-held key with backups and replicas treated as signing
  material. Server logging of the pads at install, TESTED (window A at 1ffbfa1, 2026-11-19, step M3,
  results/failing-first/live-J/windows-1ffbfa1/A/M3.out): throwaway pads in two install calls that fail on purpose;
  the node's log searched by pad hash: the code's path (parameters sent apart) 0 of 2 pads found, the positive
  control (values inlined into the statement) 2 of 2, 2 errors logged. On this server (log_statement none,
  log_parameter_max_length_on_error 0, log_min_error_statement error) the pads do not reach the log. A server with
  log_statement all or mod and log_parameter_max_length -1, or with log_parameter_max_length_on_error -1 and a
  failing install, would print them: a deployment must keep those settings for the installing session.
- M4: see the CORRECTION under H10: about 28 s for one admitted event, about 32 s per call, more with retries.
- M7: the default role-key mode (WP_BROKER unset) has NO tenant isolation; only broker-minted keys are tenant- and
  epoch-scoped; a broker-mode deployment must not mint or place the role keys where the application runs.
- M9: RH's reds at f36766c show that the hold-report API (wp_hold_report) was absent at that commit
  ('42883' != '57014', None != 'pending'), not the defect itself (a hold rolled back by a slow report). The claim
  rests on the greens (H16a, H16b).
- M10: the PG disk grep finds plaintext stored uncompressed, in line or out of line; a value Postgres compresses
  (about 2 KB and more, pglz) is visible only in its literal runs, so a repeated or long compressible plaintext can be
  missed. MEASURED (window B at 1ffbfa1, P1 toast_control, reported, not judged): a 20 KB compressible value stored
  in 251 bytes with default storage was NOT found by the grep (one copy or three copies of the pattern); the same
  value with storage external (20000 bytes) was found. The grep is blind to compressed plaintext.
- write_refusals (the gate's question, 2026-11-19): no guard triggers BY DESIGN. Counts only (code checked to
  WPM01 to WPM03; no content, subject or event id); no login role may write it; the only writer is
  wp_write_refused, SECURITY DEFINER, owned by wp_owner, search_path pinned, EXECUTE to wp_app alone (public revoked).
  Its upsert needs UPDATE, so a no-update trigger would break it; a no-delete trigger would bind only the owner, who
  can drop triggers. wp_verifier's SELECT on it is the schema's rule that the verifier reads EVERY table in public
  (schema.sql "grant select on all tables in schema public to wp_verifier"): the erasure verifier's residue and
  registry checks must be able to read any table that could hold a mention; the table holds no mention.
- M12 (hypothesis, recorded, not changed): the rescan treats an event that fails authentication (InvalidTag) as
  unscannable and removes it whole. A key service returning a wrong key of valid length (a restore, a bug) would make
  every event of that speaker fail and be removed. Refusing instead when every event of a speaker fails would block
  the erasure (WPS24) on deliberately corrupted rows; the trade-off (content removal versus erasure availability) is
  main's to decide; until then the docstring's "an environmental fault must never remove content" holds for faults
  that raise, not for a wrong key.

## Review 42cfd11 (writepath-review-42cfd11, relayed by main 2026-11-19; written before tests and code)

### R1. The binding-scan invariant (erasure completeness), stated before the fix
Definitions. A row's BINDING SCAN is the one authority scan whose identifier-set version v and mentions M the row's
attestation carries (wp/sealer.py; the store recomputes the MAC, WPS41). max_seq is the tenant's identifier-set
version; it only grows (an erasure deletes identifiers but never lowers it). X's job records ident_xmax = max_seq at
X's mark; the mark sets subjects.erased for X under the subject row lock; the cascade's final pass deletes X's
identifiers only after the rescan has used them.
Invariant. For every path by which content reaches events or quarantine, the row is bound to a scan of THAT content
whose mentions include every subject any earlier scan of the same event (in the same append call) found, and the
store accepts it only if v >= max_seq at insert (WPS17), no subject in M is erased (WPS18) and, for quarantine, the
event id was not removed by a cascade (WPS42). Consequence: once X's erasure is marked, no row can be stored whose
binding scan found X (WPS18, serialised with the mark by the subject lock) or ran below X's declaration (v < X's
declaration seq <= max_seq: WPS17); rows stored before the mark are removed by the cascade (mentions contain X, or the
rescan finds X in an event scanned below ident_xmax, or a quarantine row scanned below ident_xmax), and completion
counts what is left. So a completed erasure of X is not followed by stored content that named X at any scan the
writer obtained for it. What it cannot cover: a scan that runs AFTER the cascade's final pass deleted X's
identifiers cannot find X, so its mentions are [] at a current v and it is accepted in either table (the inherent
limit below).
Paths, from the code (grep of "into events", "into quarantine" in wp/ and exp/; every seal call in wp/log.py):
1. log._append_txn, first seal (log.py seal(), events): bound to that scan; WPS17 or WPS18 at insert. HOLDS.
2. log._append, WPS17 retry (log.py:125-126, events): the reseal is a FRESH scan; at 42cfd11 it drops a subject
   whose identifiers were deleted meanwhile (X erased between the first seal and the retry), so the event is stored
   naming X. VIOLATES the invariant ("every subject an earlier scan found"). Fix: the reseal carries the earlier
   mentions (the authority unions caller-named prior mentions into the attested list; naming more only restricts),
   so WPS18 refuses it.
3. log._append, WPS01 (fenced) branch (log.py:137-141, quarantine): at 42cfd11 a fresh scan; after X's cascade it
   returns [] at a current v, so content that named X at its first scan is quarantined after X's erasure (review
   R1). VIOLATES. Fix: the authority returns BOTH purpose MACs from ONE scan; the fenced branch stores the batch with
   the quarantine MAC of its binding scan (the first, or the last WPS17 reseal, which carries the first's mentions),
   never rescanning; WPS17 or WPS18 then drop it (counted as dropped_stale). A fenced writer has no liveness claim.
4. wp_app direct SQL with a kept attestation (replay): WPS41 (moved fields), WPS17 (older scan), WPS18 (erased
   mention), WPS42 (removed quarantine id), unique event id (one insert per table). HOLDS (review 42cfd11: closed).
5. wp_app direct SQL with a FRESH attestation it requests from the authority for any content: bound to a scan after
   the request. HOLDS for content scanned before X's identifiers were deleted; the limit below for later scans.
6. exp/row6_chain.py inserts as the admin with triggers off: an experiment fixture outside the trust boundary (the
   admin is the store's owner), never a write path of the application. Not covered, by definition.
7. schema.sql wp_erasure_apply (update events set content_enc = null) and the cascade's quarantine delete only
   remove content. Not write paths for content.
Remaining limit, exactly: after the cascade's final pass has deleted X's identifiers, ANY fresh scan of content
naming X finds nothing, so that content is accepted in either table: newly typed content; a WPS17 retry of content
whose first scan ran BEFORE X was declared (nothing to carry forward); content sealed before X's declaration and
first inserted after X's cascade (its stale v is refused WPS17, and its retry is a fresh scan); and any content
wp_app asks the authority to seal. Bound: the scan must start after X's identifiers are gone, so only content
presented to the authority after X's cascade finished (or, for a retry, first scanned before X existed) is
affected; content that any scan found naming X is refused for good. Closing it would need the erased identifiers'
HMACs kept as a deny list, which the design rejects (an erased subject keeps no identifier, DESIGN-NOTES class 3).
Erasure here removes what is stored; it is not a filter on later input.
Behaviour change from the fix (stated, not hidden): a fenced writer is no longer rescanned. Its batch goes to
quarantine under its binding scan or not at all, so a fenced batch whose scan went stale because ANY identifier was
declared meanwhile (not only an erased one) is now dropped and counted (dropped_stale), where 42cfd11 rescanned and
quarantined it. A fenced writer has no liveness claim; the content is still the client's to resend under its new
lease. log.append also refuses (SealError, nothing stored) an authority row that lacks either attestation or a retry
whose mentions are not a superset of the earlier scan's, so a mismatched authority build fails loudly instead of
silently dropping fenced writes or forgetting a mention.

### R2. Transport memory, slots and idle connections (review 42cfd11 R2a, R2b, R2c)
Measured offline on t2-dev with the real wp/keysvc.py Server and the authority's bounds (tests/r2_mem_probe.py, one
process per scenario, peak RSS from getrusage; receipts r2-red-42cfd11 and r2-wt*):

| scenario (review probe) | 42cfd11 (8 MiB lines, no slots) | after |
|---|---|---|
| PC1: one line of empty objects | +217 MiB | +54 MiB |
| PD: 8 such lines one after another, each connection then idle | over 1 GiB (capped) | +62 MiB |
| PC8: 8 at once, dispatch holding each 0.5 s | over 1 GiB (capped) | +96 MiB |
| all 32 connections at once (four uids) | not run | 285 MiB peak RSS |

What changed (wp/keysvc.py Server, wp/sealer.py):
- Nothing of a served request stays referenced while the connection waits: parse, dispatch and reply encoding run
  in one method whose locals end with it, and the handler drops the raw line and the encoded reply.
- Work slots (WORK_SLOTS = 1 for the authority): at most one request is parsed, dispatched and encoded at a time; a
  request waits up to QUEUE_WAIT_S = 60 s for the slot, then gets busy (60 s plus the worst admitted call, about
  32 s, stays under the clients' 120 s). The authority is one GIL-bound process, so this costs little throughput; its
  store lookups no longer overlap.
- Idle deadline (IDLE_TIMEOUT_S = 60 s for the authority): a connection that sends no first byte for 60 s, before
  its first request or between requests, is closed without a reply. An append opens its own connection and closes it.
- Line limit 2 MiB, from need: content travels base64-encoded (content_b64), so a call costs 4 x ceil(call_bytes/3)
  = 1,398,104 bytes of content whatever it holds, plus 256 bytes per event (keys, 32-hex event id, subject id up to
  about 160 bytes) for 1,000 events, plus 256 KiB for a WPS17 retry's prior mentions, plus 4 KiB: 1,920,344 bytes,
  asserted at import and checked by TB6 with the real request line. As JSON text, control characters cost 6 bytes
  each, which is what forced the 8 MiB line.
- Containers: 512 MiB (exp/h_d2_uids.py CONTAINER_MEMORY), sized from the measured bound: 285 MiB transport peak
  with every connection busy plus under 40 MB for the worst admitted call (H10). TB9d asserts that sum stays under 75%
  of 512 MiB. NOT RUN in the container yet (live disk floor); owed: the 512 MiB container test.
- Restart: none, decided. A dead authority fails closed (SealUnavailable: nothing stored) and the run must see it;
  the chain judges oom_kill per step. A client deployment must supervise it with restart and alerting.

Stated limits (not fixed):
- R2b: idle connections still hold slots for up to 60 s each, so eight connections of the app uid, renewed every
  60 s, keep every other writer of that uid at busy; in the lab every tenant shares the app uid, so its slots are
  every tenant's. A client deployment needs a per-tenant identity at the authority and a per-tenant limit.
- R2c: more than 8 concurrent appends from one uid get busy at once (SealUnavailable), and the listen backlog is 5,
  so a burst of connects past it is refused by the kernel; there is no client retry or backoff.
- The key service and the broker share the transport but keep no idle deadline (their clients hold long-lived
  connections) and no work slots (purge can run 60 s). They gain the release of served requests (an idle connection
  pins nothing), but concurrent parses are bounded only by the 2 MiB line, about 57 MiB each at worst: one allowed
  uid with its 32 connections could hold about 1.8 GB, over their 512 MiB containers. Computed from the authority's
  measurement, not measured for them; OPEN, not changed here (same shape as R2a; for main to rate).

### R3. The chain judge (minor)
results/failing-first/harness/step_judge.py (standard library only) judges every unittest step; live_chain.sh calls
it, and amendment H6g states the rule. A pass step (G1, G2, G3; files under expected-pass/) needs exit 0, OK, no
failing block, the pinned "Ran" count and the pinned skip count. A red step (XB, RS, RH; expected-red/) needs a
non-zero exit, exactly its listed "<FAIL|ERROR> <test> :: <text>" blocks with that text in the traceback, the pinned
counts, and no other block (a setUpClass, tearDownClass, setUpModule or import ERROR counts as extra). An empty red
file, an empty step list and an unknown step are refused before the lock. D6 needs the security enable to exit 0 and
the licence line "licence 200 basic active". Offline: tests/test_live_chain_disk.py DR8 to DR22 and StepJudge.
Owed: XB, RS and RH's files were regenerated by `step_judge.py sign` from the windows-1ffbfa1 receipts (Ran 42);
test_d3_pg now has 47 tests, so RS and RH must be re-signed from the next live red of this head before a chain run
judges them (DR19b checks the pass files' counts against the loader; the red files' counts come from a live run).

### R4. Quarantine blast radius and growth (minor)
- pg:quarantine_unscanned counts every quarantine row of the TENANT scanned below the job's identifier set, so one
  held speaker's stale row keeps every erasure job of that tenant at keys_destroyed, reported under this key, not
  under withheld_by_hold (which names the job's own subject). Fail-closed by choice: the row may name any subject and
  is not decrypted to find out. The schema comment now says so. Releasing the hold lets the cascade remove the row and
  every waiting job finish: live test test_n1c2_released_hold_lets_the_blocked_erasure_finish (two blocked erasures,
  hold released, both reach done, the held row removed). NOT RUN yet; owed with n1d to n1g.
- quarantine_removed grows by one row per removed quarantine event id and is never pruned (an event id that comes
  back must stay refused, WPS42). Unbounded, stated. WPS42 is defence in depth: the seal's version check (WPS17) and
  the mention check (WPS18) already refuse a late row under the R1 invariant.

### R5, R6. oom_kill per step, tmpfs timings, stop signals (minor)
- Each step reads memory.events oom_kill of every store container's cgroup before and after; growth on the same
  container id, or a count that cannot be read, fails the step (DR21). Each step line prints it.
- Runs with the stores on tmpfs print a store_fs line: their timings are NOT comparable with disk-backed runs.
- stopsig keeps ignoring later signals once it has begun a stop (by design: a second TERM must not interrupt the
  cleanup). Every step's timeout is now `timeout -k 120`, so a step that ignores TERM is killed 120 s later (DR22).

### R7. Seal pads at a successful install (minor; corrects the M3 limit above)
The M3 limit above is incomplete: with log_min_duration_statement >= 0 and log_parameter_max_length -1 (or any
value long enough), a SUCCESSFUL install prints the pads as bind parameters ("parameters: $1 = ..."), and
log_statement none does not prevent it. wp.setup now installs the pads inside setup.quiet(): one transaction that
first sets LOCAL log_statement none, log_min_duration_statement -1, log_min_duration_sample -1,
log_min_error_statement panic, log_parameter_max_length 0, log_parameter_max_length_on_error 0 and
log_error_verbosity terse (superuser settings, before SET LOCAL ROLE wp_owner). log_transaction_sample_rate logs
every statement of a sampled transaction and is decided when the transaction starts (REL_16_STABLE src/backend/access/transam/xact.c, StartTransaction() assigns
xact_is_sampled from log_xact_sample_rate; read 2026-11-19, high confidence; the documentation only says it applies
to each new transaction), so quiet()
sets it to 0 at session level before BEGIN and restores it after; it refuses a non-autocommit connection. Offline: S-ROLE5. Live, owed: m3_pad_log.py success_control (session at 0 and -1, a statement carrying
fresh pads as text: want 2 hits) and success_quiet (the same inside quiet(): want 0). The live arms use a stand-in
statement, not a second install (an install writes a key row). Not covered: extensions that log on their own
(auto_explain, pgaudit), likely able to print the pads, not verified; a deployment must not load them for the
installing session. That log_statement mod does not log a SELECT that calls install() is from the PostgreSQL
documentation (mod logs DDL and data-modifying statements), not tested.

### R8. D6: C1 B_read, C14c, the control's stated reason (minor)
Amendment H6g (committed alone, before the code). C1 B_read is a search through B's read alias (a write through a
read alias was refused 400 in both arms whatever the key could read). C14c runs before C5 with the still-valid key
against an unused name of A's family without require_alias, and the operator checks no such index exists afterwards;
the key's validity is observed by an own write right after (a refused own write fails C14c; a 401 among the five is
INVALID-REQUEST); the operator's HEAD never raises (an unclear answer fails C14c). -w<E+5> lies outside the key's
descriptor (epoch E only), so its refusal is the key's scope at work, as in C1 and C4; the control does not run C14c.
The old post-CAS writes are reported only. The stated reason "can re-point any tenant's alias" is withdrawn (the
receipts show alias add and the CAS refused 403); the stated limit is now what the receipts show: cross-tenant writes
through existing aliases and cross-tenant mapping updates. Offline: tests/test_d6_r8.py D6R1 to D6R7. Live D6 owed.

### R9. The SCRAM verifier is a statement literal (minor)
ALTER ROLE ... PASSWORD takes no bind parameter, so the verifier is in the statement text. Each such statement now
runs inside setup.quiet() (S-ROLE4). A verifier is not the password, but it allows an offline password guess and lets
its holder pose as the server to a client; it must not reach a log.

### M12 (unchanged, ASSUMPTION)
M12 stays as recorded above: main's decision as coordinator (ASSUMPTION, not the user's), not a user requirement.

### Live results at c6d3e85 (2026-11-19, t2-dev, stores on tmpfs, under the run lock; UNSTAMPED)
run validity not independently enforced (validity gate dropped after final review, GoodQA #16096).
Receipts on t2-dev under ~/emlab2-16096/wphard/windows-c6d3e85/ (L2, R1, R2-container.out).
- R1 red at 42cfd11 (XB step, tree 42cfd11 + tests/test_d3_pg.py@6f7d93f; R1/XB.out): Ran 47, FAILED (failures=3),
  by assertion: n1d and n1e "Tuples differ: (1, 0) != (0, 1)" (a fenced writer's row was stored after the erasure
  instead of being refused), n1f "... is not an instance of MentionsErased" (the retry stored content naming the
  erased subject); the control n1g passed. Green at c6d3e85 (L2/G2.out): Ran 47, OK (n1c2, n1d to n1g included).
- R2 in a 512 MiB container (exp/h_d2_uids.py's image, label and -m 512m, uid 20005; R2-container.out): pc1 1 peak
  95.5 MiB, pd 8 peak 102.9, pc8 8 peak 137.5, pc8 32 peak 285.6 MiB; every request answered; OOMKilled false and
  exit 0 for all four. The probe's server is keysvc.Server with the authority's bounds, not a full authority process.
- R3 chain judge, live: L2 judged every step and failed RS and RH on "Ran 47, expected 42" (exit 3) with the stale
  signatures, which is the judge catching a lost or added test; RS and RH were then re-signed from L2's outputs
  (n1c2's text set by hand from the same traceback). XB red with its signature. Chain L3 at 735e258 (the re-signed
  head; windows-735e258/L3.out): 14 steps, every step as declared, CHAIN OK (exit 0): G1 78, G2 47 and G3 3 OK; XB,
  RS and RH red with their signatures; M3 holds (2/0 on the success arms); D6 enable exit 0, licence 200 basic
  active, fenced 5/5, control 5/5 (h_d6_broker.2f45a50217db6b8a.json, limits without the read claim); oom_kill 0 for
  both stores at every step. Not shown live: a mutated red that the judge must refuse (offline DR11 to DR19 only).
- Receipts copied into results/failing-first/live-J/review42-735e258/ (home paths as ~, the stream's leak scan 0 of
  49, MANIFEST.sha256). Elasticsearch after L3: 60 indices, every one under this stream's wp16096 prefix.
- R5: oom_kill read per step for both stores, 0 throughout (the ES container id changed at D6's enable; the judge
  takes the new id from 0).
- R7 live (L2/M3.out): success_control 2 of 2 pads found, success_quiet 0 of 2, holds true.
- R8 live (L2/D6.out, h_d6_broker.bf45961f0246e00c.json): enable exit 0, licence 200 basic active, fenced 5/5,
  control 5/5 violated; every fenced C14c: 5 writes 403, own write after 201 (key valid), no index of the -w<E+5>
  name afterwards; C1 B_read 403 in both arms. The control's search of B was REFUSED (403), so the limits line of
  that receipt ("reads any tenant") was wrong; corrected in the code after this run (reading is not claimed).
