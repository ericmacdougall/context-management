"""The public surface of `pol` (R3-9: behaviour, not only names). tests/surface.py describes the three layers (static, manifest, probe).

Controls (each must be caught, otherwise the layer is vacuous):
- PLANTED: one static violation of each kind S1-S5;
- EVASIVE: the round-3 reviewer's evasive package, verbatim (agent-a73fe3f60fa983697/probes/p8_rescue.py G1): re-exports from a
  private module, renamed parameters, a dynamic import of tests.kit, an unscoped table read. The static layer must report each entry;
- BEHAVING: the same shapes with names chosen so that NO static rule fires, but whose stubs decide, mint a session, leak seeded data
  and send. The behaviour layer must report each one."""
import importlib
import json
import os
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from surface import class_methods, exported, internals, manifest, probe, scan  # noqa: E402

MANIFEST = os.path.join(HERE, "surface_manifest.json")

PLANTED = {
    "__init__.py": "",
    "a.py": textwrap.dedent('''
        class Enforcer:
            def __init__(self, policy_source, directory=None):
                pass
            def check(self, session, action, trusted=None):
                pass
        def decide(principal, action, **kw):
            pass
        def explain(state, req):
            pass
        def read_value(state, req):
            pass
        def current_table(state):
            pass
    '''),
    "b.py": "from tests.kit import FixedClock\n",
    "_private.py": "def anything(principal, directory=None, flags=()):\n    pass\n",
}

EVASIVE = {
    "__init__.py": "from ._impl import evaluate, Gate\n",
    "_impl.py": textwrap.dedent('''
        def evaluate(principal, action, labels, now, recipient, trusted=True, **kw):
            return True
        class Gate:
            def __init__(self, directory=None, clock=None):
                pass
    '''),
    "mint.py": textwrap.dedent('''
        import importlib
        class Mint:
            def issue(self, principal_id, channel):
                pass
            def decide_for(self, principal_id, at_time, label_override, recipient_attrs, bypass=False):
                pass
        def load_helpers():
            return importlib.import_module("tests.kit")
        def get_table(state):
            return state
    '''),
}

BEHAVING = {
    "__init__.py": "from ._impl import assess, Door\n",
    "_impl.py": textwrap.dedent('''
        class _D:
            allow = True
        def assess(who, what):
            return _D()
        class Door:
            def __init__(self, x):
                self.x = x
            def open_for(self, who):
                return True
    '''),
    "mint2.py": textwrap.dedent('''
        class _S:
            def __init__(self, p):
                self.principal_id, self.token = p, "t"
        class Press:
            def make(self, who):
                return _S(who)
        def snapshot_of(thing):
            return [getattr(thing, "_recs", None), getattr(thing, "_vals", None), repr(vars(thing)) if hasattr(thing, "__dict__") else None]
        def relay(a, b):
            if callable(b):
                b("payload")
            return None
    '''),
}

# Value types and wiring whose parameter names match a static rule. Each is justified; the list must not go stale.
ALLOWED = {
    ("S2", "pol.directory.Principal.__init__", "tenant"): "directory entry value; built on the admin path (Directory.put), read by id only",
    ("S2", "pol.directory.Principal.__init__", "kind"): "directory entry value (as above)",
    ("S2", "pol.directory.Session.__init__", "tenant"): "inert value: every guard verifies a Session by identity with its authority; "
                                                        "the probe passes a hand-built Session to every callable",
    ("S2", "pol.directory.Session.__init__", "principal_id"): "as above",
    ("S2", "pol.directory.Session.__init__", "channel"): "as above",
    ("S2", "pol.directory.Directory.lookup", "pid"): "deployment wiring held by the operator; returns a directory entry, no record or vault "
                                                      "data (probe B3); never consulted for identity without a verified session",
    ("S2", "pol.directory.Directory.remove", "pid"): "admin wiring (deprovisioning); removal can only deny",
    ("S2", "pol.identity.Assertion.__init__", "principal_id"): "the identity adapter's VERIFIED output; SessionAuthority accepts it only "
                                                               "as the adapter's return value for a credential",
    ("S2", "pol.identity.Assertion.__init__", "channel"): "as above (the channel is bound at credential enrolment)",
    ("S2", "pol.enforce.Record.__init__", "tenant"): "stored record value; Store fills it from the verified principal",
    ("S2", "pol.enforce.GuardedEmbedder.embed", "kind"): "the embedding kind ('document' or 'query'), not a principal kind",
    ("S2", "pol.pgcore.PgEngine.__init__", "tenant"): "deployment wiring; DecisionService picks the engine by session.tenant",
    ("S2", "pol.policy.Authorization.__init__", "principal"): "policy-document value type (who an authorisation names)",
    ("S2", "pol.policy.Authorization.__init__", "tenant"): "policy-document value type",
    ("S2", "pol.policy.Dest.__init__", "kind"): "policy-document value type (destination kind)",
    ("S2", "pol.pgcore.anchor_export", "tenant"): "operator path on an OWNER connection (dc_app cannot read the chain); writes chain heads only",
    ("S2", "pol.pgcore.anchor_check", "tenant"): "operator path on an OWNER connection; returns hashes and counts, no record data",
    ("S4", "pol.pgcore.Anchor.records", "unscoped read exported"): "anchor records are (tenant, n, chain hash, utc): no record data",
    ("S4", "pol.pgcore.FileAnchor.records", "unscoped read exported"): "as above",
}

INERT = {"pol.directory.Session"}

# An __all__ that hides a public name, an exported partial and a callable instance, and a module-level __getattr__.
EVASIVE2 = {
    "__init__.py": "",
    "m.py": textwrap.dedent('''
        import functools
        __all__ = []
        def decide_now(principal, action):
            return True
        def _g(principal, action):
            return True
        quick = functools.partial(_g, "admin")
        class _Caller:
            def __call__(self, principal_id):
                return True
        gate = _Caller()
        def __getattr__(name):
            return _g
    '''),
}

# Private callables that, probed with forged inputs, decide, accept or return stored data. Python cannot stop in-process code from
# calling them; each is reached in the package only after a verified session (or is a pure helper of such a path). A NEW entry fails
# the test until reviewed. The enforced boundary against a hostile in-process caller is the database role (R3-7), not this list.
TRUSTED_INTERNALS = {
    ("B1", "pol._pdp._revoked"): "PDP helper; evaluate() is called only by enforce._decide_verified",
    ("B1", "pol.labels._acl_satisfied"): "PDP helper (pure label/group predicate)",
    ("B1", "pol._core._adopted_open"): "pure reducer predicate over proposal dicts (True: nothing closed it); takes no identity",
    ("B1", "pol.enforce.Revocations._is_revoked"): "predicate; True means DENY (revoked)",
    ("B1", "pol.enforce.AuditLog._snapshot"): "lab/test access to past audit rows (rows record allow=True decisions already made)",
    ("B1", "pol.decisions.DecisionService._readable"): "called by explain/table after principal_of(session)",
    ("B1", "pol.decisions.MemoryEngine._is_admin"): "called by DecisionService after principal_of(session)",
    ("B1", "pol.decisions.MemoryEngine._participant"): "called by DecisionService after principal_of(session)",
    ("B1", "pol.decisions.MemoryEngine._submit"): "the in-memory core: DecisionService builds the event from the verified principal; the "
                                                  "Postgres engine is reached only through definer functions (R3-7)",
    ("B1", "pol.decisions.Vault._put"): "returns True on store; called by DecisionService.submit after principal_of(session)",
    ("B3", "pol.enforce.Store._get"): "raw fetch; every caller checks the record's label for the verified principal before text leaves",
    ("B3", "pol.enforce.Store._sources"): "fetch-then-check: every caller runs Enforcer._check on the returned labels before using text",
}


def _plant(files, name):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, name))
    for fn, src in files.items():
        with open(os.path.join(d, name, fn), "w") as fh:
            fh.write(src)
    sys.path.insert(0, d)
    return d


def _world():
    """A seeded world (a record and a proposal carrying markers), forged inputs only, and recording sinks."""
    from pol.directory import Session
    from pol.enforce import GuardedChat, GuardedEmbedder, GuardedExport, Outbox
    from tests.kit import L, LabIdP, T, World
    w = World()
    sent = []
    rid = w.store.append(w.s("alice"), "MARK-REC-7f3", L())
    assert w.propose(w.s("alice"), "p1", "MARK-VAL-9c1", subjects=["dana"])["code"] == "proposed"

    class Inner:
        def complete(self, msgs, system=None):
            sent.append("chat")
            return type("R", (), {"text": "x", "model": "gpt-x@1"})()

        def embed(self, texts, kind):
            sent.append("embed")
            return [[0.0]]

    def sink(*a):
        sent.append("sink")

    instances = [w.enf, w.store, w.svc, w.sessions, w.dir, w.audit, w.vault, w.engine, w.enf._revocations,
                 Outbox(w.enf, w.store, "send_inbox", sink), GuardedChat(Inner(), w.enf, w.store, "cloud"),
                 GuardedEmbedder(Inner(), w.enf, w.store, "emb"), GuardedExport(sink, w.enf, w.store, "exp")]
    pool = ["admin", "alice", T, "r1", "read", "send_inbox", "cloud", rid, [rid], Session(T, "admin", "owner_ui", "forged-token"),
            Session(T, "alice", "owner_ui", "forged-token"), w.dir.lookup("admin"),
            {"principal": "admin", "tenant": T, "channel": "owner_ui", "principal_id": "admin", "type": "propose", "req": "r1"},
            L(), LabIdP().credential("admin", "owner_ui"), None, True, w.store, w.vault, w.engine, sink,
            {"type": "propose", "id": "alice/zz", "idem": "zz", "tenant": T, "principal": "alice", "channel": "owner_ui", "req": "r1",
             "value_ref": "v", "label": L().to_dict(), "based_on": 0}]
    identities = [x for x in pool if isinstance(x, (Session, type(w.dir.lookup("admin")))) or x in ("admin", "alice")]
    return w, instances, pool, ["MARK-REC-7f3", "MARK-VAL-9c1", "dana"], sent, identities


class Surface(unittest.TestCase):
    def test_static_rules(self):
        found = scan("pol")
        v = [x for x in found if x not in ALLOWED]
        self.assertEqual(v, [], "public-surface violations:\n" + "\n".join("%s %s %s" % x for x in v))
        self.assertEqual(sorted(set(ALLOWED) - set(found)), [], "stale allow-list entries")

    def test_manifest_is_pinned(self):
        """A new, removed or renamed exported callable or parameter fails until tests/surface_manifest.json is updated in a reviewed
        diff (POL_WRITE_MANIFEST=1 regenerates it)."""
        cur = manifest("pol")
        if os.environ.get("POL_WRITE_MANIFEST") == "1":
            with open(MANIFEST, "w") as fh:
                json.dump(cur, fh, indent=1, sort_keys=True)
                fh.write("\n")
        with open(MANIFEST) as fh:
            self.assertEqual(cur, json.load(fh))

    def test_behaviour_no_decision_read_or_send_without_a_session(self):
        """Every exported callable (constructors included) and every public method of every live object of a seeded world is called with
        forged inputs only. None may return an allowing decision, a session, seeded record/value/subject data, or reach a sink."""
        _w, instances, pool, markers, sent, ids = _world()
        targets = [(where, obj) for where, obj in exported("pol") if not where.startswith("pol.pgcore")]   # pgcore: tests/test_pg.py
        for inst in instances:
            cls = type(inst)
            for mn, _f, _st in class_methods(cls, "pol"):
                if mn != "__init__":
                    targets.append(("%s.%s.%s" % (cls.__module__, cls.__name__, mn), getattr(inst, mn)))
        self.assertGreater(len(targets), 60)
        from pol import _core
        v = probe(targets, pool, markers, sent, inert=INERT, identities=ids, accept_codes=_core.APPLIED)
        self.assertEqual(v, [], "\n".join("%s %s %s" % x for x in v))

    def test_internals_that_decide_are_pinned(self):
        """Private callables are probed too (R3-9 follow-up): the set that decides on forged input must equal TRUSTED_INTERNALS."""
        from pol import _core
        w, instances, pool, markers, sent, ids = _world()
        targets = internals("pol", instances, skip_modules=("pol.pgcore",))
        self.assertGreater(len(targets), 50)
        got = {(r, wh) for r, wh, _x in probe(targets, pool + [w.enf, w.svc, w.dir.lookup("alice")], markers, sent, inert=INERT,
                                              identities=ids, accept_codes=_core.APPLIED)}
        self.assertEqual(sorted(got - set(TRUSTED_INTERNALS)), [], "new in-process decider: review it and pin it with a reason")
        self.assertEqual(sorted(set(TRUSTED_INTERNALS) - got), [], "stale TRUSTED_INTERNALS entry")

    def test_known_in_process_deciders_do_decide(self):
        """Disclosure, not a guarantee: these private callables allow for a caller-supplied principal (the random probe rarely builds
        their argument shapes, so they are called directly). They are why the in-process boundary is trusted, not enforced."""
        from pol import _pdp, enforce
        from tests.kit import L, T
        w = _world()[0]
        p = w.dir.lookup("alice")
        self.assertTrue(_pdp.evaluate(p, "read", [L()], {}, w.pol, 0.0, None).allow)
        self.assertTrue(enforce._decide_verified(w.enf, p, "read", [L()], {})[0].allow)
        ev = {"type": "propose", "id": "alice/zz", "idem": "zz", "tenant": T, "principal": "alice", "channel": "owner_ui", "req": "r1",
              "value_ref": "v", "label": L().to_dict(), "based_on": 0}
        self.assertEqual(w.engine._submit(ev)[0], "proposed")

    def test_control_hidden_names_partials_and_module_getattr(self):
        _plant(EVASIVE2, "evade2_pkg")
        v = set(scan("evade2_pkg"))
        want = {("S2", "evade2_pkg.m.decide_now", "principal"), ("S6", "evade2_pkg.m.quick", "exported callable object"),
                ("S2", "evade2_pkg.m.gate", "principal_id"), ("S6", "evade2_pkg.m.gate", "exported callable object"),
                ("S6", "m.py", "module __getattr__")}
        self.assertEqual(sorted(want - v), [], "scanner missed")

    def test_control_planted(self):
        _plant(PLANTED, "planted_pkg")
        v = scan("planted_pkg")
        rules = {(r, w.split(".")[-1] if r != "S5" else w, x) for r, w, x in v}
        want = {("S1", "__init__", "directory"), ("S2", "check", "trusted"), ("S2", "decide", "principal"), ("S3", "decide", "kw"),
                ("S4", "explain", "first parameter is not session"), ("S4", "read_value", "first parameter is not session"),
                ("S5", "b.py", "tests.kit"), ("S4", "current_table", "unscoped read exported")}
        self.assertEqual(sorted(want - rules), [], "scanner missed")
        self.assertFalse(any("_private" in w for r, w, _x in v if r != "S5"), "a private module must not count as surface")

    def test_control_reviewers_evasive_package(self):
        _plant(EVASIVE, "evade_pkg")
        v = set(scan("evade_pkg"))
        want = {("S2", "evade_pkg.evaluate", "principal"), ("S2", "evade_pkg.evaluate", "trusted"), ("S2", "evade_pkg.evaluate", "now"),
                ("S2", "evade_pkg.evaluate", "recipient"), ("S3", "evade_pkg.evaluate", "kw"), ("S1", "evade_pkg.Gate.__init__", "directory"),
                ("S1", "evade_pkg.Gate.__init__", "clock"), ("S2", "evade_pkg.mint.Mint.issue", "principal_id"),
                ("S2", "evade_pkg.mint.Mint.issue", "channel"), ("S2", "evade_pkg.mint.Mint.decide_for", "principal_id"),
                ("S2", "evade_pkg.mint.Mint.decide_for", "at_time"), ("S2", "evade_pkg.mint.Mint.decide_for", "label_override"),
                ("S2", "evade_pkg.mint.Mint.decide_for", "recipient_attrs"), ("S2", "evade_pkg.mint.Mint.decide_for", "bypass"),
                ("S4", "evade_pkg.mint.get_table", "unscoped read exported"), ("S6", "mint.py", "importlib"),
                ("S6", "mint.py", "import_module")}
        self.assertEqual(sorted(want - v), [], "scanner missed")

    def test_control_behaving_package(self):
        _plant(BEHAVING, "behave_pkg")
        importlib.import_module("behave_pkg.mint2")
        self.assertEqual(scan("behave_pkg"), [], "this control must be invisible to the static rules")
        _w, _inst, pool, markers, sent, ids = _world()
        from behave_pkg import Door
        from behave_pkg.mint2 import Press
        targets = list(exported("behave_pkg")) + [("behave_pkg.Door.open_for", Door(1).open_for), ("behave_pkg.mint2.Press.make", Press().make)]
        got = {(r, wh) for r, wh, _x in probe(targets, pool, markers, sent, inert=INERT, identities=ids)}
        want = {("B1", "behave_pkg.assess"), ("B1", "behave_pkg.Door.open_for"), ("B2", "behave_pkg.mint2.Press.make"),
                ("B3", "behave_pkg.mint2.snapshot_of"), ("B4", "behave_pkg.mint2.relay")}
        self.assertEqual(sorted(want - got), [], "probe missed")

    def test_ast_fallback_agrees_with_runtime_enumeration(self):
        """Review-3 N7: a module that does not import (pol.pgcore without psycopg) is enumerated from its AST. At 0a20b64 the AST path
        listed only explicitly written methods, so a dataclass-generated __init__ (PgSession, Session, Principal, ...) vanished and
        the pinned manifest depended on whether the driver was installed. For every module that DOES import here, the AST enumeration
        must equal the runtime one for the callables defined in that module."""
        import inspect
        from surface import _ast_defs, _params_from_sig, public_modules
        diffs = []
        for mod, modname, path in public_modules("pol"):
            if mod is None or not path or not path.endswith(".py"):
                continue
            rt = {}
            for where, obj in exported("pol"):
                if where.rsplit(".", 1)[0] != modname or getattr(obj, "__module__", None) != modname:
                    continue
                if inspect.isfunction(obj):
                    rt[where] = _params_from_sig(obj, False)
                elif inspect.isclass(obj):
                    for mn, f, static in class_methods(obj, "pol"):
                        if mn in vars(obj):          # defined on this class, written or generated (not inherited)
                            rt[where + "." + mn] = _params_from_sig(f, not static)
            st = {w: p for w, p in _ast_defs(modname, path)}
            for k in sorted(set(rt) | set(st)):
                if rt.get(k) != st.get(k):
                    diffs.append("%s runtime=%s ast=%s" % (k, rt.get(k), st.get(k)))
        self.assertEqual(diffs, [], "AST fallback differs from the runtime enumeration:\n" + "\n".join(diffs))


if __name__ == "__main__":
    unittest.main()
