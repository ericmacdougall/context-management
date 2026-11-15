-- Source of truth for the memory write path. Plaintext of subject-keyed content
-- never reaches Postgres storage: events and fact objects are AES-encrypted
-- (pgcrypto) under a per-data-subject key that lives in `keys`.
create extension if not exists pgcrypto;

create table keys (
  tenant_id text not null, subject_id text not null,
  key bytea, created_at timestamptz not null default now(), destroyed_at timestamptz,
  primary key (tenant_id, subject_id)
);

-- Encrypt under the subject's key. FOR SHARE on the key row is the commit-time
-- fence: erasure takes FOR UPDATE on the same row, so a derived write either
-- commits before erasure (and is purged by it) or fails after it.
create function mem_enc(t text, s text, plain text) returns bytea language plpgsql as $$
declare k bytea; d timestamptz; iv bytea;
begin
  insert into keys(tenant_id, subject_id, key) values (t, s, gen_random_bytes(32)) on conflict do nothing;
  select key, destroyed_at into k, d from keys where tenant_id = t and subject_id = s for share;
  if d is not null or k is null then raise exception 'subject_erased: %', s using errcode = 'P0001'; end if;
  iv := gen_random_bytes(16);
  return iv || encrypt_iv(convert_to(plain, 'UTF8'), k, iv, 'aes-cbc/pad:pkcs');
end $$;

-- NULL when the key is destroyed (content unreadable).
create function mem_dec(t text, s text, c bytea) returns text language sql stable as $$
  select case when k.destroyed_at is null and k.key is not null
    then convert_from(decrypt_iv(substring(c from 17), k.key, substring(c from 1 for 16), 'aes-cbc/pad:pkcs'), 'UTF8') end
  from keys k where k.tenant_id = t and k.subject_id = s
$$;

create table sessions (
  tenant_id text not null, session_id text not null,
  next_seq bigint not null default 1, last_hash text not null default repeat('0', 64),
  primary key (tenant_id, session_id)
);

create sequence ingest_seq;

create table events (
  tenant_id text not null, session_id text not null, seq bigint not null,
  event_id text not null unique,
  ingest_seq bigint not null default nextval('ingest_seq'),
  commit_xid bigint not null default (pg_current_xact_id()::text)::bigint,
  ts_event timestamptz not null, ts_ingested timestamptz not null default clock_timestamp(),
  subject_id text not null, actor text not null,
  trust_origin text not null check (trust_origin in ('user_typed','user_supplied_external','assistant','tool','web')),
  kind text not null,
  content_enc bytea not null, prev_hash text not null, hash text not null,
  primary key (tenant_id, session_id, seq)
);
create index events_cursor on events (tenant_id, commit_xid, ingest_seq);
create index events_subject on events (tenant_id, subject_id);

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

create table facts (
  tenant_id text not null, recipe_version int not null, id text not null,
  subject text not null, predicate text not null, scope text not null default '*',
  object_enc bytea not null, modality text not null, author text not null, trust_origin text not null,
  valid_from timestamptz not null, valid_to timestamptz, status text not null default 'new',
  input_event_ids text[] not null, subject_id text not null, order_key text not null,
  cue text,
  rev bigint not null default nextval('fact_rev'),
  changed_xid bigint not null default (pg_current_xact_id()::text)::bigint,
  primary key (tenant_id, recipe_version, id)
);
create index facts_cursor on facts (tenant_id, recipe_version, changed_xid, rev);
create index facts_slot on facts (tenant_id, recipe_version, subject, predicate, scope);
create index facts_inputs on facts using gin (input_event_ids);

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

create table es_buckets (
  tenant_id text not null, bucket int not null, gen int not null default 1,
  primary key (tenant_id, bucket)
);

create table erasure_receipts (
  receipt_id text primary key, tenant_id text not null, subject_id text not null,
  doc jsonb not null, created_at timestamptz not null default now()
);
