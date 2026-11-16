"""Basic-licence host integration: the client's own agent runtime calls memcore's POST /v1/pre_exec directly (no Agent
Builder, no Elasticsearch inference API) and sends the returned new_prompt to its own model.

Fail-open, client side (mirrors the Kibana workflow's step timeout + continue + default): fetch_new_prompt() returns
the ORIGINAL prompt, with a reason, on connection refused, a non-200 answer, an oversized or malformed body, a reply
that is not exactly memcore's framing, an egress-guard refusal, or when the WHOLE exchange (connect, request, headers,
body) has not finished within timeout_s (CLIENT_TIMEOUT_S = 25 s, above memcore's 20 s route budget). The deadline is
wall-clock for the whole call, not per socket read: a peer trickling bytes cannot hold the turn (review R3-1).

Accepted replies (review R3-2): new_prompt equal to the prompt, or exactly one memcore DATA block as render_data_block
renders it (MEMCORE-<16 hex> BEGIN/END pair, the fixed header whose turn is a short id @ a UTC timestamp, one JSON object
{id, ts, text} per line, at most MAX_BLOCK_CHARS in all), then a blank
line, then the prompt byte for byte. Anything else, e.g. an instruction prepended by a rogue or on-path server, is
refused (injected false, original prompt). The bearer token travels with the request, so plain http is refused for a
non-loopback service unless allow_plain_http is set explicitly (prefer https or a loopback/bridge-only service).

Use from Python:   new = fetch_new_prompt(url, token, prompt)["new_prompt"]; model.complete([{"role": "user", "content": new}])
             or:   run_turn(model, url, token, prompt)   (model: any memcore ChatModel, e.g. OpenAICompatChat)
Use from any runtime (plain HTTP hook): python3 -m memcore.hosts.basic_hook --url URL --token-file F --real-ids F < prompt
   prints {"new_prompt", "injected", "reason", "seconds"} as one JSON line; exit status is always 0.
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import socket
import sys
import threading
import time
import urllib.parse
from typing import Optional

CLIENT_TIMEOUT_S = 25.0
MAX_REPLY = 1_000_000
BLOCK_LABEL = "remembered events"            # what memtools.BudgetedInjector renders
BLOCK_FIELDS = ("id", "ts", "text")
MAX_BLOCK_CHARS = 16000                      # well above the route's default 4000 (basic_service) / 9000 (prompt_hook)
_REASON = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")


def _loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _exchange(parts, body: bytes, token: str, deadline: float, box: dict, holder: dict) -> None:
    """One POST over http.client (never follows redirects). Runs in a daemon thread; the caller enforces the deadline.
    If the caller has given up by the time the connection exists (slow DNS or connect), nothing is sent: the request,
    and the bearer token with it, never leaves after the deadline (review R4-1). Every socket operation is bounded by
    the time remaining, so an abandoned thread cannot outlive the deadline by more than one read."""
    conn = None

    def left():
        return max(0.05, deadline - time.monotonic())
    try:
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = cls(parts.hostname, parts.port, timeout=left())
        conn.connect()
        with holder["lock"]:
            if holder.get("abandoned") or time.monotonic() >= deadline:
                box["reason"] = "timeout"
                return
            holder["conn"] = conn
        conn.sock.settimeout(left())
        conn.request("POST", parts.path.rstrip("/") + "/v1/pre_exec", body=body,
                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        conn.sock.settimeout(left())
        r = conn.getresponse()
        box["status"] = r.status
        if r.status == 200:
            chunks, n = [], 0
            while n <= MAX_REPLY:
                if time.monotonic() >= deadline:
                    raise TimeoutError("deadline")
                if conn.sock is not None:
                    conn.sock.settimeout(left())
                c = r.read(min(65536, MAX_REPLY + 1 - n))
                if not c:
                    break
                chunks.append(c)
                n += len(c)
            box["raw"] = b"".join(chunks)
    except (socket.timeout, TimeoutError):
        box["reason"] = "timeout"
    except (ConnectionError, socket.gaierror):
        box["reason"] = "unreachable"
    except Exception as e:  # noqa: BLE001 - reported by type only
        box["reason"] = "error:" + type(e).__name__
    finally:
        if conn is not None:
            conn.close()


def _abort(holder: dict) -> None:
    """Mark the exchange abandoned (a connection that appears later sends nothing) and shut down a socket that already
    exists, so a trickling peer cannot keep the thread alive."""
    with holder["lock"]:
        holder["abandoned"] = True
        conn = holder.get("conn")
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def fetch_new_prompt(url: str, token: str, prompt: str, session_id: str = "", timeout_s: float = CLIENT_TIMEOUT_S,
                     allow_plain_http: bool = False, egress_guard=None, max_block_chars: int = MAX_BLOCK_CHARS) -> dict:
    """{"new_prompt", "injected", "reason", "seconds"}. For a str prompt it never raises and returns within timeout_s
    (plus thread start-up): the original prompt on any failure (a non-str prompt is a caller bug: TypeError). The
    injected text goes through the egress guard (default: guard.DEFAULT_GUARD) before it is returned."""
    t0 = time.monotonic()

    def orig(reason):
        return {"new_prompt": prompt, "injected": False, "reason": reason, "seconds": round(time.monotonic() - t0, 2)}
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return orig("bad_url")
    if parts.scheme == "http" and not _loopback(parts.hostname) and not allow_plain_http:
        return orig("plain_http_refused")          # the bearer token would cross the network in clear
    body = json.dumps({"prompt": prompt, "session_id": session_id}).encode()
    box, holder = {}, {"lock": threading.Lock()}
    th = threading.Thread(target=_exchange, args=(parts, body, token, t0 + timeout_s, box, holder), daemon=True,
                          name="memcore-basic-hook")
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        _abort(holder)
        return orig("timeout")
    if "reason" in box:
        return orig(box["reason"])
    if box.get("status") != 200:
        return orig("http_%s" % box.get("status"))
    raw = box.get("raw", b"")
    if len(raw) > MAX_REPLY:
        return orig("reply_too_large")
    try:
        out = json.loads(raw)
    except (ValueError, RecursionError):
        return orig("malformed")
    np = out.get("new_prompt") if isinstance(out, dict) else None
    if not isinstance(np, str):
        return orig("malformed")
    reason = out.get("reason")
    reason = reason if isinstance(reason, str) and _REASON.match(reason) else "unspecified"
    if np == prompt:
        return orig(reason)
    if not np.endswith("\n\n" + prompt):
        return orig("malformed")
    from ..inbox import MEMORY_TURN_RE, parse_data_block
    if not parse_data_block(np[: len(np) - len(prompt) - 2], BLOCK_LABEL, BLOCK_FIELDS, MEMORY_TURN_RE, max_block_chars):
        return orig("malformed")
    try:
        from ..guard import DEFAULT_GUARD
        (egress_guard or DEFAULT_GUARD).check_text(np, "basic_hook new_prompt")     # client-side backstop
    except Exception as e:  # noqa: BLE001 - EgressLeak or a guard without its id file: fail closed for memory
        return orig("error:" + type(e).__name__)
    return {"new_prompt": np, "injected": True, "reason": "ok", "seconds": round(time.monotonic() - t0, 2)}


def run_turn(model, url: str, token: str, prompt: str, session_id: str = "", timeout_s: float = CLIENT_TIMEOUT_S,
             allow_plain_http: bool = False, egress_guard=None, **complete_kw):
    """One agent turn on the Basic path: memory hook (fail-open, guarded), then the client's model through memcore's
    ChatModel port."""
    hook = fetch_new_prompt(url, token, prompt, session_id, timeout_s, allow_plain_http, egress_guard)
    res = model.complete([{"role": "user", "content": hook["new_prompt"]}], **complete_kw)
    return hook, res


def main(argv=None, stdin=sys.stdin, stdout=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="memcore base URL (the gateway serving /v1/pre_exec)")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--timeout", type=float, default=CLIENT_TIMEOUT_S, help="wall-clock deadline for the whole call")
    ap.add_argument("--session-id", default="")
    ap.add_argument("--max-block-chars", type=int, default=MAX_BLOCK_CHARS, help="longest memory block accepted")
    ap.add_argument("--allow-plain-http", action="store_true", help="allow http:// to a non-loopback host (the bearer "
                    "token then crosses the network in clear)")
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
        out = fetch_new_prompt(a.url, token, prompt, a.session_id, a.timeout, a.allow_plain_http, DEFAULT_GUARD,
                               a.max_block_chars)
    except Exception as e:  # noqa: BLE001 - the hook never blocks the user's prompt
        out = {"new_prompt": prompt, "injected": False, "reason": "error:" + type(e).__name__}
    stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
