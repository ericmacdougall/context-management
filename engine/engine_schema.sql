-- Engine-side tables, loaded AFTER the systems write-path schema (systems/memsys/schema.sql, copied verbatim
-- as pg/schema_systems.sql on the host). Sessions use that schema's append-only, hash-chained `events` unchanged.

-- Idempotency record for every external effect and every idempotent append (turn results, rollups, approvals).
create table if not exists effects (
  key text primary key,
  tenant_id text not null, session_id text not null, step text not null,
  status text not null, result_ref text, created_at timestamptz not null default now()
);
create index if not exists effects_session on effects (tenant_id, session_id, step);

-- The EXTERNAL system stand-in. ext_calls logs every invocation (retries included); the system applies a key once
-- (its own idempotency, like a payment API honouring an idempotency key); effect_ledger is written by the effect
-- itself, once per real application, and is what the experiments compare effect keys against.
create table if not exists ext_calls (id bigserial primary key, key text not null, worker text, at timestamptz not null default now());
create table if not exists ext_applied (key text primary key, applied_at timestamptz not null default now());
create table if not exists effect_ledger (id bigserial primary key, key text not null, ref_event text not null, at timestamptz not null default now());
create index if not exists effect_ledger_ref on effect_ledger (ref_event);

-- How many payloads the payload check inspected (flushed by every process that has a codec).
create table if not exists payload_stats (
  proc text primary key, inspected bigint not null default 0, rejected bigint not null default 0,
  max_bytes int not null default 0, updated_at timestamptz not null default now()
);

-- Configuration read by the engine through an activity (never hard-coded in workflow code): the F5 task-queue hop.
create table if not exists engine_config (name text primary key, value text not null);

-- v2 (multi-tenant review fixes), additive so old and new code can share one database:
-- tenant columns for scoped verification, and a (tenant, key) uniqueness that backs the tenant-prefixed keys.
alter table ext_calls add column if not exists tenant_id text;
alter table effect_ledger add column if not exists tenant_id text;
create unique index if not exists effects_tenant_key on effects (tenant_id, key);
create index if not exists ext_calls_tenant on ext_calls (tenant_id);
create index if not exists effect_ledger_tenant on effect_ledger (tenant_id);
