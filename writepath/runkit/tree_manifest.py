"""The manifest of a pinned tree (GATE-DESIGN 8.2 P4), built from a commit's git objects, never from a working tree.

  build COMMIT            print the manifest of the current directory's tree (writepath/) at COMMIT
  ship COMMIT OUT.tar     write that tree (without the top-level results/ directory) as a tar, with TREE.manifest
                          and GIT_HEAD (the commit's first 12 hex digits): what sync.sh copies to the lab host
  check COMMIT SHA256     exit 0 when the manifest of COMMIT has that SHA-256 (an end record's `manifest` field), else 1

Format (the gate reads it, exp/disk_gate.py _manifest): "tree-manifest v1", "commit <40 hex>", then one line per file,
sorted by path: "<sha256 of the content> <git mode> <path>"; a link (mode 120000) is hashed over its target. Only the
top-level results/ directory is left out. Run from writepath/ in the repository. Prints no file content."""
import hashlib
import io
import subprocess
import sys
import tarfile

EXCLUDE_TOP = ("results",)


def git(*args, input=None):
    r = subprocess.run(["git", *args], capture_output=True, input=input)
    if r.returncode != 0:
        raise SystemExit(f"git {args[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return r.stdout


def entries(commit):
    """(full commit id, [(path, mode, content)]) of the current directory's tree at commit."""
    full = git("rev-parse", "--verify", commit + "^{commit}").decode().strip()
    prefix = git("rev-parse", "--show-prefix").decode().strip()
    rows = []
    for rec in git("ls-tree", "--full-tree", "-r", "-z", f"{full}:{prefix}").split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, typ, oid = meta.decode().split()
        p = path.decode("utf-8", "surrogateescape")
        if p.split("/")[0] in EXCLUDE_TOP:
            continue
        if typ != "blob" or mode not in ("100644", "100755", "120000") or "\n" in p:
            raise SystemExit(f"refusing: {p!r} is a {typ} of mode {mode} (only files and links can be pinned)")
        rows.append((p, mode, oid))
    out = git("cat-file", "--batch", input="".join(o + "\n" for _, _, o in rows).encode())
    res, pos = [], 0
    for p, mode, oid in rows:
        head_end = out.index(b"\n", pos)
        _, _, size = out[pos:head_end].decode().split()
        start = head_end + 1
        res.append((p, mode, out[start:start + int(size)]))
        pos = start + int(size) + 1
    return full, sorted(res)


def manifest(full, files):
    return ("tree-manifest v1\ncommit %s\n" % full +
            "".join(f"{hashlib.sha256(c).hexdigest()} {m} {p}\n" for p, m, c in files))


def enc(text):
    return text.encode("utf-8", "surrogateescape")


def ship(full, files, out):
    when = int(git("show", "-s", "--format=%ct", full).decode().strip())
    extra = [("TREE.manifest", "100644", enc(manifest(full, files))), ("GIT_HEAD", "100644", enc(full[:12] + "\n"))]
    with tarfile.open(out, "x") as t:
        for p, m, c in files + extra:
            ti = tarfile.TarInfo(p)
            ti.mtime = when
            if m == "120000":
                ti.type, ti.linkname, ti.mode = tarfile.SYMTYPE, c.decode("utf-8", "surrogateescape"), 0o777
                t.addfile(ti)
            else:
                ti.size, ti.mode = len(c), 0o755 if m == "100755" else 0o644
                t.addfile(ti, io.BytesIO(c))


def main(cmd="", commit="", arg=""):
    if cmd not in ("build", "ship", "check") or not commit or (cmd != "build" and not arg):
        raise SystemExit(__doc__)
    full, files = entries(commit)
    text = manifest(full, files)
    got = hashlib.sha256(enc(text)).hexdigest()
    if cmd == "build":
        sys.stdout.write(text)
    elif cmd == "ship":
        ship(full, files, arg)
        print(f"shipped {len(files)} files of {full[:12]} manifest sha256 {got}")
    else:
        print(f"manifest of {full[:12]}: sha256 {got} ({'equal' if got == arg else 'DIFFERENT'})")
        sys.exit(0 if got == arg else 1)


if __name__ == "__main__":
    main(*sys.argv[1:4])
