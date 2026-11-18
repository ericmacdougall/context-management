"""Stage lab results (copied from the host after leak_scan.py found no secret value) into writepath/results/, only as
the gate recorded them (GATE-DESIGN 8.2 P8): a file is staged only when a VALID end record of the gate's ledger lists
that file of that run with that SHA-256. The ledger copy is verified first: its hash chain, and its head anchor
(stop_ledger.head, copied with it) for the last record. Any refusal stages nothing.

The host home prefix is shortened to '~/' in every copy, so no username or host home path is committed. Writes
MANIFEST.tsv: group, run, file, code_commit, sha256 of the host copy (the hash the end record lists), sha256 of the
committed copy, the end record's seq, label. Prints counts and reasons only.
usage: HOST_HOME=<host home with trailing slash> python3 runkit/stage_results.py <groups.tsv> <runs copy> <ledger copy> <dst>
  <runs copy>: a copy of WP_HOME/results/runs (one directory per run id); <ledger copy>: a copy of
  WP_HOME/state/stop_ledger.jsonl with stop_ledger.head next to it.
groups.tsv columns: group, run, file (relative to the run's directory), label."""
import csv
import hashlib
import json
import os
import pathlib
import sys


def refuse(msg):
    print(f"stage_results: refused: {msg}; nothing was staged")
    sys.exit(2)


def ledger(path):
    """The verified records of a ledger copy (up to its head anchor's count)."""
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
    return recs[:n]


def main(groups, runs, ledger_copy, dst):
    home = os.environ.get("HOST_HOME", "")
    if not home.endswith("/") or len(home) < 3:
        sys.exit("HOST_HOME must name the host home directory with a trailing slash")
    recs = ledger(ledger_copy)
    runs, dst = pathlib.Path(runs), pathlib.Path(dst)
    plan = []
    with open(groups) as f:
        for r in csv.DictReader(f, delimiter="\t"):
            run, name = r["run"], r["file"]
            if pathlib.PurePosixPath(name).is_absolute() or ".." in pathlib.PurePosixPath(name).parts:
                refuse(f"{name!r} is not a name inside a run directory")
            ends = [e for e in recs if e.get("kind") == "end" and e.get("run") == run]
            adm = [a for a in recs if a.get("kind") == "admit" and a.get("run") == run and a.get("v", 0) >= 5]
            if not adm or len(ends) != 1:
                refuse(f"run {run} has no v5 admit record or not exactly one end record in the ledger")
            e = ends[0]
            if e.get("verdict") != "VALID":
                refuse(f"run {run} is {e.get('verdict')}, not VALID")
            listed = {x.get("name"): x.get("sha256") for x in e.get("files") or []}
            if name not in listed:
                refuse(f"{name} is not listed by run {run}'s end record (the gate did not observe it in the run)")
            raw = (runs / run / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != listed[name]:
                refuse(f"{name} of run {run} differs from the SHA-256 its end record lists (changed after the run)")
            out = raw.replace(home.encode(), b"~/")
            if home.encode() in out:
                refuse(f"home path left in {name}")
            commit = json.loads(raw).get("env", {}).get("code_commit", "") if name.endswith(".json") else ""
            plan.append((r["group"], run, name, out, raw, commit, e["seq"], r["label"]))
    rows = []
    for group, run, name, out, raw, commit, seq, label in plan:
        p = dst / group / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(out)
        rows.append("\t".join([group, run, name, str(commit), hashlib.sha256(raw).hexdigest(),
                               hashlib.sha256(out).hexdigest(), str(seq), label]))
    (dst / "MANIFEST.tsv").write_text("group\trun\tfile\tcode_commit\tsha256_original\tsha256_committed\tend_seq\tlabel\n"
                                      + "\n".join(rows) + "\n")
    print("files", len(rows))


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:5])
