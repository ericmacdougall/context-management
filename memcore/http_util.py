"""HTTP opener that NEVER follows redirects: urllib would resend the Authorization header to the redirect target. A 3xx is
surfaced as an HTTPError (adapters turn it into BackendError).

Openers are cached, one per TLS context. Building an opener per call made urllib build a default TLS context for every
HTTPS request, and creating a default context loads the system CA store (about 21 ms of GIL-held CPU per request,
which capped one process near 45 requests per second; licence/DEGRADE.md). A request with no explicit context now uses
ONE process-wide default context (verifying, system CA store), created on first use: a change of SSL_CERT_FILE after that
first HTTPS request is not seen until the process restarts. A caller-supplied context (CA file, or the named verify_tls=False
opt-out) is used as given and never shared with another context."""
from __future__ import annotations

import ssl
import threading
import urllib.request

_MAX_CACHED = 32            # a process builds a handful of contexts (one per adapter instance); a runaway caller cannot grow this


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_lock = threading.Lock()
_default_ctx = None
_openers = {}               # id(context) -> (context, opener); the stored context keeps its id from being reused


def default_ssl_context() -> ssl.SSLContext:
    """The one verifying context used when a caller passes none (system CA store, loaded once)."""
    global _default_ctx
    with _lock:
        if _default_ctx is None:
            _default_ctx = ssl.create_default_context()
        return _default_ctx


def _opener_for(context):
    ctx = context if context is not None else default_ssl_context()
    with _lock:
        hit = _openers.get(id(ctx))
        if hit is not None and hit[0] is ctx:
            return hit[1]
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))
    with _lock:
        if len(_openers) >= _MAX_CACHED:
            _openers.pop(next(iter(_openers)))           # oldest first
        _openers[id(ctx)] = (ctx, opener)
    return opener


def urlopen(req, timeout=None, context=None):
    return _opener_for(context).open(req, timeout=timeout)
