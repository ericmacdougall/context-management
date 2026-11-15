"""ChatModel on the user's Claude subscription via headless Claude Code.

Invocation: claude -p --model <alias> --tools "" --strict-mcp-config --system-prompt <s> --no-session-persistence
            --output-format json [--json-schema <schema>]
Run from a neutral NON-git directory (the user's Stop hook otherwise injects text and extra turns into answers).
Never --bare, never changes setting sources, never disables hooks, never reads an API key.
Each call costs about 10.5k tokens of harness overhead and leaves a session log directory on this machine.
Native structured output: `--json-schema` returns the validated object in `structured_output`.
Not supported by the CLI (declared False, ignored unless strict_params): temperature, max output tokens.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

from ..chat_base import BackendOut, CachedChatModel, run_child
from ..ports import BackendError, Capabilities, Usage

DEFAULT_SYSTEM = "You are a concise, accurate assistant."


class ClaudeCliChat(CachedChatModel):
    adapter_id = "claude-cli"

    def __init__(self, cwd: str, model: str = "sonnet", extra_args: Optional[List[str]] = None, claude_bin: str = "claude", **kw):
        if os.path.isdir(os.path.join(cwd, ".git")):
            raise ValueError("cwd must be a neutral non-git directory")
        self.extra_args, self.claude_bin = list(extra_args or []), claude_bin
        if self.extra_args:
            kw["recipe"] = kw.get("recipe", "v1") + "+" + ",".join(self.extra_args)
        super().__init__(model=model, cwd=cwd, **kw)
        self.capabilities = Capabilities(adapter=self.adapter_id, supports_json_schema=True, json_schema_mode="native",
                                         supports_system=True, reports_usage=True, max_context_tokens=200_000,
                                         supports_temperature=False, supports_max_output_tokens=False, streaming="none")

    def command(self, system: Optional[str], json_schema: Optional[dict]) -> List[str]:
        cmd = [self.claude_bin, "-p", "--model", self.model, "--tools", "", "--strict-mcp-config",
               "--system-prompt", system if system is not None else DEFAULT_SYSTEM,
               "--no-session-persistence", "--output-format", "json"]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema)]
        return cmd + self.extra_args

    def _call_backend(self, prompt, system, json_schema, timeout_s) -> BackendOut:
        p = run_child(self.command(system, json_schema), prompt, timeout_s, self.cwd)
        if p.returncode != 0:
            raise BackendError("claude exited %d: %s" % (p.returncode, p.stderr[-300:]))
        try:
            d = json.loads(p.stdout)
        except ValueError:
            raise BackendError("claude printed non-JSON output: %r" % p.stdout[:200])
        if d.get("is_error"):
            raise BackendError("claude reported is_error: %s" % str(d.get("result"))[:300])
        u = d.get("usage")
        usage = None
        if isinstance(u, dict):
            cr, cc = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
            usage = Usage(input_tokens=(u.get("input_tokens") or 0) + cr + cc, cached_input_tokens=cr,
                          output_tokens=u.get("output_tokens"))
        parsed = d.get("structured_output")
        text = d.get("result") or ""
        if not text and parsed is not None:
            text = json.dumps(parsed)
        return BackendOut(text=text, parsed=parsed, usage=usage,
                          meta={"cost_usd": d.get("total_cost_usd"), "num_turns": d.get("num_turns")})
