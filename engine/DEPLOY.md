# Engine deploy path: downgrade guards and rollback (goodqa#16096 deploy part 2)

Scope: the Temporal server image, its two Postgres databases (main and visibility), the session worker, and the session state
carried in workflow input. The session store (events, effects) in the engine's Postgres is the source of truth; Temporal holds
ids and cursors only.

## What is refused, and where

| Older thing | Newer data | Today without the guard | With the guard |
|---|---|---|---|
| Temporal server image | database at a newer schema | the older image STARTS and keeps running (F7 control: server 1.30.9 on schema 1.19) | `python -m engine.deploy_guard schema ...` exits 3 (`SchemaTooNew`); `lib.tpg_guard` calls the same `check_schema` for the lab harness. A client deploy script must call the CLI before it starts an image: this repo does not contain that script |
| Session worker | session state with a newer `ver` | v1 code on v2 state raised `KeyError 'rollups'`, 4 of 4 sessions stuck RUNNING (F6); an additive newer state would run silently | `deploy_guard.check_state` is the FIRST statement of `SessionWorkflow.__init__` (`@workflow.init`) in every variant in `engine/wf/`, each declaring `SUPPORTED_STATE_VER` (2 for v2s and v2sx, 1 for the rest) |

Why `__init__`: that is the only place the state is loaded. Signal and query handlers cannot run before the instance exists, a
standalone `Replayer` builds the same class (so the replay path is covered by the same line), and activities receive ids and
scalars, never the state dictionary, so they have nothing to guard. A refusal is a workflow TASK failure (`StateTooNew`,
`InvalidState` are RuntimeErrors, not ApplicationErrors): the run is not failed, its history is untouched, the task retries, and the
session proceeds when a worker that supports the version polls the queue. Nothing is rewritten or deleted. The guard adds no
command to any workflow and changes nothing for accepted input; replay of existing histories under the new code is the check named
under "Not shown".

Limits of the guard, stated plainly:
- `ver` is an ordinary field of the workflow input, authenticated by nothing. Whoever can start a workflow or continue-as-new with
  arbitrary input can label newer-shaped state as version 1. The engine's own driver (`ctl`) is the only writer in this repo; close
  the gap by allowing only the engine API to start sessions.
- A state without `ver` is version 1 (legacy policy). A state that is not an object, or whose `ver` is not a positive integer, is
  refused (`InvalidState`).
- The guard compares versions only. A workflow CODE version older than the history's is caught by Temporal's own non-determinism
  check.

## What really migrates, and when (as the code does it)

`v2s` calls `state.migrate(dict(st))` in `__init__`, i.e. at the start of EVERY workflow task that constructs the run: first start,
continue-as-new, and every rebuild of the run from history (cache eviction, worker restart, a newer worker taking over). The
migration is pure and in memory; the run's recorded input in Temporal history is NOT rewritten. It becomes v2-shaped only when the
run continues-as-new under v2 code, because `continue_as_new(st)` passes the migrated `self.st`.

Rollback consequence, therefore:
- A run that has not yet continued-as-new still has a v1-shaped input in history. An older worker could replay it only if the
  command sequence is unchanged; `v2s` differs from `v1` in state handling only, so replay should pass, but it is NOT tested here.
- A run that continued-as-new under a v2 worker has a v2 input. An older worker refuses it (task failure, run stays RUNNING).
  The only ways forward are to roll the worker forward again, or to restore the pre-upgrade snapshot (which loses Temporal history
  since the snapshot; sessions rebuild from the Postgres log, F8).
- So once a newer worker has run a session through one continue-as-new, an older worker never resumes it without a snapshot restore.

Routing is not enforced by code. Worker pinning is optional (`--pinned`; the deployment mode defaults to AUTO_UPGRADE when a
deployment is used, and to a build id otherwise). To keep an older worker away from migrated runs, put the newer worker on its own
task queue or a pinned deployment version and drain the old one only after every session has reached its boundary. The pinned
plus continue-as-new combination is untested (OPEN-QUESTIONS K11).

## Upgrade order

1. Snapshot both databases under their own names (`pg_dump -Fc` of the main and the `_vis` database). Temporal stores the database
   name inside `schema_version`; a restore under another name is refused (first attempt in the F7 rollback drill).
2. Run the schema tool of the TARGET image, then start the new server image, then roll workers.
3. A worker that introduces a new state version rolls out as above (own queue or pinned version).

Preflight for a server image (numbers: the image's `/etc/temporal/schema/postgresql/v12/{temporal,visibility}/versioned` directory,
newest entry; and `select curr_version from schema_version order by creation_time desc limit 1` in each database):

    python3 -m engine.deploy_guard schema --expected-main 1.18 --expected-visibility 1.13 --actual-main 1.19 --actual-visibility 1.14
    # exit 0: safe to start; exit 3: refused (JSON on stdout), including a malformed version

## Rollback criteria (ASSUMPTION: thresholds are proposals for the deployer; they are not the user's words)

Roll the SERVER back (restore the snapshot, start the old image) when, within 30 minutes of the new image starting, ANY of:

1. It is not ready within 5 minutes. Check: `temporal operator cluster health` (or the server's health endpoint) every 10 s.
2. More than 1 percent of sessions that were RUNNING before the change have a stalled or failing workflow task for over 10
   minutes. Check, the numerator: `temporal workflow list --query 'ExecutionStatus="Running"'` joined with the workflow task
   failure indicator your server version exposes (the `TemporalReportedProblems` search attribute on recent servers; confirm it
   on yours, this was not verified here); the denominator: the same list before the change.
3. Any lost or duplicated external effect against the Postgres ledger:
   - duplicates: `select key, count(*) from effect_ledger group by key having count(*) > 1;` (expect no rows);
   - inputs with no result for over 10 minutes and no alert:
     `select e.tenant_id, e.session_id, e.event_id from events e where e.kind='turn_input' and e.ts_event < now() - interval '10 minutes' and not exists (select 1 from effects f where f.tenant_id=e.tenant_id and f.key = e.tenant_id || '/res/' || e.event_id) and not exists (select 1 from engine_alerts a where a.tenant_id=e.tenant_id and a.session_id=e.session_id and a.event_id=e.event_id);`
     (expect no rows once `reconcile` has run; this query is the same truth `unprocessed_inputs` uses).

Rollback is the snapshot restore, never "start the old image on the upgraded database" (the guard refuses it). Cost: Temporal
history written since the snapshot is lost; sessions are rebuilt from the Postgres log by reconcile (F8: 86 sessions, 1,440 turns, 0
redundant external calls on a fresh engine). Inputs appended meanwhile are processed once by reconcile; approval deadlines come from
Postgres, not from Temporal timers.

Roll a WORKER back only if no session has continued-as-new under the newer state version (see above). After that, roll forward.

## Not shown

- Replay of an existing (older) history under the guarded code, and a refusal followed by roll-forward against a real Temporal
  server (task retries, session resumes under a newer worker): not run yet. Where the guarded workflow classes were constructed
  directly with the engine venv on t2-dev (temporalio 1.36.1), see the test log committed under `engine/results/` if present.
- A multi-service cluster (single-container server only); a pinned deployment routing drill.
