"""Kibana Agent Builder pre-execution workflow: the documented template and the fail-open prompt builder behind it.

NOT RUN LIVE. Template, rendering and unit tests only; no Kibana, container or model was started for this file.

Observed on Kibana 9.5.6 (NOTES-addendum-g7.md): a pre-execution workflow's `new_prompt` REPLACES the user message, and a
failing step or a run past the 120 s hook limit stops the agent run with HTTP 500 (the original prompt is not used as a
fallback). The variant recorded in tests/fixtures/kibana956/pre_exec_variants.json put a raw `<memory>...</memory>` block
into the prompt. That format is SUPERSEDED: a stored text containing `</memory>` and `SYSTEM:` lines forges the framing
(review finding 3). Do not reuse it.

Design (two layers of fail-open):
1. memcore side, pre_exec_new_prompt(): builds the block with the SAME injector and renderer as the prompt hook
   (BudgetedInjector -> inbox.render_data_block: per-turn random delimiter, one JSON-escaped record per line, BEGIN/END
   lines, egress guard), within a time budget well under 120 s (DEFAULT_BUDGET_S). On any error, egress refusal,
   oversize block or timeout it returns the ORIGINAL prompt unchanged with a reason; it never raises.
   The HTTP route that would serve it (POST <memcore>/v1/pre_exec) is NOT BUILT yet; workflow_yaml() names it.
2. Kibana side, workflow_yaml(): one http step with a step timeout under the budget and continue-on-failure, then a
   data.set step whose new_prompt falls back to inputs.prompt when the http step produced nothing. The workflow never
   assembles the framing itself (no Liquid string building of memory text): it only passes through new_prompt.
   ASSUMPTION (unverified on 9.5.6): the step keys `timeout`, `on-failure: {continue: true}` and the Liquid `default`
   filter. Verify by running the three variants (ok, refused port, 140 s wait) through converse before relying on it.
"""
from __future__ import annotations

import threading
from typing import Optional

from ..memtools import BudgetedInjector
from .prompt_hook import DEFAULT_MAX, build_output

KIBANA_HOOK_LIMIT_S = 120.0
DEFAULT_BUDGET_S = 20.0          # memcore-side budget; the http step timeout sits a little above it, both far below 120 s
STEP_TIMEOUT_S = 25


def compose_prompt(block: str, prompt: str) -> str:
    """The new_prompt Kibana sends to the model: the framed DATA block, a blank line, then the user's prompt verbatim."""
    return (block + "\n\n" + prompt) if block else prompt


def pre_exec_new_prompt(injector: BudgetedInjector, payload: dict, now_iso: str, budget_s: float = DEFAULT_BUDGET_S,
                        max_chars: int = DEFAULT_MAX) -> dict:
    """{"new_prompt", "injected", "reason"}. Fail-open: the original prompt on error, refusal, empty result or timeout."""
    prompt = payload.get("prompt")
    prompt = prompt if isinstance(prompt, str) else ""
    box: dict = {}

    def work():
        try:
            box["out"] = build_output(injector, {"prompt": prompt, "session_id": payload.get("session_id", "")}, now_iso, max_chars)
        except Exception as e:  # noqa: BLE001 - fail open; only the type is reported (no content)
            box["err"] = type(e).__name__

    t = threading.Thread(target=work, daemon=True)   # a stuck build is abandoned, never awaited past the budget
    t.start()
    t.join(budget_s)
    if t.is_alive():
        return {"new_prompt": prompt, "injected": False, "reason": "timeout"}
    if "err" in box:
        return {"new_prompt": prompt, "injected": False, "reason": "error:" + box["err"]}
    out = box.get("out")
    if not out:
        return {"new_prompt": prompt, "injected": False, "reason": "empty"}
    return {"new_prompt": compose_prompt(out["hookSpecificOutput"]["additionalContext"], prompt), "injected": True, "reason": "ok"}


def workflow_yaml(memcore_url: str, token_const: str = "memcore_pre_exec_token", name: str = "memcore-pre-exec",
                  step_timeout_s: int = STEP_TIMEOUT_S) -> str:
    """The documented pre-execution workflow. token_const names a workflow constant holding the bearer token; the token
    itself never appears in the template. See the module docstring for the ASSUMPTION keys."""
    if not 0 < step_timeout_s < KIBANA_HOOK_LIMIT_S / 2:
        raise ValueError("step timeout must be well under Kibana's 120 s hook limit")
    if not memcore_url.startswith(("http://", "https://")) or any(c in memcore_url for c in "\n\"'{} "):
        raise ValueError("memcore_url must be a plain http(s) URL")
    return "\n".join([
        "name: %s" % name,
        "enabled: true",
        "triggers:",
        "  - type: manual",
        "    inputs:",
        "      - name: prompt",
        "        type: string",
        "steps:",
        "  - name: memory",
        "    type: http",
        "    timeout: %ds                 # ASSUMPTION key; memcore answers within %ds" % (step_timeout_s, int(DEFAULT_BUDGET_S)),
        "    on-failure:",
        "      continue: true             # ASSUMPTION key; a failure must not stop the agent run",
        "    with:",
        "      url: %s/v1/pre_exec" % memcore_url.rstrip("/"),
        "      method: POST",
        "      headers:",
        "        Authorization: \"Bearer {{ consts.%s }}\"" % token_const,
        "      body:",
        "        prompt: \"{{ inputs.prompt }}\"",
        "  - name: result",
        "    type: data.set",
        "    with:",
        "      # memcore returns the complete new_prompt (framed block + original prompt) or the original prompt itself;",
        "      # the workflow never builds memory text. Empty/failed http step -> the user's prompt unchanged.",
        "      new_prompt: \"{{ steps.memory.output.data.new_prompt | default: inputs.prompt }}\"",
        "",
    ])
