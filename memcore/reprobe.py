"""Re-probing licence tier (goodqa#16096 deploy part 3).

The start-up probe (memcore.capabilities.probe + apply) ran once and pinned `strategy="client"` for the life of the process:
a service that started while the node was unreachable or still on Basic never used backend fusion after the node answered
or after a licence arrived (5 of 5 restarts stayed on the Basic path, licence/DEGRADE.md). This tracker repeats the same
single GET /_license on a stated interval and re-applies the tier:

  - enterprise or trial, active: strategy "auto", and the adapter's own licence latch is cleared so the very next fused()
    call asks the node again instead of waiting out the latch's TTL;
  - anything else, including a probe error: strategy "client" (fail towards the required Basic path), with a short retry
    that doubles up to the interval;
  - a definite answer is re-checked every `interval_s`, so a lapse is noticed without waiting for a 403 (the adapter still
    falls back in-call on a 403, which stays the second line).

The loop never dies on one bad refresh: an exception is recorded and the next attempt follows the retry schedule. The probe
reads only /_license, which a Basic node allows, so re-probing cannot produce a licence 403."""
from __future__ import annotations

import collections
import threading
import time
from typing import Callable, Optional

from . import capabilities

DEFAULT_INTERVAL_S = 60.0
DEFAULT_BACKOFF_S = 1.0


class ReprobingTier:
    def __init__(self, search, interval_s: float = DEFAULT_INTERVAL_S, backoff_s: float = DEFAULT_BACKOFF_S,
                 clock: Callable[[], float] = time.time, initial: Optional[dict] = None,
                 on_change: Optional[Callable[[dict], None]] = None, wait: Callable[[float], bool] = None):
        if interval_s <= 0 or backoff_s <= 0:
            raise ValueError("interval_s and backoff_s must be positive")
        self.search, self.interval_s, self.backoff_s, self.clock = search, float(interval_s), float(backoff_s), clock
        self.on_change = on_change
        self.report = initial
        self.history = collections.deque(maxlen=200)       # (time, tier, probe_error, licence type, status, refresh error)
        self.failures = 0                                  # consecutive refreshes that raised or probed with an error
        self.last_error = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._wait = wait or self._stop.wait
        if initial is None:
            self._due = clock()                             # no initial answer: probe at the first opportunity
        elif initial.get("probe_error"):
            self.failures = 1                               # the start-up probe could not reach the node: retry soon
            self._due = clock() + self._retry_delay()
        else:
            self._due = clock() + self.interval_s

    def _retry_delay(self) -> float:
        return min(self.backoff_s * (2 ** max(self.failures - 1, 0)), self.interval_s)

    def tier(self) -> Optional[str]:
        return None if self.report is None else self.report["tier"]

    def refresh(self) -> Optional[dict]:
        """One probe and one re-apply. Never raises. Returns the report, or None when the refresh itself failed."""
        try:
            rep = capabilities.probe(capabilities.es_get_json(self.search))
            with self._lock:
                before = self.tier()
                if rep["tier"] == "basic":
                    capabilities.apply(rep, self.search)
                else:
                    self.search.strategy = "auto"
                    reset = getattr(self.search, "reset_probe", None)
                    if reset is not None:
                        reset()
                self.report = rep
                self.failures = self.failures + 1 if rep.get("probe_error") else 0
                self.last_error = None
                self.history.append((self.clock(), rep["tier"], rep.get("probe_error"), rep["licence_type"], rep["licence_status"], None))
                self._due = self.clock() + (self._retry_delay() if rep.get("probe_error") else self.interval_s)
            if self.on_change is not None and before != rep["tier"]:
                self.on_change(rep)
            return rep
        except Exception as e:  # noqa: BLE001 - a failed refresh (including a failing on_change) must not stop re-probing
            with self._lock:
                self.failures += 1
                self.last_error = type(e).__name__
                self.history.append((self.clock(), self.tier(), None, None, None, type(e).__name__))
                self._due = self.clock() + self._retry_delay()
            return None

    def maybe_refresh(self) -> Optional[dict]:
        if self.clock() >= self._due:
            return self.refresh()
        return None

    def start(self, poll_s: float = 0.2) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.is_set():
                self.maybe_refresh()
                self._wait(poll_s)
        self._thread = threading.Thread(target=loop, name="memcore-reprobe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
