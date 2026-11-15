"""HTTP opener that NEVER follows redirects: urllib would resend the Authorization header to the redirect target. A 3xx is
surfaced as an HTTPError (adapters turn it into BackendError)."""
from __future__ import annotations

import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(req, timeout=None, context=None):
    handlers = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)
