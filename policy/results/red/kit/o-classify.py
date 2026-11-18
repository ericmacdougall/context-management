"""Classify a unittest -v log: every FAIL/ERROR block (subtests included) with its exception type and message.
usage: python3 classify.py <log>   (prints a table and the totals; an ERROR is never failing-first evidence)"""
import collections
import re
import sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
blocks = re.split(r"\n={50,}\n", text)
rows = []
for b in blocks[1:]:
    head, _, rest = b.partition("\n")
    m = re.match(r"(FAIL|ERROR): (\S+) \(([^)]+)\)(.*)", head)
    if not m:
        continue
    kind, name, cls, sub = m.groups()
    tb = rest.split("\n-{50,}\n")[0] if False else rest
    exc = None
    for line in reversed(tb.splitlines()):
        mm = re.match(r"^([A-Za-z_][\w.]*(Error|Exception|Warning|Exit|Failure|Interrupt|errors\.\w+))(:.*)?$", line.strip())
        if mm:
            exc = line.strip()
            break
    rows.append((kind, cls.split(".")[-1] + "." + name, sub.strip(), exc or "?"))
tot = collections.Counter((k, (e or "?").split(":")[0]) for k, _, _, e in rows)
summary = re.findall(r"^(Ran \d+ tests? in [\d.]+s)\s*\n\s*\n(OK.*|FAILED.*)$", text, re.M)
for k, t, s, e in rows:
    print("%-5s %s %s | %s" % (k, t, s[:90], e[:220]))
print("TOTALS", dict(tot), summary)
vac = [r for r in rows if "vacuous" in r[3] or "did not wait" in r[3]]
print("VACUOUS/LIVENESS-CONTROL entries:", len(vac))
