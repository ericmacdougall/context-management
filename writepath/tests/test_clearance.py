"""Offline test of runkit/clearance.py (GATE-DESIGN 7.4, review F scanner half, option 1: clearance rests on teardown):
a result set may be staged only after a receipt shows that every store and credential that existed during its runs is
destroyed, that the set is the one sealed, and that the defence-in-depth scan of it passed. No stores and no real
docker: a fake docker on PATH keeps a container list (it accepts only the exact calls clearance makes); every ledger
record is written by the gate's own Ledger (exp/disk_gate.py), so the reader is tested against the real writer; the
key store is waived (--no-keystore). All values are FAKE, generated here; nothing prints a value. The module under
test is runkit/clearance.py, or the file named in CLEARANCE_UNDER_TEST (for failing-first receipts).
usage (from writepath/): python3 -m unittest tests.test_clearance -v"""
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
TOOL = os.environ.get("CLEARANCE_UNDER_TEST") or str(HERE / "runkit" / "clearance.py")
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
FMT = "{{.ID}} {{.Names}}"
STREAM_LABEL = "emlab2.stream=wphard"            # host/bootstrap.py LABEL; bound by test_integ_stream_label_is_bootstraps
DATA = {"es": "/usr/share/elasticsearch/data", "pg": "/var/lib/postgresql/data"}

DOCKER = """#!/bin/sh
# fake docker: one file <full id>.name per container, holding its name, <full id>.inspect, its inspect JSON, and
# <full id>.keys, the key-store rows `psql` returns (subject and tenant keys, hex). DOCKER_HOST or DOCKER_CONTEXT set
# selects ANOTHER daemon (review a108fc3 C-2): its own, empty, container list and its own ID.
d=$FAKE_DOCKER_DIR; id=c20f7230-59a2-4824-a2f4-fda71c982ee6; name=lab-host
if [ -n "${DOCKER_HOST:-}${DOCKER_CONTEXT:-}" ]; then d=$FAKE_DOCKER_DIR.other; id=4cee4408-10d2-4e17-891c-a41736ac4536; name=other; mkdir -p "$d"; fi
[ -n "${FAKE_DOCKER_DOWN:-}" ] && { echo "Cannot connect to the Docker daemon (fake)" >&2; exit 1; }
case "$1" in
  info)
    [ "$#" = 3 ] && [ "$2" = "--format" ] || { echo "fake docker: unexpected info call" >&2; exit 9; }
    echo "$id $name" ;;
  context)
    [ "$2" = "show" ] || { echo "fake docker: unexpected context call" >&2; exit 9; }
    echo "${DOCKER_CONTEXT:-default}" ;;
  exec)
    c=""; for f in "$d"/*.name; do [ -e "$f" ] && [ "$(cat "$f")" = "$2" ] && { i=${f##*/}; c=${i%%.name}; }; done
    [ -n "$c" ] && [ "$3" = psql ] || { echo "Error: No such container: $2" >&2; exit 1; }
    for q; do :; done
    case "$q" in
      *subject_keys*) sed -n 1p "$d/$c.keys" ;;
      *tenant_keys*) sed -n 2p "$d/$c.keys" ;;
      *pg_database*) ;;
      *) echo "fake docker: unexpected query" >&2; exit 9 ;;
    esac ;;
  ps)
    if [ "$#" = 7 ] && [ "$4" = "--filter" ]; then     # the containers carrying a label (<full id>.labels: one per line)
      [ "$2" = "-a" ] && [ "$3" = "--no-trunc" ] && [ "$6" = "--format" ] && [ "$7" = "{{.ID}}" ] &&
        [ "${5#label=}" != "$5" ] || { echo "fake docker: unexpected ps call" >&2; exit 9; }
      [ -n "${FAKE_DOCKER_FILTER_DOWN:-}" ] && { echo "Error response from daemon: (fake filter failure)" >&2; exit 1; }
      for f in "$d"/*.labels; do   # a container exists while its .name does (tests remove one by its .name)
        i=${f##*/}; i=${i%%.labels}; [ -e "$d/$i.name" ] && grep -qxF -- "${5#label=}" "$f" && echo "$i"; done
      exit 0
    fi
    [ "$#" = 5 ] && [ "$2" = "-a" ] && [ "$3" = "--no-trunc" ] && [ "$4" = "--format" ] && [ "$5" = "%s" ] ||
      { echo "fake docker: unexpected ps call" >&2; exit 9; }
    for f in "$d"/*.name; do [ -e "$f" ] || continue; i=${f##*/}; printf '%%s %%s\\n' "${i%%.name}" "$(cat "$f")"; done ;;
  rm)
    [ "$2" = "-fv" ] && [ "$#" -ge 3 ] || { echo "fake docker: unexpected rm call" >&2; exit 9; }
    shift 2
    for c in "$@"; do
      [ -e "$d/$c.name" ] || { echo "Error response from daemon: No such container: $c" >&2; exit 1; }
      rm -f "$d/$c.name" "$d/$c.inspect" "$d/$c.labels"; echo "$c"
    done ;;
  inspect)
    [ "$#" = 4 ] && [ "$2" = "--type" ] && [ "$3" = "container" ] || { echo "fake docker: unexpected inspect call" >&2; exit 9; }
    [ -e "$d/$4.inspect" ] || { echo "Error: No such container: $4" >&2; exit 1; }
    cat "$d/$4.inspect" ;;
  *) echo "fake docker: unsupported call" >&2; exit 9 ;;
esac
""" % FMT

LEDGER_TOOL = """import importlib.util, json, os, sys
root = sys.argv[1]
sys.path.insert(0, root)
spec = importlib.util.spec_from_file_location("disk_gate_for_clearance_test", os.path.join(root, "exp", "disk_gate.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
with g.Ledger(need=False) as L:
    if L.recs is None:
        L.append("init", v=g.FORMAT, source="test", reason="clearance test")
    for kind, kw in json.loads(sys.argv[2]):
        L.append(kind, **kw)
"""


class Clearance(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        for d in ("secrets", "oldhome", "conf", "state", "svc/app"):
            (self.home / d).mkdir(parents=True)
        for k, v in GOOD.items():
            (self.home / "conf" / k).write_text(v + "\n")
        self.new_secrets()
        (self.home / "oldhome" / "pg_port").write_text("25660\n")
        self.dock = self.tmp / "docker"
        self.dock.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(DOCKER)
        (bindir / "docker").chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_DOCKER_DIR=str(self.dock))
        self.env.pop("WP_SECRETS_DIR", None)
        for k in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
            self.env.pop(k, None)
        self.env.pop("FAKE_DOCKER_DOWN", None)
        self.es, self.pg = self.container("emlab2-wphard-es"), self.container("emlab2-wphard-pg")
        self.run_ledger(self.es, self.pg)
        self.set = self.mkset("chain-1")

    def new_secrets(self):
        """What bootstrap and setup write for a new store generation (H-1): fresh values in every location."""
        self.pw, self.legacy = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        (self.home / "secrets" / "pgpass").write_text(f"127.0.0.1:25660:*:wp:{self.pw}\n")
        (self.home / "secrets" / "es_elastic_password").write_text(secrets.token_urlsafe(20) + "\n")
        (self.home / "oldhome" / "pgpass").write_text(f"127.0.0.1:25660:*:emlab:{self.legacy}\n")
        (self.home / "svc" / "app" / "pgpass.wp_app").write_text(f"127.0.0.1:25660:*:wp:{self.pw}\n")

    def mkset(self, name):
        s = self.home / "results" / name
        s.mkdir(parents=True)
        (s / "one.abc.x.out").write_text("start\ngate=VALID\n")
        (s / "r.json").write_text(json.dumps({"verdict": "PASS"}))
        return s

    def container(self, name, data_type="tmpfs", labels=None):
        """A fake container; its store's data directory (by the name's -es or -pg suffix) on a mount of data_type.
        Labels: by default the stream's (host/bootstrap.py LABEL) on a name under the stream prefix, else none."""
        cid = secrets.token_hex(32)
        (self.dock / f"{cid}.name").write_text(name + "\n")
        if labels is None:
            labels = [STREAM_LABEL] if name.startswith(GOOD["stream"] + "-") else []
        if labels:
            (self.dock / f"{cid}.labels").write_text("".join(x + "\n" for x in labels))
        d = DATA.get(name.rsplit("-", 1)[-1], "/data")
        (self.dock / f"{cid}.inspect").write_text(json.dumps([{"Id": cid, "Name": "/" + name, "HostConfig": {},
                                                               "Mounts": [{"Type": data_type, "Destination": d}]}]))
        (self.dock / f"{cid}.keys").write_text(secrets.token_hex(32) + "\n" + secrets.token_hex(32) + "\n")
        return cid

    def exists(self, cid):
        return (self.dock / f"{cid}.name").exists()

    def ledger(self, *recs):
        r = subprocess.run([sys.executable, "-c", LEDGER_TOOL, str(HERE), json.dumps(recs)], capture_output=True,
                           text=True, env=dict(self.env, WP_HOME=str(self.home)), timeout=60)
        self.assertEqual(r.returncode, 0, "the gate's Ledger could not write the test ledger: " + r.stderr[-300:])

    def run_ledger(self, es, pg, end=True, v=4):
        """An admitted run; v=None writes the admit as the v2 gate did (no format field, no tmpfs rule)."""
        run = "run-" + secrets.token_hex(3)
        recs = [("admit", {"run": run, "es": es, "pg": pg, **({"v": v, "gate": f"disk_gate v{v}"} if v else {})})]
        if end:
            recs.append(("end", {"run": run, "verdict": "VALID"}))
        self.ledger(*recs)

    def tool(self, *args, **env):
        e = dict(self.env, **env)
        r = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, env=e, timeout=300)
        return r.returncode, r.stdout + r.stderr

    def seal(self, s=None, waive=None, **env):
        """Seal with the key store inventoried from the conf's PG container (fake psql), or waived with a reason."""
        return self.tool("seal", str(self.home), str(s or self.set), *(["--no-keystore", waive] if waive else []), **env)

    def teardown(self, s=None, remove=True, **env):
        return self.tool("teardown", str(self.home), str(s or self.set), *(["--remove"] if remove else []), **env)

    def check(self, s=None, **env):
        return self.tool("stage-check", str(self.home), str(s or self.set), **env)

    def leaked(self, text):
        return [i for i, v in enumerate((self.pw, self.legacy)) if any(v[j:j + 12] in text for j in range(len(v) - 11))]

    def cleared(self, s=None):
        rc, out = self.seal(s)
        self.assertEqual(rc, 0, out[-400:])
        rc, out2 = self.teardown(s)
        self.assertEqual(rc, 0, out2[-400:])
        return out + out2

    def receipt(self, name="CLEARANCE.json", s=None):
        return json.loads(((s or self.set) / name).read_text())

    # ---- the positive control
    def test_seal_teardown_stage_check(self):
        out = self.cleared()
        rc, out2 = self.check()
        self.assertEqual(rc, 0, out2[-400:])
        self.assertFalse(self.exists(self.es) or self.exists(self.pg), "a sealed container remains")
        left = [p for d in ("secrets", "svc") for p in (self.home / d).rglob("*") if not p.is_dir()]
        self.assertEqual(left, [], "a credential file remains after teardown")
        self.assertFalse((self.home / "oldhome" / "pgpass").exists(), "oldhome/pgpass remains")
        self.assertTrue((self.home / "oldhome" / "pg_port").exists(), "teardown removed a file that is no credential")
        rec = self.receipt()
        self.assertEqual(sorted(rec["containers"]), sorted([self.es, self.pg]))
        self.assertIn(hashlib.sha256(self.pw.encode()).hexdigest(), {c["sha256"] for c in rec["credentials"]},
                      "a credential is not in the receipt")
        self.assertEqual(rec["scan"]["exit"], 0)
        self.assertEqual(self.receipt("CLEARANCE.check.json")["result"], "PASS")
        texts = [(self.set / n).read_text() for n in ("CLEARANCE.json", "CLEARANCE.seal.json", "CLEARANCE.check.json")]
        for t in texts:
            for p in {str(self.tmp), os.path.realpath(self.tmp)}:
                self.assertNotIn(p, t, "a CLEARANCE file holds an absolute path")
        self.assertEqual(self.leaked(out + out2 + "".join(texts)), [], "a receipt or the output holds a value")

    def test_verify_copy_on_a_copied_set(self):
        self.cleared()
        early = self.tmp / "copy-before-check"
        shutil.copytree(self.set, early)
        rc, _ = self.tool("verify-copy", str(early))
        self.assertEqual(rc, 1, "a copy without a passing stage-check passed verify-copy")
        self.assertEqual(self.check()[0], 0)
        copy = self.tmp / "copy"
        shutil.copytree(self.set, copy)
        rc, out = self.tool("verify-copy", str(copy))
        self.assertEqual(rc, 0, out[-400:])
        (copy / "r.json").write_text(json.dumps({"verdict": "FAIL"}))
        rc, _ = self.tool("verify-copy", str(copy))
        self.assertEqual(rc, 1, "a changed file in a copied set passed verify-copy")

    # ---- each condition of stage-check and teardown
    def test_no_receipt_no_staging(self):
        self.assertEqual(self.seal()[0], 0)
        rc, _ = self.check()
        self.assertEqual(rc, 1, "a sealed set without a teardown receipt passed stage-check")

    def test_teardown_refuses_while_a_sealed_container_exists(self):
        self.seal()
        rc, _ = self.teardown(remove=False)
        self.assertEqual(rc, 1, "teardown wrote a receipt while sealed containers still exist")
        self.assertFalse((self.set / "CLEARANCE.json").exists())

    def test_set_changed_after_the_seal_fails(self):
        for change in ("added", "changed", "removed"):
            with self.subTest(change=change):
                self.setUp()
                self.cleared()
                if change == "added":
                    (self.set / "late.log").write_text("written after the seal\n")
                elif change == "changed":
                    (self.set / "r.json").write_text(json.dumps({"verdict": "FAIL"}))
                else:
                    (self.set / "r.json").unlink()
                rc, _ = self.check()
                self.assertEqual(rc, 1, f"a set {change} after the seal passed stage-check")

    def test_credential_file_after_teardown_fails(self):
        self.cleared()
        (self.home / "secrets" / "pgpass").write_text(f"127.0.0.1:25660:*:wp:{secrets.token_urlsafe(32)}\n")
        rc, _ = self.check()
        self.assertEqual(rc, 1, "a credential file present at staging passed stage-check")

    def test_run_admitted_after_the_seal_fails(self):
        self.seal()
        self.run_ledger(self.container("emlab2-wphard-es"), self.container("emlab2-wphard-pg"))
        rc, _ = self.teardown()
        self.assertEqual(rc, 1, "teardown accepted a run admitted after the seal on containers it did not seal")
        rc, _ = self.check()
        self.assertEqual(rc, 1, "a run admitted after the seal (containers not in the receipt) passed stage-check")

    def test_receipted_container_back_fails(self):
        self.cleared()
        (self.dock / f"{self.pg}.name").write_text("emlab2-wphard-pg\n")
        rc, _ = self.check()
        self.assertEqual(rc, 1, "a receipted container that exists again passed stage-check")

    def test_scan_hit_at_seal_blocks_staging(self):
        (self.set / "leak.log").write_text(f"pw={self.pw}\n")
        rc, out = self.seal()
        self.assertEqual(rc, 1, "a seal whose scan found a value did not exit 1")
        self.assertEqual(self.teardown()[0], 0, "teardown is refused after a scan hit (destroying must stay possible)")
        rc, out2 = self.check()
        self.assertEqual(rc, 1, "a set whose scan found a value passed stage-check")
        self.assertEqual(self.leaked(out + out2), [], "the output holds a value")

    def test_seal_refuses_a_special_file_or_link(self):
        for kind in ("fifo", "link"):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == "fifo":
                    os.mkfifo(self.set / "pipe")
                else:
                    (self.set / "ln").symlink_to(self.home / "secrets" / "pgpass")
                rc, out = self.seal()
                self.assertEqual((rc, "NOT SEALED" in out), (2, True), f"seal accepted a set holding a {kind}")
                self.assertFalse((self.set / "CLEARANCE.seal.json").exists())

    # ---- the ledger: read by the gate's code
    def test_open_run_refuses_the_seal(self):
        self.run_ledger(self.es, self.pg, end=False)
        rc, out = self.seal()
        self.assertEqual((rc, "NOT SEALED" in out), (2, True), "seal accepted a ledger with an open run")

    def test_edited_ledger_refuses_the_seal(self):
        led = self.home / "state" / "stop_ledger.jsonl"
        led.write_text("".join(ln for ln in led.read_text().splitlines(True) if '"admit"' not in ln))
        rc, out = self.seal()
        self.assertEqual((rc, "NOT SEALED" in out), (2, True), "seal accepted a ledger with an admit record removed")

    def test_second_chain_seals_only_its_containers(self):
        self.cleared()
        es2, pg2 = self.container("emlab2-wphard-es"), self.container("emlab2-wphard-pg")
        self.run_ledger(es2, pg2)
        self.new_secrets()
        s2 = self.mkset("chain-2")
        self.assertEqual(self.seal(s2)[0], 0)
        seal = self.receipt("CLEARANCE.seal.json", s2)
        self.assertEqual(seal["ledger"]["range"], "since-receipt")
        self.assertEqual(sorted(seal["containers"]), sorted([es2, pg2]))

    def test_ledger_reset_seals_every_admit_ever(self):
        self.cleared()
        st = self.home / "state"
        os.replace(st / "stop_ledger.jsonl", st / "stop_ledger.broken-20261117T000000Z.jsonl")
        os.replace(st / "stop_ledger.head", st / "stop_ledger.head.broken-20261117T000000Z")
        es2, pg2 = self.container("emlab2-wphard-es"), self.container("emlab2-wphard-pg")
        self.run_ledger(es2, pg2)
        self.new_secrets()
        s2 = self.mkset("chain-2")
        self.assertEqual(self.seal(s2)[0], 0)
        seal = self.receipt("CLEARANCE.seal.json", s2)
        self.assertEqual(seal["ledger"]["range"], "all")
        self.assertTrue({self.es, self.pg, es2, pg2} <= set(seal["containers"]),
                        "after a ledger reset the seal left out containers of the replaced ledger")
        self.assertEqual(self.teardown(s2)[0], 0)

    # ---- fail closed: Docker, listing, bounds
    def test_docker_error_is_never_gone(self):
        self.seal()
        for c in (self.es, self.pg):
            (self.dock / f"{c}.name").unlink()
        rc, _ = self.teardown(remove=False, FAKE_DOCKER_DOWN="1")
        self.assertEqual(rc, 1, "teardown took an unreachable Docker for 'gone'")
        self.assertFalse((self.set / "CLEARANCE.json").exists())
        self.assertEqual(self.teardown(remove=False)[0], 0)
        rc, _ = self.check(FAKE_DOCKER_DOWN="1")
        self.assertEqual(rc, 1, "stage-check took an unreachable Docker for 'gone'")

    def test_unlistable_credential_dir_refuses_the_receipt(self):
        self.seal()
        d = self.home / "svc" / "app"
        d.chmod(0)
        self.addCleanup(d.chmod, 0o700)
        rc, out = self.teardown()
        self.assertEqual(rc, 1, "teardown wrote a receipt over a credential directory it could not list")
        self.assertFalse((self.set / "CLEARANCE.json").exists())
        self.assertIn("h_d2_uids", out, "the refusal does not name the way out")

    def test_deletion_stays_inside_wp_home(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not this stream's\n")
        self.assertEqual(self.seal()[0], 0)
        (self.home / "secrets-x").symlink_to(outside)
        rc, _ = self.teardown()
        self.assertEqual(rc, 1, "teardown accepted a secrets entry that is a link")
        self.assertTrue((outside / "keep.txt").exists(), "teardown deleted a file outside WP_HOME")
        self.assertFalse((self.set / "CLEARANCE.json").exists())
        (self.home / "secrets-x").unlink()
        rc, out = self.seal(self.mkset("chain-2"), WP_SECRETS_DIR=str(outside))
        self.assertEqual((rc, "NOT SEALED" in out and "WP_SECRETS_DIR" in out), (2, True),
                         "seal accepted a WP_SECRETS_DIR outside WP_HOME")

    def test_home_of_another_stream_is_refused_without_its_path(self):
        (self.home / "conf" / "stream").write_text("another-stream\n")
        for cmd in ("seal", "stage-check"):
            with self.subTest(cmd=cmd):
                rc, out = self.seal() if cmd == "seal" else self.check()
                self.assertEqual((rc, "NOT SEALED" in out or "FAIL" in out), (2 if cmd == "seal" else 1, True),
                                 f"{cmd} ran on a home that is not this stream's")
                for p in {str(self.tmp), os.path.realpath(self.tmp)}:
                    self.assertNotIn(p, out, f"{cmd} printed an absolute path")

    def test_remove_spares_a_conf_named_container_this_ledger_never_admitted(self):
        """A shared host: another home with the same conf names runs stores under the same names. They are sealed (they
        may hold this home's credentials) but --remove must not destroy them; teardown refuses until their owner acts."""
        (self.dock / f"{self.es}.name").unlink()               # this home's admitted ES is gone
        stranger = self.container("emlab2-wphard-es")           # a store under the conf name, never admitted here
        self.assertEqual(self.seal()[0], 0)
        self.assertIn(stranger, self.receipt("CLEARANCE.seal.json")["containers"], "a conf-named container was not sealed")
        rc, _ = self.teardown()
        self.assertEqual(rc, 1, "teardown wrote a receipt while a conf-named container exists")
        self.assertTrue(self.exists(stranger), "--remove destroyed a container this home's ledger never admitted")
        self.assertFalse(self.exists(self.pg), "--remove spared an admitted container of the stream")

    def test_conf_named_store_not_on_tmpfs_blocks_staging(self):
        """A store the gate never admitted was never shown to be a private tmpfs; on a volume, docker rm leaves its roles."""
        (self.dock / f"{self.es}.name").unlink()
        stranger = self.container("emlab2-wphard-es", data_type="volume")
        rc, out = self.seal()
        self.assertEqual(rc, 1, "seal of a set whose conf-named store keeps its data on a volume did not exit 1")
        (self.dock / f"{stranger}.name").unlink()               # its owner removes it
        self.assertEqual(self.teardown()[0], 0, "teardown is refused (destroying must stay possible)")
        rc, _ = self.check()
        self.assertEqual(rc, 1, "a store whose destruction is not proven passed stage-check")
        self.setUp()                                             # the same store on a private tmpfs: proven, passes
        (self.dock / f"{self.es}.name").unlink()
        on_tmpfs = self.container("emlab2-wphard-es", data_type="tmpfs")
        self.assertEqual(self.seal()[0], 0, "seal of a set whose conf-named store is on a private tmpfs did not exit 0")
        (self.dock / f"{on_tmpfs}.name").unlink()                # its owner removes it (--remove never does)
        self.assertEqual(self.teardown()[0], 0)
        self.assertEqual(self.check()[0], 0, "a conf-named store on a private tmpfs failed stage-check")

    def test_receipt_with_no_store_blocks_staging(self):
        self.cleared()
        self.new_secrets()
        s2 = self.mkset("chain-2")                               # no admit since the receipt, no store up
        rc, _ = self.seal(s2, waive="no store is up to read the key store from")
        self.assertEqual(rc, 1, "a seal that names no store did not exit 1")
        self.assertEqual(self.teardown(s2)[0], 0)
        rc, _ = self.check(s2)
        self.assertEqual(rc, 1, "a receipt that shows no store destroyed passed stage-check")

    def test_two_sets_sealed_before_the_teardown_both_clear(self):
        """The runbook: every set of a chain is sealed while the stores are up; the first teardown destroys them."""
        s2 = self.mkset("chain-1b")
        self.assertEqual(self.seal()[0], 0)
        self.assertEqual(self.seal(s2)[0], 0)
        self.assertEqual(self.teardown()[0], 0)
        self.assertEqual(self.teardown(s2)[0], 0, "the second set's teardown is refused after the first")
        self.assertEqual((self.check()[0], self.check(s2)[0]), (0, 0))

    def test_store_started_under_a_conf_name_after_the_seal_fails(self):
        """bootstrap reuses an existing password file until H-1: a store recreated after the seal holds sealed values."""
        self.seal()
        for c in (self.es, self.pg):
            (self.dock / f"{c}.name").unlink()
        late = self.container("emlab2-wphard-pg")
        rc, _ = self.teardown(remove=False)
        self.assertEqual(rc, 1, "teardown wrote a receipt while an unsealed store runs under a conf name")
        self.assertTrue((self.home / "secrets" / "pgpass").exists(), "teardown deleted credentials before refusing")
        (self.dock / f"{late}.name").unlink()
        self.assertEqual(self.teardown(remove=False)[0], 0)
        self.container("emlab2-wphard-es")
        rc, _ = self.check()
        self.assertEqual(rc, 1, "stage-check passed while an unsealed store runs under a conf name")

    def test_set_is_wp_home_results_itself(self):
        """The runbook's shape: launch_one.sh writes flat into WP_HOME/results, and that directory is the set."""
        s = self.home / "results"
        (s / "one.abc.y.out").write_text("start\ngate=VALID\n")
        self.cleared(s)
        self.assertEqual(self.check(s)[0], 0)

    def test_admit_by_a_gate_without_a_tmpfs_rule_proves_nothing(self):
        old_es, old_pg = self.container("emlab2-wphard-es"), self.container("emlab2-wphard-pg")
        self.run_ledger(old_es, old_pg, v=None)                  # admitted by the v2 gate, removed long ago
        for c in (old_es, old_pg):
            (self.dock / f"{c}.name").unlink()
        rc, _ = self.seal()
        self.assertEqual(rc, 1, "a seal naming stores a pre-v3 gate admitted, now gone, did not exit 1")
        self.assertEqual(self.teardown()[0], 0)
        rc, _ = self.check()
        self.assertEqual(rc, 1, "an admit by a gate without a tmpfs rule passed as proof that its store was destroyed")

    def test_remove_spares_a_container_outside_the_stream_prefix(self):
        other = self.container("other-stream-es")
        self.run_ledger(other, self.pg)
        self.assertEqual(self.seal()[0], 0)
        rc, out = self.teardown()
        self.assertEqual(rc, 1, "teardown wrote a receipt while a foreign sealed container exists")
        self.assertTrue(self.exists(other), "--remove removed a container outside the stream's prefix")
        self.assertFalse((self.set / "CLEARANCE.json").exists())

    # ---- review a108fc3 C-1: the seal records the names it relied on; a store under them after the seal is refused
    def test_c1_store_recreated_under_a_sealed_conf_name_after_a_conf_edit_is_refused(self):
        self.assertEqual(self.seal()[0], 0)
        for c in (self.es, self.pg):
            (self.dock / f"{c}.name").unlink()
        late = self.container("emlab2-wphard-pg")              # a store under the seal-time conf name
        (self.home / "conf" / "pg_container").write_text("emlab2-wphard-pg2\n")     # the conf edited after the seal
        rc, out = self.teardown(remove=False)
        self.assertEqual(rc, 1, f"teardown wrote a receipt while a store runs under a sealed conf name: {out[-300:]}")
        self.assertTrue(self.exists(late))
        (self.dock / f"{late}.name").unlink()
        self.assertEqual(self.teardown(remove=False)[0], 0)
        self.container("emlab2-wphard-pg")
        rc, out = self.check()
        self.assertEqual(rc, 1, f"stage-check passed while a store runs under a sealed conf name: {out[-300:]}")

    def test_c1_unsealed_container_under_the_stream_prefix_is_refused(self):
        self.assertEqual(self.seal()[0], 0)
        self.container("emlab2-wphard-pgcopy")                 # neither a conf name nor sealed, but the stream's
        rc, out = self.teardown()
        self.assertEqual(rc, 1, f"teardown wrote a receipt while an unsealed store of the stream runs: {out[-300:]}")

    # ---- integ (hardening 8b12324): the service containers (key service, sealer) count as the stream's
    def test_integ_key_service_container_under_the_prefix_is_refused(self):
        self.assertEqual(self.seal()[0], 0)
        self.container("emlab2-wphard-keysvc")                 # the hardening key service, not sealed
        rc, out = self.teardown()
        self.assertEqual(rc, 1, f"teardown wrote a receipt while the key service runs: {out[-300:]}")

    def test_integ_unsealed_container_with_the_stream_label_is_refused(self):
        self.assertEqual(self.seal()[0], 0)
        self.container("sealer-scratch", labels=[STREAM_LABEL])  # outside the prefix, but the stream's label
        rc, out = self.teardown()
        self.assertEqual(rc, 1, f"teardown wrote a receipt while a container of the stream's label runs: {out[-300:]}")
        self.assertIn("label", out)

    def test_integ_stage_check_fails_on_a_container_with_the_stream_label(self):
        self.cleared()
        self.assertEqual(self.check()[0], 0)
        self.container("sealer-scratch", labels=[STREAM_LABEL])
        rc, out = self.check()
        self.assertEqual(rc, 1, f"stage-check passed while a container of the stream's label runs: {out[-300:]}")

    def test_integ_another_streams_label_is_not_counted(self):
        self.container("other-tool", labels=["emlab2.stream=other", "wphard=1"])
        self.cleared()
        self.assertEqual(self.check()[0], 0)

    def test_integ_label_list_failure_is_refused(self):
        self.assertEqual(self.seal()[0], 0)
        rc, out = self.teardown(FAKE_DOCKER_FILTER_DOWN="1")
        self.assertEqual(rc, 1, f"teardown wrote a receipt without the stream-label container list: {out[-300:]}")
        self.assertFalse((self.set / "CLEARANCE.json").exists())

    def test_integ_stream_label_is_bootstraps(self):
        for f in ("host/bootstrap.py", "exp/h_d2_uids.py"):
            self.assertRegex((HERE / f).read_text(), rf'(?m)^LABEL = "{re.escape(STREAM_LABEL)}"', f)
        gp = (HERE / "exp" / "guard_policy.py").read_text()
        k, v = STREAM_LABEL.split("=")
        self.assertRegex(gp, rf'(?m)^STREAM_LABEL = \("{re.escape(k)}", "{re.escape(v)}"\)')

    # ---- C-2: teardown and stage-check judge the daemon the seal judged
    def test_c2_teardown_against_another_daemon_is_refused(self):
        self.assertEqual(self.seal()[0], 0)
        rc, out = self.teardown(DOCKER_HOST="tcp://127.0.0.1:2376")
        self.assertEqual(rc, 1, f"teardown on another daemon counted the sealed stores as gone: {out[-300:]}")
        self.assertTrue(self.exists(self.es) and self.exists(self.pg))
        self.assertFalse((self.set / "CLEARANCE.json").exists(), "a receipt was written from another daemon")

    def test_c2_stage_check_against_another_daemon_fails(self):
        self.cleared()
        self.assertEqual(self.check()[0], 0)
        self.container("emlab2-wphard-es")                     # nothing of it is visible from the other daemon
        rc, out = self.check(DOCKER_CONTEXT="rootless")
        self.assertEqual(rc, 1, f"stage-check passed against another daemon: {out[-300:]}")

    # ---- C-3: a waiver never counts as clearance
    def test_c3_waived_key_store_never_passes(self):
        rc, out = self.seal(waive="key store container not reachable")
        self.assertNotEqual(rc, 0, f"a seal without the key store exited 0: {out[-300:]}")
        self.assertEqual(self.teardown()[0], 0)
        rc, out = self.check()
        self.assertEqual(rc, 1, f"stage-check passed a set whose key store was not covered: {out[-300:]}")
        self.assertIn("key store", out)
        self.assertEqual(self.receipt("CLEARANCE.check.json")["result"], "FAIL")
        self.assertEqual(self.receipt().get("keystore", {}).get("waiver"), "key store container not reachable",
                         "the receipt does not record the waiver's reason")

    # ---- review aacf3c0 C-3 remainder (m-F): verify-copy judges the receipt itself, not only the check file
    def rebind(self, edit):
        """Edit the receipt as an older clearance (or a hand) wrote it, then write a PASS check bound to it, as an
        older stage-check that had no key store or daemon rule would have."""
        rec = self.receipt()
        edit(rec)
        raw = json.dumps(rec, indent=1, sort_keys=True).encode()
        (self.set / "CLEARANCE.json").write_bytes(raw)
        chk = {"kind": "stage-check", "tool": {"clearance": "3"}, "at": "2026-11-18T00:00:00Z", "result": "PASS",
               "reasons": [], "receipt_sha256": hashlib.sha256(raw).hexdigest()}
        (self.set / "CLEARANCE.check.json").write_text(json.dumps(chk))
        copy = self.tmp / f"copy-{len(list(self.tmp.iterdir()))}"
        shutil.copytree(self.set, copy)
        return self.tool("verify-copy", str(copy))

    def test_c3_v3_receipt_with_a_waived_key_store_fails_verify_copy(self):
        self.seal(waive="key store container not reachable")
        self.assertEqual(self.teardown()[0], 0)
        rc, out = self.rebind(lambda r: r["tool"].update(clearance="3"))
        self.assertEqual(rc, 1, f"verify-copy passed a v3 receipt sealed with --no-keystore: {out[-300:]}")
        self.assertIn("older than v4", out)
        self.assertIn("key store was not covered", out)

    def test_c3_receipt_without_a_daemon_fails_verify_copy(self):
        self.cleared()
        rc, out = self.rebind(lambda r: r.pop("daemon"))
        self.assertEqual(rc, 1, f"verify-copy passed a receipt that records no daemon: {out[-300:]}")
        self.assertIn("no Docker daemon identity", out)

    def test_c3_current_receipt_with_a_rebound_check_still_passes(self):
        self.cleared()
        rc, out = self.rebind(lambda r: None)
        self.assertEqual(rc, 0, out[-400:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
