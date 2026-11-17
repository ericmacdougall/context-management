"""Focused check (runs on any machine, no stores needed): wp/config.py fails closed.

NEG-1 a process whose WP_HOME is unset, or names a home without this stream's conf marker, refuses to start before
      it could open any connection; NEG-2 a conf that names another stream's container (the previous lab's
      emlab-es-wp-16096) is refused. Also: missing store address refused, non-loopback Elasticsearch refused, a
      correct conf accepted with its own values.
Each case imports the config module in a fresh interpreter. usage (from writepath/):
  python3 -m unittest discover -s tests -v             every offline test; this file contributes 7 cases
  python3 tests/test_config_guard.py [config.py]       the original report form
The config under test is this tree's wp/config.py, or the file named in CONFIG_UNDER_TEST (or the script argument):
pass the previous commit's file to show that it fails these cases."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}


def run_case(cfg_src, home_conf, set_home=True):
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "pkg" / "wp").mkdir(parents=True)
        (d / "pkg" / "wp" / "__init__.py").write_text("")
        shutil.copy(cfg_src, d / "pkg" / "wp" / "config.py")
        home = d / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in (home_conf or {}).items():
            (home / "conf" / k).write_text(v + "\n")
        env = {k: v for k, v in os.environ.items() if k not in ("WP_HOME", "WP_SECRETS_DIR")}
        env["HOME"] = str(d / "fakeuser")             # a prior default under ~ must not exist to be used
        if set_home:
            env["WP_HOME"] = str(home)
        code = ("import sys; sys.path.insert(0, %r); import wp.config as c; "
                "print('LOADED', c.PG_PORT, c.ES_URL, getattr(c, 'ES_CONTAINER', None))" % str(d / "pkg"))
        r = subprocess.run([sys.executable, "-I", "-c", code], cwd=d / "pkg", env=env, capture_output=True, text=True,
                           timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()


CASES = [
    ("NEG-1 WP_HOME unset", None, False, "refuse"),
    ("NEG-1 home without stream marker", {k: v for k, v in GOOD.items() if k != "stream"}, True, "refuse"),
    ("NEG-1 wrong stream marker", dict(GOOD, stream="emlab-writepath"), True, "refuse"),
    ("NEG-2 previous lab's ES container", dict(GOOD, es_container="emlab-es-wp-16096"), True, "refuse"),
    # hardening stream (goodqa#16096 item 4): the step-1 owner's home and containers are another stream's
    ("NEG-3 step-1 stream marker", dict(GOOD, stream="emlab2-writepath"), True, "refuse"),
    ("NEG-3 step-1 stream's ES container", dict(GOOD, es_container="emlab2-writepath-es"), True, "refuse"),
    ("NEG-3 complete step-1 conf", dict(GOOD, stream="emlab2-writepath", pg_container="emlab2-writepath-pg",
                                        es_container="emlab2-writepath-es"), True, "refuse"),
    ("missing pg_port", {k: v for k, v in GOOD.items() if k != "pg_port"}, True, "refuse"),
    ("non-loopback es_url", dict(GOOD, es_url="http://203.0.113.5:19460"), True, "refuse"),   # RFC 5737 address
    ("correct conf", GOOD, True, "load"),
]


def check(cfg_src, conf, set_home, want):
    """Returns (ok, got, last output line)."""
    rc, out = run_case(cfg_src, conf, set_home)
    # a refusal counts only when the config itself refused (its message), never an import or other error
    got = "load" if (rc == 0 and out.startswith("LOADED")) else "refuse" if (rc != 0 and "refusing to start" in out) else "error"
    ok = got == want and (want == "refuse" or "LOADED 25660 http://127.0.0.1:19460 emlab2-wphard-es" in out)
    return ok, got, out.splitlines()[-1][:120] if out else ""


def config_under_test():
    return os.environ.get("CONFIG_UNDER_TEST") or str(HERE / "wp" / "config.py")


class ConfigFailsClosed(unittest.TestCase):
    """One test per case, so the runner reports a real count."""


def _make(case):
    name, conf, set_home, want = case

    def test(self):
        ok, got, last = check(config_under_test(), conf, set_home, want)
        self.assertTrue(ok, f"{name}: want {want}, got {got} | {last}")
    test.__doc__ = name
    return test


for _i, _case in enumerate(CASES, 1):
    setattr(ConfigFailsClosed, f"test_{_i}_" + "".join(c if c.isalnum() else "_" for c in _case[0].lower()).strip("_"),
            _make(_case))


def main(cfg_src):
    fails = 0
    for name, conf, set_home, want in CASES:
        ok, got, last = check(cfg_src, conf, set_home, want)
        fails += not ok
        print(("PASS" if ok else "FAIL"), name, "->", got, "|", last)
    print("config_guard", "PASS" if fails == 0 else f"FAIL ({fails})")
    return fails


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1] if len(sys.argv) > 1 else config_under_test()) else 0)
