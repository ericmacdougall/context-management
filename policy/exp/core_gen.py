"""Random decision-core event streams for the B-experiments (stdlib only; imports nothing from pol).

The generator is deliberately sloppy: it reuses ids and idempotency keys, binds to stale and current versions, picks
real and bogus proposal ids and hashes, uses the wrong channel, forges tenants and sends malformed events, so that every
rule and refusal code is reached. Labels stay inside the domain both implementations accept (non-empty strings,
sensitivity 0..3)."""
import hashlib
import json
import random

TENANT = "t1"
REQS = ("r1", "r2")
PRINCIPALS = {"h1": "human", "h2": "human", "h3": "human", "a1": "agent", "a2": "agent", "x1": "analyst"}


def config():
    return {"tenant": TENANT, "principals": dict(PRINCIPALS),
            "authority": {"r1": ["h1", "h2", "a1"], "r2": ["h1", "h3", "a1", "a2"]},
            "proposers": {"r1": ["x1", "a2"], "r2": ["x1"]},
            "admins": ["h1"]}


def label(rng):
    return {"tenants": [TENANT], "acl": rng.choice([[], [["g1"]], [["g1", "g2"]], [["g2"]]]), "regions": rng.choice([None, ["eu"], ["eu", "us"]]),
            "sensitivity": rng.randint(0, 3), "models": rng.choice([None, ["m1"]]), "banned": rng.choice([[], ["m2"]]),
            "local_only": rng.random() < 0.2, "restricted": rng.random() < 0.1, "holds": rng.choice([[], ["hold-1"]]),
            "subjects": rng.choice([[], ["s1"], ["s1", "s2"]])}


def phash(pid, ref, author):
    return hashlib.sha256(json.dumps([pid, ref, author], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Gen:
    def __init__(self, seed, principals=None):
        self.rng = random.Random(seed)
        self.principals = dict(principals or PRINCIPALS)
        self.n = 0
        self.items = []          # (req, id, value_ref, author, kind) of every propose/remark generated
        self.events = []         # (req, id, principal) of every generated event, for choose
        self.versions = {r: [0] for r in REQS}

    def note_version(self, req, v):
        if v not in self.versions[req]:
            self.versions[req].append(v)

    def event(self):
        r = self.rng
        self.n += 1
        p = r.choice(list(self.principals) + ["zz"] * (r.random() < 0.02))
        human = self.principals.get(p) == "human"
        ch = ("owner_ui" if r.random() < 0.5 else "owner_cli") if human else "agent_api"
        if r.random() < 0.05:
            ch = r.choice(["owner_ui", "owner_cli", "agent_api"])
        req = r.choice(REQS + ("r9",) * (r.random() < 0.02))
        vs = self.versions.get(req, [0])
        based = vs[-1] if r.random() < 0.6 else r.choice(vs + [vs[-1] + 1])
        t = r.choices(["propose", "remark", "confirm", "decide", "supersede", "retract", "revoke", "grant"], [14, 8, 8, 30, 8, 4, 1, 2])[0]
        eid = "e%d" % (self.n if r.random() > 0.03 else max(1, self.n - r.randint(1, 5)))
        idem = "k%d" % (self.n if r.random() > 0.04 else max(1, self.n - r.randint(1, 5)))
        ev = {"type": t, "id": eid, "idem": idem, "tenant": TENANT if r.random() > 0.01 else "t2", "principal": p, "channel": ch}
        if t in ("propose", "remark", "supersede"):
            ev.update(req=req, value_ref="v%d" % r.randint(1, 6), label=label(r), based_on=based)
        elif t == "confirm":
            pend = [x for x in self.items if x[4] == "pending" and x[0] == req]
            ev.update(req=req, pending=r.choice(pend)[1] if pend and r.random() < 0.9 else "e999999", based_on=based)
        elif t == "retract":
            ev.update(req=req, based_on=based)
        elif t == "revoke":
            ev.update(target=r.choice(list(self.principals)))
        elif t == "grant":
            ev.update(req=req, target=r.choice(list(self.principals)), role=r.choice(["decide", "propose"]))
        else:
            verb = r.choices(["accept", "reject", "amend", "resolve", "bogus"], [10, 6, 3, 4, 0.3])[0]
            ev.update(req=req, verb=verb, based_on=based)
            if verb in ("accept", "reject", "amend"):
                props = [x for x in self.items if x[4] == "proposal" and x[0] == req]
                if props and r.random() < 0.93:
                    x = r.choice(props[-12:] if r.random() < 0.8 else props)
                    ev["proposal"] = x[1]
                    ev["proposal_hash"] = phash(x[1], x[2], x[3]) if r.random() < 0.95 else "0" * 64
                else:
                    ev["proposal"], ev["proposal_hash"] = "e999999", "0" * 64
                if verb == "amend":
                    ev.update(value_ref="v%d" % r.randint(1, 6), label=label(r))
            elif verb == "resolve":
                if r.random() < 0.6 and self.events:
                    ev["choose"] = r.choice(self.events[-10:])[1]
                else:
                    ev.update(value_ref="v%d" % r.randint(1, 6), label=label(r))
        if r.random() < 0.01:
            ev.pop(r.choice(sorted(ev)))
        if r.random() < 0.005:
            ev["based_on"] = -1
        if t in ("propose", "remark") and all(k in ev for k in ("req", "id", "value_ref")):
            self.items.append((ev["req"], ev["id"], ev["value_ref"], p, "proposal" if t == "propose" else "pending"))
        self.events.append((ev.get("req"), ev.get("id"), p))
        return ev

    def retry_or_new(self, history):
        """With some probability resend an earlier event verbatim (idempotent retry) or with one changed field (idem_conflict)."""
        r = self.rng
        if history and r.random() < 0.05:
            ev = json.loads(json.dumps(r.choice(history[-20:])))
            if r.random() < 0.3 and "value_ref" in ev:
                ev["value_ref"] = "v-changed"
            return ev
        return self.event()
