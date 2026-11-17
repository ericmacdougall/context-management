"""Local fallback for a poison alert that Postgres will not take (goodqa#16096 deploy part 1, review F2).

When the alert write keeps failing while the database is otherwise up (for example the alert table is not writable by the
engine's role), the activity wrapper stops retrying after a bounded number of attempts, appends one JSON line here, logs
loudly on stderr and lets the Poison failure through. The input is therefore never dropped silently (it is dead-lettered or
skipped by the workflow as before, and the spool holds the alert) and never blocks the session for ever. The spool is drained into
`engine_alerts` by the next successful alert write path (`reconcile` activity, worker start, `python -m engine.alerts drain`).

The spool is a file on the worker host: it survives a worker restart, not the loss of that host. Lines carry ids, the
activity name, the exception class and the activity attempt only (no content). File mode 0600."""
import json
import os
import time

from .common import HOME


def path():
    return os.environ.get("ENGINE_ALERT_SPOOL") or str(HOME / "alerts-spool.jsonl")


def append(rec: dict) -> None:
    rec = dict(rec, spooled_at=time.time())
    fd = os.open(path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)


def drain(write) -> dict:
    """Feed every spooled record to `write(rec)`; keep the ones it fails on. Returns {"written": n, "kept": m}.
    The file is renamed first so a concurrent append goes to a fresh spool and is not lost."""
    p = path()
    work = p + ".draining.%d" % os.getpid()
    try:
        os.rename(p, work)
    except FileNotFoundError:
        return {"written": 0, "kept": 0}
    written, kept = 0, []
    with open(work) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                kept.append(line.rstrip("\n"))
                continue
            try:
                write(rec)
                written += 1
            except Exception:  # noqa: BLE001 - keep it for the next drain
                kept.append(line.rstrip("\n"))
    if kept:
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, ("\n".join(kept) + "\n").encode())
        finally:
            os.close(fd)
    os.remove(work)
    return {"written": written, "kept": len(kept)}
