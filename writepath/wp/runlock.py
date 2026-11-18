"""The run lock as the store tools see it (GATE-DESIGN 9, review aacf3c0 F-1).

exp/disk_gate.py `run` holds an exclusive flock on WP_HOME/state/run.lock from before admission until after the run's
end record, shared with its experiment. A tool that wipes or rebuilds the stores (exp.cleanup, wp.setup) calls
`hold(name)` before touching anything:
- started by the gate itself, it is given the gate's lock descriptor (WP_RUN_LOCK_FD). The descriptor must refer to
  run.lock and flock on it must succeed: it does only on the gate's own open file description, which holds the lock;
  a descriptor the caller opened afresh conflicts with the gate's lock and is refused;
- started any other way, it takes the lock itself, non-blocking, and keeps it until it exits, so no run can be
  admitted while it works. While a run holds the lock it refuses (exit 2) and changes nothing.
flock(2): a lock belongs to the open file description, is shared by descriptors inherited through fork and execve,
and conflicts with a lock taken through another open of the same file."""
import fcntl
import os
import sys

from . import config

LOCK = config.HOME / "state" / "run.lock"
_held = []                     # the lock file this process took itself, kept open until the process exits


def _refuse(tool, why):
    print(f"{tool}: refused: {why}; nothing was changed")
    sys.exit(2)


def hold(tool):
    """Return when this process may change the stores; exit 2 otherwise."""
    fd = os.environ.get("WP_RUN_LOCK_FD")
    if fd is not None:
        try:
            n = int(fd)
            a, b = os.fstat(n), os.stat(LOCK)
            if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                _refuse(tool, "WP_RUN_LOCK_FD does not refer to the run lock state/run.lock")
            fcntl.flock(n, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ValueError, OSError):
            _refuse(tool, "WP_RUN_LOCK_FD is not a descriptor holding the run lock (another run holds it)")
        return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        _refuse(tool, "a run holds the run lock state/run.lock (its gate or experiment is alive); the gate resets "
                      "the stores itself at its next admission")
    _held.append(f)
