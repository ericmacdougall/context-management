-- Write path: the append-only, hash-chained event log is the only truth. No plaintext of any data subject is stored
-- in this database: content columns are AES-GCM ciphertext under a per-subject key held by the key service (wp_keys).
--
-- ROLES (created by wp/setup.py; this file runs under SET ROLE wp_owner, so wp_owner owns every object):
--   wp_owner     NOLOGIN. Owns the tables. Only the SECURITY DEFINER functions below act as wp_owner.
--   wp_app       the application. Not an owner, not a superuser: it cannot DISABLE TRIGGER or set
--                session_replication_role, has no DELETE or TRUNCATE privilege anywhere, and no write privilege on
--                leases, subjects, chains, tombstones, decision_heads, store_registry, quarantine_quota.
--   wp_verifier  completes erasure jobs (wp_erasure_complete); otherwise read-only.
--   wp_reviewer  NOLOGIN group (D3): its member logins (one per reviewer principal) approve or reject identifier
--                proposals. Only wp_identifier_approve writes subject_identifiers; wp_app can only propose.
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

-- D6 key broker (wp/broker.py): a writer key for epoch E is issued only while (tenant, scope, E, holder) is the LIVE
-- lease. The broker's role can execute this and nothing else in this database.
create function wp_lease_holder_current(t text, sc text, e bigint, who text) returns boolean language sql stable security definer set search_path = public, pg_temp as $$
  select exists (select 1 from leases where tenant_id = t and scope = sc and epoch = e and holder = who and expires_at > clock_timestamp());
$$;

-- TG_ARGV[0] = 'projector' or 'session'; TG_ARGV[1] = the column naming the session the row belongs to.
create function wp_fence_trg() returns trigger language plpgsql as $$
declare want text;
begin
  if TG_ARGV[0] = 'projector' then want := 'projector';
  else want := 'session:' || (to_jsonb(NEW) ->> TG_ARGV[1]); end if;
  if NEW.lease_scope is distinct from want then
    raise exception 'scope_not_bound table=% presented=% required=%', TG_TABLE_NAME, NEW.lease_scope, want using errcode = 'WPS12';
  end if;
  perform public.wp_fence(NEW.tenant_id, NEW.lease_scope, NEW.lease_epoch);
  return NEW;
end $$;

create function wp_lease_acquire(t tenant_t, sc text, ttl_s double precision, force boolean, who text, lock_ms int)
returns table(epoch bigint, at timestamptz) language plpgsql security definer set search_path = public, pg_temp as $$
begin
  perform set_config('lock_timeout', greatest(coalesce(lock_ms, 2000), 1)::text, true);
  insert into leases(tenant_id, scope) values (t, sc) on conflict do nothing;
  -- the tenant's identifier-set version row exists before any append: otherwise the first appender's uncommitted
  -- insert of it (events_guard) makes every other appender in the tenant wait until that writer commits (row 23)
  insert into identifier_epochs(tenant_id) values (t) on conflict do nothing;
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
-- Review C class 3 (F3): identifier_epochs.matcher is the tenant's matcher id (wp/mentions.py matcher_id: matcher
-- version, Unicode version and data tables), set ONLY by the owner function wp_matcher_set. Every identifier carries it
-- under a foreign key, so an identifier under any other matcher cannot exist; approval (WPS32), appends and the erasure
-- rescan (WPS38) under another matcher are refused, never silently unmatched. Migration: DESIGN-NOTES class 3.
create table identifier_epochs (tenant_id tenant_t primary key, max_seq bigint not null default 0, matcher text,
  unique (tenant_id, matcher));
create table subject_identifiers (
  tenant_id tenant_t not null, ident_hmac text not null, subject_id text not null, declared_seq bigint not null,
  matcher text not null,
  primary key (tenant_id, ident_hmac),
  foreign key (tenant_id, matcher) references identifier_epochs (tenant_id, matcher)
);
create index subject_identifiers_subject on subject_identifiers (tenant_id, subject_id);
create function wp_identifier_declare(t tenant_t, s text, hmacs text[]) returns bigint language plpgsql security definer set search_path = public, pg_temp as $$
declare n bigint; er boolean; m text;
begin
  insert into subjects(tenant_id, subject_id) values (t, s) on conflict do nothing;
  -- FOR SHARE (amendment 17): still exclusive against wp_erasure_mark (FOR UPDATE), so an identifier is never declared
  -- for a subject being erased; FOR UPDATE here deadlocked with an append that mentions s (it holds identifier_epochs
  -- FOR SHARE, then share-locks s). Declares of one tenant serialise on identifier_epochs FOR UPDATE below.
  select erased into er from subjects where tenant_id = t and subject_id = s for share;
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  insert into identifier_epochs(tenant_id) values (t) on conflict do nothing;
  select matcher into m from identifier_epochs where tenant_id = t for update;
  if m is null then raise exception 'tenant has no matcher (wp_matcher_set)' using errcode = 'WPS32'; end if;
  n := nextval('ident_seq');
  insert into subject_identifiers(tenant_id, ident_hmac, subject_id, declared_seq, matcher) select t, h, s, n, m from unnest(hmacs) h
    on conflict (tenant_id, ident_hmac) do nothing;
  if exists (select 1 from subject_identifiers where tenant_id = t and ident_hmac = any(hmacs) and subject_id <> s) then
    raise exception 'identifier_names_another_subject' using errcode = 'WPS20';
  end if;
  update identifier_epochs set max_seq = n where tenant_id = t;
  return n;
end $$;
-- Owner-only (no grant): tenant setup stores the matcher. Idempotent for the same id; a change is refused while the
-- tenant has identifiers (WPS38). Serialises with approvals on identifier_epochs FOR UPDATE.
create function wp_matcher_set(t tenant_t, m text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare cur text;
begin
  if coalesce(m, '') = '' then raise exception 'matcher id missing' using errcode = 'WPS38'; end if;
  insert into identifier_epochs(tenant_id) values (t) on conflict do nothing;
  select matcher into cur from identifier_epochs where tenant_id = t for update;
  if cur is not distinct from m then return; end if;
  if exists (select 1 from subject_identifiers where tenant_id = t) then
    raise exception 'matcher change % -> % with identifiers (migration: DESIGN-NOTES class 3)', cur, m using errcode = 'WPS38';
  end if;
  update identifier_epochs set matcher = m where tenant_id = t;
end $$;
-- Present matcher check for the erasure rescan (WPS38). A tenant with no matcher has no identifiers (foreign key).
create function wp_matcher_check(t text, m text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare cur text;
begin
  select matcher into cur from identifier_epochs where tenant_id = t for share;
  if cur is not null and m is distinct from cur then
    raise exception 'matcher_mismatch presented=% tenant=%', m, cur using errcode = 'WPS38';
  end if;
end $$;
-- D3 (DESIGN-NOTES "Design: D3"; ASSUMPTION under decision 10): wp_identifier_declare above is INTERNAL. No role has
-- EXECUTE on it; it runs only inside wp_identifier_approve, so for the application role an identifier that no reviewer
-- approved has no path to subject_identifiers (the only table the matcher, the rescan and the cascade read).
-- The store enforces WHO: the proposer is session_user, set here; the approver is a member of wp_reviewer and not the
-- proposer (WPS30); the approved HMAC is the one proposed, recomputed by the reviewer from the plaintext it reviewed
-- (WPS31); both ran the rule version of the tenant's matcher, 'r3:' || identifier_epochs.matcher (WPS32). Review C
-- class 2 (F8): one lock order everywhere, subject row, identifier_epochs, proposal row, then events; propose and
-- approve check `erased` only under the subject lock, so the erasure mark (FOR UPDATE on the subject) waits for an
-- in-flight proposal and the cascade deletes it; no proposal can commit after the mark.
-- The reviewer process enforces the rule on the plaintext
-- (wp/mentions.py identifier_problem): the store sees only the HMAC and a ciphertext under the subject key.
create table identifier_proposals (
  tenant_id tenant_t not null, proposal_id text primary key, subject_id text not null,
  ident_hmac text not null,
  ident_enc bytea not null,                                 -- the identifier as written, under the SUBJECT key (AAD proposal:<id>)
  rule_version text not null,
  proposed_by name not null, proposed_at timestamptz not null default clock_timestamp(),
  state text not null default 'proposed' check (state in ('proposed', 'approved', 'rejected')),
  reviewed_by name, reviewed_at timestamptz, reason text
);
create index identifier_proposals_subject on identifier_proposals (tenant_id, subject_id);
create function wp_identifier_propose(t tenant_t, s text, pid text, h text, enc bytea, rv text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare er boolean;
begin
  insert into subjects(tenant_id, subject_id) values (t, s) on conflict do nothing;
  select erased into er from subjects where tenant_id = t and subject_id = s for share;   -- lock before check (F8)
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  if h is null or enc is null or rv is null then raise exception 'proposal incomplete' using errcode = 'WPS33'; end if;
  insert into identifier_proposals(tenant_id, proposal_id, subject_id, ident_hmac, ident_enc, rule_version, proposed_by)
    values (t, pid, s, h, enc, rv, session_user);
end $$;
create function wp_identifier_approve(pid text, h text, rv text) returns bigint language plpgsql security definer set search_path = public, pg_temp as $$
declare p record; n bigint; m text; er boolean;
begin
  if not pg_has_role(session_user, 'wp_reviewer', 'member') then
    raise exception 'approve: % is not a reviewer', session_user using errcode = 'WPS30';
  end if;
  select tenant_id, subject_id into p from identifier_proposals where proposal_id = pid;
  if not found then raise exception 'approve: no open proposal %', pid using errcode = 'WPS33'; end if;
  -- lock order (class 2): subject, identifier_epochs, proposal
  select erased into er from subjects where tenant_id = p.tenant_id and subject_id = p.subject_id for share;
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  insert into identifier_epochs(tenant_id) values (p.tenant_id) on conflict do nothing;
  select matcher into m from identifier_epochs where tenant_id = p.tenant_id for update;
  select * into p from identifier_proposals where proposal_id = pid for update;
  if not found or p.state <> 'proposed' then raise exception 'approve: no open proposal %', pid using errcode = 'WPS33'; end if;
  if p.proposed_by = session_user then raise exception 'approve: % proposed it (self-approval)', session_user using errcode = 'WPS30'; end if;
  if h is distinct from p.ident_hmac then raise exception 'approve: HMAC differs from the proposal' using errcode = 'WPS31'; end if;
  if rv is distinct from p.rule_version or m is null or rv is distinct from 'r3:' || m then
    raise exception 'approve: rule version % (proposal %, tenant matcher %)', rv, p.rule_version, m using errcode = 'WPS32';
  end if;
  n := public.wp_identifier_declare(p.tenant_id, p.subject_id, array[p.ident_hmac]);   -- same lock order as before
  update identifier_proposals set state = 'approved', reviewed_by = session_user, reviewed_at = clock_timestamp() where proposal_id = pid;
  return n;
end $$;
create function wp_identifier_reject(pid text, why text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if not pg_has_role(session_user, 'wp_reviewer', 'member') then
    raise exception 'reject: % is not a reviewer', session_user using errcode = 'WPS30';
  end if;
  update identifier_proposals set state = 'rejected', reviewed_by = session_user, reviewed_at = clock_timestamp(), reason = left(why, 200)
   where proposal_id = pid and state = 'proposed';
  if not found then raise exception 'reject: no open proposal %', pid using errcode = 'WPS33'; end if;
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
  content_enc bytea,                                        -- NULL only after erasure (wp_erasure_cascade; a tombstone and its job remain)
  -- m5 b1 (DESIGN-NOTES "Matcher budget" B3): 65,536 bytes of content (wp/mentions.py MAX_CONTENT_BYTES) plus 28 bytes
  -- of nonce and tag; the app refuses larger content first (WPM01), so this only stops a path that skips the check
  constraint events_content_size check (content_enc is null or octet_length(content_enc) <= 65564),
  content_digest text not null,                             -- sha256 of the ciphertext; the chain hashes THIS, so redaction keeps the chain verifiable
  mentions text[] not null default '{}',                    -- subjects whose declared identifiers the write-time matcher found
  mention_scan_upto bigint not null default 0,              -- identifier-set version the matcher ran against
  mention_matcher text not null default '',                 -- matcher id that ran (class 3: must be the tenant's, WPS38)
  secret_scan text not null default '',                    -- secret-scanner version the app ran (row 19); must be current
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

-- row 19: the secret-scanner version every new event must carry (wp/secrets.py VERSION); bumping it is a migration
create function wp_secret_scan_version() returns text language sql immutable as $$ select 's1'::text $$;

create function events_guard() returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
declare er boolean; ch record; src record; hd record; prior record; mx bigint; mm text;
begin
  if NEW.lease_scope is distinct from 'session:' || NEW.session_id then
    raise exception 'scope_not_bound table=events presented=% required=session:%', NEW.lease_scope, NEW.session_id using errcode = 'WPS12';
  end if;
  perform public.wp_fence(NEW.tenant_id, NEW.lease_scope, NEW.lease_epoch);
  if NEW.secret_scan is distinct from wp_secret_scan_version() then
    raise exception 'secret_scan_missing_or_stale presented=% required=%', NEW.secret_scan, wp_secret_scan_version() using errcode = 'WPS21';
  end if;
  select erased into er from subjects where tenant_id = NEW.tenant_id and subject_id = NEW.subject_id for share;
  if er is null then raise exception 'unknown_subject' using errcode = 'WPS02'; end if;
  if er then raise exception 'subject_erased' using errcode = 'WPS03'; end if;
  if NEW.content_enc is null or NEW.content_digest is distinct from encode(sha256(NEW.content_enc), 'hex') then
    raise exception 'content_digest_mismatch' using errcode = 'WPS19';
  end if;
  -- mentions: the event must have been matched against the CURRENT identifier set, and may not mention an erased subject
  insert into identifier_epochs(tenant_id) values (NEW.tenant_id) on conflict do nothing;
  select max_seq, matcher into mx, mm from identifier_epochs where tenant_id = NEW.tenant_id for share;
  if mm is not null and NEW.mention_matcher is distinct from mm then
    raise exception 'mention_matcher_mismatch presented=% tenant=%', NEW.mention_matcher, mm using errcode = 'WPS38';
  end if;
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
  at timestamptz not null default clock_timestamp(),
  constraint quarantine_content_size check (octet_length(content_enc) <= 65564)     -- as events_content_size
);

-- m5 b1 (DESIGN-NOTES "Matcher budget" B5): writes refused before anything is stored, counted per tenant, day and code
-- (WPM01 content over the size bound, WPM02 over the matcher key budget, WPM03 a call over its limits). A count and
-- a code only: no content, no subject, no event id. Written only by wp_write_refused; an operator reads it.
create table write_refusals (
  tenant_id tenant_t not null, day date not null, code text not null check (code in ('WPM01', 'WPM02', 'WPM03')),
  n bigint not null, last_at timestamptz not null default clock_timestamp(),
  primary key (tenant_id, day, code)
);

create function wp_write_refused(t tenant_t, c text) returns bigint language plpgsql security definer set search_path = public, pg_temp as $$
declare k bigint;
begin
  if c is null or c not in ('WPM01', 'WPM02', 'WPM03') then
    raise exception 'unknown write refusal code' using errcode = '22023';
  end if;
  insert into write_refusals(tenant_id, day, code, n) values (t, current_date, c, 1)
    on conflict (tenant_id, day, code) do update set n = write_refusals.n + 1, last_at = clock_timestamp()
    returning n into k;
  return k;
end $$;
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
  state text not null default 'requested' check (state in ('requested','marked','cascaded','purged','keys_destroyed','logically_erased','done')),
  verify jsonb, scope text, attempts int not null default 0, last_error_type text,
  claim_epoch bigint not null default 0, claim_expires timestamptz,
  ident_xmax bigint, ident_count int,                       -- the subject's identifier-set version and size, fixed at mark
  rescanned_xmax bigint,                                    -- identifier-set version the erasure rescan covered (attested by the runner)
  own_after text not null default '', men_after text not null default '',   -- cascade keyset cursors (D5: linear in the subject's events)
  logical_at timestamptz, physical_deadline timestamptz, physical_pending jsonb,   -- item 4: logically erased, physical purge pending
  created_at timestamptz not null default clock_timestamp(), updated_at timestamptz not null default clock_timestamp()
);
create unique index erasure_one_open on erasure_jobs (tenant_id, subject_id) where state <> 'done';

-- Mentions found by the erasure-time rescan (events written before the subject's identifiers were declared).
create table erasure_mentions (
  tenant_id tenant_t not null, subject_id text not null, event_id text not null, job_id text not null,
  source text not null default 'rescan' check (source in ('write', 'rescan')),   -- 'write': materialised at mark from events.mentions
  primary key (tenant_id, subject_id, event_id)
);
create index erasure_mentions_event on erasure_mentions (tenant_id, event_id);
-- D1: events an erasure must not touch because a legal hold covers them (the speaker, a mentioned subject, or a subject
-- the rescan linked to the event is held). They stay intact and the job stays open until the hold is released.
create table erasure_withheld (
  tenant_id tenant_t not null, job_id text not null, event_id text not null, speaker text not null, hold_ids text not null,
  withheld_at timestamptz not null default clock_timestamp(),
  primary key (job_id, event_id)
);

-- D4 (store-set rescan-done mark): every event the rescan had to examine, recorded page by page by the runner. The
-- store sets the covered version only if no candidate event (another speaker's content written against an older
-- identifier-set version) is missing from this set. The store still cannot decrypt: WHETHER a scanned event matched is
-- the runner's word; THAT every candidate was examined is the store's.
create table erasure_rescanned (
  job_id text not null, event_id text not null, primary key (job_id, event_id)
);

-- Legal hold: a recorded hold wins. Erasure of a held subject is QUEUED (the job stays open, refused at mark, at every
-- cascade batch and at key destruction with WPS16) and runs when the last hold is released.
create table legal_holds (
  tenant_id tenant_t not null, subject_id text not null, hold_id text primary key, reason_code text not null,
  placed_at timestamptz not null default clock_timestamp(), released_at timestamptz
);
create index legal_holds_subject on legal_holds (tenant_id, subject_id) where released_at is null;
create function wp_hold_place(t tenant_t, s text, hid text, reason text) returns jsonb language plpgsql security definer set search_path = public, pg_temp as $$
declare st text; gone bigint; gone_ids text[];
begin
  insert into subjects(tenant_id, subject_id) values (t, s) on conflict do nothing;
  -- FOR UPDATE conflicts with the share lock of every in-flight removal that judges this subject (review E F4): the
  -- hold lands either before that judgement (the event is withheld) or after the removal committed (counted below)
  perform 1 from subjects where tenant_id = t and subject_id = s for update;
  insert into legal_holds(tenant_id, subject_id, hold_id, reason_code) values (t, s, hid, reason);
  select state into st from erasure_jobs where tenant_id = t and subject_id = s and state <> 'done';
  -- events in this subject's footprint whose content was already removed when the hold landed: recorded, not hidden
  with fp as (select event_id from events where tenant_id = t and subject_id = s
              union select event_id from events where tenant_id = t and mentions @> array[s]
              union select event_id from erasure_mentions where tenant_id = t and subject_id = s)
  select count(*), (array_agg(fp.event_id order by fp.event_id))[1:20] into gone, gone_ids
    from fp join events e on e.tenant_id = t and e.event_id = fp.event_id where e.content_enc is null;
  return jsonb_build_object('hold_id', hid, 'open_erasure_state', st,
    'linked_removed', gone, 'linked_removed_ids', to_jsonb(coalesce(gone_ids, '{}'::text[])),
    'note', case when gone > 0 then 'linked_removed events of this subject were removed (by its own or another subject''s erasure) before this hold landed and are not recoverable; the rest is held'
                 when st is null or st = 'requested' then 'nothing erased yet'
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
  -- FOR SHARE (amendment 17): a hold (wp_hold_place, FOR UPDATE) still cannot land until this transaction ends, but two
  -- cascade batches no longer exclude each other: with FOR UPDATE here, A's batch held A and share-locked B while B's
  -- batch held B and share-locked A (deadlock between erasures of subjects who mention each other).
  perform 1 from subjects where tenant_id = t and subject_id = s for share;
  select string_agg(hold_id, ',') into hs from legal_holds where tenant_id = t and subject_id = s and released_at is null;
  if hs is not null then raise exception 'legal_hold %', hs using errcode = 'WPS16'; end if;
end $$;
create function wp_erasure_rank(st text) returns int language sql immutable as $$
  select array_position(array['requested','marked','cascaded','purged','keys_destroyed','logically_erased','done'], st);
$$;
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
    -- state transition rule (low item of the delta review): forward only; the store-written states (marked,
    -- cascaded, logically_erased, done) only through the owner's functions; the receipt's physical fields likewise
    if wp_erasure_rank(NEW.state) < wp_erasure_rank(OLD.state) then
      raise exception 'state may not move back (% -> %)', OLD.state, NEW.state using errcode = 'WPS08';
    end if;
    if NEW.state <> OLD.state and NEW.state in ('marked', 'cascaded', 'logically_erased', 'done') and current_user <> 'wp_owner' then
      raise exception 'state % is written by the store only', NEW.state using errcode = 'WPS08';
    end if;
    if (NEW.logical_at, NEW.physical_deadline, NEW.physical_pending) is distinct from (OLD.logical_at, OLD.physical_deadline, OLD.physical_pending)
       and current_user <> 'wp_owner' then
      raise exception 'physical-purge fields are written by the store only' using errcode = 'WPS08';
    end if;
  end if;
  return NEW;
end $$;
create trigger erasure_guard before insert or update on erasure_jobs for each row execute function erasure_guard();

create function wp_erasure_mark(jid text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare j record;
begin
  -- review E F12: the job's identity is immutable (erasure_guard), so it is read unlocked; the SUBJECT is locked before
  -- the job, the order of a cascade batch (subject FOR SHARE ... job cursor update). Job first made a repeated mark
  -- and a batch wait on each other (40P01).
  select tenant_id, subject_id into j from erasure_jobs where job_id = jid;
  if not found then raise exception 'no erasure job %', jid using errcode = 'WPS08'; end if;
  insert into subjects(tenant_id, subject_id) values (j.tenant_id, j.subject_id) on conflict do nothing;
  -- the subject row exclusively first (it is updated below), so the share lock of wp_hold_check is never upgraded
  perform 1 from subjects where tenant_id = j.tenant_id and subject_id = j.subject_id for update;
  perform 1 from erasure_jobs where job_id = jid for update;
  perform wp_hold_check(j.tenant_id, j.subject_id);
  update erasure_jobs set ident_xmax = wp_subject_xmax(j.tenant_id, j.subject_id),
    ident_count = (select count(*) from subject_identifiers where tenant_id = j.tenant_id and subject_id = j.subject_id)
    where job_id = jid and state = 'requested';
  update subjects set erased = true where tenant_id = j.tenant_id and subject_id = j.subject_id;
  insert into erasure_mentions(tenant_id, subject_id, event_id, job_id, source)
    select j.tenant_id, j.subject_id, e.event_id, jid, 'write' from events e
     where e.tenant_id = j.tenant_id and e.mentions @> array[j.subject_id] and e.subject_id <> j.subject_id
    on conflict do nothing;
  update erasure_jobs set state = 'marked', updated_at = clock_timestamp() where job_id = jid and state = 'requested';
end $$;

create function wp_erasure_add_mentions(jid text, ids text[]) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare j record;
begin
  select tenant_id, subject_id, state into j from erasure_jobs where job_id = jid;
  if j.state not in ('marked', 'cascaded') then raise exception 'rescan only after mark (state %)', j.state using errcode = 'WPS08'; end if;
  -- review E F4: a link adds a subject to the event's hold footprint; it waits for an in-flight removal of the event
  -- (wp_erasure_apply locks its events first), so no removal judges a footprint that grows under it
  perform 1 from events e where e.tenant_id = j.tenant_id and e.event_id = any(ids) order by e.event_id for share;
  insert into erasure_mentions(tenant_id, subject_id, event_id, job_id)
    select j.tenant_id, j.subject_id, e.event_id, jid from events e where e.tenant_id = j.tenant_id and e.event_id = any(ids)
    on conflict do nothing;
  update erasure_jobs set men_after = '' where job_id = jid and exists (select 1 from unnest(ids) x where x <= men_after);
end $$;
create function wp_erasure_rescan_page(jid text, ids text[], m text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare j record;
begin
  select tenant_id, state into j from erasure_jobs where job_id = jid;
  if j.state not in ('marked', 'cascaded') then raise exception 'rescan only after mark (state %)', j.state using errcode = 'WPS08'; end if;
  perform public.wp_matcher_check(j.tenant_id, m);          -- class 3: a page scanned by another matcher is refused
  insert into erasure_rescanned(job_id, event_id)
    select jid, e.event_id from events e where e.tenant_id = j.tenant_id and e.event_id = any(ids) on conflict do nothing;
end $$;
create function wp_erasure_rescan_done(jid text, upto bigint, m text) returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare j record; missing bigint;
begin
  select tenant_id, subject_id, ident_xmax into j from erasure_jobs where job_id = jid;
  perform public.wp_matcher_check(j.tenant_id, m);
  if upto < coalesce(j.ident_xmax, 0) then
    raise exception 'rescan_incomplete: covers version % < identifier-set version %', upto, j.ident_xmax using errcode = 'WPS24';
  end if;
  select count(*) into missing from events e
   where e.tenant_id = j.tenant_id and e.subject_id <> j.subject_id
     and e.content_enc is not null
     and e.mention_scan_upto < upto and not (e.mentions @> array[j.subject_id])
     and not exists (select 1 from erasure_rescanned r where r.job_id = jid and r.event_id = e.event_id);
  if missing > 0 then
    raise exception 'rescan_incomplete: % candidate events not examined', missing using errcode = 'WPS24';
  end if;
  update erasure_jobs set rescanned_xmax = upto where job_id = jid and state in ('marked', 'cascaded');
end $$;
create function wp_subject_xmax(t text, s text) returns bigint language sql stable security definer set search_path = public, pg_temp as $$
  select coalesce(max(declared_seq), 0) from subject_identifiers where tenant_id = t and subject_id = s;
$$;

-- One bounded batch of tombstones and cascade deletes; returns the number of events tombstoned. Idempotent.
-- Apply one candidate batch: events covered by an active legal hold of ANOTHER subject (speaker, mentioned, or linked
-- by the rescan) are withheld (D1); the rest are tombstoned, their content nulled, derived facts and jobs deleted.
-- Item 2 (4960f50 round), from EXPLAIN at 200k: the planner estimated that `input_event_ids && <5,000 ids>` matches
-- every row and chose a sequential scan of analyst_jobs, evaluating the 5,000-element overlap on every live row of the
-- TENANT (19.4 s of a 20.4 s batch; cost per event proportional to tenant size). enable_seqscan = off was NOT enough
-- (scale_1m at 481afae: the planner then used the primary key on tenant_id with the overlap as a filter, still a scan
-- of the tenant). The deletes below probe the GIN once per event id instead, which the planner costs correctly.
create function wp_erasure_apply(t text, s text, jid text, cand text[]) returns int language plpgsql security definer set search_path = public, pg_temp set enable_seqscan = off as $$
declare ids text[];
begin
  if cardinality(cand) = 0 then return 0; end if;
  -- review E F4 (the hold class): the hold decision reads each event's footprint (speaker, mentions, erasure_mentions);
  -- erasure_mentions grows (wp_erasure_add_mentions), so the events are locked FIRST: a rescan link to one of them
  -- (FOR SHARE on the event) either committed before this lock, and is read below, or waits until this batch commits
  perform 1 from events e where e.tenant_id = t and e.event_id = any(cand) order by e.event_id for no key update;
  -- then share-lock every subject the decision judges, in the statement that reads the (now fixed) footprint: a hold
  -- (FOR UPDATE on the subject row) either committed before, and withholds the event, or waits for this batch
  perform 1 from subjects sj where sj.tenant_id = t and sj.subject_id in (
      select e.subject_id from events e where e.tenant_id = t and e.event_id = any(cand)
      union select unnest(e.mentions) from events e where e.tenant_id = t and e.event_id = any(cand)
      union select m.subject_id from erasure_mentions m where m.tenant_id = t and m.event_id = any(cand))
    order by sj.subject_id for share;
  insert into erasure_withheld(tenant_id, job_id, event_id, speaker, hold_ids)
    select t, jid, e.event_id, e.subject_id, string_agg(distinct h.hold_id, ',' order by h.hold_id)
      from events e join legal_holds h on h.tenant_id = t and h.released_at is null and h.subject_id <> s
       and (h.subject_id = e.subject_id or h.subject_id = any(e.mentions)
            or exists (select 1 from erasure_mentions m where m.tenant_id = t and m.event_id = e.event_id and m.subject_id = h.subject_id))
     where e.tenant_id = t and e.event_id = any(cand)
     group by e.event_id, e.subject_id
    on conflict (job_id, event_id) do update set hold_ids = excluded.hold_ids;
  ids := array(select x from unnest(cand) x where not exists (select 1 from erasure_withheld w where w.job_id = jid and w.event_id = x));
  insert into tombstones(tenant_id, event_id, subject_id, job_id) select t, unnest(ids), s, jid on conflict do nothing;
  update events set content_enc = null where tenant_id = t and event_id = any(ids) and content_enc is not null;
  if cardinality(ids) > 0 then
    -- one GIN probe per event (input_event_ids @> array[x]), then primary-key deletes: O(batch) at any tenant size.
    -- The probe sits behind an optimisation fence (lateral subquery with OFFSET 0) and the tenant is filtered outside
    -- it (event ids are uuids, so the probe returns that event's rows only): otherwise a plan with a low tenant
    -- estimate (plpgsql's generic plan from the 6th batch, or stale statistics) read every job of the tenant per event,
    -- 1,410 s for 100,000 events in the voided 1f059f4 run (THRESHOLDS amendment 18, exp/plan_stability.py). Inside
    -- the fence the only other path is a sequential scan of the whole table per event, which the planner picked on
    -- small or unanalysed tables; the function runs with enable_seqscan off, so the GIN index is the probe (18c).
    delete from analyst_jobs a where a.tenant_id = t and a.job_id = any(array(
      select a2.job_id from unnest(ids) x
        cross join lateral (select j2.job_id, j2.tenant_id from analyst_jobs j2 where j2.input_event_ids @> array[x] offset 0) a2
       where a2.tenant_id = t));
    delete from facts f where f.tenant_id = t and f.fact_id = any(array(
      select f2.fact_id from unnest(ids) x
        cross join lateral (select g.fact_id, g.tenant_id from facts g where g.input_event_ids @> array[x] offset 0) f2
       where f2.tenant_id = t));
  end if;
  return cardinality(ids);
end $$;

-- One cascade batch. Linear in the subject's events (D5): keyset cursors over the subject's own events (events_subject)
-- and over erasure_mentions (write-time mentions materialised at mark, plus rescan hits); then withheld events whose
-- holds have been released. Returns rows handled; 0 = nothing left that can be done now (withheld events may remain).
create function wp_erasure_cascade(jid text, batch int) returns int language plpgsql security definer set search_path = public, pg_temp as $$
declare j record; cand text[]; n int := 0; held int;
begin
  -- No FOR UPDATE on the job row for the batch (item 2 of the 4960f50 round): the claim heartbeat updates that row, and
  -- a batch-long row lock made the heartbeat time out on lock_timeout and declare the claim lost. The cursor advances
  -- last, conditionally on the value read here (optimistic): a concurrent batch or a rescan rewind makes it fail
  -- (WPS23) and this batch rolls back; every step is idempotent, so the caller simply runs the next batch.
  select * into j from erasure_jobs where job_id = jid;
  if j.state in ('requested', 'done') then raise exception 'cascade only after mark (state %)', j.state using errcode = 'WPS08'; end if;
  perform wp_hold_check(j.tenant_id, j.subject_id);         -- a hold placed after mark stops the cascade at the next batch
  cand := array(select event_id from events where tenant_id = j.tenant_id and subject_id = j.subject_id and event_id > j.own_after
                order by event_id limit batch);
  if cardinality(cand) > 0 then
    perform public.wp_erasure_apply(j.tenant_id, j.subject_id, jid, cand);
    update erasure_jobs set own_after = cand[cardinality(cand)] where job_id = jid and own_after = j.own_after;
    if not found then raise exception 'cascade cursor moved concurrently' using errcode = 'WPS23'; end if;
    return cardinality(cand);
  end if;
  cand := array(select event_id from erasure_mentions where tenant_id = j.tenant_id and subject_id = j.subject_id and event_id > j.men_after
                order by event_id limit batch);
  if cardinality(cand) > 0 then
    perform public.wp_erasure_apply(j.tenant_id, j.subject_id, jid, cand);
    update erasure_jobs set men_after = cand[cardinality(cand)] where job_id = jid and men_after = j.men_after;
    if not found then raise exception 'cascade cursor moved concurrently' using errcode = 'WPS23'; end if;
    return cardinality(cand);
  end if;
  -- withheld events: retried each pass; those whose holds are gone are applied now
  cand := array(select w.event_id from erasure_withheld w where w.job_id = jid);
  if cardinality(cand) > 0 then
    delete from erasure_withheld where job_id = jid;
    n := public.wp_erasure_apply(j.tenant_id, j.subject_id, jid, cand);
    if n > 0 then return n; end if;
  end if;
  delete from facts where tenant_id = j.tenant_id and subject_id = j.subject_id;
  delete from inbox where tenant_id = j.tenant_id and subject_id = j.subject_id;
  delete from quarantine where tenant_id = j.tenant_id and (subject_id = j.subject_id or mentions @> array[j.subject_id]);
  -- D3 sealed identifiers: never used for matching, and none can be added after the mark (propose and approve judge
  -- `erased` under the subject lock), so they go at the first final pass, whether or not a rescan ran (a subject with
  -- only a proposal has identifier-set version 0 and no rescan; at 4fe3ac1 its proposal was never deleted)
  delete from identifier_proposals where tenant_id = j.tenant_id and subject_id = j.subject_id;
  select count(*) into held from erasure_withheld where job_id = jid;
  if held = 0 and (select coalesce(rescanned_xmax, -1) >= coalesce(ident_xmax, 0) from erasure_jobs where job_id = jid) then
    delete from subject_identifiers where tenant_id = j.tenant_id and subject_id = j.subject_id;   -- only after the rescan used them
    update erasure_jobs set state = 'cascaded', updated_at = clock_timestamp() where job_id = jid and state = 'marked';
  end if;
  return 0;
end $$;

-- Postgres residue for a subject, computed by the store (never taken from a caller).
create function wp_erasure_pg_residue(t text, s text) returns jsonb language sql stable security definer set search_path = public, pg_temp set enable_seqscan = off as $$
  -- Item 3 (4960f50 round): every count is driven by the SUBJECT'S footprint through an index (events_subject, the
  -- mentions GIN, erasure_mentions, tombstones, facts_subject, the input_event_ids GINs), never by a scan of the tenant.
  -- The former OR of three predicates over events could use no index and read every event of the tenant.
  -- Amendment 18: this is a SQL function, planned with t and s unknown, so estimates are generic. The per-tombstone
  -- GIN probes sit behind a fence (lateral, OFFSET 0; tenant filtered outside) and the untombstoned count is an EXCEPT
  -- (hashed or sorted, never a nested loop): with a low estimate both had become quadratic in the subject's footprint.
  -- enable_seqscan is off for this function (18c): inside the fence the alternative to the GIN probe is a sequential
  -- scan of the whole table per tombstone.
  with fp as (select event_id from events where tenant_id = t and subject_id = s
              union select event_id from events where tenant_id = t and mentions @> array[s]
              union select event_id from erasure_mentions where tenant_id = t and subject_id = s)
  select jsonb_build_object(
    'pg:events_untombstoned', (select count(*) from (select event_id from fp
                                except select x.event_id from tombstones x where x.tenant_id = t and x.subject_id = s) u),
    'pg:content_present', (select count(*) from events e join tombstones tb on tb.tenant_id = e.tenant_id and tb.event_id = e.event_id
                           where e.tenant_id = t and tb.subject_id = s and e.content_enc is not null),
    'pg:mentions_unscanned', (select case when coalesce(jb.rescanned_xmax, -1) >= coalesce(jb.ident_xmax, 0) then 0
                              else (select count(*) from events e where e.tenant_id = t and e.subject_id <> s
                                    and e.content_enc is not null
                                    and e.mention_scan_upto < coalesce(jb.ident_xmax, 0)) end
                              from (select rescanned_xmax, ident_xmax from erasure_jobs where tenant_id = t and subject_id = s
                                    order by created_at desc limit 1) jb),
    'pg:identifiers', (select count(*) from subject_identifiers where tenant_id = t and subject_id = s),
    'pg:identifier_proposals', (select count(*) from identifier_proposals where tenant_id = t and subject_id = s),
    'pg:quarantine_mentions', (select count(*) from quarantine where tenant_id = t and mentions @> array[s]),
    'pg:facts', (select count(*) from (select fact_id from facts where tenant_id = t and subject_id = s
                   union select f.fact_id from tombstones x
                           cross join lateral (select g.fact_id, g.tenant_id from facts g where g.input_event_ids @> array[x.event_id] offset 0) f
                          where x.tenant_id = t and x.subject_id = s and f.tenant_id = t) y),
    'pg:inbox', (select count(*) from inbox where tenant_id = t and subject_id = s),
    'pg:quarantine', (select count(*) from quarantine where tenant_id = t and subject_id = s),
    'pg:analyst_jobs', (select count(distinct a.job_id) from tombstones x
                          cross join lateral (select j2.job_id, j2.tenant_id from analyst_jobs j2 where j2.input_event_ids @> array[x.event_id] offset 0) a
                          where x.tenant_id = t and x.subject_id = s and a.tenant_id = t),
    'pg:subject_not_marked', (select count(*) from subjects where tenant_id = t and subject_id = s and not erased),
    'pg:withheld_by_hold', (select count(*) from erasure_withheld w join erasure_jobs jb on jb.job_id = w.job_id where jb.tenant_id = t and jb.subject_id = s));
$$;

-- Item 4: the bound on 'physically purged' after 'logically erased'. ES: the peer-recovery retention lease advances on
-- its sync interval (30 s default; measured waits 22-30 s), then a merge; key service: the cluster xid/xmin horizon,
-- which the role timeouts and the transaction watchdog bound to ~30 s. 300 s leaves room for both plus retries; a job
-- past it is reported overdue (last_error_type physical_purge_overdue) until it completes.
create function wp_physical_purge_bound_s() returns int language sql immutable as $$ select 300 $$;

-- Completion: callable by wp_verifier only. ext = counts for every non-Postgres verifiable store, measured by the
-- verifier itself. Every registered verifiable store must be present and zero; Postgres counts are recomputed here.
create function wp_erasure_complete(jid text, ext jsonb, scope_note text) returns jsonb language plpgsql security definer set search_path = public, pg_temp as $$
declare j record; j2 record; v jsonb; bad text; lbad text; missing text; sc jsonb; hl jsonb;
begin
  select * into j from erasure_jobs where job_id = jid for update;
  if j.state not in ('keys_destroyed', 'logically_erased') then raise exception 'job not ready (state %)', j.state using errcode = 'WPS08'; end if;
  v := wp_erasure_pg_residue(j.tenant_id, j.subject_id) || coalesce(ext, '{}'::jsonb);
  select string_agg(name, ',') into missing from store_registry r where r.verifiable and r.kind <> 'pg' and not (coalesce(ext, '{}') ? r.name);
  if missing is not null then raise exception 'done_missing_stores %', missing using errcode = 'WPS08'; end if;
  select string_agg(k, ',') into bad from jsonb_each(v) as x(k, val) where jsonb_typeof(val) <> 'number' or (val #>> '{}')::numeric <> 0;
  if bad is not null then
    -- item 4: logically erased (every logical count zero) but bytes may remain: soft-deleted ES documents not yet
    -- merged away, or the key row's bytes not yet purged by the key service. 'done' waits; the receipt says so.
    select string_agg(k, ',') into lbad from jsonb_each(v) as x(k, val)
      where k not in ('es:soft_deleted', 'keys:physical_pending') and (jsonb_typeof(val) <> 'number' or (val #>> '{}')::numeric <> 0);
    if lbad is null then
      update erasure_jobs set state = 'logically_erased', verify = v,
             logical_at = coalesce(logical_at, clock_timestamp()),
             physical_deadline = coalesce(physical_deadline, clock_timestamp() + make_interval(secs => wp_physical_purge_bound_s())),
             physical_pending = (select jsonb_object_agg(k, val) from jsonb_each(v) as x(k, val)
                                  where k in ('es:soft_deleted', 'keys:physical_pending') and (val #>> '{}')::numeric <> 0),
             last_error_type = case when clock_timestamp() > coalesce(physical_deadline, 'infinity') then 'physical_purge_overdue' else null end,
             updated_at = clock_timestamp()
       where job_id = jid
       returning logical_at, physical_deadline, physical_pending, last_error_type into j2;
      return jsonb_build_object('done', false, 'receipt_state', 'logically_erased_physical_purge_pending', 'verify', v,
                                'logical_at', j2.logical_at, 'physical_deadline', j2.physical_deadline,
                                'physical_pending', j2.physical_pending, 'overdue', j2.last_error_type is not null,
                                'requested_at', j.created_at,
                                'logical_delay_s', round(extract(epoch from j2.logical_at - j.created_at)::numeric, 3));
    end if;
    update erasure_jobs set verify = v, last_error_type = 'residue', updated_at = clock_timestamp() where job_id = jid;
    return jsonb_build_object('done', false, 'residue', bad, 'verify', v);
  end if;
  sc := jsonb_build_object(
    'speaker', 'the subject''s own events, facts and analyst jobs derived from them, inbox and quarantine items, the subject key',
    'mentions', jsonb_build_object(
      'rule', 'every event that mentions the subject is removed whole, whoever wrote it: original content nulled in Postgres, '
              'placeholder in ES, derived facts and jobs deleted (D3; decision R1: no span redaction, decided by the coordinator '
              'on the user''s delegation, 2026-11-17)',
      'matcher', (select matcher from identifier_epochs where tenant_id = j.tenant_id),
      'matcher_rule', 'm5: whole-token-sequence match of the one identity key of each reviewed, declared identifier against every '
                      'reading of each text window (UTS 39 prototypes, case folded, default ignorables removed; in text a capital I '
                      'is read as i and l, a digit 1 inside a word as 1, l and i, a digit 0 inside a word as 0 and o; at most 4096 '
                      'readings per window, identifiers that could need more are refused; Unicode tables pinned by version: '
                      'wp/mentions.py)',
      'matcher_budget', 'm5 b1 (ASSUMPTION): event content at most 65,536 bytes; window keys per event at most 512 per token, '
                        'at least 131,072, at most 5,000,000; per call at most 1,000 events, 1,048,576 bytes and 5,000,000 keys; '
                        'writes over these are refused (WPM01, WPM02, WPM03) and never stored. The erasure rescan runs under the '
                        'same budget: an event admitted under another matcher that is over it stops the rescan, and such an '
                        'erasure CANNOT COMPLETE until a matcher migration re-admits that event; no migration exists yet. Worst '
                        'admitted event about 14.5 s of matcher CPU (measured on a Mac; store lookups not measured); there is no '
                        'per-tenant rate limit.',
      'other_speakers_removed', (select coalesce(jsonb_agg(tb.event_id order by tb.event_id), '[]') from tombstones tb
                                 join events e on e.tenant_id = tb.tenant_id and e.event_id = tb.event_id
                                 where tb.job_id = jid and e.subject_id <> j.subject_id),
      'declared_identifiers', j.ident_count, 'identifier_set_version', j.ident_xmax, 'rescan_covered_version', j.rescanned_xmax,
      'found_at_write', (select count(*) from tombstones tb join events e on e.tenant_id = tb.tenant_id and e.event_id = tb.event_id
                         where tb.tenant_id = j.tenant_id and tb.subject_id = j.subject_id and e.subject_id <> j.subject_id and e.mentions @> array[j.subject_id]),
      'found_by_rescan', (select count(*) from erasure_mentions where tenant_id = j.tenant_id and subject_id = j.subject_id and source = 'rescan'),
      'rescan_coverage', 'store-checked: every candidate event was examined (erasure_rescanned, WPS24)', 'rescan_match_attested_by', 'erasure runner (the store cannot decrypt the examined events)',
      'over_match', jsonb_build_array('confusable variants of a declared identifier (Kim and Kirn share a key): such events of other speakers are removed too',
                                      'a capital I (or a look-alike of it) in text is read as i and as l, so text in capitals also matches a name that differs by i and l (DELIA also matches Della, NEIL also matches Nell, HAILEY also matches Halley; AIEX KIM matches Alex Kim)',
                                      'a digit 1 or 0 inside a word in text is read as the letters too (Ne1l matches Neil and Nell; J0hn matches John; maria1 matches a declared Marial)',
                                      'one text window can hit several subjects (EIIa Moreno hits both Elia Moreno and Ella Moreno)'),
      'not_covered', jsonb_build_array('identifiers that were never declared (nicknames, misspellings, initials, inflected forms)', 'pronouns and indirect references',
                                       'look-alikes outside UTS 39 confusables.txt 18.0.0 (transliterations, homophones)',
                                       'a small l written for i or a small i for l (Allson or Aiison for Alison): distinct letters are never read as each other',
                                       'a look-alike of small l or small i from another script written for the other letter (Cyrillic palochka, Hebrew vav or Arabic alef for i; Cyrillic small i for l): read as one letter only',
                                       'a letter written for a digit (XQ-l00l for XQ-1001); digits written for letters in a word with no more letters than digits (A11s0n for Alison)',
                                       'a digit of another script that looks like 0 or 1 (read as the letter its prototype names)',
                                       'a capital of another script used as the look-alike of a Latin capital when its small form looks like another letter (Greek capital Beta, Nu; Latin capital I with stroke; matcher not_closed list)',
                                       'look-alikes that are punctuation or symbols (| for l): they end the token',
                                       'words of the subject quoted by others that contain no declared identifier',
                                       'names in scripts written without spaces (refused at declaration)',
                                       'mentions inside inbox payloads and analyst-job results',
                                       'content outside this store (ES snapshots, backups, logs: see store_registry ext:*)')),
    'timing', 'no completion-time bound under concurrent writes: the erasure mark waits for every in-flight share lock on '
              'the subject row (appends by or mentioning the subject, other subjects'' cascade batches, identifier declares), and '
              'new share locks are granted ahead of the waiting mark, so a sustained overlap of such writes delays it without '
              'bound; each attempt is cut by the role statement_timeout and the job stays open for the next sweep (exp/lock_order MS)',
    'note', scope_note);
  hl := (select coalesce(jsonb_agg(jsonb_build_object('hold_id', hold_id, 'placed_at', placed_at, 'released_at', released_at) order by placed_at), '[]')
         from legal_holds where tenant_id = j.tenant_id and subject_id = j.subject_id);
  update erasure_jobs set state = 'done', verify = v, scope = sc::text, claim_expires = null, updated_at = clock_timestamp() where job_id = jid;
  return jsonb_build_object('done', true, 'receipt_state', 'done', 'verify', v, 'scope', sc, 'holds', hl,
                            'requested_at', j.created_at, 'completed_at', clock_timestamp(),
                            'logical_at', coalesce(j.logical_at, clock_timestamp()),
                            'physical_deadline_met', j.physical_deadline is null or clock_timestamp() <= j.physical_deadline,
                            'delay_s', round(extract(epoch from clock_timestamp() - j.created_at)::numeric, 3));
end $$;

-- ------------------------------------------------------------------ guards on every table (enabled ALWAYS)
do $$
declare t text;
begin
  foreach t in array array['leases','subjects','chains','decision_heads','tombstones','store_registry','quarantine_quota',
                           'identifier_epochs','subject_identifiers','erasure_mentions','legal_holds','erasure_withheld','erasure_rescanned',
                           'identifier_proposals'] loop
    execute format('create trigger %I before insert or update or delete on %I for each row execute function wp_owner_only()', t || '_owner_only', t);
  end loop;
  foreach t in array array['inbox','analyst_jobs','facts','cursors','quarantine','erasure_jobs'] loop
    execute format('create trigger %I before delete on %I for each row execute function wp_owner_only()', t || '_delete_owner_only', t);
  end loop;
  execute 'create trigger quarantine_no_update before update on quarantine for each row execute function wp_append_only()';
  foreach t in array array['leases','subjects','chains','events','decision_heads','quarantine','quarantine_quota','tombstones','inbox',
                           'analyst_jobs','facts','cursors','store_registry','erasure_jobs',
                           'identifier_epochs','subject_identifiers','erasure_mentions','legal_holds','erasure_withheld','erasure_rescanned',
                           'identifier_proposals'] loop
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
grant usage on schema public to wp_app, wp_verifier, wp_legal, wp_broker;   -- wp_broker: wp_lease_holder_current only
grant usage on type tenant_t to wp_app, wp_verifier, wp_legal;
grant select on all tables in schema public to wp_verifier;
grant select, insert on events, quarantine to wp_app;
grant select, insert, update on inbox, analyst_jobs, facts, cursors to wp_app;
grant select, insert on erasure_jobs to wp_app;
grant update (state, attempts, last_error_type, claim_epoch, claim_expires, updated_at) on erasure_jobs to wp_app;
grant select on tombstones, store_registry, decision_heads, chains, subjects, identifier_epochs, subject_identifiers, erasure_mentions, legal_holds, erasure_withheld, erasure_rescanned to wp_app;
grant usage on sequence ingest_seq, fact_rev, quarantine_id_seq to wp_app;
grant execute on function wp_lease_acquire(tenant_t, text, double precision, boolean, text, int), wp_lease_renew(tenant_t, text, bigint, text, double precision),
  wp_lease_release(tenant_t, text, bigint, text), wp_subjects_ensure(tenant_t, text[]), wp_erasure_mark(text), wp_erasure_cascade(text, int),
  wp_erasure_pg_residue(text, text), wp_fence(text, text, bigint),
  wp_hold_check(text, text), wp_erasure_add_mentions(text, text[]),
  wp_identifier_propose(tenant_t, text, text, text, bytea, text),
  wp_erasure_rescan_done(text, bigint, text), wp_erasure_rescan_page(text, text[], text), wp_subject_xmax(text, text),
  wp_write_refused(tenant_t, text) to wp_app;
-- D1: holds are placed and released only by the legal-operations role; the application cannot lift a hold.
grant execute on function wp_hold_place(tenant_t, text, text, text), wp_hold_release(text) to wp_legal;
grant select on legal_holds, erasure_jobs, erasure_withheld to wp_legal;
grant execute on function wp_erasure_pg_residue(text, text), wp_erasure_complete(text, jsonb, text) to wp_verifier;
grant execute on function wp_erasure_rank(text) to wp_app, wp_verifier, wp_legal;   -- pure: the transition rule reads it in the guard
grant execute on function wp_lease_holder_current(text, text, bigint, text) to wp_broker;   -- D6: the broker's only grant
-- D3: reviewers read proposals and approve or reject; a reviewer may also propose (and then cannot approve its own).
-- wp_identifier_declare has no grant at all: it runs only inside wp_identifier_approve.
grant usage on schema public to wp_reviewer;
grant usage on type tenant_t to wp_reviewer;
grant select on identifier_proposals, subjects to wp_reviewer;
grant execute on function wp_identifier_approve(text, text, text), wp_identifier_reject(text, text),
  wp_identifier_propose(tenant_t, text, text, text, bytea, text) to wp_reviewer;
