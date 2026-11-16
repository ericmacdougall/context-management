"""Public-surface checks (tests only). Three layers, because a name-based scan alone is weak (round-3 review R3-9):

1. STATIC rules over EVERY exported name of every public module, wherever it is defined (re-exports from private modules are scanned
   in the exporting module), and over every method a public class exposes, including methods inherited from private bases:
     S1  a safety dependency (directory, clock, policy source, sessions, audit, store, vault, ...) is a parameter with a default;
     S2  a parameter lets the caller supply identity, tenant, channel, clock, labels of stored data, recipient attributes, or a switch
         that turns a check off (principal, principal_id, channel, now, at_time, trusted, bypass, label_override, ...);
     S3  **kwargs;
     S4  an unscoped read path is exported (a known unscoped name, or a read-like name whose first parameter is not `session`);
     S5  any module of the package (private ones included) imports tests-only or experiment code;
     S6  any module of the package (private ones included) imports dynamically or executes code (importlib, __import__, exec, eval,
         compile), which would hide an import from S5 or a callable from this scan.
2. MANIFEST: the full signature of every exported callable is pinned in tests/surface_manifest.json. Any new, removed or renamed
   parameter or callable fails until the manifest is updated deliberately (a reviewed diff).
3. BEHAVIOUR (probe): every exported callable is CALLED with forged inputs (bare principal ids, a hand-built Session, a Principal object,
   identity dicts, flags) against a seeded world, and must not return a decision that allows, a session, or data from the seeded
   records and values, and must not call a sink, model or export function. See tests/test_surface.py.

4. INTERNALS (probe_internals): every PRIVATE function and method of every module is probed the same way. Python cannot stop
   in-process code from calling a private name, so those that do decide on a caller-supplied principal are pinned, with a reason, in
   TRUSTED_INTERNALS (tests/test_surface.py): a new one fails the test. The enforced boundary against a hostile in-process caller is
   the database role (R3-7), not this package.

Enumeration is the union of __all__ and dir() (an __all__ cannot hide a public name); exported callable instances and partials count;
a module-level __getattr__ (names that dir() cannot see) is an S6 violation."""
from __future__ import annotations

import ast
import importlib
import inspect
import itertools
import os
import pkgutil
import random
import re

SAFETY_PARAMS = {"directory", "clock", "policy_source", "sessions", "authority", "audit", "audit_log", "log", "store", "resolver",
                 "vault", "keyring", "enforcer", "sink", "decisions", "identity", "idp", "revocations"}
FORBIDDEN_PARAMS = {"trusted", "now", "principal", "principal_id", "pid", "viewer", "recipient", "recipient_attrs", "tenant", "channel",
                    "flags", "disabled", "enqueue_only", "check_lineage", "derive_mode", "mode", "resolve", "clock_override", "skip",
                    "unsafe", "actor", "requester", "at_time", "label_override", "labels_override", "bypass", "override", "as_principal",
                    "impersonate", "user", "user_id", "actor_id", "kind", "trust_origin", "role_override", "admin"}
UNSCOPED_READS = {"current_table", "read_state", "log_rows", "state", "dump", "records", "snapshot_plain", "get_table"}
READ_RE = re.compile(r"^(read|explain|table|retrieve|view|show|values|history|candidates|get|list|dump|fetch|load|rows)(_|$)")
DYNAMIC_BUILTINS = {"__import__", "exec", "eval", "compile"}          # bare-name calls (re.compile is not a dynamic import)
DYNAMIC_ATTRS = {"import_module", "__import__", "spec_from_file_location", "run_path", "load_source", "exec_module"}
TEST_ONLY_MODULES = ("tests", "exp", "testkit")


# ------------------------------------------------------------------------------------------------ enumeration
def public_modules(pkg_name):
    """(module or None, name, path) for the package and every public submodule (None: it does not import here; AST is used)."""
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


def _pkg_of(obj, pkg_name):
    m = getattr(obj, "__module__", None) or ""
    return m == pkg_name or m.startswith(pkg_name + ".")


def exported(pkg_name):
    """[(where, obj)] for every public function and class exported by a public module, re-exports included."""
    out = []
    for mod, modname, _path in public_modules(pkg_name):
        if mod is None:
            continue
        names = sorted(set(getattr(mod, "__all__", None) or []) | {n for n in dir(mod) if not n.startswith("_")})
        for n in names:
            obj = getattr(mod, n, None)
            if obj is None or inspect.ismodule(obj):
                continue
            if not (inspect.isclass(obj) or inspect.isfunction(obj)):
                inner = getattr(obj, "func", None) or type(obj)          # functools.partial, or an instance with __call__
                if callable(obj) and _pkg_of(inner, pkg_name):
                    out.append((modname + "." + n, obj))
                continue
            if not _pkg_of(obj, pkg_name):
                continue
            home = obj.__module__
            if home != modname and not any(part.startswith("_") for part in home.split(".")):
                continue        # re-export of a PUBLIC module's object: scanned once, at home. Re-exports from private modules count here.
            out.append((modname + "." + n, obj))
    return out


def class_methods(cls, pkg_name):
    """[(name, function, is_static)] for __init__ and every public method the class exposes, inherited ones included, when defined in
    the package (inherited object/typing internals are not the package's surface)."""
    out, seen = [], set()
    for base in cls.__mro__:
        if not _pkg_of(base, pkg_name):
            continue
        for mn, raw in base.__dict__.items():
            if mn in seen or (mn.startswith("_") and mn != "__init__"):
                continue
            f = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
            if not inspect.isfunction(f) or not _pkg_of(f, pkg_name):
                continue
            seen.add(mn)
            out.append((mn, f, isinstance(raw, staticmethod)))
    return out


# ------------------------------------------------------------------------------------------------ layer 1: static
def _params_from_sig(fn, is_method):
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    ps = list(sig.parameters.values())
    if is_method and ps and ps[0].name in ("self", "cls"):
        ps = ps[1:]
    return [(p.name, p.default is not p.empty, p.kind is p.VAR_KEYWORD) for p in ps]


def _rules(where, params, out):
    for name, has_default, varkw in params:
        if varkw:
            out.append(("S3", where, name))
        if name in FORBIDDEN_PARAMS:
            out.append(("S2", where, name))
        if name in SAFETY_PARAMS and has_default:
            out.append(("S1", where, name))
    short = where.rsplit(".", 1)[-1]
    if short in UNSCOPED_READS:
        out.append(("S4", where, "unscoped read exported"))
    elif READ_RE.match(short) and (not params or params[0][0] != "session"):
        out.append(("S4", where, "first parameter is not session"))


def _ast_params(fn, is_method):
    a = fn.args
    pos = a.posonlyargs + a.args
    nd = len(pos) - len(a.defaults)
    ps = [(x.arg, i >= nd, False) for i, x in enumerate(pos)] + [(x.arg, d is not None, False) for x, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg is not None:
        ps.append((a.kwarg.arg, False, True))
    if is_method and ps and ps[0][0] in ("self", "cls"):
        ps = ps[1:]
    return ps


def _ast_defs(modname, path):
    """[(where, params)] for public module-level defs and public class methods of a module that does not import here."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            out.append((modname + "." + node.name, _ast_params(node, False)))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and (m.name == "__init__" or not m.name.startswith("_")):
                    static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in m.decorator_list)
                    out.append((modname + "." + node.name + "." + m.name, _ast_params(m, not static)))
    return out


def signatures(pkg_name):
    """{where: [(param, has_default, is_varkw), ...]} for every exported callable (layer 2 uses the same enumeration)."""
    sigs = {}
    for mod, modname, path in public_modules(pkg_name):
        if mod is None:
            for where, ps in _ast_defs(modname, path):
                sigs[where] = ps
    for where, obj in exported(pkg_name):
        if not inspect.isclass(obj) and not inspect.isfunction(obj):
            try:
                ps = [(q.name, q.default is not q.empty, q.kind is q.VAR_KEYWORD) for q in inspect.signature(obj).parameters.values()]
            except (TypeError, ValueError):
                ps = [("<opaque callable>", False, True)]
            sigs[where] = ps
            continue
        if inspect.isfunction(obj):
            ps = _params_from_sig(obj, False)
            if ps is not None:
                sigs[where] = ps
            continue
        for mn, f, static in class_methods(obj, pkg_name):
            ps = _params_from_sig(f, not static)
            if ps is not None:
                sigs[where + "." + mn] = ps
    return sigs


def scan(pkg_name):
    """Every S1-S6 violation, as sorted (rule, where, detail) tuples."""
    out = [("S6", where, "exported callable object") for where, obj in exported(pkg_name)
           if not inspect.isclass(obj) and not inspect.isfunction(obj)]          # a partial or instance can carry a bound identity
    for where, ps in signatures(pkg_name).items():
        _rules(where, ps, out)
    out += source_violations(pkg_name)
    return sorted(set(out))


def source_violations(pkg_name):
    """S5 and S6 over the AST of every module of the package, private ones included."""
    pkg = importlib.import_module(pkg_name)
    out = []
    for root in pkg.__path__:
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dp, fn)
                rel = os.path.relpath(path, root)
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), path)
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        names = [node.module]
                    for nm in names:
                        if nm.split(".")[0] in TEST_ONLY_MODULES or nm.split(".")[-1] in ("testkit", "kit"):
                            out.append(("S5", rel, nm))
                        if nm.split(".")[0] == "importlib":
                            out.append(("S6", rel, nm))
                    if isinstance(node, ast.FunctionDef) and node.name == "__getattr__" and node in tree.body:
                        out.append(("S6", rel, "module __getattr__"))
                    if isinstance(node, ast.Call):
                        f = node.func
                        if (isinstance(f, ast.Name) and f.id in DYNAMIC_BUILTINS) or (isinstance(f, ast.Attribute) and f.attr in DYNAMIC_ATTRS):
                            out.append(("S6", rel, f.id if isinstance(f, ast.Name) else f.attr))
    return out


def manifest(pkg_name):
    """{where: "param[=], ...; **kw"} (layer 2)."""
    return {w: ", ".join(("**" if v else "") + n + ("=" if d else "") for n, d, v in ps) for w, ps in sorted(signatures(pkg_name).items())}


# ------------------------------------------------------------------------------------------------ layer 3: behaviour
DECISION_RE = re.compile(r"(allow|check|decide|evaluat|authori[sz]|permit|verify|grant|admit|approve)", re.I)


def _flat(x, depth=0):
    yield x
    if depth < 2 and isinstance(x, (tuple, list)):
        for y in x:
            yield from _flat(y, depth + 1)


def observe(where, ret, markers, sent, inert=(), identity_arg=False, accept_codes=(), pool_ids=None):
    """Violations shown by one call's result. `inert`: callables allowed to return a session-shaped object because it is a hand-built
    value that every guard rejects (the Session dataclass itself; tests prove the rejection with the same forged Session)."""
    v = []
    pool_ids = pool_ids or set()
    for x in _flat(ret):
        if getattr(x, "allow", None) is True or (isinstance(x, dict) and x.get("allow") is True):
            v.append(("B1", where, "returned an allowing decision"))
        if hasattr(x, "token") and hasattr(x, "principal_id") and where not in inert and id(x) not in pool_ids:
            v.append(("B2", where, "returned a session"))
    head = ret[0] if isinstance(ret, tuple) and ret else (ret.get("code") if isinstance(ret, dict) else None)
    if isinstance(head, str) and head in accept_codes:
        v.append(("B1", where, "accepted a forged event: " + head))
    if ret is True and (identity_arg or DECISION_RE.search(where.rsplit(".", 1)[-1])):
        v.append(("B1", where, "returned True for a forged identity" if identity_arg else "decision-shaped callable returned True"))
    try:
        r = repr(ret)
    except Exception:       # noqa: BLE001
        r = ""
    for m in markers:
        if m in r:
            v.append(("B3", where, "returned seeded data " + m))
    if sent:
        v.append(("B4", where, "called a sink, model or export function"))
    return v


def probe(targets, pool, markers, sent, combos=120, seed=7, inert=(), identities=(), accept_codes=()):
    """Call each (where, fn) with forged argument combinations drawn from `pool`; return the sorted set of violations.
    `sent` is a list that recording sinks append to; it is cleared before each call. `identities`: the pool members that stand for a
    caller's identity (bare ids, a hand-built Session, a Principal); a True returned for a call that passed one is a decision."""
    rng = random.Random(seed)
    out = set()
    pool_ids = {id(x) for x in pool}          # echoing a forged input back is not minting it
    for where, fn in targets:
        try:
            ps = [p for p in inspect.signature(fn).parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        except (TypeError, ValueError):
            continue
        n_req = len([p for p in ps if p.default is p.empty])
        tries = []
        for n in sorted({n_req, len(ps)}):
            if n > 8:
                continue
            tries += [[x] * n for x in pool]
            tries += [[rng.choice(pool) for _ in range(n)] for _ in range(combos)]
            if n <= 3:
                tries += [list(t) for t in itertools.islice(itertools.product(pool, repeat=n), 2000)]
        for args in tries:
            sent.clear()
            try:
                ret = fn(*args)
            except Exception:       # noqa: BLE001  refusing is the expected outcome
                ret = None
            ident = any(a is i for a in args for i in identities)
            out.update(observe(where, ret, markers, list(sent), inert, ident, accept_codes, pool_ids))
    return sorted(out)


def internals(pkg_name, instances, skip_modules=()):
    """[(where, callable)] for every module-level function of a private module, every private module-level function of a public one,
    and every private method of the given live instances (dunders excluded)."""
    pkg = importlib.import_module(pkg_name)
    out = []
    for info in pkgutil.walk_packages(pkg.__path__, pkg_name + "."):
        if info.name in skip_modules:
            continue
        mod = importlib.import_module(info.name)
        for n, obj in vars(mod).items():
            private_mod = any(part.startswith("_") for part in info.name.split("."))
            if (private_mod or n.startswith("_")) and not n.startswith("__") and inspect.isfunction(obj) and obj.__module__ == info.name:
                out.append((info.name + "." + n, obj))
    for inst in instances:
        cls = type(inst)
        for base in cls.__mro__:
            if not _pkg_of(base, pkg_name):
                continue
            for n, raw in vars(base).items():
                if n.startswith("_") and not n.startswith("__") and inspect.isfunction(raw):
                    out.append(("%s.%s.%s" % (cls.__module__, cls.__name__, n), getattr(inst, n)))
    return sorted(set(out), key=lambda x: x[0])
