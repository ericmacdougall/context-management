"""wp.setup's login roles on a recreated cluster (offline: a fake admin connection, an isolated lab home).

  S-ROLE1  every login role the cluster does not have is created WITH a fresh password and its 0600 pgpass file is
           rewritten, even when a pgpass file from an earlier cluster exists (the stores are on tmpfs: a recreate keeps
           the files and empties the cluster). Live origin: 2026-11-18 23:45Z on t2-dev, after the tmpfs rebuild,
           `wp.setup --reset` then tests.test_d3_pg: password authentication failed for user "wp_app".
  S-ROLE2  a role the cluster has WITH a password and whose file exists keeps it (setup stays idempotent: no rotation).
  S-ROLE3  a role the cluster has WITHOUT a password (left by a first setup run that keyed on "created now", live
           2026-11-18 23:49Z: all nine login roles had rolpassword null) gets one and its file is rewritten."""
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


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


class _PG:
    def encrypt_password(self, pw, user, alg):
        return b"SCRAM-SHA-256$4096:fake"


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeAdmin:
    """roles: those the cluster has; with_pw: those of them that have a password (pg_authid.rolpassword not null)."""
    def __init__(self, roles, with_pw=()):
        self.roles, self.with_pw, self.sql, self.pgconn = set(roles), set(with_pw), [], _PG()

    def execute(self, q, *a):
        s = repr(q)
        self.sql.append(s)
        if "pg_roles" in s:
            return _Cur([(r,) for r in sorted(self.roles)])
        if "pg_authid" in s:
            r = a[0][0]
            return _Cur([(r not in self.with_pw,)] if r in self.roles else [])
        role = next((r for r in setup.LOGIN_ROLES if f"Identifier('{r}')" in s), None)
        if role and "create role" in s:
            self.roles.add(role)
        if role and "password" in s:
            self.with_pw.add(role)
        return _Cur([])

    def password_set(self, role):
        return any(f"Identifier('{role}')" in s and "password" in s for s in self.sql)

    def transaction(self):
        import contextlib
        fa = self

        @contextlib.contextmanager
        def tx():
            fa.sql.append("BEGIN")
            yield
            fa.sql.append("COMMIT")
        return tx()


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

    def test_s_role3_existing_roles_without_password_get_one(self):
        # the cluster a first setup run left on 2026-11-18 (roles created, no passwords, old files kept)
        a = FakeAdmin(roles=["wp", "wp_owner", "wp_reviewer", *setup.LOGIN_ROLES])
        setup._ensure_roles(a)
        for r in setup.LOGIN_ROLES:
            self.assertTrue(a.password_set(r), f"{r} still has no password")
            self.assertNotIn("from-the-cluster-before", pathlib.Path(config.pgpass(r)).read_text())

    def test_s_role4_verifier_never_reaches_the_statement_log(self):
        # review 42cfd11 R9: the verifier is a literal in ALTER ROLE; each such statement runs in its own transaction
        # after SET LOCAL log_statement none, log_min_duration_statement -1 and log_min_error_statement panic
        a = FakeAdmin(roles=["wp"])
        setup._ensure_roles(a)
        pw = [i for i, s in enumerate(a.sql) if "password" in s and "alter role" in s]
        self.assertEqual(len(pw), len(setup.LOGIN_ROLES))
        for i in pw:
            begin = max((j for j in range(i) if a.sql[j] == "BEGIN"), default=None)
            self.assertIsNotNone(begin, "a password statement outside a transaction")
            self.assertFalse(any(a.sql[j] == "COMMIT" for j in range(begin, i)), "a password statement outside a transaction")
            tx = " ".join(a.sql[begin:i])
            for g, v in (("log_statement", "none"), ("log_min_duration_statement", "-1"), ("log_min_error_statement", "panic"),
                         ("log_min_duration_sample", "-1")):
                self.assertTrue(f"Identifier('{g}')" in tx and f"Literal('{v}')" in tx and "set local" in tx,
                                f"{g} not switched off before the verifier statement")
            # transaction sampling is decided when the transaction starts: switched off at session level before BEGIN
            self.assertIn("set log_transaction_sample_rate = 0", a.sql[begin - 1], "transaction sampling not off before BEGIN")

    def test_s_role5_seal_pads_install_without_statement_logging(self):
        # review 42cfd11 R7: a SUCCESSFUL install under log_min_duration_statement >= 0 would log the pads as bind
        # parameters; the install runs in one transaction that first switches statement and parameter logging off
        # (superuser SET LOCAL), then takes wp_owner for that transaction only
        import contextlib
        import io
        from wp import sealer
        td = tempfile.TemporaryDirectory()               # the throwaway key file is removed with it
        self.addCleanup(td.cleanup)
        old = os.environ.get("WP_SECRETS_DIR")
        os.environ["WP_SECRETS_DIR"] = td.name
        try:
            a = FakeAdmin(roles=["wp"])
            a.execute = lambda q, *x, _r=a.execute: (_r(q, *x), types_ns(fetchone=lambda: None))[1]
            with contextlib.redirect_stdout(io.StringIO()):   # _seal_key prints the key id: keep it out of receipts
                setup._seal_key(a)
        finally:
            if old is None:
                os.environ.pop("WP_SECRETS_DIR", None)
            else:
                os.environ["WP_SECRETS_DIR"] = old
        self.assertTrue(sealer.LINE_LIMIT)              # the authority module imported (the key file was made by it)
        ins =[i for i, s in enumerate(a.sql) if "wp_seal.install" in s]
        self.assertEqual(len(ins), 1, a.sql)
        i = ins[0]
        begin = max((j for j in range(i) if a.sql[j] == "BEGIN"), default=None)
        self.assertIsNotNone(begin, "the install runs outside a transaction")
        tx = a.sql[begin:i]
        self.assertFalse("COMMIT" in tx, "the install runs outside the quiet transaction")
        role = next((j for j, s in enumerate(tx) if "role wp_owner" in s), None)
        self.assertIsNotNone(role, "the install does not run as wp_owner")
        self.assertIn("set local role", tx[role], "wp_owner is taken beyond the install's transaction")
        self.assertIn("set log_transaction_sample_rate = 0", a.sql[begin - 1], "transaction sampling not off before BEGIN")
        for g, v in (("log_statement", "none"), ("log_min_duration_statement", "-1"), ("log_parameter_max_length", "0"),
                     ("log_parameter_max_length_on_error", "0"), ("log_min_error_statement", "panic"),
                     ("log_min_duration_sample", "-1")):
            at = next((j for j, s in enumerate(tx) if f"Identifier('{g}')" in s and f"Literal('{v}')" in s), None)
            self.assertIsNotNone(at, f"{g} is not switched off for the install")
            self.assertLess(at, role, f"{g} is set after the role change (superuser setting)")

    def test_s_role2_existing_roles_keep_their_passwords(self):
        a = FakeAdmin(roles=["wp", "wp_owner", "wp_reviewer", *setup.LOGIN_ROLES], with_pw=setup.LOGIN_ROLES)
        setup._ensure_roles(a)
        for r in setup.LOGIN_ROLES:
            self.assertFalse(a.password_set(r), f"{r}'s password was rotated")
            self.assertIn("from-the-cluster-before", pathlib.Path(config.pgpass(r)).read_text())


if __name__ == "__main__":
    unittest.main()
