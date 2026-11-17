"""Build the matcher's Unicode data tables OUTSIDE the repository (review C class 4; D3 design). Run with python3 -I.

  confusables <confusables.txt> <out dir>   parse UTS 39 confusables.txt (read as data only) into
                                            confusables-<version>.tsv: "<source hex>\t<target hex ...>" per line
  ignorables <out dir>                      enumerate Default_Ignorable_Code_Point from the host perl's Unicode data
                                            (Unicode::UCD; its version must equal Python's unicodedata version) into
                                            default_ignorable-<version>.tsv: one hex code point per line

Each output's sha256 is printed; wp/mentions.py refuses any table whose sha256 is not pinned there. The tables are not
vendored in this public repository: the Unicode License V3 permits redistribution only with its copyright and
permission notice (https://www.unicode.org/license.txt), so the lab keeps them in its own data directory."""
import hashlib
import pathlib
import re
import subprocess
import sys
import unicodedata


def _write(out, name, lines):
    p = pathlib.Path(out) / name
    p.write_text("".join(x + "\n" for x in lines))
    print(p.name, hashlib.sha256(p.read_bytes()).hexdigest(), len(lines))


def confusables(src, out):
    text = pathlib.Path(src).read_text(encoding="utf-8-sig")
    m = re.search(r"^# Version: (\d+\.\d+\.\d+)$", text, re.M)
    if not m:
        raise SystemExit("no version line")
    rows = []
    for ln in text.splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        f = [x.strip() for x in ln.split(";")]
        if len(f) < 3 or not re.fullmatch(r"[0-9A-F]{4,6}", f[0]) or not re.fullmatch(r"[0-9A-F]{4,6}( [0-9A-F]{4,6})*", f[1]):
            raise SystemExit(f"unexpected line: {ln[:60]!r}")
        rows.append(f"{f[0]}\t{f[1]}")
    _write(out, f"confusables-{m.group(1)}.tsv", sorted(set(rows)))


def ignorables(out):
    v = subprocess.run(["perl", "-MUnicode::UCD", "-e", "print Unicode::UCD::UnicodeVersion()"], capture_output=True, text=True, check=True).stdout.strip()
    if v != unicodedata.unidata_version:
        raise SystemExit(f"perl Unicode {v} != python unicodedata {unicodedata.unidata_version}")
    cps = subprocess.run(["perl", "-e", 'for my $c (0..0x10FFFF) { next if $c >= 0xD800 && $c <= 0xDFFF; '
                          'printf "%04X\\n", $c if chr($c) =~ /\\p{Default_Ignorable_Code_Point}/ }'],
                         capture_output=True, text=True, check=True).stdout.split()
    _write(out, f"default_ignorable-{v}.tsv", cps)


if __name__ == "__main__":
    if sys.argv[1] == "confusables":
        confusables(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "ignorables":
        ignorables(sys.argv[2])
    else:
        raise SystemExit(__doc__)
