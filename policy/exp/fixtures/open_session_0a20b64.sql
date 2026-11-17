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
