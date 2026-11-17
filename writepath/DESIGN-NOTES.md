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
- Identifier rule (amendment 11): ASSUMPTION, not a user decision: single-token identifiers without a digit need >= 6
  characters; stop-word-only identifiers are refused. Span redaction is not implemented: a mention removes the event
  whole.
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
removal ... as the only path the app can take". Still ASSUMPTION under decision 10 (the user has not decided it).
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
- Lock order, every path: subject rows (sorted by subject id) -> identifier_epochs (tenant row) -> proposal row ->
  event rows. Mark takes the subject FOR UPDATE; propose, approve/declare, append and cascade take subjects FOR SHARE
  first. Exclusive subject lockers (mark, hold placement) never wait on an event or proposal row, so no cycle exists.
- Pairs tested live: propose x open mark (waits, then WPS03); approve x open mark (waits, then WPS03).

### Class 3 (F3): the matcher version is stored and enforced, never implied
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
- Default_Ignorable_Code_Point is enumerated from the host perl's Unicode data (Unicode::UCD; its version must equal
  Python's unicodedata version: Mac 13.0.0, t2-dev 15.0.0) by `tools/ucd_tables.py`; the table's sha256 is pinned per
  version in `wp/mentions.py`. A missing or different table fails closed (UCDUnavailable): no fallback to matching
  without it. A test enumerates the WHOLE property: every code point, inside a name, still matches with correct offsets.
- Look-alikes (S6): the matching key is a UTS 39 skeleton per cluster, NFKC then NFD, each code point mapped by
  confusables.txt 18.0.0 (2026-08-06, sha256 6ed3ee96..., downloaded with the user's approval), NFD again, then casefold
  and NFKC. Order settled by test: skeleton BEFORE casefold ("AIex Kim" with a capital I matches "Alex Kim" only that
  way). Exact matches are a subset (identical strings have identical skeletons), so only the skeleton HMAC is stored.
- Rule r3 judges BOTH forms: scripts, unassigned code points and digits on the original letters (a skeleton hides mixed
  script); the stop list and the short-token rule on skeletons ("custorner" has the skeleton of "customer").
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
and _shards total = successful = 1, failed = 0, skipped = 0; anything else raises and the job stays open. Source: the
Elasticsearch search API reference and partial-results options (search summary of elastic.co reference pages and a
discuss thread, read 2026-11-17; confidence medium: secondary summary, no Elastic page fetched).

### F10
DESIGN-NOTES line 45 corrected; the receipt lists other speakers' removed events per event.

### Scenario map (reviews B and C)
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
