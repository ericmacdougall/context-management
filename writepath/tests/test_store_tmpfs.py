"""The stores' storage: the stamped-run gate's precondition (runkit/GATE-DESIGN.md 5.6, R1, R2, B3, B4 on
fix/writepath-gate at a293050), offline. Docker is faked: the test records the `docker run` argv that host/bootstrap.py
and wp/es_security.py would execute and checks it.

  T-STO1  bootstrap creates each store with its data directory on a private tmpfs (--tmpfs at the data path, no volume
          or bind there), nr_inodes=0 (B4), memory-swap equal to memory (R2: no swap), and size S with S - M >= MARGIN
          for PG and S - M >= the 10 GiB low watermark + MARGIN for ES (R2).
  T-STO2  PG's PGDATA is the tmpfs path (R1) and the node settings (amendment 17c, the earlier lab's parity) are on the
          server's command line, so no restart (which empties the tmpfs) is needed to apply them.
  T-STO3  ES keeps the H6d watermarks (10gb/9gb/8gb).
  T-STO4  wp.es_security's recreation (D6 turns security on) keeps the tmpfs with its options and the swap limit, and
          maps no tmpfs to a volume."""
import os
import pathlib
import importlib.util
import socket
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

from wp import es_security  # noqa: E402

GIB = 1024 ** 3
MARGIN = 256 * 1024 ** 2
ES_LOW = 10 * GIB
PG_DATA, ES_DATA = "/var/lib/postgresql/data", "/usr/share/elasticsearch/data"


def _bytes(v):
    v = str(v).lower()
    for suf, mul in (("g", GIB), ("m", 1024 ** 2), ("k", 1024)):
        if v.endswith(suf):
            return int(float(v[:-1]) * mul)
    return int(v)


def _opt(args, *names):
    out = []
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                out.append(args[i + 1])
            elif a.startswith(n + "="):
                out.append(a.split("=", 1)[1])
    return out


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Stop(Exception):
    pass


class _R:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _bootstrap_runs():
    """host/bootstrap.py's main() with docker faked: no container exists; returns the two `docker run` argvs."""
    spec = importlib.util.spec_from_file_location("wp_bootstrap_under_test", HERE / "host" / "bootstrap.py")
    B = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(B)
    runs = []

    def fake(cmd, *a, **k):
        if cmd[:2] == ["docker", "inspect"]:
            return _R(1)
        if cmd[:2] == ["docker", "run"]:
            runs.append(list(cmd))
            if len(runs) == 2:
                raise _Stop
            return _R(0, "id\n")
        raise AssertionError(f"unexpected command before both stores were created: {cmd[:3]}")

    home = pathlib.Path(tempfile.mkdtemp()) / "emlab2-16096" / "wphard" / "wp"
    old_env, old_run = os.environ.get("WP_HOME"), B.subprocess.run
    os.environ["WP_HOME"] = str(home)
    B.subprocess.run = fake
    try:
        B.main(_free_port(), _free_port())
    except _Stop:
        pass
    finally:
        B.subprocess.run = old_run
        os.environ["WP_HOME"] = old_env
    assert len(runs) == 2, runs
    pg = next(r for r in runs if "emlab2-wphard-pg" in r)
    es = next(r for r in runs if "emlab2-wphard-es" in r)
    return pg, es


def _tmpfs(args, path):
    """{option: value} of the --tmpfs (or --mount type=tmpfs) at path, or None."""
    for t in _opt(args, "--tmpfs"):
        p, _, opts = t.partition(":")
        if p == path:
            return dict((o.split("=", 1) + [""])[:2] for o in opts.split(",") if o)
    for m in _opt(args, "--mount"):
        kv = dict((x.split("=", 1) + [""])[:2] for x in m.split(","))
        if kv.get("type") == "tmpfs" and kv.get("destination", kv.get("dst", kv.get("target"))) == path:
            return {"size": kv.get("tmpfs-size", ""), **{o: "" for o in kv}}
    return None


class StoreStorage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg, cls.es = _bootstrap_runs()

    def _common(self, args, data, need):
        t = _tmpfs(args, data)
        self.assertIsNotNone(t, f"no tmpfs at {data}: the store's free space is shared with the host disk")
        self.assertFalse([v for v in _opt(args, "-v", "--volume") if v.split(":")[1:2] == [data]],
                         "a volume or bind at the data path")
        self.assertEqual(t.get("nr_inodes"), "0", "B4: nr_inodes=0")
        mem = _opt(args, "-m", "--memory")
        swap = _opt(args, "--memory-swap")
        self.assertEqual(len(mem), 1)
        self.assertEqual(swap, mem, "R2: memory-swap equal to memory (no swap)")
        size, m = _bytes(t["size"]), _bytes(mem[0])
        self.assertGreaterEqual(size - m, need, f"R2: S - M = {size - m} < {need}")
        return size, m

    def test_sto1_pg(self):
        self._common(self.pg, PG_DATA, MARGIN)

    def test_sto1_es(self):
        self._common(self.es, ES_DATA, ES_LOW + MARGIN)

    def test_sto2_pg_pgdata_and_settings(self):
        self.assertIn(f"PGDATA={PG_DATA}", _opt(self.pg, "-e", "--env"))
        img = self.pg.index("postgres:16")
        cmd = self.pg[img + 1:]
        self.assertEqual(cmd[:1], ["postgres"])
        got = dict(c.split("=", 1) for c in _opt(cmd, "-c"))
        self.assertEqual(got, {"max_wal_size": "256MB", "min_wal_size": "64MB", "wal_recycle": "off",
                               "wal_keep_size": "0", "archive_mode": "off", "max_connections": "200"})

    def test_sto3_es_watermarks(self):
        env = _opt(self.es, "-e", "--env")
        for k, v in (("low", "10gb"), ("high", "9gb"), ("flood_stage", "8gb")):
            self.assertIn(f"cluster.routing.allocation.disk.watermark.{k}={v}", env)

    def test_sto4_es_security_recreate_keeps_tmpfs(self):
        opts = "rw,size=17716740096,nr_inodes=0,mode=1777"
        j = {"Name": "/emlab2-wphard-es",
             "Config": {"Image": "docker.elastic.co/elasticsearch/elasticsearch:9.5.6",
                        "Labels": {"emlab2.stream": "wphard"},
                        "Env": ["discovery.type=single-node", "xpack.security.enabled=false"]},
             "HostConfig": {"PortBindings": {"9200/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19460"}]},
                            "Memory": 6 * GIB, "MemorySwap": 6 * GIB, "Tmpfs": {ES_DATA: opts}},
             "Mounts": []}
        args = es_security._run_args(j, True)
        self.assertIn(f"{ES_DATA}:{opts}", _opt(args, "--tmpfs"), "the recreated node loses its private tmpfs")
        self.assertEqual(_opt(args, "--memory-swap"), [str(6 * GIB)], "the recreated node may swap")
        self.assertEqual(_opt(args, "-v", "--volume"), [])
        # a tmpfs listed in Mounts (the --mount form) is not turned into a volume with an empty source
        j["Mounts"] = [{"Type": "tmpfs", "Source": "", "Destination": ES_DATA, "RW": True}]
        self.assertEqual(_opt(es_security._run_args(j, True), "-v", "--volume"), [])


if __name__ == "__main__":
    unittest.main()
