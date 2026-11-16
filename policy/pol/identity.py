"""Identity adapter INTERFACE (R3-1). No implementation ships with this package.

A session is issued only from a credential that an identity adapter has VERIFIED. The adapter returns an Assertion naming the
principal and the channel the credential was bound to when it was enrolled (the device or transport it belongs to: an owner UI
device, an owner CLI key, an agent's API key). The channel is therefore a property of the credential, never an argument a caller
chooses, so `same_channel_confirmation` means "confirmed from a second enrolled device or key".

In a deployment the adapter is the write path's authentication (lease and session binding, or the client's IdP). The lab's test
double lives in tests/kit.py (LabIdP) and is never imported by `pol`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AuthenticationFailed(Exception):
    pass


@dataclass(frozen=True)
class Assertion:
    principal_id: str
    channel: str
    factor: str            # the enrolled device or key the credential belongs to (audited; not used for decisions)


class IdentityAdapter(Protocol):
    def authenticate(self, credential) -> Assertion:
        """Verify `credential` (signature, expiry, single use, revocation at the IdP) and return its Assertion, or raise."""
        ...
