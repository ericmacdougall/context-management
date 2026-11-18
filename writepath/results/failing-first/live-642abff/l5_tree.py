"""Live item GF16096-L5 driver, sent over stdin to t2-dev: run gate v6's own tree check (exp.disk_gate._tree, the P4
check admission and end use) on a pinned tree, with a stub wp.config (the real one would name the write-path owner's
stores; no store is contacted and no store tool runs). argv: <pinned tree>. Prints the commit prefix and every reason.
The stub module is itself expected to be reported as loaded from outside the tree."""
import os
import pathlib
import sys
import types

tree = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(tree))
stub = types.ModuleType("wp.config")
stub.INDEX_PREFIX, stub.EVENTS_DB, stub.KEYS_DB = "wp16096-", "events", "keys"
sys.modules["wp.config"] = stub
import exp.disk_gate as g                      # noqa: E402

print("gate", g.VERSION, "root", g.ROOT.name, "flags isolated", bool(sys.flags.isolated), "nobytecode",
      sys.dont_write_bytecode)
info, why = g._tree()
print("commit", (info or {}).get("commit", "")[:12], "| reasons", len(why))
for w in why:
    print("  reason:", w)
