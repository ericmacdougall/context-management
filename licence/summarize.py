"""Threshold reapplication over the per-life result files. load() rebuilds every figure that has a raw source (RAW_SOURCES: the
EXP1-T2 moments from poll.gz, EXP2-T2/T3/T5 from rows.gz, EXP2-T1 from rows.gz plus oracle.gz) from typed raw rows, after deleting the
stored values; figures with no raw source (STORED) are used as recorded and every verdict that reads them says so (provenance).
current_summary() reports every life and gate of the campaign inventory (CAMPAIGN) with the disclosed limits (LIMITS).
Usage: summarize.py RESULTS_DIR [out.json] | summarize.py RESULTS_DIR --current OUT. Does not import the experiment code.

Verdicts (DESIGN-VERDICTS.md): this module decides no gate. Every gate status comes from verdicts.judge(), which parses the life into typed,
immutable evidence (evidence.py: every field's domain, every cross-field relation, HTTP outcomes as a closed set of cases, rates only as
proportions of counts, controls that cannot exist without their sample); evidence that does not parse is UNVERIFIED with the parse reason,
and verdict functions see only evidence objects. The one optional field is exp2.writes.sem_failed_other (addendum D): absent, EXP2-T4 passes
only when sem_failed is 0. The per-life reports here (figures copied for reading, matrix table, exp3 operation lists, exp4) are reports,
not gates; they may raise on a malformed life file."""
import collections
import glob
import functools
import gzip
import json
import math
import os
import sys

import evidence
import observe


def wilson_lower(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (c - r) / d


# ---- raw sources (review 10 F21): which stored figures a raw file rebuilds, declared as data ---------------------------------------
# When a source's raw file exists for a life, the loader DELETES every path the source declares and rebuilds all of them from typed
# rows; nothing stored under those paths is read. When it is absent: "delete" sources leave the figures absent (the gate is
# UNVERIFIED: every recorded life saved its rows, and a stored refusal was never classified without its poll stream); the "keep"
# source (T1 needs the oracle legs, saved only from down_7 on) keeps the stored figures and every verdict that reads them says
# "from stored figure, raw not available". Every leaf a verdict reads is either under a source here or declared in STORED
# (tests.test_evidence.RebuildTests generates that check from the evidence types).
def _paths(prefix, *names):
    return tuple(tuple(prefix.split(".")) + (n,) for n in names)


RAW_SOURCES = {
    "poll": {"needs": ("poll",), "modes": ("down",), "missing": "delete", "paths": (("exp1_moment",),)},
    "rows": {"needs": ("rows",), "modes": ("down", "up"), "missing": "delete",
             "paths": _paths("exp2", "T2_failed_requests", "T2_non_control_requests", "T2_longest_failure_window_s", "T2_failure_windows",
                             "T3_flip_latency_s", "T3_backend_after_first_client", "T3_client_after_first_backend",
                             "T3_backend_labelled_after_resp_start", "T5_control_after_1s", "T5_control_before_start_trial",
                             "T5_control_after_trial")},
    "rows+oracle": {"needs": ("rows", "oracle"), "modes": ("down", "up"), "missing": "keep",
                    "paths": _paths("exp2", "T1_examined", "T1_mismatches", "T1_examples", "T1_backend_without_oracle",
                                    "T1_control_broken_fallback")},
}
_CELL = "matrix cell, a primary observation recorded once (no raw file)"
STORED = {   # every other leaf a verdict reads, with why it has no raw source
    ("exp1_after",): _CELL, ("exp1_trial",): _CELL, ("dls_battery_trial",): _CELL, ("dls_battery_after",): _CELL,
    ("semonly_trial",): _CELL,
    ("start_basic",): "transition request and response times, a primary observation (no raw file)",
    ("start_trial",): "transition request and response times, a primary observation (no raw file)",
    ("ttl",): "configuration", ("exp2", "T3_ttl_s"): "configuration (copy of ttl)",
    ("exp2", "writes"): "derived; the write ids were never saved",
    ("exp3",): "derived; the migration inputs were never saved",
}
_NO_ORACLE = "derived; the oracle legs were not saved for this life"


def source_of(path):
    """The raw source that rebuilds this life path, or "stored"."""
    path = tuple(path)
    for name, src in RAW_SOURCES.items():
        if any(path[:len(p)] == p for p in src["paths"]):
            return name
    return "stored"


def stored_category(path):
    path = tuple(path)
    best = max((p for p in STORED if path[:len(p)] == p), key=len, default=None)
    return STORED.get(best)


def _drop(R, path):
    d = R
    for k in path[:-1]:
        if not isinstance(d, dict) or k not in d:
            return
        d = d[k]
    if isinstance(d, dict):
        d.pop(path[-1], None)


def _place(R, path, v):
    d = R
    for k in path[:-1]:
        if not isinstance(d.get(k), dict):
            d[k] = {}
        d = d[k]
    d[path[-1]] = v


def _timed(v):
    return evidence._real(v) and v >= 1


def _times(R):
    """(mode, window, t_req, t_resp) when the life's run window and transition times are finite epoch times in order, else None."""
    try:
        return _times_or_raise(R)
    except Exception:   # any shape of metadata: no times, so no raw figure is accepted (review 11 F25)
        return None


def _times_or_raise(R):
    mode = R.get("mode")
    if mode not in ("down", "up"):
        return None
    ev = (R.get("exp2") or {}).get("events") if isinstance(R.get("exp2"), dict) else None
    ev = ev if isinstance(ev, dict) else {}
    tr = R.get("start_basic" if mode == "down" else "start_trial")
    t_req, t_resp = (tr.get("t_req"), tr.get("t_resp")) if isinstance(tr, dict) else (None, None)
    window = (ev.get("t_start"), ev.get("t_stop"))
    if not (all(_timed(x) for x in window + (t_req, t_resp)) and window[0] <= window[1]):
        return None
    return mode, window, t_req, t_resp


class Unreadable(object):
    """A raw file that exists but could not be read or decoded (review 11 F25): present, never usable."""

    def __init__(self, reason):
        self.reason = reason


# The file whose existence decides whether a source's stored figures may stand (review 11 F25): its key file. Once it exists
# (readable or not), the source's paths are deleted and only a completed derivation puts figures back.
KEY_FILE = {"poll": "poll", "rows": "rows", "rows+oracle": "oracle"}


def derive(raw, mode, window, t_req, t_resp):
    """Every figure each raw source rebuilds, from typed rows only -> {source: {"figures": {path: value}} or {"error": reason}}.
    raw: {"rows": list, "poll": list, "oracle": dict, None when the file does not exist, or Unreadable}. A source whose key file
    exists always gets an entry; any failure is an "error" entry, never an exception. JSON-serialisable (load caches it per file)."""
    out, W = {}, None
    if isinstance(raw.get("rows"), Unreadable):
        W = observe.Unparsed(("rows",), raw["rows"].reason)
    elif raw.get("rows") is not None:
        try:
            W = observe.parse_work(raw["rows"], window)
        except Exception as e:   # any failure to type the rows is that source's error (review 11)
            W = e if isinstance(e, observe.Unparsed) else observe.Unparsed(("rows",), "%s: %s" % (type(e).__name__, e))
    for name, src in RAW_SOURCES.items():
        if mode not in src["modes"] or raw.get(KEY_FILE[name]) is None:
            continue
        unusable = [k for k in src["needs"] if raw.get(k) is None or isinstance(raw.get(k), Unreadable)]
        if unusable:
            out[name] = {"error": "%s: %s" % (name, "; ".join(
                ("%s file absent" % k) if raw.get(k) is None else ("%s file unreadable: %s" % (k, raw[k].reason)) for k in unusable))}
            continue
        try:
            if name == "poll":
                m = observe.moments(raw["poll"], t_resp, observe.LICENCE_PROBES + ("dls_search",), window)
                bad = [f for f, v in m.items() if v.get("stream_error")]
                if bad:
                    raise observe.Unparsed(("poll",), m[bad[0]]["stream_error"])
                figs = {("exp1_moment",): m}
            else:
                if isinstance(W, Exception):
                    raise W
                if name == "rows":
                    f = dict(observe.availability(W), **observe.flip(W, t_req, t_resp, mode))
                    if mode == "down":
                        f["T3_backend_labelled_after_resp_start"] = sum(1 for w in W if w.arm == "auto" and w.ok and w.label == "backend"
                                                                        and w.t0 > t_resp)
                    f.update(observe.control(W, t_req, t_resp, mode))
                else:
                    f = observe.correctness(W, *observe.oracle_from_file(raw["oracle"]))
                figs = {("exp2", k): v for k, v in f.items()}
            out[name] = {"figures": [[list(p), v] for p, v in figs.items()]}
        except Exception as e:   # any failure is this source's error; the figures stay absent (review 11 F25)
            out[name] = {"error": "%s does not parse: %s" % (name, e) if isinstance(e, observe.Unparsed)
                         else "%s could not be derived: %s: %s" % (name, type(e).__name__, e)}
    return out


def apply_derived(R, derived, present, mode):
    """Reviews 10 F21 and 11 F25. For each source of the life's direction: when its key file does not exist and the source is
    declared "keep", the stored figures stand (labelled by provenance); in EVERY other case its declared paths are deleted, and
    only a completed derivation puts figures back. `present` is the explicit existence of each raw file, never inferred from
    `derived`: a derivation that did not run or did not complete leaves the figures absent and the reason in _raw_check."""
    notes, built, stored_only = R["_raw_check"], R["_rebuilt"], R["_stored_only"]
    for name, src in RAW_SOURCES.items():
        if mode in ("down", "up") and mode not in src["modes"]:
            continue
        exists = bool(present.get(KEY_FILE[name]))
        if not exists and src["missing"] == "keep":
            stored_only.append(name)
            continue
        for p in src["paths"]:
            _drop(R, p)
        got = derived.get(name)
        if isinstance(got, dict) and "figures" in got and "error" not in got:
            for p, v in got["figures"]:
                _place(R, tuple(p), v)
            built.append(name)
        elif isinstance(got, dict) and "error" in got:
            notes[name] = got["error"]
        elif not exists:
            notes[name] = "no %s file: these figures are not accepted without it" % KEY_FILE[name]
        else:
            notes[name] = "%s file present but its figures were not derived (%s): not accepted" % (
                KEY_FILE[name], notes.get("life") or notes.get("files") or "derivation did not run")


_BAD_TIMES = "run window or transition times are not finite epoch times: no raw figure is accepted"


def rebuild(R, raw, present=None):
    """Rebuild every raw-sourced figure of life R from in-memory raw files (see RAW_SOURCES). Mutates R. `present` (raw file ->
    exists) defaults to "every non-None entry of raw exists"."""
    R["_raw_check"], R["_rebuilt"], R["_stored_only"] = {}, [], []
    present = {k: raw.get(k) is not None for k in ("rows", "poll", "oracle")} if present is None else present
    t = _times(R)
    if t is None:
        R["_raw_check"]["life"] = _BAD_TIMES
        apply_derived(R, {}, present, R.get("mode"))
        return R
    mode, window, t_req, t_resp = t
    apply_derived(R, derive(raw, mode, window, t_req, t_resp), present, mode)
    return R


def rebuilt_sources(R):
    return set(R.get("_rebuilt") or ())


def provenance(name, R):
    """"" when every figure the gate reads came from typed raw rows; otherwise "from stored figure, raw not available: ..." naming
    the stored parts and why they have no raw source (configuration is not a figure and is not listed)."""
    parts = {}
    for p in evidence.leaf_paths(verdicts.GATES[name].evidence):
        src = source_of(p)
        if src == "stored":
            cat = stored_category(p) or "undeclared"
        elif src in (R.get("_stored_only") or ()):
            cat = _NO_ORACLE
        else:
            continue
        if not cat.startswith("configuration"):
            parts.setdefault(cat, set()).add(".".join(p[:2]))
    if not parts:
        return ""
    return "from stored figure, raw not available: " + "; ".join("%s (%s)" % (", ".join(sorted(v)), k) for k, v in sorted(parts.items()))


RAW_FILES = ("rows", "poll", "oracle")


def _present(base):
    """Review 11 F25: which raw files EXIST beside a life (lexists: a dangling link, a directory or an unreadable file is present).
    This explicit check, never an empty or failed read, is what tells "no raw file" from "raw file present but unusable"."""
    return {k: os.path.lexists("%s.%s.gz" % (base, k)) for k in RAW_FILES}


def _read_one(p, k):
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return [json.loads(l) for l in fh] if k == "rows" else json.load(fh)


def _read_raw(base):
    """Each raw file: its content, None when it does not exist, or Unreadable(reason) when it exists and cannot be read."""
    raw = {}
    for k, exists in _present(base).items():
        if not exists:
            raw[k] = None
            continue
        try:
            raw[k] = _read_one("%s.%s.gz" % (base, k), k)
        except Exception as e:   # truncated gzip, not gzip, a directory, bad UTF-8, bad JSON, recursion: present, unusable
            raw[k] = Unreadable("%s: %s" % (type(e).__name__, e))
    return raw


def _stamp(base):
    """Cache key for the raw files of a life: existence, size and mtime per file (a file that cannot be stat'ed is its own key)."""
    out = []
    for k in RAW_FILES:
        p = "%s.%s.gz" % (base, k)
        try:
            st = os.lstat(p)
            out.append((k, st.st_mode, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            out.append((k, None, None, None))
        except OSError as e:
            out.append((k, "unstattable", type(e).__name__, None))
    return tuple(out)


@functools.lru_cache(maxsize=64)
def _derived_files(base, stamp, mode, window, t_req, t_resp):
    try:
        return json.dumps(derive(_read_raw(base), mode, window, t_req, t_resp))
    except Exception as e:   # never reached by design (derive and _read_raw catch per file and source); the figures stay absent
        return json.dumps({"_read_error": "%s: %s" % (type(e).__name__, e)})


def _unparsed_life(name, direction, reason):
    """Review 11 F26: a life file that does not parse into a life of its direction. Every gate of it is UNVERIFIED with `reason`."""
    return {"_file": name, "mode": direction, "_unparsed": reason, "_raw_check": {"life": reason}, "_rebuilt": [], "_stored_only": []}


def parse_life(f, direction):
    """Parse one life file at the boundary -> the life (a dict of its direction, the loader's "_" keys removed) or an Unparsed
    life. Never raises on any file content."""
    name = os.path.basename(f)
    try:
        with open(f, "rb") as fh:
            R = json.loads(fh.read().decode("utf-8"))
    except Exception as e:   # unreadable, not UTF-8, not JSON, nested too deep
        return _unparsed_life(name, direction, "life file does not parse: %s: %s" % (type(e).__name__, str(e)[:200]))
    if not isinstance(R, dict):
        return _unparsed_life(name, direction, "life file is a JSON %s, not an object" % type(R).__name__)
    if R.get("mode") != direction:
        return _unparsed_life(name, direction, "life mode %r does not match its file name (%s)" % (R.get("mode"), direction))
    for k in [k for k in R if isinstance(k, str) and k.startswith("_")]:   # the loader's own namespace; a file cannot set it
        del R[k]
    R["_file"] = name
    return R


def _load_one(f, direction):
    R = parse_life(f, direction)
    if "_unparsed" in R:
        return R
    R["_raw_check"], R["_rebuilt"], R["_stored_only"] = {}, [], []
    base = f[:-5]
    present = _present(base)
    t = _times(R)
    if t is None:
        R["_raw_check"]["life"] = _BAD_TIMES
        apply_derived(R, {}, present, direction)
        return R
    got = json.loads(_derived_files(base, _stamp(base), *t))
    if "_read_error" in got:
        R["_raw_check"]["files"] = got.pop("_read_error")
    apply_derived(R, got, present, direction)
    return R


def load(d, mode):
    """Every life file of a direction, with its raw-sourced figures rebuilt from the raw files beside it (RAW_SOURCES). Never raises
    on file content: a file that cannot be loaded is an Unparsed life (review 11 F26)."""
    out = []
    for f in sorted(glob.glob(os.path.join(d, "%s_[0-9]*.json" % mode))):
        try:
            out.append(_load_one(f, mode))
        except Exception as e:   # belt and braces: a failure anywhere in loading is that life's UNVERIFIED, never a crash
            out.append(_unparsed_life(os.path.basename(f), mode, "life could not be loaded: %s: %s" % (type(e).__name__, str(e)[:200])))
    return out


# ---- verdicts: every gate status comes from verdicts.judge(), which parses the life into typed evidence (evidence.py) first. Nothing in
# this module decides a gate; it writes reports and copies each verdict into them as <out>_status, <out>_pass and, when not PASS, <out>_reason.
import verdicts  # noqa: E402
from evidence import MAX_INT, MISSING  # noqa: E402


def fetch(R, path):
    """Report helper (never used by a verdict): the value at path, or MISSING."""
    v = R
    for k in path:
        if not isinstance(v, dict) or k not in v:
            return MISSING
        v = v[k]
    return v


def _is_int(v):
    """Report helper: an integer in the measurable range (evidence.MAX_INT; a 10**400 would overflow float division, review 8 F14)."""
    return type(v) is int and abs(v) <= MAX_INT


def combine(statuses):
    """For the aggregate over lives: FAIL if any life failed; else UNVERIFIED if any is not PASS (or there are none); else PASS."""
    s = list(statuses)
    return "FAIL" if "FAIL" in s else ("UNVERIFIED" if not s or any(x != "PASS" for x in s) else "PASS")


def gate(name, R):
    return verdicts.judge(name, R).status


def put(r, name, R):
    """Write gate `name` into report dict r as <out>_status and <out>_pass (true only for PASS), with the reason when not PASS."""
    v = verdicts.judge(name, R)
    out = verdicts.GATES[name].out
    r[out + "_status"], r[out + "_pass"] = v.status, v.status == "PASS"
    if v.status != "PASS":
        r[out + "_reason"] = v.reason
    prov = provenance(name, R)
    if prov:
        r[out + "_provenance"] = prov   # review 10 F21: a figure with no raw source is labelled
    return v.status



def exp1_checks(R):
    r = {}
    m = R.get("exp1_moment") if isinstance(R.get("exp1_moment"), dict) else {}
    r["T2_first_refusal_s"] = {f: v.get("first_refusal_rel_resp_s") for f, v in m.items() if isinstance(v, dict)}
    r["T2_dls_returned_before"] = (m.get("dls_search") or {}).get("dls_returned_counts_before") if isinstance(m.get("dls_search"), dict) else None
    for name in ("EXP1-T2", "EXP1-T3", "EXP1-T4", "EXP1-T4-battery"):
        put(r, name, R)
    after = R.get("exp1_after") if isinstance(R.get("exp1_after"), dict) else {}
    d = after.get("dls_search_as_user") if isinstance(after.get("dls_search_as_user"), dict) else {}
    r["T4_dls_after"] = {"status": d.get("status"), "class": d.get("class"), "returned": d.get("returned"), "reason": str((d.get("error") or {}).get("reason", ""))[:90]}
    r["T4_control_before_returned"] = r["T2_dls_returned_before"]
    a, b = R.get("dls_battery_after"), R.get("dls_battery_trial")
    if isinstance(a, dict) and isinstance(b, dict):
        r["T4_battery_after_statuses"] = {k: v.get("status") for k, v in a.items() if isinstance(v, dict)}
        r["T4_battery_trial_statuses"] = {k: v.get("status") for k, v in b.items() if isinstance(v, dict)}
    return r


def matrix_table(R):
    cols = {"basic0": R.get("exp1_basic0"), "trial": R.get("exp1_trial"), "after": R.get("exp1_after")}
    cells = sorted(set().union(*[set(c) for c in cols.values() if c]))
    t = {}
    for cell in cells:
        row = {}
        for k, c in cols.items():
            v = (c or {}).get(cell)
            if v is None:
                row[k] = None
            elif "class" in v:
                row[k] = "%s%s" % (v["class"], "/%s" % v.get("status") if v.get("status") else "")   # report text only
            else:  # composite cells
                row[k] = {kk: (vv.get("class") if isinstance(vv, dict) else vv) for kk, vv in v.items()}
        t[cell] = row
    return t


def _g(R, path):
    v = fetch(R, tuple(path.split(".")))
    return None if v is MISSING else v


def _num(x):
    return _is_int(x) or (type(x) is float and math.isfinite(x))


def exp2_checks(R, direction):
    """Report figures (None where not recorded) plus the EXP2 gates from the table. Never raises on a missing or malformed field."""
    g = lambda p: _g(R, p)
    failed, n = g("exp2.T2_failed_requests"), g("exp2.T2_non_control_requests")
    r = {"T1_examined": g("exp2.T1_examined"), "T1_mismatches": g("exp2.T1_mismatches"), "T1_control_flag_rate": g("exp2.T1_control_broken_fallback.flag_rate"),
         "T1_control_n": g("exp2.T1_control_broken_fallback.client_labelled"), "T1_control_backend_wrong": g("exp2.T1_control_broken_fallback.backend_wrong"),
         "T1_backend_without_oracle": g("exp2.T1_backend_without_oracle"),
         "T2_fail_fraction": failed / n if _num(failed) and _is_int(n) and n > 0 else None, "T2_longest_window_s": g("exp2.T2_longest_failure_window_s"),
         "T3_flip_latency_s": g("exp2.T3_flip_latency_s"), "ttl": g("ttl"), "T6_rrf": g("exp2.T6_rrf_backend_vs_client_top10"),
         "T6_linear": g("exp2.T6_linear_backend_vs_client_rrf_top10")}
    r["T4"] = {k: g("exp2.writes." + k) for k in ("acked", "acked_absent", "present_unacked", "failed_batches", "sem_acked", "sem_acked_absent", "sem_failed",
                                                   "sem_failed_loud_licence_or_missing", "sem_failed_present", "control_injected_loss_detected")}
    if direction == "down":
        r["T5"] = {k: g("exp2.T5_control_after_1s." + k) for k in ("n", "fail_fraction", "licence_failures")}
    else:
        r["T5"] = {"before_n": g("exp2.T5_control_before_start_trial.n"), "before_failures": g("exp2.T5_control_before_start_trial.failures"),
                   "before_licence_failures": g("exp2.T5_control_before_start_trial.licence_failures"), "after_trial_n": g("exp2.T5_control_after_trial.n"),
                   "after_trial_success": g("exp2.T5_control_after_trial.success")}
        r["sem_first_ok_after_trial_s"] = g("exp2.sem_first_ok_after_trial_s")
    w = r["T4"]
    # report, not a gate: the pre-addendum-C rule (every semantic failure with any 403/404 body counted loud), kept for comparison
    r["T4_legacy_rule_result"] = (w["acked_absent"] == 0 and w["sem_acked_absent"] == 0 and w["sem_failed_present"] == 0 and w["control_injected_loss_detected"] is True
                                  and w["sem_failed"] == w["sem_failed_loud_licence_or_missing"]) if all(x is not None for x in w.values()) else None
    for name in ("EXP2-T1", "EXP2-T2", "EXP2-T3-" + direction, "EXP2-T4", "EXP2-T5-" + direction):
        put(r, name, R)
    return r


def exp3_checks(R):
    E = R["exp3"]
    out = {"ops": {}, "silent": [], "reads_diff": []}
    for kind in ("sem", "bbq_disk", "bbq_hnsw", "plain"):
        tm, am = E["twin_mutations"][kind], E["after"]["mutations"][kind]
        for op, t in tm.items():
            a = am[op]
            if op == "bulk_partial_update":
                tr = "ok" if t["summary"]["failed"] == 0 else "fail"
                ba = "ok" if a["summary"]["failed"] == 0 else ("LOUD_LICENCE_ITEM" if "non-compliant" in json.dumps(a["summary"]["first_error"]) else "FAIL_OTHER")
                same = (tr == ba == "ok" and t["effect"] == a["effect"])
                verdict = "WORKS" if same else ba
                if tr == "ok" and ba == "ok" and not same:
                    out["silent"].append([kind, op, t["effect"], a["effect"]])
                out["ops"].setdefault(kind, {})[op] = {"trial": tr + "/http%s" % t["status"], "basic": verdict + "/http%s" % a["status"]}
                continue
            if a["status"] < 300:
                same = t["effect"] == a["effect"] and t["status"] < 300
                if not same:
                    out["silent"].append([kind, op, t.get("effect"), a.get("effect")])
                verdict = "WORKS" if same else "SILENT_DIFFERENCE"
            else:
                verdict = "LOUD_LICENCE" if a["class"] == "LICENCE_ERROR" else "FAIL_OTHER(%s)" % a["class"]
                # a loud failure must leave data intact: effect fields equal pre-op values (tag unchanged, embedding present)
            out["ops"].setdefault(kind, {})[op] = {"trial": t["class"] + "/%s" % t["status"], "basic": verdict + "/%s" % a["status"]}
        pre, aft = E["pre_reads"][kind], E["after"]["reads"][kind]
        for k in pre:
            if k in ("lexical_title", "match_on_semantic_field", "semantic_query", "query_string_on_all"):
                if aft[k].get("class") == "AVAILABLE" and aft[k].get("hits") != pre[k].get("hits"):
                    out["reads_diff"].append([kind, k, pre[k].get("hits"), aft[k].get("hits")])
                if aft[k].get("class") != pre[k].get("class"):
                    out["reads_diff"].append([kind, k, pre[k].get("class"), aft[k].get("class"), (aft[k].get("error") or {}).get("reason")])
            elif k in ("get_all", "count", "inference_embeddings_sample", "knn_self_top1", "vectors_equal_ground_truth"):
                if json.dumps(pre[k], sort_keys=True) != json.dumps(aft[k], sort_keys=True) and kind != "x":
                    out["reads_diff"].append([kind, k, pre[k], aft[k]])
    chk = {}
    if isinstance(E.get("migration"), dict) and "error" in E["migration"]:
        chk["error"] = E["migration"]["error"]
    put(chk, "EXP3-migration", R)   # EXP3 migration and EXP3-T2 (Basic operations on the migrated indices): one gate from the table
    chk["migrated_basic_ops"] = _g(R, "exp3.migration.migrated_basic_ops")
    out["migration_checks"] = chk
    if "unrefreshed_twin_on_trial" in R:
        u = lambda x: {"searchable_before_update": x.get("searchable_before_update", "not recorded (fixture was refreshed by a restart: not a clean test)"), "bulk_failed": x["bulk"]["failed"], "bulk_http": x["bulk_status"],
                       "tags": x["tags"], "embeddings": x["embeddings"], "semantic_search": x["semantic_search"].get("status"), "count": x["count"]}
        bs = R.get("unrefreshed_after_start_basic") or E["after"].get("unrefreshed_partial_on_basic")
        out["unrefreshed_partial"] = {"trial": u(R["unrefreshed_twin_on_trial"]), "basic": u(bs)}
    if "semonly_trial" in R:
        sm = {}
        try:   # report only (the EXP3-semonly-control verdict parses its own evidence); a malformed record is reported, not raised
            for k, t in R["semonly_trial"].items():
                a = R["semonly_after"][k]
                sm[k] = {"trial": [t["status"], t["hits"]], "basic": [a["status"], a["hits"], (a["error"] or {}).get("reason", "")[:50]],
                         "silent_difference": a["status"] < 300 and t["status"] < 300 and a["hits"] != t["hits"]}
        except (ArithmeticError, TypeError, ValueError, KeyError, AttributeError) as e:
            sm = "malformed record: %s" % type(e).__name__
        out["semonly"] = sm
        out["semonly_silent"] = [k for k, v in sm.items() if v["silent_difference"]] if isinstance(sm, dict) else []
    put(out, "EXP3-semonly-control", R)
    out["restart"] = {"counts_equal": E["after_restart"]["counts_before"] == E["after_restart"]["counts_after"], "counts": E["after_restart"]["counts_after"]}
    return out


def exp4_checks(R):
    out = {}
    for k in ("exp4_state_basic0", "exp4_state_trial", "exp4_state_trial_after_restart", "exp4_state_basic_after"):
        s = R.get(k)
        if s:
            out[k] = {"tier": s["report"]["tier"], "readback": s["readback"]["type"], "safe": s["safe"], "exact": s["exact"], "paths": s["paths"]}
    out["units"] = {n: v["tier"] for n, v in R["exp4_units"].items()}
    ent = [n for n, v in R["exp4_units"].items() if v["tier"] == "enterprise"]
    out["units_enterprise_cases"] = ent
    out["units_only_license_path"] = all(v["paths"] == ["/_license"] for v in R["exp4_units"].values())
    for k in ("exp4_fresh_start", "exp4_restart_trial", "exp4_restart_basic"):
        d = R[k]
        out[k] = {"status_sequence": d["status_sequence"], "services": {s: {kk: v[kk] for kk in ("first_tier", "first_probe_error", "reviewed_correct", "reprobe_correct", "reprobe_seconds_to_correct_after_first_200")} for s, v in d["services"].items()}}
    return out


def aggregate(d, res):
    agg = {}
    downs = [r for r in load(d, "down") if r.get("status") == "complete"]
    ups = [r for r in load(d, "up") if r.get("status") == "complete"]
    # matrix: distinct values per cell and phase across lives (a cell with more than one value is non-deterministic)
    mt = {}
    for R in downs:
        for cell, row in matrix_table(R).items():
            for ph, v in row.items():
                mt.setdefault(cell, {}).setdefault(ph, set()).add(json.dumps(v, sort_keys=True))
    agg["matrix_distinct_values_across_lives"] = {c: {ph: sorted(v) for ph, v in d2.items()} for c, d2 in mt.items()}
    agg["matrix_nondeterministic_cells"] = sorted(c for c, d2 in mt.items() if any(len(v) > 1 for v in d2.values()))
    allx = [exp2_checks(R, "down") for R in downs] + [exp2_checks(R, "up") for R in ups]
    nums = lambda key, sub=None: [v for v in ((x.get(key) if sub is None else (x.get(key) or {}).get(sub)) for x in allx) if _num(v)]
    lo = lambda xs: min(xs) if xs else None
    hi = lambda xs: max(xs) if xs else None
    ex, mm = sum(nums("T1_examined")), sum(nums("T1_mismatches"))
    agg["exp2_total_examined"], agg["exp2_total_mismatches"] = ex, mm
    agg["exp2_correct_rate_wilson_lower"] = wilson_lower(ex - mm, ex)
    agg["exp2_lives"] = {"down": len(downs), "up": len(ups)}
    agg["exp2_worst"] = {"min_examined": lo(nums("T1_examined")), "max_longest_window_s": hi(nums("T2_longest_window_s")),
                         "max_fail_fraction": hi(nums("T2_fail_fraction")), "min_control_flag_rate": lo(nums("T1_control_flag_rate")),
                         "max_flip_down_s": hi([x["T3_flip_latency_s"] for x in allx[:len(downs)] if _num(x["T3_flip_latency_s"])]),
                         "max_flip_up_s": hi([x["T3_flip_latency_s"] for x in allx[len(downs):] if _num(x["T3_flip_latency_s"])]),
                         "total_acked_writes": sum(nums("T4", "acked")), "total_acked_absent": sum(nums("T4", "acked_absent"))}
    # mandatory: at least one complete life in each direction (EXP2 has both); each life's own gate status, combined
    both = bool(downs) and bool(ups)
    agg["all_status"] = {k + "_pass": combine(x[k + "_status"] for x in allx) if both else "UNVERIFIED" for k in ("T1", "T2", "T3", "T4", "T5")}
    agg["all_pass"] = {k: v == "PASS" for k, v in agg["all_status"].items()}
    return agg


# ---- the campaign inventory (review 10 F23), independent of the files found ------------------------------------------------------
# THRESHOLDS.md "Lives": N = 5 lives of A (down) and 5 of B (up). down_6 and down_7 are supplemental down lives (down_6: 7566b92;
# down_7: addendum B, 10441ea), counted in every result table (DEGRADE.md evidence ids). A missing life is UNVERIFIED, never skipped.
CAMPAIGN = {"down": (1, 2, 3, 4, 5, 6, 7), "up": (1, 2, 3, 4, 5)}

# The six registered-wording differences gate review 10 classed as disclosed lab limits. They are stated with the results: here (and
# so in summary_current.json), in DEGRADE.md and in results/README.md. A PASS below establishes the implemented rule, not more.
LIMITS = (
    {"gate": "EXP3-migration (THRESHOLDS EXP3-T2)",
     "limit": "PASS establishes the implemented rule: counts equal the independent source counts (299/299 and 199/199 after an earlier "
              "delete; registered text 300/300 and 200/200) and semantic vectors within absolute error 0.005 and cosine 0.9999 of the "
              "stub. It does NOT establish the registered per-value 1% relative criterion; the vectors needed for it were not kept."},
    {"gate": "EXP1-T3", "limit": "checks three fixed latch cells (rrf retriever, linear retriever, reranker gate), not every "
                                 "LICENCE_ERROR cell of a search path; a three-cell PASS cannot establish the every-cell criterion."},
    {"gate": "EXP2-T2", "limit": "the longest failure window joins failed auto, linear, lexical and vector reads whose gap is under 1 s; "
                                 "the registered window is consecutive failed auto requests (every committed life has 0 failed reads)."},
    {"gate": "EXP1-T2", "limit": "refusal timing uses the poll's end time, not the probe start time addendum B names; conservative for "
                                 "the 5 s bound, but the reported timing is not the registered probe-start measurement."},
    {"gate": "EXP1-T1", "limit": "a 400 carrying \"non-compliant\" is not counted as a licence refusal; only a 403 with the licence "
                                 "text is (THRESHOLDS EXP1-T1 says 403 or 400)."},
    {"gate": "EXP3-T1, EXP3-T4, EXP3-T5, EXP4-T1, EXP4-T2",
     "limit": "no gate: these registered thresholds are reported only, so this artefact is not a complete registered-threshold verdict."},
)


def _judge_life(name, R):
    """One gate of one inventory life -> (Verdict, provenance). Absent, unparsed or incomplete lives are UNVERIFIED with the reason;
    a verdict that raises is UNVERIFIED with the exception named (review 11 F26: current_summary never raises on file content)."""
    if R is None:
        return verdicts.Verdict(verdicts.UNVERIFIED, "life file absent (campaign inventory)"), ""
    if "_unparsed" in R:
        return verdicts.Verdict(verdicts.UNVERIFIED, R["_unparsed"]), ""
    if R.get("status") != "complete":
        return verdicts.Verdict(verdicts.UNVERIFIED, "life status %r, not complete" % (R.get("status"),)), ""
    try:
        return verdicts.judge(name, R), provenance(name, R)
    except Exception as e:
        return verdicts.Verdict(verdicts.UNVERIFIED, "verdict could not be computed: %s: %s" % (type(e).__name__, str(e)[:200])), ""


def current_summary(d):
    """Reviews 9 F19 and 10 F23: the current summary artefact. The inventory is CAMPAIGN x verdicts.GATES, not the files found: a
    missing life is listed with every gate UNVERIFIED ("life file absent"), and a gate's aggregate is FAIL if any expected life fails,
    else UNVERIFIED if any expected life is absent or not PASS, else PASS. Files outside the inventory are listed, not counted. Every
    reason says when a figure is from a stored value with no raw source (provenance). Deterministic: no time, no host."""
    found = {}
    for mode in ("down", "up"):
        for R in load(d, mode):
            found[R["_file"]] = R
    lives, notes, agg = {}, {}, {}
    for mode, ns in sorted(CAMPAIGN.items()):
        for n in ns:
            f = "%s_%d.json" % (mode, n)
            R = found.pop(f, None)
            row = lives[f] = {}
            notes[f] = {"mode": mode, "raw_check": (R or {}).get("_raw_check") or {}, "rebuilt_from_raw": sorted((R or {}).get("_rebuilt") or ())}
            for name, g in sorted(verdicts.GATES.items()):
                if g.scope not in ("both", mode):
                    continue
                v, prov = _judge_life(name, R)
                reason = ("PASS: " + g.doc) if v.status == verdicts.PASS else (v.reason or "no reason given")
                row[name] = {"status": v.status, "reason": reason + ("; " + prov if prov else "")}
                agg.setdefault(name, []).append((f, v.status, "absent" if R is None else "unparsed" if "_unparsed" in R else ""))
    out = {}
    for name, g in sorted(verdicts.GATES.items()):
        xs = agg.get(name, [])
        st = [s for _, s, _ in xs]
        absent = [f for f, _, a in xs if a == "absent"]
        unparsed = [f for f, _, a in xs if a == "unparsed"]
        status = verdicts.FAIL if verdicts.FAIL in st else verdicts.UNVERIFIED if (absent or unparsed or not st or any(s != verdicts.PASS for s in st)) \
            else verdicts.PASS
        reason = ("absent: %s; " % ", ".join(absent) if absent else "") + ("does not parse: %s; " % ", ".join(unparsed) if unparsed else "") \
            + "; ".join("%s %s" % (f, s) for f, s, _ in xs)
        out[name] = {"status": status, "reason": reason or "no life in scope"}
    return {"artefact": "summary_current.json", "generated_by": "summarize.current_summary (python3 summarize.py <results> --current <out>)",
            "inventory": {m: ["%s_%d.json" % (m, n) for n in ns] for m, ns in sorted(CAMPAIGN.items())},
            "unexpected_files": sorted(found), "limits": list(LIMITS), "lives": lives, "raw_checks": notes, "aggregate": out}


def main():
    if len(sys.argv) == 4 and sys.argv[2] == "--current":
        json.dump(current_summary(sys.argv[1]), open(sys.argv[3], "w"), indent=1, sort_keys=True)
        open(sys.argv[3], "a").write("\n")
        return
    d = sys.argv[1]
    res = {"down": [], "up": []}
    for R in load(d, "down"):
        if R.get("status") != "complete":
            res["down"].append({"file": R["_file"], "status": R.get("status"), "error": R.get("error")})
            continue
        res["down"].append({"file": R["_file"], "seed": R["seed"], "commit": R["stamp_end"]["code_commit"], "licence_end": R["licence_end"]["type"],
                            "exp1": exp1_checks(R), "exp2": exp2_checks(R, "down"), "exp3": exp3_checks(R), "exp4": exp4_checks(R),
                            "second_start_trial": R["second_start_trial"]["body"], "start_trial_first": R["start_trial"]["body"]})
    for R in load(d, "up"):
        if R.get("status") != "complete":
            res["up"].append({"file": R["_file"], "status": R.get("status"), "error": R.get("error")})
            continue
        res["up"].append({"file": R["_file"], "seed": R["seed"], "ttl": R["ttl"], "commit": R["stamp_end"]["code_commit"], "exp2": exp2_checks(R, "up"),
                          "moment": R.get("exp1_moment"), "licence_end": R["licence_end"]["type"]})
    res["aggregate"] = aggregate(d, res)
    if len(sys.argv) > 2:
        json.dump(res, open(sys.argv[2], "w"), indent=1, sort_keys=True)
    print(json.dumps(res["aggregate"], indent=1, sort_keys=True)[:6000])


if __name__ == "__main__":
    main()
