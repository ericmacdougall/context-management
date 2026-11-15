"""Static guards on the write-path package (runs without services):
 S1 only wp/lease.py mentions the leases table (a path that re-read the epoch before writing would defeat the fence);
 S2 only wp/es.py sends /_bulk, and it always sets require_alias=true;
 S3 no module under wp/ writes documents with PUT/POST /<index>/_doc or _update (the alias-only write path is the only one);
 S4 every family in wp.families.FAMILIES is registered in wp.registry.STORES;
 S5 every Postgres table created by schema.sql is registered.
Control: each check is also run on a mutated copy of the source that violates it and must fail there."""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent / "wp"


def checks(src: dict):
    out = {}
    out["S1"] = sorted(n for n, s in src.items() if re.search(r"(from|update|join|into)\s+leases\b", s) and n not in ("lease.py", "schema.sql"))
    out["S2"] = sorted(n for n, s in src.items() if "/_bulk" in s and n != "es.py") + ([] if "require_alias=True" in src.get("es.py", "") else ["es.py:no require_alias"])
    out["S3"] = sorted(n for n, s in src.items() if n.endswith(".py") and re.search(r"/_doc|/_update|/_create", s))
    reg = src.get("registry.py", "")
    fams = re.findall(r'^    "([a-z0-9_]+)": \{"mappings"', src.get("families.py", ""), re.M)
    out["S4"] = [f for f in fams if f'"es", "{f}"' not in reg]
    tables = re.findall(r"^create table ([a-z_]+)", src.get("schema.sql", ""), re.M)
    out["S5"] = [t for t in tables if f'_pg("{t}"' not in reg]
    return out, fams, tables


def load():
    return {p.name: p.read_text() for p in ROOT.iterdir() if p.suffix in (".py", ".sql")}


if __name__ == "__main__":
    src = load()
    res, fams, tables = checks(src)
    ok = not any(res.values())
    muts = {
        "S1": ("projector.py", "\nQ = 'select epoch from leases where tenant_id=%s'\n"),
        "S2": ("reader.py", '\nX = "/_bulk"\n'),
        "S3": ("reader.py", '\nY = "/idx/_doc/1"\n'),
        "S4": ("families.py", '\n    "ghost": {"mappings": {}},\n'),
        "S5": ("schema.sql", "\ncreate table ghost_cache (x text);\n"),
    }
    ctl = {}
    for k, (fn, add) in muts.items():
        m = dict(src)
        m[fn] = m[fn] + add
        r, _, _ = checks(m)
        ctl[k] = bool(r[k])
    print({"checks": res, "families": fams, "tables": tables, "controls_fail": ctl})
    from exp.common import write_result
    write_result("static_check", {"checks": res, "families": fams, "tables": tables, "controls_fail": ctl, "pass": ok and all(ctl.values())})
    sys.exit(0 if ok and all(ctl.values()) else 1)
