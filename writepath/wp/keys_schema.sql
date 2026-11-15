-- Lab stand-in for an external key service (KMS). Holds ONLY per-subject data keys and a tenant HMAC key.
-- Production replaces this with a KMS whose deletion is real and whose backups have a stated retention bound.
create table subject_keys (tenant_id text not null, subject_id text not null, key_hex text not null, primary key (tenant_id, subject_id));
create table destroyed (tenant_id text not null, subject_id text not null, at timestamptz not null default now(), primary key (tenant_id, subject_id));
create table tenant_keys (tenant_id text primary key, hkey_hex text not null);
