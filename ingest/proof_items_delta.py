"""Proof runs added for review 2 (PREREG-amend-3 A3): hosts that repeat content, and the retry-identity probe. Stubs only."""
from __future__ import annotations

import json
import shutil
from typing import List

from .proofs_common import event_tuples, repeated_conversation, tmpdir, write_artifact
from .stubs import text
from .testkit import Rig

PATTERNS = ("same", "alt", "tri")
WINDOWS = (4, 6, 8)


def item13(out: str) -> dict:
    runs = []
    cells = [(prov, pat, w) for prov in ("anthropic", "openai") for pat in PATTERNS for w in WINDOWS]
    cells += [(prov, "unique", w) for prov in ("anthropic", "openai") for w in (4, 8)]
    for prov, pat, w in cells:
        d = tmpdir()
        try:
            rig, led = repeated_conversation(Rig, d, prov, pat, w, 20)
            rig.sync()
            ev = rig.events()
            comp = rig.ing.completeness(rig.stream_id)
            exp, got = led.expected(), event_tuples(ev)
            runs.append({"provider": prov, "pattern": pat, "window_msgs": w, "calls": 20,
                         "user_events": sum(1 for e in ev if e["prov"] == "user"), "assistant_events": sum(1 for e in ev if e["prov"] == "assistant"),
                         "oracle_match": got == exp, "expected_events": len(exp), "got_events": len(got),
                         "flagged_request_events": sum(1 for e in ev if json.loads(e["meta"]).get("ambiguous_repeat")),
                         "ambiguous_calls": comp["ambiguous_calls"], "state": comp["state"],
                         "blocking_gaps": [g["kind"] for g in comp["blocking_gaps"]]})
        finally:
            shutil.rmtree(d, ignore_errors=True)
    rep = [r for r in runs if r["pattern"] != "unique"]
    uni = [r for r in runs if r["pattern"] == "unique"]
    ok = [r for r in rep if r["user_events"] == 20 and r["assistant_events"] == 20 and r["oracle_match"] and not r["blocking_gaps"]]
    S = {"repeated_runs": len(rep), "repeated_pass": len(ok), "repeated_runs_with_ambiguity_flag": sum(1 for r in rep if r["ambiguous_calls"]),
         "unique_runs": len(uni), "unique_runs_with_ambiguity_flag": sum(1 for r in uni if r["ambiguous_calls"]),
         "unique_oracle_match": sum(r["oracle_match"] for r in uni),
         "failing_first_0801866_user_events_for_20_calls": "same/4: 3; alt/4: 4 (anthropic); see results3/failing-first.txt"}
    S["artifact"] = write_artifact(out, "item13", {"patterns": PATTERNS, "windows": WINDOWS, "calls": 20, "providers": 2, "unique_control_windows": [4, 8]}, runs, S)
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
                                 "port_kinds": sorted(e["kind"] for e in ev if e["prov"] == "port"), "ambiguous_calls": comp["ambiguous_calls"]}
                finally:
                    shutil.rmtree(d, ignore_errors=True)
            runs.append(row)
    a = sum(1 for r in runs if r["undeclared"]["user_events"] == 2 and r["undeclared"]["flagged_user_events"] == 1 and r["undeclared"]["ambiguous_calls"] == [2])
    b = sum(1 for r in runs if r["declared"]["user_events"] == 1 and r["declared"]["assistant_events"] == 2 and "declared_retry" in r["declared"]["port_kinds"]
            and r["declared"]["ambiguous_calls"] == [])
    c = sum(1 for r in runs if r["false_claim"]["user_events"] == 2 and "retry_claim_ignored" in r["false_claim"]["port_kinds"] and "declared_retry" not in r["false_claim"]["port_kinds"])
    S = {"runs": len(runs), "undeclared_flagged_duplicate": a, "declared_suppressed_visibly": b, "false_claim_ignored": c}
    S["artifact"] = write_artifact(out, "item13b", {"providers": 2, "seeds": "101-110"}, runs, S)
    return S
