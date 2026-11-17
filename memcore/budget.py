"""One counted, capped, ledgered choke point for every model-touching path (chat calls, session turns, streams, embeddings).
The counter is a flock-guarded file written atomically (temp + rename); an unreadable counter FAILS CLOSED (a crash while writing
must never reset the cap to zero)."""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from typing import Optional

from .ports import CallCapExceeded

_LOCKS = {}
_GUARD = threading.Lock()


class CallBudget:
    def __init__(self, directory: str, cap: int, ledger_path: Optional[str] = None, name: str = "calls"):
        os.makedirs(directory, exist_ok=True)
        self.dir, self.cap, self.ledger_path = directory, cap, ledger_path
        self.counter = os.path.join(directory, name + ".count")
        with _GUARD:
            self._lock = _LOCKS.setdefault(os.path.abspath(self.counter), threading.Lock())

    def used(self) -> int:
        try:
            with open(self.counter) as fh:
                raw = fh.read().strip()
        except FileNotFoundError:
            return 0
        try:
            n = int(raw)
            if n < 0:
                raise ValueError
            return n
        except ValueError:
            raise CallCapExceeded("call counter %s is unreadable (%r); refusing to guess (fail closed)" % (self.counter, raw[:20]))

    def reserve(self) -> int:
        with self._lock:
            with open(self.counter + ".lock", "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                n = self.used()
                if n >= self.cap:
                    raise CallCapExceeded("call cap %d reached for %s" % (self.cap, self.dir))
                tmp = self.counter + ".tmp%d" % os.getpid()
                with open(tmp, "w") as fh:
                    fh.write(str(n + 1))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.counter)
                return n + 1

    def ledger(self, **row) -> None:
        if not self.ledger_path:
            return
        row["ts"] = round(time.time(), 3)
        with self._lock, open(self.ledger_path, "a") as fh:
            fh.write(json.dumps(row) + "\n")
