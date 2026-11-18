#!/usr/bin/env python3
"""Clearance by teardown (GATE-DESIGN 7.4; review F, scanner half, option 1). A result set may be staged for commit
only after a receipt shows that every store and credential that existed during its runs is destroyed, that the set is
byte-for-byte the one sealed, and that the defence-in-depth scan of it (runkit/leak_scan.py) passed.

  seal WP_HOME SET [--keystore-container NAME | --no-keystore REASON]
      while the stores are up: the SET manifest (SHA-256 per file, taken before and after the scan, required equal);
      the containers of every gate admit record since the previous receipt (every admit, in the ledger and in each
      replaced ledger, when there is no valid previous receipt) and the containers the conf names; each inventoried
      credential by inventory id, class, source number and SHA-256; the scanner's result. Writes SET/CLEARANCE.seal.json.
      Exit 0 sealed and the scan clean, 1 sealed and the scan not clean (teardown still proceeds), 2 not sealed.
  teardown WP_HOME SET [--remove]
      --remove runs `docker rm -fv` on each sealed container that still exists, that this home's ledger admitted, and
      whose name starts with the stream's OWN_PREFIX (a container named only by the conf is never removed); then requires every sealed Id absent from a successful `docker ps -a --no-trunc`, removes every
      non-directory entry in WP_HOME/secrets*, WP_SECRETS_DIR and WP_HOME/svc and the file oldhome/pgpass, and writes
      SET/CLEARANCE.json and WP_HOME/state/clearance.last.json. Exit 0 receipt written, 1 refused, 2 no seal or usage.
  stage-check WP_HOME SET
      exit 0 only when the receipt is present, the set equals its manifest, the ledger holds no admit after the seal on
      a container the receipt does not hold, every receipted container is absent, the credential locations are empty
      and listable, and the recorded scan exited 0. Writes SET/CLEARANCE.check.json (PASS or FAIL) every time.
  verify-copy COPY
      for a copy fetched from the host, before runkit/stage_results.py rewrites it: receipt present and torn down,
      recorded scan exit 0, a PASS check bound to that receipt, and the copy's files equal to the manifest. Exit 0 or 1.

The ledger is read by the gate's own code (exp/disk_gate.py Ledger: its lock, hash chain and head anchor). Nothing
printed or written names a path below WP_HOME or SET other than the fixed location names, and no value is printed or
written: credentials appear as SHA-256 only.
usage (from writepath/): python3 runkit/clearance.py <command> ..."""
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time

VERSION = "2"
LIMITS = ("LIMITS clearance v2 (GATE-DESIGN 7.4): cleared = every store (a container named by a gate admit record "
          "since the previous receipt, by every admit when there is none, or by the conf) was shown to keep its data "
          "on a private tmpfs, by the gate at admission (B3) or by clearance from docker inspect at the seal, and was "
          "absent from Docker's container list at teardown and at the stage check, so every role, API key and key held "
          "only in it is destroyed; a receipt that names no store, or a store not so shown, fails the stage check; "
          "every entry in WP_HOME/secrets*, WP_SECRETS_DIR and WP_HOME/svc and the file oldhome/pgpass was removed; "
          "the set is byte-for-byte the sealed set; and the scanner (its own LIMITS) found nothing at the seal. Not "
          "covered: data encrypted or keyed under a key-store key and published with it; copies of a store or "
          "credential made elsewhere (docker commit, backups, other paths or hosts); a container that served a run "
          "without a gate admit record and is not the conf's; operator credentials; other streams' secrets; tampering "
          "by the same OS user with the receipt, the ledger or this tool. The receipt holds the SHA-256 of each "
          "inventoried value: safe to publish for generated values (token_urlsafe(32), os.urandom), not for a "
          "low-entropy one (H-1 rules those out).")
RECEIPT, SEAL, CHECK = "CLEARANCE.json", "CLEARANCE.seal.json", "CLEARANCE.check.json"
OWN = (RECEIPT, SEAL, CHECK)
HEX64 = re.compile(r"[0-9a-f]{64}")
PS = ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}"]
SVC_WAY_OUT = ("WP_HOME/svc is written by exp/h_d2_uids.py as the service uids (0700 directories): remove it the way "
               "h_d2_uids does at the end of its run (its root helper container: rm -rf /h/svc /h/run), then run "
               "teardown again")
OTHER_WAY_OUT = "make it listable and removable by this user (or remove it as its owner), then run teardown again"


class Refused(Exception):
    """A condition is not met. The message holds no path below WP_HOME or SET and no value."""


class Usage(Exception):
    pass


def _root():
    for p in pathlib.Path(__file__).resolve().parents:
        if (p / "runkit" / "leak_scan.py").is_file() and (p / "exp" / "disk_gate.py").is_file():
            return p
    raise SystemExit("clearance: cannot find the writepath tree (runkit/leak_scan.py and exp/disk_gate.py)")


ROOT = _root()


def _utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _sha_path(p):
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused("an entry changed into something that is not a regular file while it was read")
        h = hashlib.sha256()
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                return h.hexdigest()
            h.update(b)
    finally:
        os.close(fd)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _gate(home):
    """exp/disk_gate.py loaded for this WP_HOME: its Ledger, LedgerError, STATE_DIR, LEDGER, VERSION and config."""
    os.environ["WP_HOME"] = str(home)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        return _load("disk_gate_for_clearance", ROOT / "exp" / "disk_gate.py")
    except (Exception, SystemExit) as e:   # wp.config's NotThisStream is a SystemExit whose text names the path
        raise Refused(f"the gate's code cannot be loaded for this WP_HOME ({type(e).__name__}: is it this stream's "
                      "home, with its conf?)") from None


def _write(path, obj):
    data = (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode()
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}")     # O_EXCL: never reused
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return _sha(data)


def _read_json(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return json.loads(raw), _sha(raw)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError):
        return "invalid", None


def _real_dir(p):
    try:
        return stat.S_ISDIR(os.lstat(p).st_mode)
    except OSError:
        return False


# ------------------------------------------------------------------------------------------------------- the set
def manifest(s):
    """{relative path: sha256} of every regular file in s (the CLEARANCE files at its top excepted), and the reasons
    it is incomplete: an entry that is not a regular file or directory, or one that cannot be read."""
    out, bad = {}, []

    def walk(d, rel):
        try:
            ents = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as e:
            bad.append(f"a directory in the set cannot be listed ({type(e).__name__})")
            return
        for e in ents:
            if not rel and e.name in OWN:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as x:
                bad.append(f"an entry in the set cannot be examined ({type(x).__name__})")
                continue
            if stat.S_ISDIR(st.st_mode):
                walk(e.path, f"{rel}{e.name}/")
            elif stat.S_ISREG(st.st_mode):
                try:
                    out[f"{rel}{e.name}"] = _sha_path(e.path)
                except OSError as x:
                    bad.append(f"a file in the set cannot be read ({type(x).__name__})")
            else:
                bad.append("the set holds an entry that is not a regular file or directory (link, FIFO, socket or "
                           "device): nothing in a set may point elsewhere or change on read")
    walk(str(s), "")
    return out, bad


def _diff(a, b):
    add, rem = len(set(b) - set(a)), len(set(a) - set(b))
    chg = sum(1 for k in set(a) & set(b) if a[k] != b[k])
    return f"{add} added, {chg} changed, {rem} removed"


def _set_dir(s):
    if not _real_dir(s):
        raise Usage("SET must be an existing directory (not a link)")
    return pathlib.Path(s)


# ------------------------------------------------------------------------------------------------------- the ledger
def ledger(g):
    """(records, SHA-256 of each record line), read and verified by the gate's Ledger under its lock."""
    try:
        with g.Ledger(need=False) as L:
            recs = L.recs or []
            raw = g.LEDGER.read_bytes() if recs else b""
    except g.LedgerError as e:
        raise Refused(f"the stop ledger is not intact ({str(e)[:200]})") from None
    lines = raw.split(b"\n")[:-1]
    if len(lines) != len(recs):
        raise Refused("the stop ledger changed while it was read")
    return recs, [_sha(ln) for ln in lines]


def _open_runs(recs):
    ended = {r.get("run") for r in recs if r.get("kind") == "end"}
    return [r for r in recs if r.get("kind") == "admit" and r.get("run") not in ended]


def _admit_ids(r):
    ids = [r.get("es"), r.get("pg")]
    if not all(isinstance(x, str) and HEX64.fullmatch(x) for x in ids):
        raise Refused(f"stop ledger admit record {r.get('seq')} does not name its containers by full 64-hex Docker Id")
    return ids


def sealed_containers(g, recs, hashes, home):
    """(range, first seq, {id: {from, kind, admitted, store}}) per GATE-DESIGN 7.4. An admitted container is a store
    whose data root the gate showed to be a private tmpfs at admission (B3); a 64-hex token of an unreadable replaced
    ledger line is no known store, only an Id that must be absent."""
    last, _ = _read_json(g.STATE_DIR / "clearance.last.json")
    ok = (isinstance(last, dict) and isinstance(last.get("seq"), int) and 1 <= last["seq"] <= len(recs) and
          hashes[last["seq"] - 1] == last.get("tip"))
    rng, first = ("since-receipt", last["seq"] + 1) if ok else ("all", 1)
    ids = {}
    for r in recs[first - 1:]:
        if r.get("kind") == "admit":
            for k, x in zip(("es", "pg"), _admit_ids(r)):
                ids.setdefault(x, {"from": f"admit {r['seq']}", "kind": k, "admitted": True, "store": True})
    if rng == "all":
        for n, f in enumerate(sorted(g.STATE_DIR.glob("stop_ledger.broken-*.jsonl")), 1):
            try:
                raw = f.read_bytes()
            except OSError as e:
                raise Refused(f"a replaced ledger cannot be read ({type(e).__name__})") from None
            for ln in raw.split(b"\n"):
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    r = None
                if isinstance(r, dict) and r.get("kind") != "admit":
                    continue
                if isinstance(r, dict) and all(isinstance(r.get(k), str) and HEX64.fullmatch(r[k]) for k in ("es", "pg")):
                    for k in ("es", "pg"):
                        ids.setdefault(r[k], {"from": f"replaced ledger #{n}, admit {r.get('seq')}", "kind": k,
                                              "admitted": True, "store": True})
                else:                     # unreadable: every 64-hex token must be absent too (it costs nothing)
                    for t in re.findall(rb"[0-9a-f]{64}", ln):
                        ids.setdefault(t.decode(), {"from": f"replaced ledger #{n}, unreadable line", "kind": None,
                                                    "admitted": False, "store": False})
    return rng, first, ids


def _after_seal(recs, hashes, sealed_ledger, sealed):
    """Reasons the ledger no longer fits the seal: replaced since, an open run, or an admit on unsealed containers."""
    why = []
    n, tip = sealed_ledger["to_seq"], sealed_ledger["tip"]
    if n and (len(hashes) < n or hashes[n - 1] != tip):
        return ["the stop ledger was replaced or cut after the seal: seal again"]
    late = [r for r in recs[n:] if r.get("kind") == "admit"]
    for r in late:
        try:
            ids = _admit_ids(r)
        except Refused as e:
            why.append(str(e))
            continue
        if not set(ids) <= sealed:
            why.append(f"run admitted after the seal (ledger record {r['seq']}) on a container the seal does not hold: "
                       "seal again")
    if _open_runs(recs):
        why.append("a run is open in the stop ledger (admitted, not ended)")
    return why


# ------------------------------------------------------------------------------------------------------- docker
def containers():
    """{full id: name} from a successful `docker ps -a --no-trunc`. Anything else is a Refused: an error is never
    'gone'."""
    try:
        r = subprocess.run(PS, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(f"Docker's container list cannot be read ({type(e).__name__}): nothing counts as gone") from None
    if r.returncode != 0:
        raise Refused(f"Docker's container list cannot be read (docker exit {r.returncode}): nothing counts as gone")
    out = {}
    for ln in r.stdout.splitlines():
        p = ln.split(" ", 1)
        if len(p) != 2 or not HEX64.fullmatch(p[0]):
            raise Refused("Docker's container list has a line of an unexpected form: nothing counts as gone")
        out[p[0]] = p[1].split(",")[0].strip()
    return out


def _storage(cid, data):
    """None when Docker shows the container's data directory on a private tmpfs with nothing mounted below it (the
    gate's B3 rule, from inspect only); else the reason its removal does not prove its store destroyed."""
    try:
        r = subprocess.run(["docker", "inspect", "--type", "container", cid], capture_output=True, text=True,
                           timeout=120, stdin=subprocess.DEVNULL)
        j = json.loads(r.stdout)[0] if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError, KeyError):
        j = None
    if not isinstance(j, dict):
        return "its storage could not be inspected"
    mounts = j.get("Mounts") or []
    at = [m for m in mounts if m.get("Destination") == data]
    if any(str(m.get("Destination", "")).startswith(data + "/") for m in mounts):
        return "a mount lies below its data directory"
    if (len(at) == 1 and at[0].get("Type") == "tmpfs") or (not at and data in ((j.get("HostConfig") or {}).get("Tmpfs")
                                                                                or {})):
        return None
    return (f"its data directory is on a {at[0].get('Type')} mount" if at else
            "its data directory is in the container's own layer") + ": removing the container is not shown to destroy it"


def _store_reasons(ids):
    """Why a receipt does not show its credentials dead: no store at all, or a store whose destruction is not proven."""
    stores = {c: v for c, v in ids.items() if isinstance(v, dict) and v.get("store")}
    why = [] if stores else ["the seal names no store (no admit since the previous receipt, no conf-named container "
                             "running): nothing shows this set's credentials destroyed"]
    why += [f"store {c[:12]} ({v.get('from')}): {v.get('storage')}" for c, v in sorted(stores.items())
            if not v.get("proven")]
    return why


# ------------------------------------------------------------------------------------------------------- credentials
def locations(home):
    """([(label, path)], reasons): the credential directories, each a real directory inside the resolved WP_HOME."""
    locs, bad = [], []
    try:
        names = sorted(os.listdir(home))
    except OSError as e:
        raise Refused(f"WP_HOME cannot be listed ({type(e).__name__})") from None
    for n in names:
        if n.startswith("secrets"):
            if _real_dir(home / n):
                locs.append((n if re.fullmatch(r"secrets[A-Za-z0-9._-]{0,40}", n) else "secrets*", home / n))
            else:
                bad.append("a WP_HOME/secrets* entry is not a real directory (a link or a file): it is not followed; "
                           "move it out of WP_HOME or remove it, then run again")
    sd = os.environ.get("WP_SECRETS_DIR")
    if sd:
        rp = pathlib.Path(os.path.realpath(sd))
        if not (rp == home or str(rp).startswith(str(home).rstrip(os.sep) + os.sep)):
            raise Refused("WP_SECRETS_DIR lies outside WP_HOME: not supported (GATE-DESIGN 7.4); unset it or move it")
        if not _real_dir(rp):
            bad.append("WP_SECRETS_DIR is not a directory")
        elif all(os.path.realpath(p) != str(rp) for _, p in locs):
            locs.append(("WP_SECRETS_DIR", rp))
    for n in ("svc", "oldhome"):
        if os.path.lexists(home / n) and not _real_dir(home / n):
            bad.append(f"WP_HOME/{n} is not a real directory (a link or a file): it is not followed; fix it, then run "
                       "again")
    if _real_dir(home / "svc"):
        locs.append(("svc", home / "svc"))
    return locs, bad


def entries(path):
    """(non-directory entries under path, links never followed; reasons a directory could not be listed)."""
    found, bad = [], []
    stack = [str(path)]
    while stack:
        d = stack.pop()
        try:
            ents = list(os.scandir(d))
        except OSError as e:
            bad.append(type(e).__name__)
            continue
        for e in ents:
            try:
                (stack if e.is_dir(follow_symlinks=False) else found).append(e.path)
            except OSError as x:
                bad.append(type(x).__name__)
    return found, bad


def _way_out(label):
    return SVC_WAY_OUT if label == "svc" else OTHER_WAY_OUT


def credentials_left(home):
    """Reasons the credential locations are not empty and listable (stage-check, and teardown after removal)."""
    locs, why = locations(home)
    for label, p in locs:
        found, bad = entries(p)
        if bad:
            why.append(f"credential location {label} cannot be listed completely ({bad[0]}): {_way_out(label)}")
        if found:
            why.append(f"credential location {label} holds {len(found)} entr{'y' if len(found) == 1 else 'ies'}")
    if os.path.lexists(home / "oldhome" / "pgpass"):
        why.append("oldhome/pgpass exists")
    return why


def inventory(home, ks, nks):
    """[{id, class, source, location, sha256}] for every inventoried value, in the scanner's inventory order."""
    ls = _load("leak_scan_for_clearance", ROOT / "runkit" / "leak_scan.py")
    try:
        inv = ls.load_inventory(home, ks, nks, os.environ.get("WP_SECRETS_DIR"))
    except ls.ScanError as e:                       # the scanner's messages hold no path or value
        raise Refused(f"no credential inventory ({e})") from None
    except OSError as e:
        raise Refused(f"no credential inventory ({type(e).__name__})") from None
    if inv["errors"]:
        raise Refused(f"{len(inv['errors'])} secret source(s) cannot be read: no credential inventory")
    idx = ls.Index(inv)
    vals = [v for _, v, _ in inv["file"]] + [hx.encode() for _, hx in inv["keystore"]]
    srcs, out = {}, []
    for (iid, src, cls), v in zip(idx.ids, vals):
        k = srcs.setdefault(src, len(srcs) + 1)
        head = src.split("/", 1)[0] if "/" in src else src
        loc = head if re.fullmatch(r"(secrets[A-Za-z0-9._-]{0,40}|oldhome|keystore|derived chain key|legacy key store)",
                                   head) else "other"
        out.append({"id": iid, "class": cls, "source": f"source #{k}", "location": loc, "sha256": _sha(v)})
    return out


# ------------------------------------------------------------------------------------------------------- commands
def _scan(home, s, ks, nks):
    cmd = [sys.executable, str(ROOT / "runkit" / "leak_scan.py"), str(home), str(s)]
    cmd += (["--keystore-container", ks] if ks else []) + (["--no-keystore", nks] if nks else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(f"the scanner could not run ({type(e).__name__})") from None
    rec = {"exit": r.returncode, "version": None, "sha256": None, "limits": None, "summary": None}
    for ln in r.stdout.splitlines():          # the scanner's output holds no value and no tree-derived text (FS4)
        print(f"scan| {ln}")
        m = re.fullmatch(r"leak_scan version (\S+) sha256 ([0-9a-f]{64})", ln)
        if m:
            rec["version"], rec["sha256"] = m.groups()
        elif ln.startswith("LIMITS v"):
            rec["limits"] = " ".join(ln.split()[:2])
        elif ln.startswith("files scanned:"):
            rec["summary"] = ln
    if r.stderr.strip():
        print(f"scan| (stderr: {len(r.stderr.splitlines())} line(s), not printed)")
    return rec


def _tool():
    return {"clearance": VERSION, "sha256": _sha(pathlib.Path(__file__).read_bytes())}


def seal(home, s, ks, nks):
    g = _gate(home)
    if os.path.lexists(s / RECEIPT):
        raise Refused("this set already has a teardown receipt; a cleared set is not sealed again")
    m1, bad = manifest(s)
    if bad:
        raise Refused(bad[0])
    recs, hashes = ledger(g)
    if _open_runs(recs):
        raise Refused("a run is open in the stop ledger (admitted, not ended): seal after it ends")
    rng, first, ids = sealed_containers(g, recs, hashes, home)
    listing = containers()
    for k, name in (("es", g.config.ES_CONTAINER), ("pg", g.config.PG_CONTAINER)):
        for cid, n in listing.items():
            if n == name:
                ids.setdefault(cid, {"from": f"conf {k}_container", "kind": k, "admitted": False, "store": True})
    for cid, v in ids.items():
        if v["admitted"]:
            v["storage"] = "private tmpfs (shown by the gate at admission, B3)"
        elif v["store"]:
            why = _storage(cid, g.DATA[v["kind"]])
            v["storage"] = "private tmpfs (shown by clearance at the seal)" if why is None else why
            v["proven"] = why is None
        v.setdefault("proven", v["admitted"])
    locs, bad = locations(home)
    for label, p in locs:
        _, b = entries(p)
        if b:
            bad.append(f"credential location {label} cannot be listed completely ({b[0]}): {_way_out(label)}")
    if bad:
        raise Refused(bad[0])
    creds = inventory(home, ks, nks)
    scan = _scan(home, s, ks, nks)
    m2, bad = manifest(s)
    if bad or m2 != m1:
        raise Refused("the set changed while it was sealed" + (f" ({_diff(m1, m2)})" if not bad else "") +
                      ": seal it when nothing writes to it")
    obj = {"kind": "seal", "tool": _tool(), "set": s.name, "sealed_at": _utc(), "manifest": m1,
           "containers": sorted(ids), "container_sources": ids,
           "ledger": {"range": rng, "from_seq": first, "to_seq": len(recs), "tip": hashes[-1] if hashes else None},
           "credentials": creds, "keystore": "waived (reason not recorded)" if nks else "inventoried",
           "scan": scan, "gate": {"version": g.VERSION, "sha256": _sha((ROOT / "exp" / "disk_gate.py").read_bytes())},
           "locations": [label for label, _ in locs] + ["oldhome/pgpass"], "limits": LIMITS}
    _write(s / SEAL, obj)
    print(f"clearance seal: SEALED: {len(m1)} file(s); {len(ids)} container(s) (ledger {rng}, records {first} to "
          f"{len(recs)}); {len(creds)} credential value(s); key store {obj['keystore']}; scan exit {scan['exit']}")
    blocked = _store_reasons(ids)
    if scan["exit"] == 1:
        blocked.append("the scan found a value: tear down anyway (the values die with the stores)")
    elif scan["exit"] != 0:
        blocked.append(f"the scan could not complete (exit {scan['exit']}): fix the cause and seal again BEFORE "
                       "teardown; after teardown the credentials are gone and this set can never be scanned")
    for b in blocked:
        print(f"clearance seal: staging is blocked: {b}")
    return 1 if blocked else 0


def teardown(home, s, remove):
    g = _gate(home)
    sl, seal_sha = _read_json(s / SEAL)
    if not isinstance(sl, dict) or sl.get("kind") != "seal":
        raise Usage("no valid seal in this set: run seal first")
    if os.path.lexists(s / RECEIPT):
        raise Usage("this set already has a teardown receipt")
    m, bad = manifest(s)
    if bad:
        raise Refused(bad[0])
    if m != sl["manifest"]:
        raise Refused(f"the set changed after the seal ({_diff(sl['manifest'], m)}): seal it again")
    recs, hashes = ledger(g)
    sealed = set(sl["containers"])
    why = _after_seal(recs, hashes, sl["ledger"], sealed)
    if why:
        raise Refused(why[0])
    listing = containers()
    present = sorted(c for c in sealed if c in listing)
    # --remove destroys only what this home's ledger admitted, under the stream's prefix: a container named only by
    # the conf may be another home's store on a shared host (the same conf names), so its owner removes it.
    admitted = {c for c, v in sl["container_sources"].items() if v.get("admitted")}
    foreign = [c for c in present if c in admitted and not listing[c].startswith(g.config.OWN_PREFIX)]
    conf_only = [c for c in present if c not in admitted]
    removed = []
    if remove:
        for c in present:
            if c in foreign or c in conf_only:
                continue
            r = subprocess.run(["docker", "rm", "-fv", c], capture_output=True, text=True, timeout=600,
                               stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                raise Refused(f"docker rm -fv failed for container {c[:12]} (exit {r.returncode})")
            removed.append(c)
        listing = containers()
        present = sorted(c for c in sealed if c in listing)
    if present:
        spared = []
        if remove and foreign:
            spared.append(f"{len(foreign)} with a name outside the stream prefix {g.config.OWN_PREFIX}")
        if remove and conf_only:
            spared.append(f"{len(conf_only)} named only by the conf, never admitted in this home's ledger (on a shared "
                          "host they may be another home's stores)")
        raise Refused(f"{len(present)} sealed container(s) still exist ({', '.join(c[:12] for c in present)})" +
                      (f"; not removed by --remove: {'; '.join(spared)}" if spared else "") +
                      ": if they are this home's stores, remove them (docker rm -fv), then run teardown again")
    locs, bad = locations(home)
    if bad:
        raise Refused(bad[0])
    deleted = {}
    for label, p in locs:
        found, b = entries(p)
        if b:
            raise Refused(f"credential location {label} cannot be listed completely ({b[0]}): {_way_out(label)}")
        for e in found:
            try:
                os.unlink(e)                        # a link is removed, never followed
            except OSError as x:
                raise Refused(f"an entry in credential location {label} cannot be removed ({type(x).__name__}): "
                              f"{_way_out(label)}") from None
        deleted[label] = len(found)
    pg = home / "oldhome" / "pgpass"
    if os.path.lexists(pg):
        try:
            os.unlink(pg)
        except OSError as x:
            raise Refused(f"oldhome/pgpass cannot be removed ({type(x).__name__}): {OTHER_WAY_OUT}") from None
        deleted["oldhome/pgpass"] = 1
    left = credentials_left(home)
    if left:
        raise Refused(left[0])
    obj = dict(sl, kind="teardown", tool=_tool(), torn_down_at=_utc(), seal_sha256=seal_sha, removed=removed,
               deleted=deleted, limits=LIMITS)
    h = _write(s / RECEIPT, obj)
    _write(g.STATE_DIR / "clearance.last.json", {"set": s.name, "seq": sl["ledger"]["to_seq"],
                                                 "tip": sl["ledger"]["tip"], "receipt_sha256": h, "at": _utc()})
    print(f"clearance teardown: RECEIPT WRITTEN: {len(sealed)} sealed container(s) absent ({len(removed)} removed now); "
          f"{sum(deleted.values())} credential entr{'y' if sum(deleted.values()) == 1 else 'ies'} removed")
    return 0


def stage_check(home, s):
    why = []
    rec, rec_sha = _read_json(s / RECEIPT)
    if not isinstance(rec, dict) or rec.get("kind") != "teardown":
        why.append("no valid teardown receipt (CLEARANCE.json) in this set")
    else:
        m, bad = manifest(s)
        why += bad
        if not bad and m != rec["manifest"]:
            why.append(f"the set differs from the sealed manifest ({_diff(rec['manifest'], m)})")
        _, seal_sha = _read_json(s / SEAL)
        if seal_sha != rec.get("seal_sha256"):
            why.append("CLEARANCE.seal.json is missing or differs from the seal the receipt names")
        try:
            g = _gate(home)
            recs, hashes = ledger(g)
            why += _after_seal(recs, hashes, rec["ledger"], set(rec["containers"]))
            listing = containers()
            back = sorted(c for c in rec["containers"] if c in listing)
            if back:
                why.append(f"{len(back)} receipted container(s) exist ({', '.join(c[:12] for c in back)})")
            why += credentials_left(home)
        except Refused as e:
            why.append(str(e))
        if (rec.get("scan") or {}).get("exit") != 0:
            why.append(f"the scan at the seal did not exit 0 (exit {(rec.get('scan') or {}).get('exit')})")
        why += _store_reasons(rec.get("container_sources") or {})
    _write(s / CHECK, {"kind": "stage-check", "tool": _tool(), "at": _utc(), "result": "FAIL" if why else "PASS",
                       "reasons": why, "receipt_sha256": rec_sha, "limits": LIMITS})
    for w in why:
        print(f"clearance stage-check: {w}")
    print(f"clearance stage-check: {'FAIL' if why else 'PASS'}")
    return 1 if why else 0


def verify_copy(c):
    why = []
    rec, rec_sha = _read_json(c / RECEIPT)
    chk, _ = _read_json(c / CHECK)
    if not isinstance(rec, dict) or rec.get("kind") != "teardown" or not rec.get("torn_down_at"):
        why.append("no valid teardown receipt (CLEARANCE.json) in this copy")
    else:
        if (rec.get("scan") or {}).get("exit") != 0:
            why.append("the scan at the seal did not exit 0")
        if not isinstance(chk, dict) or chk.get("result") != "PASS" or chk.get("receipt_sha256") != rec_sha:
            why.append("no PASS stage-check bound to this receipt (CLEARANCE.check.json): run stage-check on the host "
                       "first, then copy")
        _, seal_sha = _read_json(c / SEAL)
        if seal_sha != rec.get("seal_sha256"):
            why.append("CLEARANCE.seal.json is missing or differs from the seal the receipt names")
        m, bad = manifest(c)
        why += bad
        if not bad and m != rec["manifest"]:
            why.append(f"the copy differs from the sealed manifest ({_diff(rec['manifest'], m)})")
    for w in why:
        print(f"clearance verify-copy: {w}")
    print(f"clearance verify-copy: {'FAIL' if why else 'PASS'} (the receipt and the copy only; Docker and WP_HOME "
          "are checked by stage-check on the host)")
    return 1 if why else 0


def main(argv):
    try:
        if not argv:
            raise Usage("no command")
        cmd, rest = argv[0], argv[1:]
        if cmd == "verify-copy" and len(rest) == 1:
            if not _real_dir(rest[0]):
                raise Usage("COPY must be an existing directory (not a link)")
            return verify_copy(pathlib.Path(rest[0]))
        if cmd not in ("seal", "teardown", "stage-check") or len(rest) < 2:
            raise Usage("unknown command or missing WP_HOME SET")
        if not _real_dir(os.path.realpath(rest[0])):
            raise Usage("WP_HOME must be an existing directory")
        home, s, opts = pathlib.Path(os.path.realpath(rest[0])), _set_dir(rest[1]), rest[2:]
        if cmd == "seal":
            ks = nks = None
            while opts:
                if opts[0] in ("--keystore-container", "--no-keystore") and len(opts) > 1 and not (ks or nks):
                    ks, nks = (opts[1], None) if opts[0] == "--keystore-container" else (None, opts[1])
                    opts = opts[2:]
                else:
                    raise Usage("seal takes one of --keystore-container NAME or --no-keystore REASON")
            try:
                return seal(home, s, ks, nks)
            except Refused as e:
                print(f"clearance seal: NOT SEALED: {e}")
                return 2
        if cmd == "teardown":
            if opts not in ([], ["--remove"]):
                raise Usage("teardown takes only --remove")
            try:
                return teardown(home, s, opts == ["--remove"])
            except Refused as e:
                print(f"clearance teardown: NO RECEIPT: {e}")
                return 1
        if opts:
            raise Usage("stage-check takes no options")
        return stage_check(home, s)
    except Usage as e:
        print(f"clearance: {e}\n{__doc__}")
        return 2
    finally:
        print(LIMITS)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
