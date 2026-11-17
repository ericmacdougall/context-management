"""The observation boundary (DESIGN-VERDICTS.md section 5b, review 8 F13).

Every recorded HTTP observation that a verdict depends on is classified here, once, into an evidence.Outcome: by the harness where it is
recorded (exp2.moment_analysis, the EXP2-T5 control counts) and by summarize.load / recompute.py where committed raw files are first
re-read. A derived figure (a refusal timing, a licence-refusal count) is only ever produced next to the Outcome it was selected by, so a
summary cannot claim a refusal that its observation does not show.

Pure functions; no I/O.
"""
import re

import evidence

LICENCE_PROBES = ("rrf", "linear", "semantic_query", "inference_use")
# The refusal each mandatory EXP1-T2 probe must show after start_basic: the licensed features refuse with the licence text (a 403 whose
# body names the non-compliant licence); dls_search, which the DLS user may simply no longer be authorised for, must refuse with a 403
# that is an authorisation refusal, not a licence one (the security feature is then off, not the probe broken).
REQUIRED_REFUSAL = dict({p: "LICENCE_REFUSAL" for p in LICENCE_PROBES}, dls_search="AUTHORISATION_REFUSAL")
_HTTP = re.compile(r"\bHTTP (\d{3})\b")
NOT_OBSERVED = ("SKIP", "UNVERIFIED_PREREQUISITE", "EMPTY_WITNESS")   # mirrors matrix.NOT_OBSERVED (tested)


def outcome_from_cell(cell):
    """A recorded matrix probe cell [class, status, ...] -> Outcome, or None when it is not an HTTP observation.

    The cell keeps no body; its class carries the licence text: lib.classify labels LICENCE_ERROR every 403 with the licence text (and,
    before review 8, also any 4xx/5xx with it). Outcome cases depend on the text only for a 403, so both labellings give the same case."""
    if not isinstance(cell, (list, tuple)) or len(cell) < 2 or type(cell[0]) is not str:
        return None
    try:
        return evidence.Outcome.of(cell[1], cell[0] == "LICENCE_ERROR")
    except evidence.Unparsed:
        return None


def outcome_from_row(row):
    """A recorded workload row [t0, t1, arm, qi, ok, label, ids, errtext] -> Outcome, or None when a failure's text names no HTTP status
    (a client-side exception: not an HTTP observation)."""
    if row[4] is True:
        return evidence.Outcome.of(200)
    m = _HTTP.search(row[7] if isinstance(row[7], str) else "")
    if not m:
        return None
    try:
        return evidence.Outcome.of(int(m.group(1)), row[7])
    except evidence.Unparsed:
        return None


def is_licence_refusal(row):
    o = outcome_from_row(row)
    return o is not None and o.case == "LICENCE_REFUSAL"


def moments(rows, t_resp, features=LICENCE_PROBES + ("dls_search",)):
    """Down direction of EXP1-T2 from poll rows ({"_t1": end time, probe: cell}): per probe the first observation, from t_resp - 0.5 s,
    whose Outcome is the probe's REQUIRED_REFUSAL, recorded as first_refusal {status, licence_text, rel_resp_s} with the counts the verdict
    and the reports use. A probe whose required refusal never appears has first_refusal None (the verdict is then UNVERIFIED)."""
    out = {}
    for f in features:
        seq = [(r["_t1"], r.get(f)) for r in rows if f in r]
        if not seq or all(x[1] and x[1][0] in NOT_OBSERVED for x in seq):
            out[f] = {"skipped": True, "unverified": True, "n": len(seq), "classes": sorted({x[1][0] for x in seq if x[1]})}
            continue
        want = REQUIRED_REFUSAL[f]
        sel = None
        for t, v in seq:
            o = outcome_from_cell(v)
            if t >= t_resp - 0.5 and o is not None and o.case == want:
                sel = (t, o)
                break
        first_t = sel[0] if sel else None
        out[f] = {
            "first_refusal": None if sel is None else {"status": sel[1].status, "licence_text": sel[1].licence_text, "rel_resp_s": first_t - t_resp},
            "first_refusal_rel_resp_s": None if sel is None else first_t - t_resp,
            "first_refusal_status": None if sel is None else sel[1].status,
            "success_before_transition": sum(1 for t, v in seq if t < t_resp and v[0] == "AVAILABLE"),
            "success_after_first_refusal": sum(1 for t, v in seq if first_t is not None and t > first_t and v[0] == "AVAILABLE"),
            "dls_returned_counts_before": sorted({v[2] for t, v in seq if t < t_resp and v[0] == "AVAILABLE" and len(v) > 2})
            if f == "dls_search" else None,
            "n": len(seq)}
    return out


def control_counts(rows, keep):
    """EXP2-T5 control rows selected by keep(row) -> {n, licence_refusals}: refusals counted by Outcome case, not by error-text substring."""
    ctl = [r for r in rows if r[2] == "ctl" and keep(r)]
    return {"n": len(ctl), "licence_refusals": sum(1 for r in ctl if is_licence_refusal(r))}
