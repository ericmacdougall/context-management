"""Subprocess body for crash tests: runs a host loop through the tap and os._exit(137) at a named hook on call k."""
from __future__ import annotations

import json
import os
import sys

from . import shapes
from .controls import LateTap
from .stubs import text
from .testkit import Rig
from .tap import CrashPlan


def main(cfg: dict) -> None:
    provider, stream, point, k, n_calls = cfg["provider"], cfg["stream"], cfg["point"], cfg["k"], cfg["n_calls"]
    if provider == "anthropic":
        script = [{"content": [text("a%d" % i)]} for i in range(1, n_calls + 1)]
    else:
        script = [{"message": {"role": "assistant", "content": "a%d" % i}} for i in range(1, n_calls + 1)]
    cur = {"n": 0}

    def hit(p, idx=None):
        if cur["n"] == k and p == point and (p != "mid_stream" or idx == 2):
            os._exit(137)

    rig = Rig(cfg["dir"], provider, script, stream=stream, truth=True, stub_kw={"event_delay_s": 0.0},
              tap_kw={"crash": hit})
    if cfg["tap"] == "late":
        rig.tap.__class__ = LateTap
    for i in range(1, n_calls + 1):
        cur["n"] = i
        rig.ask("u%d" % i)
        rig.truth.add(ev="host_received", n=i)
        hit("after_return")
        rig.tap.flush()
    sys.exit(0)




def stream_contract(cfg: dict) -> None:
    """Crash mid-stream (os._exit) while the host consumes the stream; the host's received events are written to the truth file."""
    provider, delivery, at = cfg["provider"], cfg["delivery"], cfg["at"]

    def hit(p, idx=None):
        if p == "mid_stream" and idx == at:
            os._exit(137)
    script = [{"content": [text("a long answer made of several small chunks of text")]}] if provider == "anthropic" else \
        [{"message": {"role": "assistant", "content": "a long answer made of several small chunks of text"}}]
    rig = Rig(cfg["dir"], provider, script, stream=True, truth=True, tap_kw={"crash": hit, "stream_delivery": delivery})
    rig.msgs.append({"role": "user", "content": "q"})
    for ev in rig.tap.call(rig.request(), stream=True):
        rig.truth.add(ev="host_event", n=1)
    sys.exit(0)


if __name__ == "__main__":
    cfg = json.loads(sys.argv[1])
    (stream_contract if cfg.get("mode") == "stream_contract" else main)(cfg)
