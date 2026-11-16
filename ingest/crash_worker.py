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


if __name__ == "__main__":
    main(json.loads(sys.argv[1]))
