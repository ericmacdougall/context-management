-- Write path: the append-only, hash-chained event log is the only truth. No plaintext of any data subject is stored
-- in this database: content columns are AES-GCM ciphertext under a per-subject key held by the key service (wp_keys).
--
-- ROLES (created by wp/setup.py; this file runs under SET ROLE wp_owner, so wp_owner owns every object):
--   wp_owner     NOLOGIN. Owns the tables. Only the SECURITY DEFINER functions below act as wp_owner.
--   wp_app       the application. Not an owner, not a superuser: it cannot DISABLE TRIGGER or set
--                session_replication_role, has no DELETE or TRUNCATE privilege anywhere, and no write privilege on
--                leases, subjects, chains, tombstones, decision_heads, store_registry, quarantine_quota.
--   wp_verifier  completes erasure jobs (wp_erasure_complete); otherwise read-only.
--
-- FENCE BY CONSTRUCTION. Every effect row carries (lease_scope, lease_epoch). A BEFORE trigger on every effect table
-- checks that the scope is the one BOUND TO THE TABLE AND ROW (projector for facts/cursors, session:<the row's
-- session> for events/inbox/analyst_jobs) and calls wp_fence(), which takes FOR SHARE on the lease row and raises
-- WPS01 unless the presented epoch is current. A takeover is an UPDATE of that row (epoch+1). Leases are written only
-- by the wp_lease_* functions. Every table has guard triggers for INSERT, UPDATE, DELETE and TRUNCATE, enabled
-- ALWAYS (they fire even under session_replication_role=replica). exp/guard_check.py enumerates pg_class/pg_trigger
-- and fails on any table without them.

create domain tenant_t as text check (value ~ '^[a-z0-9_]{1,40}$');

-- ------------------------------------------------------------------ generic guards
create function wp_owner_only() returns trigger language plpgsql as $$
begin
  if current_user <> 'wp_owner' then
    raise exception '% on % only through owner functions', TG_OP, TG_TABLE_NAME using errcode = 'WPS10';
  end if;
  if TG_LEVEL = 'STATEMENT' then return null; end if;
  if TG_OP = 'DELETE' then return OLD; end if;
  return NEW;
end $$;

create function wp_no_truncate() returns trigger language plpgsql as $$
begin raise exception 'TRUNCATE % refused', TG_TABLE_NAME using errcode = 'WPS11'; end $$;

create function wp_append_only() returns trigger language plpgsql as $$
begin raise exception '% is append-only (% refused)', TG_TABLE_NAME, TG_OP using errcode = 'WPS06'; end $$;

-- ------------------------------------------------------------------ leases
create table leases (
  tenant_id tenant_t not null, scope text not null check (scope ~ '^(projector|session:[A-Za-z0-9_]{1,64})$'),
  epoch bigint not null default 0, holder text, expires_at timestamptz,
  primary key (tenant_id, scope)
);

create function wp_fence(t text, sc text, e bigint) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare cur bigint;
begin
  select epoch into cur from leases where tenant_id = t and scope = sc for share;
  if cur is null or cur <> e then
    raise exception 'stale_epoch scope=% presented=% current=%', sc, e, cur using errcode = 'WPS01';
  end if;
end $$;

-- TG_ARGV[0] = 'projector' or 'session'; TG_ARGV[1] = the column naming the session the row belongs to.
create function wp_fence_trg() returns trigger language plpgsql as $$
declare want text;
begin
  if TG_ARGV[0] = 'projector' then want := 'projector';
  else want := 'session:' || (to_jsonb(NEW) ->> TG_ARGV[1]); end if;
  if NEW.lease_scope is distinct from want then
    raise exception 'scope_not_bound table=% presented=% required=%', TG_TABLE_NAME, NEW.lease_scope, want using errcode = 'WPS12';
  end if;
  perform wp_fence(NEW.tenant_id, NEW.lease_scope, NEW.lease_epoch);
  return NEW;
end $$;

create function wp_lease_acquire(t tenant_t, sc text, ttl_s double precision, force boolean, who text, lock_ms int)
returns table(epoch bigint, at timestamptz) language plpgsql security definer set search_path = public, pg_temp as $$
begin
  perform set_config('lock_timeout', greatest(coalesce(lock_ms, 2000), 1)::text, true);
  insert into leases(tenant_id, scope) values (t, sc) on conflict do nothing;
  return query update leases l set epoch = l.epoch + 1, holder = who, expires_at = clock_timestamp() + make_interval(secs => ttl_s)
    where l.tenant_id = t and l.scope = sc and (force or l.expires_at is null or l.expires_at < clock_timestamp())
    returning l.epoch, clock_timestamp();
end $$;

create function wp_lease_renew(t tenant_t, sc text, e bigint, who text, ttl_s double precision) returns boolean
language sql security definer set search_path = public, pg_temp as $$
  with u as (update leases set expires_at = clock_timestamp() + make_interval(secs => ttl_s)
             where tenant_id = t and scope = sc and epoch = e and holder = who returning 1)
  select exists (select 1 from u);
$$;

create function wp_lease_release(t tenant_t, sc text, e bigint, who text) returns void
language sql security definer set search_path = public, pg_temp as $$
  update leases set expires_at = clock_timestamp() - interval '1 second' where tenant_id = t and scope = sc and epoch = e and holder = who;
$$;

-- ------------------------------------------------------------------ subjects (erasure fence) and chains
create table subjects (
  tenant_id tenant_t not null, subject_id text not null, erased boolean not null default false,
  primary key (tenant_id, subject_id)
);

create function wp_subjects_ensure(t tenant_t, ss text[]) returns void language sql security definer set search_path = public, pg_temp as $$
  insert into subjects(tenant_id, subject_id) select t, s from unnest(ss) s on conflict do nothing;
$$;

-- Declared identifiers of a subject (names, handles, customer numbers), stored ONLY as HMAC under the tenant key of
-- the normalised token sequence (wp/mentions.py). One identifier names one subject in a tenant (ambiguity is refused).
-- identifier_epochs.max_seq is the tenant's identifier-set version: events_guard refuses an event scanned against an
-- older version (WPS17), so after a declaration commits no event can be written without being matched against it.
create sequence ident_seq;
create table identifier_epochs (tenant_id tenant_t primary key, max_seq bigint not null default 0);
create table subject_identifiers (
  tenant_id tenant_t not null, ident_hmac text not null, subject_id text not null, declared_seq bigint not null,
  primary key (tenant_id, ident_hmac)
);
create index subject_identifiers_subject on subject_identifiers (tenant_id, subject_id);
create function wp_identifier_declare(t tenant_t, s text, hmacs text[]) returns bigint language plpgsql security definer set search_path = public, pg_temp as $$
declare n bigint; er boolean;
begin
  insert into subjects(tenant_id, subject_id) values (t, s) on conflict do nothing;
  select erased into er from subjects where tenant_id = t and subject_id = s for update;
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  insert into identifier_epochs(tenant_id) values (t) on conflict do nothing;
  perform 1 from identifier_epochs where tenant_id = t for update;
  n := nextval('ident_seq');
  insert into subject_identifiers(tenant_id, ident_hmac, subject_id, declared_seq) select t, h, s, n from unnest(hmacs) h
    on conflict (tenant_id, ident_hmac) do nothing;
  if exists (select 1 from subject_identifiers where tenant_id = t and ident_hmac = any(hmacs) and subject_id <> s) then
    raise exception 'identifier_names_another_subject' using errcode = 'WPS20';
  end if;
  update identifier_epochs set max_seq = n where tenant_id = t;
  return n;
end $$;

create table chains (
  tenant_id tenant_t not null, session_id text not null,
  next_seq bigint not null default 1, last_hash text not null default repeat('0', 64),
  primary key (tenant_id, session_id)
);

create sequence ingest_seq;

-- ------------------------------------------------------------------ events
create table events (
  tenant_id tenant_t not null, session_id text not null, seq bigint not null,
  event_id text not null,                                   -- client-generated, bound into the ciphertext AAD
  idem_key text not null,                                   -- client idempotency key, unique per SESSION
  req_hash text not null,                                   -- HMAC of the request under the SUBJECT key (erasure makes it unconfirmable)
  kind text not null, subject_id text not null,
  actor text not null, trust_origin text not null,
  ts_event timestamptz not null, ts_ingested timestamptz not null default clock_timestamp(),
  content_enc bytea,                                        -- NULL only after erasure redaction (wp_erasure_cascade)
  content_digest text not null,                             -- sha256 of the ciphertext; the chain hashes THIS, so redaction keeps the chain verifiable
  mentions text[] not null default '{}',                    -- subjects whose declared identifiers the write-time matcher found
  mention_scan_upto bigint not null default 0,              -- identifier-set version the matcher ran against
  prev_hash text not null, hash text not null,
  decision_item text, decision_action text, based_on_event_id text, source_event_id text,
  lease_scope text not null, lease_epoch bigint not null,
  ingest_seq bigint not null default nextval('ingest_seq'),
  commit_xid bigint not null default (pg_current_xact_id()::text)::bigint,
  primary key (tenant_id, session_id, seq),
  unique (tenant_id, event_id),
  unique (tenant_id, session_id, idem_key),
  constraint actor_trust check (
    (actor = 'user' and trust_origin in ('user_typed','user_supplied_external')) or
    (actor = 'assistant' and trust_origin = 'assistant') or
    (actor in ('tool','analyst') and trust_origin in ('tool','web'))),
  constraint decision_shape check (kind <> 'decision' or (decision_item is not null and source_event_id is not null
                                   and decision_action in ('approve','amend','reject','execute')))
);
create index events_cursor on events (tenant_id, commit_xid, ingest_seq);
create index events_subject on events (tenant_id, subject_id, event_id);
create index events_mentions on events using gin (mentions);
create index events_scan on events (tenant_id, mention_scan_upto);
create unique index decision_source_once on events (tenant_id, source_event_id) where kind = 'decision';

create table decision_heads (
  tenant_id tenant_t not null, item text not null,          -- tenant-wide: an amend in any session moves the head
  version bigint not null default 0, last_event_id text, last_action text,
  primary key (tenant_id, item)
);

create function events_guard() returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
declare er boolean; ch record; src record; hd record; prior record; mx bigint;
begin
  if NEW.lease_scope is distinct from 'session:' || NEW.session_id then
    raise exception 'scope_not_bound table=events presented=% required=session:%', NEW.lease_scope, NEW.session_id using errcode = 'WPS12';
  end if;
  perform wp_fence(NEW.tenant_id, NEW.lease_scope, NEW.lease_epoch);
  select erased into er from subjects where tenant_id = NEW.tenant_id and subject_id = NEW.subject_id for share;
  if er is null then raise exception 'unknown_subject' using errcode = 'WPS02'; end if;
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  if NEW.content_enc is null or NEW.content_digest is distinct from encode(sha256(NEW.content_enc), 'hex') then
    raise exception 'content_digest_mismatch' using errcode = 'WPS19';
  end if;
  -- mentions: the event must have been matched against the CURRENT identifier set, and may not mention an erased subject
  insert into identifier_epochs(tenant_id) values (NEW.tenant_id) on conflict do nothing;
  select max_seq into mx from identifier_epochs where tenant_id = NEW.tenant_id for share;
  if mx > NEW.mention_scan_upto then
    raise exception 'mention_scan_stale scanned=% current=%', NEW.mention_scan_upto, mx using errcode = 'WPS17';
  end if;
  if cardinality(NEW.mentions) > 0 then
    perform 1 from subjects where tenant_id = NEW.tenant_id and subject_id = any(NEW.mentions) for share;
    if exists (select 1 from subjects where tenant_id = NEW.tenant_id and subject_id = any(NEW.mentions) and erased) then
      raise exception 'mentions_erased_subject' using errcode = 'WPS18';
    end if;
  end if;
  -- idempotency: the same key with different content is an error, never a silent duplicate
  select req_hash, event_id into prior from events where tenant_id = NEW.tenant_id and session_id = NEW.session_id and idem_key = NEW.idem_key;
  if found then
    if prior.req_hash <> NEW.req_hash then
      raise exception 'idempotency_key_reused_with_different_request' using errcode = 'WPS09';
    end if;
    raise exception 'duplicate %', prior.event_id using errcode = 'WPS14';
  end if;
  -- the chain head moves only here (the app role has no write privilege on chains)
  insert into chains(tenant_id, session_id) values (NEW.tenant_id, NEW.session_id) on conflict do nothing;
  select next_seq, last_hash into ch from chains where tenant_id = NEW.tenant_id and session_id = NEW.session_id for update;
  if NEW.seq <> ch.next_seq or NEW.prev_hash <> ch.last_hash then
    raise exception 'chain_mismatch seq=% expected=%', NEW.seq, ch.next_seq using errcode = 'WPS15';
  end if;
  update chains set next_seq = ch.next_seq + 1, last_hash = NEW.hash where tenant_id = NEW.tenant_id and session_id = NEW.session_id;
  if NEW.kind = 'decision' then
    select kind, trust_origin, session_id into src from events where tenant_id = NEW.tenant_id and event_id = NEW.source_event_id;
    if src is null or src.kind = 'decision' or src.trust_origin <> 'user_typed' or src.session_id <> NEW.session_id then
      raise exception 'decision_without_user_source' using errcode = 'WPS04';
    end if;
    if exists (select 1 from events where tenant_id = NEW.tenant_id and kind = 'decision' and source_event_id = NEW.source_event_id) then
      raise exception 'source_already_used' using errcode = 'WPS13';
    end if;
    insert into decision_heads(tenant_id, item) values (NEW.tenant_id, NEW.decision_item) on conflict do nothing;
    select version, last_event_id, last_action into hd from decision_heads where tenant_id = NEW.tenant_id and item = NEW.decision_item for update;
    if hd.last_event_id is distinct from NEW.based_on_event_id then
      raise exception 'stale_decision head=% based_on=%', hd.last_event_id, NEW.based_on_event_id using errcode = 'WPS05';
    end if;
    if NEW.decision_action = 'execute' and hd.last_action is distinct from 'approve' then
      raise exception 'execute_without_current_approval head_action=%', hd.last_action using errcode = 'WPS05';
    end if;
    update decision_heads set version = hd.version + 1, last_event_id = NEW.event_id, last_action = NEW.decision_action
     where tenant_id = NEW.tenant_id and item = NEW.decision_item;
  end if;
  return NEW;
end $$;
create trigger events_guard before insert on events for each row execute function events_guard();
create function events_change_guard() returns trigger language plpgsql as $$
begin
  if TG_OP = 'UPDATE' and current_user = 'wp_owner' and OLD.content_enc is not null and NEW.content_enc is null
     and (to_jsonb(NEW) - 'content_enc') = (to_jsonb(OLD) - 'content_enc') then
    return NEW;                                             -- redaction by wp_erasure_cascade: content only, chain intact
  end if;
  raise exception 'events is append-only (% refused)', TG_OP using errcode = 'WPS06';
end $$;
create trigger events_no_change before update or delete on events for each row execute function events_change_guard();

-- ------------------------------------------------------------------ quarantine (bounded per session)
create table quarantine (
  id bigserial primary key, tenant_id tenant_t not null, session_id text not null, subject_id text not null,
  presented_epoch bigint not null, reason text not null, kind text not null, content_enc bytea not null, mentions text[] not null default '{}',
  at timestamptz not null default clock_timestamp()
);
create table quarantine_quota (
  tenant_id tenant_t not null, session_id text not null, kept bigint not null default 0, dropped bigint not null default 0,
  primary key (tenant_id, session_id)
);
create function quarantine_guard() returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
declare er boolean; q record; cap constant bigint := 1000;
begin
  select erased into er from subjects where tenant_id = NEW.tenant_id and subject_id = NEW.subject_id for share;
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  insert into quarantine_quota(tenant_id, session_id) values (NEW.tenant_id, NEW.session_id) on conflict do nothing;
  select kept into q from quarantine_quota where tenant_id = NEW.tenant_id and session_id = NEW.session_id for update;
  if q.kept >= cap then
    update quarantine_quota set dropped = dropped + 1 where tenant_id = NEW.tenant_id and session_id = NEW.session_id;
    return null;                                            -- over the cap: counted, not stored
  end if;
  update quarantine_quota set kept = kept + 1 where tenant_id = NEW.tenant_id and session_id = NEW.session_id;
  return NEW;
end $$;
create trigger quarantine_guard before insert on quarantine for each row execute function quarantine_guard();

create table tombstones (
  tenant_id tenant_t not null, event_id text not null, subject_id text not null, job_id text not null,
  primary key (tenant_id, subject_id, event_id)
);
create index tombstones_event on tombstones (tenant_id, event_id);

-- ------------------------------------------------------------------ inbox, analyst jobs, facts, cursors
create function subject_not_erased() returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
declare er boolean;
begin
  select erased into er from subjects where tenant_id = NEW.tenant_id and subject_id = NEW.subject_id for share;
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  return NEW;
end $$;

create table inbox (
  tenant_id tenant_t not null, item_id text not null, sender_session text not null, target_session text not null, subject_id text not null,
  payload_enc bytea not null, state text not null default 'new' check (state in ('new','claimed','confirmed')),
  claimed_epoch bigint, confirmed_epoch bigint, shown_count int not null default 0,
  lease_scope text not null, lease_epoch bigint not null,
  primary key (tenant_id, item_id)
);
create trigger inbox_fence_ins before insert on inbox for each row execute function wp_fence_trg('session', 'sender_session');
create trigger inbox_fence_upd before update on inbox for each row execute function wp_fence_trg('session', 'target_session');
create trigger inbox_subject before insert on inbox for each row execute function subject_not_erased();

create table analyst_jobs (
  tenant_id tenant_t not null, job_id text not null, idem_key text not null, session_id text not null,
  input_event_ids text[] not null, state text not null default 'queued' check (state in ('queued','claimed','done')),
  claim_epoch bigint not null default 0, claim_expires timestamptz, result_enc bytea, verify jsonb,
  lease_scope text not null, lease_epoch bigint not null,
  primary key (tenant_id, job_id), unique (tenant_id, idem_key),
  constraint done_needs_verify check (state <> 'done' or (verify is not null and (verify->>'ok')::boolean is true and result_enc is not null))
);
create index analyst_jobs_inputs on analyst_jobs using gin (input_event_ids);
create function jobs_guard() returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if NEW.lease_scope <> OLD.lease_scope or NEW.lease_epoch <> OLD.lease_epoch or NEW.input_event_ids <> OLD.input_event_ids
     or NEW.session_id <> OLD.session_id or NEW.tenant_id <> OLD.tenant_id then
    raise exception 'job identity fields are immutable' using errcode = 'WPS07';
  end if;
  if NEW.claim_epoch <> OLD.claim_epoch and (NEW.claim_epoch <> OLD.claim_epoch + 1 or NEW.state <> 'claimed') then
    raise exception 'claim_epoch only advances by one, on a claim' using errcode = 'WPS07';
  end if;
  if OLD.state = 'queued' and NEW.state = 'claimed' and NEW.claim_epoch <> OLD.claim_epoch + 1 then
    raise exception 'claim must advance claim_epoch' using errcode = 'WPS07';
  end if;
  if OLD.state = 'done' then raise exception 'done is final' using errcode = 'WPS07'; end if;
  if NEW.state = 'done' then
    if OLD.state <> 'claimed' or NEW.claim_epoch <> OLD.claim_epoch then
      raise exception 'stale_claim' using errcode = 'WPS01';
    end if;
    if exists (select 1 from events e join subjects s on s.tenant_id = e.tenant_id and s.subject_id = e.subject_id
               where e.tenant_id = NEW.tenant_id and e.event_id = any(NEW.input_event_ids) and s.erased) then
      raise exception 'inputs_erased' using errcode = 'WPS03';
    end if;
  end if;
  return NEW;
end $$;
create trigger jobs_fence_ins before insert on analyst_jobs for each row execute function wp_fence_trg('session', 'session_id');
create trigger jobs_guard before update on analyst_jobs for each row execute function jobs_guard();

create sequence fact_rev;
create table facts (
  tenant_id tenant_t not null, fact_id text not null, subject_id text not null,  -- subject_id = speaker whose key encrypts it
  slot text not null,                                                        -- keyed hash of (entity, attribute)
  body_enc bytea not null, valid_from timestamptz not null, valid_to timestamptz,
  input_event_ids text[] not null, rev bigint not null default nextval('fact_rev'),
  changed_xid bigint not null default (pg_current_xact_id()::text)::bigint,
  lease_scope text not null, lease_epoch bigint not null,
  primary key (tenant_id, fact_id)
);
create index facts_inputs on facts using gin (input_event_ids);
create index facts_cursor on facts (tenant_id, changed_xid, rev);
create index facts_subject on facts (tenant_id, subject_id);
create index facts_slot on facts (tenant_id, slot) where valid_to is null;
create trigger facts_fence before insert or update on facts for each row execute function wp_fence_trg('projector');
create trigger facts_subject before insert on facts for each row execute function subject_not_erased();

create table cursors (
  tenant_id tenant_t not null, family text not null, cx bigint not null default 0, cs bigint not null default 0,
  lease_scope text not null, lease_epoch bigint not null,
  primary key (tenant_id, family)
);
create trigger cursors_fence before insert or update on cursors for each row execute function wp_fence_trg('projector');

-- ------------------------------------------------------------------ store registry (written only by setup as wp_owner)
create table store_registry (name text primary key, kind text not null, verifiable boolean not null);

-- ------------------------------------------------------------------ erasure
-- One open job per (tenant, subject): a second request returns the open one, so the same subject serialises.
-- Runners claim a job (claim_epoch, heartbeat-renewed claim_expires). 'done' is written ONLY by wp_erasure_complete,
-- which recomputes every Postgres residue count itself and takes the cross-store counts (ES, key service) from the
-- verifier role's call; the app role cannot write 'done' or 'verify' at all.
create table erasure_jobs (
  job_id text primary key, tenant_id tenant_t not null, subject_id text not null,
  state text not null default 'requested' check (state in ('requested','marked','cascaded','purged','keys_destroyed','done')),
  verify jsonb, scope text, attempts int not null default 0, last_error_type text,
  claim_epoch bigint not null default 0, claim_expires timestamptz,
  ident_xmax bigint, ident_count int,                       -- the subject's identifier-set version and size, fixed at mark
  rescanned_xmax bigint,                                    -- identifier-set version the erasure rescan covered (attested by the runner)
  created_at timestamptz not null default clock_timestamp(), updated_at timestamptz not null default clock_timestamp()
);
create unique index erasure_one_open on erasure_jobs (tenant_id, subject_id) where state <> 'done';

-- Mentions found by the erasure-time rescan (events written before the subject's identifiers were declared).
create table erasure_mentions (
  tenant_id tenant_t not null, subject_id text not null, event_id text not null, job_id text not null,
  primary key (tenant_id, subject_id, event_id)
);

-- Legal hold: a recorded hold wins. Erasure of a held subject is QUEUED (the job stays open, refused at mark, at every
-- cascade batch and at key destruction with WPS16) and runs when the last hold is released.
create table legal_holds (
  tenant_id tenant_t not null, subject_id text not null, hold_id text primary key, reason_code text not null,
  placed_at timestamptz not null default clock_timestamp(), released_at timestamptz
);
create index legal_holds_subject on legal_holds (tenant_id, subject_id) where released_at is null;
create function wp_hold_place(t tenant_t, s text, hid text, reason text) returns jsonb language plpgsql security definer set search_path = public, pg_temp as $$
declare st text;
begin
  insert into subjects(tenant_id, subject_id) values (t, s) on conflict do nothing;
  perform 1 from subjects where tenant_id = t and subject_id = s for update;
  insert into legal_holds(tenant_id, subject_id, hold_id, reason_code) values (t, s, hid, reason);
  select state into st from erasure_jobs where tenant_id = t and subject_id = s and state <> 'done';
  return jsonb_build_object('hold_id', hid, 'open_erasure_state', st,
    'note', case when st is null or st = 'requested' then 'nothing erased yet'
                 else 'erasure already past mark: content removed before this hold is not recoverable; the rest is held' end);
end $$;
create function wp_hold_release(hid text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare h record;
begin
  select tenant_id, subject_id into h from legal_holds where hold_id = hid;
  perform 1 from subjects where tenant_id = h.tenant_id and subject_id = h.subject_id for update;
  update legal_holds set released_at = clock_timestamp() where hold_id = hid and released_at is null;
end $$;
create function wp_hold_check(t text, s text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare hs text;
begin
  perform 1 from subjects where tenant_id = t and subject_id = s for update;
  select string_agg(hold_id, ',') into hs from legal_holds where tenant_id = t and subject_id = s and released_at is null;
  if hs is not null then raise exception 'legal_hold %', hs using errcode = 'WPS16'; end if;
end $$;
create function erasure_guard() returns trigger language plpgsql as $$
begin
  if TG_OP = 'INSERT' and (NEW.state <> 'requested' or NEW.verify is not null) then
    raise exception 'a job starts as requested' using errcode = 'WPS08';
  end if;
  if TG_OP = 'UPDATE' then
    if OLD.state = 'done' then raise exception 'done is final' using errcode = 'WPS08'; end if;
    if NEW.job_id <> OLD.job_id or NEW.tenant_id <> OLD.tenant_id or NEW.subject_id <> OLD.subject_id then
      raise exception 'job identity is immutable' using errcode = 'WPS08';
    end if;
    if (NEW.state = 'done' or NEW.verify is distinct from OLD.verify) and current_user <> 'wp_owner' then
      raise exception 'only wp_erasure_complete may complete a job' using errcode = 'WPS08';
    end if;
  end if;
  return NEW;
end $$;
create trigger erasure_guard before insert or update on erasure_jobs for each row execute function erasure_guard();

create function wp_erasure_mark(jid text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare j record;
begin
  select tenant_id, subject_id into j from erasure_jobs where job_id = jid for update;
  insert into subjects(tenant_id, subject_id) values (j.tenant_id, j.subject_id) on conflict do nothing;
  perform wp_hold_check(j.tenant_id, j.subject_id);
  update erasure_jobs set ident_xmax = wp_subject_xmax(j.tenant_id, j.subject_id),
    ident_count = (select count(*) from subject_identifiers where tenant_id = j.tenant_id and subject_id = j.subject_id)
    where job_id = jid and state = 'requested';
  update subjects set erased = true where tenant_id = j.tenant_id and subject_id = j.subject_id;
  update erasure_jobs set state = 'marked', updated_at = clock_timestamp() where job_id = jid and state = 'requested';
end $$;

create function wp_erasure_add_mentions(jid text, ids text[]) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare j record;
begin
  select tenant_id, subject_id, state into j from erasure_jobs where job_id = jid;
  if j.state not in ('marked', 'cascaded') then raise exception 'rescan only after mark (state %)', j.state using errcode = 'WPS08'; end if;
  insert into erasure_mentions(tenant_id, subject_id, event_id, job_id)
    select j.tenant_id, j.subject_id, e.event_id, jid from events e where e.tenant_id = j.tenant_id and e.event_id = any(ids)
    on conflict do nothing;
end $$;
create function wp_erasure_rescan_done(jid text, upto bigint) returns void language sql security definer set search_path = public, pg_temp as $$
  update erasure_jobs set rescanned_xmax = upto where job_id = jid and state in ('marked', 'cascaded');
$$;
create function wp_subject_xmax(t text, s text) returns bigint language sql stable security definer set search_path = public, pg_temp as $$
  select coalesce(max(declared_seq), 0) from subject_identifiers where tenant_id = t and subject_id = s;
$$;

-- One bounded batch of tombstones and cascade deletes; returns the number of events tombstoned. Idempotent.
create function wp_erasure_cascade(jid text, batch int) returns int language plpgsql security definer set search_path = public, pg_temp as $$
declare j record; n int; ids text[];
begin
  select tenant_id, subject_id into j from erasure_jobs where job_id = jid;
  perform wp_hold_check(j.tenant_id, j.subject_id);         -- a hold placed after mark stops the cascade at the next batch
  select coalesce(array_agg(event_id), '{}') into ids from (
    select event_id from (
      select e.event_id from events e where e.tenant_id = j.tenant_id and e.subject_id = j.subject_id
      union select e.event_id from events e where e.tenant_id = j.tenant_id and e.mentions @> array[j.subject_id]
      union select m.event_id from erasure_mentions m where m.tenant_id = j.tenant_id and m.subject_id = j.subject_id) u
    where not exists (select 1 from tombstones tb where tb.tenant_id = j.tenant_id and tb.subject_id = j.subject_id and tb.event_id = u.event_id)
    order by event_id limit batch) x;
  insert into tombstones(tenant_id, event_id, subject_id, job_id) select j.tenant_id, unnest(ids), j.subject_id, jid;
  update events set content_enc = null where tenant_id = j.tenant_id and event_id = any(ids) and content_enc is not null;
  n := cardinality(ids);
  if n > 0 then
    delete from analyst_jobs a where a.tenant_id = j.tenant_id and a.input_event_ids && ids;
    delete from facts f where f.tenant_id = j.tenant_id and f.input_event_ids && ids;
  end if;
  delete from facts where tenant_id = j.tenant_id and subject_id = j.subject_id;
  delete from inbox where tenant_id = j.tenant_id and subject_id = j.subject_id;
  delete from quarantine where tenant_id = j.tenant_id and (subject_id = j.subject_id or mentions @> array[j.subject_id]);
  if n = 0 and (select coalesce(rescanned_xmax, -1) >= coalesce(ident_xmax, 0) from erasure_jobs where job_id = jid) then
    delete from subject_identifiers where tenant_id = j.tenant_id and subject_id = j.subject_id;   -- only after the rescan used them
    update erasure_jobs set state = 'cascaded', updated_at = clock_timestamp() where job_id = jid and state = 'marked';
  end if;
  return n;
end $$;

-- Postgres residue for a subject, computed by the store (never taken from a caller).
create function wp_erasure_pg_residue(t text, s text) returns jsonb language sql stable security definer set search_path = public, pg_temp as $$
  select jsonb_build_object(
    'pg:events_untombstoned', (select count(*) from events e where e.tenant_id = t
                                and (e.subject_id = s or e.mentions @> array[s]
                                     or exists (select 1 from erasure_mentions m where m.tenant_id = t and m.subject_id = s and m.event_id = e.event_id))
                                and not exists (select 1 from tombstones tb where tb.tenant_id = t and tb.subject_id = s and tb.event_id = e.event_id)),
    'pg:content_present', (select count(*) from events e join tombstones tb on tb.tenant_id = e.tenant_id and tb.event_id = e.event_id
                           where e.tenant_id = t and tb.subject_id = s and e.content_enc is not null),
    'pg:mentions_unscanned', (select case when coalesce(jb.rescanned_xmax, -1) >= coalesce(jb.ident_xmax, 0) then 0
                              else (select count(*) from events e where e.tenant_id = t and e.subject_id <> s and e.content_enc is not null
                                    and e.mention_scan_upto < coalesce(jb.ident_xmax, 0)) end
                              from (select rescanned_xmax, ident_xmax from erasure_jobs where tenant_id = t and subject_id = s
                                    order by created_at desc limit 1) jb),
    'pg:identifiers', (select count(*) from subject_identifiers where tenant_id = t and subject_id = s),
    'pg:quarantine_mentions', (select count(*) from quarantine where tenant_id = t and mentions @> array[s]),
    'pg:facts', (select count(*) from facts f where f.tenant_id = t and (f.subject_id = s or exists (
                   select 1 from tombstones tb where tb.tenant_id = t and tb.subject_id = s and tb.event_id = any(f.input_event_ids)))),
    'pg:inbox', (select count(*) from inbox where tenant_id = t and subject_id = s),
    'pg:quarantine', (select count(*) from quarantine where tenant_id = t and subject_id = s),
    'pg:analyst_jobs', (select count(*) from analyst_jobs a where a.tenant_id = t and exists (
                   select 1 from tombstones tb where tb.tenant_id = t and tb.subject_id = s and tb.event_id = any(a.input_event_ids))),
    'pg:subject_not_marked', (select count(*) from subjects where tenant_id = t and subject_id = s and not erased));
$$;

-- Completion: callable by wp_verifier only. ext = counts for every non-Postgres verifiable store, measured by the
-- verifier itself. Every registered verifiable store must be present and zero; Postgres counts are recomputed here.
create function wp_erasure_complete(jid text, ext jsonb, scope_note text) returns jsonb language plpgsql security definer set search_path = public, pg_temp as $$
declare j record; v jsonb; bad text; missing text; sc jsonb; hl jsonb;
begin
  select * into j from erasure_jobs where job_id = jid for update;
  if j.state <> 'keys_destroyed' then raise exception 'job not ready (state %)', j.state using errcode = 'WPS08'; end if;
  v := wp_erasure_pg_residue(j.tenant_id, j.subject_id) || coalesce(ext, '{}'::jsonb);
  select string_agg(name, ',') into missing from store_registry r where r.verifiable and r.kind <> 'pg' and not (coalesce(ext, '{}') ? r.name);
  if missing is not null then raise exception 'done_missing_stores %', missing using errcode = 'WPS08'; end if;
  select string_agg(k, ',') into bad from jsonb_each(v) as x(k, val) where jsonb_typeof(val) <> 'number' or (val #>> '{}')::numeric <> 0;
  if bad is not null then
    update erasure_jobs set verify = v, last_error_type = 'residue', updated_at = clock_timestamp() where job_id = jid;
    return jsonb_build_object('done', false, 'residue', bad, 'verify', v);
  end if;
  sc := jsonb_build_object(
    'speaker', 'the subject''s own events, facts and analyst jobs derived from them, inbox and quarantine items, the subject key',
    'mentions', jsonb_build_object(
      'rule', 'every event that mentions the subject is removed whole: tombstoned, content nulled in Postgres, placeholder in ES, derived facts and jobs deleted',
      'matcher', 'm1: case-insensitive exact match of the declared identifiers as whole token sequences (wp/mentions.py)',
      'declared_identifiers', j.ident_count, 'identifier_set_version', j.ident_xmax, 'rescan_covered_version', j.rescanned_xmax,
      'found_at_write', (select count(*) from tombstones tb join events e on e.tenant_id = tb.tenant_id and e.event_id = tb.event_id
                         where tb.tenant_id = j.tenant_id and tb.subject_id = j.subject_id and e.subject_id <> j.subject_id and e.mentions @> array[j.subject_id]),
      'found_by_rescan', (select count(*) from erasure_mentions where tenant_id = j.tenant_id and subject_id = j.subject_id),
      'rescan_attested_by', 'erasure runner (the store cannot decrypt; it enforces only that the rescan covered the identifier-set version)',
      'not_covered', jsonb_build_array('identifiers that were never declared (nicknames, misspellings, initials)', 'pronouns and indirect references',
                                       'mentions inside inbox payloads and analyst-job results',
                                       'content outside this store (ES snapshots, backups, logs: see store_registry ext:*)')),
    'note', scope_note);
  hl := (select coalesce(jsonb_agg(jsonb_build_object('hold_id', hold_id, 'placed_at', placed_at, 'released_at', released_at) order by placed_at), '[]')
         from legal_holds where tenant_id = j.tenant_id and subject_id = j.subject_id);
  update erasure_jobs set state = 'done', verify = v, scope = sc::text, claim_expires = null, updated_at = clock_timestamp() where job_id = jid;
  return jsonb_build_object('done', true, 'verify', v, 'scope', sc, 'holds', hl,
                            'requested_at', j.created_at, 'completed_at', clock_timestamp(),
                            'delay_s', round(extract(epoch from clock_timestamp() - j.created_at)::numeric, 3));
end $$;

-- ------------------------------------------------------------------ guards on every table (enabled ALWAYS)
do $$
declare t text;
begin
  foreach t in array array['leases','subjects','chains','decision_heads','tombstones','store_registry','quarantine_quota',
                           'identifier_epochs','subject_identifiers','erasure_mentions','legal_holds'] loop
    execute format('create trigger %I before insert or update or delete on %I for each row execute function wp_owner_only()', t || '_owner_only', t);
  end loop;
  foreach t in array array['inbox','analyst_jobs','facts','cursors','quarantine','erasure_jobs'] loop
    execute format('create trigger %I before delete on %I for each row execute function wp_owner_only()', t || '_delete_owner_only', t);
  end loop;
  execute 'create trigger quarantine_no_update before update on quarantine for each row execute function wp_append_only()';
  foreach t in array array['leases','subjects','chains','events','decision_heads','quarantine','quarantine_quota','tombstones','inbox',
                           'analyst_jobs','facts','cursors','store_registry','erasure_jobs',
                           'identifier_epochs','subject_identifiers','erasure_mentions','legal_holds'] loop
    execute format('create trigger %I before truncate on %I for each statement execute function wp_no_truncate()', t || '_no_truncate', t);
  end loop;
  for t in select format('alter table %I enable always trigger %I', c.relname, g.tgname) from pg_trigger g join pg_class c on c.oid = g.tgrelid
           join pg_namespace n on n.oid = c.relnamespace where not g.tgisinternal and n.nspname = 'public' loop
    execute t;
  end loop;
end $$;

-- ------------------------------------------------------------------ privileges
revoke all on all tables in schema public from public;
revoke all on all functions in schema public from public;
revoke all on all sequences in schema public from public;
grant usage on schema public to wp_app, wp_verifier;
grant usage on type tenant_t to wp_app, wp_verifier;
grant select on all tables in schema public to wp_verifier;
grant select, insert on events, quarantine to wp_app;
grant select, insert, update on inbox, analyst_jobs, facts, cursors to wp_app;
grant select, insert on erasure_jobs to wp_app;
grant update (state, attempts, last_error_type, claim_epoch, claim_expires, updated_at) on erasure_jobs to wp_app;
grant select on tombstones, store_registry, decision_heads, chains, subjects, identifier_epochs, subject_identifiers, erasure_mentions, legal_holds to wp_app;
grant usage on sequence ingest_seq, fact_rev, quarantine_id_seq to wp_app;
grant execute on function wp_lease_acquire(tenant_t, text, double precision, boolean, text, int), wp_lease_renew(tenant_t, text, bigint, text, double precision),
  wp_lease_release(tenant_t, text, bigint, text), wp_subjects_ensure(tenant_t, text[]), wp_erasure_mark(text), wp_erasure_cascade(text, int),
  wp_erasure_pg_residue(text, text), wp_fence(text, text, bigint), wp_identifier_declare(tenant_t, text, text[]),
  wp_hold_place(tenant_t, text, text, text), wp_hold_release(text), wp_hold_check(text, text), wp_erasure_add_mentions(text, text[]),
  wp_erasure_rescan_done(text, bigint), wp_subject_xmax(text, text) to wp_app;
-- In a client deployment wp_hold_place/wp_hold_release belong to a legal-operations role, not the application.
grant execute on function wp_erasure_pg_residue(text, text), wp_erasure_complete(text, jsonb, text) to wp_verifier;
