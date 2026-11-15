"""Terms guard for the subscription-backed adapters (claude-cli, codex-cli, claude stream-json session, ACP).

They are DEVELOPMENT-ONLY: one developer, one seat, the unmodified CLIs on this machine, loopback only, no credential is ever
read, stored or forwarded. Until the vendors' terms question is answered (see NOTES.md) every use is an ASSUMPTION and must be
switched on explicitly: config flag dev_subscription_backends=True. The default configuration uses ElasticInferenceChat or a
first-party API-key adapter.
"""
from __future__ import annotations

from .ports import PortError


class DevSubscriptionNotEnabled(PortError):
    pass


def require_dev_subscription(flag, what: str) -> None:
    if flag is not True:
        raise DevSubscriptionNotEnabled(
            "%s drives a subscription CLI and is development-only; pass dev_subscription_backends=True explicitly "
            "(single developer, own machine, loopback only). Default adapters: ElasticInferenceChat or an API-key adapter." % what)
