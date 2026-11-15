-- Write path: the append-only, hash-chained event log is the only truth. No plaintext of any data subject is stored
-- in this database: content columns are AES-GCM ciphertext under a per-subject key held by the key service (wp_keys).
--
-- FENCE BY CONSTRUCTION. Every effect row carries (lease_scope, lease_epoch). A BEFORE trigger on every effect table
-- calls wp_fence(), which takes FOR SHARE on the lease row and raises SQLSTATE WPS01 unless the presented epoch is
-- the current one. A takeover is an UPDATE of that row (epoch+1), so it conflicts with the FOR SHARE: either the
-- effect commits before the takeover, or it sees the new epoch and is refused. No caller can skip it: the trigger
-- runs for every INSERT/UPDATE whatever code path issued it, and lease_scope/lease_epoch are NOT NULL.

create table leases (
  tenant_id text not null, scope text not null,              -- 'session:<id>' or 'projector'
  epoch bigint not null default 0, holder text, expires_at timestamptz,
  primary key (tenant_id, scope)
);

create function wp_fence(t text, sc text, e bigint) returns void language plpgsql as $$
declare cur bigint;
begin
  select epoch into cur from leases where tenant_id = t and scope = sc for share;
  if cur is null or cur <> e then
    raise exception 'stale_epoch scope=% presented=% current=%', sc, e, cur using errcode = 'WPS01';
  end if;
end $$;

create function wp_fence_trg() returns trigger language plpgsql as $$
begin
  perform wp_fence(NEW.tenant_id, NEW.lease_scope, NEW.lease_epoch);
  return NEW;
end $$;

-- Key-free per-subject erasure fence. Appends take FOR SHARE (in the events trigger), erasure takes FOR UPDATE.
create table subjects (
  tenant_id text not null, subject_id text not null, erased boolean not null default false,
  primary key (tenant_id, subject_id)
);

create table chains (
  tenant_id text not null, session_id text not null,
  next_seq bigint not null default 1, last_hash text not null default repeat('0', 64),
  primary key (tenant_id, session_id)
);

create sequence ingest_seq;

create table events (
  tenant_id text not null, session_id text not null, seq bigint not null,
  event_id text not null,                                   -- client-generated, bound into the ciphertext AAD
  idem_key text not null,                                   -- idempotency key: the same key twice is one event
  kind text not null, subject_id text not null,
  actor text not null, trust_origin text not null,
  ts_event timestamptz not null, ts_ingested timestamptz not null default clock_timestamp(),
  content_enc bytea not null, prev_hash text not null, hash text not null,
  decision_item text, based_on_version bigint,              -- decisions only: keyed hash of the item, head version decided against
  source_event_id text,                                     -- decisions only: the user_typed event the decision answers
  lease_scope text not null, lease_epoch bigint not null,
  ingest_seq bigint not null default nextval('ingest_seq'),
  commit_xid bigint not null default (pg_current_xact_id()::text)::bigint,
  primary key (tenant_id, session_id, seq),
  unique (tenant_id, event_id),
  unique (tenant_id, idem_key),
  constraint actor_trust check (
    (actor = 'user' and trust_origin in ('user_typed','user_supplied_external')) or
    (actor = 'assistant' and trust_origin = 'assistant') or
    (actor in ('tool','analyst') and trust_origin in ('tool','web'))),
  constraint decision_shape check (kind <> 'decision' or (decision_item is not null and based_on_version is not null and source_event_id is not null))
);
create index events_cursor on events (tenant_id, commit_xid, ingest_seq);
create index events_subject on events (tenant_id, subject_id);

create function events_guard() returns trigger language plpgsql as $$
declare er boolean; hv bigint; src record;
begin
  perform wp_fence(NEW.tenant_id, NEW.lease_scope, NEW.lease_epoch);
  select erased into er from subjects where tenant_id = NEW.tenant_id and subject_id = NEW.subject_id for share;
  if er is null then raise exception 'unknown_subject %', NEW.subject_id using errcode = 'WPS02'; end if;
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  if NEW.kind = 'decision' then
    -- version-bound decision: compare-and-set on the item head, inside the append
    select trust_origin, session_id into src from events where tenant_id = NEW.tenant_id and event_id = NEW.source_event_id;
    if src is null or src.trust_origin <> 'user_typed' or src.session_id <> NEW.session_id then
      raise exception 'decision_without_user_source' using errcode = 'WPS04';
    end if;
    insert into decision_heads(tenant_id, session_id, item, version) values (NEW.tenant_id, NEW.session_id, NEW.decision_item, 0) on conflict do nothing;
    select version into hv from decision_heads where tenant_id = NEW.tenant_id and session_id = NEW.session_id and item = NEW.decision_item for update;
    if hv <> NEW.based_on_version then
      raise exception 'stale_decision head=% based_on=%', hv, NEW.based_on_version using errcode = 'WPS05';
    end if;
    update decision_heads set version = hv + 1, last_event_id = NEW.event_id
     where tenant_id = NEW.tenant_id and session_id = NEW.session_id and item = NEW.decision_item;
  end if;
  return NEW;
end $$;
create trigger events_guard before insert on events for each row execute function events_guard();

create function append_only() returns trigger language plpgsql as $$
begin raise exception '% is append-only', TG_TABLE_NAME using errcode = 'WPS06'; end $$;
create trigger events_no_update before update or delete on events for each row execute function append_only();
create trigger events_no_truncate before truncate on events for each statement execute function append_only();

create table decision_heads (
  tenant_id text not null, session_id text not null, item text not null,
  version bigint not null default 0, last_event_id text,
  primary key (tenant_id, session_id, item)
);
-- the head moves only inside the events trigger (i.e. only together with a fenced, version-checked decision event)
create function heads_only_from_trigger() returns trigger language plpgsql as $$
begin
  if pg_trigger_depth() < 2 then raise exception 'decision_heads is written only by the events trigger' using errcode = 'WPS06'; end if;
  return coalesce(NEW, OLD);
end $$;
create trigger heads_guard before insert or update or delete on decision_heads for each row execute function heads_only_from_trigger();

-- Stale writes are kept, unadopted: never in the chain, never projected, never sent to an analyst. Ciphertext only.
create table quarantine (
  id bigserial primary key, tenant_id text not null, session_id text not null, subject_id text not null,
  presented_epoch bigint not null, reason text not null, kind text not null, content_enc bytea not null,
  at timestamptz not null default clock_timestamp()
);

create table tombstones (
  tenant_id text not null, event_id text not null, subject_id text not null, job_id text not null,
  primary key (tenant_id, event_id)
);

-- Inbox: items addressed to a session. enqueue/claim/confirm are fenced by the acting session's lease.
create table inbox (
  tenant_id text not null, item_id text not null, target_session text not null, subject_id text not null,
  payload_enc bytea not null, state text not null default 'new' check (state in ('new','claimed','confirmed')),
  claimed_epoch bigint, confirmed_epoch bigint, shown_count int not null default 0,
  lease_scope text not null, lease_epoch bigint not null,
  primary key (tenant_id, item_id)
);
create trigger inbox_fence before insert or update on inbox for each row execute function wp_fence_trg();

-- Analyst jobs: enqueued in the append transaction (fenced by the session lease); claimed with a claim epoch;
-- completed only by the current claimant AND only with a verify result; the store refuses completion over erased inputs.
create table analyst_jobs (
  tenant_id text not null, job_id text not null, idem_key text not null, session_id text not null,
  input_event_ids text[] not null, state text not null default 'queued' check (state in ('queued','claimed','done')),
  claim_epoch bigint not null default 0, claim_expires timestamptz, result_enc bytea, verify jsonb,
  lease_scope text not null, lease_epoch bigint not null,
  primary key (tenant_id, job_id), unique (tenant_id, idem_key),
  constraint done_needs_verify check (state <> 'done' or (verify is not null and (verify->>'ok')::boolean is true and result_enc is not null))
);
create function jobs_guard() returns trigger language plpgsql as $$
begin
  if TG_OP = 'INSERT' then
    perform wp_fence(NEW.tenant_id, NEW.lease_scope, NEW.lease_epoch);
    return NEW;
  end if;
  if NEW.lease_scope <> OLD.lease_scope or NEW.lease_epoch <> OLD.lease_epoch then
    raise exception 'job lease fields are immutable' using errcode = 'WPS07';
  end if;
  if NEW.claim_epoch <> OLD.claim_epoch and (NEW.claim_epoch <> OLD.claim_epoch + 1 or NEW.state <> 'claimed') then
    raise exception 'claim_epoch only advances by one, on a claim' using errcode = 'WPS07';
  end if;
  if OLD.state = 'queued' and NEW.state = 'claimed' and NEW.claim_epoch <> OLD.claim_epoch + 1 then
    raise exception 'claim must advance claim_epoch' using errcode = 'WPS07';
  end if;
  if NEW.state = 'done' then
    if OLD.state <> 'claimed' or NEW.claim_epoch <> OLD.claim_epoch then
      raise exception 'stale_claim' using errcode = 'WPS01';
    end if;
    if exists (select 1 from tombstones tb where tb.tenant_id = NEW.tenant_id and tb.event_id = any(NEW.input_event_ids)) then
      raise exception 'inputs_erased' using errcode = 'WPS03';
    end if;
  end if;
  return NEW;
end $$;
create trigger jobs_guard before insert or update on analyst_jobs for each row execute function jobs_guard();

-- Derived facts (dated, with provenance). Written by the projector-lease holder.
create sequence fact_rev;
create table facts (
  tenant_id text not null, fact_id text not null, subject_id text not null,  -- subject_id = speaker whose key encrypts it
  slot text not null,                                                        -- keyed hash of (entity, attribute)
  body_enc bytea not null, valid_from timestamptz not null, valid_to timestamptz,
  input_event_ids text[] not null, rev bigint not null default nextval('fact_rev'),
  changed_xid bigint not null default (pg_current_xact_id()::text)::bigint,
  lease_scope text not null, lease_epoch bigint not null,
  primary key (tenant_id, fact_id)
);
create index facts_inputs on facts using gin (input_event_ids);
create index facts_cursor on facts (tenant_id, changed_xid, rev);
create trigger facts_fence before insert or update on facts for each row execute function wp_fence_trg();

-- Projection cursors: one per (tenant, family). Advancing is fenced by the projector lease.
create table cursors (
  tenant_id text not null, family text not null, cx bigint not null default 0, cs bigint not null default 0,
  lease_scope text not null, lease_epoch bigint not null,
  primary key (tenant_id, family)
);
create trigger cursors_fence before insert or update on cursors for each row execute function wp_fence_trg();

-- Registry mirror (synced from wp/registry.py at setup): the store-side list the erasure completion is checked against.
create table store_registry (name text primary key, verifiable boolean not null);

-- Erasure: the request is recorded before anything else; phase 1 (tombstones + lease bump) is one transaction;
-- 'done' is accepted by the store only with a verify count of zero for EVERY verifiable registered store.
create table erasure_jobs (
  job_id text primary key, tenant_id text not null, subject_id text not null,
  state text not null default 'requested' check (state in ('requested','fenced','purged','keys_destroyed','done')),
  event_ids text[] not null default '{}', verify jsonb, attempts int not null default 0, last_error_type text,
  lease_scope text not null, lease_epoch bigint not null,
  created_at timestamptz not null default clock_timestamp(), updated_at timestamptz not null default clock_timestamp()
);
create function erasure_guard() returns trigger language plpgsql as $$
declare missing text; nonzero text;
begin
  if TG_OP = 'UPDATE' then
    perform wp_fence(NEW.tenant_id, NEW.lease_scope, NEW.lease_epoch);
  end if;
  if NEW.state = 'done' then
    if NEW.verify is null then raise exception 'done_without_verify' using errcode = 'WPS08'; end if;
    select string_agg(name, ',') into missing from store_registry r where r.verifiable and not (NEW.verify ? r.name);
    if missing is not null then raise exception 'done_missing_stores %', missing using errcode = 'WPS08'; end if;
    select string_agg(r.name, ',') into nonzero from store_registry r
      where r.verifiable and (jsonb_typeof(NEW.verify->r.name) <> 'number' or (NEW.verify->>r.name)::bigint <> 0);
    if nonzero is not null then raise exception 'done_with_residue %', nonzero using errcode = 'WPS08'; end if;
  end if;
  return NEW;
end $$;
create trigger erasure_guard before insert or update on erasure_jobs for each row execute function erasure_guard();


-- Every content-bearing insert for a subject checks the erasure fence (FOR SHARE vs erasure's FOR UPDATE), not only events.
create function subject_not_erased() returns trigger language plpgsql as $$
declare er boolean;
begin
  select erased into er from subjects where tenant_id = NEW.tenant_id and subject_id = NEW.subject_id for share;
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  return NEW;
end $$;
create trigger inbox_subject before insert on inbox for each row execute function subject_not_erased();
create trigger facts_subject before insert on facts for each row execute function subject_not_erased();
create trigger quarantine_subject before insert on quarantine for each row execute function subject_not_erased();
