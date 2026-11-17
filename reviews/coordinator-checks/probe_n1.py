"""Main's own probe of the delta reviewer's N1 case (no dependence on the author's test module)."""
import json, os, sys, tempfile
from ingest.stubs import text
from ingest.testkit import Rig
d = tempfile.mkdtemp(prefix="probe-n1-")
os.makedirs(os.path.join(d, "r"))
rig = Rig(os.path.join(d, "r"), "anthropic", [{"content": [text("ok")]} for _ in range(2)])
rig.msgs.append({"role": "user", "content": "yes"})
o = rig.call(rig.request())
rig.msgs.append({"role": "assistant", "content": o["content"]})
rig.msgs[:] = [{"role": "user", "content": "yes"}, {"role": "user", "content": "new"}]
rig.call(rig.request())
rig.sync()
users = sorted(json.loads(e["content"])["text"] for e in rig.events() if e["prov"] == "user")
st = rig.stream_id
blocking = [g["kind"] for g in rig.ing.gaps(st) if g.get("blocking")]
once = getattr(rig.ing, "claim_exactly_once", None)
print("user events:", users, "| blocking gaps:", blocking, "| claim_exactly_once:", once(st) if once else "n/a")
print("VERDICT:", "LOSES A MESSAGE SILENTLY" if (len(users) < 3 and not blocking) else ("nothing lost, flagged and blocked" if len(users) == 3 and blocking and once and not once(st) else "other"))
