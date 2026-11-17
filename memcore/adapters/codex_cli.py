"""ChatModel on the user's ChatGPT/Codex subscription, ONLY through the harness entry point
`python3 ~/.claude/hooks/codex_pool.py run --purpose build ... -- exec ...` (the pool resolves the local codex binary; t2-dev is
only its key-fetch path). This adapter never touches ~/.codex or any key.

Spend guard: the pool is subscription-first but falls through to capped paid API keys inside the same invocation if the
subscription reports a usage limit. We cannot prevent that inside the pool, so before every call we require
`status` to show the subscription available and today's USD spend equal to the baseline taken at construction; after
every call we re-check, and a "falling back" line on stderr or any spend raises PaidFallback and stops all further calls.
Codex is an agent, not a bare model: it has no system-prompt parameter (system text is prepended to the prompt), runs in a
read-only sandbox from a neutral directory, and reports ~21k input tokens of overhead per call.
Native structured output: `exec --output-schema FILE` (needs all properties required + additionalProperties:false).
Not supported: temperature, max output tokens. Serial only (concurrency 1).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import List, Optional

from ..chat_base import BackendOut, CachedChatModel, run_child
from ..devgate import require_dev_subscription
from ..ports import BackendError, Capabilities, PortError, Usage

POOL = os.path.expanduser("~/.claude/hooks/codex_pool.py")
DEFAULT_MODEL = "gpt-6.1-terra"  # the pool policy's probe model; confirmed to run on the subscription


class PaidFallback(PortError):
    """The pool spent, or may have spent, paid API-key budget; all further calls are refused."""


def _status() -> dict:
    p = run_child([sys.executable, POOL, "status"], "", 60, None)
    m = re.search(r"total today \$([0-9.]+)", p.stdout)
    return {"sub_available": "subscription: available" in p.stdout, "today": float(m.group(1)) if m else None,
            "blocked": "blocked" in p.stdout.split("\n")[0]}


class CodexCliChat(CachedChatModel):
    adapter_id = "codex-cli"
    subscription_backed = True

    def __init__(self, cwd: str, dev_subscription_backends: bool = False, schema_dir: Optional[str] = None, model: Optional[str] = None, effort: str = "low", **kw):
        require_dev_subscription(dev_subscription_backends, "CodexCliChat")
        kw["concurrency"] = 1
        super().__init__(model=model or DEFAULT_MODEL, cwd=cwd, **kw)
        self.effort = effort
        self.schema_dir = schema_dir or os.path.join(os.path.dirname(os.path.abspath(self.cache_dir)), "codex-schemas")
        os.makedirs(self.schema_dir, exist_ok=True)
        st = _status()
        if st["today"] is None or not st["sub_available"] or st["blocked"]:
            raise PaidFallback("codex pool status does not show an available, unblocked subscription: %r" % (st,))
        self._baseline, self._tripped = st["today"], False
        self.capabilities = Capabilities(adapter=self.adapter_id, supports_json_schema=True, json_schema_mode="native",
                                         supports_system=True, reports_usage=True, max_context_tokens=None,
                                         supports_temperature=False, supports_max_output_tokens=False, streaming="none")

    def settings_fingerprint(self):
        return {"effort": self.effort, "sandbox": "read-only"}

    def _guard(self, when: str, stderr: str = "") -> None:
        if "falling back to API keys" in stderr:
            self._tripped = True
        st = _status()
        if st["today"] is None or st["today"] != self._baseline or not st["sub_available"] or st["blocked"]:
            self._tripped = True
        if self._tripped:
            raise PaidFallback("stopped %s: pool status/stderr indicates paid fallback or an unavailable subscription" % when)

    def _call_backend(self, prompt, system, json_schema, timeout_s, params=None) -> BackendOut:
        if self._tripped:
            raise PaidFallback("a previous call tripped the spend guard")
        self._guard("before call")
        cmd = [sys.executable, POOL, "run", "--purpose", "build", "--model", self.model, "--effort", self.effort,
               "--max-seconds", str(int(max(30, timeout_s))), "--", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "-s", "read-only"]
        if json_schema is not None:
            h = hashlib.sha256(json.dumps(json_schema, sort_keys=True).encode()).hexdigest()[:16]
            sp = os.path.join(self.schema_dir, h + ".json")
            with open(sp, "w") as fh:
                json.dump(json_schema, fh)
            cmd += ["--output-schema", sp]
        full = prompt if system is None else "%s\n\n%s" % (system, prompt)
        p = run_child(cmd, full, timeout_s, self.cwd)
        self._guard("after call", p.stderr)
        text, usage, err = None, None, None
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                text = ev["item"].get("text")
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                usage = Usage(input_tokens=u.get("input_tokens"), cached_input_tokens=u.get("cached_input_tokens"),
                              output_tokens=u.get("output_tokens"))
            elif t in ("error", "turn.failed"):
                err = json.dumps(ev)[:300]
        if p.returncode != 0 or text is None:
            raise BackendError("codex run failed (exit %d): %s %s" % (p.returncode, err or "", p.stderr[-200:]))
        return BackendOut(text=text, usage=usage, meta={"effort": self.effort})
