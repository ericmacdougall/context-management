"""Temporal PayloadCodec that runs the payload check on every payload leaving a client or worker (signals, activity
args and results, workflow input, continue-as-new input, workflow results) and counts what it inspected."""
import threading
from typing import List, Sequence

from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

from .payloadcheck import check_bytes


class CheckingCodec(PayloadCodec):
    """Counts and checks; `on_flush(inspected, rejected, max_bytes)` is called from a background thread."""

    def __init__(self, on_flush=None, flush_every=1.0):
        self.inspected = 0
        self.rejected = 0
        self.max_bytes = 0
        self._lock = threading.Lock()
        self._on_flush = on_flush
        self._flush_every = flush_every
        if on_flush:
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()

    def _loop(self):
        import time
        while True:
            time.sleep(self._flush_every)
            self.flush()

    def flush(self):
        if self._on_flush:
            with self._lock:
                snap = (self.inspected, self.rejected, self.max_bytes)
            try:
                self._on_flush(*snap)
            except Exception:
                pass

    async def encode(self, payloads: Sequence[Payload]) -> List[Payload]:
        for p in payloads:
            with self._lock:
                self.inspected += 1
                self.max_bytes = max(self.max_bytes, len(p.data))
            try:
                check_bytes(p.data, p.metadata.get("encoding", b""))
            except Exception:
                with self._lock:
                    self.rejected += 1
                raise
        return list(payloads)

    async def decode(self, payloads: Sequence[Payload]) -> List[Payload]:
        return list(payloads)
