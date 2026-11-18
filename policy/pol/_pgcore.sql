-- Decision core IN THE DATABASE (round 3, R3-7). Run by pol.pgcore.install as the superuser; every object is owned by dc_owner.
--
-- Boundary. The application role dc_app holds NO table, column or sequence privilege. It may EXECUTE only the session-keyed API
-- (dc.submit, dc.explain, dc.read_table, dc.erase_candidates, dc.erase_values), each SECURITY DEFINER with a pinned search_path. The
-- identity adapter's role dc_idp may EXECUTE only dc.open_session and dc.end_session. Identity (tenant, principal, channel) comes ONLY
-- from a dc.sessions row found by sha256(token); the row is written only by dc.open_session, and only from an authentication PROOF made
-- by an enrolled factor (a device or key: HMAC-SHA256 under the factor's key over tenant, principal, channel, factor, nonce, expiry).
-- Factor keys live in dc.factors, which no login role can read, so dc_idp relays proofs and cannot mint a session from a bare principal
-- id (step 2, R3-1). Actor and trust origin are derived from the session (dc._origin). Authority changes (revoke, grant) are applied inside the
-- same function, from the event that function built and judged: there is no separately callable apply step.
--
-- Acquire, judge, apply (S1; FINAL-JUDGMENT.md section 4, DESIGN.md "Final judgment after the last wait"). Every session-keyed call
-- and dc.open_session takes EVERY lock it needs before it judges, in one global order (L0 relation locks, L1 the authority lock, L2
-- the authority rows FOR SHARE, L3 the tenant lock, L4 the idempotency lock, L5 data rows, L6 subject locks, L7 the chain head, L8 the
-- nonce lock), each wait capped by the credential's remaining life (dc._budget). The authority rows are pinned, so no transaction can
-- change the caller's session, factor or principal until the call ends: a revoke, end_session or factor revoke waits for the call.
-- Then lock_timeout drops to 1 ms: the staged writes, the final judgment (dc._gate, the wall clock read last) and a revoke's authority
-- writes never wait; a wait there fails the call (55P03) and writes nothing. A refusal rolls the stage back; its audit row stays.
--
-- The rules are CORE-RULES.md, ported rule by rule from pol/_core.py (the Python core stays as the differential oracle). Labels are
-- normalised and joined here exactly as pol.labels does (sorted with COLLATE "C", which orders UTF-8 by code point like Python).
-- Label filtering of READS stays in the in-process policy point (pol.enforce): dc.explain returns raw state to a verified participant
-- and the Python service filters it by label. That part is trusted in-process code, not enforced by the database (disclosed).
--
-- ASSUMPTION (round 3, not confirmed by the user): non-humans only propose and remark; the per-req cap binds non-human authors only;
-- idempotency is per (tenant, principal, idem) with a fingerprint over the event, never the session token.

-- Review of 2262eaa F2a: refuse to install on a cluster that allows prepared transactions (see dc._no2pc)
do $$
begin
  if current_setting('max_prepared_transactions')::int > 0 then
    raise exception 'install refused: max_prepared_transactions > 0 (a prepared transaction could hold authority locks no revocation can end)'
      using errcode = '55000';
  end if;
end $$;
drop schema if exists dc cascade;
create schema dc authorization dc_owner;
create schema if not exists dc_crypto authorization dc_owner;
create extension if not exists pgcrypto with schema dc_crypto;
set role dc_owner;

-- Review of 2262eaa, F1: the lock identity of a tenant and of a principal is an exact surrogate number (lockid), unique in the database
-- and never changed, so two tenants or two principals never share an authority lock A, a tenant lock T or their fences (a 32-bit hash
-- of the names did: one tenant's admin could evict and fence another tenant's backends through a collision). A name with no row has
-- no lockid, so nothing can be locked, evicted or fenced in its name. Range 1..1e9: below every fixed advisory class (1145241601-6).
create sequence dc.lockid_seq as int minvalue 1 maxvalue 1000000000 no cycle;
create table dc.tenant(tenant text primary key, lk int not null default 0, lockid int not null unique default nextval('dc.lockid_seq'));
create table dc.principal(tenant text not null, pid text not null, kind text not null, admin bool not null, revoked bool not null default false,
                          lockid int not null unique default nextval('dc.lockid_seq'), primary key (tenant, pid));
create function dc.lockid_frozen() returns trigger language plpgsql as $$
begin
  if new.lockid is distinct from old.lockid then raise exception 'dc.%.lockid is fixed at insert', tg_table_name; end if;
  return new;
end $$;
create trigger tenant_lockid before update on dc.tenant for each row execute function dc.lockid_frozen();
create trigger principal_lockid before update on dc.principal for each row execute function dc.lockid_frozen();
create table dc.role(tenant text not null, req text not null, pid text not null, role text not null, primary key (tenant, req, pid, role));
-- open_n: open items with a NON-HUMAN author (rule 9, R3-4), maintained by the delta ops: O(1) cap check
create table dc.head(tenant text not null, req text not null, label jsonb not null, open_n int not null default 0, doc jsonb not null,
                     primary key (tenant, req));
create table dc.props(tenant text not null, req text not null, id text not null, kind text not null, author text not null, status text not null,
                      doc jsonb not null, primary key (tenant, req, id));
create index props_open_by_author on dc.props(tenant, req, author) where status = 'open';
create index props_open_author_all on dc.props(tenant, author) where status = 'open';
create table dc.chain(n bigserial primary key, tenant text not null, req text not null, doc jsonb not null);
create table dc.events(seq bigserial primary key, tenant text not null, principal text not null, idem text not null, req text, evid text not null,
                       fp text not null, code text not null, ev jsonb not null, chain_n bigint, prev_hash text, hash text);
create unique index events_idem on dc.events(tenant, principal, idem);
-- grants are exempt from the duplicate-id rule (CORE-RULES rule 6 precedes rule 8), so the backstop index excludes them (found by exp/pg_diff)
create unique index events_id on dc.events(tenant, req, evid) where req is not null and code <> 'granted';
-- the duplicate-id lookup in dc._reduce (every event) cannot use the partial index above (its predicate names no code), so without this
-- one it scans the tenant's events: per-event cost grew with the log (tests.test_pg proposal-cost test failed at f66f76c, 4.0x > 3x)
create index events_by_id on dc.events(tenant, req, evid);
create table dc.chainhead(tenant text primary key, n bigint not null default 0, hash text not null);
create table dc.audit(n bigserial primary key, tenant text, principal text, code text not null, original text, ev jsonb);
-- the vault: value text, its label as submitted (subject NAMES, needed to find what to erase), erased on request
create table dc.vals(tenant text not null, ref text not null, text text not null, label jsonb not null, subjects text[] not null,
                     primary key (tenant, ref));
create index vals_subjects on dc.vals using gin(subjects);
-- random token per (tenant, canonical subject); erasure deletes the row, after which a token in the log links to nothing (R3-3)
create table dc.subject_tokens(tenant text not null, subject text not null, token text not null unique, primary key (tenant, subject));
create table dc.sessions(token_hash text primary key, tenant text not null, principal text not null, channel text not null, factor text not null,
                         expires_at timestamptz not null, revoked bool not null default false);
-- a revoke locks and ends every session of its target (S1, L2.4)
create index sessions_by_principal on dc.sessions(tenant, principal);
-- step 2: enrolled factors and the proofs already used (replay ledger, pruned after expiry). Owner-only: no login role holds any privilege.
create table dc.factors(factor text primary key, tenant text not null, principal text not null, channel text not null, key bytea not null,
                        revoked bool not null default false, enrolled_at timestamptz not null default now(),
                        foreign key (tenant, principal) references dc.principal(tenant, pid));
create table dc.used_proofs(factor text not null references dc.factors(factor), nonce text not null, expires timestamptz not null,
                            primary key (factor, nonce));

-- ------------------------------------------------------------------------------------------------ append-only, chained log
create function dc.events_chain() returns trigger language plpgsql security definer set search_path = dc, pg_temp as $$
#variable_conflict use_variable
declare h text; k bigint;
begin
  select hash, n + 1 into h, k from dc.chainhead where tenant = new.tenant for update;
  if h is null then raise exception 'no chain head for tenant %', new.tenant; end if;
  new.chain_n := k;
  new.prev_hash := h;
  new.hash := encode(sha256(convert_to(h || '|' || new.tenant || '|' || k::text || '|' || new.code || '|' || new.ev::text, 'UTF8')), 'hex');
  update dc.chainhead set hash = new.hash, n = k where tenant = new.tenant;
  return new;
end $$;
create trigger events_chain before insert on dc.events for each row execute function dc.events_chain();

create function dc.events_frozen() returns trigger language plpgsql as $$
#variable_conflict use_variable
begin raise exception 'dc.events is append-only (% refused)', tg_op; end $$;
create trigger events_no_update before update or delete on dc.events for each row execute function dc.events_frozen();
create trigger events_no_truncate before truncate on dc.events for each statement execute function dc.events_frozen();

create function dc.verify_chain(t text) returns table(n bigint, ok bool) language plpgsql security definer set search_path = dc, pg_temp as $$
#variable_conflict use_variable
declare h text := repeat('0', 64); r record; good bool := true; cnt bigint := 0;
begin
  for r in select * from dc.events e where e.tenant = t order by e.chain_n loop
    cnt := cnt + 1;
    if r.chain_n <> cnt or r.prev_hash <> h or r.hash <> encode(sha256(convert_to(h || '|' || r.tenant || '|' || r.chain_n::text || '|' || r.code || '|' || r.ev::text, 'UTF8')), 'hex') then
      good := false;
    end if;
    h := r.hash;
  end loop;
  if h <> (select c.hash from dc.chainhead c where c.tenant = t) then good := false; end if;
  return query select cnt, good;
end $$;

-- ------------------------------------------------------------------------------------------------ labels (pol.labels, ported)
create function dc._canon_subject(x text) returns text language plpgsql immutable as $$
#variable_conflict use_variable
declare y text;
begin
  y := btrim(regexp_replace(lower(normalize(x, NFKC)), '[[:space:]]+', ' ', 'g'), ' ');
  y := normalize(y, NFKC);
  if y = '' or y ~ '[\x01-\x1f\x7f-\x9f]' then raise exception 'subject: empty or control characters'; end if;
  return y;
end $$;

create function dc._strset(name text, v jsonb, allow_null bool, canon_subjects bool default false) returns jsonb language plpgsql immutable as $$
#variable_conflict use_variable
declare out jsonb;
begin
  if v is null or jsonb_typeof(v) = 'null' then
    if allow_null then return 'null'::jsonb; end if;
    raise exception '%: not a collection', name;
  end if;
  if jsonb_typeof(v) <> 'array' then raise exception '%: not a collection', name; end if;
  if exists (select 1 from jsonb_array_elements(v) e where jsonb_typeof(e) <> 'string' or e #>> '{}' = '') then
    raise exception '%: members must be non-empty str', name;
  end if;
  select coalesce(jsonb_agg(to_jsonb(x) order by x collate "C"), '[]'::jsonb) into out
    from (select distinct case when canon_subjects then dc._canon_subject(e #>> '{}') else e #>> '{}' end as x from jsonb_array_elements(v) e) s;
  return out;
end $$;

-- Label.from_dict(d).to_dict(): validation, sets sorted, ACL clauses sorted and absorbed, subjects canonicalised
create function dc._label_norm(d jsonb) returns jsonb language plpgsql immutable as $$
#variable_conflict use_variable
declare keys text[] := array['tenants','acl','regions','sensitivity','models','banned','local_only','restricted','holds','subjects'];
        t jsonb; acl jsonb; c jsonb; cl text[][]; i int; s text;
begin
  if d is null or jsonb_typeof(d) <> 'object' then raise exception 'label: not a dict'; end if;
  if (select array_agg(k order by k) from jsonb_object_keys(d) k) is distinct from (select array_agg(k order by k) from unnest(keys) k) then
    raise exception 'label dict must have exactly the dimension keys';
  end if;
  t := dc._strset('tenants', d->'tenants', false);
  if jsonb_array_length(t) = 0 then raise exception 'tenants: empty'; end if;
  if jsonb_typeof(d->'acl') <> 'array' then raise exception 'acl: not a collection'; end if;
  for c in select e from jsonb_array_elements(d->'acl') e loop
    if jsonb_array_length(dc._strset('acl clause', c, false)) = 0 then raise exception 'acl: empty clause'; end if;
  end loop;
  with clauses as (
         select distinct array(select x from jsonb_array_elements_text(dc._strset('acl clause', e, false)) x order by x collate "C") as cl
           from jsonb_array_elements(d->'acl') e),
       kept as (select a.cl from clauses a where not exists (select 1 from clauses b where b.cl <@ a.cl and not a.cl <@ b.cl))
  select coalesce(jsonb_agg(to_jsonb(k.cl) order by k.cl collate "C"), '[]'::jsonb) into acl from kept k;
  s := d->>'sensitivity';
  if jsonb_typeof(d->'sensitivity') <> 'number' or s !~ '^[0-3]$' then raise exception 'sensitivity: int 0..3'; end if;
  if jsonb_typeof(d->'local_only') <> 'boolean' or jsonb_typeof(d->'restricted') <> 'boolean' then raise exception 'flags must be bool'; end if;
  return jsonb_build_object('tenants', t, 'acl', acl, 'regions', dc._strset('regions', d->'regions', true), 'sensitivity', s::int,
                            'models', dc._strset('models', d->'models', true), 'banned', dc._strset('banned', d->'banned', false),
                            'local_only', d->'local_only', 'restricted', d->'restricted', 'holds', dc._strset('holds', d->'holds', false),
                            'subjects', dc._strset('subjects', d->'subjects', false, true));
end $$;

create function dc._inter(a jsonb, b jsonb) returns jsonb language sql immutable as $$
  select case when a is null or jsonb_typeof(a) = 'null' then b when b is null or jsonb_typeof(b) = 'null' then a
              else coalesce((select jsonb_agg(x) from jsonb_array_elements(a) x where b @> jsonb_build_array(x)), '[]'::jsonb) end
$$;

create function dc._label_join2(a jsonb, b jsonb) returns jsonb language plpgsql immutable as $$
#variable_conflict use_variable
begin
  a := dc._label_norm(a); b := dc._label_norm(b);
  return dc._label_norm(jsonb_build_object(
    'tenants', (a->'tenants') || (b->'tenants'), 'acl', (a->'acl') || (b->'acl'), 'regions', dc._inter(a->'regions', b->'regions'),
    'sensitivity', greatest((a->>'sensitivity')::int, (b->>'sensitivity')::int), 'models', dc._inter(a->'models', b->'models'),
    'banned', (a->'banned') || (b->'banned'), 'local_only', (a->>'local_only')::bool or (b->>'local_only')::bool,
    'restricted', (a->>'restricted')::bool or (b->>'restricted')::bool, 'holds', (a->'holds') || (b->'holds'),
    'subjects', (a->'subjects') || (b->'subjects')));
end $$;

create function dc._jl(a jsonb, b jsonb, c jsonb default null) returns jsonb language sql immutable as $$
  select case when c is null then dc._label_join2(a, b) else dc._label_join2(dc._label_join2(a, b), c) end
$$;

-- json.dumps(s) with ensure_ascii (Python's escaping, byte for byte), for proposal_hash
create function dc._pyjson_str(s text) returns text language plpgsql immutable as $$
#variable_conflict use_variable
declare out text := '"'; ch text; n int; hi int; lo int; i int;
begin
  for i in 1 .. length(s) loop
    ch := substr(s, i, 1); n := ascii(ch);
    if ch = '"' then out := out || '\"';
    elsif ch = '\' then out := out || '\\';
    elsif n = 8 then out := out || '\b';
    elsif n = 12 then out := out || '\f';
    elsif n = 10 then out := out || '\n';
    elsif n = 13 then out := out || '\r';
    elsif n = 9 then out := out || '\t';
    elsif n >= 32 and n <= 126 then out := out || ch;
    elsif n > 65535 then
      n := n - 65536; hi := 55296 + (n >> 10); lo := 56320 + (n & 1023);
      out := out || '\u' || lpad(to_hex(hi), 4, '0') || '\u' || lpad(to_hex(lo), 4, '0');
    else out := out || '\u' || lpad(to_hex(n), 4, '0');
    end if;
  end loop;
  return out || '"';
end $$;

create function dc._proposal_hash(pid text, ref text, author text) returns text language sql immutable as $$
  select encode(sha256(convert_to('[' || dc._pyjson_str(pid) || ',' || dc._pyjson_str(ref) || ',' || dc._pyjson_str(author) || ']', 'UTF8')), 'hex')
$$;

-- ------------------------------------------------------------------------------------------------ event shape (_core._shape)
create function dc._is_str(ev jsonb, k text) returns bool language sql immutable as $$
  select jsonb_typeof(ev->k) = 'string' and ev->>k <> ''
$$;

create function dc._shape(ev jsonb) returns bool language plpgsql immutable as $$
#variable_conflict use_variable
declare t text; v text; need text[]; k text;
  str_fields text[] := array['id','idem','tenant','principal','channel','req','value_ref','proposal','proposal_hash','pending','choose','target','verb','role'];
begin
  if ev is null or jsonb_typeof(ev) <> 'object' or jsonb_typeof(ev->'type') is distinct from 'string' then return false; end if;
  t := ev->>'type';
  need := array['type','id','idem','tenant','principal','channel'];
  case t
    when 'propose', 'remark', 'supersede' then need := need || array['req','value_ref','label','based_on'];
    when 'confirm' then need := need || array['req','pending','based_on'];
    when 'decide' then need := need || array['req','verb','based_on'];
    when 'retract' then need := need || array['req','based_on'];
    when 'revoke' then need := need || array['target'];
    when 'grant' then need := need || array['req','target','role'];
    else return false;
  end case;
  if t = 'decide' then
    v := case when jsonb_typeof(ev->'verb') = 'string' then ev->>'verb' end;
    if v in ('accept', 'reject') then need := need || array['proposal','proposal_hash'];
    elsif v = 'amend' then need := need || array['proposal','proposal_hash','value_ref','label'];
    elsif v = 'resolve' and not ev ? 'choose' then need := need || array['value_ref','label'];
    end if;
  end if;
  foreach k in array need loop
    if not ev ? k then return false; end if;
  end loop;
  foreach k in array str_fields loop
    if ev ? k and not dc._is_str(ev, k) then return false; end if;
  end loop;
  if ev ? 'based_on' and (jsonb_typeof(ev->'based_on') <> 'number' or ev->>'based_on' !~ '^[0-9]+$') then return false; end if;
  if t = 'grant' and ev->>'role' not in ('decide', 'propose') then return false; end if;
  if ev ? 'label' then
    begin
      perform dc._label_norm(ev->'label');
    exception when others then
      return false;
    end;
  end if;
  return true;
end $$;

-- ------------------------------------------------------------------------------------------------ the rules (_core._rules)
create function dc._prev(h jsonb) returns jsonb language sql immutable as $$
  select jsonb_build_object('value_ref', h->'value_ref', 'status', h->'status', 'value_event', h->'value_event', 'value_by', h->'value_by',
                            'value_version', h->'value_version', 'value_label', h->'value_label')
$$;

create function dc._chain_op(req text, h jsonb) returns jsonb language sql immutable as $$
  select case when jsonb_typeof(h->'value_ref') = 'null' then '[]'::jsonb else jsonb_build_array(jsonb_build_array('chain', req,
    jsonb_build_object('value_ref', h->'value_ref', 'value_version', h->'value_version', 'value_event', h->'value_event',
                       'value_by', h->'value_by', 'value_label', h->'value_label'))) end
$$;

-- _set / _retract: return the new head; the chain op goes to the caller's delta
create function dc._set(h jsonb, ref jsonb, lab jsonb, evid text, pid text) returns jsonb language sql immutable as $$
  select h || jsonb_build_object('value_ref', ref, 'status', 'current', 'value_event', evid, 'value_by', pid, 'value_label', coalesce(lab, 'null'::jsonb),
                                 'value_version', (h->>'version')::bigint + 1, 'contested', null)
$$;

create function dc._retract(h jsonb, evid text, pid text) returns jsonb language sql immutable as $$
  select h || jsonb_build_object('value_ref', null, 'status', 'retracted', 'value_event', evid, 'value_by', pid, 'value_label', null,
                                 'value_version', (h->>'version')::bigint + 1, 'contested', null)
$$;

create function dc._adopted_open(prop jsonb, pend jsonb, closers jsonb) returns bool language sql immutable as $$
  select case when coalesce(prop, pend) is null then true
              else coalesce(prop, pend)->>'status' = 'open' or (jsonb_typeof(coalesce(prop, pend)->'closed_by') = 'string'
                                                                and closers @> jsonb_build_array(coalesce(prop, pend)->'closed_by')) end
$$;

create function dc._rules(p_tenant text, ev jsonb, out code text, out delta jsonb) language plpgsql as $$
#variable_conflict use_variable
declare t text := ev->>'type'; pid text := ev->>'principal'; kind text; adm bool; rev bool; req text; human bool; chan text; verb text;
        pend jsonb; prop jsonb; h jsonb; based bigint; rl jsonb; eff jsonb; elab jsonb; c jsonb; lst jsonb; sib bool; prev jsonb;
        cand jsonb; lab jsonb; prior jsonb; doc jsonb; tk text; n int;
begin
  delta := '[]'::jsonb;
  select p.kind, p.admin, p.revoked into kind, adm, rev from dc.principal p where p.tenant = p_tenant and p.pid = ev->>'principal';
  if kind is null then code := 'unauthorized'; return; end if;
  if rev then code := 'principal_revoked'; return; end if;
  if t = 'revoke' then
    if not adm then code := 'unauthorized'; return; end if;
    if not exists (select 1 from dc.principal p where p.tenant = p_tenant and p.pid = ev->>'target') then code := 'bad_event'; return; end if;
    code := 'revoked'; delta := jsonb_build_array(jsonb_build_array('revoke', ev->>'target')); return;
  end if;
  if t = 'grant' then
    if not adm then code := 'unauthorized'; return; end if;
    select p.kind, p.revoked into tk, rev from dc.principal p where p.tenant = p_tenant and p.pid = ev->>'target';
    if not exists (select 1 from dc.head x where x.tenant = p_tenant and x.req = ev->>'req') or tk is null or rev then
      code := 'bad_event'; return;
    end if;
    code := 'granted'; delta := jsonb_build_array(jsonb_build_array('grant', ev->>'req', ev->>'role', ev->>'target')); return;
  end if;
  req := ev->>'req';
  select x.doc, x.label, x.open_n into h, rl, n from dc.head x where x.tenant = p_tenant and x.req = req;
  if h is null then code := 'unknown_req'; return; end if;
  if exists (select 1 from dc.events e where e.tenant = p_tenant and e.req = req and e.evid = ev->>'id') then code := 'duplicate_id'; return; end if;
  if t in ('propose', 'remark') then
    if not exists (select 1 from dc.role r where r.tenant = p_tenant and r.req = req and r.pid = pid and r.role in ('decide', 'propose')) then
      code := 'unauthorized'; return;
    end if;
    if (select count(*) from dc.props x where x.tenant = p_tenant and x.req = req and x.author = pid and x.status = 'open') >= 16
       or (kind <> 'human' and n >= 256) then
      code := 'proposal_cap'; return;
    end if;
    doc := jsonb_build_object('id', ev->'id', 'kind', case when t = 'propose' then 'proposal' else 'pending' end, 'author', pid, 'author_kind', kind,
                              'value_ref', ev->'value_ref', 'label', dc._label_norm(ev->'label'), 'status', 'open', 'closed_by', null,
                              'base', ev->'based_on', 'channel', case when t = 'remark' then ev->'channel' else 'null'::jsonb end);
    code := case when t = 'propose' then 'proposed' else 'pending_created' end;
    delta := jsonb_build_array(jsonb_build_array('prop_new', req, doc)); return;
  end if;

  -- head-moving types
  if not exists (select 1 from dc.role r where r.tenant = p_tenant and r.req = req and r.pid = pid and r.role = 'decide') then
    code := 'unauthorized'; return;
  end if;
  human := kind = 'human';
  chan := ev->>'channel';
  if (human and chan not in ('owner_ui', 'owner_cli')) or (not human and chan not in ('agent_api')) then code := 'channel_mismatch'; return; end if;
  verb := case when t = 'decide' then ev->>'verb' end;
  if not human then                                   -- round 3 (ASSUMPTION): non-humans only propose and remark
    code := case when t = 'confirm' then 'agent_cannot_confirm' else 'agent_not_permitted' end; return;
  end if;
  if t = 'confirm' then
    select x.doc into pend from dc.props x where x.tenant = p_tenant and x.req = req and x.id = ev->>'pending';
    if pend is null or pend->>'kind' <> 'pending' then code := 'unknown_pending'; return; end if;
    if pend->>'status' <> 'open' then code := 'pending_closed'; return; end if;
    if pend->>'channel' = chan then code := 'same_channel_confirmation'; return; end if;
  end if;
  if t = 'decide' then
    if verb not in ('accept', 'reject', 'amend', 'resolve') then code := 'bad_event'; return; end if;
    if verb in ('accept', 'reject', 'amend') then
      select x.doc into prop from dc.props x where x.tenant = p_tenant and x.req = req and x.id = ev->>'proposal';
      if prop is null or prop->>'kind' <> 'proposal' then code := 'unknown_proposal'; return; end if;
      if ev->>'proposal_hash' <> dc._proposal_hash(prop->>'id', prop->>'value_ref', prop->>'author') then code := 'proposal_mismatch'; return; end if;
    end if;
  end if;
  based := (ev->>'based_on')::bigint;
  eff := case when t = 'decide' and verb = 'accept' then jsonb_build_array('set', prop->'value_ref')
              when t = 'decide' and verb = 'reject' then jsonb_build_array('reject', prop->'id')
              when t = 'decide' and verb = 'amend' then jsonb_build_array('set', ev->'value_ref')
              when t = 'supersede' then jsonb_build_array('set', ev->'value_ref')
              when t = 'retract' then jsonb_build_array('retract')
              when t = 'confirm' then jsonb_build_array('set', pend->'value_ref') end;
  elab := case when t = 'decide' and verb = 'accept' then dc._jl(rl, prop->'label')
               when t = 'decide' and verb = 'amend' then dc._jl(rl, prop->'label', ev->'label')
               when t = 'confirm' then dc._jl(rl, pend->'label')
               when t = 'supersede' then dc._jl(rl, ev->'label') end;

  if h->>'status' = 'contested' then
    c := h->'contested';
    if verb = 'resolve' then
      if based <> (h->>'version')::bigint then code := 'stale'; return; end if;
      -- _resolve
      if ev ? 'choose' then
        select x into cand from jsonb_array_elements(c->'candidates') x where x->>'event' = ev->>'choose' limit 1;
        if cand is null then code := 'unknown_candidate'; return; end if;
        eff := cand->'effect'; lab := cand->'label';
      else
        eff := jsonb_build_array('set', ev->'value_ref'); lab := dc._jl(rl, ev->'label');
      end if;
      prior := c->'prior';
      h := h || prior || jsonb_build_object('contested', null);
      if eff->>0 = 'set' then
        delta := delta || dc._chain_op(req, h); h := dc._set(h, eff->1, lab, ev->>'id', pid);
      elsif eff->>0 = 'retract' then
        delta := delta || dc._chain_op(req, h); h := dc._retract(h, ev->>'id', pid); lab := null;
      else
        delta := delta || jsonb_build_array(jsonb_build_array('prop_close', req, eff->>1, 'rejected', ev->>'id')); lab := null;
      end if;
      h := h || jsonb_build_object('version', (h->>'version')::bigint + 1, 'last',
             jsonb_build_object('event', ev->'id', 'by', pid, 'based_on', ev->'based_on', 'effect', eff, 'verb', 'resolve', 'prev', prior,
                                'label', coalesce(lab, 'null'::jsonb)));
      code := 'resolved'; delta := delta || jsonb_build_array(jsonb_build_array('head', req, h)); return;
    end if;
    if based = (c->>'base')::bigint then
      if jsonb_array_length(c->'candidates') >= 8 then code := 'candidates_full'; return; end if;
      if not dc._adopted_open(prop, pend, (select coalesce(jsonb_agg(x->'event'), '[]'::jsonb) from jsonb_array_elements(c->'candidates') x)) then
        code := 'proposal_closed'; return;
      end if;
      c := jsonb_set(c, '{candidates}', (c->'candidates') || jsonb_build_array(jsonb_build_object('event', ev->'id', 'by', pid,
             'effect', coalesce(eff, 'null'::jsonb), 'verb', coalesce(verb, t), 'label', coalesce(elab, 'null'::jsonb))));
      h := jsonb_set(h, '{contested}', c);
      code := 'contested_joined'; delta := jsonb_build_array(jsonb_build_array('head', req, h)); return;
    end if;
    if based = (h->>'version')::bigint then code := 'contested_needs_resolve'; return; end if;
    code := 'stale'; return;
  end if;
  if verb = 'resolve' then
    code := case when based <> (h->>'version')::bigint then 'stale' else 'not_contested' end; return;
  end if;

  if based <> (h->>'version')::bigint then
    lst := h->'last';
    sib := lst is not null and jsonb_typeof(lst) <> 'null' and based = (lst->>'based_on')::bigint and (h->>'version')::bigint = based + 1
           and lst->>'by' <> pid and lst->>'verb' <> 'resolve';
    if not coalesce(sib, false) then code := 'stale'; return; end if;
    if not dc._adopted_open(prop, pend, jsonb_build_array(lst->'event')) then code := 'proposal_closed'; return; end if;
    if eff is not distinct from (lst->'effect') then code := 'concurrent_agree'; return; end if;
    h := h || jsonb_build_object('contested', jsonb_build_object('base', based, 'prior', lst->'prev', 'candidates', jsonb_build_array(
             jsonb_build_object('event', lst->'event', 'by', lst->'by', 'effect', lst->'effect', 'verb', lst->'verb', 'label', lst->'label'),
             jsonb_build_object('event', ev->'id', 'by', pid, 'effect', coalesce(eff, 'null'::jsonb), 'verb', coalesce(verb, t),
                                'label', coalesce(elab, 'null'::jsonb)))),
             'status', 'contested', 'value_ref', null, 'version', (h->>'version')::bigint + 1, 'last', null);
    code := 'contested_opened'; delta := jsonb_build_array(jsonb_build_array('head', req, h)); return;
  end if;

  -- rule 15: normal application at the head
  prev := dc._prev(h);
  if t = 'decide' then
    if prop->>'status' <> 'open' then code := 'proposal_closed'; return; end if;
    if verb = 'reject' then
      code := 'decided_reject';
      delta := delta || jsonb_build_array(jsonb_build_array('prop_close', req, prop->>'id', 'rejected', ev->>'id'));
    else
      code := case when verb = 'accept' then 'decided_accept' else 'decided_amend' end;
      delta := delta || jsonb_build_array(jsonb_build_array('prop_close', req, prop->>'id', 'accepted', ev->>'id'));
      delta := delta || dc._chain_op(req, h); h := dc._set(h, eff->1, elab, ev->>'id', pid);
    end if;
  elsif t = 'supersede' then
    code := 'superseded';
    delta := delta || dc._chain_op(req, h); h := dc._set(h, eff->1, elab, ev->>'id', pid);
  elsif t = 'retract' then
    if h->>'status' in ('unset', 'retracted') then code := 'nothing_to_retract'; delta := '[]'::jsonb; return; end if;
    code := 'retracted';
    delta := delta || dc._chain_op(req, h); h := dc._retract(h, ev->>'id', pid);
  else
    code := 'confirmed_applied';
    delta := delta || jsonb_build_array(jsonb_build_array('prop_close', req, pend->>'id', 'confirmed', ev->>'id'));
    delta := delta || dc._chain_op(req, h); h := dc._set(h, eff->1, elab, ev->>'id', pid);
  end if;
  h := h || jsonb_build_object('version', (h->>'version')::bigint + 1, 'last',
         jsonb_build_object('event', ev->'id', 'by', pid, 'based_on', based, 'effect', coalesce(eff, 'null'::jsonb), 'verb', coalesce(verb, t),
                            'prev', prev, 'label', coalesce(elab, 'null'::jsonb)));
  delta := delta || jsonb_build_array(jsonb_build_array('head', req, h));
end $$;

-- ------------------------------------------------------------------------------------------------ delta, event row, refusal audit
create function dc._apply_delta(p_tenant text, delta jsonb) returns void language plpgsql as $$
#variable_conflict use_variable
declare op jsonb; k text; was text; ak text; tgt text;
begin
  for op in select x from jsonb_array_elements(delta) x loop
    k := op->>0;
    if k = 'head' then
      update dc.head set doc = op->2 where tenant = p_tenant and req = op->>1;
    elsif k = 'prop_new' then
      insert into dc.props values (p_tenant, op->>1, op->2->>'id', op->2->>'kind', op->2->>'author', 'open', op->2);
      if op->2->>'author_kind' <> 'human' then update dc.head set open_n = open_n + 1 where tenant = p_tenant and req = op->>1; end if;
    elsif k = 'prop_close' then
      select status, doc->>'author_kind' into was, ak from dc.props where tenant = p_tenant and req = op->>1 and id = op->>2 for update;
      update dc.props set status = op->>3, doc = doc || jsonb_build_object('status', op->>3, 'closed_by', op->>4)
        where tenant = p_tenant and req = op->>1 and id = op->>2;
      if was = 'open' and ak <> 'human' then update dc.head set open_n = open_n - 1 where tenant = p_tenant and req = op->>1; end if;
    elsif k = 'chain' then
      insert into dc.chain(tenant, req, doc) values (p_tenant, op->>1, op->2);
    elsif k = 'revoke' then
      -- the head counts and the target's open proposals only: the target's principal row and sessions are the authority the final
      -- judgment reads, written by dc._apply_authority after that judgment (final review P1)
      tgt := op->>1;
      update dc.head h set open_n = h.open_n - x.c
        from (select req, count(*) c from dc.props where tenant = p_tenant and author = tgt and status = 'open' and doc->>'author_kind' <> 'human'
              group by req) x
        where h.tenant = p_tenant and h.req = x.req;
      update dc.props set status = 'invalidated', doc = doc || '{"status": "invalidated", "closed_by": null}'::jsonb
        where tenant = p_tenant and author = tgt and status = 'open';
    elsif k = 'grant' then
      insert into dc.role values (p_tenant, op->>1, op->>3, op->>2) on conflict do nothing;
    else
      raise exception 'unknown delta op %', k;
    end if;
  end loop;
end $$;

-- ------------------------------------------------------------------------------------------------ S1: the lock order and its budget
-- FINAL-JUDGMENT.md 4.2. Advisory keys (two-int4 key space). Review of 2262eaa F1: A and T, and their fences F_A, F_T, use the EXACT
-- lockid of the principal or tenant row (dc._ak, dc._tk): A(principal) (1145241601, lockid), T(tenant) (1145241602, lockid), fences
-- 1145241605 and 1145241606 on the same lockid. S(tenant, subject) is (-tenant lockid, hash of the subject): a class of its own per
-- tenant, so a subject of one tenant never shares a lock with another tenant's (within a tenant two subjects share one with chance
-- 2^-32, a wait, never an eviction). N(factor, nonce) 1145241604 keeps the 32-bit hash (the nonce is the prover's random choice; a
-- collision is a wait for one open_session). The per-principal idempotency lock keeps its int8 key (L4; 64-bit hash of names that only
-- the owner chooses; a collision is a wait). dc._k is the hash of S and N.
create function dc._k(kind text, a text, b text) returns int language sql immutable as $$
  select hashtext(jsonb_build_array(kind, a, b)::text)
$$;

-- the lockid of a principal, or NULL when the tenant has no such principal (then there is nothing to lock; the pins refuse the call)
create function dc._ak(p_tenant text, p_pid text) returns int language sql stable as $$
  select p.lockid from dc.principal p where p.tenant = p_tenant and p.pid = p_pid
$$;

-- the lockid of a tenant; an uninstalled tenant raises (a NULL key would make the STRICT advisory functions lock nothing)
create function dc._tk(p_tenant text) returns int language plpgsql stable as $$
#variable_conflict use_variable
declare k int;
begin
  select t.lockid into k from dc.tenant t where t.tenant = p_tenant;
  if k is null then raise exception 'tenant not installed' using errcode = 'P0001'; end if;
  return k;
end $$;

-- Review of 2262eaa F2a: a PREPARED transaction keeps its locks with no backend, so eviction cannot end it (ROLLBACK PREPARED cannot run
-- inside a transaction), and a dc_app transaction that made a call and was prepared would make its principal unrevocable. Fail closed:
-- install refuses, and every API entry refuses, while max_prepared_transactions > 0 (a server setting that changes only at restart, so
-- no call can run under 0 and its transaction be prepared later). Disclosed: the API is unusable on a cluster that enables 2PC.
create function dc._no2pc() returns void language plpgsql as $$
begin
  if current_setting('max_prepared_transactions')::int > 0 then
    raise exception 'refused: max_prepared_transactions > 0 (a prepared transaction could hold authority locks no revocation can end)'
      using errcode = '55000';
  end if;
end $$;

-- the cap on an exclusive wait (A or T): past it the call fails retryably (55P03) and the queue behind it moves on
create function dc._xwait_ms() returns bigint language sql immutable as $$ select 2000::bigint $$;

-- the caller's lock_timeout in ms (0: none), read by each API function before its inner function's SET clause replaces it
create function dc._lt() returns bigint language sql stable as $$
  select (extract(epoch from current_setting('lock_timeout')::interval) * 1000)::bigint
$$;

-- the run-time order check: each lock request records (level, key) in the transaction-local setting dc.trace, and a request below
-- the last one raises DCORD. Reset at the start of every call (a transaction that makes several calls is outside the proof, 4.5).
create function dc._at_reset() returns void language plpgsql as $$
begin
  perform set_config('dc.at', '', true);
  perform set_config('dc.trace', '', true);
end $$;

create function dc._at(p_level bigint, p_key bigint) returns void language plpgsql as $$
#variable_conflict use_variable
declare held text := coalesce(current_setting('dc.at', true), '');
begin
  if p_level is null or p_key is null then raise exception 'lock order: null level or key' using errcode = 'DCORD'; end if;
  if held <> '' and (p_level < split_part(held, ':', 1)::bigint
                     or (p_level = split_part(held, ':', 1)::bigint and p_key < split_part(held, ':', 2)::bigint)) then
    raise exception 'lock order: %:% requested after %', p_level, p_key, held using errcode = 'DCORD';
  end if;
  perform set_config('dc.at', p_level || ':' || p_key, true);
  perform set_config('dc.trace', coalesce(current_setting('dc.trace', true), '') || p_level || ':' || p_key || ',', true);
end $$;

-- before every acquisition statement: lock_timeout = the smallest of the caller's own (when set), the time left until p_deadline (at
-- least 1 ms) and p_cap. It never refuses: a timeout after the deadline is the call's refusal, one before it is re-raised. NULL p_lt:
-- the owner-only raw path, which keeps the owner's own lock_timeout.
create function dc._budget(p_deadline timestamptz, p_lt bigint, p_cap bigint default null) returns void language plpgsql as $$
#variable_conflict use_variable
declare ms bigint;
begin
  if p_lt is null then return; end if;
  if p_deadline is not null then
    ms := least(greatest(1, ceil(extract(epoch from p_deadline - clock_timestamp()) * 1000)), 2147483647);
  end if;
  if p_lt > 0 then ms := least(coalesce(ms, p_lt), p_lt); end if;
  if p_cap is not null then ms := least(coalesce(ms, p_cap), p_cap); end if;
  perform set_config('lock_timeout', coalesce(ms, 0)::text, true);
end $$;

-- ------------------------------------------------------------------------------------------------ 4.4: stalled holders are evicted
-- FINAL-JUDGMENT.md 4.4. ASSUMPTION (the policy owner's choice, not the user's): a backend that holds (or waits for) a principal's
-- authority lock A or a tenant lock T past XWAIT, while a revocation, end_session, factor revocation, grant or erasure needs that lock
-- exclusively, is ended (pg_terminate_backend), so a revocation applies in bounded time even against a holder that resets its idle
-- timeout, runs a long statement or never goes idle. Eviction comes only after the revoking call's own authority is pinned and judged
-- (clock read last), and only through dc._evict, owned by the NOLOGIN role dc_evictor (the only role granted pg_signal_backend;
-- EXECUTE for dc_owner only). Before a revocation evicts for a lock, it takes the FENCE of that lock (F_A 1145241605, F_T 1145241606,
-- the same lockid); a call about to take that lock SHARED refuses (55P03, retryable, nothing done), so each round can only meet holders
-- that passed the fence check before it was taken (review of 2262eaa F3: the fence now comes BEFORE the judgment and the eviction on
-- every path). The fence is read from pg_locks by calls, never waited for, so it adds no step to the lock order of 4.2.
--
-- Whom (review of 2262eaa F2b, H1): every backend of THIS database, other than the caller, whose session user is neither a superuser
-- nor a member of dc_owner (a login role that is a MEMBER of dc_app or dc_idp is evicted like dc_app itself; roles are not listed by
-- name), and that holds the lock or a fence in a way that can stall: a SHARED holder or shared waiter, or an EXCLUSIVE holder that is
-- not itself inside the lock protocol. "Inside" means it waits right now for an A, T or fence lock, or for a backend it is evicting
-- to exit: that is a revocation in progress (bounded by its own XWAIT rounds and expiry), and ending it would cancel it (H1). An
-- exclusive holder that is NOT waiting (a revocation that applied and whose transaction stays open, holding A, T or a fence) is a
-- stalled holder and is evicted. An exclusive WAITER is a queued revocation and is left alone. Owner and superuser backends are never
-- evicted; a round that finds nothing to evict gives up (55P03).

create function dc._fenced(p_fcls int, p_key int) returns void language plpgsql as $$
begin
  if exists (select 1 from pg_catalog.pg_locks l
              where l.locktype = 'advisory' and l.objsubid = 2 and l.granted and l.pid <> pg_backend_pid()
                and l.database = (select d.oid from pg_catalog.pg_database d where d.datname = current_database())
                and l.classid = p_fcls::bigint::oid and l.objid = (p_key::bigint & 4294967295)::oid) then
    raise exception 'a revocation of this authority is in progress' using errcode = 'lock_not_available';
  end if;
end $$;

-- SECURITY DEFINER as dc_evictor (install sets the owner): end the backends described above for the advisory lock (p_cls, p_key);
-- p_cls must be A, T, F_A or F_T. Waits up to 1 s for each to exit. Returns how many were signalled.
create function dc._evict(p_cls int, p_key int) returns int language plpgsql security definer set search_path = pg_catalog, pg_temp as $$
#variable_conflict use_variable
declare n int := 0; r record; db oid := (select d.oid from pg_catalog.pg_database d where d.datname = current_database());
begin
  if p_cls is null or p_key is null or p_cls not in (1145241601, 1145241602, 1145241605, 1145241606) then
    raise exception 'evict: lock class % is not evictable', p_cls;
  end if;
  for r in select distinct l.pid from pg_catalog.pg_locks l
              join pg_catalog.pg_stat_activity a on a.pid = l.pid
              join pg_catalog.pg_roles ro on ro.oid = a.usesysid
            where l.locktype = 'advisory' and l.objsubid = 2 and l.pid <> pg_backend_pid() and l.database = db
              and a.datname = current_database()
              and not ro.rolsuper and not pg_catalog.pg_has_role(a.usesysid, 'dc_owner', 'MEMBER')
              and l.classid = p_cls::bigint::oid and l.objid = (p_key::bigint & 4294967295)::oid
              and (l.mode = 'ShareLock'
                   or (l.granted and a.wait_event is distinct from 'BackendTermination'
                       and not exists (select 1 from pg_catalog.pg_locks w
                                        where w.pid = l.pid and not w.granted and w.locktype = 'advisory' and w.objsubid = 2
                                          and w.database = db
                                          and w.classid in (1145241601::oid, 1145241602::oid, 1145241605::oid, 1145241606::oid))))
            order by l.pid loop
    perform pg_terminate_backend(r.pid, 1000);
    n := n + 1;
  end loop;
  return n;
end $$;

-- The fence of (p_fcls, p_key), waited for up to XWAIT (another evictor of the same lock holds it while it works). TRUE when held; FALSE
-- when another backend still holds it. Scoped to the transaction: a refusal or a 55P03 inside an API call's exception block releases it.
create function dc._try_fence(p_fcls int, p_key int, p_deadline timestamptz, p_lt bigint) returns boolean language plpgsql as $$
begin
  perform dc._budget(p_deadline, p_lt, dc._xwait_ms());
  begin
    perform pg_advisory_xact_lock(p_fcls, p_key);
    return true;
  exception when lock_not_available then
    return false;
  end;
end $$;

-- After an exclusive request timed out (the caller is already pinned and judged): give up (55P03) at or after the deadline, when the
-- caller's own lock_timeout (not XWAIT) ran out, or after the third round. Else take the fence, read the clock again, and evict the
-- lock's stalled holders. When another backend holds the fence, evict that one instead if it is a stalled holder (a revocation that
-- applied and stayed open), and try again next round. A round that signals nobody gives up.
create function dc._evict_round(p_cls int, p_key int, p_fcls int, p_deadline timestamptz, p_lt bigint, p_round int) returns void
language plpgsql as $$
#variable_conflict use_variable
declare fenced boolean;
begin
  if (p_deadline is not null and clock_timestamp() >= p_deadline) or (p_lt > 0 and p_lt <= dc._xwait_ms()) or p_round >= 3 then
    raise exception 'exclusive lock still held' using errcode = 'lock_not_available';
  end if;
  fenced := dc._try_fence(p_fcls, p_key, p_deadline, p_lt);
  if p_deadline is not null and clock_timestamp() >= p_deadline then
    raise exception 'exclusive lock still held' using errcode = 'lock_not_available';
  end if;
  if dc._evict(case when fenced then p_cls else p_fcls end, p_key) = 0 then
    raise exception 'exclusive lock held by a backend that is never evicted' using errcode = 'lock_not_available';
  end if;
end $$;

-- An EXCLUSIVE A or T for a call whose own authority is already settled: dc_idp's end_session, the owner's revoke_factor, and the
-- tenant lock of an admin's revoke, grant or erasure (taken after the caller's pins and judgment). At most three rounds of: wait up
-- to XWAIT, then dc._evict_round.
create function dc._xlock(p_cls int, p_key int, p_fcls int, p_deadline timestamptz, p_lt bigint) returns void language plpgsql as $$
#variable_conflict use_variable
declare i int;
begin
  for i in 1 .. 3 loop
    perform dc._budget(p_deadline, p_lt, dc._xwait_ms());
    begin
      perform pg_advisory_xact_lock(p_cls, p_key);
      return;
    exception when lock_not_available then
      null;
    end;
    perform dc._evict_round(p_cls, p_key, p_fcls, p_deadline, p_lt, i);
  end loop;
end $$;

-- The A EXCLUSIVE of dc.submit's acquisition (a revoke's target, or a self-revoke), which comes BEFORE the caller's pins (L1). After
-- a timeout: the fence of the target first (F3), then the caller is judged in a sub-block: its own authority lock shared (fence checked,
-- XWAIT cap), its pins (L2.0-2.2), its pinned admin flag, the clock last; only then the eviction. The sub-block is rolled back (DCEVI)
-- so the call's lock sequence stays the declared one; the judgment's verdict and the eviction count leave it in the error detail. A
-- refusal returns into dc._submit's exception block, which releases the fence with everything else the call took. Returns the refusal,
-- or NULL when the lock is held.
create function dc._xlock_caller(s dc.sessions, p_key int, p_lt bigint, p_adm boolean) returns text language plpgsql as $$
#variable_conflict use_variable
declare i int; why text; n int; d text; ck int := dc._ak(s.tenant, s.principal); fenced boolean;
begin
  for i in 1 .. 3 loop
    perform dc._budget(s.expires_at, p_lt, dc._xwait_ms());
    begin
      perform pg_advisory_xact_lock(1145241601, p_key);
      return null;
    exception when lock_not_available then
      null;
    end;
    if clock_timestamp() >= s.expires_at or (p_lt > 0 and p_lt <= dc._xwait_ms()) or i >= 3 then
      raise exception 'authority lock still held' using errcode = 'lock_not_available';
    end if;
    fenced := dc._try_fence(1145241605, p_key, s.expires_at, p_lt);
    begin
      why := null;
      n := 0;
      if ck is null then
        why := 'principal_revoked';
      else
        perform dc._fenced(1145241605, ck);
        perform dc._budget(s.expires_at, p_lt, dc._xwait_ms());
        perform pg_advisory_xact_lock_shared(1145241601, ck);
        why := dc._pin(s, p_lt, false);
        if why is null and (select p.admin from dc.principal p where p.tenant = s.tenant and p.pid = s.principal) is distinct from p_adm then
          why := 'unauthorized';
        end if;
      end if;
      if why is null and clock_timestamp() >= s.expires_at then why := 'unauthenticated:invalid_session'; end if;
      if why is null then n := dc._evict(case when fenced then 1145241601 else 1145241605 end, p_key); end if;
      raise exception 'judged' using errcode = 'DCEVI', detail = coalesce(why, '') || '|' || n;
    exception when sqlstate 'DCEVI' then
      get stacked diagnostics d = pg_exception_detail;
    end;
    why := nullif(split_part(d, '|', 1), '');
    if why is not null then return why; end if;
    if split_part(d, '|', 2)::int = 0 then
      raise exception 'authority lock held by a backend that is never evicted' using errcode = 'lock_not_available';
    end if;
  end loop;
  raise exception 'authority lock still held' using errcode = 'lock_not_available';
end $$;

-- L0: one relation lock per statement, in the global table order (key: the position in it), in the mode the entry point needs
create function dc._rel(p_deadline timestamptz, p_lt bigint, p_tables text[], p_modes text[]) returns void language plpgsql as $$
#variable_conflict use_variable
declare i int; k int;
        rels text[] := array['audit','chain','chainhead','events','factors','head','principal','props','role','sessions','subject_tokens',
                             'tenant','used_proofs','vals'];
begin
  for i in 1 .. coalesce(array_length(p_tables, 1), 0) loop
    k := array_position(rels, p_tables[i]) - 1;
    if k is null or p_modes[i] not in ('access share', 'row share', 'row exclusive') then
      raise exception 'lock order: relation % in mode %', p_tables[i], p_modes[i] using errcode = 'DCORD';
    end if;
    perform dc._budget(p_deadline, p_lt);
    perform dc._at(0, k);
    execute format('lock table dc.%I in %s mode', p_tables[i], p_modes[i]);
  end loop;
end $$;

-- L2.3-2.4: a revoke's target principal row and its session rows, one per statement, so its authority writes after the final judgment
-- never wait. Every API writer of these rows holds A(target), which the revoke holds exclusively.
create function dc._lock_target(p_tenant text, tgt text, p_deadline timestamptz, p_lt bigint) returns void language plpgsql as $$
#variable_conflict use_variable
declare r record;
begin
  perform dc._budget(p_deadline, p_lt);
  perform dc._at(2, 3);
  perform 1 from dc.principal p where p.tenant = p_tenant and p.pid = tgt for no key update;
  for r in select x.token_hash from dc.sessions x where x.tenant = p_tenant and x.principal = tgt order by x.token_hash collate "C" loop
    perform dc._budget(p_deadline, p_lt);
    perform dc._at(2, 4);
    perform 1 from dc.sessions x where x.token_hash = r.token_hash for no key update;
  end loop;
end $$;

-- L3-L5, shared by dc.submit's acquisition and the owner-only raw path (dc._lock_event): the tenant lock T (exclusive for an authority
-- change, p_xt), the per-principal idempotency lock, the event's head row; for a revoke, its target's head rows and open props rows.
-- The B3 controls patch the idempotency-lock and head-row lines.
create function dc._lock_tail(p_tenant text, ev jsonb, p_xt boolean, p_deadline timestamptz, p_lt bigint) returns void language plpgsql as $$
#variable_conflict use_variable
declare typ text := case when jsonb_typeof(ev) = 'object' then ev->>'type' end; r record;
begin
  perform dc._budget(p_deadline, p_lt, case when p_xt then dc._xwait_ms() end);
  perform dc._at(3, 0);
  if p_xt and p_lt is not null then          -- an admin's revoke or grant, after its pins and judgment: evicts a stalled holder (4.4)
    perform dc._xlock(1145241602, dc._tk(p_tenant), 1145241606, p_deadline, p_lt);
  elsif p_xt then perform pg_advisory_xact_lock(1145241602, dc._tk(p_tenant));
  else
    if p_lt is not null then perform dc._fenced(1145241606, dc._tk(p_tenant)); end if;
    perform pg_advisory_xact_lock_shared(1145241602, dc._tk(p_tenant));
  end if;
  if jsonb_typeof(ev) = 'object' and jsonb_typeof(ev->'principal') = 'string' then
    perform dc._budget(p_deadline, p_lt);
    perform dc._at(4, 0);
    perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array(p_tenant, ev->>'principal')::text, 0));
  end if;
  if jsonb_typeof(ev) = 'object' and typ is distinct from 'revoke' and jsonb_typeof(ev->'req') = 'string' then
    perform dc._budget(p_deadline, p_lt);
    perform dc._at(5, 0);
    perform 1 from dc.head where tenant = p_tenant and req = ev->>'req' for update;
  end if;
  if typ = 'revoke' and jsonb_typeof(ev->'target') = 'string' then
    for r in select distinct x.req from dc.props x where x.tenant = p_tenant and x.author = ev->>'target' and x.status = 'open'
              order by x.req loop
      perform dc._budget(p_deadline, p_lt);
      perform dc._at(5, 0);
      perform 1 from dc.head h where h.tenant = p_tenant and h.req = r.req for update;
    end loop;
    for r in select x.req, x.id from dc.props x where x.tenant = p_tenant and x.author = ev->>'target' and x.status = 'open'
              order by x.req, x.id loop
      perform dc._budget(p_deadline, p_lt);
      perform dc._at(5, 1);
      perform 1 from dc.props x where x.tenant = p_tenant and x.req = r.req and x.id = r.id for update;
    end loop;
  end if;
end $$;

-- OWNER-ONLY raw path (PgEngine, B3, the differential), called before dc._reduce_apply: the S1 order from L1 on, without a budget (the
-- owner's own lock_timeout), without L0 and without the chain-head row, which the event insert takes as before (so the B3 controls
-- still show what a missing lock does).
create function dc._lock_event(p_tenant text, ev jsonb) returns void language plpgsql as $$
#variable_conflict use_variable
declare typ text := case when jsonb_typeof(ev) = 'object' then ev->>'type' end;
        tgt text := case when jsonb_typeof(ev) = 'object' and jsonb_typeof(ev->'target') = 'string' then ev->>'target' end;
begin
  perform dc._at_reset();
  if not exists (select 1 from dc.tenant t where t.tenant = p_tenant) then raise exception 'tenant not installed'; end if;
  if typ = 'revoke' and dc._ak(p_tenant, tgt) is not null then      -- F1: a target with no row has no lock (the rules refuse it)
    perform dc._at(1, dc._ak(p_tenant, tgt));
    perform pg_advisory_xact_lock(1145241601, dc._ak(p_tenant, tgt));
    perform dc._lock_target(p_tenant, tgt, null, null);
  end if;
  perform dc._lock_tail(p_tenant, ev, coalesce(typ in ('revoke', 'grant'), false), null, null);
end $$;

-- reduce(view, ev) + apply everything but a revoke's authority writes: {code, original, target}; target is the revoked principal when a
-- revoke was applied (the caller then runs its final judgment and dc._apply_authority). Locks must already be held (dc._acquire_submit
-- or dc._lock_event).
create function dc._reduce_stage(p_tenant text, ev jsonb) returns jsonb language plpgsql as $$
#variable_conflict use_variable
declare r record; fp text; ofp text; ocode text; code text; orig text;
        refused text[] := array['bad_event','tenant_mismatch','unauthorized','principal_revoked','unknown_req','duplicate_id','proposal_cap',
          'channel_mismatch','agent_not_permitted','agent_cannot_confirm','unknown_pending','pending_closed','same_channel_confirmation',
          'unknown_proposal','proposal_mismatch','stale','proposal_closed','candidates_full','contested_needs_resolve','not_contested',
          'nothing_to_retract','unknown_candidate','idem_conflict','duplicate'];
begin
  if not dc._shape(ev) then code := 'bad_event';
  elsif ev->>'tenant' <> p_tenant then code := 'tenant_mismatch';
  else
    fp := encode(sha256(convert_to(ev::text, 'UTF8')), 'hex');
    select e.fp, e.code into ofp, ocode from dc.events e where e.tenant = p_tenant and e.principal = ev->>'principal' and e.idem = ev->>'idem';
    if ocode is not null then
      code := case when ofp = fp then 'duplicate' else 'idem_conflict' end; orig := ocode;
    else
      select * into r from dc._rules(p_tenant, ev);
      code := r.code;
    end if;
  end if;
  if code = any(refused) then
    insert into dc.audit(tenant, principal, code, original, ev)
      values (p_tenant, case when jsonb_typeof(ev) = 'object' and jsonb_typeof(ev->'principal') = 'string' then ev->>'principal' end, code, orig,
              case when jsonb_typeof(ev) = 'object' then ev else jsonb_build_object('malformed', true) end);
    return jsonb_build_object('code', code, 'original', orig);
  end if;
  perform dc._apply_delta(p_tenant, r.delta);
  insert into dc.events(tenant, principal, idem, req, evid, fp, code, ev)
    values (p_tenant, ev->>'principal', ev->>'idem', case when ev->>'type' <> 'revoke' then ev->>'req' end, ev->>'id', fp, code, ev);
  return jsonb_build_object('code', code, 'original', null,
                            'target', (select op->>1 from jsonb_array_elements(r.delta) op where op->>0 = 'revoke' limit 1));
end $$;

-- after the final judgment: a revoke's authority writes, on the rows dc._lock_target locked, so they never wait. Every session of the
-- target is ended: no new one can be opened meanwhile (dc.open_session takes A(target) shared, the revoke holds it exclusively).
create function dc._apply_authority(p_tenant text, tgt text) returns void language plpgsql as $$
#variable_conflict use_variable
begin
  update dc.principal set revoked = true where tenant = p_tenant and pid = tgt;
  update dc.sessions x set revoked = true where x.tenant = p_tenant and x.principal = tgt and not x.revoked;
end $$;

-- reduce + apply, authority writes included: {code, original}. For the owner-only raw callers (PgEngine, B3, the differential), which
-- judge no session. Locks must already be held (dc._lock_event).
create function dc._reduce_apply(p_tenant text, ev jsonb) returns jsonb language plpgsql as $$
#variable_conflict use_variable
declare r jsonb;
begin
  r := dc._reduce_stage(p_tenant, ev);
  if r->>'target' is not null then perform dc._apply_authority(p_tenant, r->>'target'); end if;
  return r - 'target';
end $$;

-- OWNER-ONLY raw entry for tests and experiments (differential, B3): locks, then reduce/apply. Executable by no login role.
create function dc._apply_event(p_tenant text, ev jsonb) returns jsonb language plpgsql as $$
#variable_conflict use_variable
begin
  perform dc._lock_event(p_tenant, ev);
  return dc._reduce_apply(p_tenant, ev);
end $$;

-- ------------------------------------------------------------------------------------------------ sessions (identity adapter only)
-- (actor, trust_origin) from the session's principal kind and channel: pol._core.origin is the same table (ASSUMPTION: vocabulary from
-- COMPOSITION.md items 1 and 3; only a human on an owner channel is user_typed)
create function dc._origin(p_kind text, p_channel text) returns jsonb language sql immutable as $$
  select case when p_kind = 'human' and p_channel in ('owner_ui', 'owner_cli') then jsonb_build_object('actor', 'user', 'trust_origin', 'user_typed')
              else jsonb_build_object('actor', 'assistant', 'trust_origin', 'agent_generated') end
$$;

-- admin path (owner or superuser only; granted to no login role): enrol a factor for a principal on the channel its kind allows. The key
-- is returned ONCE to the enroller (the device keeps it) and stays in dc.factors. LIMIT: a symmetric MAC, so the database owner can
-- forge proofs; pgcrypto offers no signature verification.
create function dc.enroll_factor(p_tenant text, p_principal text, p_channel text) returns jsonb
language plpgsql security definer set search_path = dc, pg_temp as $$
#variable_conflict use_variable
declare k text; f text; key bytea;
begin
  select p.kind into k from dc.principal p where p.tenant = p_tenant and p.pid = p_principal and not p.revoked;
  if k is null then raise exception 'unknown or revoked principal' using errcode = 'P0001'; end if;
  if (k = 'human' and p_channel not in ('owner_ui', 'owner_cli')) or (k <> 'human' and p_channel is distinct from 'agent_api') then
    raise exception 'channel does not match the principal kind' using errcode = 'P0001';
  end if;
  f := 'fac-' || encode(dc_crypto.gen_random_bytes(12), 'hex');
  key := dc_crypto.gen_random_bytes(32);
  insert into dc.factors(factor, tenant, principal, channel, key) values (f, p_tenant, p_principal, p_channel, key);
  return jsonb_build_object('factor', f, 'key', encode(key, 'hex'));
end $$;

-- the judgment of a well-formed proof (body, mac, parsed body b): the factor row, or a P0001 'proof refused: <reason>'. dc.open_session runs
-- it with p_lt (its acquisition: the factor row L2.1 and the principal row L2.2 are locked FOR SHARE, each wait capped at the proof's
-- expiry, and judged as locked), then again after the ledger insert without p_lt, on the rows it already holds (final review P4).
-- VOLATILE: each statement reads a fresh snapshot.
create function dc._judge_proof(body text, mac text, b jsonb, p_lt bigint default null) returns dc.factors language plpgsql volatile as $$
#variable_conflict use_variable
declare fr dc.factors; k text; rv bool; x numeric;
        dl timestamptz := to_timestamp(least(greatest((b->>'expires')::numeric, 0), extract(epoch from clock_timestamp()) + 301));
begin
  if p_lt is not null then perform dc._budget(dl, p_lt); perform dc._at(2, 1); end if;
  select f.* into fr from dc.factors f where f.factor = b->>'factor' for share;
  -- compare digests of the two MACs, so the comparison time says nothing about how much of a guessed MAC was right
  if fr.factor is null or dc_crypto.digest(mac, 'sha256')
       <> dc_crypto.digest(encode(dc_crypto.hmac(convert_to(body, 'UTF8'), fr.key, 'sha256'), 'hex'), 'sha256') then
    raise exception 'proof refused: signature' using errcode = 'P0001';
  end if;
  if fr.revoked then raise exception 'proof refused: factor revoked' using errcode = 'P0001'; end if;
  if b->>'tenant' <> fr.tenant or b->>'principal' <> fr.principal or b->>'channel' <> fr.channel then
    raise exception 'proof refused: binding' using errcode = 'P0001';
  end if;
  x := (b->>'expires')::numeric;
  -- delta review M1: the wall clock, not now() (fixed when the caller's transaction began)
  if x <= extract(epoch from clock_timestamp()) or x > extract(epoch from clock_timestamp()) + 300 then
    raise exception 'proof refused: time' using errcode = 'P0001';
  end if;
  if p_lt is not null then perform dc._budget(dl, p_lt); perform dc._at(2, 2); end if;
  select p.kind, p.revoked into k, rv from dc.principal p where p.tenant = fr.tenant and p.pid = fr.principal for share;
  if k is null or rv then raise exception 'proof refused: principal' using errcode = 'P0001'; end if;
  if (k = 'human' and fr.channel not in ('owner_ui', 'owner_cli')) or (k <> 'human' and fr.channel <> 'agent_api') then
    raise exception 'proof refused: channel' using errcode = 'P0001';
  end if;
  return fr;
end $$;

-- the identity adapter (dc_idp): a session ONLY from a proof. Tenant, principal and channel come from the factor row; the proof must name
-- the same ones, be unexpired and at most 300 s ahead, carry a nonce not used before with this factor, and the principal must be
-- unrevoked with a channel its kind allows (dc._judge_proof). Refusals raise P0001 'proof refused: <reason>' and write nothing.
create function dc.open_session(p_proof text, p_ttl_s integer) returns jsonb
language plpgsql security definer set search_path = dc, pg_temp as $$
begin
  perform dc._no2pc();                      -- review of 2262eaa F2a
  return dc._open_session(p_proof, p_ttl_s, dc._lt());
end $$;

-- S1 (FINAL-JUDGMENT.md 4.2): L0, L1 A(factor's principal) shared, L2.1-2.2 inside dc._judge_proof, L8 N(factor, nonce); every wait
-- capped at the proof's expiry (a timeout after it is 'proof refused: time'). Then, under 1 ms: the prune (rows another presentation
-- holds are skipped), the ledger, the session and the second judgment, on the rows already locked.
create function dc._open_session(p_proof text, p_ttl_s integer, p_lt bigint) returns jsonb language plpgsql set lock_timeout = '0' as $$
#variable_conflict use_variable
declare body text; mac text; b jsonb; fr dc.factors; x numeric; tok text; n int; dl timestamptz; ft text; fp text; nk int;
begin
  if p_proof is null or length(p_proof) > 2048 or p_ttl_s is null or p_ttl_s < 1 or p_ttl_s > 86400 then
    raise exception 'proof refused: shape' using errcode = 'P0001';
  end if;
  -- delta review M1: a REPEATABLE READ or SERIALIZABLE snapshot could predate a factor or principal revoke; refuse it (see dc._session)
  if current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'proof refused: isolation' using errcode = 'P0001';
  end if;
  body := substring(p_proof from '^(.*)[.][0-9a-f]{64}$');
  mac := substring(p_proof from '[.]([0-9a-f]{64})$');
  if body is null or mac is null then raise exception 'proof refused: shape' using errcode = 'P0001'; end if;
  begin
    b := body::jsonb;
  exception when others then
    raise exception 'proof refused: shape' using errcode = 'P0001';
  end;
  if jsonb_typeof(b) is distinct from 'object'
     or (select array_agg(j order by j collate "C") from jsonb_object_keys(b) j)
        is distinct from array['channel', 'expires', 'factor', 'nonce', 'principal', 'tenant']
     or jsonb_typeof(b->'expires') <> 'number'
     or exists (select 1 from unnest(array['channel', 'factor', 'nonce', 'principal', 'tenant']) j where jsonb_typeof(b->j) <> 'string')
     or (b->>'nonce') !~ '^[0-9a-f]{32}$' then
    raise exception 'proof refused: shape' using errcode = 'P0001';
  end if;
  x := (b->>'expires')::numeric;
  -- the deadline of every wait below: the proof's expiry (one more than 300 s ahead is refused by the time check anyway)
  dl := to_timestamp(least(greatest(x, 0), extract(epoch from clock_timestamp()) + 301));
  perform dc._at_reset();
  begin
    perform dc._budget(dl, p_lt);
    select f.tenant, f.principal into ft, fp from dc.factors f where f.factor = b->>'factor';   -- unlocked: whose authority to lock
    perform dc._rel(dl, p_lt, array['factors', 'principal', 'sessions', 'used_proofs'],
                    array['row share', 'row share', 'row exclusive', 'row exclusive']);
    if ft is not null then
      perform dc._budget(dl, p_lt);
      perform dc._at(1, dc._ak(ft, fp));
      perform dc._fenced(1145241605, dc._ak(ft, fp));
      perform pg_advisory_xact_lock_shared(1145241601, dc._ak(ft, fp));
    end if;
    fr := dc._judge_proof(body, mac, b, p_lt);
    nk := dc._k('nonce', fr.factor, b->>'nonce');
    perform dc._budget(dl, p_lt);
    perform dc._at(8, nk);
    perform pg_advisory_xact_lock(1145241604, nk);                 -- a concurrent twin of this proof: wait for its outcome
  exception when lock_not_available then
    if clock_timestamp() < dl then raise; end if;
    raise exception 'proof refused: time' using errcode = 'P0001';
  end;
  perform set_config('lock_timeout', '1ms', true);
  delete from dc.used_proofs u where (u.factor, u.nonce) in (
    select v.factor, v.nonce from dc.used_proofs v where v.factor = fr.factor and v.expires < clock_timestamp() - interval '1 minute'
       for update skip locked);                                        -- refused anyway; rows another presentation holds are skipped
  insert into dc.used_proofs values (fr.factor, b->>'nonce', to_timestamp(x)) on conflict do nothing;      -- a twin committed it first
  get diagnostics n = row_count;
  if n = 0 then raise exception 'proof refused: replayed' using errcode = 'P0001'; end if;
  tok := encode(dc_crypto.gen_random_bytes(32), 'hex');
  insert into dc.sessions values (encode(sha256(convert_to(tok, 'UTF8')), 'hex'), fr.tenant, fr.principal, fr.channel, fr.factor,
                                  clock_timestamp() + make_interval(secs => p_ttl_s), false);
  -- final review P4: the whole proof is judged again after the writes (signature, factor revoked, binding, time, principal, channel),
  -- on the rows locked above, under 1 ms. A refusal raises, so the statement writes nothing.
  fr := dc._judge_proof(body, mac, b);
  return jsonb_build_object('token', tok, 'tenant', fr.tenant, 'principal', fr.principal, 'channel', fr.channel, 'factor', fr.factor);
end $$;

-- the identity adapter ends a session (logout, or a failed in-process issue): the token is refused from then on. Only under READ COMMITTED
-- (final review P5): under REPEATABLE READ or SERIALIZABLE the update reads the transaction's snapshot, so a session opened after it
-- would be "not found" (false) and stay live; READ UNCOMMITTED is refused by the same exact comparison.
create function dc.end_session(p_token text) returns boolean language plpgsql security definer set search_path = dc, pg_temp as $$
begin
  perform dc._no2pc();                      -- review of 2262eaa F2a
  if current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'end_session refused: isolation' using errcode = 'P0001';
  end if;
  return dc._end_session(p_token, dc._lt());
end $$;

-- S1: L0 dc.sessions; L1 A(the session's principal) EXCLUSIVE, so it waits FIFO for that principal's in-flight calls (which pin the
-- session row) and new ones queue behind it; capped at XWAIT (55P03, retryable). Then the update, which no API call holds.
create function dc._end_session(p_token text, p_lt bigint) returns boolean language plpgsql set lock_timeout = '0' as $$
#variable_conflict use_variable
declare s dc.sessions; n int; k int;
begin
  perform dc._at_reset();
  -- review of 2262eaa: a 55P03 leaves this block before it reaches the caller, which releases every lock taken here (the fence too),
  -- so a caller that keeps its failed transaction open holds nothing of the protocol
  begin
    perform dc._rel(null, p_lt, array['sessions'], array['row exclusive']);
    s := dc._peek(p_token);
    if s.token_hash is null or s.revoked then return false; end if;
    k := dc._ak(s.tenant, s.principal);
    if k is not null then                         -- no principal row: no call of it can hold anything (the pins refuse)
      perform dc._budget(null, p_lt, dc._xwait_ms());
      perform dc._at(1, k);
      perform dc._xlock(1145241601, k, 1145241605, null, p_lt);
    end if;
    perform dc._budget(null, p_lt, dc._xwait_ms());
    perform dc._at(2, 4);
    update dc.sessions x set revoked = true where x.token_hash = s.token_hash and not x.revoked;
    get diagnostics n = row_count;
  exception when lock_not_available then
    raise;
  end;
  return n = 1;
end $$;

-- OWNER-ONLY (granted to no login role): revoke a factor through the authority lock, so the revocation waits FIFO for the in-flight
-- calls of the factor's principal (XWAIT per attempt) instead of only for the calls that pinned this factor's row. Direct owner SQL
-- on dc.factors also waits for those pins, but is not queued.
create function dc.revoke_factor(p_factor text) returns boolean language plpgsql security definer set search_path = dc, pg_temp as $$
begin
  return dc._revoke_factor(p_factor, dc._lt());
end $$;

create function dc._revoke_factor(p_factor text, p_lt bigint) returns boolean language plpgsql set lock_timeout = '0' as $$
#variable_conflict use_variable
declare ft text; fp text; n int;
begin
  perform dc._at_reset();
  perform dc._rel(null, p_lt, array['factors'], array['row exclusive']);
  select f.tenant, f.principal into ft, fp from dc.factors f where f.factor = p_factor;
  if ft is null then return false; end if;
  begin                                           -- as dc._end_session: a 55P03 releases what this block took
    perform dc._budget(null, p_lt, dc._xwait_ms());
    perform dc._at(1, dc._ak(ft, fp));
    perform dc._xlock(1145241601, dc._ak(ft, fp), 1145241605, null, p_lt);
    perform dc._budget(null, p_lt, dc._xwait_ms());
    perform dc._at(2, 1);
    update dc.factors f set revoked = true where f.factor = p_factor and not f.revoked;
    get diagnostics n = row_count;
  exception when lock_not_available then
    raise;
  end;
  return n = 1;
end $$;

-- The live session for a token, or a row of nulls. Delta review M1: expiry is judged on the WALL CLOCK (clock_timestamp()), never on
-- now(), which stays fixed at the start of the caller's transaction (a caller could open a transaction while its token is valid and
-- keep calling after expiry). VOLATILE, so every call reads afresh; and only under READ COMMITTED: in a REPEATABLE READ or
-- SERIALIZABLE transaction the snapshot is fixed when the transaction first reads, so an end_session, a session revoke or a principal
-- revoke committed after that would stay invisible to it. Such a transaction gets no session. Final review P4: a session is live only
-- while its factor is unrevoked (use-time factor revocation), so revoking a factor also ends the sessions it opened.
create function dc._session(p_token text) returns dc.sessions language plpgsql volatile as $$
declare s dc.sessions;
begin
  if current_setting('transaction_isolation') <> 'read committed' then return s; end if;
  select x.* into s from dc.sessions x
   where p_token is not null and p_token <> '' and x.token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex')
     and exists (select 1 from dc.factors f where f.factor = x.factor and not f.revoked)
     and not x.revoked and x.expires_at > clock_timestamp();
  return s;
end $$;

-- S1, before any lock: the session row for a token, unlocked and unjudged, only to learn whose authority to lock and the deadline of
-- the call's waits (expires_at, never updated). NULL outside READ COMMITTED: the guard comes before the first row lock (a FOR SHARE in
-- a REPEATABLE READ transaction on a row changed after its snapshot raises 40001). Its own wait (a relation lock on dc.sessions, which
-- only owner DDL takes) is bounded by the caller's lock_timeout alone: the deadline is not known yet (FINAL-JUDGMENT.md 4.4).
create function dc._peek(p_token text) returns dc.sessions language plpgsql volatile as $$
declare s dc.sessions;
begin
  if current_setting('transaction_isolation') <> 'read committed' then return null; end if;
  select x.* into s from dc.sessions x
   where p_token is not null and p_token <> '' and x.token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
  return s;
end $$;

-- S1, L2.0-2.2: the pins. The caller's session row, its factor row and its principal row, each locked FOR SHARE in a statement of its
-- own (each wait capped at the session's expiry) and judged as locked (PostgreSQL 13.2.1: a locking read that waited locks and returns
-- the newest version). NULL, or 'unauthenticated:invalid_session', 'principal_revoked', 'unauthorized' (p_admin and not an admin).
-- No transaction can change these rows until the call ends. Expiry is not judged here: dc._gate reads the clock last.
create function dc._pin(s dc.sessions, p_lt bigint, p_admin boolean) returns text language plpgsql volatile as $$
#variable_conflict use_variable
declare sv bool; fv bool; pr dc.principal;
begin
  perform dc._budget(s.expires_at, p_lt);
  perform dc._at(2, 0);
  select x.revoked into sv from dc.sessions x where x.token_hash = s.token_hash for share;
  perform dc._budget(s.expires_at, p_lt);
  perform dc._at(2, 1);
  select f.revoked into fv from dc.factors f where f.factor = s.factor for share;
  perform dc._budget(s.expires_at, p_lt);
  perform dc._at(2, 2);
  select p.* into pr from dc.principal p where p.tenant = s.tenant and p.pid = s.principal for share;
  if sv is distinct from false or fv is distinct from false then return 'unauthenticated:invalid_session'; end if;
  if pr.pid is null or pr.revoked then return 'principal_revoked'; end if;
  if p_admin and not pr.admin then return 'unauthorized'; end if;
  return null;
end $$;

-- The FINAL judgment of a session-keyed call (final review P1; DESIGN.md "Final judgment after the last wait"): NULL when the token's
-- session (the one the call began with, s) is still live and its principal unrevoked (and, with p_admin, an admin); else the refusal:
-- 'unauthenticated:invalid_session', 'principal_revoked' or 'unauthorized'. The caller runs it after its stage, under lock_timeout 1 ms:
-- the rows it reads are pinned (dc._pin) and their tables locked (L0), so it never waits and reads exactly the pinned versions; the wall
-- clock is read last, inside dc._session. With the pins removed (the S1_Control broken variant) it is the recheck of the old design.
create function dc._gate(p_token text, s dc.sessions, p_admin bool default false) returns text language plpgsql volatile as $$
#variable_conflict use_variable
declare pr dc.principal; cur dc.sessions;
begin
  select p.* into pr from dc.principal p where p.tenant = s.tenant and p.pid = s.principal;
  cur := dc._session(p_token);
  if s.token_hash is null or cur.token_hash is distinct from s.token_hash then return 'unauthenticated:invalid_session'; end if;
  if pr.pid is null or pr.revoked then return 'principal_revoked'; end if;
  if p_admin and not pr.admin then return 'unauthorized'; end if;
  return null;
end $$;

-- ------------------------------------------------------------------------------------------------ the application API (dc_app)
create function dc._out(code text, orig text, evid text, ref text) returns jsonb language sql immutable as $$
  select jsonb_build_object('code', code, 'original', orig, 'id', evid, 'value_ref', ref)
$$;

-- Everything dc.submit writes, for the session s it began with. Runs after dc._acquire_submit, under lock_timeout 1 ms: every lock it
-- needs is already held, so a wait here fails the call (55P03). Returns dc._out, plus 'target' when a revoke was applied.
create function dc._submit_stage(s dc.sessions, p_body jsonb) returns jsonb language plpgsql as $$
#variable_conflict use_variable
declare ev jsonb; cid text; typ text; lab jsonb; tlab jsonb; vref text; created bool := false; r jsonb; code text;
        body_keys text[] := array['type','client_id','idem','req','value','label','based_on','proposal','proposal_hash','pending','verb',
                                  'choose','target','role'];
        subj text; toks text[] := array[]::text[]; tok text; x jsonb;
begin
  if p_body is null or jsonb_typeof(p_body) <> 'object' then
    insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'bad_event'); return dc._out('bad_event', null, null, null);
  end if;
  if p_body ?| array['tenant', 'principal', 'channel', 'id', 'actor', 'trust_origin'] then
    insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'identity_in_body'); return dc._out('identity_in_body', null, null, null);
  end if;
  cid := case when jsonb_typeof(p_body->'client_id') = 'string' then p_body->>'client_id' end;
  if exists (select 1 from jsonb_object_keys(p_body) k where k <> all(body_keys)) or cid is null or cid !~ '^[A-Za-z0-9._-]{1,64}$' then
    insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'bad_event'); return dc._out('bad_event', null, null, null);
  end if;
  ev := (p_body - 'client_id' - 'value' - 'label')
        || jsonb_build_object('id', s.principal || '/' || cid, 'tenant', s.tenant, 'principal', s.principal, 'channel', s.channel)
        || dc._origin((select p.kind from dc.principal p where p.tenant = s.tenant and p.pid = s.principal), s.channel);
  if p_body ? 'value' then
    if jsonb_typeof(p_body->'value') <> 'string' or not p_body ? 'label' then
      insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'bad_event'); return dc._out('bad_event', null, null, null);
    end if;
    begin
      lab := dc._label_norm(p_body->'label');
    exception when others then
      insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'bad_event'); return dc._out('bad_event', null, null, null);
    end;
    -- subjects must arrive canonical (the caller applies pol.labels.canon_subject): a non-canonical spelling is refused, never stored
    if exists (select 1 from jsonb_array_elements(p_body->'label'->'subjects') e where e #>> '{}' <> dc._canon_subject(e #>> '{}')) then
      insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'bad_subject'); return dc._out('bad_event', null, null, null);
    end if;
    for subj in select jsonb_array_elements_text(lab->'subjects') loop
      insert into dc.subject_tokens values (s.tenant, subj, 'subj-' || replace(gen_random_uuid()::text, '-', '')) on conflict do nothing;
      select t.token into tok from dc.subject_tokens t where t.tenant = s.tenant and t.subject = subj;
      toks := toks || tok;
    end loop;
    tlab := jsonb_set(lab, '{subjects}', coalesce((select jsonb_agg(to_jsonb(t) order by t collate "C") from unnest(toks) t), '[]'::jsonb));
    -- review-3 N5: the reference is RANDOM, never computed from the value, its label or a subject, so a reference left in the log after
    -- erasure cannot be confirmed by guessing a text or a name (there is no key to guess with). A retry by the same principal with the
    -- same idempotency key, value and label, while that value is stored, reuses the logged reference, so the event fingerprint is the
    -- same and the core answers duplicate. The per-principal idempotency lock (L4, dc._lock_tail) serialises such retries; the tenant
    -- lock (L3, shared here, exclusive in dc.erase_values) keeps erasure from interleaving with the minting above.
    select v.ref into vref from dc.events e join dc.vals v on v.tenant = e.tenant and v.ref = e.ev->>'value_ref'
     where e.tenant = s.tenant and e.principal = s.principal and e.idem = p_body->>'idem' and v.text = p_body->>'value' and v.label = lab;
    if vref is null then vref := 'val-' || encode(dc_crypto.gen_random_bytes(20), 'hex'); end if;
    insert into dc.vals values (s.tenant, vref, p_body->>'value', lab, array(select jsonb_array_elements_text(lab->'subjects')))
      on conflict do nothing;
    created := found;
    ev := ev || jsonb_build_object('value_ref', vref, 'label', tlab);
  elsif p_body ? 'label' then
    insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'bad_event'); return dc._out('bad_event', null, null, null);
  end if;
  r := dc._reduce_stage(s.tenant, ev);
  code := r->>'code';
  if code in ('bad_event','tenant_mismatch','unauthorized','principal_revoked','unknown_req','duplicate_id','proposal_cap','channel_mismatch',
              'agent_not_permitted','agent_cannot_confirm','unknown_pending','pending_closed','same_channel_confirmation','unknown_proposal',
              'proposal_mismatch','stale','proposal_closed','candidates_full','contested_needs_resolve','not_contested','nothing_to_retract',
              'unknown_candidate','idem_conflict','duplicate') then
    if vref is not null and created then delete from dc.vals v where v.tenant = s.tenant and v.ref = vref; end if;
    return dc._out(code, r->>'original', ev->>'id', case when code = 'duplicate' and vref is not null and not created then vref end);
  end if;
  return dc._out(code, null, ev->>'id', vref) || case when r->>'target' is null then '{}'::jsonb else jsonb_build_object('target', r->>'target') end;
end $$;

-- S1 acquisition of dc.submit (L0-L7, FINAL-JUDGMENT.md 4.2): NULL, or the pins' refusal. A revoke by an admin takes A(target)
-- exclusive beside A(caller) shared, sorted by key (a self-revoke takes A exclusive only), then its target's rows; a revoke or grant by
-- an admin takes T exclusive. Another principal's revoke or grant is refused by the rules and takes neither, so it cannot stall
-- anyone. The admin flag read before L1 is unlocked; the API never changes it, and if owner SQL did during the call the pinned flag
-- differs and the call is refused.
-- Review of 2262eaa F1/F5: an exclusive request (A of the target, T) is made only when an unlocked pre-read of the rows the rules
-- judge says the rules would apply it: a revoke's target exists in the caller's tenant; a grant's requirement exists and its target
-- exists unrevoked (CORE-RULES; the same checks as dc._rules). Otherwise the call takes the shared locks of any other submit and the
-- rules refuse it as before (same code, same audit row), so a refused revoke or grant waits for nobody and evicts nobody. xa reports
-- whether the exclusive locks were taken; dc._submit fails closed (55P03, nothing applied) if the rules nevertheless applied an
-- authority change without them (the rows changed between the pre-read and the judgment).
create function dc._acquire_submit(s dc.sessions, p_body jsonb, p_lt bigint, out why text, out xa boolean) language plpgsql as $$
#variable_conflict use_variable
declare typ text := case when jsonb_typeof(p_body) = 'object' and jsonb_typeof(p_body->'type') = 'string' then p_body->>'type' end;
        adm bool; tgt text; tk int; r record; sc int;
        wr text := case when typ = 'revoke' then 'row exclusive' else 'row share' end;
begin
  perform dc._rel(s.expires_at, p_lt,
    array['audit', 'chain', 'chainhead', 'events', 'factors', 'head', 'principal', 'props', 'role', 'sessions', 'subject_tokens', 'vals'],
    array['row exclusive', 'row exclusive', 'row exclusive', 'row exclusive', 'row share', 'row exclusive', wr, 'row exclusive',
          'row exclusive', wr, 'row exclusive', 'row exclusive']);
  xa := false;
  select coalesce(p.admin, false) into adm from dc.principal p where p.tenant = s.tenant and p.pid = s.principal;
  adm := coalesce(adm, false);
  if adm and typ = 'revoke' and jsonb_typeof(p_body->'target') = 'string' then
    tk := dc._ak(s.tenant, p_body->>'target');               -- NULL: no such principal in this tenant (the rules: bad_event)
    if tk is not null then tgt := p_body->>'target'; xa := true; end if;
  elsif adm and typ = 'grant' and jsonb_typeof(p_body->'req') = 'string' and jsonb_typeof(p_body->'target') = 'string'
        and jsonb_typeof(p_body->'role') = 'string' then
    xa := exists (select 1 from dc.head x where x.tenant = s.tenant and x.req = p_body->>'req')
          and exists (select 1 from dc.principal p where p.tenant = s.tenant and p.pid = p_body->>'target' and not p.revoked);
  end if;
  for r in select x.k, coalesce(bool_or(x.ex), false) ex
             from (values (dc._ak(s.tenant, s.principal), s.principal = tgt), (tk, tgt is not null)) x(k, ex)
            where x.k is not null group by x.k order by x.k loop
    perform dc._budget(s.expires_at, p_lt, case when r.ex then dc._xwait_ms() end);
    perform dc._at(1, r.k);
    if r.ex then
      why := dc._xlock_caller(s, r.k, p_lt, adm);
      if why is not null then return; end if;
    else
      perform dc._fenced(1145241605, r.k);
      perform pg_advisory_xact_lock_shared(1145241601, r.k);
    end if;
  end loop;
  why := dc._pin(s, p_lt, false);
  if why is not null then return; end if;
  if (select p.admin from dc.principal p where p.tenant = s.tenant and p.pid = s.principal) is distinct from adm then why := 'unauthorized'; return; end if;
  if tgt is not null then perform dc._lock_target(s.tenant, tgt, s.expires_at, p_lt); end if;
  perform dc._lock_tail(s.tenant, jsonb_build_object('type', typ, 'principal', s.principal, 'req', p_body->'req', 'target', tgt),
                        xa, s.expires_at, p_lt);
  if typ is not null and p_body ? 'value' and jsonb_typeof(p_body->'label'->'subjects') = 'array' then
    sc := -dc._tk(s.tenant);                                   -- F1: the subject locks of this tenant are a class of their own
    for r in select distinct dc._k('subject', s.tenant, x #>> '{}') k from jsonb_array_elements(p_body->'label'->'subjects') x
              where jsonb_typeof(x) = 'string' order by 1 loop
      perform dc._budget(s.expires_at, p_lt);
      perform dc._at(6, r.k);
      perform pg_advisory_xact_lock(sc, r.k);
    end loop;
  end if;
  perform dc._budget(s.expires_at, p_lt);
  perform dc._at(7, 0);
  perform 1 from dc.chainhead where tenant = s.tenant for update;
  why := null;
end $$;

create function dc.submit(p_token text, p_body jsonb) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
begin
  perform dc._no2pc();                      -- review of 2262eaa F2a
  return dc._submit(p_token, p_body, dc._lt());
end $$;

-- Acquire (every wait capped at the session's expiry; a timeout after it is the refusal), then under lock_timeout 1 ms the stage and
-- ONE final judgment, then a revoke's authority writes. A refusal rolls the stage back (its audit rows too). A wait after the
-- acquisition fails the call (55P03), everything rolled back. The SET clause restores the caller's lock_timeout on every exit.
create function dc._submit(p_token text, p_body jsonb, p_lt bigint) returns jsonb language plpgsql set lock_timeout = '0' as $$
#variable_conflict use_variable
declare s dc.sessions; out jsonb; why text; xa boolean;
begin
  perform dc._at_reset();
  perform dc._budget(null, p_lt);
  s := dc._peek(p_token);
  if s.token_hash is null or s.revoked or s.expires_at <= clock_timestamp() then
    return dc._out('unauthenticated:invalid_session', null, null, null);
  end if;
  begin
    select a.why, a.xa into why, xa from dc._acquire_submit(s, p_body, p_lt) a;
    if why is not null then raise exception 'refused by the pins' using errcode = 'DCGAT'; end if;
    perform set_config('lock_timeout', '1ms', true);
    out := dc._submit_stage(s, p_body);
    if not xa and (out->>'target' is not null or out->>'code' = 'granted') then
      raise exception 'an authority change raced its pre-read; retry' using errcode = 'lock_not_available';
    end if;
    why := dc._gate(p_token, s);
    if why is not null then raise exception 'final judgment refused' using errcode = 'DCGAT'; end if;
  exception
    when sqlstate 'DCGAT' then
      perform set_config('lock_timeout', '1ms', true);
      if why = 'principal_revoked' then insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, why); end if;
      return dc._out(why, null, null, null);
    when lock_not_available then
      if clock_timestamp() < s.expires_at then raise; end if;
      return dc._out('unauthenticated:invalid_session', null, null, null);
  end;
  if out->>'target' is not null then perform dc._apply_authority(s.tenant, out->>'target'); end if;
  return out - 'target';
end $$;

-- raw state of one requirement for a verified participant; label filtering is done by the in-process policy point (see header)
create function dc._explain_raw(s dc.sessions, p_req text) returns jsonb language plpgsql stable as $$
#variable_conflict use_variable
declare h jsonb; items jsonb; refs text[]; vals jsonb;
begin
  if s.principal is null or p_req is null then return null; end if;
  if exists (select 1 from dc.principal p where p.tenant = s.tenant and p.pid = s.principal and p.revoked) then return null; end if;
  select x.doc into h from dc.head x where x.tenant = s.tenant and x.req = p_req;
  if h is null then return null; end if;
  if not exists (select 1 from dc.role r where r.tenant = s.tenant and r.req = p_req and r.pid = s.principal)
     and not exists (select 1 from dc.principal p where p.tenant = s.tenant and p.pid = s.principal and p.admin) then
    return null;
  end if;
  select coalesce(jsonb_agg(x.doc order by x.id collate "C"), '[]'::jsonb) into items from dc.props x
   where x.tenant = s.tenant and x.req = p_req and x.status = 'open';
  refs := array(select h->>'value_ref' where jsonb_typeof(h->'value_ref') = 'string'
                union select c->'effect'->>1 from jsonb_array_elements(case when jsonb_typeof(h->'contested') = 'object' then h->'contested'->'candidates' else '[]'::jsonb end) c
                       where c->'effect'->>0 = 'set'
                union select i->>'value_ref' from jsonb_array_elements(items) i);
  select coalesce(jsonb_object_agg(v.ref, jsonb_build_object('text', v.text, 'label', v.label)), '{}'::jsonb) into vals
    from dc.vals v where v.tenant = s.tenant and v.ref = any(refs);
  return jsonb_build_object('principal', s.principal, 'tenant', s.tenant, 'req', p_req, 'head', h, 'open', items, 'values', vals);
end $$;

-- S1 acquisition shared by the reads and erasure: L0 (the entry point's tables), L1 A(caller) shared, L2.0-2.2 the pins. NULL, or the
-- pins' refusal.
create function dc._acquire_session(s dc.sessions, p_lt bigint, p_admin boolean, p_tables text[], p_modes text[]) returns text
language plpgsql as $$
#variable_conflict use_variable
declare k int := dc._ak(s.tenant, s.principal);
begin
  perform dc._rel(s.expires_at, p_lt, p_tables, p_modes);
  if k is not null then                           -- no principal row: nothing to lock; the pins refuse
    perform dc._budget(s.expires_at, p_lt);
    perform dc._at(1, k);
    perform dc._fenced(1145241605, k);
    perform pg_advisory_xact_lock_shared(1145241601, k);
  end if;
  return dc._pin(s, p_lt, p_admin);
end $$;

create function dc.explain(p_token text, p_req text) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
begin
  perform dc._no2pc();                      -- review of 2262eaa F2a
  return dc._read(p_token, p_req, false, dc._lt());
end $$;

create function dc.read_table(p_token text) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
begin
  perform dc._no2pc();                      -- review of 2262eaa F2a
  return coalesce(dc._read(p_token, null, true, dc._lt()), '[]'::jsonb);
end $$;

-- dc.explain (one requirement) and dc.read_table (p_all): acquire, then under 1 ms the read and the final judgment; NULL when refused.
-- The read cannot wait (its tables are locked at L0), so the STABLE dc._explain_raw reads after every wait of the call.
create function dc._read(p_token text, p_req text, p_all boolean, p_lt bigint) returns jsonb language plpgsql set lock_timeout = '0' as $$
#variable_conflict use_variable
declare s dc.sessions; out jsonb;
begin
  perform dc._at_reset();
  perform dc._budget(null, p_lt);
  s := dc._peek(p_token);
  if s.token_hash is null or s.revoked or s.expires_at <= clock_timestamp() then return null; end if;
  begin
    if dc._acquire_session(s, p_lt, false, array['factors', 'head', 'principal', 'props', 'role', 'sessions', 'vals'],
                           array['row share', 'access share', 'row share', 'access share', 'access share', 'row share', 'access share'])
       is not null then
      return null;
    end if;
    perform set_config('lock_timeout', '1ms', true);
    if p_all then
      out := coalesce((select jsonb_agg(x order by x->>'req' collate "C") from (
                select dc._explain_raw(s, h.req) x from dc.head h where h.tenant = s.tenant) q where x is not null), '[]'::jsonb);
    else
      out := dc._explain_raw(s, p_req);
    end if;
    if dc._gate(p_token, s) is not null then return null; end if;
  exception when lock_not_available then
    if clock_timestamp() < s.expires_at then raise; end if;
    return null;
  end;
  return out;
end $$;

-- erasure, step 1 (admin session): the values about a canonical subject and their labels, for the in-process hold/erase decision
create function dc.erase_candidates(p_token text, p_subject text) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
begin
  perform dc._no2pc();                      -- review of 2262eaa F2a
  return dc._erase_candidates(p_token, p_subject, dc._lt());
end $$;

create function dc._erase_candidates(p_token text, p_subject text, p_lt bigint) returns jsonb language plpgsql set lock_timeout = '0' as $$
#variable_conflict use_variable
declare s dc.sessions; out jsonb; deny jsonb := jsonb_build_object('refused', 'unauthorized', 'values', '[]'::jsonb);
begin
  perform dc._at_reset();
  perform dc._budget(null, p_lt);
  s := dc._peek(p_token);
  if s.token_hash is null or s.revoked or s.expires_at <= clock_timestamp() then return deny; end if;
  begin
    if dc._acquire_session(s, p_lt, true, array['factors', 'principal', 'sessions', 'vals'],
                           array['row share', 'row share', 'row share', 'access share']) is not null then
      return deny;
    end if;
    perform set_config('lock_timeout', '1ms', true);
    if p_subject is null or p_subject <> dc._canon_subject(p_subject) then return jsonb_build_object('refused', 'bad_subject', 'values', '[]'::jsonb); end if;
    out := jsonb_build_object('refused', null, 'values', coalesce((select jsonb_agg(jsonb_build_object('ref', v.ref, 'label', v.label) order by v.ref collate "C")
                                                                   from dc.vals v where v.tenant = s.tenant and p_subject = any(v.subjects)), '[]'::jsonb));
    if dc._gate(p_token, s, true) is not null then return deny; end if;
  exception when lock_not_available then
    if clock_timestamp() < s.expires_at then raise; end if;
    return deny;
  end;
  return out;
end $$;

-- erasure, step 2 (admin session): delete the given values (only those still about the subject) and, when no value about the subject
-- remains, the subject's token mapping. S1: L0, L1, the pins (admin), L3 the tenant lock EXCLUSIVE (no submit can mint or use the token
-- meanwhile; it waits FIFO for the submits in flight and new ones queue behind it, XWAIT per attempt), L5.2 the value rows by reference,
-- L5.3 the token row; then under 1 ms the deletes and the final judgment.
create function dc.erase_values(p_token text, p_subject text, p_refs text[]) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
begin
  perform dc._no2pc();                      -- review of 2262eaa F2a
  return dc._erase_values(p_token, p_subject, p_refs, dc._lt());
end $$;

create function dc._erase_values(p_token text, p_subject text, p_refs text[], p_lt bigint) returns jsonb language plpgsql
set lock_timeout = '0' as $$
#variable_conflict use_variable
declare s dc.sessions; n int; rest int; r record; refs text[] := coalesce(p_refs, array[]::text[]);
begin
  perform dc._at_reset();
  perform dc._budget(null, p_lt);
  s := dc._peek(p_token);
  if s.token_hash is null or s.revoked or s.expires_at <= clock_timestamp() then return jsonb_build_object('refused', 'unauthorized'); end if;
  begin
    if dc._acquire_session(s, p_lt, true, array['audit', 'factors', 'principal', 'sessions', 'subject_tokens', 'vals'],
                           array['row exclusive', 'row share', 'row share', 'row share', 'row exclusive', 'row exclusive']) is not null then
      raise exception 'refused by the pins' using errcode = 'DCGAT';
    end if;
    if p_subject is null or p_subject <> dc._canon_subject(p_subject) then return jsonb_build_object('refused', 'bad_subject'); end if;
    perform dc._budget(s.expires_at, p_lt, dc._xwait_ms());
    perform dc._at(3, 0);
    perform dc._xlock(1145241602, dc._tk(s.tenant), 1145241606, s.expires_at, p_lt);
    for r in select v.ref from dc.vals v where v.tenant = s.tenant and v.ref = any(refs) and p_subject = any(v.subjects)
              order by v.ref collate "C" loop
      perform dc._budget(s.expires_at, p_lt);
      perform dc._at(5, 2);
      perform 1 from dc.vals v where v.tenant = s.tenant and v.ref = r.ref for update;
    end loop;
    perform dc._budget(s.expires_at, p_lt);
    perform dc._at(5, 3);
    perform 1 from dc.subject_tokens t where t.tenant = s.tenant and t.subject = p_subject for update;
    perform set_config('lock_timeout', '1ms', true);
    delete from dc.vals v where v.tenant = s.tenant and v.ref = any(refs) and p_subject = any(v.subjects);
    get diagnostics n = row_count;
    select count(*) into rest from dc.vals v where v.tenant = s.tenant and p_subject = any(v.subjects);
    if rest = 0 then delete from dc.subject_tokens t where t.tenant = s.tenant and t.subject = p_subject; end if;
    insert into dc.audit(tenant, principal, code, ev) values (s.tenant, s.principal, 'erase_subject', jsonb_build_object('erased', n, 'remaining', rest));
    if dc._gate(p_token, s, true) is not null then raise exception 'final judgment refused' using errcode = 'DCGAT'; end if;
  exception
    when sqlstate 'DCGAT' then
      return jsonb_build_object('refused', 'unauthorized');
    when lock_not_available then
      if clock_timestamp() < s.expires_at then raise; end if;
      return jsonb_build_object('refused', 'unauthorized');
  end;
  return jsonb_build_object('refused', null, 'erased', n, 'remaining', rest);
end $$;

reset role;

-- ------------------------------------------------------------------------------------------------ privileges: the API and nothing else
revoke all on schema dc from public;
revoke all on schema dc_crypto from public;
revoke create on schema public from public;
revoke all on all tables in schema dc from public;
revoke all on all sequences in schema dc from public;
revoke execute on all functions in schema dc from public;
revoke execute on all functions in schema dc_crypto from public;
grant usage on schema dc_crypto to dc_owner;
grant execute on all functions in schema dc_crypto to dc_owner;
grant usage on schema dc to dc_app, dc_idp;
grant execute on function dc.submit(text, jsonb), dc.explain(text, text), dc.read_table(text), dc.erase_candidates(text, text),
                          dc.erase_values(text, text, text[]) to dc_app;
grant execute on function dc.open_session(text, integer), dc.end_session(text) to dc_idp;
-- 4.4: the only function that signals backends runs as dc_evictor (NOLOGIN, member of pg_signal_backend, nothing else) and only
-- owner code can call it; no login role can.
alter function dc._evict(int, int) owner to dc_evictor;
grant execute on function dc._evict(int, int) to dc_owner;
-- 4.4: the advisory-lock functions are PUBLIC by default, so a login role could take A, T, a fence or an idempotency lock with a raw
-- call and hold it outside any API call. In this database only the owner's code may call them (pg_proc is per database; other
-- databases keep the default).
-- Review of 2262eaa F8: every EXPLICIT grant is revoked too (to dc_app, dc_idp, a role they are members of, or any other role), and
-- install refuses unless, afterwards, neither dc_app nor dc_idp can execute any of them (directly or through a membership).
do $$
declare f regprocedure; g text;
begin
  for f in select p.oid::regprocedure from pg_catalog.pg_proc p
            where p.pronamespace = 'pg_catalog'::regnamespace and p.proname like 'pg\_%advisory%' loop
    for g in select distinct case when a.grantee = 0 then 'public' else quote_ident(r.rolname) end
               from pg_catalog.pg_proc p cross join lateral pg_catalog.aclexplode(coalesce(p.proacl, pg_catalog.acldefault('f', p.proowner))) a
               left join pg_catalog.pg_roles r on r.oid = a.grantee
              where p.oid = f and a.grantee <> p.proowner and a.privilege_type = 'EXECUTE' loop
      execute format('revoke execute on function %s from %s cascade', f, g);
    end loop;
    execute format('grant execute on function %s to dc_owner', f);
  end loop;
  if exists (select 1 from pg_catalog.pg_proc p
              where p.pronamespace = 'pg_catalog'::regnamespace and p.proname like 'pg\_%advisory%'
                and (pg_catalog.has_function_privilege('dc_app', p.oid, 'EXECUTE') or pg_catalog.has_function_privilege('dc_idp', p.oid, 'EXECUTE'))) then
    raise exception 'install refused: dc_app or dc_idp can still execute an advisory-lock function';
  end if;
end $$;
