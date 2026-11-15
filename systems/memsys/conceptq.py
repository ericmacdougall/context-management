"""Concept rows as a keyed job queue: dirty flag + next_fold_at, claimed with
SKIP LOCKED, debounce with an upper bound, fenced by a claim token.

Policy (registered before the C8 run, see exp/c8_concept_queue.py):
  debounce  D : each new activity pushes next_fold_at to now()+D ...
  max_wait  M : ... but never beyond dirty_since+M (the upper bound).
  lease     L : a claimed row is invisible to other workers until lease_until.
A fold commits only with the claim token it started with and only if no new
activity arrived since (gen unchanged); otherwise the row stays dirty.
"""
import hashlib
import uuid

DEBOUNCE_S = 1.0
MAX_WAIT_S = 5.0
LEASE_S = 30.0


def touch_concepts(cur, tenant, subjects, debounce=DEBOUNCE_S, max_wait=MAX_WAIT_S, n=1):
    """Record activity for concepts (one upsert per concept)."""
    subjects = sorted(set(subjects))
    if not subjects:
        return
    cur.execute(
        "insert into concepts(tenant_id, concept_id, dirty, dirty_since, last_activity, next_fold_at, gen) "
        "select %s, s, true, clock_timestamp(), clock_timestamp(), clock_timestamp() + make_interval(secs => %s), %s "
        "from unnest(%s::text[]) s "
        "on conflict (tenant_id, concept_id) do update set "
        " dirty_since = case when concepts.dirty then concepts.dirty_since else clock_timestamp() end, "
        " pending_since = case when concepts.claim_token is not null then coalesce(concepts.pending_since, clock_timestamp()) else concepts.pending_since end, "
        " dirty = true, last_activity = clock_timestamp(), gen = concepts.gen + %s, "
        " next_fold_at = least(clock_timestamp() + make_interval(secs => %s), "
        "     (case when concepts.dirty then concepts.dirty_since else clock_timestamp() end) + make_interval(secs => %s))",
        (tenant, debounce, n, subjects, n, debounce, max_wait))


def claim(conn, limit=1, lease=LEASE_S):
    """Claim due concepts. Returns [(tenant, concept_id, token, gen)]."""
    token = uuid.uuid4().hex
    with conn.transaction():
        rows = conn.execute(
            "with due as (select tenant_id, concept_id from concepts where dirty and next_fold_at <= clock_timestamp() "
            " and (lease_until is null or lease_until < clock_timestamp()) "
            " order by next_fold_at limit %s for update skip locked) "
            "update concepts c set claim_token = %s || '-' || c.concept_id, lease_until = clock_timestamp() + make_interval(secs => %s) "
            "from due where c.tenant_id = due.tenant_id and c.concept_id = due.concept_id "
            "returning c.tenant_id, c.concept_id, c.claim_token, c.gen", (limit, token, lease)).fetchall()
    return rows


def compute_fold(cur, tenant, concept_id, recipe=1, kms=None):
    """Deterministic stand-in for the LLM fold: a digest over the entity's current facts (concept_id is the keyed
    entity hash; no plaintext in the row). Regenerated from facts only, never from the previous concept state."""
    from . import kms as _kms
    kms = kms or _kms.get()
    rows = cur.execute(
        "select id, object_enc, subject_id, input_event_ids from facts where tenant_id=%s and recipe_version=%s and entity_hash=%s and status='current' "
        "order by slot_hash, id", (tenant, recipe, concept_id)).fetchall()
    objs = kms.dec_many(tenant, [(r[2], r[1]) for r in rows])
    live = [(r, o) for r, o in zip(rows, objs) if o is not None]
    fids = [r[0] for r, _ in live]
    evs = sorted({e for r, _ in live for e in r[3]})
    h = hashlib.sha256("|".join(f"{r[0]}={o}" for r, o in live).encode()).hexdigest()
    return fids, evs, h


def commit_fold(conn, tenant, concept_id, token, gen_at_claim, result, debounce=DEBOUNCE_S, max_wait=MAX_WAIT_S, fence=True):
    """Fenced commit. Returns 'committed' | 'committed_dirty' | 'rejected'.
    Activity that arrived during the fold (gen moved) keeps the row dirty, and its
    deadline is measured from the FIRST such activity (pending_since)."""
    fids, evs, digest = result
    guard, extra = "", []
    if fence:
        guard = ("and claim_token = %s and not exists (select 1 from tombstones tb "
                 "where tb.tenant_id = %s and tb.event_id = any(%s))")
        extra = [token, tenant, evs]
    with conn.transaction():
        r = conn.execute(
            "update concepts set input_fact_ids=%s, input_event_ids=%s, digest=%s, fold_count=fold_count+1, "
            " dirty = (gen <> %s), folded_gen = %s, claim_token = null, lease_until = null, "
            " dirty_since = case when gen <> %s then coalesce(pending_since, clock_timestamp()) else dirty_since end, "
            " next_fold_at = case when gen <> %s then least(clock_timestamp() + make_interval(secs => %s), "
            "     coalesce(pending_since, clock_timestamp()) + make_interval(secs => %s)) else next_fold_at end, "
            " pending_since = null "
            f"where tenant_id=%s and concept_id=%s {guard} returning dirty",
            [fids, evs, digest, gen_at_claim, gen_at_claim, gen_at_claim, gen_at_claim, debounce, max_wait,
             tenant, concept_id] + extra).fetchone()
    if r is None:
        return "rejected"
    return "committed_dirty" if r[0] else "committed"
