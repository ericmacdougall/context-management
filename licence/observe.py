"""The observation boundary (DESIGN-VERDICTS.md sections 3b and 3c; reviews 8 F13 and 9 F17, F18).

Raw rows are the edge. A poll stream (EXP1-T2) and a workload stream (EXP2) are parsed into typed rows before anything is selected or
counted: finite epoch times, t0 <= t1, inside the run window, t1 non-decreasing, polls not overlapping, a closed set of arms and cell
labels, every cell's label agreeing with its HTTP status, no duplicated workload row. A stream that does not parse raises
evidence.Unparsed; the evidence that depends on it is then UNVERIFIED. Every HTTP observation is classified once into an
evidence.Outcome; "after" means a later sequence position.

The same functions run where the harness records (exp2.moment_analysis, exp2 T5 counts) and where summarize.load / recompute.py re-read
the committed raw files.

EXP1-T3 is the adapter's own predicate: adapter_message() builds exactly what ElasticsearchSearch._req puts in its BackendError, and
adapter_detects() runs the real ElasticsearchSearch._is_licence_error on it.

Pure functions; no I/O.
"""
import collections
import dataclasses
import json
import os
import re
import sys

import evidence

LICENCE_PROBES = ("rrf", "linear", "semantic_query", "inference_use")
# The refusal each mandatory EXP1-T2 probe must show after start_basic (its timing): the licensed features refuse with the licence text;
# dls_search, which the DLS user may no longer be authorised for, refuses with a plain 403 (AUTHORISATION_REFUSAL).
REQUIRED_REFUSAL = dict({p: "LICENCE_REFUSAL" for p in LICENCE_PROBES}, dls_search="AUTHORISATION_REFUSAL")
_HTTP = re.compile(r"\bHTTP (\d{3})\b")
NOT_OBSERVED = ("SKIP", "UNVERIFIED_PREREQUISITE", "EMPTY_WITNESS")   # mirrors matrix.NOT_OBSERVED (tested)
LABELS = ("AVAILABLE", "LICENCE_ERROR", "OTHER_ERROR", "TRANSPORT") + NOT_OBSERVED
ARMS = ("auto", "lin", "lex", "vec", "ctl", "brk")
READ_ARMS = ("auto", "lin", "lex", "vec")
Unparsed = evidence.Unparsed


# ---- typed raw rows -------------------------------------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Cell:
    label: str
    outcome: object      # evidence.Outcome, or None for a NOT_OBSERVED label
    extra: tuple         # further recorded values (dls_search: the returned count)

    @property
    def success(self):   # any 2xx answer, whatever its label (an EMPTY_WITNESS 200 after a refusal is still an answer)
        return self.outcome is not None and self.outcome.case == "SUCCESS"


@dataclasses.dataclass(frozen=True)
class PollRow:
    seq: int
    t0: float
    t1: float
    cells: tuple         # ((probe, Cell), ...)

    def cell(self, probe):
        return dict(self.cells).get(probe)


@dataclasses.dataclass(frozen=True)
class WorkRow:
    seq: int
    t0: float
    t1: float
    arm: str
    ok: bool
    label: object        # fusion label: a member of LABELS_BY_ARM[arm] for a success, None for a failure
    outcome: object      # SUCCESS for ok rows; else the Outcome named by the error text, or None (no HTTP status: a client exception)
    qi: int = 0          # query index
    ids: tuple = ()      # returned ids (a success), () for a failure
    licence_text: bool = False   # a failure whose error text contains "non-compliant" (addendum C's licence failure)


def _time(v, at, window):
    if not (evidence._real(v) and v >= 1):
        raise Unparsed(at, "%r is not a finite epoch time" % (v,))
    if window and not (window[0] <= v <= window[1]):
        raise Unparsed(at, "%r outside the run window [%s, %s]" % (v, window[0], window[1]))
    return float(v)


def _label_agrees(label, o):
    if label == "AVAILABLE":
        return o.case == "SUCCESS"
    if label == "TRANSPORT":
        return o.case == "TRANSPORT_FAILURE"
    if label == "LICENCE_ERROR":   # 403 with the licence text (and, in lives recorded before review 8, any >= 400 with it)
        return o.status >= 400
    if label == "OTHER_ERROR":
        return o.case not in ("SUCCESS", "TRANSPORT_FAILURE", "LICENCE_REFUSAL")
    return True                    # NOT_OBSERVED labels carry any status (or none)


def parse_cell(v, at):
    if type(v) is not list or len(v) < 2 or type(v[0]) is not str or v[0] not in LABELS:
        raise Unparsed(at, "%r is not a probe cell [label, status, ...]" % (v,))
    if v[0] in NOT_OBSERVED:
        try:
            o = evidence.Outcome.of(v[1]) if v[1] is not None else None
        except Unparsed:
            o = None
        return Cell(v[0], o if o is not None and o.case == "SUCCESS" else None, tuple(v[2:]))
    o = evidence.Outcome.of(v[1], v[0] == "LICENCE_ERROR") if type(v[1]) is int else None
    if o is None:
        raise Unparsed(at, "%r: status %r is not an HTTP status" % (v, v[1]))
    if not _label_agrees(v[0], o):
        raise Unparsed(at, "%r: label %s contradicts HTTP %d" % (v, v[0], o.status))
    return Cell(v[0], o, tuple(v[2:]))


def parse_poll(rows, window=None, probes=LICENCE_PROBES + ("dls_search",)):
    """A poll stream (matrix.quick_probes rows with _t0/_t1) -> tuple of PollRow. Raises Unparsed on any row or ordering violation."""
    if type(rows) is not list or not rows:
        raise Unparsed(("poll",), "no poll rows")
    out, prev = [], None
    for i, r in enumerate(rows):
        at = ("poll", i)
        if type(r) is not dict:
            raise Unparsed(at, "a poll row is not an object")
        t0, t1 = _time(r.get("_t0"), at + ("_t0",), window), _time(r.get("_t1"), at + ("_t1",), window)
        if t0 > t1:
            raise Unparsed(at, "_t0 %r after _t1 %r" % (t0, t1))
        if prev is not None and t0 < prev.t1:
            raise Unparsed(at, "poll starts at %r before the previous poll ended at %r (out of order, duplicated or overlapping)" % (t0, prev.t1))
        cells = tuple((p, parse_cell(r[p], at + (p,))) for p in probes if p in r)
        prev = PollRow(i, t0, t1, cells)
        out.append(prev)
    return tuple(out)


_OK = evidence.Outcome.of(200)

# ---- the workload row schema (review 10 F22): every position has a declared domain, and the relations between them are declared ----
# Observed in all 12 committed lives before being enforced: auto/lin/brk successes are labelled client or backend; the ctl arm
# (strategy=backend) only backend; lexical and vector arms "none"; a failure has no label, no ids and error text "<Exception>: ...";
# a success has a list of at most 10 string ids and no error text; qi is a non-negative integer.
LABELS_BY_ARM = {"auto": ("client", "backend"), "lin": ("client", "backend"), "brk": ("client", "backend"), "ctl": ("backend",),
                 "lex": ("none",), "vec": ("none",)}
FAILURE_FORM = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*: ")
MAX_IDS = 10


def _is_int(v):
    return type(v) is int and 0 <= v < evidence.MAX_INT


DOMAINS = {   # domain name -> (predicate over the value alone, description)
    "time": (lambda v: evidence._real(v) and v >= 1, "finite epoch seconds >= 1 (and inside the run window)"),
    "arm": (lambda v: type(v) is str and v in ARMS, "one of %s" % (ARMS,)),
    "query_index": (_is_int, "integer >= 0"),
    "ok": (lambda v: type(v) in (int, bool) and v in (0, 1), "0 or 1"),
    "label": (lambda v: v is None or (type(v) is str and v in {l for ls in LABELS_BY_ARM.values() for l in ls}),
              "None or a fusion label (client, backend, none)"),
    "ids": (lambda v: v is None or (type(v) is list and len(v) <= MAX_IDS and all(type(x) is str and x for x in v)),
            "None or a list of at most 10 non-empty strings"),
    "error_text": (lambda v: v is None or (type(v) is str and FAILURE_FORM.match(v) is not None), "None or '<Exception>: ...'"),
}
RowField = collections.namedtuple("RowField", "index name domain")
ROW_FIELDS = (RowField(0, "t0", "time"), RowField(1, "t1", "time"), RowField(2, "arm", "arm"), RowField(3, "qi", "query_index"),
              RowField(4, "ok", "ok"), RowField(5, "label", "label"), RowField(6, "ids", "ids"), RowField(7, "error", "error_text"))
ROW_RELATIONS = (
    "t0 <= t1; t1 non-decreasing over the stream (completion order); both inside the run window",
    "ok 1: label in LABELS_BY_ARM[arm], ids a list, error None",
    "ok 0: label None, ids None, error text in the failure form",
    "no two rows identical",
)


def _row(r, at, window):
    if type(r) is not list or len(r) != len(ROW_FIELDS):
        raise Unparsed(at, "a workload row is not [%s]" % ", ".join(f.name for f in ROW_FIELDS))
    for f in ROW_FIELDS:   # every field is checked against its domain before anything compares, hashes or stores it
        ok, doc = DOMAINS[f.domain]
        if not ok(r[f.index]):
            raise Unparsed(at + (f.name,), "%r is not %s" % (r[f.index], doc))
    t0, t1 = _time(r[0], at + ("t0",), window), _time(r[1], at + ("t1",), window)
    if t0 > t1:
        raise Unparsed(at, "t0 %r after t1 %r" % (t0, t1))
    arm, ok, label, ids, err = r[2], bool(r[4]), r[5], r[6], r[7]
    if ok and not (label in LABELS_BY_ARM[arm] and type(ids) is list and err is None):
        raise Unparsed(at, "a success on arm %s needs a label in %s, ids and no error text (got %r, %r, %r)"
                       % (arm, LABELS_BY_ARM[arm], label, type(ids).__name__, err))
    if not ok and not (label is None and ids is None and err is not None):
        raise Unparsed(at, "a failure has no label and no ids and carries its error text (got %r, %r, %r)" % (label, ids, err))
    return t0, t1


def parse_work(rows, window=None):
    """A workload stream (ROW_FIELDS per row, in completion order) -> tuple of WorkRow. Raises Unparsed on any field outside its domain
    or any violated ROW_RELATIONS entry."""
    if type(rows) is not list or not rows:
        raise Unparsed(("rows",), "no workload rows")
    out, seen, last = [], set(), None
    for i, r in enumerate(rows):
        at = ("rows", i)
        t0, t1 = _row(r, at, window)
        if last is not None and t1 < last:
            raise Unparsed(at, "completion time %r before the previous row's %r (rows are recorded in completion order)" % (t1, last))
        last = t1
        ids = tuple(r[6]) if r[4] else ()
        key = (t0, t1, r[2], r[3], bool(r[4]), r[5], ids, r[7])   # every element already in a hashable domain
        if key in seen:
            raise Unparsed(at, "duplicated workload row")
        seen.add(key)
        if r[4]:
            o = _OK
        else:
            m = _HTTP.search(r[7])
            o = evidence.Outcome.of(int(m.group(1)), r[7]) if m and 100 <= int(m.group(1)) <= 599 else None
        out.append(WorkRow(i, t0, t1, r[2], bool(r[4]), r[5], o, r[3], ids, (not r[4]) and "non-compliant" in r[7]))
    return tuple(out)


# ---- derived figures, computed only from typed rows -------------------------------------------------------------------------------
def moments(rows, t_resp, features=LICENCE_PROBES + ("dls_search",), window=None, direction="down"):
    """EXP1-T2 from a poll stream. Down, per probe: `first_refusal` {status, licence_text, rel_resp_s} is the first observation (from
    t_resp - 0.5 s) whose Outcome is the probe's REQUIRED_REFUSAL; successes after the first refusal OF ANY KIND are counted by
    sequence position (a probe refused with the wrong kind and then answering is a success after refusal). A stream that does not parse
    gives every probe `stream_error` and no first_refusal. Up: first success at or after t_resp."""
    try:
        P = parse_poll(rows, window, features)
    except Unparsed as e:
        return {f: {"stream_error": str(e), "n": len(rows) if type(rows) is list else 0} for f in features}
    out = {}
    for f in features:
        seq = [(r, r.cell(f)) for r in P if r.cell(f) is not None]
        if not seq or all(c.label in NOT_OBSERVED for _, c in seq):
            out[f] = {"skipped": True, "unverified": True, "n": len(seq), "classes": sorted({c.label for _, c in seq})}
            continue
        if direction != "down":
            first = next((r for r, c in seq if r.t1 >= t_resp and c.label == "AVAILABLE" and c.success), None)
            out[f] = {"first_success_rel_resp_s": None if first is None else first.t1 - t_resp, "n": len(seq)}
            continue
        late = [(r, c) for r, c in seq if r.t1 >= t_resp - 0.5 and c.outcome is not None]
        sel = next(((r, c) for r, c in late if c.outcome.case == REQUIRED_REFUSAL[f]), None)
        any_ref = next((r for r, c in late if c.outcome.case in evidence.REFUSALS), None)
        o = sel[1].outcome if sel else None
        out[f] = {
            "first_refusal": None if sel is None else {"status": o.status, "licence_text": o.licence_text, "rel_resp_s": sel[0].t1 - t_resp},
            "first_refusal_rel_resp_s": None if sel is None else sel[0].t1 - t_resp,
            "first_refusal_status": None if sel is None else o.status,
            "success_before_transition": sum(1 for r, c in seq if r.t1 < t_resp and c.success),
            "success_after_first_refusal": sum(1 for r, c in seq if any_ref is not None and r.seq > any_ref.seq and c.success),
            "dls_returned_counts_before": sorted({c.extra[0] for r, c in seq if r.t1 < t_resp and c.label == "AVAILABLE" and c.extra})
            if f == "dls_search" else None,
            "n": len(seq)}
        if f == "dls_search":
            out[f]["docs_after_transition"] = _docs_after(seq, t_resp)
    return out


def _docs_after(seq, t_resp):
    """Review 12 F28 (THRESHOLDS EXP1-T4 "returning any document is a FAIL"): documents carried by DLS probe answers that STARTED after
    the start_basic response, read from the recorded status-independent count (matrix.docs_in_payload, the fourth row element). None
    (no witness) when any such row was recorded without it: rows recorded before review 12 have three elements."""
    after = [c for r, c in seq if r.t0 >= t_resp]
    if not after:
        return None
    got = [c.extra[1] if len(c.extra) > 1 else None for c in after]
    if any(type(k) is not int or k < 0 for k in got):
        return None
    return sum(got)


def control_counts(W, keep):
    """EXP2-T5 control rows (typed WorkRow) selected by keep(row) -> {n, licence_refusals}, refusals by Outcome case."""
    ctl = [w for w in W if w.arm == "ctl" and keep(w)]
    return {"n": len(ctl), "licence_refusals": sum(1 for w in ctl if w.outcome is not None and w.outcome.case == "LICENCE_REFUSAL")}


def availability(W):
    """EXP2-T2 as exp2.analyse computes it, from typed rows: failed read requests and failure windows (gap < 1 s merges)."""
    fails = sorted((w for w in W if not w.ok and w.arm in READ_ARMS), key=lambda w: (w.t0, w.t1, w.seq))
    wins, cur = [], None
    for w in fails:
        if cur and w.t0 - cur[1] < 1.0:
            cur[1] = max(cur[1], w.t1)
            cur[2] += 1
        else:
            cur = [w.t0, w.t1, 1]
            wins.append(cur)
    return {"T2_failed_requests": len(fails), "T2_non_control_requests": sum(1 for w in W if w.arm in READ_ARMS),
            "T2_longest_failure_window_s": max([x[1] - x[0] for x in wins] or [0.0]), "T2_failure_windows": wins[:10],
            "T2_longest_consecutive_auto_failure_s": consecutive_auto_failure(W)}


AUTO_ARMS = ("auto", "lin")   # strategy=auto readers: the 8 auto and the 2 auto+linear readers (THRESHOLDS EXP2-T2, "any thread")


def consecutive_auto_failure(W):
    """THRESHOLDS EXP2-T2 as registered (review 12 closes the review-10 limit): the longest run of consecutive failed auto requests by
    time, any thread. Auto requests in start order; a run is a maximal sequence of failed ones with no successful auto request between
    them; its length is from the first start to the latest end. 0.0 when no auto request failed."""
    longest, run = 0.0, None
    for w in sorted((w for w in W if w.arm in AUTO_ARMS), key=lambda w: (w.t0, w.t1, w.seq)):
        if w.ok:
            if run:
                longest = max(longest, run[1] - run[0])
            run = None
        elif run is None:
            run = [w.t0, w.t1]
        else:
            run[1] = max(run[1], w.t1)
    if run:
        longest = max(longest, run[1] - run[0])
    return longest


def flip(W, t_req, t_resp, direction):
    """EXP2-T3 as exp2.analyse computes it, from typed rows (successful auto answers in start order)."""
    auto = sorted((w for w in W if w.arm == "auto" and w.ok), key=lambda w: (w.t0, w.t1, w.seq))
    if direction == "down":
        first = next((w for w in auto if w.label == "client" and w.t1 >= t_req), None)
        return {"T3_flip_latency_s": None if first is None else first.t1 - t_resp,
                "T3_backend_after_first_client": sum(1 for w in auto if first and w.label == "backend" and w.t0 > first.t1)}
    first = next((w for w in auto if w.label == "backend" and w.t1 >= t_resp), None)
    return {"T3_flip_latency_s": None if first is None else first.t1 - t_resp,
            "T3_client_after_first_backend": sum(1 for w in auto if first and w.label == "client" and w.t0 > first.t1)}


def control(W, t_req, t_resp, direction):
    """EXP2-T5 from typed rows, both directions (review 10 F21: the post-transition figures too). A licence failure is a failed control
    request whose error text contains "non-compliant" (addendum C); `licence_refusals` counts the Outcome case LICENCE_REFUSAL (a 403
    with that text). Down: requests started >= 1 s after the start_basic response. Up: before start_trial (completed before the
    request) and after it (started >= 1 s after the response)."""
    ctl = [w for w in W if w.arm == "ctl"]
    case = lambda ws: sum(1 for w in ws if w.outcome is not None and w.outcome.case == "LICENCE_REFUSAL")
    if direction == "down":
        late = [w for w in ctl if w.t0 >= t_resp + 1.0]
        ok = sum(1 for w in late if w.ok)
        return {"T5_control_after_1s": {"n": len(late), "licence_failures": _lic(late), "other_failures": len(late) - ok - _lic(late),
                                        "success": ok, "fail_fraction": (len(late) - ok) / max(1, len(late)),
                                        "licence_refusals": case(late)}}
    pre, post = [w for w in ctl if w.t1 < t_req], [w for w in ctl if w.t0 >= t_resp + 1.0]
    return {"T5_control_before_start_trial": {"n": len(pre), "failures": sum(1 for w in pre if not w.ok), "licence_failures": _lic(pre),
                                              "licence_refusals": case(pre)},
            "T5_control_after_trial": {"n": len(post), "success": sum(1 for w in post if w.ok)}}


def _lic(ws):
    return sum(1 for w in ws if not w.ok and w.licence_text)


def correctness(W, legs, rrf, lin):
    """EXP2-T1 from typed rows and the oracle (legs, rrf, lin: dicts keyed by integer query index), exactly as exp2.analyse records it.
    A client answer is correct when it is a legal top-10 of the client RRF over its legs; a backend answer when it equals the recorded
    backend reference. A backend answer with no reference is not examined (counted in T1_backend_without_oracle); a client answer with
    no legs cannot be checked and counts as a mismatch (fail closed)."""
    import oracle
    legal = lambda w: w.qi in legs and oracle.valid_topk(list(w.ids), list(legs[w.qi]), 10)
    ex = wrong = without = 0
    examples = []
    for w in W:
        if w.arm not in ("auto", "lin") or not w.ok:
            continue
        if w.label == "client":
            good = legal(w)
        else:
            ref = (rrf if w.arm == "auto" else lin).get(w.qi)
            if ref is None:
                without += 1
                continue
            good = list(w.ids) == list(ref)
        ex += 1
        if not good:
            wrong += 1
            if len(examples) < 5:
                examples.append({"arm": w.arm, "qi": w.qi, "fusion": w.label, "ids": list(w.ids)})
    brk_client = [w for w in W if w.arm == "brk" and w.ok and w.label == "client"]
    flagged = sum(1 for w in brk_client if not legal(w))
    brk_backend = [w for w in W if w.arm == "brk" and w.ok and w.label == "backend" and w.qi in rrf]
    return {"T1_examined": ex, "T1_mismatches": wrong, "T1_examples": examples, "T1_backend_without_oracle": without,
            "T1_control_broken_fallback": {"client_labelled": len(brk_client), "flagged_wrong": flagged,
                                           "flag_rate": flagged / max(1, len(brk_client)), "backend_labelled_checked": len(brk_backend),
                                           "backend_wrong": sum(1 for w in brk_backend if list(w.ids) != list(rrf[w.qi]))}}


def oracle_from_file(O):
    """The saved oracle (results/<life>.oracle.gz: string query keys) -> (legs, rrf, lin) keyed by integer query index."""
    conv = lambda d: {int(k): v for k, v in d.items()}
    return conv(O["legs"]), conv(O["rrf"]), conv(O["linear"])


# ---- EXP1-T3: the adapter's own view of an error ------------------------------------------------------------------------------------
def adapter_message(code, raw):
    """Exactly the text ElasticsearchSearch._req raises for an HTTP error: "HTTP <code>: " + the first 400 bytes, decoded with replace."""
    return "HTTP %d: %s" % (code, (raw or b"")[:400].decode("utf-8", "replace"))


def adapter_detects(message):
    """The REAL adapter detector on the message (memcore.adapters.elasticsearch_search.ElasticsearchSearch._is_licence_error)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.append(root)
    from memcore.adapters.elasticsearch_search import ElasticsearchSearch
    from memcore.ports import BackendError
    return bool(ElasticsearchSearch._is_licence_error(BackendError(message)))
