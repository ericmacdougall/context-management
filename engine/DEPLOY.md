# Engine deploy path: downgrade guards and rollback (goodqa#16096 deploy part 2)

Scope: the Temporal server image, its two Postgres databases (main and visibility), the session worker, and the session state
carried in workflow input. The session store (events, effects) in the engine's Postgres is the source of truth; Temporal holds
ids and cursors only.

## What is refused, and by what

| Older thing | Newer data | Today without the guard | With the guard |
|---|---|---|---|
| Temporal server image | database at a newer schema (after an upgrade) | the older image STARTS and keeps running (F7 control: server 1.30.9 on schema 1.19) | `engine.deploy_guard.check_schema` refuses (`SchemaTooNew`); CLI exit 3. `lib.tpg_guard` calls it for the lab harness; a client deploy script calls the CLI before it starts an image |
| Session worker | session state with a newer `ver` | accident: v1 code on v2 state raised `KeyError 'rollups'` and 4 of 4 sessions stuck RUNNING (F6); an additive newer state would run silently | `engine.guard_interceptor.StateGuardInterceptor` (installed by `engine/worker.py`) refuses by name (`StateTooNew`) before any workflow code runs |

A worker refusal is a workflow TASK failure, not a failed workflow: the history is untouched, the task retries, and the session
resumes when a worker that supports the state's version polls the queue. Nothing is rewritten or deleted by a refusal.

Preflight for a server image (numbers come from the image's `/etc/temporal/schema/postgresql/v12/{temporal,visibility}/versioned`
directory and from `select curr_version from schema_version order by creation_time desc limit 1` in each database):

    python3 -m engine.deploy_guard schema --expected-main 1.18 --expected-visibility 1.13 --actual-main 1.19 --actual-visibility 1.14
    # exit 0: safe to start; exit 3: refused, the reason is printed as JSON

## Upgrade order

1. Snapshot both databases under their own names (`pg_dump -Fc` of the main and the `_vis` database). Temporal stores the
   database name inside `schema_version`; a restore under another name is refused (first attempt in the F7 rollback drill).
2. Run the schema tool of the TARGET image, then start the new server image, then roll workers.
3. Workers that introduce a new session-state version roll out only after every worker that cannot read it is gone from the
   task queue; a state migrates at a continue-as-new boundary, so an old worker keeps running old sessions until each reaches
   its boundary (pinned deployments plus a boundary hop: untested together, K11).

## Rollback criteria (ASSUMPTION: thresholds are proposed for the deployer to set; they are not the user's words)

Roll the SERVER back (restore the snapshot, start the old image) when, within 30 minutes of the new image starting:
- it does not report ready within 5 minutes, or
- the share of RUNNING sessions with a failing workflow task or an unscheduled activity for over 10 minutes exceeds 1 percent, or
- any session shows a lost or duplicated external effect against the Postgres effect ledger (reconcile count differs).

Rollback is the snapshot restore, never "start the old image on the upgraded database" (the guard refuses it). Cost: Temporal
history written since the snapshot is lost; sessions are rebuilt from the Postgres log by reconcile (F8: 86 sessions, 1,440 turns,
0 redundant external calls on a fresh engine). Inputs appended meanwhile are processed once by reconcile; approval deadlines
come from Postgres, not from Temporal timers.

Roll a WORKER back only if no session has migrated to the newer state version. After a migration, do not roll back: roll
forward. An older worker refuses the migrated sessions (they stay RUNNING, task retrying) until a newer worker returns; that is
the F6 outcome made explicit, and the recovery is the same (roll forward).

## Not shown

- The interceptor has been unit-tested against the SDK interface on t2-dev; a refusal against a real Temporal server (task
  retries, session resumes under a newer worker) is not shown yet. A multi-service cluster is not covered (single-container
  server only).
- Worker-level refusal covers state `ver` only. A workflow CODE version older than the history's (replay of newer commands) is
  caught by Temporal's own non-determinism check, not by this guard.
