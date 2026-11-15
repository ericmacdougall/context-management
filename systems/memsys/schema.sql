-- Source of truth for the memory write path (events database). This database holds NO key material and no
-- subject-keyed plaintext: events and fact text are ciphertext made by the key service (kms.py).

-- Key-free fence row per data subject. Appends and derivation take FOR SHARE and refuse an erased subject;
-- erasure takes FOR UPDATE and flips `erased`. Lives here so the fence is transactional with the writes it protects.
create table subjects (
  tenant_id text not null, subject_id text not null,
  erased boolean not null default false, epoch bigint not null default 0,
  primary key (tenant_id, subject_id)
);

create table sessions (
  tenant_id text not null, session_id text not null,
  next_seq bigint not null default 1, last_hash text not null default repeat('0', 64),
  primary key (tenant_id, session_id)
);

create sequence ingest_seq;

create table events (
  tenant_id text not null, session_id text not null, seq bigint not null,
  event_id text not null unique,
  client_event_id text,
  ingest_seq bigint not null default nextval('ingest_seq'),
  commit_xid bigint not null default (pg_current_xact_id()::text)::bigint,
  ts_event timestamptz not null, ts_ingested timestamptz not null default clock_timestamp(),
  subject_id text not null, actor text not null,
  trust_origin text not null check (trust_origin in ('user_typed','user_supplied_external','assistant','tool','web')),
  kind text not null,
  content_enc bytea not null, prev_hash text not null, hash text not null,
  primary key (tenant_id, session_id, seq),
  -- trust is a function of who produced the event: an assistant or tool event can never carry user_typed
  constraint actor_trust_consistent check (
    (actor = 'user' and trust_origin in ('user_typed','user_supplied_external')) or
    (actor = 'assistant' and trust_origin = 'assistant') or
    (actor = 'tool' and trust_origin in ('tool','web')))
);
create index events_cursor on events (tenant_id, commit_xid, ingest_seq);
create index events_subject on events (tenant_id, subject_id);
create unique index events_client_id on events (tenant_id, client_event_id) where client_event_id is not null;

create function events_append_only() returns trigger language plpgsql as $$
begin raise exception 'events is append-only'; end $$;
create trigger events_no_update before update or delete on events for each row execute function events_append_only();
create trigger events_no_truncate before truncate on events for each statement execute function events_append_only();

create table tombstones (
  tenant_id text not null, event_id text not null, subject_id text not null,
  receipt_id text not null, erased_at timestamptz not null default now(),
  primary key (tenant_id, event_id)
);

create sequence fact_rev;

-- Slot text (subject, predicate, scope, cue) and object are ciphertext under the speaker's key. slot_hash and
-- entity_hash are keyed hashes (tenant key in the key service) used for grouping, locking and concept ids.
create table facts (
  tenant_id text not null, recipe_version int not null, id text not null,
  slot_hash text not null, entity_hash text not null,
  slot_enc bytea not null, object_enc bytea not null,
  modality text not null, author text not null, trust_origin text not null,
  valid_from timestamptz not null, valid_to timestamptz, status text not null default 'new',
  input_event_ids text[] not null, subject_id text not null, order_key text not null,
  rev bigint not null default nextval('fact_rev'),
  changed_xid bigint not null default (pg_current_xact_id()::text)::bigint,
  primary key (tenant_id, recipe_version, id)
);
create index facts_cursor on facts (tenant_id, recipe_version, changed_xid, rev);
create index facts_slot on facts (tenant_id, recipe_version, slot_hash);
create index facts_entity on facts (tenant_id, recipe_version, entity_hash);
create index facts_inputs on facts using gin (input_event_ids);

-- concept_id is the entity_hash: no plaintext in the row.
create table concepts (
  tenant_id text not null, concept_id text not null,
  dirty boolean not null default true, dirty_since timestamptz default now(),
  last_activity timestamptz default now(), next_fold_at timestamptz default now(),
  gen bigint not null default 1, folded_gen bigint not null default 0,
  claim_token text, lease_until timestamptz, pending_since timestamptz, fold_count int not null default 0,
  input_event_ids text[] not null default '{}', input_fact_ids text[] not null default '{}', digest text,
  primary key (tenant_id, concept_id)
);
create index concepts_due on concepts (next_fold_at) where dirty;
create index concepts_inputs on concepts using gin (input_event_ids);

create table projection_state (
  view text not null, version int not null, tenant_id text not null,
  hwm_xid bigint not null default 0, hwm_seq bigint not null default 0,
  updated_at timestamptz not null default now(),
  primary key (view, version, tenant_id)
);

-- cache of the current generation per index family (the ES alias names are the authority; see esx.ensure_*)
create table es_buckets (
  tenant_id text not null, bucket int not null, gen int not null default 1,
  primary key (tenant_id, bucket)
);

create table erasure_receipts (
  receipt_id text primary key, tenant_id text not null, subject_id text not null,
  doc jsonb not null, created_at timestamptz not null default now()
);

-- Durable erasure state: written in the phase-1 transaction, completed idempotently by erase_subject or the sweeper.
create table erasure_jobs (
  job_id text primary key, tenant_id text not null, subject_id text not null,
  state text not null default 'pending' check (state in ('pending','key_destroyed','es_purged','done')),
  event_ids text[] not null, event_ts timestamptz[] not null,
  attempts int not null default 0, last_error text,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create index erasure_jobs_open on erasure_jobs (tenant_id) where state <> 'done';
