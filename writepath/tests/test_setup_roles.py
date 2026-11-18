"""wp.setup's login roles on a recreated cluster (offline: a fake admin connection, an isolated lab home).

  S-ROLE1  every login role the cluster does not have is created WITH a fresh password and its 0600 pgpass file is
           rewritten, even when a pgpass file from an earlier cluster exists (the stores are on tmpfs: a recreate keeps
           the files and empties the cluster). Live origin: 2026-11-18 23:45Z on t2-dev, after the tmpfs rebuild,
           `wp.setup --reset` then tests.test_d3_pg: password authentication failed for user "wp_app".
  S-ROLE2  a role the cluster has and whose file exists keeps its password (setup stays idempotent: no rotation)."""
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

if not os.environ.get("WP_HOME"):                 # an isolated lab home, as tests/test_sealer.py does
    _home = pathlib.Path(tempfile.mkdtemp()) / "home"
    (_home / "conf").mkdir(parents=True)
    for _k, _v in {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
                   "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}.items():
        (_home / "conf" / _k).write_text(_v + "\n")
    os.environ["WP_HOME"] = str(_home)

from wp import config, setup  # noqa: E402


class _PG:
    def encrypt_password(self, pw, user, alg):
        return b"SCRAM-SHA-256$4096:fake"


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeAdmin:
    def __init__(self, roles):
        self.roles, self.sql, self.pgconn = roles, [], _PG()

    def execute(self, q, *a):
        self.sql.append(repr(q))
        return _Cur([(r,) for r in self.roles] if "pg_roles" in repr(q) else [])

    def password_set(self, role):
        return any(f"Identifier('{role}')" in s and "password" in s for s in self.sql)


class SetupRoles(unittest.TestCase):
    def setUp(self):
        config.SECRETS.mkdir(parents=True, exist_ok=True)
        pathlib.Path(config.pgpass()).write_text(f"127.0.0.1:{config.PG_PORT}:*:wp:admin\n")
        for r in setup.LOGIN_ROLES:
            p = pathlib.Path(config.pgpass(r))
            p.write_text(f"127.0.0.1:{config.PG_PORT}:*:{r}:from-the-cluster-before\n")
            os.chmod(p, 0o600)

    def test_s_role1_new_cluster_gets_fresh_passwords(self):
        a = FakeAdmin(roles=["wp"])
        setup._ensure_roles(a)
        for r in setup.LOGIN_ROLES:
            self.assertTrue(a.password_set(r), f"{r} created without a password")
            p = pathlib.Path(config.pgpass(r))
            self.assertNotIn("from-the-cluster-before", p.read_text(), f"{r}'s file still holds the old cluster's line")
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_s_role2_existing_roles_keep_their_passwords(self):
        a = FakeAdmin(roles=["wp", "wp_owner", "wp_reviewer", *setup.LOGIN_ROLES])
        setup._ensure_roles(a)
        for r in setup.LOGIN_ROLES:
            self.assertFalse(a.password_set(r), f"{r}'s password was rotated")
            self.assertIn("from-the-cluster-before", pathlib.Path(config.pgpass(r)).read_text())


if __name__ == "__main__":
    unittest.main()
