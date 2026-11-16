-- Lab stand-in for an external key service (KMS), in its own database, owned by role wp_kms (runs as SET ROLE wp_kms).
-- Holds ONLY per-subject data keys and a tenant HMAC key. The application role has no access to this database at all;
-- the verifier may see which subjects still have a key (column privileges, never the key material).
-- Production replaces this with a KMS whose deletion is real and whose backups have a stated retention bound.
create table subject_keys (tenant_id text not null, subject_id text not null, key_hex text not null, primary key (tenant_id, subject_id));
create table destroyed (tenant_id text not null, subject_id text not null, at timestamptz not null default now(), primary key (tenant_id, subject_id));
create table tenant_keys (tenant_id text primary key, hkey_hex text not null);
revoke all on all tables in schema public from public;
grant usage on schema public to wp_verifier;
grant select (tenant_id, subject_id) on subject_keys to wp_verifier;
grant select on destroyed to wp_verifier;
