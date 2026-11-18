"""Stage lab results into writepath/results/, only as the gate recorded them and only from a cleared set (GATE-DESIGN
8.2 P8 and 9; reviews a108fc3 G-2 and aacf3c0 m-E, H-9, F-2).

A SET is a copy of WP_HOME/results made after clearance (runkit/clearance.py seal, teardown, stage-check on the host):
its `runs/` holds one directory per run id. The ledger copy is a copy of WP_HOME/state/stop_ledger.jsonl with
stop_ledger.head next to it. Before anything is staged:
- the set passes `clearance.py verify-copy` (a v4+ receipt, a PASS stage-check bound to it, the key store covered, the
  daemon recorded, every store proven gone, and the set's files equal to the receipt's manifest, so every staged file
  is one clearance judged);
- the ledger copy verifies: its hash chain, and its head anchor for the last record; and it is the ledger the receipt
  sealed: its record at the receipt's to_seq has the receipt's tip hash, and every staged run ended at or before it;
- every requested file is listed, with its SHA-256, by the one end record of a run admitted by gate v6 or later
  (exactly one admit, exactly one end), whose verdict is VALID and whose experiment exited by itself with a status
  >= 0 (F-2: a status < 0 is a signal);
- the groups file lists at least one file; every group is a plain name (no path); no requested file was staged before
  (in MANIFEST.v6.tsv or on disk).
Any refusal stages nothing. code_commit is the commit the gate's admit and end records name (never a field of the
row). The host home prefix is shortened to '~/' in every copy. The set's CLEARANCE files are copied to
<dst>/clearance/<set name>/ (H-9: the committed set carries its receipt). Rows are appended to <dst>/MANIFEST.v6.tsv;
<dst>/MANIFEST.tsv (the pre-v5 provenance list, runkit/groups.pre-v5.tsv) is never written. Prints counts and reasons
only.
usage: HOST_HOME=<host home with trailing slash> python3 runkit/stage_results.py <groups.tsv> <SET>/runs <ledger copy> <dst>
groups.tsv columns: group, run, file (relative to the run's directory), label."""
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import stat
import sys

MANIFEST = "MANIFEST.v6.tsv"
HEADER = ["group", "run", "file", "code_commit", "sha256_original", "sha256_committed", "end_seq", "label", "set"]
GROUP_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
COMMIT_RX = re.compile(r"[0-9a-f]{40}")
GATE_RX = re.compile(r"disk_gate v(\d+)")
CLEARANCE_FILES = ("CLEARANCE.json", "CLEARANCE.seal.json", "CLEARANCE.check.json")


def refuse(msg):
    print(f"stage_results: refused: {msg}; nothing was staged")
    sys.exit(2)


def ledger(path):
    """(the verified records of a ledger copy up to its head anchor's count, the SHA-256 of each record line)."""
    raw = pathlib.Path(path).read_bytes()
    try:
        head = json.loads((pathlib.Path(path).parent / "stop_ledger.head").read_text())
        n, tip = int(head["count"]), str(head["tip"])
    except (OSError, ValueError, KeyError, TypeError):
        refuse("the ledger copy has no readable head anchor (stop_ledger.head next to it)")
    recs, prev, hashes = [], "0" * 64, []
    for i, ln in enumerate(raw.split(b"\n")[:-1] if raw.endswith(b"\n") else [None]):
        try:
            r = json.loads(ln) if ln is not None else None
        except ValueError:
            r = None
        if not isinstance(r, dict) or r.get("seq") != i + 1 or r.get("prev") != prev:
            refuse(f"ledger record {i + 1} breaks the sequence or the hash chain (edited, removed or partial)")
        recs.append(r)
        prev = hashlib.sha256(ln).hexdigest()
        hashes.append(prev)
    if n < 1 or len(recs) < n or hashes[n - 1] != tip:
        refuse("the ledger copy does not match its head anchor (edited, truncated or replaced)")
    return recs[:n], hashes[:n]


def _read_regular(p):
    """The bytes of the regular file p (a link, FIFO or other entry is refused, never opened for reading)."""
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        refuse(f"{pathlib.Path(p).name} cannot be opened as a regular file ({type(e).__name__})")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            refuse(f"{pathlib.Path(p).name} is not a regular file")
        chunks = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                return b"".join(chunks)
            chunks.append(b)
    finally:
        os.close(fd)


def cleared(s):
    """H-9: the set passes clearance verify-copy (the clearance tool of this tree, loaded by path)."""
    path = pathlib.Path(__file__).resolve().parent / "clearance.py"
    spec = importlib.util.spec_from_file_location("clearance_for_staging", path)
    cl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cl)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cl.verify_copy(pathlib.Path(s))
    for ln in buf.getvalue().splitlines():
        print(f"stage_results: {ln}")
    if rc != 0:
        refuse("the set did not pass clearance verify-copy")
    return json.loads(_read_regular(pathlib.Path(s) / "CLEARANCE.json"))


def main(groups, runs, ledger_copy, dst):
    home = os.environ.get("HOST_HOME", "")
    if not home.endswith("/") or len(home) < 3:
        sys.exit("HOST_HOME must name the host home directory with a trailing slash")
    runs, dst = pathlib.Path(runs), pathlib.Path(dst)
    s = runs.parent
    if runs.name != "runs" or not os.path.isdir(s) or os.path.islink(runs):
        refuse("the runs copy must be <SET>/runs of a cleared set")
    rec = cleared(s)
    recs, hashes = ledger(ledger_copy)
    sealed = rec.get("ledger") if isinstance(rec.get("ledger"), dict) else {}
    to_seq = sealed.get("to_seq")
    if not (isinstance(to_seq, int) and 1 <= to_seq <= len(hashes) and hashes[to_seq - 1] == sealed.get("tip")):
        refuse("the ledger copy is not the ledger the clearance receipt sealed (its record at the receipt's to_seq "
               "does not have the receipt's tip hash)")
    plan, seen = [], set()
    with open(groups, newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    if not rows:
        refuse("the groups file lists no file (a header alone stages nothing and writes no manifest)")
    for r in rows:
        group, run, name, label = r.get("group") or "", r.get("run") or "", r.get("file") or "", r.get("label") or ""
        if not GROUP_RX.fullmatch(group) or group in (".", ".."):
            refuse(f"group {group[:60]!r} is not a plain name (letters, digits, '_', '.', '-'; no path)")
        pn = pathlib.PurePosixPath(name)
        if not name or pn.is_absolute() or ".." in pn.parts or "\\" in name:
            refuse(f"{name[:80]!r} is not a name inside a run directory")
        if (group, name) in seen:
            refuse(f"{group}/{name} is requested twice")
        seen.add((group, name))
        adm = [a for a in recs if a.get("kind") == "admit" and a.get("run") == run]
        ends = [e for e in recs if e.get("kind") == "end" and e.get("run") == run]
        if len(adm) != 1 or len(ends) != 1:
            refuse(f"run {run[:40]} has not exactly one admit record and one end record in the ledger")
        a, e = adm[0], ends[0]
        if not isinstance(a.get("v"), int) or a["v"] < 6:
            refuse(f"run {run} was admitted by gate format {a.get('v')}, older than v6 (no reset under the lock, no "
                   "catalog or experiment-death check)")
        m = GATE_RX.fullmatch(str(e.get("gate") or ""))
        if not m or int(m.group(1)) < 6:
            refuse(f"run {run}'s end record was written by {str(e.get('gate'))[:40]}, not gate v6 or later")
        if e.get("verdict") != "VALID":
            refuse(f"run {run} is {e.get('verdict')}, not VALID")
        if not isinstance(e.get("seq"), int) or e["seq"] > to_seq:
            refuse(f"run {run} ended after the seal (end seq {e.get('seq')} > {to_seq}): its set was not cleared")
        rc = e.get("exp_rc")
        if not (isinstance(rc, int) and not isinstance(rc, bool) and rc >= 0):
            refuse(f"run {run}'s experiment did not exit by itself (exit status {rc!r}; < 0 is a signal)")
        commit = e.get("commit")
        if not (isinstance(commit, str) and COMMIT_RX.fullmatch(commit) and a.get("commit") == commit):
            refuse(f"run {run}'s admit and end records do not name one commit")
        listed = {x.get("name"): x.get("sha256") for x in e.get("files") or [] if isinstance(x, dict)}
        if name not in listed:
            refuse(f"{name} is not listed by run {run}'s end record (the gate did not observe it in the run)")
        raw = _read_regular(runs / run / name)
        if hashlib.sha256(raw).hexdigest() != listed[name]:
            refuse(f"{name} of run {run} differs from the SHA-256 its end record lists (changed after the run)")
        out = raw.replace(home.encode(), b"~/")
        if home.encode() in out:
            refuse(f"home path left in {name}")
        if os.path.lexists(dst / group / name):
            refuse(f"{group}/{name} is already staged: a staged file is never overwritten")
        plan.append((group, run, name, out, raw, commit, e["seq"], label))
    man = dst / MANIFEST
    old = []
    if os.path.lexists(man):
        lines = _read_regular(man).decode("utf-8").splitlines()
        if not lines or lines[0].split("\t") != HEADER:
            refuse(f"{MANIFEST} has another header: it is not this tool's")
        old = [ln.split("\t") for ln in lines[1:] if ln]
    dup = {(x[0], x[2]) for x in old if len(x) > 2} & {(p[0], p[2]) for p in plan}
    if dup:
        refuse(f"{len(dup)} requested file(s) are already in {MANIFEST} (e.g. {'/'.join(sorted(dup)[0])})")
    cdir = dst / "clearance" / s.name
    for n in CLEARANCE_FILES:
        if os.path.lexists(cdir / n) and _read_regular(cdir / n) != _read_regular(s / n):
            refuse(f"clearance/{s.name}/{n} is already committed with other content")
    for group, run, name, out, raw, commit, seq, label in plan:
        p = dst / group / name
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, out)
        finally:
            os.close(fd)
    cdir.mkdir(parents=True, exist_ok=True)
    for n in CLEARANCE_FILES:
        if not os.path.lexists(cdir / n):
            (cdir / n).write_bytes(_read_regular(s / n))
    rows = ["\t".join([group, run, name, commit, hashlib.sha256(raw).hexdigest(), hashlib.sha256(out).hexdigest(),
                       str(seq), label, s.name]) for group, run, name, out, raw, commit, seq, label in plan]
    new = not os.path.lexists(man)
    with open(man, "a") as f:
        f.write(("\t".join(HEADER) + "\n" if new else "") + "\n".join(rows) + "\n")
    print("files", len(rows), "set", s.name)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:5])
