"""Run a module of this pinned tree in an isolated interpreter (review aacf3c0 m-G).

usage: python -I -B <tree>/runkit/isolated.py MODULE [ARGS]

`python -I` ignores every PYTHON* variable and the user's site directory and puts neither the working directory nor
the script's directory on sys.path, so a module of the tree would not import. This file puts exactly the tree it
lives in on sys.path, first, and runs MODULE as __main__ with ARGS. Refuses (exit 2) without -I and -B, and a MODULE
that is not a dotted name of a module file in this tree. The venv's own site-packages (and its .pth files) still
load: GATE-DESIGN 9 LIMITS."""
import pathlib
import re
import runpy
import sys

TREE = pathlib.Path(__file__).resolve().parents[1]


def main():
    if not (sys.flags.isolated and sys.dont_write_bytecode):
        print("isolated.py: refused: run as python -I -B (isolated, no bytecode)")
        sys.exit(2)
    if len(sys.argv) < 2 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", sys.argv[1]):
        print("isolated.py: refused: usage: python -I -B runkit/isolated.py MODULE [ARGS]")
        sys.exit(2)
    mod = sys.argv[1]
    rel = pathlib.Path(*mod.split("."))
    if not ((TREE / rel).with_suffix(".py").is_file() or (TREE / rel / "__main__.py").is_file()):
        print(f"isolated.py: refused: {mod} is not a module of this tree")
        sys.exit(2)
    sys.path.insert(0, str(TREE))
    sys.argv = [mod] + sys.argv[2:]
    runpy.run_module(mod, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
