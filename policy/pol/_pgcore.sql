-- Decision core IN THE DATABASE (round 3, R3-7). Run by pol.pgcore.install as the superuser; every object is owned by dc_owner.
--
-- Boundary. The application role dc_app holds NO table, column or sequence privilege. It may EXECUTE only the session-keyed API
-- (dc.submit, dc.explain, dc.read_table, dc.erase_candidates, dc.erase_values), each SECURITY DEFINER with a pinned search_path. The
-- identity adapter's role dc_idp may EXECUTE only dc.open_session. Identity (tenant, principal, channel) comes ONLY from a dc.sessions
-- row found by sha256(token); the row is written only by dc.open_session. Authority changes (revoke, grant) are applied inside the
-- same function, from the event that function built and judged: there is no separately callable apply step.
--
-- The rules are CORE-RULES.md, ported rule by rule from pol/_core.py (the Python core stays as the differential oracle). Labels are
-- normalised and joined here exactly as pol.labels does (sorted with COLLATE "C", which orders UTF-8 by code point like Python).
-- Label filtering of READS stays in the in-process policy point (pol.enforce): dc.explain returns raw state to a verified participant
-- and the Python service filters it by label. That part is trusted in-process code, not enforced by the database (disclosed).
--
-- ASSUMPTION (round 3, not confirmed by the user): non-humans only propose and remark; the per-req cap binds non-human authors only;
-- idempotency is per (tenant, principal, idem) with a fingerprint over the event, never the session token.

drop schema if exists dc cascade;
create schema dc authorization dc_owner;
create schema if not exists dc_crypto authorization dc_owner;
create extension if not exists pgcrypto with schema dc_crypto;
set role dc_owner;

create table dc.tenant(tenant text primary key, lk int not null default 0);
create table dc.principal(tenant text not null, pid text not null, kind text not null, admin bool not null, revoked bool not null default false,
                          primary key (tenant, pid));
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
create unique index events_id on dc.events(tenant, req, evid) where req is not null;
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
create table dc.vkey(id int primary key check (id = 1), k bytea not null);          -- value-ref HMAC key; never leaves the database
insert into dc.vkey values (1, dc_crypto.gen_random_bytes(32));

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
      tgt := op->>1;
      update dc.principal set revoked = true where tenant = p_tenant and pid = tgt;
      update dc.head h set open_n = h.open_n - x.c
        from (select req, count(*) c from dc.props where tenant = p_tenant and author = tgt and status = 'open' and doc->>'author_kind' <> 'human'
              group by req) x
        where h.tenant = p_tenant and h.req = x.req;
      update dc.props set status = 'invalidated', doc = doc || '{"status": "invalidated", "closed_by": null}'::jsonb
        where tenant = p_tenant and author = tgt and status = 'open';
      update dc.sessions set revoked = true where tenant = p_tenant and principal = tgt;
    elsif k = 'grant' then
      insert into dc.role values (p_tenant, op->>1, op->>3, op->>2) on conflict do nothing;
    else
      raise exception 'unknown delta op %', k;
    end if;
  end loop;
end $$;

-- the lock order (tenant row, then the (tenant, principal) advisory lock, then the requirement's head row); called before _reduce_apply
create function dc._lock_event(p_tenant text, ev jsonb) returns void language plpgsql as $$
#variable_conflict use_variable
declare typ text := case when jsonb_typeof(ev) = 'object' then ev->>'type' end;
begin
  if typ in ('revoke', 'grant') then perform 1 from dc.tenant where tenant = p_tenant for update;
  else perform 1 from dc.tenant where tenant = p_tenant for share; end if;
  if not found then raise exception 'tenant not installed'; end if;
  if jsonb_typeof(ev) = 'object' and jsonb_typeof(ev->'principal') = 'string' then
    perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array(p_tenant, ev->>'principal')::text, 0));
  end if;
  if jsonb_typeof(ev) = 'object' and typ is distinct from 'revoke' and jsonb_typeof(ev->'req') = 'string' then
    perform 1 from dc.head where tenant = p_tenant and req = ev->>'req' for update;
  end if;
end $$;

-- reduce(view, ev) + apply: {code, original}. Locks must already be held (_lock_event).
create function dc._reduce_apply(p_tenant text, ev jsonb) returns jsonb language plpgsql as $$
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
  return jsonb_build_object('code', code, 'original', null);
end $$;

-- OWNER-ONLY raw entry for tests and experiments (differential, B3): locks, then reduce/apply. Executable by no login role.
create function dc._apply_event(p_tenant text, ev jsonb) returns jsonb language plpgsql as $$
#variable_conflict use_variable
begin
  perform dc._lock_event(p_tenant, ev);
  return dc._reduce_apply(p_tenant, ev);
end $$;

-- ------------------------------------------------------------------------------------------------ sessions (identity adapter only)
create function dc.open_session(p_tenant text, p_principal text, p_channel text, p_factor text, p_ttl_s integer) returns text
language plpgsql security definer set search_path = dc, pg_temp as $$
#variable_conflict use_variable
declare k text; tok text;
begin
  select p.kind into k from dc.principal p where p.tenant = p_tenant and p.pid = p_principal and not p.revoked;
  if k is null then raise exception 'unknown or revoked principal' using errcode = 'P0001'; end if;
  if (k = 'human' and p_channel not in ('owner_ui', 'owner_cli')) or (k <> 'human' and p_channel is distinct from 'agent_api') then
    raise exception 'channel does not match the principal kind' using errcode = 'P0001';
  end if;
  if p_factor is null or p_factor = '' or p_ttl_s is null or p_ttl_s < 1 or p_ttl_s > 86400 then raise exception 'bad factor or ttl'; end if;
  tok := replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '');
  insert into dc.sessions values (encode(sha256(convert_to(tok, 'UTF8')), 'hex'), p_tenant, p_principal, p_channel, p_factor,
                                  now() + make_interval(secs => p_ttl_s), false);
  return tok;
end $$;

create function dc._session(p_token text) returns dc.sessions language sql stable as $$
  select s.* from dc.sessions s
   where p_token is not null and p_token <> '' and s.token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex')
     and not s.revoked and s.expires_at > now()
$$;

-- ------------------------------------------------------------------------------------------------ the application API (dc_app)
create function dc._out(code text, orig text, evid text, ref text) returns jsonb language sql immutable as $$
  select jsonb_build_object('code', code, 'original', orig, 'id', evid, 'value_ref', ref)
$$;

create function dc.submit(p_token text, p_body jsonb) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
#variable_conflict use_variable
declare s dc.sessions; ev jsonb; cid text; typ text; lab jsonb; tlab jsonb; vref text; created bool := false; r jsonb; code text;
        body_keys text[] := array['type','client_id','idem','req','value','label','based_on','proposal','proposal_hash','pending','verb',
                                  'choose','target','role'];
        subj text; toks text[] := array[]::text[]; tok text; x jsonb;
begin
  s := dc._session(p_token);
  if s.principal is null then return dc._out('unauthenticated:invalid_session', null, null, null); end if;
  if p_body is null or jsonb_typeof(p_body) <> 'object' then
    insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'bad_event'); return dc._out('bad_event', null, null, null);
  end if;
  if exists (select 1 from dc.principal p where p.tenant = s.tenant and p.pid = s.principal and p.revoked) then
    insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'principal_revoked'); return dc._out('principal_revoked', null, null, null);
  end if;
  if p_body ?| array['tenant', 'principal', 'channel', 'id'] then
    insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'identity_in_body'); return dc._out('identity_in_body', null, null, null);
  end if;
  cid := case when jsonb_typeof(p_body->'client_id') = 'string' then p_body->>'client_id' end;
  if exists (select 1 from jsonb_object_keys(p_body) k where k <> all(body_keys)) or cid is null or cid !~ '^[A-Za-z0-9._-]{1,64}$' then
    insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'bad_event'); return dc._out('bad_event', null, null, null);
  end if;
  ev := (p_body - 'client_id' - 'value' - 'label')
        || jsonb_build_object('id', s.principal || '/' || cid, 'tenant', s.tenant, 'principal', s.principal, 'channel', s.channel);
  perform dc._lock_event(s.tenant, ev);                 -- tenant FOR SHARE: erase_values (FOR UPDATE) cannot interleave with minting below
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
    vref := 'val-' || left(encode(dc_crypto.hmac(convert_to(jsonb_build_array(s.tenant, s.principal, coalesce(p_body->>'idem', ''), p_body->>'value', tlab)::text, 'UTF8'),
                                                  (select k from dc.vkey where id = 1), 'sha256'), 'hex'), 40);
    insert into dc.vals values (s.tenant, vref, p_body->>'value', lab, array(select jsonb_array_elements_text(lab->'subjects')))
      on conflict do nothing;
    created := found;
    ev := ev || jsonb_build_object('value_ref', vref, 'label', tlab);
  elsif p_body ? 'label' then
    insert into dc.audit(tenant, principal, code) values (s.tenant, s.principal, 'bad_event'); return dc._out('bad_event', null, null, null);
  end if;
  r := dc._reduce_apply(s.tenant, ev);
  code := r->>'code';
  if code in ('bad_event','tenant_mismatch','unauthorized','principal_revoked','unknown_req','duplicate_id','proposal_cap','channel_mismatch',
              'agent_not_permitted','agent_cannot_confirm','unknown_pending','pending_closed','same_channel_confirmation','unknown_proposal',
              'proposal_mismatch','stale','proposal_closed','candidates_full','contested_needs_resolve','not_contested','nothing_to_retract',
              'unknown_candidate','idem_conflict','duplicate') then
    if vref is not null and created then delete from dc.vals v where v.tenant = s.tenant and v.ref = vref; end if;
    return dc._out(code, r->>'original', ev->>'id', case when code = 'duplicate' and vref is not null and not created then vref end);
  end if;
  return dc._out(code, null, ev->>'id', vref);
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

create function dc.explain(p_token text, p_req text) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
#variable_conflict use_variable
begin
  return dc._explain_raw(dc._session(p_token), p_req);
end $$;

create function dc.read_table(p_token text) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
#variable_conflict use_variable
declare s dc.sessions := dc._session(p_token);
begin
  if s.principal is null then return '[]'::jsonb; end if;
  return coalesce((select jsonb_agg(x order by x->>'req' collate "C") from (
            select dc._explain_raw(s, h.req) x from dc.head h where h.tenant = s.tenant) q where x is not null), '[]'::jsonb);
end $$;

-- erasure, step 1 (admin session): the values about a canonical subject and their labels, for the in-process hold/erase decision
create function dc.erase_candidates(p_token text, p_subject text) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
#variable_conflict use_variable
declare s dc.sessions := dc._session(p_token);
begin
  if s.principal is null or not exists (select 1 from dc.principal p where p.tenant = s.tenant and p.pid = s.principal and p.admin and not p.revoked) then
    return jsonb_build_object('refused', 'unauthorized', 'values', '[]'::jsonb);
  end if;
  if p_subject is null or p_subject <> dc._canon_subject(p_subject) then return jsonb_build_object('refused', 'bad_subject', 'values', '[]'::jsonb); end if;
  return jsonb_build_object('refused', null, 'values', coalesce((select jsonb_agg(jsonb_build_object('ref', v.ref, 'label', v.label) order by v.ref collate "C")
                                                                 from dc.vals v where v.tenant = s.tenant and p_subject = any(v.subjects)), '[]'::jsonb));
end $$;

-- erasure, step 2 (admin session): delete the given values (only those still about the subject) and, when no value about the subject
-- remains, the subject's token mapping; all under the tenant row FOR UPDATE, so no submit can mint or use the token meanwhile
create function dc.erase_values(p_token text, p_subject text, p_refs text[]) returns jsonb language plpgsql security definer set search_path = dc, pg_temp as $$
#variable_conflict use_variable
declare s dc.sessions := dc._session(p_token); n int; rest int;
begin
  if s.principal is null or not exists (select 1 from dc.principal p where p.tenant = s.tenant and p.pid = s.principal and p.admin and not p.revoked) then
    return jsonb_build_object('refused', 'unauthorized');
  end if;
  if p_subject is null or p_subject <> dc._canon_subject(p_subject) then return jsonb_build_object('refused', 'bad_subject'); end if;
  perform 1 from dc.tenant where tenant = s.tenant for update;
  delete from dc.vals v where v.tenant = s.tenant and v.ref = any(coalesce(p_refs, array[]::text[])) and p_subject = any(v.subjects);
  get diagnostics n = row_count;
  select count(*) into rest from dc.vals v where v.tenant = s.tenant and p_subject = any(v.subjects);
  if rest = 0 then delete from dc.subject_tokens t where t.tenant = s.tenant and t.subject = p_subject; end if;
  insert into dc.audit(tenant, principal, code, ev) values (s.tenant, s.principal, 'erase_subject', jsonb_build_object('erased', n, 'remaining', rest));
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
grant execute on function dc.open_session(text, text, text, text, integer) to dc_idp;
