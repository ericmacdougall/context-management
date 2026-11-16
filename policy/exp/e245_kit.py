"""Shared harness pieces for E2, E4, E5 (experiment code; never imported by pol).

- multi_variant: like exp.patches.variant but applies several (old, new) fragments to ONE production module, each asserted to match
  exactly once. Only the id-pattern-guard control of E2 needs two fragments (the guard must see texts, which only Store._sources has,
  and the decision point must be replaced in Enforcer._decide); every other control is a single-fragment exp.patches.variant.
- Net: recording stubs for every outbound channel (model, embedder, inbox, export). A payload records the channel, destination,
  the destination ATTRIBUTES the oracle needs, the tenant and the text. No model call of any kind is made.
- bypass(action): the control 'enforcement disabled for one action', as a one-fragment patch of pol/enforce.py Enforcer._decide."""
import importlib
import os
import sys
import types

from exp.patches import ROOT, variant

DECIDE_LINE = "    d = _evaluate(p, action, labels, request, pol, float(enf._clock()), rcp)"


def multi_variant(module_name, pairs, tag):
    path = os.path.join(ROOT, *module_name.split(".")) + ".py"
    src = open(path, encoding="utf-8").read()
    for old, new in pairs:
        n = src.count(old)
        if n != 1:
            raise AssertionError("patch %s: expected exactly one match in %s, found %d" % (tag, module_name, n))
        src = src.replace(old, new)
    importlib.import_module(module_name)
    name = module_name + "__ctl_" + tag
    mod = types.ModuleType(name)
    mod.__file__ = path + "#" + tag
    mod.__package__ = module_name.rsplit(".", 1)[0]
    sys.modules[name] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


def bypass(action):
    return variant("pol.enforce", DECIDE_LINE,
                   DECIDE_LINE + " if action != %r else _Decision(True, 'ctl_bypass', ver, action)" % action, "bypass_" + action)


def derive_first():
    """Derived records labelled with the FIRST input's label only (the derivation join dropped)."""
    return variant("pol.enforce", "        label = join(r.label for r in src)\n", "        label = src[0].label\n", "derive_first")


def id_pattern_guard():
    """A canary-blind guard replacing the decision point: it refuses only text that looks like a real session id."""
    return multi_variant("pol.enforce", [
        ("            recs.append(r)\n        return recs",
         "            if __import__('re').search(r'answer_[0-9a-f]{4,}|sharegpt_\\w+', r.text):\n"
         "                self._enf._deny('id_pattern', action, self._pid(session), self._enf._ver())\n"
         "            recs.append(r)\n        return recs"),
        (DECIDE_LINE, "    d = _Decision(True, 'id_guard_only', ver, action)"),
    ], "id_pattern_guard")


def read_without_decision():
    """Store.read returns the record without the per-reference read decision (expansion without a decision)."""
    return variant("pol.enforce", "            self._enf._check(session, \"read\", [r.label], {})\n", "            pass\n", "read_without_decision")


def enqueue_only():
    """Outbox delivery trusts the enqueue-time decision: no decision at delivery (labels and recipient not re-checked)."""
    old = "                d, sender, rcp = self._enf._redeem(ticket, [r.label for r in recs])\n"
    new = ("                d = _Decision(True, 'enqueue_only', ticket.enqueue_version, self._action)\n"
           "                sender, rcp = self._enf._dir.lookup(ticket.sender), (self._enf._dir.lookup(ticket.recipient_id) if ticket.recipient_id else None)\n")
    return variant("pol.enforce", old, new, "enqueue_only")


class Payload:
    __slots__ = ("channel", "dest", "attrs", "tenant", "text")

    def __init__(self, channel, dest, attrs, tenant, text):
        self.channel, self.dest, self.attrs, self.tenant, self.text = channel, dest, attrs, tenant, text


class Net:
    def __init__(self):
        self.payloads = []
        self.registered = {}        # channel -> action

    def register(self, channel, action):
        self.registered[channel] = action

    def record(self, channel, dest, attrs, tenant, text):
        self.payloads.append(Payload(channel, dest, attrs, tenant, text))


class _Answer:
    def __init__(self, text, model):
        self.text, self.model = text, model


class StubChat:
    def __init__(self, net, dest, model, attrs, tenant):
        self.net, self.dest, self.model, self.attrs, self.tenant = net, dest, model, attrs, tenant

    def complete(self, msgs, system=None):
        text = "\n".join(m["content"] for m in msgs) + ("\n" + system if system else "")
        self.net.record("model", self.dest, self.attrs, self.tenant, text)
        return _Answer("answer: " + text, self.model)


class StubEmbedder:
    def __init__(self, net, dest, attrs, tenant):
        self.net, self.dest, self.attrs, self.tenant = net, dest, attrs, tenant

    def embed(self, texts, kind):
        for t in texts:
            self.net.record("embed", self.dest, self.attrs, self.tenant, t)
        return [[0.0] for _ in texts]
