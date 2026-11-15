"""ContextInjector host adapter for command hooks that fire on prompt submit: Claude Code (UserPromptSubmit) and Codex (UserPromptSubmit).
Both pass JSON on stdin (session_id, cwd, hook_event_name, prompt, ...) and accept {"hookSpecificOutput": {"hookEventName":
"UserPromptSubmit", "additionalContext": "..."}} on stdout with exit 0.

Caps: Claude Code truncates any hook output field over 10,000 characters (saves it to a file, shows a 2,000 char preview, and
does not ask the model to read it), so the default cap here is 9,000. Codex limits additionalContext to about 2,500 tokens by
default (override additionalContextLimit per handler); use --max-chars 6000 there.
Fail-safe: any error prints nothing and exits 0 so a broken memory never blocks a prompt (exit 2 would block it).
The injected text is the data block from inbox.render_block / BudgetedInjector: facts and ids, not instructions.
NOT installed anywhere by this repo; see hosts/examples for settings a user could add themselves.
Usage: python3 -m memcore.hosts.prompt_hook --store events.json [--max-chars 9000]
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from ..memtools import BudgetedInjector, InMemoryStore
from ..ports import Turn

DEFAULT_MAX = 9000


def load_store(path: str) -> InMemoryStore:
    s = InMemoryStore()
    for e in json.load(open(path)).get("events", []):
        s.add_event(e["id"], e.get("session", ""), e["ts"], e["text"], e.get("entities"))
    return s


def build_output(injector: BudgetedInjector, payload: dict, now_iso: str, max_chars: int = DEFAULT_MAX) -> Optional[dict]:
    turn = Turn(session_id=str(payload.get("session_id", "")), user_text=str(payload.get("prompt", "")), time=now_iso)
    inj = injector.build(turn)
    if not inj.block:
        return None
    block = inj.block
    if len(block) > max_chars:  # never rely on the host's truncation
        block = block[: max_chars - 40].rsplit("\n", 1)[0] + "\n</memory>"
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": block}}


def main(argv=None, stdin=sys.stdin, stdout=sys.stdout):
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX)
    ap.add_argument("--now", default=None, help="ISO time; default is the current UTC time")
    ap.add_argument("--real-ids", default=None, help="file of real dataset session ids (JSON list or one per line); required "
                    "once given: unreadable or empty means nothing is injected. Default: $MEMCORE_REAL_IDS_FILE")
    a = ap.parse_args(argv)
    try:
        import datetime
        if a.real_ids:
            from ..guard import DEFAULT_GUARD
            DEFAULT_GUARD.load(a.real_ids)        # in place: the injector and every adapter use this one object
        payload = json.loads(stdin.read() or "{}")
        now = a.now or datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        out = build_output(BudgetedInjector(load_store(a.store), char_budget=a.max_chars - 200), payload, now, a.max_chars)
        if out:
            stdout.write(json.dumps(out))
    except Exception as e:  # noqa: BLE001 - fail safe: never block the prompt
        print("memcore prompt_hook error: %s" % type(e).__name__, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
