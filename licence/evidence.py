"""Typed, immutable evidence parsed once from a life result file (DESIGN-VERDICTS.md).

A life file is untrusted input. Each evidence type declares, as data, every raw path it reads (SPEC: field -> Field(path, kind or nested
evidence type)) and every cross-field relation (RELATIONS). `parse()` reads exactly those paths, checks every leaf against its kind's domain
(KINDS) and every relation, and returns a frozen object or raises Unparsed(path, reason). Verdicts (verdicts.py) accept only these objects.

- HTTP statuses become a closed Outcome case; no verdict sees an integer status.
- Rates exist only as Proportion(numerator, denominator); a stored derived rate is a `verify` field: checked against its counts, then
  dropped, so no verdict can read it.
- A control is an evidence type whose fields are `enforce`d: its constructor raises unless the control observation is what it must be.
- What is guaranteed (review 8 F15): validated construction from JSON under trusted Python code. parse() and the generated dataclass
  constructors check every domain, control and relation, and nested evidence by exact type. Every stored value is immutable and owned
  by the evidence object (review 9 F20): ints, finite floats, bools, strs, enum strings, Outcomes, nested evidence, and windows copied
  into tuples of numbers. No kind stores a dict or list from the input (the former `text` kind, which kept an error body by reference,
  is removed), so mutating the source after parsing cannot change evidence.
  Evidence classes use __slots__ (no per-instance __dict__). Python still cannot stop trusted code from bypassing a frozen dataclass
  (object.__setattr__, copy tricks); the verdict modules are checked by an AST test to touch nothing but evidence objects.
- Malformed input never raises out of parse(): input-related arithmetic, type and value errors (a 10**400 integer, a nested wrong type)
  become Unparsed at the parsing boundary (review 8 F14).
- Mappings with a fixed key set (probes, battery paths, migrated operations) are declared one field per key: a missing key does not parse.
"""
import collections
import dataclasses
import json
import math
import re

MISSING = object()


class Unparsed(Exception):
    """The evidence does not parse: the verdict is UNVERIFIED with this reason."""

    def __init__(self, path, reason):
        self.path, self.reason = tuple(path), reason
        super().__init__("%s: %s" % (".".join(str(p) for p in self.path) or "<life>", reason))


# ---- kinds: the domain of every leaf field ----------------------------------------------------------------------------------------------
Kind = collections.namedtuple("Kind", "type lo hi doc")
KINDS = {
    "status": Kind("status", 0, 599, "HTTP status: 0 (transport failure) or 100..599, parsed into an Outcome case"),
    "count": Kind("int", 0, None, "integer count >= 0"),
    "pos": Kind("int", 1, None, "integer count >= 1 (an observation that must have happened)"),
    "rate": Kind("real", 0, 1, "finite fraction in [0, 1]"),
    "duration": Kind("real", 0, None, "finite seconds >= 0"),
    "magnitude": Kind("real", 0, None, "finite error magnitude >= 0"),
    "cosine": Kind("real", -1, 1, "finite cosine similarity in [-1, 1]"),
    "offset": Kind("real", None, None, "finite SIGNED seconds from a transition; lower bound per field or by relation"),
    "timestamp": Kind("real", 1, None, "finite epoch seconds"),
    "flag": Kind("bool", None, None, "true or false"),
    "cell_class": Kind("enum", None, None, "a matrix cell class: one of lib.classify's and matrix's labels"),
    "windows": Kind("windows", None, None, "list of [start, end, count]: finite, end >= start, count >= 1"),
    "message": Kind("message", None, None, "the adapter-visible error text: 'HTTP <ddd>: ' + at most 400 decoded bytes (<= 410 characters)"),
}


ENUMS = {"cell_class": frozenset({"AVAILABLE", "LICENCE_ERROR", "OTHER_ERROR", "TRANSPORT", "SKIP", "UNVERIFIED_PREREQUISITE", "EMPTY_WITNESS"})}


@dataclasses.dataclass(frozen=True)
class Field:
    path: tuple
    kind: object                 # a KINDS key, or an Evidence subclass for a nested object
    optional: bool = False       # absent is allowed (value None)
    expect: object = None        # intended observation: Outcome case names (status), int set (count), bool (flag)
    enforce: bool = False        # a control: parse fails unless the value is the intended observation
    verify: bool = False         # a stored derived figure: checked by RELATIONS, never exposed to verdicts
    lo: float = None             # per-field bounds narrowing the kind's domain
    hi: float = None
    text: str = None             # status fields: the sibling key holding the error body (licence-text classification)
    bad: tuple = ()              # in-domain values the verdict must reject (used by the generated tests)


def F(path, kind, **kw):
    return Field(tuple(path.split(".")), kind, **kw)


# ---- HTTP outcome: a closed set of cases -------------------------------------------------------------------------------------------
CASES = ("SUCCESS", "NOT_FOUND", "AUTHORISATION_REFUSAL", "LICENCE_REFUSAL", "AUTHENTICATION_FAILURE", "CLIENT_ERROR",
         "INFRASTRUCTURE_FAILURE", "TRANSPORT_FAILURE", "UNEXPECTED")
REFUSALS = frozenset({"AUTHORISATION_REFUSAL", "LICENCE_REFUSAL"})


def licence_text(body):
    """The registered licence signature (THRESHOLDS.md EXP1-T1, addendum C): the text "non-compliant". The earlier "license ... expired"
    branch was never registered; in committed rows it only ever matched Elasticsearch's `license.expired.feature` key (review 9 F18)."""
    txt = json.dumps(body, default=str).lower() if body else ""
    return "non-compliant" in txt


@dataclasses.dataclass(frozen=True)
class Outcome:
    """One closed case per status. A refusal is only ever a 403: LICENCE_REFUSAL is a 403 whose body carries the licence text,
    AUTHORISATION_REFUSAL a 403 without it. `licence_text` records the text on any status; only on a 403 does it change the case, and
    lib.classify labels LICENCE_ERROR only for LICENCE_REFUSAL. Verdicts match on `case` only."""
    status: int
    case: str
    licence_text: bool = False

    def __post_init__(self):
        if type(self.status) is not int or not (self.status == 0 or 100 <= self.status <= 599) or self.case not in CASES \
                or type(self.licence_text) is not bool:
            raise Unparsed((), "not an outcome: %r %r" % (self.status, self.case))
        if Outcome._case(self.status, self.licence_text) != self.case:
            raise Unparsed((), "status %d is not case %s" % (self.status, self.case))

    @staticmethod
    def of(status, body=None):
        if type(status) is not int or not (status == 0 or 100 <= status <= 599):
            raise Unparsed((), "HTTP status %r is not 0 or 100..599" % (status,))
        lt = body if type(body) is bool else licence_text(body)   # a recorded cell carries the licence text as a flag (observe.py)
        return Outcome(status, Outcome._case(status, lt), lt)

    @staticmethod
    def _case(status, lt):
        if status == 0:
            case = "TRANSPORT_FAILURE"
        elif 200 <= status < 300:
            case = "SUCCESS"
        elif status < 200 or 300 <= status < 400:
            case = "UNEXPECTED"
        elif status == 403:
            case = "LICENCE_REFUSAL" if lt else "AUTHORISATION_REFUSAL"
        elif status == 401:
            case = "AUTHENTICATION_FAILURE"
        elif status == 404:
            case = "NOT_FOUND"
        elif status < 500:
            case = "CLIENT_ERROR"
        else:
            case = "INFRASTRUCTURE_FAILURE"
        return case


@dataclasses.dataclass(frozen=True)
class Proportion:
    """numerator of denominator; the only way a rate exists."""
    numerator: int
    denominator: int

    def __post_init__(self):
        if not (type(self.numerator) is int and type(self.denominator) is int and self.denominator >= 1 and 0 <= self.numerator <= self.denominator):
            raise Unparsed((), "not a proportion: %r of %r" % (self.numerator, self.denominator))

    @property
    def rate(self):
        return self.numerator / self.denominator


# ---- leaf parsing ------------------------------------------------------------------------------------------------------------------
MAX_INT = 2 ** 53   # no recorded count, status or time exceeds it; a larger integer is not a measurement (and overflows float arithmetic)


def _int(v):
    return type(v) is int and -MAX_INT <= v <= MAX_INT


def _real(v):
    return _int(v) or (type(v) is float and math.isfinite(v))


def parse_leaf(f, v, parent, at):
    k = KINDS[f.kind]
    lo = f.lo if f.lo is not None else k.lo
    hi = f.hi if f.hi is not None else k.hi
    if k.type == "status":
        try:
            return Outcome.of(v, parent.get(f.text) if f.text else None)
        except Unparsed as e:
            raise Unparsed(at, e.reason)
    return _domain(f, v, at)


def _domain(f, v, at):
    """The value must lie in the field's domain (KINDS[kind] narrowed by the field's lo/hi); returns it unchanged."""
    k = KINDS[f.kind]
    lo = f.lo if f.lo is not None else k.lo
    hi = f.hi if f.hi is not None else k.hi
    if k.type == "status":
        ok = type(v) is Outcome
    elif k.type == "int":
        ok = _int(v)
    elif k.type == "real":
        ok = _real(v)
    elif k.type == "bool":
        ok = type(v) is bool
    elif k.type == "message":
        ok = type(v) is str and len(v) <= 410 and re.match(r"HTTP \d{3}: ", v) is not None   # "HTTP ddd: " + <= 400 chars (F24)
    elif k.type == "enum":
        ok = type(v) is str and v in ENUMS[f.kind]
    elif k.type == "windows":
        ok = type(v) in (list, tuple) and all(type(w) in (list, tuple) and len(w) == 3 and _real(w[0]) and _real(w[1]) and w[1] >= w[0]
                                              and _int(w[2]) and w[2] >= 1 for w in v)
    else:
        raise KeyError(f.kind)
    if not ok:
        raise Unparsed(at, "%r is not a %s (%s)" % (v, f.kind, k.doc))
    if k.type in ("int", "real") and (lo is not None and v < lo or hi is not None and v > hi):
        raise Unparsed(at, "%r outside [%s, %s] for %s" % (v, lo, hi, f.kind))
    if k.type == "windows":
        return tuple(tuple(w) for w in v)   # stored immutable
    return v


def check_value(f, x, at):
    """A constructed field value: nested evidence of the declared type, an Outcome for a status, else a value in its domain; an
    enforced (control) field must hold its intended observation."""
    if isinstance(f.kind, type):
        if type(x) is not f.kind:   # exact type: a subclass may override validation (review 8 F15)
            raise Unparsed(at, "expected %s evidence, got %s" % (f.kind.__name__, type(x).__name__))
        return
    _domain(f, x, at)
    if f.enforce:
        got = x.case if type(x) is Outcome else x
        want = f.expect if isinstance(f.expect, (set, frozenset)) else {f.expect}
        if got not in want:
            raise Unparsed(at, "control not established: %r, need %s" % (got, sorted(want, key=str)))


def _fetch(raw, path):
    for k in path:
        if not isinstance(raw, dict) or k not in raw:
            return MISSING
        raw = raw[k]
    return raw


def _parent(raw, path):
    p = _fetch(raw, path[:-1])
    return p if isinstance(p, dict) else {}


# ---- relations ---------------------------------------------------------------------------------------------------------------------
def _sum(vals, names):
    return sum(vals[n] for n in (names if isinstance(names, tuple) else (names,)))


def check_relation(rel, vals):
    """Raise Unparsed unless the relation holds; a relation naming an absent optional field is skipped."""
    names = [x for part in rel[1:] for x in (part if isinstance(part, tuple) else (part,))]
    if any(vals.get(n) is None for n in names):
        return
    kind = rel[0]
    if kind == "le":
        ok = _sum(vals, rel[1]) <= _sum(vals, rel[2])
    elif kind == "eq":
        ok = abs(vals[rel[1]] - vals[rel[2]]) <= 1e-9
    elif kind == "sum_eq":
        ok = _sum(vals, rel[1]) == vals[rel[2]]
    elif kind == "ratio_matches":   # stored derived rate == numerator / denominator (counts are the evidence)
        den = _sum(vals, rel[3])
        ok = den >= 1 and abs(vals[rel[1]] - _sum(vals, rel[2]) / den) <= 1e-9
    elif kind == "ge_diff":         # offset >= a - b (an answer cannot precede the transition request)
        ok = vals[rel[1]] >= vals[rel[2]] - vals[rel[3]]
    elif kind == "windows_match":   # windows, longest, failed: harness stores at most 10 windows
        w, longest, failed = vals[rel[1]], vals[rel[2]], vals[rel[3]]
        top = max([x[1] - x[0] for x in w] or [0.0])
        tot = sum(x[2] for x in w)
        ok = (abs(longest - top) <= 1e-9 and tot == failed) if len(w) < 10 else (longest >= top and tot <= failed)
    else:
        raise KeyError(kind)
    if not ok:
        raise Unparsed((), "relation %s does not hold: %s" % (rel, {n: vals[n] for n in names}))


# ---- evidence base -----------------------------------------------------------------------------------------------------------------
def _flat(vals):
    """Relation values: scalar fields by name, a status by its case, and one level of nested evidence as "field.attr" (so a stored
    summary can be related to the observation it summarises, e.g. first_refusal_s == first_refusal.at_s)."""
    out = {}
    for n, v in vals.items():
        if isinstance(v, Evidence):
            for k in v.SPEC:
                if not v.SPEC[k].verify and not isinstance(getattr(v, k), (Evidence, Outcome)):
                    out[n + "." + k] = getattr(v, k)
        else:
            out[n] = v.case if isinstance(v, Outcome) else v
    return out


class Evidence:
    """Base of every evidence type. The generated constructor checks every domain, control and relation over the stored fields and
    stores containers as tuples; parse() also checks the relations that involve a stored derived figure (a `verify` field), which is then
    dropped. This holds for construction from JSON and for ordinary construction by trusted Python code; it is not a defence against
    code that deliberately bypasses a frozen dataclass (see the module docstring)."""
    __slots__ = ()
    SPEC = {}
    RELATIONS = ()
    CHECKS = ()   # domain checks over several fields that are not a declared relation kind (functions of the field values)

    def __post_init__(self):
        vals = {}
        for n, f in self.SPEC.items():
            if f.verify:
                continue
            x = getattr(self, n)
            if x is None:
                if not f.optional:
                    raise Unparsed((n,), "absent")
            else:
                check_value(f, x, (n,))
                if not isinstance(f.kind, type) and KINDS[f.kind].type == "windows":
                    x = _domain(f, x, (n,))
                    object.__setattr__(self, n, x)
            vals[n] = x
        for rel in self.RELATIONS:
            check_relation(rel, _flat(vals))
        for chk in self.CHECKS:
            chk(vals)

    @classmethod
    def parse(cls, raw, at=()):
        try:
            return cls._parse(raw, at)
        except (ArithmeticError, TypeError, ValueError, RecursionError) as e:   # input-shaped errors become UNVERIFIED (review 8 F14)
            raise Unparsed(at, "malformed input: %s" % type(e).__name__)

    @classmethod
    def _parse(cls, raw, at=()):
        if not isinstance(raw, dict):
            raise Unparsed(at, "expected an object, got %s" % type(raw).__name__)
        vals = {}
        for name, f in cls.SPEC.items():
            p = at + f.path
            v = _fetch(raw, f.path)
            if v is MISSING or (v is None and f.optional):
                if not f.optional:
                    raise Unparsed(p, "absent")
                vals[name] = None
                continue
            if isinstance(f.kind, type):
                vals[name] = f.kind._parse(v, p)
                continue
            x = parse_leaf(f, v, _parent(raw, f.path), p)
            check_value(f, x, p)
            vals[name] = x
        try:
            for rel in cls.RELATIONS:
                check_relation(rel, _flat(vals))
            for chk in cls.CHECKS:
                chk(vals)
            return cls(**{n: v for n, v in vals.items() if not cls.SPEC[n].verify})
        except Unparsed as e:
            raise Unparsed(at + e.path, e.reason)


def leaf_paths(cls, prefix=()):
    """Every absolute life path an evidence type reads (stored derived `verify` figures included), nested types expanded. The single
    source for summarize's provenance and the review 10 completeness tests."""
    out = []
    for f in cls.SPEC.values():
        p = prefix + f.path
        out += leaf_paths(f.kind, p) if isinstance(f.kind, type) and issubclass(f.kind, Evidence) else [p]
    return out


def evidence(name, spec, relations=(), doc="", props=None, checks=()):
    """Declare an evidence type: a frozen dataclass whose fields are the non-`verify` SPEC entries."""
    fields = [(n, object) for n, f in spec.items() if not f.verify]
    ns = {"SPEC": dict(spec), "RELATIONS": tuple(relations), "CHECKS": tuple(checks), "__doc__": doc,
          "__slots__": tuple(n for n, f in spec.items() if not f.verify)}   # no per-instance __dict__ (review 8 F15)
    ns.update(props or {})
    return dataclasses.make_dataclass(name, fields, bases=(Evidence,), frozen=True, namespace=ns)


# ---- the evidence types ------------------------------------------------------------------------------------------------------------
T2_PROBES = ("rrf", "linear", "semantic_query", "inference_use", "dls_search")
BATTERY = ("get_allowed_doc0", "get_hidden_doc1", "search_match_all", "search_term_hidden", "count", "mget", "msearch", "scroll_open", "knn_search",
           "termvectors_hidden", "wildcard_index", "esql")
LATCH_CELLS = ("retriever_rrf", "retriever_linear", "text_similarity_reranker_gate")
MIG_INDICES = ("lic3-bbq_disk-mig", "lic3-sem-mig")
MIG_OPS = ("update", "delete", "force_merge", "index_new")
OK = {"SUCCESS"}

# EXP1-T2 (review 8 F13): a refusal timing exists only attached to the refusal Outcome it was selected by (observe.moments), and that Outcome
# must be the probe's required case. The legacy summary fields (first_refusal_rel_resp_s, first_refusal_status) are not read: a timing
# without its Outcome does not parse.
def _refusal_at(name, case):
    return evidence(name, {"outcome": F("status", "status", text="licence_text", expect={case}, enforce=True),
                           # the recorded licence-text flag is itself evidence: mandatory, bool, and the one the case requires
                           "licence_text": F("licence_text", "flag", expect=case == "LICENCE_REFUSAL", enforce=True),
                           "at_s": F("rel_resp_s", "offset", lo=-0.5)},   # observe.moments looks from t_resp - 0.5 s
                    doc="The first %s observed after start_basic and when (seconds from its response)." % case)


LicenceRefusalAt = _refusal_at("LicenceRefusalAt", "LICENCE_REFUSAL")
DlsRefusalAt = _refusal_at("DlsRefusalAt", "AUTHORISATION_REFUSAL")


def _moment_probe(name, at):
    return evidence(name, {"first_refusal": F("first_refusal", at),
                           "success_after_refusal": F("success_after_first_refusal", "count", expect={0}),
                           "n": F("n", "pos"),
                           "first_refusal_s": F("first_refusal_rel_resp_s", "offset", lo=-0.5, verify=True)},   # legacy summary: must agree
                    [("le", "success_after_refusal", "n"), ("eq", "first_refusal_s", "first_refusal.at_s")],
                    "One mandatory probe polled across start_basic. A skipped or never-refused probe has no first_refusal and does not parse.")


LicenceMomentProbe = _moment_probe("LicenceMomentProbe", LicenceRefusalAt)
DlsMomentProbe = _moment_probe("DlsMomentProbe", DlsRefusalAt)
Moment = evidence("Moment", {p: F("exp1_moment." + p, DlsMomentProbe if p == "dls_search" else LicenceMomentProbe) for p in T2_PROBES},
                  doc="All five mandatory probes (EXP1-T2).")

def _adapter_view(v):
    """EXP1-T3 (THRESHOLDS.md): the adapter's own detector decides. The recorded message must carry the cell's status, and the recorded
    detector result must equal what the REAL ElasticsearchSearch._is_licence_error says on that message (observe.adapter_detects)."""
    import observe
    if not v["message"].startswith("HTTP %d: " % v["outcome"].status):
        raise Unparsed(("adapter_message",), "message %r does not carry the cell's HTTP %d" % (v["message"][:40], v["outcome"].status))
    real = observe.adapter_detects(v["message"])
    if real != v["detects"]:
        raise Unparsed(("adapter_detects",), "recorded %r, but the adapter detector says %r on the recorded message" % (v["detects"], real))


# EXP1-T3 (review 9 F18): the adapter-visible message and the adapter detector's result, not the generic Outcome, decide; the Outcome
# stays a separate field. The error summary dict is not retained (F20).
LatchCell = evidence("LatchCell", {"outcome": F("status", "status", expect=REFUSALS),
                                   "message": F("adapter_message", "message", bad=("HTTP 403: forbidden",)),
                                   "detects": F("adapter_detects", "flag", expect=True)}, checks=(_adapter_view,))
Latch = evidence("Latch", {c: F("exp1_after." + c, LatchCell) for c in LATCH_CELLS}, doc="EXP1-T3: the three latch cells after start_basic.")

DlsControl = evidence("DlsControl", {
    "outcome": F("status", "status", expect=OK, enforce=True, text="error"),
    "only_kw_a": F("only_kw_a", "flag", expect=True, enforce=True),
    "returned": F("returned", "count", lo=1, hi=7)},
    doc="Positive restricted control on trial: the DLS user read some (1..7 of 8) documents, all permitted.")
DlsAfter = evidence("DlsAfter", {
    "outcome": F("status", "status", expect=REFUSALS, text="error"),
    "cell_class": F("class", "cell_class", bad=("UNVERIFIED_PREREQUISITE", "EMPTY_WITNESS", "SKIP"))})
Dls = evidence("Dls", {"control": F("exp1_trial.dls_search_as_user", DlsControl), "after": F("exp1_after.dls_search_as_user", DlsAfter)},
               doc="EXP1-T4.")

TrialRead = evidence("TrialRead", {"outcome": F("status", "status", expect=OK, enforce=True),
                                   "hidden": F("returned_hidden_doc", "flag", expect=False, enforce=True),
                                   "any_source": F("returned_any_source", "flag")})
TrialHiddenRead = evidence("TrialHiddenRead", {"outcome": F("status", "status", expect={"NOT_FOUND"}, enforce=True),
                                               "hidden": F("returned_hidden_doc", "flag", expect=False, enforce=True),
                                               "any_source": F("returned_any_source", "flag", expect=False, enforce=True)})
BatteryTrial = evidence("BatteryTrial", dict({p: F(p, TrialHiddenRead if p == "get_hidden_doc1" else TrialRead) for p in BATTERY}, **{
    "allowed_readable": F("get_allowed_doc0.returned_any_source", "flag", expect=True, enforce=True),
    "match_all_readable": F("search_match_all.returned_any_source", "flag", expect=True, enforce=True)}),
    doc="Trial control: every read path answers as DLS should, the allowed document is readable, the hidden one never returned.")
AfterRead = evidence("AfterRead", {"outcome": F("status", "status", expect=REFUSALS),
                                   "hidden": F("returned_hidden_doc", "flag", expect=False),
                                   "any_source": F("returned_any_source", "flag", expect=False)})
BatteryAfter = evidence("BatteryAfter", {p: F(p, AfterRead) for p in BATTERY})
Battery = evidence("Battery", {"control": F("exp1_trial.dls_search_as_user", DlsControl), "trial": F("dls_battery_trial", BatteryTrial),
                               "after": F("dls_battery_after", BatteryAfter)}, doc="EXP1-T4 battery.")

FallbackControl = evidence("FallbackControl", {
    "client_labelled": F("client_labelled", "pos"),
    "flagged_wrong": F("flagged_wrong", "count"),
    "flag_rate": F("flag_rate", "rate", verify=True),
    "backend_checked": F("backend_labelled_checked", "pos"),
    "backend_wrong": F("backend_wrong", "count", expect={0})},
    [("le", "flagged_wrong", "client_labelled"), ("le", "backend_wrong", "backend_checked"),
     ("ratio_matches", "flag_rate", "flagged_wrong", "client_labelled")],
    "Sensitivity control (broken fallback): it exists only with a sample of client-labelled and backend-labelled answers.",
    {"flagged": property(lambda s: Proportion(s.flagged_wrong, s.client_labelled))})
Correctness = evidence("Correctness", {
    "examined": F("exp2.T1_examined", "pos", bad=(19999,)),
    "mismatches": F("exp2.T1_mismatches", "count", expect={0}),
    "backend_without_oracle": F("exp2.T1_backend_without_oracle", "count", expect={0}),
    "control": F("exp2.T1_control_broken_fallback", FallbackControl)}, [("le", "mismatches", "examined")], "EXP2-T1.")

Availability = evidence("Availability", {
    "failed": F("exp2.T2_failed_requests", "count"),
    "requests": F("exp2.T2_non_control_requests", "pos"),
    "longest_window_s": F("exp2.T2_longest_failure_window_s", "duration", bad=(2.5,)),
    "windows": F("exp2.T2_failure_windows", "windows")},
    [("le", "failed", "requests"), ("windows_match", "windows", "longest_window_s", "failed")], "EXP2-T2.",
    {"failed_share": property(lambda s: Proportion(s.failed, s.requests))})

FlipDown = evidence("FlipDown", {
    "flip_s": F("exp2.T3_flip_latency_s", "offset", bad=(5.5,)),
    "backend_after_client": F("exp2.T3_backend_after_first_client", "count", expect={0}),
    "t_req": F("start_basic.t_req", "timestamp"), "t_resp": F("start_basic.t_resp", "timestamp")},
    [("le", "t_req", "t_resp"), ("ge_diff", "flip_s", "t_req", "t_resp")],
    "EXP2-T3 down. The flip offset is signed: an answer received after the start_basic request but before its response is negative.")
FlipUp = evidence("FlipUp", {
    "flip_s": F("exp2.T3_flip_latency_s", "duration"),
    "ttl_s": F("ttl", "duration"),
    "ttl_recorded": F("exp2.T3_ttl_s", "duration", verify=True),
    "client_after_backend": F("exp2.T3_client_after_first_backend", "count", expect={0})}, [("eq", "ttl_recorded", "ttl_s")], doc="EXP2-T3 up (first backend answer at or after t_resp).")

Writes = evidence("Writes", {
    "acked": F("acked", "pos"), "acked_absent": F("acked_absent", "count", expect={0}),
    "sem_acked": F("sem_acked", "pos"), "sem_acked_absent": F("sem_acked_absent", "count", expect={0}),
    "sem_failed": F("sem_failed", "count"), "sem_failed_present": F("sem_failed_present", "count", expect={0}),
    "sem_failed_loud": F("sem_failed_loud_licence_or_missing", "count"),
    "sem_failed_other": F("sem_failed_other", "count", optional=True, expect={0}),
    "loss_control": F("control_injected_loss_detected", "flag", expect=True)},
    [("le", "acked_absent", "acked"), ("le", "sem_acked_absent", "sem_acked"), ("le", "sem_failed_present", "sem_failed"),
     ("le", "sem_failed_loud", "sem_failed"), ("le", "sem_failed_other", "sem_failed")],
    "EXP2-T4 writes. sem_failed_other is optional (addendum D): absent with sem_failed > 0 means unclassified.")
WritesGate = evidence("WritesGate", {"writes": F("exp2.writes", Writes)}, doc="EXP2-T4.")

ControlDown = evidence("ControlDown", {
    "n": F("n", "pos"), "licence_failures": F("licence_failures", "count"), "other_failures": F("other_failures", "count"),
    "success": F("success", "count"), "fail_fraction": F("fail_fraction", "rate", verify=True),
    "licence_refusals": F("licence_refusals", "count")},   # counted by Outcome case (observe.control_counts), review 8 F13
    [("sum_eq", ("licence_failures", "other_failures", "success"), "n"),
     ("ratio_matches", "fail_fraction", ("licence_failures", "other_failures"), "n"),
     ("le", "licence_refusals", ("licence_failures", "other_failures"))],
    props={"licence_share": property(lambda s: Proportion(s.licence_refusals, s.n))})
LicenceDown = evidence("LicenceDown", {"control": F("exp2.T5_control_after_1s", ControlDown)}, doc="EXP2-T5 down.")
ControlPre = evidence("ControlPre", {"n": F("n", "pos"), "failures": F("failures", "count"), "licence_refusals": F("licence_refusals", "count"),
                                     "licence_failures": F("licence_failures", "count", verify=True)},   # legacy substring count: checked, not used
                      [("le", "licence_refusals", "failures"), ("le", "failures", "n"), ("le", "licence_failures", "failures")],
                      props={"licence_share": property(lambda s: Proportion(s.licence_refusals, s.n))})
ControlPost = evidence("ControlPost", {"n": F("n", "pos"), "success": F("success", "count")}, [("le", "success", "n")],
                       props={"success_share": property(lambda s: Proportion(s.success, s.n))})
LicenceUp = evidence("LicenceUp", {"before": F("exp2.T5_control_before_start_trial", ControlPre),
                                   "after": F("exp2.T5_control_after_trial", ControlPost)}, doc="EXP2-T5 up.")

VectorCopy = evidence("VectorCopy", {
    "src": F("src_count", "pos"), "dst": F("dst_count", "pos"), "mismatches": F("vector_mismatches", "count", expect={0}),
    "knn_top1": F("knn_self_top1", "count"), "knn_of": F("knn_self_of", "pos")},
    [("le", "knn_top1", "knn_of"), ("le", "mismatches", "src")], props={"knn": property(lambda s: Proportion(s.knn_top1, s.knn_of))})
VectorCopyGT = evidence("VectorCopyGT", dict(VectorCopy.SPEC, src_vs_truth=F("src_vs_ground_truth_max_abs", "magnitude", bad=(0.5,))),
                        VectorCopy.RELATIONS, props={"knn": property(lambda s: Proportion(s.knn_top1, s.knn_of))})
SemanticCopy = evidence("SemanticCopy", {
    "docs": F("docs", "pos"), "dst": F("dst_count", "pos"), "mismatches": F("vector_mismatches_vs_stored", "count", expect={0}),
    "abs_err": F("worst_abs_err_vs_stub", "magnitude", bad=(0.5,)), "cosine": F("worst_cosine_vs_stub", "cosine", bad=(0.5,)),
    "knn_top1": F("knn_self_top1", "count"), "knn_of": F("knn_self_of", "pos"), "skipped": F("skipped_not_single_chunk", "count", expect={0})},
    [("le", "knn_top1", "knn_of"), ("le", "mismatches", "docs")], props={"knn": property(lambda s: Proportion(s.knn_top1, s.knn_of))})
PlainControl = evidence("PlainControl", {"src": F("src_count", "pos"), "dst": F("dst_count", "pos"),
                                         "mismatches": F("vector_mismatches", "count", expect={0})}, [("le", "mismatches", "src")])
CorruptedControl = evidence("CorruptedControl", {"mismatches": F("vector_mismatches", "count", expect={1})})
SourceCounts = evidence("SourceCounts", {"bbq_disk": F("bbq_disk", "pos"), "sem": F("sem", "pos"), "plain": F("plain", "pos")})
MigratedOps = evidence("MigratedOps", {o: F(o, "status", expect=OK) for o in MIG_OPS})
MigratedOpsAll = evidence("MigratedOpsAll", {i.replace("lic3-", "").replace("-", "_"): Field((i,), MigratedOps) for i in MIG_INDICES})
Migration = evidence("Migration", {
    "bbq_to_hnsw": F("exp3.migration.bbq_disk_to_hnsw", VectorCopyGT),
    "bbq_to_bbq_hnsw": F("exp3.migration.bbq_disk_to_bbq_hnsw", VectorCopy),
    "semantic": F("exp3.migration.semantic_to_text_vector", SemanticCopy),
    "plain_control": F("exp3.migration.control_plain_to_hnsw", PlainControl),
    "corrupted_control": F("exp3.migration.control_corrupted_vs_true_source", CorruptedControl),
    "independent": F("exp3.after_restart.counts_before", SourceCounts),
    "ops": F("exp3.migration.migrated_basic_ops", MigratedOpsAll)}, doc="EXP3 migration and EXP3-T2 Basic operations on the migrated indices.")

SemCall = evidence("SemCall", {"outcome": F("status", "status", expect=OK), "hits": F("hits", "pos")})
SemOnly = evidence("SemOnly", {k: F("semonly_trial." + k, SemCall) for k in ("semantic", "match", "match_all")}, doc="EXP3 semantic-only fixture control.")


def parse(cls, life):
    """-> (evidence, None) or (None, reason). Never raises for malformed input."""
    try:
        return cls.parse(life), None
    except Unparsed as e:
        return None, str(e)
    except (ArithmeticError, TypeError, ValueError, RecursionError) as e:
        return None, "malformed input: %s" % type(e).__name__
