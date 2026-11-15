-- Stand-in for an external key service (own Postgres container, holds ONLY keys). Authenticated encryption:
-- pgcrypto pgp_sym_encrypt_bytea (AES-256 with MDC integrity packet). Production needs a real key service whose
-- deletes are real and whose backups are short-lived; here destroy = DELETE + VACUUM + CHECKPOINT, wal_recycle=off, no archiving.
create extension if not exists pgcrypto;
create table keys (tenant_id text not null, subject_id text not null, key text not null, primary key (tenant_id, subject_id));
create table destroyed (tenant_id text not null, subject_id text not null, at timestamptz not null default now(), primary key (tenant_id, subject_id));
create table tenant_keys (tenant_id text primary key, hkey text not null);

create function kms_key(t text, s text) returns text language plpgsql as $$
declare k text;
begin
  perform pg_advisory_xact_lock(hashtextextended('kms:' || t || '/' || s, 0));
  if exists (select 1 from destroyed where tenant_id = t and subject_id = s) then raise exception 'subject_erased: %', s using errcode = 'P0001'; end if;
  select key into k from keys where tenant_id = t and subject_id = s;
  if k is null then
    k := encode(gen_random_bytes(32), 'hex');
    insert into keys(tenant_id, subject_id, key) values (t, s, k);
  end if;
  return k;
end $$;

create function kms_enc(t text, s text, p bytea) returns bytea language sql as $$
  select pgp_sym_encrypt_bytea(p, kms_key(t, s), 'cipher-algo=aes256, s2k-mode=0, compress-algo=0')
$$;

create function kms_dec(t text, s text, c bytea) returns bytea language plpgsql stable as $$
declare k text;
begin
  select key into k from keys where tenant_id = t and subject_id = s;
  if k is null then return null; end if;
  return pgp_sym_decrypt_bytea(c, k, 'cipher-algo=aes256, s2k-mode=0, compress-algo=0');
exception when others then return null;
end $$;

create function kms_hmac(t text, x text) returns text language plpgsql as $$
declare k text;
begin
  insert into tenant_keys(tenant_id, hkey) values (t, encode(gen_random_bytes(32), 'hex')) on conflict do nothing;
  select hkey into k from tenant_keys where tenant_id = t;
  return encode(hmac(x::bytea, k::bytea, 'sha256'), 'hex');
end $$;

create function kms_destroy(t text, s text) returns void language plpgsql as $$
begin
  perform pg_advisory_xact_lock(hashtextextended('kms:' || t || '/' || s, 0));
  delete from keys where tenant_id = t and subject_id = s;
  insert into destroyed(tenant_id, subject_id) values (t, s) on conflict do nothing;
end $$;
