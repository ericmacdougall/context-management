"""Public-surface scanner (tests only). Enumerates every importable public module of a package, every public class, function and
method defined there, and inspects their signatures. A violation is reported when:

  S1  a safety-relevant dependency (directory, clock, policy source, session authority, audit log, store, resolver, vault...) is a
      parameter with a default value, so a caller can leave it out;
  S2  a parameter lets the caller supply what must come from the runtime: identity, tenant, clock, labels of stored data, the
      recipient's attributes, or a switch that turns a check off (trusted, now, principal, viewer, recipient, flags, disabled, ...);
  S3  a parameter is **kwargs (anything can be smuggled through it);
  S4  an unscoped read path is exported: a name in UNSCOPED_READS, or a read-like callable whose first parameter is not `session`;
  S5  a module under the package imports a tests-only or experiment module (AST check, so it holds even for code that is never run).

Private names (leading underscore) and private modules are not part of the surface. Python cannot stop in-process code from reaching
private names; this scan enforces what the package EXPORTS and what its modules IMPORT, which is the trust boundary the deployment
wiring and every request handler sits behind."""
from __future__ import annotations

import ast
import importlib
import inspect
import os
import pkgutil
import re

SAFETY_PARAMS = {"directory", "clock", "policy_source", "sessions", "authority", "audit", "audit_log", "log", "store", "resolver",
                 "vault", "keyring", "enforcer", "sink", "decisions"}
FORBIDDEN_PARAMS = {"trusted", "now", "principal", "viewer", "recipient", "tenant", "flags", "disabled", "enqueue_only", "check_lineage",
                    "derive_mode", "mode", "resolve", "clock_override", "skip", "unsafe", "actor", "requester"}
UNSCOPED_READS = {"current_table", "read_state", "log_rows", "state", "dump", "records", "snapshot_plain"}
READ_RE = re.compile(r"^(read|explain|table|retrieve|view|show|values|history|candidates)(_|$)")
TEST_ONLY_MODULES = ("tests", "exp", "testkit")


def _public_modules(pkg_name):
    """(module or None, name, path). A module that cannot be imported here (a missing third-party driver) is scanned from its AST."""
    pkg = importlib.import_module(pkg_name)
    mods = [(pkg, pkg_name, getattr(pkg, "__file__", None))]
    for info in pkgutil.iter_modules(pkg.__path__, pkg_name + "."):
        if any(part.startswith("_") for part in info.name.split(".")):
            continue
        path = os.path.join(info.module_finder.path, info.name.rsplit(".", 1)[-1] + ".py")
        try:
            mods.append((importlib.import_module(info.name), info.name, path))
        except ImportError:
            mods.append((None, info.name, path))
    return mods


def _ast_scan(modname, path, out):
    """Fallback for a module that does not import here: the same rules over the AST (module-level defs and class methods)."""
    tree = ast.parse(open(path, encoding="utf-8").read(), path)

    def sig(where, fn, is_method):
        a = fn.args
        pos = a.posonlyargs + a.args
        defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
        params = list(zip([x.arg for x in pos], defaults)) + [(x.arg, d) for x, d in zip(a.kwonlyargs, a.kw_defaults)]
        if is_method and params and params[0][0] in ("self", "cls"):
            params = params[1:]
        if a.kwarg is not None:
            out.append(("S3", where, a.kwarg.arg))
        for name, d in params:
            if name in FORBIDDEN_PARAMS:
                out.append(("S2", where, name))
            if name in SAFETY_PARAMS and d is not None:
                out.append(("S1", where, name))
        nm = where.rsplit(".", 1)[-1]
        if READ_RE.match(nm) and (not params or params[0][0] != "session"):
            out.append(("S4", where, "first parameter is not session"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            if node.name in UNSCOPED_READS:
                out.append(("S4", modname + "." + node.name, "unscoped read exported"))
            sig(modname + "." + node.name, node, False)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and (m.name == "__init__" or not m.name.startswith("_")):
                    static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in m.decorator_list)
                    if m.name in UNSCOPED_READS:
                        out.append(("S4", modname + "." + node.name + "." + m.name, "unscoped read exported"))
                    sig(modname + "." + node.name + "." + m.name, m, not static)


def _exported(mod):
    names = getattr(mod, "__all__", None)
    if names is None:
        names = [n for n in dir(mod) if not n.startswith("_")]
    out = []
    for n in names:
        obj = getattr(mod, n, None)
        if obj is None or not (inspect.isclass(obj) or inspect.isfunction(obj)):
            continue
        if getattr(obj, "__module__", None) != mod.__name__:
            continue                                   # re-exported from elsewhere: checked where it is defined
        out.append((n, obj))
    return out


def _check_sig(where, fn, out, is_method=False):
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return
    params = list(sig.parameters.values())
    if is_method and params and params[0].name in ("self", "cls"):
        params = params[1:]
    for p in params:
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            out.append(("S3", where, p.name))
        if p.name in FORBIDDEN_PARAMS:
            out.append(("S2", where, p.name))
        if p.name in SAFETY_PARAMS and p.default is not inspect.Parameter.empty:
            out.append(("S1", where, p.name))
    name = where.rsplit(".", 1)[-1]
    if READ_RE.match(name) and (not params or params[0].name != "session"):
        out.append(("S4", where, "first parameter is not session"))


def scan(pkg_name):
    """Every violation of S1-S5 in the package, as (rule, where, detail) tuples."""
    out = []
    for mod, modname, path in _public_modules(pkg_name):
        if mod is None:
            _ast_scan(modname, path, out)
            continue
        for n, obj in _exported(mod):
            where = mod.__name__ + "." + n
            if n in UNSCOPED_READS:
                out.append(("S4", where, "unscoped read exported"))
            if inspect.isfunction(obj):
                _check_sig(where, obj, out)
                continue
            if "__init__" in obj.__dict__ or not any("__init__" in b.__dict__ for b in obj.__mro__[1:-1]):
                _check_sig(where + ".__init__", obj.__init__, out, is_method=True)
            for mn, m in inspect.getmembers(obj):
                if mn.startswith("_") or mn not in obj.__dict__:
                    continue
                raw = obj.__dict__[mn]
                f = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
                if not inspect.isfunction(f):
                    continue
                if mn in UNSCOPED_READS:
                    out.append(("S4", where + "." + mn, "unscoped read exported"))
                _check_sig(where + "." + mn, f, out, is_method=not isinstance(raw, staticmethod))
    out += import_violations(pkg_name)
    return sorted(set(out))


def import_violations(pkg_name):
    """S5: no module of the package (private ones included) imports a tests-only or experiment module."""
    pkg = importlib.import_module(pkg_name)
    out = []
    for root in pkg.__path__:
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dp, fn)
                tree = ast.parse(open(path, encoding="utf-8").read(), path)
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        names = [node.module]
                    for nm in names:
                        if nm.split(".")[0] in TEST_ONLY_MODULES or nm.split(".")[-1] in ("testkit", "kit"):
                            out.append(("S5", os.path.relpath(path, root), nm))
    return out
