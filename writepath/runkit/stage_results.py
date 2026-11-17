"""Copy lab results (already copied from the host, after leak_scan.py found no secret value) into writepath/results/.

The host home prefix is shortened to '~/' in every copy, so no username or host home path is committed. Writes
MANIFEST.tsv: group, file, code_commit, sha256 of the host copy (the hash the RESULT lines print and the file name's
16-hex suffix), sha256 of the committed copy, label. Prints counts only.
usage: HOST_HOME=<host home with trailing slash> python3 runkit/stage_results.py <groups.tsv> <results copy> <ab/res copy> <dst>
groups.tsv columns: group, source (results | ab), file, label."""
import csv
import hashlib
import json
import os
import pathlib
import sys


def main(groups, results, ab, dst):
    home = os.environ.get("HOST_HOME", "")
    if not home.endswith("/") or len(home) < 3:
        sys.exit("HOST_HOME must name the host home directory with a trailing slash")
    src = {"results": pathlib.Path(results), "ab": pathlib.Path(ab)}
    dst = pathlib.Path(dst)
    rows = []
    with open(groups) as f:
        for r in csv.DictReader(f, delimiter="\t"):
            raw = (src[r["source"]] / r["file"]).read_bytes()
            out = raw.replace(home.encode(), b"~/")
            if home.encode() in out:
                sys.exit(f"home path left in {r['file']}")
            (dst / r["group"]).mkdir(parents=True, exist_ok=True)
            (dst / r["group"] / r["file"]).write_bytes(out)
            commit = json.loads(raw).get("env", {}).get("code_commit", "") if r["file"].endswith(".json") else ""
            rows.append("\t".join([r["group"], r["file"], str(commit), hashlib.sha256(raw).hexdigest(),
                                   hashlib.sha256(out).hexdigest(), r["label"]]))
    (dst / "MANIFEST.tsv").write_text("group\tfile\tcode_commit\tsha256_original\tsha256_committed\tlabel\n" + "\n".join(rows) + "\n")
    print("files", len(rows))


if __name__ == "__main__":
    main(*sys.argv[1:5])
