import random
from ingest.scan import redact_text, ReferenceScanner
from ingest.canon import Hasher
random.seed(7)
body = "\n".join("".join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") for _ in range(64)) for _ in range(6))
blk = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + body + "\n-----END " + "RSA PRIVATE KEY-----"
out = redact_text("note before\n" + blk + "\nnote after", ReferenceScanner(), Hasher(b"k"))
out = out[0] if isinstance(out, tuple) else out
surv = max((len(l) for l in body.split("\n") if l in out), default=0)
print("body lines surviving verbatim:", sum(1 for l in body.split("\n") if l in out), "of 6 | any 8-char fragment survives:", any(body[i:i+8] in out for i in range(0, len(body)-8) if "\n" not in body[i:i+8]))
