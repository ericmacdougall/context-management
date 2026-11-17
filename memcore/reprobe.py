"""Re-probing licence tier (goodqa#16096 deploy part 3).

The start-up probe (memcore.capabilities.probe + apply) ran once and pinned `strategy="client"` for the life of the process:
a service that started while the node was unreachable or still on Basic never used backend fusion after the node answered
or after a licence arrived (5 of 5 restarts stayed on the Basic path, licence/DEGRADE.md). This tracker repeats the same
single GET /_license on a stated interval and re-applies the tier:

  - enterprise or trial, active: strategy "auto"; when the tier CHANGED to this (or the strategy had been pinned) the adapter's own
    licence latch is cleared so the next fused() call asks the node again instead of waiting out the latch's TTL. An unchanged
    enterprise answer does not touch the latch (no extra retriever query per interval);
  - anything else, including a probe error: strategy "client" (fail towards the required Basic path), with a short retry
    that doubles up to the interval (the exponent is capped: no overflow however long the node stays down);
  - a definite answer is re-checked every `interval_s`, so a lapse is noticed without waiting for a 403.

Guarantees and their limits:
  - A failing refresh (including a failing `on_change` publication) is recorded and never stops the loop. A publication that failed
    is retried until it succeeds, even when the tier does not change again (so capabilities.json cannot keep advertising a tier the
    search no longer follows).
  - Scheduling uses a monotonic clock by default: a wall-clock step cannot defer or burst refreshes.
  - RACE (not closed): the tracker does not synchronise with ElasticsearchSearch.fused(). A query that passed the strategy check just
    before a Basic refresh can still send one retriever request after it. The server's licence 403 is the enforcement boundary: the
    adapter turns it into an in-call fall back to client fusion. An adapter probe in flight can also overwrite a concurrent latch
    reset; the next interval corrects it.
  - stop() waits up to 5 s; the probe's own transport timeout can be 30 s, so a request may outlive stop(); a refresh that finishes
    after stop() applies nothing.
  - The probe reads only /_license, which a Basic licence allows (an entitlement; a missing or restricted API key is a different
    failure and surfaces as a probe error, which fails towards Basic)."""
from __future__ import annotations

import collections
import math
import threading
import time
from typing import Callable, Optional

from . import capabilities

DEFAULT_INTERVAL_S = 60.0
DEFAULT_BACKOFF_S = 1.0
_MAX_EXPONENT = 30          # backoff * 2**30 is far above any interval; capped BEFORE exponentiation


class ReprobingTier:
    def __init__(self, search, interval_s: float = DEFAULT_INTERVAL_S, backoff_s: float = DEFAULT_BACKOFF_S,
                 clock: Callable[[], float] = time.monotonic, initial: Optional[dict] = None,
                 on_change: Optional[Callable[[dict], None]] = None, wait: Callable[[float], bool] = None):
        for name, v in (("interval_s", interval_s), ("backoff_s", backoff_s)):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
                raise ValueError("%s must be a finite number greater than zero, got %r" % (name, v))
        self.search, self.interval_s, self.backoff_s, self.clock = search, float(interval_s), float(backoff_s), clock
        self.on_change = on_change
        self.report = initial
        self.history = collections.deque(maxlen=200)       # (time, tier, probe_error, licence type, status, refresh error)
        self.failures = 0                                  # consecutive refreshes that raised or probed with an error
        self.last_error = None
        self._published = self._key(initial)               # what on_change last accepted (the start-up report is published by the service)
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

    @staticmethod
    def _key(rep):
        return None if rep is None else (rep["tier"], rep["licence_type"], rep["licence_status"])

    def _retry_delay(self) -> float:
        exp = min(max(self.failures - 1, 0), _MAX_EXPONENT)
        return min(self.backoff_s * (2 ** exp), self.interval_s)

    def tier(self) -> Optional[str]:
        return None if self.report is None else self.report["tier"]

    def _publish(self) -> bool:
        """Offer the current report to on_change until it accepts it. Returns True when nothing is pending."""
        if self.on_change is None or self._key(self.report) == self._published:
            return True
        rep = self.report
        try:
            self.on_change(rep)
        except Exception as e:  # noqa: BLE001 - retried on the next pass; never stops the loop
            self.last_error = "publish:" + type(e).__name__
            self.history.append((self.clock(), self.tier(), None, None, None, "publish:" + type(e).__name__))
            return False
        self._published = self._key(rep)
        return True

    def refresh(self) -> Optional[dict]:
        """One probe, one re-apply, one publication attempt. Never raises. Returns the report, or None when the refresh itself failed."""
        try:
            rep = capabilities.probe(capabilities.es_get_json(self.search))
            if self._stop.is_set():
                return None                                 # stopped while the request was in flight: apply nothing
            with self._lock:
                before = self.tier()
                was_auto = getattr(self.search, "strategy", None) == "auto"
                if rep["tier"] == "basic":
                    capabilities.apply(rep, self.search)
                else:
                    self.search.strategy = "auto"
                    reset = getattr(self.search, "reset_probe", None)
                    if reset is not None and (before != "enterprise" or not was_auto):
                        reset()                             # only on a change: an unchanged licensed answer keeps the adapter's latch
                self.report = rep
                self.failures = self.failures + 1 if rep.get("probe_error") else 0
                self.last_error = None
                self.history.append((self.clock(), rep["tier"], rep.get("probe_error"), rep["licence_type"], rep["licence_status"], None))
                self._due = self.clock() + (self._retry_delay() if rep.get("probe_error") else self.interval_s)
            published = self._publish()
            if not published:
                with self._lock:
                    self._due = min(self._due, self.clock() + self._retry_delay_for_publish())
            return rep
        except Exception as e:  # noqa: BLE001 - a failed refresh must not stop re-probing
            with self._lock:
                self.failures += 1
                self.last_error = type(e).__name__
                self.history.append((self.clock(), self.tier(), None, None, None, type(e).__name__))
                self._due = self.clock() + self._retry_delay()
            return None

    def _retry_delay_for_publish(self) -> float:
        return min(self.backoff_s, self.interval_s)

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
