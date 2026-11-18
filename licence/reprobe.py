"""Proposed re-probing tier tracker (NOT part of the reviewed adapters). The reviewed capabilities.apply() pins strategy='client' for the
life of the process after one start-up probe, so a service that starts while the node is unreadable (or on Basic) never uses Enterprise
fusion after the node answers (or after the licence returns). This wrapper re-runs the same probe and re-applies the tier:
 - enterprise (active trial/enterprise): strategy 'auto' (the adapter then verifies with its own licence latch, so a lapse falls back in-call);
 - anything else, including probe errors: strategy 'client' (fail towards the required Basic path) and a quick retry (backoff_s);
 - a definite answer is re-checked every interval_s, so a lapse is noticed without waiting for a 403."""
import threading
import time

from memcore import capabilities


class ReprobingTier:
    def __init__(self, search, interval_s=5.0, backoff_s=1.0, clock=time.time):
        self.search, self.interval_s, self.backoff_s, self.clock = search, interval_s, backoff_s, clock
        self.report, self.history, self._due = None, [], 0.0
        self._stop = threading.Event()
        self._t = None

    def refresh(self):
        rep = capabilities.probe(capabilities.es_get_json(self.search))
        self.report = rep
        if rep["tier"] == "basic":
            capabilities.apply(rep, self.search)
        else:
            self.search.strategy = "auto"
        self.history.append((self.clock(), rep["tier"], rep.get("probe_error"), rep["licence_type"], rep["licence_status"]))
        self._due = self.clock() + (self.backoff_s if rep.get("probe_error") else self.interval_s)
        return rep

    def maybe_refresh(self):
        if self.clock() >= self._due:
            return self.refresh()
        return None

    def start(self):
        def loop():
            while not self._stop.is_set():
                self.maybe_refresh()
                self._stop.wait(0.1)
        self._t = threading.Thread(target=loop, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=5)
