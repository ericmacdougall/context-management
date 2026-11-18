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
_HTTP = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*: HTTP (\d{3}): ")   # the adapter's own prefix only (review 13 audit: anchored)
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
            m = _HTTP.match(r[7])
            o = evidence.Outcome.of(int(m.group(1)), r[7]) if m and 100 <= int(m.group(1)) <= 599 else None
        out.append(WorkRow(i, t0, t1, r[2], bool(r[4]), r[5], o, r[3], ids, (not r[4]) and "non-compliant" in r[7]))
    return tuple(out)


# ---- derived figures, computed only from typed rows -------------------------------------------------------------------------------
def moments(rows, t_resp, features=LICENCE_PROBES + ("dls_search",), window=None, direction="down", t_req=None):
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
            # review 13 audit (EXP1-T2 "polled ... across the start_basic call"): the probe answered before the call, it was not
            # already refused before the observation window (a refusal on trial is not dated by the transition), and every poll after
            # the first refusal observed it (a missing key, an exception row or a NOT_OBSERVED label is a hole in "no success after")
            "success_before_request": None if t_req is None else sum(1 for r, c in seq if r.t1 < t_req and c.success),
            "refusals_before_window": sum(1 for r, c in seq if r.t1 < t_resp - 0.5 and c.outcome is not None
                                          and c.outcome.case in evidence.REFUSALS),
            "unobserved_after_first_refusal": None if any_ref is None else sum(
                1 for r in P if r.seq > any_ref.seq and (r.cell(f) is None or r.cell(f).label in NOT_OBSERVED)),
            "dls_returned_counts_before": sorted({c.extra[0] for r, c in seq if r.t1 < t_resp and c.label == "AVAILABLE" and c.extra})
            if f == "dls_search" else None,
            "n": len(seq)}
        if f == "dls_search":
            out[f]["docs_after_transition"] = _docs_after(seq, t_resp)
    if direction == "down":   # reported for the cadence LIMIT (EXP1-T2.cadence): the recorder sleeps 50 ms BETWEEN polls
        starts = [r.t0 for r in P]
        gaps = sorted(b - a for a, b in zip(starts, starts[1:]))
        for f in out:
            if "first_refusal" in out[f]:
                out[f]["poll_period_median_s"] = gaps[len(gaps) // 2] if gaps else None
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


PHASE_ARMS = {"auto": ("auto", "lin"), "lex": ("lex",), "vec": ("vec",)}


def phases(W, t_req, t_resp, arms):
    """Review 13 audit ("no evidence is not no violation"): requests of each arm group that completed before the transition request
    and that started at or after its response -> {group: {before, after}}. A gate that judges behaviour across a transition needs
    both to be positive."""
    return {g: {"before": sum(1 for w in W if w.arm in a and w.t1 < t_req), "after": sum(1 for w in W if w.arm in a and w.t0 >= t_resp)}
            for g, a in arms.items()}


def availability(W, t_req=None, t_resp=None):
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
            "T2_longest_consecutive_auto_failure_s": consecutive_auto_failure(W),
            "T2_requests_by_phase": None if t_req is None else phases(W, t_req, t_resp, PHASE_ARMS)}


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
    # review 13 audit: a latch "flips" only from the other label. Every auto answer completed before the transition request must carry
    # the pre-transition label (down: backend, up: client), and there must be at least one; otherwise the flip time measures nothing.
    pre = [w for w in auto if w.t1 < t_req]
    before = {"n": len(pre), "other_label": sum(1 for w in pre if w.label != ("backend" if direction == "down" else "client"))}
    return dict(_flip(auto, t_req, t_resp, direction), T3_before_transition=before)


def _flip(auto, t_req, t_resp, direction):
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
    request) and after it (started at or after the response: review 13 F31). Each post-transition figure records where its window starts
    (start_rel_resp_s) so a figure computed over another window does not parse."""
    ctl = [w for w in W if w.arm == "ctl"]
    case = lambda ws: sum(1 for w in ws if w.outcome is not None and w.outcome.case == "LICENCE_REFUSAL")
    if direction == "down":
        late = [w for w in ctl if w.t0 >= t_resp + 1.0]
        ok = sum(1 for w in late if w.ok)
        return {"T5_control_after_1s": {"n": len(late), "licence_failures": _lic(late), "other_failures": len(late) - ok - _lic(late),
                                        "success": ok, "fail_fraction": (len(late) - ok) / max(1, len(late)),
                                        "licence_refusals": case(late), "start_rel_resp_s": 1.0}}
    # review 13 F31: THRESHOLDS addendum C "after the trial ... every control request succeeds" has no grace interval (the "after the
    # first 1 s" of line 34 is the DOWN control's). Every control request STARTED at or after the start_trial response counts.
    pre, post = [w for w in ctl if w.t1 < t_req], [w for w in ctl if w.t0 >= t_resp]
    return {"T5_control_before_start_trial": {"n": len(pre), "failures": sum(1 for w in pre if not w.ok), "licence_failures": _lic(pre),
                                              "licence_refusals": case(pre)},
            "T5_control_after_trial": {"n": len(post), "success": sum(1 for w in post if w.ok), "start_rel_resp_s": 0.0}}


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
    by_label = {"client": 0, "backend": 0}
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
        by_label[w.label] += 1
        if not good:
            wrong += 1
            if len(examples) < 5:
                examples.append({"arm": w.arm, "qi": w.qi, "fusion": w.label, "ids": list(w.ids)})
    # review 13 audit: THRESHOLDS EXP2-T1 "for every response labelled ... backend": the control readers (strategy=backend, rrf) answer
    # backend-labelled responses too; they are compared with the same rrf reference and counted apart (examined stays auto+lin, so the
    # 20,000 floor is not met by adding them)
    ctl = [w for w in W if w.arm == "ctl" and w.ok]
    ctl_ref = [w for w in ctl if w.qi in rrf]
    brk_client = [w for w in W if w.arm == "brk" and w.ok and w.label == "client"]
    flagged = sum(1 for w in brk_client if not legal(w))
    brk_backend = [w for w in W if w.arm == "brk" and w.ok and w.label == "backend" and w.qi in rrf]
    return {"T1_examined": ex, "T1_mismatches": wrong, "T1_examples": examples, "T1_backend_without_oracle": without,
            "T1_examined_by_label": by_label,
            "T1_control_readers": {"checked": len(ctl_ref), "wrong": sum(1 for w in ctl_ref if list(w.ids) != list(rrf[w.qi])),
                                   "without_oracle": len(ctl) - len(ctl_ref)},
            "T1_control_broken_fallback": {"client_labelled": len(brk_client), "flagged_wrong": flagged,
                                           "flag_rate": flagged / max(1, len(brk_client)), "backend_labelled_checked": len(brk_backend),
                                           "backend_wrong": sum(1 for w in brk_backend if list(w.ids) != list(rrf[w.qi]))}}


def answers_bound(W):
    """Review 13 audit: an upper bound on EXP2-T1 "examined" from the rows alone (no oracle needed): the successful auto and auto+linear
    answers. Below 20,000 the registered floor is violated whatever the oracle would say."""
    return sum(1 for w in W if w.arm in ("auto", "lin") and w.ok)


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


# ---- EXP2-T4: the write record (review 13 F32) -------------------------------------------------------------------------------------
def classify_sem_failure(code, e):
    """Addendum C: a failed semantic write is 'licence' (a 403 whose error names a non-compliant licence), 'missing_endpoint' (404
    resource_not_found for the inference endpoint) or 'other' (any other 403/404, a missing index, a transport failure). Only the first
    two are loud licensing failures. Through the same Outcome cases as the verdicts (review 8 F13)."""
    text = json.dumps(e).lower() if e else ""
    try:
        o = evidence.Outcome.of(code, e)
    except evidence.Unparsed:
        return "other"
    if o.case == "LICENCE_REFUSAL":
        return "licence"
    if o.case == "NOT_FOUND" and isinstance(e, dict) and e.get("type") == "resource_not_found_exception" and "inference" in text:
        return "missing_endpoint"
    return "other"


def _ids(v, at):
    if type(v) is not list or not all(type(x) is str and x for x in v):
        raise Unparsed(at, "not a list of document ids")
    return set(v)


def parse_writes(raw):
    """The raw write record (exp2.Workload.writes_raw, results/<life>.writes.gz) -> (acked {id: ack time}, failed batches,
    semantic rows (t0, id, status, error, t1 or None), live ids present, semantic ids present). Raises Unparsed on any shape violation."""
    if type(raw) is not dict:
        raise Unparsed(("writes",), "the write record is not an object")
    acked = raw.get("acked")
    if type(acked) is not dict or not all(type(k) is str and k and evidence._real(t) and t >= 1 for k, t in acked.items()):
        raise Unparsed(("writes", "acked"), "not {id: acknowledgement time}")
    failed = raw.get("failed_batches")
    if type(failed) is not list or not all(type(b) is list and len(b) == 3 and evidence._real(b[0]) for b in failed):
        raise Unparsed(("writes", "failed_batches"), "not [[t0, ids, error], ...]")
    for i, b in enumerate(failed):
        _ids(b[1], ("writes", "failed_batches", i))
    sem = []
    rows = raw.get("sem_rows")
    if type(rows) is not list:
        raise Unparsed(("writes", "sem_rows"), "not a list")
    for i, r in enumerate(rows):
        at = ("writes", "sem_rows", i)
        if type(r) is not list or len(r) not in (4, 5) or not (evidence._real(r[0]) and r[0] >= 1) or type(r[1]) is not str \
                or not r[1] or type(r[2]) is not int or not (r[2] == 0 or 100 <= r[2] <= 599):
            raise Unparsed(at, "not [t0, id, HTTP status, error(, t1)]")
        t1 = r[4] if len(r) == 5 else None
        if t1 is not None and not (evidence._real(t1) and t1 >= r[0]):
            raise Unparsed(at, "t1 %r is not a time at or after t0" % (t1,))
        sem.append((float(r[0]), r[1], r[2], r[3], t1))
    if len({r[1] for r in sem}) != len(sem):
        raise Unparsed(("writes", "sem_rows"), "a semantic write id is recorded twice")
    return acked, failed, sem, _ids(raw.get("live_present"), ("writes", "live_present")), _ids(raw.get("sem_present"), ("writes", "sem_present"))


def writes(raw, t_req=None, t_resp=None, direction=None):
    """EXP2-T4 figures from the raw write record (one function for the recorder, exp2.Workload.verify_writes, and the loader).

    Whole life (as before): acknowledged-but-absent ids (plain writers and the semantic writer), the injected-loss control, and every
    semantic failure classified (addendum C). Review 13 F32, THRESHOLDS.md:33 "fails loudly on Basic: every failure carries a 403
    licence error or 404 for a missing endpoint, none acknowledged, none present": the semantic writes issued ON BASIC are selected by
    time and counted separately (`sem_basic`): down, every write STARTED at or after the start_basic response; up, every write
    COMPLETED before the start_trial request (rows recorded without a completion time cannot be placed: None). Writes in flight
    across the transition are only counted (`transition`). `acked_by_phase` is the per-phase witness of the plain writers
    (acknowledged before the transition request, at or after its response). Without transition times both are None."""
    acked, failed, sem, present, sem_present = parse_writes(raw)
    A = set(acked)
    loss_detected = len((A | {"__injected_lost_write__"}) - present) == len(A - present) + 1   # addendum A, as registered
    failed_ids = {i for _, ids, _ in failed for i in ids}
    sem_ack = {r[1] for r in sem if r[2] in (200, 201)}
    sem_fail = [r for r in sem if r[2] not in (200, 201)]   # every write is acked or failed (a 1xx/202 is not silently neither)
    loud = sum(1 for r in sem_fail if classify_sem_failure(r[2], r[3]) != "other")
    out = {"control_injected_loss_detected": loss_detected, "live_present": len(present), "acked": len(A), "acked_absent": len(A - present),
           "present_unacked": len(present - A), "failed_batches": len(failed), "failed_ids_present": len(failed_ids & present),
           "sem_acked": len(sem_ack), "sem_acked_absent": len(sem_ack - sem_present), "sem_present_unacked": len(sem_present - sem_ack),
           "sem_failed": len(sem_fail), "sem_failed_loud_licence_or_missing": loud, "sem_failed_other": len(sem_fail) - loud,
           "sem_failed_present": len({r[1] for r in sem_fail} & sem_present),
           "sem_first_ok_t": min([r[0] for r in sem if r[2] in (200, 201)] or [None]) if sem_ack else None,
           "acked_by_phase": None, "sem_basic": None}
    if t_req is None or t_resp is None or direction not in ("down", "up"):
        return out
    out["acked_by_phase"] = {"before": sum(1 for t in acked.values() if t < t_req), "after": sum(1 for t in acked.values() if t >= t_resp)}
    if direction == "down":
        basic = [r for r in sem if r[0] >= t_resp]
    elif all(r[4] is not None for r in sem):
        basic = [r for r in sem if r[4] < t_req]
    else:
        return out   # up rows without a completion time: the Basic writes cannot be told from those acknowledged on trial
    on = {r[1] for r in basic}
    bfail = [r for r in basic if r[2] not in (200, 201)]
    bloud = sum(1 for r in bfail if classify_sem_failure(r[2], r[3]) != "other")
    out["sem_basic"] = {"attempted": len(basic), "acked": sum(1 for r in basic if r[2] in (200, 201)), "present": len(on & sem_present),
                        "failed": len(bfail), "failed_loud": bloud, "failed_other": len(bfail) - bloud,
                        "transition": sum(1 for r in sem if r[1] not in on and t_req <= r[0] < t_resp)}
    return out
