"""Read-only precheck before the live session (prints role names and counts only, never a value): would the gate's
reset (wp.setup --reset, _ensure_roles) write a credential? It writes one for a login role whose credential file is
missing, or whose cluster role has no password. Either case means the run would change the owner's credentials: stop
and report instead of running.
usage: cd <tree>/writepath && . ~/emlab2-16096/gatefix/env.sh && "$PY" -I -B <this file>"""
import os
import pathlib
import sys

sys.path.insert(0, os.getcwd())
from wp import config, db  # noqa: E402
from wp.setup import LOGIN_ROLES  # noqa: E402

missing = sorted(r for r in LOGIN_ROLES if not pathlib.Path(config.pgpass(r)).exists())
admin = db.admin(db="postgres")
rows = admin.execute("select rolname, rolpassword is null from pg_authid where rolname = any(%s)",
                     (list(LOGIN_ROLES),)).fetchall()
no_pw = sorted(r for r, n in rows if n)
absent = sorted(set(LOGIN_ROLES) - {r for r, _ in rows})
print(f"login roles {len(LOGIN_ROLES)}; credential file missing: {missing or 'none'}; cluster role without a password: "
      f"{no_pw or 'none'}; cluster role absent: {absent or 'none'}")
print("PRECHECK", "CLEAN" if not (missing or no_pw or absent) else "WOULD WRITE CREDENTIALS")
