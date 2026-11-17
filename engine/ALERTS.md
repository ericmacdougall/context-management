# Poisoned-input alerts (goodqa#16096 deploy part 1)

## What the code did before (3423f4e)
- The only alert was `st["alert"]`, one string in workflow state, overwritten by the next alert and readable only through the
  `status` query of a live run. A poison whose dead-letter succeeded raised no alert at all (a `turn_dead_letter` row only).
- When the dead-letter also failed, the input went into the workflow's `poisoned` set, passed to `turn_read` as a skip list and
  carried across continue-as-new. All three places cut it to `sorted(...)[:20]`, a TEXT sort (`/10` before `/2`). From the 21st
  poisoned input in one session newer inputs deferred behind it at the 60 s backoff (no spin, no progress); at a continue-as-new
  boundary ids past 20 were forgotten and, once out of the 40 recently-done ids, reconcile queued them again.
- Workflow state survives a worker restart (Temporal history), and a successful dead-letter is a row in Postgres. What did not
  survive a finished workflow or a rebuilt engine was the alert itself and, for a failed dead-letter, the skip set.

## What it does now
- Every Poison failure writes an alert row first (`engine_alerts`, additive DDL in `engine_schema.sql`, ensured at worker start),
  whether or not the dead-letter later works. The row is idempotent per (tenant, session, input, kind): a retry after a crash
  between commit and delivery moves `last_attempt` (highest activity attempt seen) and `last_seen`; it is not an occurrence count.
  Stored and logged: ids, activity name, exception CLASS, attempt (and `updated_by`, an operator label, after an operator acts); no
  content. One stderr JSON line on first creation.
- Any input with an alert row is skipped in SQL by `has_older_unprocessed` and `unprocessed_inputs`: no skip list, no cap. Any number
  of poisoned inputs in one session leave the next input unblocked and none is re-queued after a continue-as-new. The workflow's own
  `poisoned` list and `[:20]` cuts are unchanged and now redundant while Postgres is reachable; they still matter in the one case
  below (alert spooled, not yet in Postgres).
- An activity with no session (`get_config`) alerts under the unattached key ('-', '-') and logs `poison_unattached`; no Poison
  failure is silent.

## When the alert cannot be written (fail-safe, review F2)
The write is retried with the activity for 5 attempts past the poison threshold (attempts 5 to 9). After that the alert is appended
to a local spool (`alertspool.py`, mode 0600), logged loudly (`poison_alert_spooled`) and the Poison failure goes through. Chosen:
the session must not block behind one input for ever (the dead-letter activity has no schedule-to-close bound, so unbounded retry
would stall it), and the input must not vanish (it is dead-lettered or skipped by the workflow, and the spool keeps the alert). The
spool is drained into Postgres by the `reconcile` activity, at worker start and by `python -m engine.alerts drain`. Tradeoffs: the
spool is a file on one worker host (lost with the host); until it drains, an alert is not in Postgres, so the SQL skip does not
cover that input and only the workflow's skip list (up to 20) does. A Postgres outage is different: transient errors retry
without bound as before, and no poison is classified while the database is down.

## Operators (`python -m engine.alerts`)
`list` (exit 1 while any alert is open), `ack` (seen; the input stays skipped), `release`, `drain`.
`release` RESUBMITS the input: it appends a NEW input of the same session with the same content (marked `retry_of` and
`retry_depth`) through the normal path, so reconcile or a doorbell picks it up. The original keeps its alert row (status `released`,
`retried_as` = the new event id) and stays skipped for ever. Why not re-open the original: after a successful dead-letter it owns
the result-key effects row and a dead-letter event in the append-only hash chain, and after a failed one the workflow holds it in
`done`/`poisoned`; neither can be undone safely from outside. Consequences: the retried input is processed after newer inputs
(it loses its place in the sequence); it needs a running workflow or a new run, as any input does; releasing twice returns the same
new input; a retry that poisons again raises its own alert, and the chain is refused beyond depth 3, so a still-poisoned input
cannot loop. An erased subject cannot be released (the content is gone).

Trust boundary: the tool runs with the engine's database credentials and trusts the tenant and operator name it is given. `--by` is
an audit label, not authentication; `--tenant` is not checked against the caller. Whoever can run it can act on every tenant's
alerts, so it is for trusted operators on the engine host only.

## Rollback
The table is additive; leaving it in place is harmless. An image from before this change ignores `engine_alerts` entirely: it
restores the old behaviour, including the 20-item skip cap, and alerts stop being written (existing rows stay). Resubmitted inputs
are ordinary inputs and need nothing undone.

## ASSUMPTION (not the user's words; accepted by main)
"More than 20 poisoned inputs in one session block progress" is read as the RV-ENG limit to remove, not behaviour to keep. A hard
stop at N would be a workflow change and a new variant.

## Not shown
- Postgres-backed tests (`ENGINE_TEST_PG=1`) and a Temporal run with release through the real workflow: see the report for what has run.
- Concurrent first-start DDL and a runtime role without DDL privilege are not tested.
