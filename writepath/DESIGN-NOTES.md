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
- Physical purge (amendment 9): "logically erased" is reported at once; "done" waits for the physical purge, bounded by
  the store deadline (300 s). The ES floor is the retention-lease sync (about 30 s). With a real KMS the key's physical
  deletion is the KMS's own record and its pending-deletion window, which may be days: the receipt must then say
  "logically erased, key deletion scheduled until <date>".
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
- Projector start race (amendment 19, exp/projector_start_race.py):
  - Fixed in 5d26746: a projector that loses the start-up race to a forced takeover now drops the lease and returns -1
    from step(). Until then, the StaleEpoch (alias compare-and-set) or StaleWrite (cursor insert) raised inside start()
    escaped step() and run() and killed the losing process.
  - Safety was never affected: no stale write reached Elasticsearch.
  - Residual, not fixed: families.fence_es turns every ESError on the alias request into StaleEpoch, not only a lost
    compare-and-set. This includes a flood-stage write block or a cluster error.
    - Since 5d26746, such an error inside start() also returns -1 instead of crashing.
    - The lease row stays held under the dropped epoch until its TTL (30 s).
    - The projector's own next acquire then finds that live lease, so run() can return True as if idle.
    - Hard bulk errors in the main loop already behaved this way before 5d26746.
    - Only errors outside ESError and the stale types propagate. Examples are the RuntimeError for a family with other
      than one index, or an ESError from ensure_family; the lease just taken then also stays held until its TTL.
    - Needed: tell a lost CAS apart from an ES failure, and release or report the latter.
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
