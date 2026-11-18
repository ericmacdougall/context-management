"""Static guards on the write-path package (runs without services):
 S1 only wp/lease.py mentions the leases table (a path that re-read the epoch before writing would defeat the fence);
 S2 only wp/es.py sends /_bulk, and it always sets require_alias=true;
 S3 no module under wp/ writes documents with PUT/POST /<index>/_doc or _update (the alias-only write path is the only one);
 S4 every family in wp.families.FAMILIES is registered in wp.registry.STORES;
 S5 every Postgres table created by schema.sql is registered;
 S6 no module under wp/ uses _reindex, _delete_by_query or _update_by_query (writes that bypass the alias fence);
 S7 (review J M6) schema.sql and setup.py grant nothing on wp_seal (any case, alter default privileges included) and
    grant the role wp_owner to nobody.
S5 (review J M5) matches create [global|local] [temp|temporary|unlogged] table [if not exists] with any case and quoted
names; a table created by dynamic SQL inside a DO block or function is NOT seen here (the runtime gate,
wp/registry.py unregistered(), covers relkinds r, p, v, m, f).
Control: each check is also run on a mutated copy of the source that violates it and must fail there."""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent / "wp"
_NAME = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
TABLE_RX = re.compile(rf"^\s*create\s+(?:(?:global|local)\s+)?(?:(?:temporary|temp|unlogged)\s+)?table\s+"
                      rf"(?:if\s+not\s+exists\s+)?({_NAME}(?:\.{_NAME})?)", re.I | re.M)
PRIV_RX = (re.compile(r"\bgrant\b[^;]*?\bwp_seal\b", re.I | re.S),                     # a privilege on wp_seal (not wp_sealer)
           re.compile(r"\balter\s+default\s+privileges\b[^;]*?\bwp_seal\b", re.I | re.S),
           re.compile(r"\bgrant\s+wp_owner\s+to\b", re.I))


def checks(src: dict):
    out = {}
    out["S1"] = sorted(n for n, s in src.items() if re.search(r"(from|update|join|into)\s+leases\b", s) and n not in ("lease.py", "schema.sql"))
    out["S2"] = sorted(n for n, s in src.items() if "/_bulk" in s and n != "es.py") + ([] if "require_alias=True" in src.get("es.py", "") else ["es.py:no require_alias"])
    out["S3"] = sorted(n for n, s in src.items() if n.endswith(".py") and re.search(r"/_doc|/_update|/_create", s))
    reg = src.get("registry.py", "")
    fams = re.findall(r'^    "([a-z0-9_]+)": \{"mappings"', src.get("families.py", ""), re.M)
    out["S4"] = [f for f in fams if f'"es", "{f}"' not in reg]
    tables = [t.replace('"', "").lower() for t in TABLE_RX.findall(src.get("schema.sql", ""))]
    out["S5"] = [t for t in tables if f'_pg("{t}"' not in reg]
    out["S6"] = sorted(n for n, s in src.items() if n.endswith(".py") and re.search(r"_reindex|_delete_by_query|_update_by_query", s))
    out["S7"] = sorted(f"{n}: {m.group(0)[:60]!r}" for n in ("schema.sql", "setup.py") for rx in PRIV_RX
                       for m in rx.finditer(src.get(n, "")))
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
        "S6": ("erasure.py", '\nZ = "/idx/_delete_by_query"\n'),
        "S5b": ("schema.sql", "\ncreate unlogged table ghost_u (x text);\n"),
        "S5c": ("schema.sql", "\nCREATE TABLE IF NOT EXISTS Ghost_Up (x text);\n"),
        "S5d": ("schema.sql", '\n  create temp table "ghost_q" (x text);\n'),
        "S7": ("schema.sql", "\nGRANT SELECT ON wp_seal.seal_keys TO wp_app;\n"),
        "S7b": ("schema.sql", "\nalter default privileges in schema wp_seal grant select on tables to wp_app;\n"),
        "S7c": ("setup.py", "\nX = 'grant wp_owner to wp_app'\n"),
    }
    ctl = {}
    for k, (fn, add) in muts.items():
        m = dict(src)
        m[fn] = m[fn] + add
        r, _, _ = checks(m)
        ctl[k] = bool(r[k[:2]])
    print({"checks": res, "families": fams, "tables": tables, "controls_fail": ctl})
    from exp.common import write_result
    write_result("static_check", {"checks": res, "families": fams, "tables": tables, "controls_fail": ctl, "pass": ok and all(ctl.values())})
    sys.exit(0 if ok and all(ctl.values()) else 1)
