"""Focused check (runs on any machine, no stores needed): wp/config.py fails closed.

NEG-1 a process whose WP_HOME is unset, or names a home without this stream's conf marker, refuses to start before
      it could open any connection; NEG-2 a conf that names another stream's container (the previous lab's
      emlab-es-wp-16096) is refused. Also: missing store address refused, non-loopback Elasticsearch refused, a
      correct conf accepted with its own values.
Each case imports the config module in a fresh interpreter. usage: python3 tests/test_config_guard.py [config.py]
(default: this tree's wp/config.py; pass the previous commit's file to show that it fails these cases)."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-writepath", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-writepath-pg", "es_container": "emlab2-writepath-es"}


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


def main(cfg_src):
    bad_es = dict(GOOD, es_container="emlab-es-wp-16096")
    remote_es = dict(GOOD, es_url="http://10.0.0.5:19460")
    no_port = {k: v for k, v in GOOD.items() if k != "pg_port"}
    wrong_stream = dict(GOOD, stream="emlab-writepath")
    cases = [
        ("NEG-1 WP_HOME unset", None, False, "refuse"),
        ("NEG-1 home without stream marker", {k: v for k, v in GOOD.items() if k != "stream"}, True, "refuse"),
        ("NEG-1 wrong stream marker", wrong_stream, True, "refuse"),
        ("NEG-2 previous lab's ES container", bad_es, True, "refuse"),
        ("missing pg_port", no_port, True, "refuse"),
        ("non-loopback es_url", remote_es, True, "refuse"),
        ("correct conf", GOOD, True, "load"),
    ]
    fails = 0
    for name, conf, set_home, want in cases:
        rc, out = run_case(cfg_src, conf, set_home)
        # a refusal counts only when the config itself refused (its message), never an import or other error
        got = "load" if (rc == 0 and out.startswith("LOADED")) else "refuse" if (rc != 0 and "refusing to start" in out) else "error"
        ok = got == want and (want == "refuse" or "LOADED 25660 http://127.0.0.1:19460 emlab2-writepath-es" in out)
        fails += not ok
        print(("PASS" if ok else "FAIL"), name, "->", got, "|", out.splitlines()[-1][:120] if out else "")
    print("config_guard", "PASS" if fails == 0 else f"FAIL ({fails})")
    return fails


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1] if len(sys.argv) > 1 else HERE / "wp" / "config.py") else 0)
