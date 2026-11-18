"""The sink's fences (GATE-DESIGN 7.6 and 7.8, review F scanner half, option 2). Two checks over this tree by parsing:
every secret creation site that SITES marks register=True calls secret_sink.register in its function, and no code
writes into results/ or logs/ except through secret_sink.write. Both are EXPECTED FAILURES on this branch: the calls
belong in the hardening owner's files (hand-off H-1 to H-5), and the owner removes each mark when its calls land, after
which any regression fails. The positive controls below are not marked: they show each fence finds what it claims.
Limits of the write fence: it follows names (RESULTS, LOG_DIR and locals assigned from them in the same function);
a path built elsewhere and passed in is not seen.
usage (from writepath/): python3 -m unittest tests.test_result_sink_fence -v"""
import ast
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "runkit"))
import leak_scan  # noqa: E402

TARGETS = {"RESULTS", "LOG_DIR"}
WRITE_ATTRS = {"write_text", "write_bytes", "touch", "symlink_to", "hardlink_to", "rename", "replace"}


def _refs(node, tainted):
    return any((isinstance(n, ast.Name) and (n.id in TARGETS or n.id in tainted)) or
               (isinstance(n, ast.Attribute) and n.attr in TARGETS) for n in ast.walk(node))


def _mode(call):
    m = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    return m.value if isinstance(m, ast.Constant) and isinstance(m.value, str) else ("?" if m is not None else "r")


class _Writes(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel, self.stack, self.tainted, self.out = rel, [], [set()], []

    def _scope(self, n):
        self.stack.append(n.name)
        self.tainted.append(set())
        self.generic_visit(n)
        self.tainted.pop()
        self.stack.pop()
    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def visit_Assign(self, n):
        if _refs(n.value, self.tainted[-1]):
            for t in n.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        self.tainted[-1].add(x.id)
        self.generic_visit(n)

    def visit_Call(self, n):
        f, t = n.func, self.tainted[-1]
        hit = None
        if isinstance(f, ast.Attribute) and f.attr in WRITE_ATTRS and _refs(f.value, t):
            hit = f.attr
        elif isinstance(f, ast.Attribute) and f.attr == "open" and _refs(f.value, t) and set(_mode(n)) & set("wax+?"):
            hit = "open"
        elif isinstance(f, ast.Name) and f.id == "open" and n.args and _refs(n.args[0], t) and set(_mode(n)) & set("wax+?"):
            hit = "open"
        elif (isinstance(f, ast.Attribute) and f.attr in ("open", "copy", "copy2", "copyfile", "copytree", "move")
              and isinstance(f.value, ast.Name) and f.value.id in ("os", "shutil") and
              any(_refs(a, t) for a in n.args[1 if f.value.id == "shutil" else 0:])):
            hit = f"{f.value.id}.{f.attr}"
        elif isinstance(f, (ast.Attribute, ast.Name)) and getattr(f, "attr", getattr(f, "id", "")) == "FileHandler" \
                and any(_refs(a, t) for a in n.args):
            hit = "FileHandler"
        if hit:
            self.out.append(f"{self.rel} {'.'.join(self.stack) or '<module>'} {hit}")
        self.generic_visit(n)


def direct_writes(root):
    out = []
    for f, rel in leak_scan._site_files(root):
        if rel == "wp/secret_sink.py":
            continue
        v = _Writes(rel)
        v.visit(ast.parse(f.read_bytes(), rel))
        out += v.out
    return out


class Fence(unittest.TestCase):
    @unittest.expectedFailure
    def test_every_secret_creation_site_registers_with_the_sink(self):
        # H-1 to H-4: remove this mark when the hardening owner's calls land
        self.assertEqual(leak_scan.unregistered(HERE), [], "creation sites that do not register their value")

    @unittest.expectedFailure
    def test_no_result_or_log_file_is_written_outside_the_sink(self):
        # H-5: remove this mark when exp/common.py write_result (and the other writers listed) use secret_sink.write
        self.assertEqual(direct_writes(HERE), [], "direct writes into results/ or logs/")

    # ---- positive controls (not marked): each fence finds what it claims, on copies of this tree
    def mirror(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for f in HERE.rglob("*.py"):
            rel = f.relative_to(HERE)
            if rel.parts[0] not in ("tests", "results"):
                (d / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, d / rel)
        return d

    def test_registration_fence_sees_a_register_call(self):
        d = self.mirror()
        before = leak_scan.unregistered(d)
        self.assertTrue(any(x.startswith("wp/setup.py _ensure_roles ") for x in before))
        p = d / "wp" / "setup.py"
        line = "pw = secrets.token_urlsafe(32)"
        src = p.read_text()
        indent = src.split(line)[0].rsplit("\n", 1)[1]
        p.write_text(src.replace(line, line + f"\n{indent}secret_sink.register(pw, 'PG login role password')", 1)
                     .replace("import secrets", "import secrets\nfrom wp import secret_sink", 1))
        after = leak_scan.unregistered(d)
        self.assertFalse(any(x.startswith("wp/setup.py _ensure_roles ") for x in after), after)
        self.assertEqual(len(after), len(before) - 1)

    def test_write_fence_sees_direct_writes_and_passes_the_sink(self):
        d = self.mirror()
        found = direct_writes(d)
        self.assertIn("exp/common.py write_result write_bytes", found, "the known direct result writer was not found")
        (d / "exp" / "zz_fence.py").write_text(
            "from wp import config, secret_sink\nfrom exp.common import RESULTS\n\n\n"
            "def bad():\n    p = RESULTS / 'a.json'\n    with open(p, 'w') as f:\n        f.write('x')\n\n\n"
            "def bad_log():\n    (config.LOG_DIR / 'x.log').write_text('x')\n\n\n"
            "def good():\n    secret_sink.write(RESULTS / 'b.json', b'x')\n\n\n"
            "def reads():\n    return open(RESULTS / 'c.json').read()\n")
        found = direct_writes(d)
        self.assertIn("exp/zz_fence.py bad open", found)
        self.assertIn("exp/zz_fence.py bad_log write_text", found)
        self.assertFalse([x for x in found if " good " in x or " reads " in x], found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
