"""Recording stubs for every outbound path. No network, no model. Whatever a stub records is what escaped to that endpoint.
The Network registry is the inventory: a channel is registered with the policy action that must guard it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class Payload:
    channel: str
    action: str
    dest: str
    tenant: str            # tenant of the principal that sent it
    text: str


class Network:
    def __init__(self):
        self.channels: Dict[str, str] = {}      # channel name -> action that must guard it
        self.payloads: List[Payload] = []
        self.current_tenant = "?"       # set by the driver to the tenant of the acting principal

    def register(self, channel: str, action: str):
        self.channels[channel] = action

    def record(self, channel: str, dest: str, tenant: str, text: str):
        self.payloads.append(Payload(channel, self.channels[channel], dest, tenant, text))


class _R:
    def __init__(self, text, model):
        self.text, self.model, self.parsed, self.usage = text, model, None, None


class StubChat:
    """Echo model: its answer contains its input, so markers propagate into derived (summary) records."""

    def __init__(self, net: Network, dest_id: str, model_id: str, reported_model: Optional[str] = None, channel="model"):
        self.net, self.dest_id, self.model, self.reported, self.channel = net, dest_id, model_id, reported_model, channel

    def complete(self, messages, system=None, **kw):
        text = (system or "") + "\n" + "\n".join(m["content"] for m in messages)
        self.net.record(self.channel, self.dest_id, self.net.current_tenant, text)
        return _R("SUMMARY[" + text[:6000] + "]", self.reported or self.model)


class StubEmbedder:
    def __init__(self, net: Network, dest_id: str, channel="embed"):
        self.net, self.dest_id, self.channel = net, dest_id, channel

    def embed(self, texts, kind="document"):
        for t in texts:
            self.net.record(self.channel, self.dest_id, self.net.current_tenant, t)
        return [[float(len(t))] for t in texts]
