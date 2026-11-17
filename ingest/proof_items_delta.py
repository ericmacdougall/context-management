"""Proof runs added for review 2 (PREREG-amend-3 A3): hosts that repeat content, and the retry-identity probe. Stubs only."""
from __future__ import annotations

import json
import shutil
from collections import Counter
from typing import List

from .proofs_common import event_tuples, repeated_conversation, tmpdir, write_artifact
from .stubs import text
from .testkit import Rig

PATTERNS = ("same", "alt", "tri")
WINDOWS = (4, 6, 8)


def _arm(prov, pat, w, per_call, boundary, calls=20):
    d = tmpdir()
    try:
        rig, led = repeated_conversation(Rig, d, prov, pat, w, calls, per_call=per_call, boundary=boundary)
        rig.sync()
        ev = rig.events()
        comp = rig.ing.completeness(rig.stream_id)
        exp, got = led.expected(), event_tuples(ev)
        missing = Counter(map(tuple, exp)) - Counter(map(tuple, got))
        extra = Counter(map(tuple, got)) - Counter(map(tuple, exp))
        return {"provider": prov, "pattern": pat, "window_msgs": w, "new_per_call": per_call, "boundary_declared": boundary, "calls": calls,
                "expected_events": len(exp), "got_events": len(got), "oracle_match": got == exp, "missing_events": sum(missing.values()),
                "extra_events": sum(extra.values()), "flagged_request_events": sum(1 for e in ev if json.loads(e["meta"]).get("ambiguous_repeat")),
                "ambiguous_calls": comp["ambiguous_calls"], "declared_calls": comp["declared_calls"], "state": comp["state"],
                "blocking_gaps": [g["kind"] for g in comp["blocking_gaps"]], "exactly_once": rig.ing.claim_exactly_once(rig.stream_id)}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _summ(rows):
    return {"runs": len(rows), "exact_and_unblocked": sum(1 for r in rows if r["oracle_match"] and not r["blocking_gaps"] and r["exactly_once"]),
            "no_event_lost": sum(1 for r in rows if r["missing_events"] == 0), "with_blocking_gap": sum(1 for r in rows if r["blocking_gaps"]),
            "exactly_once_claimed": sum(1 for r in rows if r["exactly_once"]), "extra_flagged_events": sum(r["extra_events"] for r in rows)}


def item13(out: str) -> dict:
    """Repeated-content hosts, one new message per call, two arms (PREREG-amend-4 A1)."""
    runs = []
    for prov in ("anthropic", "openai"):
        for pat in PATTERNS:
            for w in WINDOWS:
                runs.append(_arm(prov, pat, w, 1, True))
                runs.append(_arm(prov, pat, w, 1, False))
        for w in (4, 8):
            runs.append(_arm(prov, "unique", w, 1, False))
    A = [r for r in runs if r["pattern"] != "unique" and r["boundary_declared"]]
    B = [r for r in runs if r["pattern"] != "unique" and not r["boundary_declared"]]
    U = [r for r in runs if r["pattern"] == "unique"]
    S = {"declared_boundary": _summ(A), "no_boundary": _summ(B), "unique_text": _summ(U),
         "unique_runs_with_ambiguity_flag": sum(1 for r in U if r["ambiguous_calls"])}
    S["artifact"] = write_artifact(out, "item13", {"patterns": PATTERNS, "windows": WINDOWS, "calls": 20, "providers": 2, "arms": ["declared new_count", "none"]}, runs, S)
    return S


def item13c(out: str) -> dict:
    """Hosts that append 2 (window 6) or 3 (window 8) new messages per call under repeating content, both arms."""
    runs = []
    for prov in ("anthropic", "openai"):
        for pat in ("same", "alt"):
            for w, k in ((6, 2), (8, 3)):
                runs.append(_arm(prov, pat, w, k, True, calls=12))
                runs.append(_arm(prov, pat, w, k, False, calls=12))
    A = [r for r in runs if r["boundary_declared"]]
    B = [r for r in runs if not r["boundary_declared"]]
    S = {"declared_boundary": _summ(A), "no_boundary": _summ(B)}
    S["artifact"] = write_artifact(out, "item13c", {"patterns": ["same", "alt"], "cells": "window 6 with 2 new, window 8 with 3 new", "calls": 12, "providers": 2}, runs, S)
    return S


def item13b(out: str) -> dict:
    """(a) undeclared identical resend, (b) declared retry, (c) retry claim naming a call whose keys differ. 10 seeds x 2 providers."""
    runs: List[dict] = []
    for prov in ("anthropic", "openai"):
        for seed in range(101, 111):
            word = "hello %d" % seed
            row = {"provider": prov, "seed": seed}
            for mode in ("undeclared", "declared", "false_claim"):
                d = tmpdir()
                try:
                    spec = (lambda t: {"content": [text(t)]}) if prov == "anthropic" else (lambda t: {"message": {"role": "assistant", "content": t}})
                    rig = Rig(d, prov, [spec("a1"), spec("a2")])
                    rig.msgs.append({"role": "user", "content": word})
                    o = rig.call(rig.request())
                    if mode == "undeclared":
                        rig.call(rig.request())
                    elif mode == "declared":
                        rig.tap.call(rig.request(), retry_of=1)
                    else:
                        rig.msgs.append({"role": "assistant", "content": o["content"]} if prov == "anthropic" else o["choices"][0]["message"])
                        rig.msgs.append({"role": "user", "content": "other %d" % seed})
                        rig.tap.call(rig.request(), retry_of=1)
                    rig.sync()
                    ev = rig.events(include_port=True)
                    users = [e for e in ev if e["prov"] == "user"]
                    comp = rig.ing.completeness(rig.stream_id)
                    row[mode] = {"user_events": len(users), "assistant_events": sum(1 for e in ev if e["prov"] == "assistant"),
                                 "flagged_user_events": sum(1 for e in users if json.loads(e["meta"]).get("ambiguous_repeat")),
                                 "port_kinds": sorted(e["kind"] for e in ev if e["prov"] == "port"), "ambiguous_calls": comp["ambiguous_calls"],
                                 "declared_calls": comp["declared_calls"], "exactly_once": rig.ing.claim_exactly_once(rig.stream_id),
                                 "resp_events_marked_declared": sum(1 for e in ev if e["side"] == "resp" and json.loads(e["meta"]).get("declared"))}
                finally:
                    shutil.rmtree(d, ignore_errors=True)
            runs.append(row)
    a = sum(1 for r in runs if r["undeclared"]["user_events"] == 2 and r["undeclared"]["flagged_user_events"] == 1 and r["undeclared"]["ambiguous_calls"] == [2] and not r["undeclared"]["exactly_once"])
    b = sum(1 for r in runs if r["declared"]["user_events"] == 1 and r["declared"]["assistant_events"] == 2 and "declared_retry" in r["declared"]["port_kinds"]
            and r["declared"]["ambiguous_calls"] == [] and r["declared"]["declared_calls"] == [2] and r["declared"]["exactly_once"]
            and r["declared"]["resp_events_marked_declared"] >= 1)
    c = sum(1 for r in runs if r["false_claim"]["user_events"] == 2 and "retry_claim_ignored" in r["false_claim"]["port_kinds"] and "declared_retry" not in r["false_claim"]["port_kinds"])
    S = {"runs": len(runs), "undeclared_flagged_duplicate": a, "declared_suppressed_visibly": b, "false_claim_ignored": c}
    S["artifact"] = write_artifact(out, "item13b", {"providers": 2, "seeds": "101-110"}, runs, S)
    return S
