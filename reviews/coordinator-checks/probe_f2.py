"""Main's probe of gate finding F2 (undeclared request, entirely new user messages after history). argv[1]=provider."""
import json, os, sys, tempfile
from ingest.stubs import text
from ingest.testkit import Rig
prov = sys.argv[1]
spec = (lambda t: {"content": [text(t)]}) if prov == "anthropic" else (lambda t: {"message": {"role": "assistant", "content": t}})
def case(name, second):
    d = tempfile.mkdtemp(prefix="probe-f2-"); os.makedirs(os.path.join(d, "r"))
    rig = Rig(os.path.join(d, "r"), prov, [spec("old answer"), spec("new answer")])
    rig.msgs.append({"role": "user", "content": "old question"})
    o = rig.call(rig.request())
    rig.msgs.append({"role": "assistant", "content": o["content"] if prov == "anthropic" else "old answer"})
    rig.msgs[:] = second(list(rig.msgs))
    rig.call(rig.request()); rig.sync()
    st = rig.stream_id
    ev = [(e["prov"], json.loads(e["content"]).get("text")) for e in rig.events() if e["side"] != "resp"]
    host = [t for p, t in ev if p != "user" and t in ("first new question", "second new question", "third new question")]
    users = [t for p, t in ev if p == "user"]
    blocking = [g["kind"] for g in rig.ing.gaps(st) if g.get("blocking")]
    once = rig.ing.claim_exactly_once(st); comp = rig.ing.claim_complete(st, rig.log.head(st)[0])
    bad = bool(host) and (not blocking or once or comp)
    print("%-9s %-30s user=%d as_host=%d blocking=%s once=%s complete=%s -> %s" % (prov, name, len(users), len(host), blocking, once, comp, "SILENT PROVENANCE LOSS" if bad else ("blocked, nothing relabelled" if not host and blocking and not once and not comp else ("clean" if not host and not blocking else "CHECK"))))
N = lambda *t: [{"role": "user", "content": x} for x in t]
case("only new turn, two users", lambda m: N("first new question", "second new question"))
case("only new turn, three users", lambda m: N("first new question", "second new question", "third new question"))
case("full history + two new users", lambda m: m + N("first new question", "second new question"))
case("full history + one new user", lambda m: m + N("first new question"))
