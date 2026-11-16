"""Basic-licence host integration: the client's own agent runtime calls memcore's POST /v1/pre_exec directly (no Agent
Builder, no Elasticsearch inference API) and sends the returned new_prompt to its own model.

Fail-open, client side (mirrors the Kibana workflow's step timeout + continue + default): fetch_new_prompt() never
raises; on connection refused, a time-out (CLIENT_TIMEOUT_S, above memcore's 20 s route budget and far below any
host limit), a non-200 answer, a malformed body or a wrong type it returns the ORIGINAL prompt with a reason.

Use from Python:   new = fetch_new_prompt(url, token, prompt)["new_prompt"]; model.complete([{"role": "user", "content": new}])
             or:   run_turn(model, url, token, prompt)   (model: any memcore ChatModel, e.g. OpenAICompatChat)
Use from any runtime (plain HTTP hook): python3 -m memcore.hosts.basic_hook --url URL --token-file F --real-ids F < prompt
   prints {"new_prompt", "injected", "reason"} as one JSON line; exit status is always 0.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

CLIENT_TIMEOUT_S = 25.0
MAX_REPLY = 1_000_000


def _open(req, timeout):
    """Plain urllib opener that does NOT follow redirects (a redirect must not resend the bearer token elsewhere)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    return urllib.request.build_opener(NoRedirect).open(req, timeout=timeout)


def fetch_new_prompt(url: str, token: str, prompt: str, session_id: str = "", timeout_s: float = CLIENT_TIMEOUT_S) -> dict:
    """{"new_prompt", "injected", "reason", "seconds"}. Never raises; the original prompt on any failure."""
    t0 = time.monotonic()

    def orig(reason):
        return {"new_prompt": prompt, "injected": False, "reason": reason, "seconds": round(time.monotonic() - t0, 2)}
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    body = json.dumps({"prompt": prompt, "session_id": session_id}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/pre_exec", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    deadline = t0 + timeout_s
    try:
        with _open(req, timeout_s) as r:
            if r.status != 200:
                return orig("http_%d" % r.status)
            raw = r.read(MAX_REPLY + 1)
        if time.monotonic() > deadline:          # a slow trickle past the budget counts as a time-out
            return orig("timeout")
        if len(raw) > MAX_REPLY:
            return orig("reply_too_large")
        out = json.loads(raw)
    except urllib.error.HTTPError as e:
        return orig("http_%d" % e.code)
    except (socket.timeout, TimeoutError):
        return orig("timeout")
    except urllib.error.URLError as e:
        return orig("timeout" if isinstance(e.reason, (socket.timeout, TimeoutError)) else "unreachable")
    except (ValueError, RecursionError, OSError) as e:
        return orig("error:" + type(e).__name__)
    np = out.get("new_prompt") if isinstance(out, dict) else None
    if not isinstance(np, str) or not np.endswith(prompt):      # memcore always ends new_prompt with the prompt verbatim
        return orig("malformed")
    return {"new_prompt": np, "injected": out.get("injected") is True, "reason": str(out.get("reason", "")),
            "seconds": round(time.monotonic() - t0, 2)}


def run_turn(model, url: str, token: str, prompt: str, session_id: str = "", timeout_s: float = CLIENT_TIMEOUT_S, **complete_kw):
    """One agent turn on the Basic path: memory hook (fail-open), then the client's model through memcore's ChatModel port."""
    hook = fetch_new_prompt(url, token, prompt, session_id, timeout_s)
    res = model.complete([{"role": "user", "content": hook["new_prompt"]}], **complete_kw)
    return hook, res


def main(argv=None, stdin=sys.stdin, stdout=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="memcore base URL (the gateway serving /v1/pre_exec)")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--timeout", type=float, default=CLIENT_TIMEOUT_S)
    ap.add_argument("--session-id", default="")
    ap.add_argument("--real-ids", default=None, help="REQUIRED (or $MEMCORE_REAL_IDS_FILE); without it the prompt is "
                    "passed through unchanged (fail closed for memory, open for the user)")
    a = ap.parse_args(argv)
    prompt = stdin.read()
    out: Optional[dict] = None
    try:
        from ..guard import DEFAULT_GUARD, require_real_ids
        require_real_ids(DEFAULT_GUARD, a.real_ids, "basic_hook")
        with open(a.token_file) as fh:
            token = fh.read().strip()
        out = fetch_new_prompt(a.url, token, prompt, a.session_id, a.timeout)
        if out["injected"]:
            DEFAULT_GUARD.check_text(out["new_prompt"], "basic_hook new_prompt")   # client-side backstop
    except Exception as e:  # noqa: BLE001 - the hook never blocks the user's prompt
        out = {"new_prompt": prompt, "injected": False, "reason": "error:" + type(e).__name__}
    stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
