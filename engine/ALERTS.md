# Poisoned-input alerts (goodqa#16096 deploy part 1)

## What the code did before (3423f4e)
- The only alert was `st["alert"]`, one string in workflow state, overwritten by the next alert and readable only through the
  `status` query of a live run. A poison whose dead-letter succeeded raised no alert at all (a `turn_dead_letter` row only).
- When the dead-letter also failed, the input went into the workflow's `poisoned` set, passed to `turn_read` as a skip list and
  carried across continue-as-new. All three places cut it to `sorted(...)[:20]`, a TEXT sort (`/10` before `/2`).
  From the 21st poisoned input in one session: newer inputs deferred behind it at the 60 s backoff (no spin, no progress, alert
  overwritten by `blocked_by_older_input`); at a continue-as-new boundary ids past 20 were forgotten and, once out of the 40
  recently-done ids, reconcile queued them again (5 attempts and one more dead-letter try each, per run).
- Nothing survived a finished workflow, a rebuilt engine or a worker restart.

## What it does now
- `activities.tracked` writes the alert to Postgres (`engine_alerts`, additive DDL in `engine_schema.sql`, created by
  `pgstore.ensure_alert_schema()` at worker start) BEFORE it raises the non-retryable Poison failure, for every poison, whether or
  not the dead-letter later works. It stores ids and the exception CLASS name only and logs one JSON line to stderr on first
  creation. If the write fails the activity is retried (the alert write is retried with it); the workflow is never failed by it.
- Open and acked alerts are skipped in SQL by `has_older_unprocessed` and `unprocessed_inputs`: no skip list, no cap. Any number
  of poisoned inputs in one session leave the next input unblocked, and none is re-queued after a continue-as-new.
- Operators: `python -m engine.alerts list | ack | release` (list exits 1 while any alert is open). `ack` keeps the input skipped,
  `release` retries it, a new poison on a released input reopens the alert.
- The workflow is unchanged (no replay change for in-flight sessions); its `poisoned` list and its `[:20]` cuts remain as a
  harmless optimisation. `st["alert"]` and the `status` query are unchanged.

## ASSUMPTION (not the user's words; main to confirm)
The design's condition "more than 20 poisoned inputs in one session block progress" is read as the RV-ENG limit to remove, not a
behaviour to keep. If a hard stop is wanted instead (a session pauses itself at N poisoned inputs), that is a workflow change and
a new variant.

## Not shown
- The Postgres-backed tests (`ENGINE_TEST_PG=1`) and a Temporal run with 25 poisoned inputs have not been run; they need the lab run
  lock on t2-dev. If Postgres is down for longer than the activity's own retries, alerts wait in the retry loop with the activity.
- A poison inside an activity without a session (`get_config`) cannot be attached to a session and raises no alert.
