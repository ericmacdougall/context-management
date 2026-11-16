"""Controls are broken IMPLEMENTATIONS: a one-patch source variant of a production module, built here and never shipped.

variant(module_name, old, new) reads the production source, asserts `old` occurs exactly once, replaces it with `new`, and executes
the result as a fresh module object in package `pol` (relative imports resolve to the real sibling modules). The production module is
not modified. Each control in CONTROLS names the finding it reintroduces."""
import importlib
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def variant(module_name, old, new, tag):
    path = os.path.join(ROOT, *module_name.split(".")) + ".py"
    src = open(path, encoding="utf-8").read()
    n = src.count(old)
    if n != 1:
        raise AssertionError("patch %s: expected exactly one match in %s, found %d" % (tag, module_name, n))
    importlib.import_module(module_name)
    name = module_name + "__ctl_" + tag
    mod = types.ModuleType(name)
    mod.__file__ = path + "#" + tag
    mod.__package__ = module_name.rsplit(".", 1)[0]
    sys.modules[name] = mod
    exec(compile(src.replace(old, new), mod.__file__, "exec"), mod.__dict__)
    return mod


CONTROLS = {
    # N3 reintroduced: a non-human reject moves the head like any decision.
    "n3_agent_reject_moves_head": ("pol._core", "    if t == \"decide\" and verb == \"reject\" and not human:\n",
                                   "    if False:\n"),
    # N3 reintroduced in its value-unchanged form: a non-human accept of the current value moves the head (amendment 4).
    "n3_value_unchanged_moves_head": ("pol._core", "        if not human and verb == \"accept\" and h[\"status\"] == \"current\" and eff[1] == h[\"value_ref\"]:\n",
                                      "        if False:\n"),
    # N2 reintroduced: no open-proposal cap.
    "n2_no_cap": ("pol._core", "        if view.open_count(req, pid) >= MAX_OPEN_PER_PRINCIPAL or view.open_total(req) >= MAX_OPEN_PER_REQ:\n",
                  "        if False:\n"),
    # N2 reintroduced: proposing needs no role.
    "n2_no_role": ("pol._core", "        if not (view.has_authority(req, pid) or view.is_proposer(req, pid)):\n            return \"unauthorized\", []\n", ""),
    # Retry outcome lost (M2): a duplicate does not return the original code.
    "m2_no_original": ("pol._core", "        return (\"duplicate\" if old[0] == fp else \"idem_conflict\"), old[1], []\n",
                       "        return (\"duplicate\" if old[0] == fp else \"idem_conflict\"), None, []\n"),
    # Stale binding not checked (lost update).
    "stale_unchecked": ("pol._core", "    if based != h[\"version\"]:\n        last = h[\"last\"]\n",
                        "    if False:\n        last = h[\"last\"]\n"),
    # Agent may supersede (humans-only rule dropped).
    "agent_may_supersede": ("pol._core", "    if not human and (t in (\"supersede\", \"retract\") or verb in (\"amend\", \"resolve\")):\n",
                            "    if not human and verb in (\"amend\", \"resolve\"):\n"),
    # H8 reintroduced: the recipient used for the send is looked up again after the decision.
    "h8_second_lookup": ("pol.enforce", "                self._sink(sender, rcp, [r.text for r in recs], dict(it[\"request\"]))",
                         "                self._sink(sender, self._enf._dir.lookup(it[\"recipient_id\"]), [r.text for r in recs], dict(it[\"request\"]))"),
    # (Removed after the dev runs: "h8_no_reread" was vacuous, as its setdefault snapshot was never filled; E4 uses the
    # enqueue_only variant from exp/e245_kit.py instead.)
    # H2 reintroduced: unknown credential keys ignored.
    "h2_ignore_unknown_keys": ("pol._pdp", "        if not isinstance(k, str) or not k.isascii() or k not in CREDENTIAL_KEYS:\n            _deny(\"attr_unrecognised_key\")\n", "        pass\n"),
    # (Removed after the dev runs: "m1_body_identity" was masked by the BODY_KEYS check; E3 uses its two-fragment BODY_TRUST variant.)
    # H7 reintroduced: hidden-candidate flag always present.
    "h7_flag_always": ("pol.decisions", "        if notice:\n            out[\"hidden_candidates\"] = hidden\n", "        out[\"hidden_candidates\"] = hidden\n"),
    # INFORMATIONAL ONLY (vacuous by construction: explain checks _readable before _value is reached); E5 uses its no_read_check.
    "h7_unscoped_value": ("pol.decisions", "        return {\"ref\": ref, \"text\": v[\"text\"], \"erased\": False} if self._readable(session, v[\"label\"]) else None",
                          "        return {\"ref\": ref, \"text\": v[\"text\"], \"erased\": False}"),
}


def control(tag):
    mod, old, new = CONTROLS[tag]
    return variant(mod, old, new, tag)


def check_all_apply():
    """Every control patch applies exactly once against the current source (run by the experiment preflight)."""
    return {tag: control(tag).__name__ for tag in CONTROLS}
