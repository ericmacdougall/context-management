-- Control variants for exp/scale_1m.py: the shipped functions as of 4960f50, before items 2 and 3 of that round.
-- wp_erasure_cascade held the job row FOR UPDATE for the whole batch; wp_erasure_pg_residue scanned the tenant.

create function wp_erasure_cascade(jid text, batch int) returns int language plpgsql security definer set search_path = public, pg_temp as $$
declare j record; cand text[]; n int := 0; held int;
begin
  select * into j from erasure_jobs where job_id = jid for update;
  perform wp_hold_check(j.tenant_id, j.subject_id);         -- a hold placed after mark stops the cascade at the next batch
  cand := array(select event_id from events where tenant_id = j.tenant_id and subject_id = j.subject_id and event_id > j.own_after
                order by event_id limit batch);
  if cardinality(cand) > 0 then
    update erasure_jobs set own_after = cand[cardinality(cand)] where job_id = jid;
    perform public.wp_erasure_apply(j.tenant_id, j.subject_id, jid, cand);
    return cardinality(cand);
  end if;
  cand := array(select event_id from erasure_mentions where tenant_id = j.tenant_id and subject_id = j.subject_id and event_id > j.men_after
                order by event_id limit batch);
  if cardinality(cand) > 0 then
    update erasure_jobs set men_after = cand[cardinality(cand)] where job_id = jid;
    perform public.wp_erasure_apply(j.tenant_id, j.subject_id, jid, cand);
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
  select count(*) into held from erasure_withheld where job_id = jid;
  if held = 0 and (select coalesce(rescanned_xmax, -1) >= coalesce(ident_xmax, 0) from erasure_jobs where job_id = jid) then
    delete from subject_identifiers where tenant_id = j.tenant_id and subject_id = j.subject_id;   -- only after the rescan used them
    update erasure_jobs set state = 'cascaded', updated_at = clock_timestamp() where job_id = jid and state = 'marked';
  end if;
  return 0;
end $$;

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
    'pg:subject_not_marked', (select count(*) from subjects where tenant_id = t and subject_id = s and not erased),
    'pg:withheld_by_hold', (select count(*) from erasure_withheld w join erasure_jobs jb on jb.job_id = w.job_id where jb.tenant_id = t and jb.subject_id = s));
$$;
